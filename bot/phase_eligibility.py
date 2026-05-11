"""Per-wallet phase eligibility checker.

OpenSea SeaDrop differentiates phases (KOL/GTD/FCFS/Public) only at the
backend layer — the smart contract just sees mintSigned/mintPublic. So
the bot cannot tell from on-chain data which wallet is eligible for
which phase. The user supplies that information manually via config:

    "phase_schedule": {
        "KOL WL":   { "start": "2026-05-11T15:00:00Z", "end": "2026-05-11T15:15:00Z" },
        "GTD WL":   { "start": "2026-05-11T15:15:00Z", "end": "2026-05-11T15:45:00Z" },
        "FCFS WL":  { "start": "2026-05-11T15:45:00Z", "end": "2026-05-11T16:15:00Z" },
        "Public":   { "start": "2026-05-11T16:15:00Z", "end": "2026-05-11T16:45:00Z" }
    },
    "wallet_eligibility": {
        "0xMAIN_ADDR": ["FCFS WL", "Public"],
        "0xDENI_ADDR": ["KOL WL",  "Public"]
    }

In sniper mode, when a signature arrives the bot:
  1. Looks up the current UTC time → finds the active phase
  2. Checks if the signing wallet is in the eligibility list for that phase
  3. Rejects with a clear message if not eligible (saves gas on a doomed tx)

This is purely defensive — the userscript's time guard is the primary
control. This is a second layer in case Tampermonkey somehow fires at
the wrong time, or the user mis-configures the browser side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .logger import get_logger
from .utils import short_addr


log = get_logger("phase_eligibility")


@dataclass
class PhaseWindow:
    """A single named phase with its UTC start and end."""
    name: str
    start_utc: datetime
    end_utc: datetime

    def is_active_at(self, when_utc: datetime, tolerance_seconds: int = 0) -> bool:
        """Return True if `when_utc` is within [start - tol, end + tol]."""
        if tolerance_seconds:
            from datetime import timedelta
            tol = timedelta(seconds=tolerance_seconds)
            return (self.start_utc - tol) <= when_utc <= (self.end_utc + tol)
        return self.start_utc <= when_utc <= self.end_utc

    def status_at(self, when_utc: datetime) -> str:
        if when_utc < self.start_utc:
            return "upcoming"
        if when_utc > self.end_utc:
            return "ended"
        return "active"


@dataclass
class ScheduleConfig:
    """All phase windows + per-wallet eligibility map for one drop."""
    phases: List[PhaseWindow] = field(default_factory=list)
    # eligibility: lowercase 0x-wallet → set of phase names it can mint
    eligibility: Dict[str, set] = field(default_factory=dict)

    def find_phase_by_name(self, name: str) -> Optional[PhaseWindow]:
        for p in self.phases:
            if p.name.lower() == (name or "").lower():
                return p
        return None

    def find_active_phase(self, now_utc: Optional[datetime] = None,
                          tolerance_seconds: int = 30) -> Optional[PhaseWindow]:
        """Return the phase whose window contains now_utc (with tolerance).

        If multiple overlap, prefer the most-recently-started one.
        """
        now_utc = now_utc or datetime.now(timezone.utc)
        active = [p for p in self.phases if p.is_active_at(now_utc, tolerance_seconds)]
        if not active:
            return None
        # Prefer most recent start
        active.sort(key=lambda p: p.start_utc, reverse=True)
        return active[0]

    def is_wallet_eligible(self, wallet_addr: str, phase_name: str) -> bool:
        addr = (wallet_addr or "").lower()
        names = self.eligibility.get(addr)
        if not names:
            return False
        # Case-insensitive phase name match
        target = (phase_name or "").lower()
        return any(n.lower() == target for n in names)

    def validate_signature(
        self,
        wallet_addr: str,
        now_utc: Optional[datetime] = None,
        tolerance_seconds: int = 60,
    ) -> Tuple[bool, str, Optional[PhaseWindow]]:
        """Decide whether to accept a signature from `wallet_addr` right now.

        Returns (ok, reason, matched_phase). ok=False means the bot
        should refuse to fire — reason is user-facing.
        """
        now_utc = now_utc or datetime.now(timezone.utc)
        if not self.phases:
            return True, "no schedule configured — accepting", None
        active = self.find_active_phase(now_utc, tolerance_seconds)
        if active is None:
            # Outside all phase windows
            return False, (
                f"no active phase at {now_utc.isoformat()} (±{tolerance_seconds}s)"
            ), None
        if not self.eligibility:
            return True, f"no wallet_eligibility map — accepting for {active.name}", active
        if self.is_wallet_eligible(wallet_addr, active.name):
            return True, f"wallet eligible for {active.name}", active
        return False, (
            f"wallet {short_addr(wallet_addr)} NOT eligible for active phase '{active.name}'"
        ), active


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp, returning a timezone-aware datetime in UTC.

    Accepts:
      - "2026-05-11T15:45:00Z"
      - "2026-05-11T15:45:00+00:00"
      - "2026-05-11T22:45:00+07:00"
      - "2026-05-11 15:45" (assumed UTC if no tzinfo)
    """
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    # Replace 'Z' suffix → '+00:00' (Python's fromisoformat accepts the latter)
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    # Allow "YYYY-MM-DD HH:MM" with a space instead of T
    if " " in raw and "T" not in raw:
        raw = raw.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("phase_eligibility: cannot parse timestamp %r", s)
        return None
    if dt.tzinfo is None:
        # Naive → assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_from_cfg(cfg: Dict) -> ScheduleConfig:
    """Build a ScheduleConfig from the bot's config dict.

    Reads `phase_schedule` and `wallet_eligibility` keys. Missing/empty
    means "no enforcement" — returns an empty ScheduleConfig.
    """
    sched = ScheduleConfig()
    raw_phases = cfg.get("phase_schedule") or {}
    if isinstance(raw_phases, dict):
        for name, window in raw_phases.items():
            if not isinstance(window, dict):
                continue
            start = _parse_iso(window.get("start", ""))
            end = _parse_iso(window.get("end", ""))
            if not start or not end:
                log.warning("phase_eligibility: skipping invalid phase %r (need start+end)", name)
                continue
            if end <= start:
                log.warning("phase_eligibility: phase %r has end<=start, skipping", name)
                continue
            sched.phases.append(PhaseWindow(name=str(name), start_utc=start, end_utc=end))
    # Sort phases by start time for nice display
    sched.phases.sort(key=lambda p: p.start_utc)

    raw_elig = cfg.get("wallet_eligibility") or {}
    if isinstance(raw_elig, dict):
        for addr, phase_list in raw_elig.items():
            addr_low = str(addr).lower().strip()
            if not addr_low.startswith("0x"):
                log.warning("phase_eligibility: wallet_eligibility key %r not a 0x address", addr)
                continue
            if isinstance(phase_list, str):
                phase_list = [phase_list]
            if not isinstance(phase_list, (list, tuple, set)):
                continue
            sched.eligibility[addr_low] = {str(p) for p in phase_list if p}
    return sched


