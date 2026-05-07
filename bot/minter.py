"""Mint transaction building, gas configuration and broadcast logic."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3Exception

from .chains import ChainConfig, resolve_broadcast_rpcs
from .eligibility import WalletEligibility
from .logger import get_logger
from .seadrop import (
    MintPhase,
    encode_mint_params,
    encode_proof,
    encode_salt,
    encode_signature,
    get_seadrop_contract,
)
from .utils import (
    fmt_eth,
    fmt_gwei,
    gwei_to_wei,
    short_addr,
)
from .wallet import Wallet, build_web3


log = get_logger("minter")


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------

@dataclass
class GasSettings:
    """User-provided gas overrides.

    Attributes:
      mode: 'eip1559' | 'legacy' | 'auto'. 'auto' picks eip1559 when supported.
      max_fee_gwei: hard cap for EIP-1559 maxFeePerGas. 0 means auto.
      priority_fee_gwei: tip for EIP-1559. 0 means auto (1.5 gwei default).
      gas_price_gwei: legacy gas price override. 0 means auto.
      multiplier: scales auto-detected fees (e.g. 1.25 = +25%).
      gas_limit: hard override of gas limit. 0 means auto-estimate.
      escalation_factor: per-retry gas bump (1.25 = +25% per attempt).
                         Each retry replaces the pending tx with higher gas
                         to push it ahead in the mempool. Min 1.10 enforced
                         to satisfy Ethereum's "replace-by-fee" rule.
      max_escalation_cap: absolute upper bound (gwei) to prevent runaway
                          gas costs after many retries. 0 disables the cap.
    """
    mode: str = "auto"
    max_fee_gwei: float = 0.0
    priority_fee_gwei: float = 0.0
    gas_price_gwei: float = 0.0
    multiplier: float = 1.25
    gas_limit: int = 0
    escalation_factor: float = 1.25
    max_escalation_cap: float = 0.0


@dataclass
class MintConfig:
    """Per-execution mint settings."""
    quantity: int = 1
    fee_recipient_override: str = ""
    max_retries: int = 3
    retry_delay_ms: int = 1500
    parallel_wallets: bool = True
    receipt_timeout: int = 180
    # Speed-tuning options (FCFS sniping)
    prebuild_tx: bool = True             # build tx ahead of fire time
    prebuild_lead_seconds: int = 8       # how early to build tx (T-N seconds)
    multi_rpc_broadcast: bool = True     # send to multiple RPCs in parallel
    broadcast_rpc_count: int = 4         # how many RPCs to fan-out to
    extra_broadcast_rpcs: List[str] = field(default_factory=list)


@dataclass
class MintResult:
    wallet: str
    label: str
    phase: str
    success: bool
    tx_hash: str = ""
    quantity: int = 0
    cost_wei: int = 0
    error: str = ""
    receipt_status: Optional[int] = None
    explorer_url: str = ""


# ---------------------------------------------------------------------------
# Gas helpers
# ---------------------------------------------------------------------------

def _scaled_int(value: int, multiplier: float) -> int:
    if multiplier <= 0:
        return value
    return int(Decimal(value) * Decimal(str(multiplier)))


def build_gas_fields(w3: Web3, chain: ChainConfig, gas: GasSettings) -> Dict[str, int]:
    """Return a dict of gas-related fields to merge into a transaction."""
    use_eip1559 = chain.supports_eip1559 and gas.mode != "legacy"

    if gas.mode == "legacy":
        use_eip1559 = False

    out: Dict[str, int] = {}

    if use_eip1559:
        try:
            base_fee = w3.eth.get_block("latest").get("baseFeePerGas") or 0
        except Exception:
            base_fee = 0

        # priority fee
        if gas.priority_fee_gwei > 0:
            priority = gwei_to_wei(gas.priority_fee_gwei)
        else:
            try:
                priority = int(w3.eth.max_priority_fee)
            except Exception:
                priority = gwei_to_wei(1.5)

        # max fee
        if gas.max_fee_gwei > 0:
            max_fee = gwei_to_wei(gas.max_fee_gwei)
        else:
            max_fee = _scaled_int(int(base_fee) * 2 + priority, gas.multiplier)

        if max_fee < priority:
            max_fee = priority + gwei_to_wei(1)

        out["maxFeePerGas"] = max_fee
        out["maxPriorityFeePerGas"] = priority
        out["type"] = 2
        log.info(
            "Gas (EIP-1559): baseFee=%s priority=%s maxFee=%s",
            fmt_gwei(int(base_fee)),
            fmt_gwei(priority),
            fmt_gwei(max_fee),
        )
    else:
        if gas.gas_price_gwei > 0:
            price = gwei_to_wei(gas.gas_price_gwei)
        else:
            try:
                price = int(w3.eth.gas_price)
                price = _scaled_int(price, gas.multiplier)
            except Exception:
                price = gwei_to_wei(20)
        out["gasPrice"] = price
        log.info("Gas (legacy): %s", fmt_gwei(price))

    return out


def apply_gas_escalation(
    tx: Dict[str, Any],
    gas: GasSettings,
    *,
    attempt: int,
    label: str = "",
) -> Dict[str, Any]:
    """Bump gas fields in-place for retry attempts.

    Each retry multiplies the existing gas by `escalation_factor`. Ethereum's
    replace-by-fee rule requires +10% min over the pending tx, so the factor
    is clamped to 1.10 minimum. The bumped tx uses the SAME nonce to replace
    the in-flight one in the mempool.

    Returns the modified tx dict (also mutates input for convenience).
    """
    if attempt <= 1:
        return tx

    factor = max(1.10, float(gas.escalation_factor or 1.25))
    # Compound: attempt 2 = factor^1, attempt 3 = factor^2, etc.
    total = factor ** (attempt - 1)

    cap_wei = gwei_to_wei(gas.max_escalation_cap) if gas.max_escalation_cap > 0 else 0

    if "maxFeePerGas" in tx:
        old_max = int(tx["maxFeePerGas"])
        old_pri = int(tx.get("maxPriorityFeePerGas", 0))
        new_max = _scaled_int(old_max, total / (factor ** (attempt - 2))) if attempt > 2 else _scaled_int(old_max, factor)
        new_pri = _scaled_int(old_pri, total / (factor ** (attempt - 2))) if attempt > 2 else _scaled_int(old_pri, factor)
        if cap_wei > 0:
            new_max = min(new_max, cap_wei)
            new_pri = min(new_pri, cap_wei)
        if new_max < new_pri:
            new_max = new_pri + gwei_to_wei(1)
        tx["maxFeePerGas"] = new_max
        tx["maxPriorityFeePerGas"] = new_pri
        log.info(
            "[%s] gas escalation #%d: priority %s -> %s, maxFee %s -> %s",
            label, attempt - 1,
            fmt_gwei(old_pri), fmt_gwei(new_pri),
            fmt_gwei(old_max), fmt_gwei(new_max),
        )
    elif "gasPrice" in tx:
        old = int(tx["gasPrice"])
        new = _scaled_int(old, factor)
        if cap_wei > 0:
            new = min(new, cap_wei)
        tx["gasPrice"] = new
        log.info(
            "[%s] gas escalation #%d (legacy): %s -> %s",
            label, attempt - 1, fmt_gwei(old), fmt_gwei(new),
        )
    return tx


def estimate_gas_limit(
    w3: Web3,
    contract_call,
    *,
    sender: str,
    value: int,
    gas_settings: GasSettings,
) -> int:
    """Estimate the gas limit for a contract function call with safety buffer."""
    if gas_settings.gas_limit and gas_settings.gas_limit > 0:
        return int(gas_settings.gas_limit)
    try:
        est = contract_call.estimate_gas({"from": sender, "value": value})
        # +20% safety buffer
        return int(est + max(int(est * 0.2), 5000))
    except Exception as exc:
        log.warning("estimate_gas failed: %s — falling back to 350,000", exc)
        return 350_000


# ---------------------------------------------------------------------------
# Transaction builders
# ---------------------------------------------------------------------------

def _ensure_address(addr: str) -> str:
    return Web3.to_checksum_address(addr)


def build_public_mint_tx(
    w3: Web3,
    chain: ChainConfig,
    nft_contract: str,
    phase: MintPhase,
    wallet: Wallet,
    quantity: int,
    fee_recipient: str,
    gas_settings: GasSettings,
) -> Tuple[Dict, int]:
    """Build a SeaDrop `mintPublic` tx dict (unsigned). Returns (tx, value)."""
    seadrop = get_seadrop_contract(w3, chain.seadrop)
    minter_address = _ensure_address(wallet.address)
    nft_address = _ensure_address(nft_contract)
    fee = _ensure_address(fee_recipient or phase.fee_recipient)

    value = int(phase.mint_price_wei) * int(quantity)

    fn = seadrop.functions.mintPublic(
        nft_address,
        fee,
        minter_address,  # minterIfNotPayer (same wallet pays + receives)
        int(quantity),
    )

    gas_limit = estimate_gas_limit(
        w3, fn, sender=minter_address, value=value, gas_settings=gas_settings
    )

    nonce = w3.eth.get_transaction_count(minter_address, "pending")
    tx = fn.build_transaction(
        {
            "from": minter_address,
            "value": value,
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": chain.chain_id,
        }
    )
    tx.update(build_gas_fields(w3, chain, gas_settings))
    return tx, value


def build_allowlist_mint_tx(
    w3: Web3,
    chain: ChainConfig,
    nft_contract: str,
    phase: MintPhase,
    wallet: Wallet,
    quantity: int,
    proof: List[str],
    fee_recipient: str,
    gas_settings: GasSettings,
) -> Tuple[Dict, int]:
    """Build a SeaDrop `mintAllowList` tx dict (unsigned)."""
    seadrop = get_seadrop_contract(w3, chain.seadrop)
    minter_address = _ensure_address(wallet.address)
    nft_address = _ensure_address(nft_contract)
    fee = _ensure_address(fee_recipient or phase.fee_recipient)

    value = int(phase.mint_price_wei) * int(quantity)

    mint_params_tuple = encode_mint_params(phase)
    encoded_proof = encode_proof(proof)

    fn = seadrop.functions.mintAllowList(
        nft_address,
        fee,
        minter_address,
        int(quantity),
        mint_params_tuple,
        encoded_proof,
    )

    gas_limit = estimate_gas_limit(
        w3, fn, sender=minter_address, value=value, gas_settings=gas_settings
    )

    nonce = w3.eth.get_transaction_count(minter_address, "pending")
    tx = fn.build_transaction(
        {
            "from": minter_address,
            "value": value,
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": chain.chain_id,
        }
    )
    tx.update(build_gas_fields(w3, chain, gas_settings))
    return tx, value


def build_signed_mint_tx(
    w3: Web3,
    chain: ChainConfig,
    nft_contract: str,
    phase: MintPhase,
    wallet: Wallet,
    quantity: int,
    salt: str,
    signature: str,
    fee_recipient: str,
    gas_settings: GasSettings,
) -> Tuple[Dict, int]:
    """Build a SeaDrop `mintSigned` tx dict (unsigned).

    Used by Guaranteed/FCFS phases on OpenSea Studio drops where eligibility is
    verified via an EIP-712 signature from a backend signer key (instead of a
    merkle proof).
    """
    seadrop = get_seadrop_contract(w3, chain.seadrop)
    minter_address = _ensure_address(wallet.address)
    nft_address = _ensure_address(nft_contract)
    fee = _ensure_address(fee_recipient or phase.fee_recipient)

    value = int(phase.mint_price_wei) * int(quantity)

    mint_params_tuple = encode_mint_params(phase)
    salt_uint = encode_salt(salt)
    sig_bytes = encode_signature(signature)

    fn = seadrop.functions.mintSigned(
        nft_address,
        fee,
        minter_address,
        int(quantity),
        mint_params_tuple,
        salt_uint,
        sig_bytes,
    )

    gas_limit = estimate_gas_limit(
        w3, fn, sender=minter_address, value=value, gas_settings=gas_settings
    )

    nonce = w3.eth.get_transaction_count(minter_address, "pending")
    tx = fn.build_transaction(
        {
            "from": minter_address,
            "value": value,
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": chain.chain_id,
        }
    )
    tx.update(build_gas_fields(w3, chain, gas_settings))
    return tx, value


# ---------------------------------------------------------------------------
# Sign + broadcast
# ---------------------------------------------------------------------------

_NONCE_LOCK = threading.Lock()


def _sign_tx(tx: Dict, private_key: str) -> Tuple[bytes, str]:
    """Sign a tx dict; return (raw_bytes, tx_hash_hex)."""
    signed = Account.sign_transaction(tx, private_key)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction", None)
    if raw is None:  # pragma: no cover
        raise RuntimeError("Signed transaction has no raw payload")
    h = getattr(signed, "hash", None)
    tx_hash = h.hex() if isinstance(h, (bytes, bytearray)) else str(h or "")
    if tx_hash and not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    return bytes(raw), tx_hash


def sign_and_send(w3: Web3, tx: Dict, private_key: str) -> str:
    """Sign and broadcast a transaction via the primary RPC. Returns tx hash."""
    raw, _ = _sign_tx(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(raw)
    return tx_hash.hex() if isinstance(tx_hash, (bytes, bytearray)) else str(tx_hash)


def multi_rpc_send_raw(
    raw: bytes,
    rpc_urls: List[str],
    *,
    timeout: int = 5,
    label: str = "",
) -> Tuple[str, List[str]]:
    """Broadcast a signed raw tx to many RPCs in parallel.

    The transaction hash is deterministic from the signed payload, so all
    successful submissions return the same hash. Network mempool dedups
    duplicates: only one tx is mined, no risk of double-charging.

    Returns (tx_hash, errors). tx_hash is empty if every RPC failed.
    """
    if not rpc_urls:
        return "", ["no rpc urls"]

    results: Dict[str, Any] = {"hash": "", "errors": []}
    lock = threading.Lock()

    def _submit(url: str) -> None:
        try:
            w3_local = build_web3(url, timeout=timeout)
            h = w3_local.eth.send_raw_transaction(raw)
            tx_hash = h.hex() if isinstance(h, (bytes, bytearray)) else str(h)
            with lock:
                if not results["hash"]:
                    results["hash"] = tx_hash
                log.debug("[%s] broadcast OK via %s", label, url)
        except Exception as exc:  # noqa: BLE001 - we capture all
            with lock:
                results["errors"].append(f"{url}: {exc}")
            log.debug("[%s] broadcast fail %s: %s", label, url, exc)

    with ThreadPoolExecutor(max_workers=min(len(rpc_urls), 8)) as pool:
        futures = [pool.submit(_submit, u) for u in rpc_urls]
        for _ in as_completed(futures, timeout=timeout * 2):
            pass

    return results["hash"], results["errors"]


def sign_and_broadcast_multi(
    tx: Dict,
    private_key: str,
    rpc_urls: List[str],
    *,
    label: str = "",
) -> str:
    """Sign tx once, broadcast to many RPCs in parallel. Returns tx hash."""
    raw, expected_hash = _sign_tx(tx, private_key)
    tx_hash, errors = multi_rpc_send_raw(raw, rpc_urls, label=label)
    if not tx_hash:
        raise Web3Exception(
            f"All {len(rpc_urls)} RPC submissions failed. First errors: "
            + "; ".join(errors[:3])
        )
    if expected_hash and tx_hash.lower() != expected_hash.lower():
        # Rare: some RPCs return weird hash format; trust local computation
        log.debug("[%s] hash mismatch: local=%s rpc=%s", label, expected_hash, tx_hash)
        tx_hash = expected_hash
    log.info("[%s] broadcast to %d RPCs (succeeded on %d)",
             label, len(rpc_urls), len(rpc_urls) - len(errors))
    return tx_hash


def wait_for_receipt(w3: Web3, tx_hash: str, timeout: int = 180) -> Optional[Dict]:
    """Poll for a tx receipt; return the receipt dict or None on timeout."""
    deadline = time.time() + max(int(timeout), 5)
    while time.time() < deadline:
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt is not None:
                return dict(receipt)
        except TransactionNotFound:
            pass
        except Web3Exception as exc:
            log.debug("get_transaction_receipt error: %s", exc)
        time.sleep(2)
    return None


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def check_balance(
    w3: Web3, wallet: Wallet, value_wei: int, gas_fields: Dict, gas_limit: int, native_symbol: str
) -> Tuple[bool, str]:
    """Return (ok, reason) - True when wallet can afford value + gas."""
    try:
        balance = w3.eth.get_balance(_ensure_address(wallet.address))
    except Web3Exception as exc:
        return False, f"balance check failed: {exc}"

    if "maxFeePerGas" in gas_fields:
        gas_cost = int(gas_fields["maxFeePerGas"]) * int(gas_limit)
    else:
        gas_cost = int(gas_fields.get("gasPrice", 0)) * int(gas_limit)

    needed = int(value_wei) + gas_cost
    if balance < needed:
        return False, (
            f"insufficient balance ({fmt_eth(balance, native_symbol)} < "
            f"{fmt_eth(needed, native_symbol)} required)"
        )
    return True, ""


# ---------------------------------------------------------------------------
# Main mint runner
# ---------------------------------------------------------------------------

def _explorer_url(chain: ChainConfig, tx_hash: str) -> str:
    base = chain.explorer.rstrip("/") if chain.explorer else ""
    return f"{base}/tx/{tx_hash}" if base else tx_hash


def _build_tx_for_phase(
    w3: Web3,
    chain: ChainConfig,
    nft_contract: str,
    phase: MintPhase,
    wallet: Wallet,
    qty: int,
    eligibility: WalletEligibility,
    fee_recipient: str,
    gas_settings: GasSettings,
) -> Tuple[Dict, int]:
    """Dispatch to the correct tx builder based on phase type. Returns (tx, value)."""
    if phase.is_public:
        return build_public_mint_tx(
            w3, chain, nft_contract, phase, wallet, qty, fee_recipient, gas_settings
        )
    if phase.is_signed:
        if not eligibility.signature:
            raise ValueError("no signed-mint signature for this wallet")
        return build_signed_mint_tx(
            w3, chain, nft_contract, phase, wallet, qty,
            eligibility.salt, eligibility.signature,
            fee_recipient, gas_settings,
        )
    proof = eligibility.proof
    if not proof:
        raise ValueError("no merkle proof for allowlist mint")
    return build_allowlist_mint_tx(
        w3, chain, nft_contract, phase, wallet, qty, proof,
        fee_recipient, gas_settings,
    )


def _resolve_broadcast_urls(
    cfg: MintConfig,
    chain: ChainConfig,
    primary_url: str,
) -> List[str]:
    """Pick the RPC URLs the bot should fan-out to during fire phase."""
    if not cfg.multi_rpc_broadcast:
        return [primary_url] if primary_url else []
    return resolve_broadcast_rpcs(
        chain,
        primary=primary_url,
        extras=cfg.extra_broadcast_rpcs,
        limit=max(1, int(cfg.broadcast_rpc_count)),
    )


def execute_mint_for_wallet(
    w3: Web3,
    chain: ChainConfig,
    nft_contract: str,
    phase: MintPhase,
    wallet: Wallet,
    eligibility: WalletEligibility,
    cfg: MintConfig,
    gas_settings: GasSettings,
) -> MintResult:
    """Execute a single mint for a given wallet+phase pair with retries.

    Speed pipeline:
      1. Pre-build tx (T-prebuild_lead_seconds before phase start) — cached
      2. Gas escalation per retry attempt — bumps priority by escalation_factor
      3. Multi-RPC broadcast — fires raw tx to N RPCs in parallel
    """
    label = wallet.label or short_addr(wallet.address)
    requested_qty = max(1, int(cfg.quantity))
    remaining = max(int(eligibility.remaining_for_wallet), 0)
    qty = max(1, min(requested_qty, remaining)) if remaining > 0 else requested_qty

    fee_recipient = cfg.fee_recipient_override or phase.fee_recipient

    # Pick broadcast URLs once. Primary RPC is whatever the main w3 talks to.
    primary_url = ""
    try:
        primary_url = w3.provider.endpoint_uri  # type: ignore[attr-defined]
    except Exception:
        primary_url = ""
    broadcast_urls = _resolve_broadcast_urls(cfg, chain, primary_url)

    # Pre-build tx if requested. We do this OUTSIDE the retry loop so the
    # initial build cost is paid before fire time. On retries we re-fetch
    # nonce + base_fee since pending-tx replacement may have shifted things.
    cached_tx: Optional[Dict] = None
    cached_value: int = 0
    if cfg.prebuild_tx:
        try:
            with _NONCE_LOCK:
                cached_tx, cached_value = _build_tx_for_phase(
                    w3, chain, nft_contract, phase, wallet, qty, eligibility,
                    fee_recipient, gas_settings,
                )
            log.info(
                "[%s] tx pre-built (qty=%d, value=%s, gas=%d, nonce=%s)",
                label, qty, fmt_eth(cached_value, chain.native_symbol),
                int(cached_tx.get("gas", 0)), cached_tx.get("nonce"),
            )
        except ValueError as exc:
            return MintResult(
                wallet=wallet.address, label=label, phase=phase.name,
                success=False, error=str(exc), quantity=qty,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] pre-build failed (%s) — will build at fire time", label, exc)
            cached_tx = None

    last_err = ""
    last_tx_hash = ""
    last_value = 0
    for attempt in range(1, cfg.max_retries + 1):
        try:
            with _NONCE_LOCK:
                if cached_tx is not None and attempt == 1:
                    tx, value = dict(cached_tx), cached_value
                else:
                    # Re-build for retry (nonce + base_fee may have shifted)
                    tx, value = _build_tx_for_phase(
                        w3, chain, nft_contract, phase, wallet, qty, eligibility,
                        fee_recipient, gas_settings,
                    )

                # Gas escalation on retry attempts
                apply_gas_escalation(tx, gas_settings, attempt=attempt, label=label)

                gas_limit = int(tx.get("gas", 0))
                ok, reason = check_balance(
                    w3, wallet, value, tx, gas_limit, chain.native_symbol
                )
                if not ok:
                    return MintResult(
                        wallet=wallet.address, label=label, phase=phase.name,
                        success=False, error=reason, quantity=qty, cost_wei=value,
                    )

                log.info(
                    "[%s] Sending %s mint (qty=%d, value=%s) attempt %d/%d",
                    label, phase.name, qty, fmt_eth(value, chain.native_symbol),
                    attempt, cfg.max_retries,
                )
                last_value = value

                # Multi-RPC broadcast (or single send if disabled)
                if cfg.multi_rpc_broadcast and len(broadcast_urls) > 1:
                    tx_hash = sign_and_broadcast_multi(
                        tx, wallet.private_key, broadcast_urls, label=label,
                    )
                else:
                    tx_hash = sign_and_send(w3, tx, wallet.private_key)

            tx_hash = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash
            last_tx_hash = tx_hash
            log.info("[%s] tx submitted: %s", label, tx_hash)

            receipt = wait_for_receipt(w3, tx_hash, cfg.receipt_timeout)
            if receipt is None:
                # Receipt timeout: tx may still confirm later, but treat as
                # non-final for retry decision.
                last_err = "receipt timeout (tx may still confirm)"
                if attempt >= cfg.max_retries:
                    return MintResult(
                        wallet=wallet.address, label=label, phase=phase.name,
                        success=False, error=last_err,
                        tx_hash=tx_hash, quantity=qty, cost_wei=value,
                        explorer_url=_explorer_url(chain, tx_hash),
                    )
                # else fall through to retry with escalated gas
            else:
                status = int(receipt.get("status", 0))
                if status == 1:
                    log.info("[%s] mint succeeded in block %s",
                             label, receipt.get("blockNumber"))
                    return MintResult(
                        wallet=wallet.address, label=label, phase=phase.name,
                        success=True, tx_hash=tx_hash, quantity=qty, cost_wei=value,
                        receipt_status=status,
                        explorer_url=_explorer_url(chain, tx_hash),
                    )
                last_err = f"reverted (status=0) in block {receipt.get('blockNumber')}"
                log.error("[%s] %s", label, last_err)

        except ContractLogicError as exc:
            last_err = f"revert: {exc}"
            log.error("[%s] %s", label, last_err)
        except Web3Exception as exc:
            last_err = f"rpc error: {exc}"
            log.warning("[%s] %s", label, last_err)
        except Exception as exc:  # pragma: no cover - defensive
            last_err = f"unexpected: {exc}"
            log.exception("[%s] mint failed: %s", label, exc)

        if attempt < cfg.max_retries:
            delay = max(cfg.retry_delay_ms, 0) / 1000.0
            log.info("[%s] retrying in %.2fs (gas will escalate %.2fx)",
                     label, delay, gas_settings.escalation_factor or 1.25)
            time.sleep(delay)

    return MintResult(
        wallet=wallet.address, label=label, phase=phase.name,
        success=False, error=last_err or "exhausted retries", quantity=qty,
        tx_hash=last_tx_hash, cost_wei=last_value,
        explorer_url=_explorer_url(chain, last_tx_hash) if last_tx_hash else "",
    )


def execute_mint_batch(
    w3: Web3,
    chain: ChainConfig,
    nft_contract: str,
    phase: MintPhase,
    wallets: List[Wallet],
    eligibility_map: Dict[str, WalletEligibility],
    cfg: MintConfig,
    gas_settings: GasSettings,
) -> List[MintResult]:
    """Run mints for many wallets, optionally in parallel."""
    if not wallets:
        return []

    targets: List[Tuple[Wallet, WalletEligibility]] = []
    skipped: List[MintResult] = []
    for w in wallets:
        elig = eligibility_map.get(w.address.lower())
        if elig is None:
            skipped.append(MintResult(
                wallet=w.address, label=w.label or short_addr(w.address),
                phase=phase.name, success=False,
                error="no eligibility record for wallet",
            ))
            continue
        if not elig.eligible:
            skipped.append(MintResult(
                wallet=w.address, label=w.label or short_addr(w.address),
                phase=phase.name, success=False,
                error=f"wallet not eligible: {elig.reason}",
            ))
            continue
        if elig.remaining_for_wallet <= 0:
            skipped.append(MintResult(
                wallet=w.address, label=w.label or short_addr(w.address),
                phase=phase.name, success=False,
                error=f"wallet at max ({elig.already_minted} minted)",
            ))
            continue
        targets.append((w, elig))

    if not targets:
        return skipped

    results: List[MintResult] = list(skipped)

    if not cfg.parallel_wallets or len(targets) == 1:
        for wal, elig in targets:
            results.append(
                execute_mint_for_wallet(
                    w3, chain, nft_contract, phase, wal, elig, cfg, gas_settings
                )
            )
        return results

    # Parallel mode (thread per wallet, capped to 8 concurrent for sanity)
    max_workers = min(len(targets), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                execute_mint_for_wallet,
                w3, chain, nft_contract, phase, wal, elig, cfg, gas_settings,
            ): wal
            for wal, elig in targets
        }
        for fut in as_completed(futures):
            wal = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("[%s] worker crashed: %s", wal.label, exc)
                results.append(MintResult(
                    wallet=wal.address, label=wal.label or short_addr(wal.address),
                    phase=phase.name, success=False,
                    error=f"worker crashed: {exc}",
                ))
    return results
