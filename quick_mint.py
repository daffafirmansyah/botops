"""quick_mint.py — single-file all-in-one OpenSea SeaDrop mint bot.

Goal: ONE command, no config.json, no manual phase schedule, no manual
eligibility map. Just supply a contract address; the bot does the rest.

    python quick_mint.py                              # interactive prompt
    python quick_mint.py 0xCONTRACT                   # ethereum, qty=1
    python quick_mint.py 0xCONTRACT base 2            # base chain, qty=2
    python quick_mint.py 0xCONTRACT --auto            # auto-pick best mode
    python quick_mint.py 0xCONTRACT --sniper-host 0.0.0.0 --sniper-port 8888

What it does
------------
1. Loads wallets from wallets.txt (no config file needed).
2. Connects to the selected chain (ethereum/base/arbitrum/optimism/polygon).
3. Discovers all SeaDrop phases on-chain + best-effort OpenSea API enrichment.
4. Per-wallet eligibility check (on-chain + OpenSea proof lookup).
5. Picks operating mode automatically:
     - If a signed phase exists  → starts sniper server (Tampermonkey hand-off)
     - Else if public phase live → fires public mint immediately
     - Else if public upcoming   → schedules + fires at start_time
6. Prints a single dashboard. No interactive menus, no JSON editing.

This file is INTENTIONALLY a thin orchestrator on top of `bot/` modules
and `main.py` flows; it's the user-facing simple front-end. Heavy
logic stays in `main.py` / `bot/`.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional

# Force UTF-8 I/O (Windows compatibility for box drawing)
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    from colorama import Fore, Style, init as _ci
    _ci()
    C_HEAD = Fore.CYAN + Style.BRIGHT
    C_OK = Fore.GREEN + Style.BRIGHT
    C_WARN = Fore.YELLOW + Style.BRIGHT
    C_ERR = Fore.RED + Style.BRIGHT
    C_INFO = Fore.WHITE + Style.BRIGHT
    C_DIM = Style.DIM
    C_RESET = Style.RESET_ALL
except Exception:
    C_HEAD = C_OK = C_WARN = C_ERR = C_INFO = C_DIM = C_RESET = ""


from bot.chains import CHAINS, get_chain
from bot.eligibility import evaluate_eligibility
from bot.logger import get_logger, setup_logging
from bot.opensea_api import OpenSeaClient
from bot.utils import fmt_eth, fmt_timestamp, humanize_seconds, is_hex_address, now_unix, short_addr
from bot.wallet import load_wallets


log = get_logger("quick_mint")


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

BANNER = f"""{C_HEAD}
 ┌─────────────────────────────────────────────────────────┐
 │  quick_mint  ·  one-shot OpenSea SeaDrop sniper bot     │
 └─────────────────────────────────────────────────────────┘{C_RESET}
