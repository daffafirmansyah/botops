"""OpenSea NFT Mint Bot - interactive command line entry point.

Usage:
  python main.py                        # interactive menu
  python main.py --config config.json   # auto-run using a config file
  python main.py --check                # check eligibility only (no mint)

The bot relies entirely on local files. It never sends private keys anywhere
except to your selected RPC for signing transactions locally.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

# Force UTF-8 stdout/stderr on Windows so the unicode banner + log glyphs
# work even when output is piped/redirected (default cp1252 chokes on box
# drawing characters like ╔ ═ ║).
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:  # pragma: no cover - colorama is optional
    from colorama import Fore, Style, init as _ci
    _ci()
    C_HEAD = Fore.CYAN + Style.BRIGHT
    C_OK = Fore.GREEN + Style.BRIGHT
    C_WARN = Fore.YELLOW + Style.BRIGHT
    C_ERR = Fore.RED + Style.BRIGHT
    C_INFO = Fore.WHITE + Style.BRIGHT
    C_RESET = Style.RESET_ALL
except Exception:  # pragma: no cover
    C_HEAD = C_OK = C_WARN = C_ERR = C_INFO = C_RESET = ""

from bot.chains import CHAINS, ChainConfig, get_chain, list_chains
from bot.eligibility import (
    DropPlan,
    WalletEligibility,
    discover_phases,
    evaluate_eligibility,
)
from bot.logger import get_logger, setup_logging
from bot.minter import (
    GasSettings,
    MintConfig,
    MintResult,
    execute_mint_batch,
)
from bot.opensea_api import OpenSeaClient
from bot.phase_eligibility import (
    ScheduleConfig,
    format_matrix as format_phase_matrix,
    load_from_cfg as load_phase_schedule,
)
from bot.seadrop import MintPhase, fetch_collection_meta
from bot.utils import (
    fmt_eth,
    fmt_timestamp,
    humanize_seconds,
    is_hex_address,
    now_unix,
    parse_float,
    parse_int,
    short_addr,
    sleep_until,
)
from bot.wallet import Wallet, connect, ensure_address, load_wallets


log = get_logger("main")


BANNER = f"""{C_HEAD}
 ╔══════════════════════════════════════════════════════════════════╗
 ║              OpenSea NFT Mint Bot v1.0  |  SeaDrop               ║
 ║  multi-account · multi-chain · gas tuning · auto eligibility     ║
 ╚══════════════════════════════════════════════════════════════════╝{C_RESET}
