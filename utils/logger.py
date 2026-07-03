"""
utils/logger.py

Centralized logging configuration for VFX.

Design intent: errors and tracebacks are logged *silently* to `vfx.log`
(rotating, so it never grows unbounded), while the user-facing terminal
only ever sees short, friendly, rich-formatted messages. Modules should
never call `print()` directly for errors — they should log here and let
the UI layer render the friendly version via ui/console.py.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "vfx.log"

_LOGGER_NAME = "vfx"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per log file before rotation
_BACKUP_COUNT = 3


def get_logger() -> logging.Logger:
    """
    Return the singleton VFX logger, configuring it on first call.

    Safe to call repeatedly from any module — logging.getLogger() returns
    the same instance, and handlers are only attached once.
    """
    logger = logging.getLogger(_LOGGER_NAME)

    if logger.handlers:
        # Already configured by an earlier call — don't double-attach handlers,
        # which would otherwise duplicate every log line.
        return logger

    logger.setLevel(logging.DEBUG)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    except OSError:
        # If we can't even write a log file (e.g. read-only filesystem, permissions),
        # fall back to a null handler so the app doesn't crash on logger.error() calls.
        logger.addHandler(logging.NullHandler())

    # Intentionally NOT adding a StreamHandler here — terminal output is owned
    # exclusively by the rich-based UI layer, never by the stdlib logging module.
    logger.propagate = False
    return logger
