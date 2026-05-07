"""Color-aware logging setup.

Writes a stream handler with ANSI colors to the console (when supported) and
optionally a rotating file handler. Designed to be called once at start-up.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

try:  # pragma: no cover - colorama is optional but in requirements.txt
    from colorama import Fore, Style, init as colorama_init

    colorama_init()
    _COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
    }
    _RESET = Style.RESET_ALL
except Exception:  # pragma: no cover
    _COLORS = {}
    _RESET = ""


class _ColorFormatter(logging.Formatter):
    """Formatter that injects ANSI color codes per level."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        color = _COLORS.get(record.levelno, "")
        message = super().format(record)
        if color:
            return f"{color}{message}{_RESET}"
        return message


_CONSOLE_FMT = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"
_FILE_FMT = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Configure root logging once and return the root logger.

    Repeated calls are idempotent: handlers will be replaced.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    log_level = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(log_level)

    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(_ColorFormatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
    root.addHandler(console)

    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATE_FMT))
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            root.warning("Could not initialise file logger '%s': %s", log_file, exc)

    # Silence overly-verbose third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that inherits from the configured root logger."""
    return logging.getLogger(name)
