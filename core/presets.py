"""
core/presets.py

Concrete definitions for the "Quick Presets" branch of the interactive
menu. These are pure data — yt-dlp format selectors and FFmpeg target
parameters — with zero engine logic. The actual download engine (built
in the next phase) will consume a `Preset` instance and translate it into
`yt-dlp` options + post-processor arguments.

Defining these now (rather than as TODO stubs) is deliberate: the
interactive menu in main.py needs real, selectable, descriptive objects
to render its rich Table and questionary choices today, even before the
engine that executes them exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MediaKind(str, Enum):
    """Whether a preset targets audio-only or audio+video output."""

    AUDIO = "audio"
    VIDEO = "video"


@dataclass(frozen=True)
class Preset:
    """
    A complete, ready-to-execute download profile.

    :param key: Stable internal identifier (used in config, logs, history DB).
    :param label: Human-readable name shown in menus.
    :param emoji: Single emoji glyph shown next to the label.
    :param description: One-line explanation shown under the label.
    :param kind: AUDIO or VIDEO.
    :param format_selector: The exact yt-dlp `-f` / `format` selector string.
    :param max_height: Optional hard resolution cap (height in px) used both
        as a yt-dlp filter hint and as an FFmpeg scale fallback for sources
        that don't natively offer that resolution.
    :param target_container: Final container format (mp4, mp3, m4a, 3gp, etc.).
    :param audio_bitrate_kbps: Target audio bitrate for re-encoded output.
    :param video_bitrate_kbps: Optional explicit video bitrate cap (used by
        the Feature Phone preset, where low bitrate matters more than
        resolution alone for playback compatibility).
    """

    key: str
    label: str
    emoji: str
    description: str
    kind: MediaKind
    format_selector: str
    target_container: str
    max_height: Optional[int] = None
    audio_bitrate_kbps: int = 192
    video_bitrate_kbps: Optional[int] = None

    @property
    def menu_title(self) -> str:
        """Formatted string for display in questionary/rich menus."""
        return f"{self.emoji}  {self.label} — {self.description}"


# ---------------------------------------------------------------------------
# Concrete preset catalog, matching feature spec section 2B exactly.
# ---------------------------------------------------------------------------

ORIGINAL_QUALITY = Preset(
    key="original_quality",
    label="Original Quality",
    emoji="🖥️",
    description="Best available video + audio, no re-encoding",
    kind=MediaKind.VIDEO,
    format_selector="bestvideo+bestaudio/best",
    target_container="MKV",  # mkv is the safe container when streams are merged without re-encode
    max_height=None,
)

COMPUTER_TV = Preset(
    key="computer_tv",
    label="Computer / TV",
    emoji="💻",
    description="1080p–4K MP4, high bitrate for large screens",
    kind=MediaKind.VIDEO,
    format_selector=(
        "bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=2160]+bestaudio/best[height<=2160]"
    ),
    target_container="mp4",
    max_height=2160,
    video_bitrate_kbps=8000,
)

MOBILE_PHONE = Preset(
    key="mobile_phone",
    label="Mobile Phone",
    emoji="📱",
    description="720p–1080p MP4, universal smartphone playback",
    kind=MediaKind.VIDEO,
    format_selector=(
        "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    ),
    target_container="mp4",
    max_height=1080,
    video_bitrate_kbps=4000,
)

FEATURE_PHONE = Preset(
    key="feature_phone",
    label="Feature Phone",
    emoji="📞",
    description="320x240 / 176x144, MP4 or 3GP, ultra-low bitrate",
    kind=MediaKind.VIDEO,
    # Deliberately uncapped selector here — Feature Phone ALWAYS re-encodes
    # via FFmpeg regardless of source resolution, so we grab the smallest
    # available source stream to minimize download time before re-encoding.
    format_selector="worstvideo+worstaudio/worst",
    target_container="3gp",
    max_height=240,
    audio_bitrate_kbps=32,
    video_bitrate_kbps=128,
)

AUDIO_BEST = Preset(
    key="audio_best",
    label="Audio Best",
    emoji="🎧",
    description="320kbps MP3 (or native M4A if already lossless-equivalent)",
    kind=MediaKind.AUDIO,
    format_selector="bestaudio/best",
    target_container="mp3",
    audio_bitrate_kbps=320,
)

QUICK_PRESETS: list[Preset] = [
    ORIGINAL_QUALITY,
    COMPUTER_TV,
    MOBILE_PHONE,
    FEATURE_PHONE,
    AUDIO_BEST,
]


def get_preset_by_key(key: str) -> Optional[Preset]:
    """Look up a preset by its stable key. Returns None if not found."""
    return next((p for p in QUICK_PRESETS if p.key == key), None)


# ---------------------------------------------------------------------------
# Manual Mode -> Preset conversion
#
# main.py's Manual Mode flow collects raw human choices ("1080p", "MP4")
# rather than yt-dlp selector syntax. Converting that into a real Preset
# here means the download engine never needs to know Manual Mode exists —
# it only ever consumes Preset objects, regardless of which menu branch
# produced one.
# ---------------------------------------------------------------------------

_AUDIO_QUALITY_KBPS: dict[str, int] = {
    "320 kbps (best)": 320,
    "256 kbps": 256,
    "192 kbps": 192,
    "128 kbps (smallest)": 128,
}
_AUDIO_FORMAT_CONTAINER: dict[str, str] = {
    "MP3": "mp3",
    "M4A": "m4a",
    "FLAC (if source supports lossless)": "flac",
    "OPUS": "opus",
}
_VIDEO_QUALITY_HEIGHT: dict[str, Optional[int]] = {
    "4K (2160p)": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "Lowest available": None,
}
_VIDEO_FORMAT_CONTAINER: dict[str, str] = {
    "MP4": "mp4",
    "MKV": "mkv",
    "WEBM": "webm",
    "3GP (legacy devices)": "3gp",
}


def build_manual_preset(kind: MediaKind, quality: str, fmt: str) -> Preset:
    """
    Convert a Manual Mode selection (raw UI strings) into a real Preset.

    Unrecognized quality/format strings fall back to a sane default
    (192kbps/mp3 for audio, 1080p/mp4 for video) rather than raising —
    Manual Mode's choices always come from main.py's own fixed menu, so an
    unrecognized value here would indicate a menu/converter drift bug, not
    bad user input; failing soft is safer than crashing mid-selection.
    """
    if kind == MediaKind.AUDIO:
        bitrate = _AUDIO_QUALITY_KBPS.get(quality, 192)
        container = _AUDIO_FORMAT_CONTAINER.get(fmt, "mp3")
        return Preset(
            key="manual_audio",
            label="Manual Audio",
            emoji="🎧",
            description=f"{bitrate}kbps {container.upper()}",
            kind=MediaKind.AUDIO,
            format_selector="bestaudio/best",
            target_container=container,
            audio_bitrate_kbps=bitrate,
        )

    height = _VIDEO_QUALITY_HEIGHT.get(quality, 1080)
    container = _VIDEO_FORMAT_CONTAINER.get(fmt, "mp4")
    if height is None:
        format_selector = "worstvideo+worstaudio/worst"
    else:
        format_selector = (
            f"bestvideo[height<={height}][ext={container}]+bestaudio/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        )
    return Preset(
        key="manual_video",
        label="Manual Video",
        emoji="🎬",
        description=f"{quality} {container.upper()}",
        kind=MediaKind.VIDEO,
        format_selector=format_selector,
        target_container=container,
        max_height=height,
        video_bitrate_kbps=8000 if height and height >= 1080 else 4000,
    )
