"""
utils/system_check.py

Pre-flight environment checks that run before VFX does anything else.

Currently covers the FFmpeg binary check described in the "Self-Healing"
feature requirement. Designed to be cheap (a single `shutil.which` call
plus an optional version probe) so it never noticeably delays startup.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

from utils.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class FFmpegStatus:
    """Result of an FFmpeg availability probe."""

    found: bool
    path: Optional[str]
    version: Optional[str]


def check_ffmpeg(explicit_path: Optional[str] = None) -> FFmpegStatus:
    """
    Determine whether a usable FFmpeg binary is available.

    Resolution order:
        1. `explicit_path` (from config.json's `ffmpeg_path`, if the user set one)
        2. System PATH (via shutil.which)

    :param explicit_path: optional user-configured override path to the binary.
    :return: FFmpegStatus describing what was found, if anything.
    """
    candidate = explicit_path or shutil.which("ffmpeg")

    if not candidate:
        logger.warning("FFmpeg binary not found on PATH and no override configured.")
        return FFmpegStatus(found=False, path=None, version=None)

    resolved = shutil.which(candidate) or candidate

    try:
        result = subprocess.run(
            [resolved, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("FFmpeg at %s exited non-zero on -version probe.", resolved)
            return FFmpegStatus(found=False, path=resolved, version=None)

        first_line = result.stdout.splitlines()[0] if result.stdout else "unknown version"
        logger.info("FFmpeg detected at %s — %s", resolved, first_line)
        return FFmpegStatus(found=True, path=resolved, version=first_line)

    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("FFmpeg probe failed for path %s: %s", resolved, exc)
        return FFmpegStatus(found=False, path=resolved, version=None)


def install_hint() -> str:
    """
    Return a platform-aware, friendly install hint for FFmpeg.

    Pure string logic — the UI layer decides how to render this
    (rich Panel, plain print, etc.).
    """
    import platform

    system = platform.system().lower()
    if "windows" in system:
        return (
            "Install FFmpeg on Windows via:\n"
            "  winget install ffmpeg\n"
            "or download from https://ffmpeg.org/download.html and add it to PATH."
        )
    if "darwin" in system:
        return "Install FFmpeg on macOS via:\n  brew install ffmpeg"
    return (
        "Install FFmpeg on Linux via your package manager, e.g.:\n"
        "  sudo apt install ffmpeg      # Debian/Ubuntu\n"
        "  sudo dnf install ffmpeg      # Fedora\n"
        "  sudo pacman -S ffmpeg        # Arch"
    )
