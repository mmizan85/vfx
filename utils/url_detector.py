"""
utils/url_detector.py

Classifies a pasted URL by source platform without making any network
calls. This is intentionally a pure, fast, regex-based first pass — full
verification (e.g. "does this Spotify playlist actually exist and is it
public?") happens later in core/spotify_client.py, which DOES hit the
network and therefore belongs behind the async/try-except boundary, not
here.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import NamedTuple, Optional


class MediaSource(str, Enum):
    """Recognized source platforms."""

    SPOTIFY = "spotify"
    YOUTUBE = "youtube"
    YOUTUBE_MUSIC = "youtube_music"
    GENERIC = "generic"  # Any other yt-dlp-supported site (SoundCloud, Vimeo, etc.)
    INVALID = "invalid"


class SpotifyResourceType(str, Enum):
    """Spotify URLs encode the resource type directly in the path."""

    TRACK = "track"
    ALBUM = "album"
    PLAYLIST = "playlist"
    ARTIST = "artist"
    UNKNOWN = "unknown"


class DetectionResult(NamedTuple):
    """Outcome of classifying a single URL."""

    source: MediaSource
    spotify_type: Optional[SpotifyResourceType]
    resource_id: Optional[str]
    original_url: str


_SPOTIFY_PATTERN = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|playlist|artist)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_SPOTIFY_URI_PATTERN = re.compile(r"^spotify:(track|album|playlist|artist):([A-Za-z0-9]+)$")
_YOUTUBE_MUSIC_PATTERN = re.compile(r"music\.youtube\.com", re.IGNORECASE)
_YOUTUBE_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|playlist\?list=|shorts/)|youtu\.be/)", re.IGNORECASE
)
_URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


def detect(url: str) -> DetectionResult:
    """
    Classify a URL or Spotify URI by source platform.

    :param url: Raw string as pasted by the user (whitespace-trimmed internally).
    :return: A DetectionResult. `source` is MediaSource.INVALID if the input
        is not a recognizable URL/URI at all (e.g. empty string, plain text).

    Examples
    --------
    >>> detect("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M").source
    <MediaSource.SPOTIFY: 'spotify'>
    >>> detect("https://www.youtube.com/watch?v=dQw4w9WgXcQ").source
    <MediaSource.YOUTUBE: 'youtube'>
    >>> detect("not a url").source
    <MediaSource.INVALID: 'invalid'>
    """
    cleaned = url.strip()

    if not cleaned:
        return DetectionResult(MediaSource.INVALID, None, None, cleaned)

    spotify_match = _SPOTIFY_PATTERN.search(cleaned)
    if spotify_match:
        resource_type_str, resource_id = spotify_match.groups()
        return DetectionResult(
            MediaSource.SPOTIFY,
            SpotifyResourceType(resource_type_str.lower()),
            resource_id,
            cleaned,
        )

    uri_match = _SPOTIFY_URI_PATTERN.match(cleaned)
    if uri_match:
        resource_type_str, resource_id = uri_match.groups()
        return DetectionResult(
            MediaSource.SPOTIFY,
            SpotifyResourceType(resource_type_str.lower()),
            resource_id,
            cleaned,
        )

    if _YOUTUBE_MUSIC_PATTERN.search(cleaned):
        return DetectionResult(MediaSource.YOUTUBE_MUSIC, None, None, cleaned)

    if _YOUTUBE_PATTERN.search(cleaned):
        return DetectionResult(MediaSource.YOUTUBE, None, None, cleaned)

    if _URL_PATTERN.match(cleaned):
        # A well-formed URL that isn't Spotify/YouTube — hand it to yt-dlp's
        # generic extractor later (SoundCloud, Vimeo, Bandcamp, etc.).
        return DetectionResult(MediaSource.GENERIC, None, None, cleaned)

    return DetectionResult(MediaSource.INVALID, None, None, cleaned)


def is_playlist_like(result: DetectionResult) -> bool:
    """True if this resource likely contains multiple tracks/videos."""
    if result.source == MediaSource.SPOTIFY:
        return result.spotify_type in (SpotifyResourceType.PLAYLIST, SpotifyResourceType.ALBUM)
    if result.source in (MediaSource.YOUTUBE, MediaSource.YOUTUBE_MUSIC):
        return "list=" in result.original_url or "playlist" in result.original_url.lower()
    return False
