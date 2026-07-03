"""
utils/path_manager.py

All filesystem-path intelligence for VFX lives here:

1. Native "Downloads" folder detection per-OS (Section 2 of the spec) —
   not a naive `Path.home() / "Downloads"` guess, but an actual query of
   the OS's configured location, since both Windows (folder redirection)
   and Linux (XDG user-dirs, often localized) can legitimately differ
   from the English default.

2. Filesystem-safe name sanitization for playlist/track titles that
   preserves non-Latin scripts (Bengali, Arabic, CJK, etc.) — VFX pulls
   titles from a global media catalog, so an aggressive ASCII-only
   slugifier would mangle a large fraction of real-world titles.

3. `PathManager` — playlist folder isolation and duplicate-file
   detection, used by the (upcoming) download engine to decide where a
   file belongs and whether it's already been downloaded.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import re
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger()


class PathManagerError(Exception):
    """Raised when a required filesystem operation (mkdir, stat) fails."""


# ============================================================================
# Native "Downloads" folder detection
# ============================================================================


def _get_windows_downloads_folder() -> Optional[Path]:
    """
    Query Windows' actual Known Folder location for "Downloads" via the
    Shell API (SHGetKnownFolderPath), rather than assuming the default
    `%USERPROFILE%\\Downloads`.

    This matters because Windows allows users to relocate this folder
    (Properties -> Location tab) to another drive or a synced cloud
    folder (OneDrive, etc.) — a naive path guess would silently write
    files to the wrong place in that case.

    Returns None on any failure so the caller can fall back safely; this
    function must never raise.
    """
    try:
        from ctypes import wintypes  # Only resolvable on Windows builds of ctypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        # FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
        folder_id_downloads = GUID(
            0x374DE290,
            0x123F,
            0x4565,
            (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
        )

        path_ptr = ctypes.c_wchar_p()
        result_code = ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
            ctypes.byref(folder_id_downloads), 0, None, ctypes.byref(path_ptr)
        )

        if result_code != 0 or not path_ptr.value:
            logger.debug("SHGetKnownFolderPath returned HRESULT=%s", result_code)
            return None

        resolved = Path(path_ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)  # type: ignore[attr-defined]
        return resolved if resolved.exists() else None

    except (OSError, AttributeError, ValueError) as exc:
        logger.debug("Windows known-folder lookup failed, will fall back: %s", exc)
        return None


def _get_linux_downloads_folder() -> Path:
    """
    Respect XDG user-dirs configuration (written by `xdg-user-dirs-update`,
    used by GNOME, KDE, and most modern distros) so a relocated or
    localized Downloads folder (e.g. `~/Téléchargements` on a French
    locale system) is detected correctly instead of assuming the English
    folder name.

    Falls back to `~/Downloads` if no XDG config is present, which is
    correct for minimal/server distros and containers.
    """
    xdg_config_path = Path.home() / ".config" / "user-dirs.dirs"

    if xdg_config_path.exists():
        try:
            content = xdg_config_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if not line.startswith("XDG_DOWNLOAD_DIR"):
                    continue
                _, _, raw_value = line.partition("=")
                raw_value = raw_value.strip().strip('"')
                expanded = raw_value.replace("$HOME", str(Path.home()))
                resolved = Path(expanded)
                if resolved.exists():
                    return resolved
        except OSError as exc:
            logger.debug("Could not parse XDG user-dirs.dirs (%s): %s", xdg_config_path, exc)

    return Path.home() / "Downloads"


def get_native_downloads_folder() -> Path:
    """
    Return the OS's actual configured "Downloads" folder.

    Resolution strategy:
        - Windows: Shell API known-folder lookup, falling back to
          `%USERPROFILE%\\Downloads` if the lookup fails.
        - Linux: XDG user-dirs lookup, falling back to `~/Downloads`.
        - macOS / other Unix: `~/Downloads` (macOS does not support
          relocating this folder the way Windows does, so no special
          lookup is needed).
    """
    system = platform.system()

    if system == "Windows":
        detected = _get_windows_downloads_folder()
        return detected if detected is not None else Path.home() / "Downloads"

    if system == "Linux":
        return _get_linux_downloads_folder()

    return Path.home() / "Downloads"


def get_default_vfx_download_root() -> Path:
    """
    The native Downloads folder plus a dedicated "VFX" subfolder, so VFX's
    output stays clearly separated from the user's other downloaded files
    rather than mixing into a general-purpose folder.
    """
    return get_native_downloads_folder() / "VFX"


# ============================================================================
# Filesystem-safe name sanitization
# ============================================================================

_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

# Characters illegal on at least one of Windows/macOS/Linux, plus all
# control characters. Deliberately NOT touching anything outside this set —
# non-Latin scripts (Bengali, Arabic, CJK, etc.) must pass through untouched.
_ILLEGAL_CHARS_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REPEATED_UNDERSCORE_PATTERN = re.compile(r"_{2,}")


def sanitize_filename(name: str, max_length: int = 150, fallback: str = "untitled") -> str:
    """
    Make `name` safe to use as a file or directory name on Windows, macOS,
    and Linux simultaneously.

    Preserves all Unicode letters/scripts — only strips the specific
    characters that are filesystem-illegal (or reserved) on at least one
    target OS, plus control characters and unsafe trailing whitespace/dots.

    :param name: Raw title (e.g. a track or playlist title from an API).
    :param max_length: Hard cap to stay well under filesystem path-length
        limits even after a parent directory and extension are appended.
    :param fallback: Returned when `name` is empty or sanitizes to nothing.
    """
    if not name or not name.strip():
        return fallback

    cleaned = _ILLEGAL_CHARS_PATTERN.sub("_", name.strip())
    cleaned = cleaned.rstrip(" .")  # Windows disallows trailing dots/spaces
    cleaned = _REPEATED_UNDERSCORE_PATTERN.sub("_", cleaned)

    if not cleaned:
        return fallback

    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")

    return cleaned or fallback


# ============================================================================
# PathManager — playlist isolation + duplicate detection
# ============================================================================


class PathManager:
    """
    Owns filesystem decisions for a single download session: where a
    playlist's files live, what a given track's final path should be, and
    whether that path already represents a completed prior download.
    """

    def __init__(self, base_download_path: Path | str) -> None:
        self.base_download_path: Path = Path(base_download_path).expanduser()

    def ensure_base_exists(self) -> None:
        """
        Create the base download directory if it doesn't exist yet.

        :raises PathManagerError: if the directory cannot be created
            (permissions, invalid drive letter, read-only filesystem, etc.)
        """
        try:
            self.base_download_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Could not create base download directory %s: %s", self.base_download_path, exc)
            raise PathManagerError(
                f"Could not create download directory {self.base_download_path}: {exc}"
            ) from exc

    def resolve_playlist_directory(self, playlist_title: str, source_id: Optional[str] = None) -> Path:
        """
        Build and create an isolated sub-folder for a playlist/album, per
        the "Playlist Folder Isolation" requirement.

        When `source_id` is provided (e.g. a Spotify playlist ID or
        YouTube playlist ID), a short suffix derived from it is appended
        to the folder name. This serves two purposes:

        1. Two unrelated playlists that happen to share an identical
           title never collide into the same folder.
        2. Re-running the same playlist later deterministically resolves
           to the *same* folder, instead of naively appending "(1)",
           "(2)" on every re-run — which would otherwise scatter a
           playlist's tracks across several duplicate folders over time.

        :param playlist_title: Raw title as returned by the source API.
        :param source_id: Optional stable identifier for the playlist/album.
        :raises PathManagerError: if the folder cannot be created.
        """
        safe_title = sanitize_filename(playlist_title, fallback="Untitled Playlist")

        if source_id:
            short_id = sanitize_filename(source_id, max_length=8)
            folder_name = f"{safe_title} [{short_id}]"
        else:
            folder_name = safe_title

        target_dir = self.base_download_path / folder_name
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Could not create playlist folder %s: %s", target_dir, exc)
            raise PathManagerError(f"Could not create playlist folder {target_dir}: {exc}") from exc

        logger.info("Resolved playlist directory: %s", target_dir)
        return target_dir

    def is_already_downloaded(self, expected_path: Path, min_valid_size_bytes: int = 1024) -> bool:
        """
        Duplicate-prevention check used before starting a new download.

        A path counts as "already downloaded" only if it exists, is a
        regular file, AND is at least `min_valid_size_bytes` large. This
        deliberately rejects zero-byte or tiny leftover files from a
        previous interrupted download — a plain `Path.exists()` check
        would otherwise treat a corrupt partial file as complete and
        silently skip a track the user never actually received.
        """
        try:
            if not expected_path.exists() or not expected_path.is_file():
                return False
            return expected_path.stat().st_size >= min_valid_size_bytes
        except OSError as exc:
            logger.warning("Could not stat %s during duplicate check: %s", expected_path, exc)
            return False

    def find_existing_variant(self, directory: Path, base_filename: str) -> Optional[Path]:
        """
        Look for a previously-downloaded file with the same base name but
        a different extension — e.g. a track saved as `.m4a` in an earlier
        run, now being requested again as `.mp3`. Returns the first valid
        match, or None.

        This catches a duplicate-prevention edge case a simple exact-path
        check misses entirely: the same source media re-downloaded only
        because the *requested output container* changed between runs.
        """
        if not directory.exists():
            return None

        stem = Path(base_filename).stem
        try:
            for candidate in directory.glob(f"{stem}.*"):
                if self.is_already_downloaded(candidate):
                    return candidate
        except OSError as exc:
            logger.warning("Could not scan %s for existing variants: %s", directory, exc)
        return None