"""


# ---------------------------------------------------------------------------
# Config loading & user prompts
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict = {
    "nft_contract": "",
    "chain": "ethereum",
    "rpc_url": "",
    # Per-chain RPC overrides (used when chain switches without setting rpc_url).
    # Example:
    #   "rpc_urls": {
    #     "ethereum": "https://eth-mainnet.g.alchemy.com/v2/<KEY>",
    #     "base":     "https://base-mainnet.g.alchemy.com/v2/<KEY>",
    #     "arbitrum": "https://arb-mainnet.g.alchemy.com/v2/<KEY>",
    #     "polygon":  "https://polygon-mainnet.g.alchemy.com/v2/<KEY>"
    #   }
    "rpc_urls": {},
    "mint": {
        "phase": "auto",
        "amount_per_wallet": 1,
        "max_total": 0,
        "stop_on_first_success_per_wallet": True,
        "fee_recipient": "",
    },
    "gas": {
        "mode": "eip1559",
        "max_fee_gwei": 0,
        "priority_fee_gwei": 0,
        "gas_price_gwei": 0,
        "multiplier": 1.25,
        "gas_limit": 0,
        # Gas escalation per retry attempt: each retry replaces the pending
        # tx with gas multiplied by this factor (min 1.10 enforced by EIP).
        "escalation_factor": 1.25,
        # Hard cap on escalated gas in gwei (0 = no cap, escalate freely).
        "max_escalation_cap": 0,
    },
    "scheduler": {
        "lead_time_ms": 250,
        "poll_interval_ms": 1000,
        "max_retries": 3,
        "retry_delay_ms": 1500,
        "parallel_wallets": True,
        # Pre-build tx ahead of fire time (saves ~150-200ms RPC latency).
        "prebuild_tx": True,
        "prebuild_lead_seconds": 8,
        # Multi-RPC parallel broadcast (sends raw tx to many RPCs at once).
        "multi_rpc_broadcast": True,
        "broadcast_rpc_count": 4,
        # Extra RPCs to fan-out to during fire phase (e.g. private mempools).
        "extra_broadcast_rpcs": [],
    },
    "opensea_api_key": "",
    "allowlists": [],
    "wallets_file": "wallets.txt",
    "logging": {
        "level": "INFO",
        "log_file": "logs/mint_bot.log",
    },
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Recursive dict merge: values in override win, dicts merged recursively."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str]) -> Dict:
    """Load JSON config file merged onto defaults. Missing path -> defaults."""
    if not path:
        return dict(DEFAULT_CONFIG)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    return _deep_merge(DEFAULT_CONFIG, data)


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{C_INFO}{text}{suffix}{C_RESET}: ").strip()
    except EOFError:
        return default
    return ans or default


def prompt_yes_no(text: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = prompt(f"{text} ({d})", "y" if default else "n").lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def prompt_chain() -> str:
    print(f"\n{C_HEAD}Supported chains:{C_RESET}")
    for key in list_chains():
        c = CHAINS[key]
        print(f"  - {key:<10} ({c.name}, chainId={c.chain_id})")
    while True:
        choice = prompt("Choose chain", "ethereum").lower()
        if choice in CHAINS:
            return choice
        print(f"{C_ERR}Unknown chain '{choice}'.{C_RESET}")


def prompt_contract() -> str:
    while True:
        addr = prompt("NFT contract address (0x...)")
        if is_hex_address(addr):
            return addr
        print(f"{C_ERR}Invalid address.{C_RESET}")


def interactive_config() -> Dict:
    print(f"\n{C_HEAD}=== Configure mint ==={C_RESET}")
    cfg = dict(DEFAULT_CONFIG)
    cfg["chain"] = prompt_chain()
    cfg["nft_contract"] = prompt_contract()
    cfg["rpc_url"] = prompt(
        "Custom RPC URL (leave blank to auto-pick public RPCs)", ""
    )

    mint = cfg["mint"]
    mint["phase"] = prompt(
        "Phase preference [auto / guaranteed / fcfs / public / signed / allowlist]", "auto"
    ).lower()
    mint["amount_per_wallet"] = parse_int(prompt("Amount to mint per wallet", "1"), 1)
    mint["fee_recipient"] = prompt(
        "Fee recipient override (leave blank to use phase default)", ""
    )

    gas = cfg["gas"]
    gas["mode"] = prompt("Gas mode [auto / eip1559 / legacy]", "auto").lower()
    gas["multiplier"] = parse_float(prompt("Gas multiplier (e.g. 1.25)", "1.25"), 1.25)
    if gas["mode"] in ("eip1559", "auto"):
        gas["max_fee_gwei"] = parse_float(
            prompt("Max fee per gas in gwei (0=auto)", "0"), 0.0
        )
        gas["priority_fee_gwei"] = parse_float(
            prompt("Priority fee in gwei (0=auto)", "0"), 0.0
        )
    if gas["mode"] == "legacy":
        gas["gas_price_gwei"] = parse_float(
            prompt("Legacy gas price in gwei (0=auto)", "0"), 0.0
        )
    gas["gas_limit"] = parse_int(prompt("Gas limit override (0=estimate)", "0"), 0)

    cfg["wallets_file"] = prompt("Wallets file path", "wallets.txt")
    cfg["opensea_api_key"] = prompt(
        "OpenSea API key (optional, used for proof lookups)", ""
    )

    sched = cfg["scheduler"]
    sched["parallel_wallets"] = prompt_yes_no("Mint wallets in parallel?", True)
    sched["lead_time_ms"] = parse_int(
        prompt("Lead time before phase starts (ms)", "250"), 250
    )
    sched["max_retries"] = parse_int(prompt("Retries on failure", "3"), 3)
    return cfg


# ---------------------------------------------------------------------------
# Pretty printers
# ---------------------------------------------------------------------------

def print_collection_summary(plan: DropPlan) -> None:
    meta = plan.collection_meta
    name = meta.get("name") or "(unknown)"
    symbol = meta.get("symbol") or "?"
    supply = meta.get("totalSupply", "?")
    max_supply = meta.get("maxSupply", "?")
    print(f"\n{C_HEAD}Collection:{C_RESET} {name} ({symbol})")
    print(f"  contract : {plan.nft_contract}")
    print(f"  chain    : {plan.chain.name} (chainId {plan.chain.chain_id})")
    print(f"  supply   : {supply}/{max_supply}")
    print(f"  seadrop  : {plan.seadrop_address}")


def print_phase_table(plan: DropPlan) -> None:
    if not plan.phases:
        print(f"{C_WARN}No phases discovered. Provide a manual allowlist file or check the contract.{C_RESET}")
        return
    print(f"\n{C_HEAD}Mint phases:{C_RESET}")
    now = now_unix()
    for idx, p in enumerate(plan.phases):
        starts = fmt_timestamp(p.start_time)
        if p.start_time and p.start_time > now:
            starts += f" (in {humanize_seconds(p.start_time - now)})"
        ends = fmt_timestamp(p.end_time) if p.end_time else "open-ended"
        print(
            f"  [{idx}] {p.name:<14} type={p.phase_type:<10} "
            f"price={fmt_eth(p.mint_price_wei, plan.chain.native_symbol):<14} "
            f"max/wallet={p.max_per_wallet:<3} "
            f"starts={starts}  ends={ends}  "
            f"status={p.status(now)}"
        )


def print_eligibility_summary(
    plan: DropPlan, wallet_results: Dict[str, List[WalletEligibility]]
) -> None:
    if not wallet_results:
        return
    print(f"\n{C_HEAD}Wallet eligibility:{C_RESET}")
    for wallet, recs in wallet_results.items():
        print(f"\n  {C_INFO}{wallet}{C_RESET}")
        if not recs:
            print("    (no phases)")
            continue
        for r in recs:
            badge = f"{C_OK}YES{C_RESET}" if r.eligible else f"{C_ERR}no{C_RESET}"
            print(
                f"    - {r.phase.name:<14} ({r.phase.phase_type:<10}) "
                f"{badge}  remaining={r.remaining_for_wallet}  reason={r.reason}"
            )


def print_results(results: List[MintResult]) -> None:
    if not results:
        return
    print(f"\n{C_HEAD}Mint results:{C_RESET}")
    success = 0
    for r in results:
        if r.success:
            success += 1
            print(
                f"  {C_OK}OK{C_RESET}  [{r.label}] phase={r.phase} qty={r.quantity} "
                f"tx={r.tx_hash}  {r.explorer_url}"
            )
        else:
            print(
                f"  {C_ERR}FAIL{C_RESET}  [{r.label}] phase={r.phase} "
                f"qty={r.quantity}  reason={r.error}"
            )
            if r.tx_hash:
                print(f"        tx: {r.tx_hash}  {r.explorer_url}")
    total = len(results)
    print(f"\n{C_INFO}{success}/{total} mints succeeded.{C_RESET}\n")


# ---------------------------------------------------------------------------
# Main flow steps
# ---------------------------------------------------------------------------

def build_chain_and_w3(cfg: Dict) -> Tuple[ChainConfig, "object"]:
    chain_key = (cfg.get("chain") or "ethereum").lower()
    chain = get_chain(chain_key)

    # Resolve RPC override priority:
    #   1) cfg.rpc_url           (single legacy override / CLI --rpc)
    #   2) cfg.rpc_urls[chain]   (per-chain map, e.g. all Alchemy URLs)
    #   3) chain default RPC list
    rpc_override = (cfg.get("rpc_url") or "").strip() or None
    if not rpc_override:
        per_chain = cfg.get("rpc_urls") or {}
        if isinstance(per_chain, dict):
            candidate = (per_chain.get(chain_key) or "").strip()
            if candidate:
                rpc_override = candidate

    w3 = connect(chain, rpc_override)
    return chain, w3


def discover(cfg: Dict, chain: ChainConfig, w3) -> DropPlan:
    nft_contract = cfg.get("nft_contract", "").strip()
    if not is_hex_address(nft_contract):
        raise ValueError(f"Invalid NFT contract address: {nft_contract!r}")

    nft_contract = ensure_address(nft_contract)
    api_key = (cfg.get("opensea_api_key") or "").strip()
    client = OpenSeaClient(api_key=api_key) if api_key else None

    meta = fetch_collection_meta(w3, nft_contract)
    plan = discover_phases(
        w3,
        chain,
        nft_contract,
        manual_allowlists=cfg.get("allowlists") or [],
        opensea_client=client,
        collection_meta=meta,
    )
    return plan


def evaluate_all(
    w3, plan: DropPlan, wallets: List[Wallet], opensea_api_key: str = ""
) -> Dict[str, List[WalletEligibility]]:
    client = OpenSeaClient(api_key=opensea_api_key) if opensea_api_key else None
    out: Dict[str, List[WalletEligibility]] = {}
    for w in wallets:
        try:
            recs = evaluate_eligibility(w3, plan, w.address, opensea_client=client)
        except Exception as exc:
            log.exception("Eligibility check failed for %s: %s", w.address, exc)
            recs = []
        out[w.address] = recs
    return out


def select_phases(
    plan: DropPlan, preference: str
) -> List[MintPhase]:
    """Pick which phases to participate in based on user preference."""
    pref = (preference or "auto").lower()
    if not plan.phases:
        return []
    if pref in ("public",):
        return [p for p in plan.phases if p.is_public]
    if pref in ("guaranteed",):
        return [
            p for p in plan.phases
            if p.phase_type == "guaranteed"
            or (p.is_signed and "guarantee" in p.name.lower())
        ]
    if pref in ("fcfs",):
        return [
            p for p in plan.phases
            if p.phase_type == "fcfs"
            or (p.is_signed and "fcfs" in p.name.lower())
        ]
    if pref in ("allowlist",):
        return [p for p in plan.phases if p.requires_allowlist]
    if pref in ("signed",):
        return [p for p in plan.phases if p.is_signed]
    return list(plan.phases)


def gas_settings_from_cfg(cfg: Dict) -> GasSettings:
    g = cfg.get("gas") or {}
    return GasSettings(
        mode=str(g.get("mode") or "auto").lower(),
        max_fee_gwei=parse_float(g.get("max_fee_gwei", 0), 0.0),
        priority_fee_gwei=parse_float(g.get("priority_fee_gwei", 0), 0.0),
        gas_price_gwei=parse_float(g.get("gas_price_gwei", 0), 0.0),
        multiplier=parse_float(g.get("multiplier", 1.25), 1.25),
        gas_limit=parse_int(g.get("gas_limit", 0), 0),
        escalation_factor=parse_float(g.get("escalation_factor", 1.25), 1.25),
        max_escalation_cap=parse_float(g.get("max_escalation_cap", 0), 0.0),
    )


def mint_config_from_cfg(cfg: Dict) -> MintConfig:
    m = cfg.get("mint") or {}
    s = cfg.get("scheduler") or {}
    extras = s.get("extra_broadcast_rpcs") or []
    if not isinstance(extras, list):
        extras = []
    return MintConfig(
        quantity=max(1, parse_int(m.get("amount_per_wallet", 1), 1)),
        fee_recipient_override=str(m.get("fee_recipient") or "").strip(),
        max_retries=max(1, parse_int(s.get("max_retries", 3), 3)),
        retry_delay_ms=max(0, parse_int(s.get("retry_delay_ms", 1500), 1500)),
        parallel_wallets=bool(s.get("parallel_wallets", True)),
        prebuild_tx=bool(s.get("prebuild_tx", True)),
        prebuild_lead_seconds=max(2, parse_int(s.get("prebuild_lead_seconds", 8), 8)),
        multi_rpc_broadcast=bool(s.get("multi_rpc_broadcast", True)),
        broadcast_rpc_count=max(1, parse_int(s.get("broadcast_rpc_count", 4), 4)),
        extra_broadcast_rpcs=[str(u).strip() for u in extras if str(u).strip()],
    )


def run_phase(
    cfg: Dict,
    chain: ChainConfig,
    w3,
    plan: DropPlan,
    phase: MintPhase,
    wallets: List[Wallet],
    eligibility_map: Dict[str, List[WalletEligibility]],
    succeeded: set,
) -> List[MintResult]:
    """Wait for the phase to start, then run mints for eligible wallets."""
    sched = cfg.get("scheduler") or {}
    lead_ms = max(0, parse_int(sched.get("lead_time_ms", 250), 250))
    mint_cfg = mint_config_from_cfg(cfg)
    gas_settings = gas_settings_from_cfg(cfg)
    stop_on_success = bool((cfg.get("mint") or {}).get("stop_on_first_success_per_wallet", True))

    # Build per-phase eligibility map
    per_phase: Dict[str, WalletEligibility] = {}
    for w in wallets:
        if stop_on_success and w.address.lower() in succeeded:
            continue
        recs = eligibility_map.get(w.address, [])
        elig = next(
            (
                r for r in recs
                if r.phase is phase
                or (r.phase.name == phase.name and r.phase.phase_type == phase.phase_type)
            ),
            None,
        )
        if elig is not None:
            per_phase[w.address.lower()] = elig

    if not per_phase:
        log.info("No eligible wallets for phase '%s' – skipping", phase.name)
        return []

    # Wait for phase start (minus lead time)
    target_ts = float(phase.start_time) - (lead_ms / 1000.0) if phase.start_time else 0.0
    now = time.time()
    if target_ts > now:
        delta = int(target_ts - now)
        log.info(
            "Phase '%s' starts in %s (target=%s, lead=%dms)",
            phase.name, humanize_seconds(delta), fmt_timestamp(int(target_ts)), lead_ms,
        )
        last_print = 0.0

        def _tick(remaining: float) -> None:
            nonlocal last_print
            if time.time() - last_print >= 5:
                print(
                    f"  ⌛ waiting {humanize_seconds(int(remaining))} for '{phase.name}'...",
                    flush=True,
                )
                last_print = time.time()

        try:
            sleep_until(target_ts, on_tick=_tick, tick_seconds=1.0)
        except KeyboardInterrupt:
            print(f"{C_WARN}Wait cancelled by user.{C_RESET}")
            return []
    else:
        log.info("Phase '%s' is already live, executing immediately", phase.name)

    log.info(
        "Starting batch mint for phase '%s' across %d wallet(s)",
        phase.name, len(per_phase),
    )
    results = execute_mint_batch(
        w3=w3,
        chain=chain,
        nft_contract=plan.nft_contract,
        phase=phase,
        wallets=[w for w in wallets if w.address.lower() in per_phase],
        eligibility_map=per_phase,
        cfg=mint_cfg,
        gas_settings=gas_settings,
    )
    for r in results:
        if r.success:
            succeeded.add(r.wallet.lower())
    return results


def run_sniper_flow(
    cfg: Dict,
    *,
    host: str = "127.0.0.1",
    port: int = 8888,
    shared_secret: str = "",
) -> int:
    """Sniper mode: pre-position bot, wait for signature POSTs from userscript.

    Flow:
      1. Build chain + RPC (same as normal flow)
      2. Discover drop, parse phases
      3. Load wallets, evaluate eligibility
      4. Pick the signed-mint phase as target (for sniper use case)
      5. Start HTTP server listening on host:port
      6. On POST /signature with {wallet, salt, signature, contract}:
         a. Look up wallet by address
         b. Build mintSigned tx with received salt+signature
         c. Fire via multi-RPC broadcast
         d. Return tx hash to userscript
      7. Loop forever (or until Ctrl+C). Multiple signatures (different
         wallets) all get fired in their own threads.
    """
    from bot.sniper import SniperServer, SignaturePayload  # local import
    from bot.minter import execute_mint_for_wallet
    from bot.eligibility import WalletEligibility

    setup_logging(
        level=(cfg.get("logging") or {}).get("level", "INFO"),
        log_file=(cfg.get("logging") or {}).get("log_file"),
    )

    chain, w3 = build_chain_and_w3(cfg)
    plan = discover(cfg, chain, w3)
    print_collection_summary(plan)
    print_phase_table(plan)

    wallets_path = cfg.get("wallets_file") or "wallets.txt"
    wallets = load_wallets(wallets_path)
    log.info("Sniper: loaded %d wallet(s)", len(wallets))

    # Build wallet lookup by address (lowercase)
    wallet_by_addr: Dict[str, Wallet] = {w.address.lower(): w for w in wallets}
    wallet_labels: Dict[str, str] = {w.address.lower(): w.label or "" for w in wallets}

    # Per-wallet phase eligibility (manual, from config). If user supplied a
    # phase_schedule + wallet_eligibility map, sniper will refuse signatures
    # that don't match the active phase for that wallet. This is a defensive
    # check on top of the userscript's time guard.
    schedule: ScheduleConfig = load_phase_schedule(cfg)
    if schedule.phases:
        log.info("Sniper: phase_schedule loaded with %d window(s)", len(schedule.phases))
    if schedule.eligibility:
        log.info("Sniper: wallet_eligibility map loaded for %d wallet(s)", len(schedule.eligibility))
        print(f"\n{C_HEAD}Wallet eligibility matrix:{C_RESET}")
        print(format_phase_matrix(schedule, wallet_labels))
    else:
        log.info("Sniper: no wallet_eligibility configured — bot will fire any signature from a known wallet")

    # Pick signed-mint phase. Stubs (on-chain hint without data) are fine here:
    # the userscript will provide salt+signature at fire time, regardless of
    # whether the user pre-supplied signed_mints.json.
    signed_phases = [p for p in plan.phases if p.is_signed]
    if not signed_phases:
        log.warning("Sniper: no signed-mint phase found; will fall back to first non-stub phase on demand")
    fallback_phases = [p for p in plan.phases if not p.is_stub]
    target_phase = (
        signed_phases[0]
        if signed_phases
        else (fallback_phases[0] if fallback_phases else None)
    )
    if target_phase is None:
        print(f"{C_ERR}No phases available to mint.{C_RESET}")
        return 1

    log.info("Sniper: target phase = %s (%s)", target_phase.name, target_phase.phase_type)

    mint_cfg = mint_config_from_cfg(cfg)
    gas_settings = gas_settings_from_cfg(cfg)
    nft_contract = cfg.get("nft_contract", "")

    fire_lock = threading.Lock()
    fired_wallets: set = set()

    def fire_callback(payload: "SignaturePayload"):
        addr = (payload.wallet or "").lower()
        if not addr:
            return False, "payload missing wallet address"
        wallet = wallet_by_addr.get(addr)
        if wallet is None:
            return False, f"wallet {short_addr(addr)} not in wallets.txt"

        # Sanity-check contract match (best-effort; userscript may not always send)
        if payload.contract and payload.contract.lower() != nft_contract.lower():
            log.warning("Sniper: payload contract %s != configured %s — proceeding anyway",
                        payload.contract, nft_contract)

        # Per-wallet phase eligibility check (no-op when schedule is empty).
        # Rejects signatures that don't match the active phase for that wallet,
        # saving gas on transactions that would revert on-chain.
        if schedule.phases:
            ok, reason, active_phase = schedule.validate_signature(addr)
            phase_label = active_phase.name if active_phase else "none"
            if not ok:
                log.warning("Sniper: REJECT wallet=%s active_phase=%s reason=%s",
                            short_addr(addr), phase_label, reason)
                return False, reason
            log.info("Sniper: phase check OK wallet=%s active_phase=%s",
                     short_addr(addr), phase_label)

        with fire_lock:
            if addr in fired_wallets:
                return False, f"wallet {short_addr(addr)} already fired"
            fired_wallets.add(addr)

        # Build a synthetic eligibility entry containing the signature
        elig = WalletEligibility(
            wallet_address=wallet.address,
            phase=target_phase,
            eligible=True,
            remaining_for_wallet=mint_cfg.quantity,
            salt=payload.salt,
            signature=payload.signature,
            proof=[],
            reason="sniper-injected from userscript",
        )

        log.info("Sniper: firing wallet=%s salt=%s… sig=%s…",
                 short_addr(wallet.address), payload.salt[:14], payload.signature[:14])
        result = execute_mint_for_wallet(
            w3, chain, nft_contract, target_phase, wallet, elig, mint_cfg, gas_settings,
        )
        if result.success:
            return True, f"tx {result.tx_hash} ({result.explorer_url})"
        return False, result.error or "mint failed"

    server = SniperServer(
        host=host, port=port,
        fire_callback=fire_callback,
        shared_secret=shared_secret,
    )
    server.start()

    print(f"\n{C_HEAD}Sniper mode active.{C_RESET}")
    print(f"  Listening on http://{host}:{port}/signature")
    print(f"  Configure userscript -> Bot URL: http://{host}:{port}/signature")
    if shared_secret:
        print(f"  Shared secret: (configured, {len(shared_secret)} chars)")
    print(f"  Target phase  : {target_phase.name} ({target_phase.phase_type})")
    print(f"  Wallets       : {len(wallets)} loaded")
    print(f"  Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"\n{C_WARN}Sniper stopping...{C_RESET}")
    finally:
        server.stop()
    return 0


def run_full_flow(cfg: Dict, *, check_only: bool = False) -> int:
    setup_logging(
        level=(cfg.get("logging") or {}).get("level", "INFO"),
        log_file=(cfg.get("logging") or {}).get("log_file"),
    )

    chain, w3 = build_chain_and_w3(cfg)
    plan = discover(cfg, chain, w3)
    print_collection_summary(plan)
    print_phase_table(plan)

    wallets_path = cfg.get("wallets_file") or "wallets.txt"
    wallets = load_wallets(wallets_path)
    log.info("Loaded %d wallet(s) from %s", len(wallets), wallets_path)

    eligibility_map = evaluate_all(
        w3, plan, wallets, opensea_api_key=cfg.get("opensea_api_key", "")
    )
    print_eligibility_summary(plan, eligibility_map)

    if check_only:
        return 0

    if not plan.phases:
        print(f"{C_WARN}Aborting: no phases to mint.{C_RESET}")
        return 1

    pref = (cfg.get("mint") or {}).get("phase", "auto")
    target_phases = select_phases(plan, pref)
    if not target_phases:
        print(f"{C_WARN}No phases match preference '{pref}'.{C_RESET}")
        return 1

    # Filter out stub phases (on-chain detected but no proof/signature data).
    # They show up in --check for visibility but cannot be fired against.
    stubs = [p for p in target_phases if p.is_stub]
    if stubs:
        for p in stubs:
            print(
                f"{C_WARN}Skipping '{p.name}' ({p.phase_type}): "
                f"phase detected on-chain but no proof/signature supplied. "
                f"Run with --check for guidance.{C_RESET}"
            )
        target_phases = [p for p in target_phases if not p.is_stub]
        if not target_phases:
            print(
                f"{C_WARN}All matching phases require off-chain data "
                f"(allowlist proofs or signed-mint signatures). Cannot proceed.{C_RESET}"
            )
            return 1

    # Filter out ended phases
    now = now_unix()
    target_phases = [
        p for p in target_phases
        if not (p.end_time and p.end_time != 0 and p.end_time < now)
    ]
    if not target_phases:
        print(f"{C_WARN}All matching phases have ended.{C_RESET}")
        return 1

    print(f"\n{C_HEAD}Plan:{C_RESET} run mints across {len(target_phases)} phase(s):")
    for p in target_phases:
        print(f"  - {p.name} ({p.phase_type}) at {fmt_timestamp(p.start_time)}")

    if not prompt_yes_no("Proceed with mint?", True):
        print(f"{C_WARN}Cancelled.{C_RESET}")
        return 0

    succeeded: set = set()
    all_results: List[MintResult] = []
    for phase in target_phases:
        results = run_phase(cfg, chain, w3, plan, phase, wallets, eligibility_map, succeeded)
        all_results.extend(results)

    print_results(all_results)

    return 0 if any(r.success for r in all_results) else 2


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

def menu_loop() -> int:
    print(BANNER)
    cfg: Dict = dict(DEFAULT_CONFIG)
    while True:
        print(
            f"\n{C_HEAD}Main menu:{C_RESET}\n"
            f"  1) Configure new mint (interactive)\n"
            f"  2) Load configuration from file\n"
            f"  3) Save current configuration to file\n"
            f"  4) Check eligibility (no mint)\n"
            f"  5) Run mint with current configuration\n"
            f"  6) Show current configuration\n"
            f"  0) Exit"
        )
        choice = prompt("Select", "5")

        try:
            if choice == "1":
                cfg = interactive_config()
            elif choice == "2":
                path = prompt("Path to config JSON", "config.json")
                cfg = load_config(path)
                print(f"{C_OK}Loaded configuration from {path}.{C_RESET}")
            elif choice == "3":
                path = prompt("Save path", "config.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh, indent=2)
                print(f"{C_OK}Saved configuration to {path}.{C_RESET}")
            elif choice == "4":
                if not _ready_to_run(cfg):
                    continue
                run_full_flow(cfg, check_only=True)
            elif choice == "5":
                if not _ready_to_run(cfg):
                    continue
                run_full_flow(cfg, check_only=False)
            elif choice == "6":
                print(json.dumps(cfg, indent=2))
            elif choice == "0":
                print("bye")
                return 0
            else:
                print(f"{C_ERR}Unknown option.{C_RESET}")
        except KeyboardInterrupt:
            print(f"{C_WARN}\nInterrupted.{C_RESET}")
        except Exception as exc:
            log.exception("Operation failed: %s", exc)
            print(f"{C_ERR}Error: {exc}{C_RESET}")


def _ready_to_run(cfg: Dict) -> bool:
    if not is_hex_address(cfg.get("nft_contract", "")):
        print(f"{C_ERR}NFT contract address is not configured. Use option 1 first.{C_RESET}")
        return False
    if not cfg.get("wallets_file"):
        print(f"{C_ERR}Wallets file is not configured.{C_RESET}")
        return False
    return True


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenSea NFT mint bot (SeaDrop, multi-account, multi-chain)",
        epilog=(
            "Quick examples:\n"
            "  python main.py                                       # interactive menu\n"
            "  python main.py 0xCONTRACT                            # one-liner mint (ethereum, qty=1)\n"
            "  python main.py 0xCONTRACT base 2                     # base chain, mint 2 per wallet\n"
            "  python main.py 0xCONTRACT --check                    # only check eligibility\n"
            "  python main.py -c config.json                        # full config from file"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "contract", nargs="?", default=None,
        help="(quick mode) NFT contract address. If given, bot skips menu and auto-runs.",
    )
    parser.add_argument(
        "chain", nargs="?", default="ethereum",
        help="(quick mode) chain key: ethereum / base / arbitrum / optimism / polygon (default: ethereum)",
    )
    parser.add_argument(
        "amount", nargs="?", type=int, default=1,
        help="(quick mode) amount to mint per wallet (default: 1)",
    )
    parser.add_argument("--config", "-c", help="Path to JSON config file", default=None)
    parser.add_argument(
        "--rpc", default=None,
        help="(quick mode) custom RPC URL (Alchemy/Infura/etc)",
    )
    parser.add_argument(
        "--phase", default="auto",
        help="(quick mode) phase preference: auto / public / guaranteed / fcfs / signed (default: auto)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Only run discovery + eligibility (no mint)",
    )
    parser.add_argument(
        "--non-interactive", "-y", action="store_true",
        help="Do not prompt before sending mints",
    )
    parser.add_argument(
        "--sniper", action="store_true",
        help="Sniper mode: pre-position bot, accept signatures from userscript via HTTP, fire instantly",
    )
    parser.add_argument(
        "--sniper-host", default="127.0.0.1",
        help="(sniper mode) HTTP server bind host (default: 127.0.0.1; use 0.0.0.0 for VPS)",
    )
    parser.add_argument(
        "--sniper-port", type=int, default=8888,
        help="(sniper mode) HTTP server port (default: 8888)",
    )
    parser.add_argument(
        "--sniper-secret", default="",
        help="(sniper mode) shared secret to authenticate userscript POSTs (recommended for VPS)",
    )
    args = parser.parse_args(argv)

    # ------------------------------------------------------------------
    # Resolve config:
    # 1) start with config file if provided, else DEFAULT_CONFIG
    # 2) overlay any CLI args (contract, chain, amount, rpc, phase)
    # This lets users keep rpc/wallets/gas in config.json and just pass
    # the contract via CLI per drop:
    #   python main.py 0xCONTRACT -c config.json
    # ------------------------------------------------------------------
    cli_overrides = bool(
        args.contract
        or args.rpc
        or args.phase != "auto"
        or args.amount != 1
        or args.chain != "ethereum"
    )

    if args.config:
        cfg = load_config(args.config)
    elif args.contract:
        cfg = dict(DEFAULT_CONFIG)
        cfg["mint"] = dict(cfg["mint"])
    else:
        # No contract, no config -> show interactive menu
        return menu_loop()

    # Apply CLI overrides on top of cfg
    if args.contract:
        if not is_hex_address(args.contract):
            print(f"{C_ERR}Invalid contract address: {args.contract}{C_RESET}")
            return 2
        cfg["nft_contract"] = args.contract
    if args.rpc:
        cfg["rpc_url"] = args.rpc
    if args.chain and args.chain != "ethereum":
        # Only override chain if user explicitly set it (not the default)
        cfg["chain"] = args.chain.lower()
    elif not cfg.get("chain"):
        cfg["chain"] = "ethereum"
    cfg["mint"] = dict(cfg.get("mint") or {})
    if args.amount and args.amount != 1:
        cfg["mint"]["amount_per_wallet"] = max(1, int(args.amount))
    if args.phase and args.phase != "auto":
        cfg["mint"]["phase"] = args.phase.lower()

    setup_logging(
        level=(cfg.get("logging") or {}).get("level", "INFO"),
        log_file=(cfg.get("logging") or {}).get("log_file"),
    )
    print(BANNER)
    if cli_overrides:
        print(
            f"{C_HEAD}Quick mode:{C_RESET} contract={cfg.get('nft_contract')} "
            f"chain={cfg.get('chain')} qty={cfg['mint'].get('amount_per_wallet', 1)} "
            f"phase={cfg['mint'].get('phase', 'auto')}"
        )

    # Sniper mode dispatch (HTTP server, wait for userscript signatures)
    if args.sniper:
        return run_sniper_flow(
            cfg,
            host=args.sniper_host,
            port=args.sniper_port,
            shared_secret=args.sniper_secret,
        )

    return _run_with_optional_yes(cfg, args.check, args.non_interactive)


def _run_with_optional_yes(cfg: Dict, check_only: bool, non_interactive: bool) -> int:
    """Run the full flow, optionally auto-confirming all yes/no prompts."""
    if not non_interactive:
        return run_full_flow(cfg, check_only=check_only)

    global prompt_yes_no  # noqa: PLW0603
    original = prompt_yes_no
    def _yes(_text: str, default: bool = True) -> bool:
        return True
    prompt_yes_no = _yes  # type: ignore[assignment]
    try:
        return run_full_flow(cfg, check_only=check_only)
    finally:
        prompt_yes_no = original  # type: ignore[assignment]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
