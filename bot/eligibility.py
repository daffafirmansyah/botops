"""Phase discovery and per-wallet eligibility evaluation.

Combines three data sources:

1. **On-chain SeaDrop reads** for the public phase (always available when the
   collection uses SeaDrop).
2. **Local allowlist files** the user supplies for guaranteed/FCFS/allowlist
   phases (since those merkle proofs are usually hosted off-chain).
3. **OpenSea public API** as a best-effort fallback to fetch a proof for a
   wallet.

The result is a list of `MintPhase` objects + per-wallet eligibility records
that the scheduler / minter can reason about.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from web3 import Web3

from .chains import ChainConfig
from .logger import get_logger
from .opensea_api import OpenSeaClient
from .seadrop import (
    MintPhase,
    PHASE_ALLOWLIST,
    PHASE_FCFS,
    PHASE_GUARANTEED,
    PHASE_SIGNED,
    fetch_allowlist_root,
    fetch_mint_stats,
    fetch_public_drop,
    fetch_signers,
    phase_from_manual_dict,
)
from .utils import fmt_eth, fmt_timestamp, humanize_seconds, now_unix


log = get_logger("eligibility")


@dataclass
class WalletEligibility:
    """Per-wallet eligibility decision for a single phase."""

    wallet_address: str
    phase: MintPhase
    eligible: bool
    proof: List[str] = field(default_factory=list)
    reason: str = ""
    already_minted: int = 0
    remaining_for_wallet: int = 0
    # mintSigned payload (only used for signed phases)
    salt: str = ""
    signature: str = ""


@dataclass
class DropPlan:
    """Aggregated drop info computed from on-chain + manual + API data."""

    chain: ChainConfig
    nft_contract: str
    seadrop_address: str
    phases: List[MintPhase]
    collection_meta: Dict[str, str]

    def upcoming_or_live(self) -> List[MintPhase]:
        now = now_unix()
        out = []
        for p in self.phases:
            if p.end_time and p.end_time != 0 and p.end_time < now:
                continue
            out.append(p)
        return out


# ---------------------------------------------------------------------------
# Phase discovery
# ---------------------------------------------------------------------------

def load_manual_allowlists(paths_or_entries) -> List[MintPhase]:
    """Load allowlist phases from a list of file paths or inline dicts."""
    phases: List[MintPhase] = []
    if not paths_or_entries:
        return phases

    for item in paths_or_entries:
        # Inline dict
        if isinstance(item, dict):
            phase = phase_from_manual_dict(item)
            if phase is not None:
                phases.append(phase)
            continue

        # File path
        if not isinstance(item, str):
            continue
        path = item.strip()
        if not path:
            continue
        if not os.path.exists(path):
            log.warning("Allowlist file not found, skipping: %s", path)
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed reading allowlist file %s: %s", path, exc)
            continue

        entries = data if isinstance(data, list) else data.get("phases", [])
        if not isinstance(entries, list):
            log.warning("Allowlist file %s has no 'phases' array", path)
            continue
        for entry in entries:
            phase = phase_from_manual_dict(entry)
            if phase is not None:
                phases.append(phase)
    return phases


def discover_phases(
    w3: Web3,
    chain: ChainConfig,
    nft_contract: str,
    *,
    manual_allowlists: Optional[List] = None,
    opensea_client: Optional[OpenSeaClient] = None,
    collection_meta: Optional[Dict[str, str]] = None,
) -> DropPlan:
    """Discover all known mint phases for an NFT collection."""
    nft_addr = Web3.to_checksum_address(nft_contract)
    seadrop = chain.seadrop

    phases: List[MintPhase] = []

    public = fetch_public_drop(w3, seadrop, nft_addr)
    if public is not None:
        phases.append(public)
        log.info("Public phase detected: %s", public.describe(chain.native_symbol))
    else:
        log.info("No SeaDrop public phase configured on-chain for %s", nft_addr)

    root = fetch_allowlist_root(w3, seadrop, nft_addr)
    if root:
        log.info("SeaDrop allowlist merkle root present: %s", root)
    else:
        log.info("No SeaDrop allowlist merkle root configured on-chain")

    signers = fetch_signers(w3, seadrop, nft_addr)
    if signers:
        log.info(
            "SeaDrop signed-mint signer(s) registered: %s -> drop supports mintSigned (Guaranteed/FCFS via signatures)",
            ", ".join(signers),
        )
    else:
        log.info("No SeaDrop signed-mint signers registered on-chain")

    if manual_allowlists:
        manual_phases = load_manual_allowlists(manual_allowlists)
        if manual_phases:
            log.info("Loaded %d allowlist phase(s) from manual config", len(manual_phases))
            phases.extend(manual_phases)

    # Best-effort enrichment from OpenSea API
    if opensea_client is not None:
        try:
            drop_info = opensea_client.get_drop_info(chain.slug, nft_addr)
            if drop_info:
                added = _phases_from_opensea_payload(drop_info)
                if added:
                    log.info("Discovered %d phase(s) via OpenSea API", len(added))
                    phases.extend(added)
        except Exception as exc:  # pragma: no cover
            log.debug("OpenSea drop enrichment failed: %s", exc)

    # ------------------------------------------------------------------
    # Gap detection: on-chain says allowlist/signed phase exists but no
    # manual data was supplied. Add a "stub" phase so the eligibility
    # report still surfaces the phase with actionable guidance, instead
    # of silently dropping it. Stubs are marked source="onchain_partial"
    # so fire mode can skip them.
    # ------------------------------------------------------------------
    has_allowlist_phase = any(
        p.phase_type in (PHASE_GUARANTEED, PHASE_FCFS, PHASE_ALLOWLIST)
        for p in phases
    )
    has_signed_phase = any(p.phase_type == PHASE_SIGNED for p in phases)

    if root and not has_allowlist_phase:
        phases.append(
            MintPhase(
                name="Allowlist (data missing)",
                phase_type=PHASE_ALLOWLIST,
                start_time=0,
                end_time=0,
                mint_price_wei=0,
                max_per_wallet=0,
                merkle_root=root,
                source="onchain_partial",
            )
        )
        log.warning(
            "On-chain allowlist root detected but no proof file supplied. "
            "Edit allowlist.json with your wallet's merkle proof "
            "(scrape from OpenSea Network tab in DevTools), then reference it "
            "via config.json -> 'allowlists': ['allowlist.json']."
        )

    if signers and not has_signed_phase:
        signers_short = ", ".join(signers[:2]) + ("..." if len(signers) > 2 else "")
        phases.append(
            MintPhase(
                name="Signed Mint (data missing)",
                phase_type=PHASE_SIGNED,
                start_time=0,
                end_time=0,
                mint_price_wei=0,
                max_per_wallet=0,
                source="onchain_partial",
            )
        )
        log.warning(
            "On-chain signed-mint signers (%s) detected but no signature data supplied. "
            "Options: (a) scrape salt+signature from OpenSea DevTools when clicking Mint, "
            "save to signed_mints.json. (b) Use --sniper mode + Tampermonkey for "
            "automatic capture (see SNIPER_SETUP.md).",
            signers_short,
        )

    # Sort by start time so consumers can iterate in chronological order.
    # Stub phases (start_time=0) end up first; that's fine since they're
    # surfaced for visibility and skipped in fire mode.
    phases.sort(key=lambda p: (p.start_time or 0, 0 if p.is_public else 1))
    return DropPlan(
        chain=chain,
        nft_contract=nft_addr,
        seadrop_address=seadrop,
        phases=phases,
        collection_meta=collection_meta or {},
    )


def _phases_from_opensea_payload(payload) -> List[MintPhase]:
    """Try to interpret OpenSea drop JSON into MintPhase objects.

    The schema is undocumented and changes; we look for a `stages` or
    `phases` array with common field names.
    """
    if not isinstance(payload, dict):
        return []
    raw_phases = payload.get("stages") or payload.get("phases") or []
    if not isinstance(raw_phases, list):
        return []

    out: List[MintPhase] = []
    for raw in raw_phases:
        if not isinstance(raw, dict):
            continue
        is_public = bool(raw.get("is_public") or raw.get("public") or raw.get("type") == "public")
        phase_type = "public" if is_public else (str(raw.get("type") or "allowlist").lower())
        try:
            mint_price_wei = int(raw.get("price_wei") or raw.get("mint_price_wei") or 0)
        except (TypeError, ValueError):
            mint_price_wei = 0
        out.append(
            MintPhase(
                name=str(raw.get("name") or phase_type.title()),
                phase_type=phase_type if phase_type in ("public", "guaranteed", "fcfs", "allowlist") else "allowlist",
                start_time=int(raw.get("start_time") or 0),
                end_time=int(raw.get("end_time") or 0),
                mint_price_wei=mint_price_wei,
                max_per_wallet=int(raw.get("wallet_limit") or raw.get("max_per_wallet") or 1),
                fee_bps=int(raw.get("fee_bps") or 0),
                merkle_root=str(raw.get("merkle_root") or ""),
                source="opensea_api",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Per-wallet eligibility evaluation
# ---------------------------------------------------------------------------

def evaluate_eligibility(
    w3: Web3,
    plan: DropPlan,
    wallet_address: str,
    *,
    opensea_client: Optional[OpenSeaClient] = None,
) -> List[WalletEligibility]:
    """Return one eligibility record per phase for the given wallet."""
    wallet = Web3.to_checksum_address(wallet_address)
    minted, _supply, _max = fetch_mint_stats(w3, plan.seadrop_address, plan.nft_contract, wallet)

    results: List[WalletEligibility] = []
    for phase in plan.phases:
        remaining = max(int(phase.max_per_wallet) - int(minted), 0)

        # ------------------------------------------------------------------
        # Public phase: anyone can mint
        # ------------------------------------------------------------------
        if phase.is_public:
            results.append(
                WalletEligibility(
                    wallet_address=wallet,
                    phase=phase,
                    eligible=True,
                    reason="public phase - open to all",
                    already_minted=minted,
                    remaining_for_wallet=remaining,
                )
            )
            continue

        # ------------------------------------------------------------------
        # Signed mint phase: needs salt + signature pre-fetched from OpenSea
        # ------------------------------------------------------------------
        if phase.is_signed:
            sig_data = phase.signature_for(wallet)
            eligible = bool(sig_data and sig_data.get("signature"))
            is_stub = phase.source == "onchain_partial"
            if eligible and remaining <= 0:
                reason = f"have signature but already minted max ({minted})"
            elif eligible:
                reason = f"have signed-mint payload for '{phase.name}'"
            elif is_stub:
                reason = (
                    "signed-mint phase detected on-chain. To check this wallet's "
                    "eligibility: (a) scrape salt+signature from OpenSea DevTools "
                    "when clicking Mint, save to signed_mints.json, or "
                    "(b) re-enable --sniper mode with Tampermonkey for auto-capture."
                )
            else:
                reason = (
                    f"no signed-mint signature for '{phase.name}' "
                    "(scrape signature from OpenSea DevTools and add to "
                    "signed_mints.json - see README)"
                )
            results.append(
                WalletEligibility(
                    wallet_address=wallet,
                    phase=phase,
                    eligible=eligible,
                    reason=reason,
                    already_minted=minted,
                    remaining_for_wallet=remaining if eligible else 0,
                    salt=str(sig_data.get("salt", "")) if sig_data else "",
                    signature=str(sig_data.get("signature", "")) if sig_data else "",
                )
            )
            continue

        # ------------------------------------------------------------------
        # Merkle allowlist phase
        # ------------------------------------------------------------------
        proof = phase.proof_for(wallet)
        if not proof and opensea_client is not None:
            try:
                fetched = opensea_client.get_allowlist_proof(
                    plan.chain.slug, plan.nft_contract, wallet
                )
                if fetched:
                    proof = fetched
                    phase.proofs[wallet.lower()] = proof  # cache
            except Exception as exc:  # pragma: no cover
                log.debug("Proof fetch failed for %s: %s", wallet, exc)

        eligible = bool(proof)
        is_stub = phase.source == "onchain_partial"
        if eligible and remaining <= 0:
            reason = f"on allowlist but already minted max ({minted})"
        elif eligible:
            reason = f"on '{phase.name}' allowlist"
        elif is_stub:
            reason = (
                "allowlist phase detected on-chain. To check this wallet's "
                "eligibility: scrape merkle proof from OpenSea DevTools "
                "(Network tab, look for response containing 'proof' array) and add "
                "to allowlist.json -> 'proofs' map."
            )
        else:
            reason = f"not on '{phase.name}' allowlist"

        results.append(
            WalletEligibility(
                wallet_address=wallet,
                phase=phase,
                eligible=eligible,
                proof=proof,
                reason=reason,
                already_minted=minted,
                remaining_for_wallet=remaining if eligible else 0,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def render_eligibility_table(records: List[WalletEligibility], native_symbol: str = "ETH") -> str:
    """Render eligibility results as a plaintext table for CLI output."""
    if not records:
        return "(no eligibility data)"
    now = now_unix()
    rows = []
    rows.append(
        f"{'Phase':<12} {'Type':<10} {'Eligible':<8} {'Status':<10} {'Price':<14} "
        f"{'Max':<5} {'Starts':<22} {'Reason'}"
    )
    rows.append("-" * 120)
    for r in records:
        ph = r.phase
        starts = fmt_timestamp(ph.start_time)
        if ph.start_time and ph.start_time > now:
            starts += f" (in {humanize_seconds(ph.start_time - now)})"
        rows.append(
            f"{ph.name[:12]:<12} {ph.phase_type[:10]:<10} "
            f"{'YES' if r.eligible else 'no':<8} "
            f"{ph.status(now)[:10]:<10} "
            f"{fmt_eth(ph.mint_price_wei, native_symbol):<14} "
            f"{ph.max_per_wallet:<5} "
            f"{starts:<22} {r.reason}"
        )
    return "\n".join(rows)