def format_matrix(
    sched: ScheduleConfig,
    wallet_labels: Optional[Dict[str, str]] = None,
) -> str:
    """Return a human-readable eligibility matrix string.

    `wallet_labels` maps 0x... → friendly name (e.g. "main", "deni") for
    display. Wallets not in the eligibility map but in labels are still
    shown (with all-no row).
    """
    if not sched.phases:
        return "(no phase_schedule configured)"
    wallet_labels = wallet_labels or {}
    addrs = list(sched.eligibility.keys())
    # Also include labelled wallets even if absent from eligibility map
    for addr in wallet_labels:
        if addr.lower() not in addrs:
            addrs.append(addr.lower())
    if not addrs:
        return "(no wallet_eligibility configured)"

    # Column widths
    phase_names = [p.name for p in sched.phases]
    label_w = max(
        [len(wallet_labels.get(a, "") or short_addr(a)) for a in addrs] + [6]
    )
    col_w = max([len(n) for n in phase_names] + [4])

    header = "  " + " " * label_w + " | " + "  ".join(n.ljust(col_w) for n in phase_names)
    sep = "  " + "-" * label_w + "-+-" + "-".join("-" * (col_w + 1) for _ in phase_names)
    lines = [header, sep]
    for addr in addrs:
        label = wallet_labels.get(addr, "") or short_addr(addr)
        cells = []
        elig = sched.eligibility.get(addr, set())
        for n in phase_names:
            has = any(p.lower() == n.lower() for p in elig)
            mark = "OK  " if has else "--  "
            cells.append(mark.ljust(col_w))
        lines.append("  " + label.ljust(label_w) + " | " + "  ".join(cells))
    return "\n".join(lines)