"""

USERSCRIPT_URL = (
    "https://raw.githubusercontent.com/daffafirmansyah/botops/"
    "main/userscript/opensea-sniper.user.js"
)


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Single-file all-in-one OpenSea SeaDrop mint bot",
        epilog=(
            "Examples:\n"
            "  python quick_mint.py                              # interactive\n"
            "  python quick_mint.py 0xCONTRACT                   # eth, qty=1\n"
            "  python quick_mint.py 0xCONTRACT base 2            # base, qty=2\n"
            "  python quick_mint.py 0xCONTRACT --rpc https://...\n"
            "  python quick_mint.py 0xCONTRACT --sniper-host 0.0.0.0\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("contract", nargs="?", default=None,
                        help="NFT contract address (0x...). Prompted if omitted.")
    parser.add_argument("chain", nargs="?", default="ethereum",
                        help=f"Chain key: {'/'.join(CHAINS.keys())} (default: ethereum)")
    parser.add_argument("qty", nargs="?", type=int, default=1,
                        help="Quantity per wallet (default: 1)")
    parser.add_argument("--config", default=None,
                        help="Optional config.json — only used to source rpc_url / rpc_urls / opensea_api_key, "
                             "everything else is taken from CLI args. Lets you reuse an existing config "
                             "without dealing with shell escape rules.")
    parser.add_argument("--wallets-file", default="wallets.txt",
                        help="Path to wallets file (default: wallets.txt)")
    parser.add_argument("--rpc", default=None,
                        help="Custom RPC URL (Alchemy/Infura/etc). Overrides --config rpc.")
    parser.add_argument("--opensea-key", default=os.environ.get("OPENSEA_API_KEY", ""),
                        help="Optional OpenSea API key (for richer drop info)")
    parser.add_argument("--sniper-host", default="127.0.0.1",
                        help="Sniper HTTP server bind host (default: 127.0.0.1)")
    parser.add_argument("--sniper-port", type=int, default=8888,
                        help="Sniper HTTP server port (default: 8888)")
    parser.add_argument("--sniper-secret", default=os.environ.get("SNIPER_SECRET", ""),
                        help="Shared secret for userscript auth (env: SNIPER_SECRET)")
    parser.add_argument("--mode", choices=("auto", "sniper", "public", "check"), default="auto",
                        help="Operating mode (default: auto-pick based on phases)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def _print_section(title: str) -> None:
    print(f"\n{C_HEAD}── {title} ──{C_RESET}")


def _print_collection(plan) -> None:
    meta = plan.collection_meta or {}
    name = meta.get("name") or "(unknown)"
    symbol = meta.get("symbol") or "?"
    supply = meta.get("totalSupply", "?")
    max_supply = meta.get("maxSupply", "?")
    print(f"  {C_INFO}{name}{C_RESET} ({symbol})  {supply}/{max_supply} minted")
    print(f"  contract  : {plan.nft_contract}")
    print(f"  chain     : {plan.chain.name} (chainId {plan.chain.chain_id})")


def _print_phases(plan) -> None:
    if not plan.phases:
        print(f"  {C_WARN}No SeaDrop phases discovered.{C_RESET}")
        print(f"  This contract may not use SeaDrop, or the drop hasn't been")
        print(f"  configured yet. Check the contract on Etherscan or try again later.")
        return
    now = now_unix()
    for idx, p in enumerate(plan.phases):
        starts = fmt_timestamp(p.start_time) if p.start_time else "?"
        if p.start_time and p.start_time > now:
            starts += f" (in {humanize_seconds(p.start_time - now)})"
        ends = fmt_timestamp(p.end_time) if p.end_time else "open-ended"
        status = p.status(now)
        # Color-code by status
        status_color = (
            C_OK if status == "live"
            else C_WARN if status == "upcoming"
            else C_DIM if status in ("ended", "data needed")
            else C_INFO
        )
        type_label = p.phase_type
        if p.is_stub:
            type_label += "*"
        price = fmt_eth(p.mint_price_wei, plan.chain.native_symbol)
        print(
            f"  [{idx}] {p.name:<24} {type_label:<11} {price:<14} "
            f"max/wallet={p.max_per_wallet:<3} "
            f"{status_color}{status}{C_RESET}"
        )
        print(f"      starts {starts}")
        print(f"      ends   {ends}")
    has_stub = any(p.is_stub for p in plan.phases)
    if has_stub:
        print(f"  {C_DIM}* = stub (on-chain hint, no off-chain data — see README){C_RESET}")


def _print_eligibility(plan, eligibility_map: Dict[str, List]) -> None:
    if not eligibility_map:
        return
    for wallet, recs in eligibility_map.items():
        print(f"\n  {C_INFO}{short_addr(wallet)}{C_RESET}  ({wallet})")
        if not recs:
            print("    (no phases available)")
            continue
        for r in recs:
            badge = (
                f"{C_OK}YES{C_RESET}" if r.eligible
                else f"{C_ERR} no{C_RESET}"
            )
            print(
                f"    {badge}  {r.phase.name:<24} "
                f"remaining={r.remaining_for_wallet:<3} "
                f"{C_DIM}{r.reason}{C_RESET}"
            )


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

def pick_mode(plan, requested: str) -> str:
    """Decide what to actually run based on detected phases.

    Returns one of: 'sniper' / 'public' / 'check' / 'none'.
    """
    if requested in ("sniper", "public", "check"):
        return requested
    if not plan.phases:
        return "none"
    has_signed = any(p.is_signed for p in plan.phases)
    has_public_live = any(
        p.is_public and not p.is_stub and p.status(now_unix()) in ("live", "upcoming")
        for p in plan.phases
    )
    if has_signed:
        return "sniper"
    if has_public_live:
        return "public"
    return "none"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(level="INFO", log_file="logs/quick_mint.log")

    print(BANNER)

    # --- contract address -----------------------------------------------
    contract = args.contract or input("NFT contract address (0x...): ").strip()
    if not is_hex_address(contract):
        print(f"{C_ERR}Invalid contract address: {contract!r}{C_RESET}")
        return 2

    chain_key = (args.chain or "ethereum").lower()
    if chain_key not in CHAINS:
        print(f"{C_ERR}Unknown chain: {chain_key}. Available: {', '.join(CHAINS.keys())}{C_RESET}")
        return 2

    # --- optionally pull rpc / api key from existing config.json --------
    cfg_from_file: Dict = {}
    if args.config:
        try:
            import json as _json
            with open(args.config, "r", encoding="utf-8") as fh:
                cfg_from_file = _json.load(fh)
            log.info("Loaded config from %s", args.config)
        except Exception as exc:
            print(f"  {C_WARN}Could not read --config {args.config}: {exc}{C_RESET}")
            cfg_from_file = {}

    rpc_from_file = ""
    if cfg_from_file:
        # rpc_urls (per-chain map) wins; fall back to legacy rpc_url
        rpc_map = cfg_from_file.get("rpc_urls") or {}
        if isinstance(rpc_map, dict):
            rpc_from_file = (rpc_map.get(chain_key) or "").strip()
        if not rpc_from_file:
            rpc_from_file = (cfg_from_file.get("rpc_url") or "").strip()

    rpc_url = (args.rpc or "").strip() or rpc_from_file
    opensea_key = (args.opensea_key or "").strip() or (cfg_from_file.get("opensea_api_key") or "").strip()

    # --- build minimal cfg (avoids needing config.json for everything) ---
    cfg: Dict = {
        "nft_contract": contract,
        "chain": chain_key,
        "rpc_url": rpc_url,
        "wallets_file": args.wallets_file,
        "opensea_api_key": opensea_key,
        "mint": {
            "amount_per_wallet": max(1, int(args.qty)),
            "max_retries": 2,
            "retry_delay_seconds": 1,
            "wait_receipt_seconds": 60,
            "parallel_wallets": 0,  # all in parallel
            "phase": "auto",
        },
        "gas": {
            "mode": "auto",
            "priority_fee_gwei": 2.5,
            "max_fee_gwei": 0.0,
            "max_fee_multiplier": 1.5,
            "gas_limit": 0,
        },
        "scheduler": {"poll_seconds": 5, "lead_seconds": 12, "max_wait_seconds": 86400},
        "logging": {"level": "INFO", "log_file": "logs/quick_mint.log"},
    }

    # --- import heavy modules late (after sys.path setup, faster startup) -
    from main import (
        build_chain_and_w3,
        discover,
        evaluate_all,
        run_full_flow,
        run_sniper_flow,
    )

    # --- step 1: load wallets -------------------------------------------
    _print_section("Wallets")
    try:
        wallets = load_wallets(cfg["wallets_file"])
    except Exception as exc:
        print(f"  {C_ERR}Failed to load {cfg['wallets_file']}: {exc}{C_RESET}")
        print(f"  Create a wallets.txt with one private key per line.")
        return 1
    print(f"  {len(wallets)} wallet(s) loaded:")
    for w in wallets:
        label = w.label or "(unnamed)"
        print(f"    {C_INFO}{label:<14}{C_RESET} {w.address}")

    # --- step 2: chain + web3 -------------------------------------------
    _print_section("Chain")
    try:
        chain, w3 = build_chain_and_w3(cfg)
    except Exception as exc:
        print(f"  {C_ERR}Failed to connect to {chain_key}: {exc}{C_RESET}")
        return 1
    # Block-number probe is best-effort; default public RPCs often rate-limit.
    block_str = "?"
    try:
        block_str = str(w3.eth.block_number)
    except Exception as exc:
        log.debug("block_number probe failed: %s", exc)
        block_str = f"{C_DIM}(rpc rate-limited — use --rpc with Alchemy/Infura URL){C_RESET}"
    print(f"  {chain.name} (chainId {chain.chain_id}) — block {block_str}")

    # --- step 3: discover phases ----------------------------------------
    _print_section("Drop discovery")
    try:
        plan = discover(cfg, chain, w3)
    except Exception as exc:
        print(f"  {C_ERR}Discovery failed: {exc}{C_RESET}")
        return 1
    _print_collection(plan)
    print()
    _print_phases(plan)

    # --- step 4: per-wallet eligibility ---------------------------------
    _print_section("Per-wallet eligibility")
    if not plan.phases:
        print("  (no phases — skipping)")
    else:
        try:
            eligibility = evaluate_all(w3, plan, wallets, opensea_api_key=cfg["opensea_api_key"])
        except Exception as exc:
            log.warning("eligibility evaluation failed: %s", exc)
            eligibility = {}
        _print_eligibility(plan, eligibility)

    # --- step 5: pick mode ----------------------------------------------
    mode = pick_mode(plan, args.mode)
    _print_section("Action plan")
    if mode == "none":
        print(f"  {C_WARN}No mintable phases — nothing to do.{C_RESET}")
        return 0
    if mode == "check":
        print(f"  Check-only mode — no transactions will be sent.")
        return 0
    if mode == "sniper":
        print(f"  Mode      : {C_INFO}sniper{C_RESET} (signed phase detected)")
        print(f"  Listening : http://{args.sniper_host}:{args.sniper_port}/signature")
        print(f"  Userscript: {USERSCRIPT_URL}")
        print(f"  Steps     : 1) install Tampermonkey + paste userscript above")
        print(f"              2) Tampermonkey menu → Configure Sniper:")
        print(f"                 Bot URL = http://{args.sniper_host}:{args.sniper_port}/signature")
        if args.sniper_secret:
            print(f"                 Shared secret = ({len(args.sniper_secret)} chars, set via --sniper-secret)")
        print(f"              3) open the OpenSea drop page, connect your wallet")
        print(f"              4) Tampermonkey menu → ⏰ Set Target Phase Time")
        print(f"              5) wait for the click; bot will auto-fire.")
    elif mode == "public":
        public_phase = next((p for p in plan.phases if p.is_public and not p.is_stub), None)
        if public_phase:
            now = now_unix()
            if public_phase.start_time and public_phase.start_time > now:
                wait = public_phase.start_time - now
                print(f"  Mode      : {C_INFO}public scheduled{C_RESET}")
                print(f"  Phase     : {public_phase.name} starts {fmt_timestamp(public_phase.start_time)}")
                print(f"  Waiting   : {humanize_seconds(wait)} until phase open")
            else:
                print(f"  Mode      : {C_INFO}public live{C_RESET}")
                print(f"  Phase     : {public_phase.name} (live now)")
            print(f"  Quantity  : {cfg['mint']['amount_per_wallet']}/wallet × {len(wallets)} wallet(s)")

    # --- step 6: confirm + run ------------------------------------------
    if not args.yes:
        ans = input(f"\n{C_WARN}Proceed? (Y/n): {C_RESET}").strip().lower()
        if ans == "n":
            print("Aborted.")
            return 0

    print(f"\n{C_HEAD}── Starting {mode} mode ──{C_RESET}")
    if mode == "sniper":
        return run_sniper_flow(
            cfg,
            host=args.sniper_host,
            port=args.sniper_port,
            shared_secret=args.sniper_secret,
        )
    if mode == "public":
        return run_full_flow(cfg, check_only=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{C_WARN}Interrupted by user.{C_RESET}")
        sys.exit(130)
