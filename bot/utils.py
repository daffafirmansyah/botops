"""Generic helpers: number formatting, time-sleep with countdown, address checks."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


WEI_PER_ETH = Decimal(10**18)
WEI_PER_GWEI = Decimal(10**9)


def is_hex_address(value: str) -> bool:
    """Return True if value looks like an Ethereum address (0x + 40 hex chars)."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v.startswith("0x") and not v.startswith("0X"):
        return False
    if len(v) != 42:
        return False
    try:
        int(v, 16)
        return True
    except ValueError:
        return False


def normalize_pk(pk: str) -> str:
    """Return a 0x-prefixed lower-case private key. Validates length/hex."""
    s = (pk or "").strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        raise ValueError(f"private key must be 64 hex chars (got {len(s)})")
    int(s, 16)  # raises if non-hex
    return "0x" + s.lower()


def wei_to_eth(wei: int) -> Decimal:
    """Convert wei to ETH as a Decimal (preserves precision)."""
    return Decimal(int(wei)) / WEI_PER_ETH


def eth_to_wei(eth) -> int:
    """Convert ETH (str/Decimal/float) to wei as int."""
    if isinstance(eth, float):
        eth = str(eth)
    return int(Decimal(eth) * WEI_PER_ETH)


def gwei_to_wei(gwei) -> int:
    """Convert gwei (str/Decimal/float/int) to wei as int."""
    if isinstance(gwei, float):
        gwei = str(gwei)
    return int(Decimal(gwei) * WEI_PER_GWEI)


def wei_to_gwei(wei: int) -> Decimal:
    """Convert wei to gwei as a Decimal."""
    return Decimal(int(wei)) / WEI_PER_GWEI


def fmt_eth(wei: int, symbol: str = "ETH", decimals: int = 6) -> str:
    """Format wei as a short ETH string, e.g. '0.012345 ETH'."""
    eth = wei_to_eth(wei)
    return f"{eth:.{decimals}f} {symbol}"


def fmt_gwei(wei: int, decimals: int = 3) -> str:
    """Format wei as a short gwei string, e.g. '12.500 gwei'."""
    return f"{wei_to_gwei(wei):.{decimals}f} gwei"


def fmt_timestamp(ts: int) -> str:
    """Format unix timestamp as ISO UTC. Returns '-' for non-positive values."""
    if not ts or ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (OSError, ValueError, OverflowError):
        return "-"


def now_unix() -> int:
    """Return current unix timestamp (seconds, UTC)."""
    return int(time.time())


def short_addr(addr: str) -> str:
    """Return short 0xAAAA…BBBB representation of an address."""
    if not addr or not isinstance(addr, str) or len(addr) < 10:
        return str(addr)
    return f"{addr[:6]}…{addr[-4:]}"


def humanize_seconds(seconds: int) -> str:
    """Convert seconds to human-readable duration string."""
    seconds = int(seconds)
    if seconds < 0:
        return f"-{humanize_seconds(-seconds)}"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m {sec}s"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h {minutes}m"


def sleep_until(target_ts: float, on_tick=None, tick_seconds: float = 1.0) -> None:
    """Sleep until the given unix timestamp (seconds, can be float).

    `on_tick(remaining_seconds)` is called once per `tick_seconds` while waiting
    so the caller can render a countdown. Returns immediately when the target
    time has already passed.
    """
    while True:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        if on_tick is not None:
            try:
                on_tick(remaining)
            except Exception:  # pragma: no cover - tick should never break wait
                pass
        time.sleep(min(tick_seconds, max(remaining, 0.01)))


def parse_int(value, default: int = 0) -> int:
    """Best-effort int parser used for user input. Empty/None -> default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    s = str(value).strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return default


def parse_float(value, default: float = 0.0) -> float:
    """Best-effort float parser used for user input."""
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def safe_get(obj, *keys, default: Optional[object] = None):
    """Walk nested dicts/lists and return default on any missing key/index."""
    cur = obj
    for k in keys:
        if cur is None:
            return default
        try:
            cur = cur[k]
        except (KeyError, IndexError, TypeError):
            return default
    return cur if cur is not None else default
