"""
core/config_schema.py

Strict, validated schema definitions for VFX's configuration file.

Using Pydantic here (rather than raw dicts) means malformed config.json
files fail loudly and specifically at load time — e.g. "thread_count must
be >= 1" — instead of causing a cryptic KeyError three modules deep during
a download. This is the difference between a config bug surfacing in two
seconds vs. two hours into a 500-track playlist run.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from core.presets import QUICK_PRESETS
from utils.path_manager import get_default_vfx_download_root

_VALID_PRESET_KEYS = frozenset(p.key for p in QUICK_PRESETS)
_SUPPORTED_COOKIE_BROWSERS = frozenset(
    {"chrome", "firefox", "edge", "brave", "opera", "vivaldi", "chromium", "safari"}
)


class ThemeMode(str, Enum):
    """Visual theme for the rich-rendered terminal UI."""

    DARK = "dark"
    LIGHT = "light"
    NEON = "neon"


class SpotifyCredentials(BaseModel):
    """
    Spotify Web API application credentials.

    These are NOT account credentials — they identify a Spotify Developer
    "app" registered at https://developer.spotify.com/dashboard, used for
    Client Credentials OAuth flow (read-only catalog/playlist access).
    """

    model_config = {"validate_assignment": True}

    client_id: Optional[str] = Field(default=None, description="Spotify app Client ID")
    client_secret: Optional[str] = Field(default=None, description="Spotify app Client Secret")

    @field_validator("client_id", "client_secret")
    @classmethod
    def strip_whitespace(cls, value: Optional[str]) -> Optional[str]:
        """Trim accidental whitespace/newlines from copy-pasted credentials."""
        if value is None:
            return value
        cleaned = value.strip()
        return cleaned or None

    @property
    def is_configured(self) -> bool:
        """True only if BOTH client_id and client_secret are present and non-empty."""
        return bool(self.client_id) and bool(self.client_secret)


class DownloadDefaults(BaseModel):
    """Default behavior applied to every download unless overridden per-session."""

    model_config = {"validate_assignment": True}

    download_path: str = Field(default_factory=lambda: str(get_default_vfx_download_root()))
    thread_count: int = Field(default=4, ge=1, le=16, description="Concurrent download workers")
    embed_metadata: bool = Field(default=True)
    embed_thumbnail: bool = Field(default=True)
    write_subtitles: bool = Field(default=False)
    preferred_audio_format: str = Field(default="mp3")
    preferred_video_format: str = Field(default="mp4")
    rate_limit_kbps: Optional[int] = Field(
        default=None, ge=64, description="Optional throttle in KB/s; None = unlimited"
    )
    default_preset_key: Optional[str] = Field(
        default=None,
        description="Preset key to auto-apply without prompting on every download; "
        "None means always show the selection menu.",
    )
    fetch_synced_lyrics: bool = Field(
        default=False,
        description="Fetch and save synced (.lrc) lyrics alongside audio downloads when available.",
    )
    skip_existing_files: bool = Field(
        default=True,
        description="Skip re-downloading a track if a valid file already exists at the "
        "destination path (duplicate prevention). Disable to force overwrite.",
    )

    @field_validator("download_path")
    @classmethod
    def expand_path(cls, value: str) -> str:
        """Normalize ~ and relative segments so downstream Path() calls are safe."""
        return str(Path(value).expanduser())

    @field_validator("default_preset_key")
    @classmethod
    def validate_preset_key(cls, value: Optional[str]) -> Optional[str]:
        """Reject unknown preset keys early rather than failing deep inside the engine."""
        if value is None:
            return value
        if value not in _VALID_PRESET_KEYS:
            raise ValueError(
                f"Unknown preset key '{value}'. Must be one of "
                f"{sorted(_VALID_PRESET_KEYS)} or null (always ask)."
            )
        return value


class AppConfig(BaseModel):
    """
    Root configuration model — the validated, in-memory representation of
    config.json. Everything in VFX that needs a setting reads it from an
    instance of this model, never from a raw dict.
    """

    spotify: SpotifyCredentials = Field(default_factory=SpotifyCredentials)
    defaults: DownloadDefaults = Field(default_factory=DownloadDefaults)
    theme: ThemeMode = Field(default=ThemeMode.NEON)
    ffmpeg_path: Optional[str] = Field(
        default=None, description="Explicit FFmpeg binary path override; None = auto-detect on PATH"
    )
    check_for_updates: bool = Field(
        default=True,
        description="Check for new VFX releases on startup. Informational only — never "
        "auto-installs anything.",
    )
    auto_update_ytdlp: bool = Field(
        default=False,
        description="Automatically update the yt-dlp engine on startup to keep extractors "
        "current against site changes. Off by default to keep startup fast and avoid "
        "unexpected network/package operations; the engine self-heals on extraction "
        "errors regardless of this setting (see utils/system_check.py).",
    )
    cookies_from_browser: Optional[str] = Field(
        default=None,
        description="Browser to extract cookies from for age-restricted/private content "
        "(e.g. 'chrome', 'firefox'). Mutually exclusive with cookies_file_path.",
    )
    cookies_file_path: Optional[str] = Field(
        default=None,
        description="Path to a manually exported cookies.txt file. Mutually exclusive "
        "with cookies_from_browser.",
    )
    config_version: int = Field(default=1, description="Schema version for future migrations")

    model_config = {"validate_assignment": True}

    @field_validator("cookies_from_browser")
    @classmethod
    def validate_browser(cls, value: Optional[str]) -> Optional[str]:
        """Catch typos in browser names at config-load time, not at engine runtime."""
        if value is None:
            return value
        cleaned = value.strip().lower()
        if cleaned not in _SUPPORTED_COOKIE_BROWSERS:
            raise ValueError(
                f"Unsupported browser '{value}'. Supported: {sorted(_SUPPORTED_COOKIE_BROWSERS)}."
            )
        return cleaned

    @field_validator("cookies_file_path")
    @classmethod
    def expand_cookies_path(cls, value: Optional[str]) -> Optional[str]:
        """Normalize ~ without requiring the file to exist yet at schema-validation time."""
        if value is None or not value.strip():
            return None
        return str(Path(value).expanduser())
