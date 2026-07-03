"""
core/config.py

Central configuration manager for VFX.

Responsibilities
-----------------
1. Locate and load `config.json` (creating a default one on first run).
2. Overlay `.env` values for Spotify credentials (environment takes
   precedence over the JSON file, so CI / Docker / shared-machine users
   can avoid putting secrets on disk at all).
3. Validate everything through `AppConfig` (Pydantic) before anything else
   in the app is allowed to read it.
4. Persist changes back to disk **atomically** — writes go to a temp file
   first and are only renamed over the real config once fully flushed,
   so a crash or power loss mid-write can never corrupt config.json.

This module deliberately has zero dependency on `rich` or `questionary`.
It is pure logic. The interactive "Config Manager" *panel* described in
the feature spec lives in `ui/config_panel.py` and calls into this class —
keeping presentation and state management strictly separated.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import ValidationError

from core.config_schema import AppConfig
from utils.logger import get_logger

logger = get_logger()

# Project root is the parent of this file's parent (core/ -> vfx/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(Exception):
    """Base exception for all configuration-related failures."""


class ConfigLoadError(ConfigError):
    """Raised when config.json exists but cannot be parsed or validated."""


class ConfigWriteError(ConfigError):
    """Raised when config.json cannot be written to disk."""


class ConfigManager:
    """
    Loads, validates, mutates, and persists VFX's configuration.

    Usage
    -----
        config_manager = ConfigManager()
        config = config_manager.load()
        config_manager.set_spotify_credentials("abc123", "secret456")
        config_manager.save()

    The manager holds exactly one in-memory `AppConfig` instance at a time
    (`self._config`), accessible via the `.config` property. All mutator
    methods operate on that instance and require an explicit `.save()`
    call to persist — this keeps "preview changes" (e.g. in an interactive
    settings panel) cheap and side-effect-free until the user confirms.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        env_path: Optional[Path] = None,
    ) -> None:
        self.config_path: Path = config_path or DEFAULT_CONFIG_PATH
        self.env_path: Path = env_path or DEFAULT_ENV_PATH
        self._config: Optional[AppConfig] = None

    # ------------------------------------------------------------------ #
    # Public properties
    # ------------------------------------------------------------------ #

    @property
    def config(self) -> AppConfig:
        """
        Return the currently loaded config, loading it from disk first
        if this is the first access in this process.
        """
        if self._config is None:
            self._config = self.load()
        return self._config

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def load(self) -> AppConfig:
        """
        Load configuration from disk, applying `.env` overrides, and
        validate it into an `AppConfig`.

        If `config.json` does not exist, a default config is created and
        written to disk so subsequent runs (and direct inspection by the
        user) have a real file to look at.

        :raises ConfigLoadError: if an existing config.json is present but
            is malformed JSON or fails schema validation.
        """
        self._load_dotenv_if_present()

        if not self.config_path.exists():
            logger.info("No config.json found at %s — creating default.", self.config_path)
            default_config = AppConfig()
            self._config = default_config
            self.save()
            self._apply_env_overrides()
            return self._config

        try:
            raw_text = self.config_path.read_text(encoding="utf-8")
            raw_data: dict[str, Any] = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.error("config.json is not valid JSON: %s", exc)
            raise ConfigLoadError(
                f"Your config.json contains invalid JSON near line {exc.lineno}, "
                f"column {exc.colno}. Fix the syntax or delete the file to "
                f"regenerate a default one."
            ) from exc
        except OSError as exc:
            logger.error("Could not read config.json: %s", exc)
            raise ConfigLoadError(f"Could not read {self.config_path}: {exc}") from exc

        try:
            self._config = AppConfig.model_validate(raw_data)
        except ValidationError as exc:
            logger.error("config.json failed schema validation: %s", exc)
            readable_errors = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            raise ConfigLoadError(f"Your config.json has invalid values: {readable_errors}") from exc

        self._apply_env_overrides()
        return self._config

    def _load_dotenv_if_present(self) -> None:
        """Load `.env` into process environment variables if the file exists."""
        if self.env_path.exists():
            load_dotenv(dotenv_path=self.env_path, override=False)
            logger.debug(".env loaded from %s", self.env_path)

    def _apply_env_overrides(self) -> None:
        """
        Environment variables take precedence over config.json for secrets.

        Recognized variables:
            VFX_SPOTIFY_CLIENT_ID
            VFX_SPOTIFY_CLIENT_SECRET
        """
        if self._config is None:
            return

        env_client_id = os.environ.get("VFX_SPOTIFY_CLIENT_ID")
        env_client_secret = os.environ.get("VFX_SPOTIFY_CLIENT_SECRET")

        if env_client_id:
            self._config.spotify.client_id = env_client_id.strip()
            logger.debug("Spotify client_id overridden from environment.")
        if env_client_secret:
            self._config.spotify.client_secret = env_client_secret.strip()
            logger.debug("Spotify client_secret overridden from environment.")

    # ------------------------------------------------------------------ #
    # Saving
    # ------------------------------------------------------------------ #

    def save(self) -> None:
        """
        Persist the in-memory config to disk atomically.

        Writes to a temporary file in the same directory (so the rename
        is on the same filesystem and therefore atomic on POSIX and
        near-atomic on Windows), then replaces config.json.

        :raises ConfigWriteError: if the file cannot be written.
        """
        if self._config is None:
            raise ConfigWriteError("No configuration loaded in memory to save.")

        # NOTE: secrets sourced purely from environment variables are still
        # written to disk here if they were also set via the in-memory model.
        # This is intentional — explicit user action (e.g. the config panel)
        # should be able to persist credentials. Pure env-only overrides that
        # were never explicitly saved by the user remain env-only because
        # `_apply_env_overrides` only mutates the in-memory object, and `save()`
        # is only called explicitly by mutator methods or first-run init.
        payload = self._config.model_dump(mode="json")

        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path_str = tempfile.mkstemp(
                dir=self.config_path.parent, prefix=".config_", suffix=".tmp"
            )
            tmp_path = Path(tmp_path_str)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                    json.dump(payload, tmp_file, indent=2, ensure_ascii=False)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                tmp_path.replace(self.config_path)
            finally:
                # If replace() succeeded, tmp_path no longer exists — this is a no-op.
                # If something failed before replace(), clean up the leftover temp file.
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.error("Failed to write config.json: %s", exc)
            raise ConfigWriteError(f"Could not save configuration: {exc}") from exc

        logger.info("Configuration saved to %s", self.config_path)

    # ------------------------------------------------------------------ #
    # Mutators (operate on in-memory config; caller must call save())
    # ------------------------------------------------------------------ #

    def set_spotify_credentials(self, client_id: str, client_secret: str) -> None:
        """Update Spotify credentials in memory. Call `.save()` to persist."""
        self.config.spotify.client_id = client_id.strip()
        self.config.spotify.client_secret = client_secret.strip()
        logger.info("Spotify credentials updated in memory.")

    def set_download_path(self, path: str) -> None:
        """Update the default download directory in memory."""
        expanded = str(Path(path).expanduser())
        self.config.defaults.download_path = expanded
        logger.info("Default download path set to %s", expanded)

    def set_thread_count(self, count: int) -> None:
        """
        Update the concurrency limit for parallel downloads.

        :raises ValueError: if count is outside the 1-16 bound enforced
            by the schema (re-raised from Pydantic's validate_assignment).
        """
        try:
            self.config.defaults.thread_count = count
        except ValidationError as exc:
            raise ValueError(f"Invalid thread count: {exc.errors()[0]['msg']}") from exc
        logger.info("Thread count set to %d", count)

    def toggle_metadata_embedding(self, enabled: bool) -> None:
        """Enable/disable automatic ID3/MP4 metadata tagging."""
        self.config.defaults.embed_metadata = enabled
        logger.info("Metadata embedding set to %s", enabled)

    def toggle_thumbnail_embedding(self, enabled: bool) -> None:
        """Enable/disable automatic thumbnail/album-art embedding."""
        self.config.defaults.embed_thumbnail = enabled
        logger.info("Thumbnail embedding set to %s", enabled)

    def set_ffmpeg_path(self, path: Optional[str]) -> None:
        """Override the auto-detected FFmpeg binary location, or clear it (None)."""
        self.config.ffmpeg_path = path
        logger.info("FFmpeg path override set to %s", path or "(auto-detect)")

    def set_default_preset(self, key: Optional[str]) -> None:
        """
        Set the preset auto-applied on every download without prompting.

        :param key: A valid preset key from core.presets.QUICK_PRESETS, or
            None to always show the selection menu.
        :raises ValueError: if `key` doesn't match a known preset.
        """
        try:
            self.config.defaults.default_preset_key = key
        except ValidationError as exc:
            raise ValueError(f"Invalid preset key: {exc.errors()[0]['msg']}") from exc
        logger.info("Default preset set to %s", key or "(always ask)")

    def set_rate_limit(self, kbps: Optional[int]) -> None:
        """
        Set a download speed cap in KB/s, or clear it (None = unlimited).

        :raises ValueError: if kbps is set but below the schema's 64 KB/s floor.
        """
        try:
            self.config.defaults.rate_limit_kbps = kbps
        except ValidationError as exc:
            raise ValueError(f"Invalid rate limit: {exc.errors()[0]['msg']}") from exc
        logger.info("Rate limit set to %s", f"{kbps} KB/s" if kbps else "(unlimited)")

    def toggle_auto_update_ytdlp(self, enabled: bool) -> None:
        """Enable/disable automatic yt-dlp updates on startup."""
        self.config.auto_update_ytdlp = enabled
        logger.info("Auto-update yt-dlp set to %s", enabled)

    def toggle_synced_lyrics(self, enabled: bool) -> None:
        """Enable/disable fetching synced (.lrc) lyrics alongside audio downloads."""
        self.config.defaults.fetch_synced_lyrics = enabled
        logger.info("Synced lyrics fetching set to %s", enabled)

    def toggle_skip_existing(self, enabled: bool) -> None:
        """Enable/disable duplicate-prevention (skip files that already exist)."""
        self.config.defaults.skip_existing_files = enabled
        logger.info("Skip-existing-files set to %s", enabled)

    def set_browser_cookies(self, browser: Optional[str]) -> None:
        """
        Use cookies extracted from an installed browser for age-restricted
        or private content. Clears any manually-set cookies file, since
        yt-dlp only accepts one cookie source at a time and silently
        picking between two configured sources would be surprising.

        :raises ValueError: if `browser` isn't a recognized browser name.
        """
        try:
            self.config.cookies_from_browser = browser
        except ValidationError as exc:
            raise ValueError(f"Invalid browser: {exc.errors()[0]['msg']}") from exc
        if browser:
            self.config.cookies_file_path = None
        logger.info("Cookie source set to browser=%s", browser)

    def set_cookies_file(self, path: Optional[str]) -> None:
        """
        Use a manually exported cookies.txt file for age-restricted or
        private content. Clears any browser-cookie setting (see
        `set_browser_cookies` for why these stay mutually exclusive).
        """
        expanded = str(Path(path).expanduser()) if path and path.strip() else None
        self.config.cookies_file_path = expanded
        if expanded:
            self.config.cookies_from_browser = None
        logger.info("Cookie source set to file=%s", expanded or "(none)")

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def is_spotify_configured(self) -> bool:
        """True if both Spotify client_id and client_secret are present."""
        return self.config.spotify.is_configured

    def describe_source(self) -> str:
        """
        Human-readable summary of where config is being loaded from —
        used by the UI's startup banner and the `--config` panel header.
        """
        env_note = " (+ .env overrides applied)" if self.env_path.exists() else ""
        return f"{self.config_path}{env_note}"
