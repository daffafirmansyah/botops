"""Wallet loading and Web3 client management with RPC fail-over."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List, Optional

from eth_account import Account
from web3 import Web3
from web3.exceptions import Web3Exception

try:  # web3 7.x
    from web3.middleware import ExtraDataToPOAMiddleware as _POA_MIDDLEWARE
except ImportError:  # pragma: no cover - older versions
    try:
        from web3.middleware import geth_poa_middleware as _POA_MIDDLEWARE  # type: ignore
    except ImportError:
        _POA_MIDDLEWARE = None  # type: ignore[assignment]

from .chains import ChainConfig, resolve_rpc
from .logger import get_logger
from .utils import is_hex_address, normalize_pk, short_addr


log = get_logger("wallet")


@dataclass
class Wallet:
    """A single signer with associated metadata."""

    private_key: str
    address: str
    label: str = ""

    def __repr__(self) -> str:
        tag = f" '{self.label}'" if self.label else ""
        return f"Wallet({short_addr(self.address)}{tag})"


# ---------------------------------------------------------------------------
# Loading wallets from file
# ---------------------------------------------------------------------------

def _wallet_from_pk(pk: str, label: str = "") -> Wallet:
    """Build a Wallet by deriving the address from a private key."""
    pk_norm = normalize_pk(pk)
    account = Account.from_key(pk_norm)
    return Wallet(private_key=pk_norm, address=Web3.to_checksum_address(account.address), label=label or "")


def load_wallets(path: str) -> List[Wallet]:
    """Load wallets from a text or JSON file.

    Text format: one private key per line, optional ',label' suffix.
    JSON format: {"wallets": [{"private_key": "0x..", "label": ".."}, ...]}
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(f"Wallets file not found: {path}")

    wallets: List[Wallet] = []
    seen = set()

    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read().strip()

    if not content:
        raise ValueError(f"Wallets file is empty: {path}")

    # Try JSON first
    try:
        data = json.loads(content)
        if isinstance(data, dict) and "wallets" in data:
            entries = data["wallets"]
        elif isinstance(data, list):
            entries = data
        else:
            entries = None
        if entries is not None:
            for idx, entry in enumerate(entries):
                if isinstance(entry, str):
                    pk, label = entry, f"acct{idx + 1}"
                elif isinstance(entry, dict):
                    pk = entry.get("private_key") or entry.get("pk") or ""
                    label = entry.get("label") or f"acct{idx + 1}"
                else:
                    continue
                try:
                    w = _wallet_from_pk(pk, label)
                except Exception as exc:
                    log.warning("Skipping bad wallet entry #%d: %s", idx + 1, exc)
                    continue
                if w.address.lower() in seen:
                    log.warning("Duplicate wallet %s skipped", w.address)
                    continue
                seen.add(w.address.lower())
                wallets.append(w)
            return wallets
    except json.JSONDecodeError:
        pass

    # Fallback: text file
    for line_no, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Support PRIVKEY,LABEL or just PRIVKEY
        parts = [p.strip() for p in line.split(",")]
        pk = parts[0]
        label = parts[1] if len(parts) > 1 and parts[1] else f"acct{len(wallets) + 1}"
        try:
            w = _wallet_from_pk(pk, label)
        except Exception as exc:
            log.warning("Skipping wallets.txt line %d: %s", line_no, exc)
            continue
        if w.address.lower() in seen:
            log.warning("Duplicate wallet %s skipped (line %d)", w.address, line_no)
            continue
        seen.add(w.address.lower())
        wallets.append(w)

    if not wallets:
        raise ValueError(
            f"No usable wallets found in {path}. Check format: PRIVATE_KEY[,label]"
        )
    return wallets


# ---------------------------------------------------------------------------
# Web3 connection helpers
# ---------------------------------------------------------------------------

def build_web3(rpc_url: str, *, timeout: int = 15, poa: bool = False) -> Web3:
    """Build a Web3 client with sane HTTP timeouts and optional POA middleware."""
    provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout})
    w3 = Web3(provider)
    if poa and _POA_MIDDLEWARE is not None:
        try:
            w3.middleware_onion.inject(_POA_MIDDLEWARE, layer=0)
        except Exception:
            pass
    return w3


def connect(chain: ChainConfig, rpc_override: Optional[str] = None) -> Web3:
    """Connect to a chain by trying its RPC URLs in order until one responds.

    Raises ConnectionError if all candidates fail.
    """
    candidates = resolve_rpc(chain, rpc_override)
    last_err: Optional[Exception] = None

    for url in candidates:
        try:
            w3 = build_web3(url)
            chain_id = w3.eth.chain_id
            if chain_id != chain.chain_id:
                log.warning(
                    "RPC %s returned chain_id=%s, expected %s; skipping",
                    url, chain_id, chain.chain_id,
                )
                continue
            log.info("Connected to %s via %s (chainId=%d)", chain.name, url, chain_id)
            return w3
        except (Web3Exception, ConnectionError, ValueError, OSError) as exc:
            last_err = exc
            log.warning("RPC %s failed: %s", url, exc)
            continue

    raise ConnectionError(
        f"Could not connect to any RPC for {chain.name}. Last error: {last_err}"
    )


def ensure_address(value: str) -> str:
    """Validate and return a checksum address. Raises ValueError on bad input."""
    if not is_hex_address(value):
        raise ValueError(f"'{value}' is not a valid Ethereum address")
    return Web3.to_checksum_address(value)
