"""
core/spotify_engine.py

Resolves Spotify URLs → spotipy metadata → YouTube search queries,
which the YouTubeEngine then downloads.

Spotify's API provides: title, artist, album, year, duration, track
number, and album-art thumbnail URLs — everything VFX needs for the ID3
embed step. What it doesn't provide is the actual audio file (DRM).
So our flow is:

    Spotify URL
        └─→ SpotifyEngine.resolve()
                └─→ list[SpotifyTrack]  (metadata)
                        └─→ each track.search_query  (ytsearch1:...)
                                └─→ YouTubeEngine.download()

Search-query accuracy notes
-----------------------------
The spec calls for "highly accurate search queries". The simple approach
of `"Artist - Title"` works most of the time but fails for:
  - Tracks with colons or commas in the title (YouTube search tokenises them)
  - Tracks with featured artists in different formats ("ft.", "feat.", "&")
  - Live/acoustic/remix versions with ambiguous parenthetical suffixes
  - Tracks where the YouTube Music title includes "(Official Audio)" etc.

We mitigate all of these with:
  1. Exact-phrase quoting for the title segment
  2. A two-stage query strategy: `ytsearch1:` (exact) with an automatic
     fallback to a cleaned `ytsearch3:` (looser) if the first result's
     duration is >20% off the Spotify duration.
  3. Duration-based similarity scoring so we pick the closest match among
     the top-3 fallback results rather than always taking result[0].

The duration fallback is implemented on the YouTubeEngine side (since it
needs a yt-dlp extract to get video duration). SpotifyEngine only builds
the two query strings and exposes the Spotify duration for comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from tenacity import retry, stop_after_attempt, wait_exponential

from utils.logger import get_logger

logger = get_logger()


class SpotifyEngineError(Exception):
    """Raised for Spotify-level failures (auth, bad URL, API quota, etc.)."""


class SpotifyCredentialError(SpotifyEngineError):
    """Raised specifically when client_id or client_secret are missing/invalid."""


@dataclass
class SpotifyTrack:
    """
    All Spotify metadata for one track, plus the pre-built YouTube search
    queries the engine will use if an exact match isn't found.
    """

    title: str
    artist: str
    album: str
    spotify_id: str
    search_query_exact: str   # `ytsearch1: "Artist - Title"` — tight, used first
    search_query_loose: str   # `ytsearch3: Artist Title` — broader fallback
    year: Optional[str] = None
    track_number: Optional[int] = None
    total_tracks: Optional[int] = None
    duration_ms: int = 0
    thumbnail_url: Optional[str] = None
    genre: Optional[str] = None
    additional_artists: list[str] = field(default_factory=list)

    @property
    def display_artist(self) -> str:
        """Full artist credit, e.g. 'Daft Punk ft. Pharrell Williams'."""
        if not self.additional_artists:
            return self.artist
        others = ", ".join(self.additional_artists[:2])
        return f"{self.artist} ft. {others}"

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000.0


class SpotifyEngine:
    """
    Fetches Spotify metadata and builds YouTube search queries.

    Authentication uses the Client Credentials OAuth flow — read-only
    catalog access, no user login required. Credentials come from
    the VFX config (or the VFX_SPOTIFY_CLIENT_ID / VFX_SPOTIFY_CLIENT_SECRET
    environment variables, per ConfigManager's precedence rules).

    :param client_id: Spotify application Client ID.
    :param client_secret: Spotify application Client Secret.
    :raises SpotifyCredentialError: if either credential is falsy.
    """

    # Spotify API pagination limit (their max per request is 100 for tracks,
    # 50 for playlist items — use 100 everywhere we can for fewer round-trips)
    _PAGE_LIMIT = 100

    def __init__(self, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            raise SpotifyCredentialError(
                "Spotify Client ID and Secret are both required. "
                "Run `vfx --config` to add your credentials from "
                "https://developer.spotify.com/dashboard."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._sp: Optional[spotipy.Spotify] = None

    @property
    def sp(self) -> spotipy.Spotify:
        """Lazy-initialised spotipy client (avoids auth until first real call)."""
        if self._sp is None:
            try:
                auth = SpotifyClientCredentials(
                    client_id=self._client_id,
                    client_secret=self._client_secret,
                )
                self._sp = spotipy.Spotify(auth_manager=auth, requests_timeout=15)
            except Exception as exc:
                raise SpotifyCredentialError(
                    f"Spotify authentication failed. Check your Client ID and Secret. ({exc})"
                ) from exc
        return self._sp

    # ------------------------------------------------------------------ #
    # Public resolution entry point
    # ------------------------------------------------------------------ #

    def resolve(self, url: str) -> tuple[str, list[SpotifyTrack]]:
        """
        Resolve any Spotify URL to `(collection_title, list_of_SpotifyTrack)`.

        :param url: A Spotify URL (track, album, or playlist) or URI.
        :returns: (title, tracks) where `title` is suitable for a folder name
            and `tracks` is the ordered list of SpotifyTrack objects to download.
        :raises SpotifyEngineError: for API errors, invalid URLs, or empty results.
        """
        resource_type, resource_id = self._parse_url(url)

        if resource_type == "track":
            track = self._fetch_track(resource_id)
            return track.title, [track]
        elif resource_type == "album":
            return self._fetch_album(resource_id)
        elif resource_type == "playlist":
            return self._fetch_playlist(resource_id)
        else:
            raise SpotifyEngineError(
                f"Unsupported Spotify resource type: '{resource_type}'. "
                "VFX supports track, album, and playlist URLs."
            )

    # ------------------------------------------------------------------ #
    # Resource-specific fetchers
    # ------------------------------------------------------------------ #

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _fetch_track(self, track_id: str) -> SpotifyTrack:
        try:
            data = self.sp.track(track_id)
        except spotipy.SpotifyException as exc:
            raise SpotifyEngineError(f"Failed to fetch track {track_id}: {exc}") from exc
        return self._parse_track(data)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _fetch_album(self, album_id: str) -> tuple[str, list[SpotifyTrack]]:
        try:
            album_data = self.sp.album(album_id)
        except spotipy.SpotifyException as exc:
            raise SpotifyEngineError(f"Failed to fetch album {album_id}: {exc}") from exc

        album_title = album_data.get("name", "Unknown Album")
        year = self._extract_year(album_data.get("release_date", ""))
        thumbnail_url = self._best_image(album_data.get("images", []))
        genre = (album_data.get("genres") or [None])[0]
        total_tracks = album_data.get("total_tracks", 0)

        tracks: list[SpotifyTrack] = []
        page = album_data.get("tracks", {})
        while page:
            for item in page.get("items") or []:
                if item:
                    track = self._parse_album_track(
                        item,
                        album_name=album_title,
                        year=year,
                        thumbnail_url=thumbnail_url,
                        genre=genre,
                        total_tracks=total_tracks,
                    )
                    tracks.append(track)
            next_url = page.get("next")
            page = self.sp.next(page) if next_url else None

        if not tracks:
            raise SpotifyEngineError(f"Album '{album_title}' has no accessible tracks.")
        logger.info("Fetched album '%s' — %d tracks", album_title, len(tracks))
        return album_title, tracks

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    def _fetch_playlist(self, playlist_id: str) -> tuple[str, list[SpotifyTrack]]:
        try:
            playlist_data = self.sp.playlist(playlist_id, fields="name,id,tracks,images")
        except spotipy.SpotifyException as exc:
            raise SpotifyEngineError(f"Failed to fetch playlist {playlist_id}: {exc}") from exc

        playlist_title = playlist_data.get("name", "Unknown Playlist")
        tracks: list[SpotifyTrack] = []
        page = playlist_data.get("tracks", {})

        while page:
            for item in page.get("items") or []:
                # Playlist items are wrapped in an extra "track" key
                raw_track = (item or {}).get("track")
                if raw_track and raw_track.get("id"):
                    # Skip local files (no Spotify ID = no YouTube match possible)
                    if raw_track.get("is_local"):
                        logger.debug("Skipping local file: %s", raw_track.get("name"))
                        continue
                    track = self._parse_track(raw_track)
                    tracks.append(track)
            next_url = page.get("next")
            page = self.sp.next(page) if next_url else None

        if not tracks:
            raise SpotifyEngineError(f"Playlist '{playlist_title}' has no accessible tracks.")
        logger.info("Fetched playlist '%s' — %d tracks", playlist_title, len(tracks))
        return playlist_title, tracks

    # ------------------------------------------------------------------ #
    # Track data parsing
    # ------------------------------------------------------------------ #

    def _parse_track(self, data: dict) -> SpotifyTrack:
        """Parse a full Spotify track object (as returned by sp.track())."""
        artists = data.get("artists") or []
        primary_artist = artists[0]["name"] if artists else "Unknown Artist"
        additional = [a["name"] for a in artists[1:] if a.get("name")]

        album_data = data.get("album") or {}
        album_name = album_data.get("name", "Unknown Album")
        year = self._extract_year(album_data.get("release_date", ""))
        thumbnail_url = self._best_image(album_data.get("images", []))

        return SpotifyTrack(
            title=data.get("name", "Unknown Track"),
            artist=primary_artist,
            album=album_name,
            spotify_id=data.get("id", ""),
            year=year,
            track_number=data.get("track_number"),
            total_tracks=album_data.get("total_tracks"),
            duration_ms=data.get("duration_ms", 0),
            thumbnail_url=thumbnail_url,
            additional_artists=additional,
            search_query_exact=self._build_exact_query(data.get("name", ""), primary_artist, additional),
            search_query_loose=self._build_loose_query(data.get("name", ""), primary_artist),
        )

    def _parse_album_track(
        self,
        data: dict,
        *,
        album_name: str,
        year: Optional[str],
        thumbnail_url: Optional[str],
        genre: Optional[str],
        total_tracks: int,
    ) -> SpotifyTrack:
        """Parse a simplified track object from an album's track listing."""
        artists = data.get("artists") or []
        primary_artist = artists[0]["name"] if artists else "Unknown Artist"
        additional = [a["name"] for a in artists[1:] if a.get("name")]
        track_name = data.get("name", "Unknown Track")
        return SpotifyTrack(
            title=track_name,
            artist=primary_artist,
            album=album_name,
            spotify_id=data.get("id", ""),
            year=year,
            track_number=data.get("track_number"),
            total_tracks=total_tracks,
            duration_ms=data.get("duration_ms", 0),
            thumbnail_url=thumbnail_url,
            genre=genre,
            additional_artists=additional,
            search_query_exact=self._build_exact_query(track_name, primary_artist, additional),
            search_query_loose=self._build_loose_query(track_name, primary_artist),
        )

    # ------------------------------------------------------------------ #
    # Search query builders
    # ------------------------------------------------------------------ #

    # Parenthetical suffixes that YouTube Music typically appends to the video
    # title but are NOT part of the Spotify track name — keeping them in the
    # search query degrades result quality.
    _NOISE_PATTERN = re.compile(
        r"\s*[\(\[](?:official\s+(?:audio|video|music\s+video|lyric\s+video)"
        r"|audio|lyrics?|hd|4k|remaster(?:ed)?"
        r"|hq|feat(?:uring)?\.?\s+[^\)\]]+"
        r"|ft\.?\s+[^\)\]]+)\s*[\)\]]",
        re.IGNORECASE,
    )
    # Featured-artist patterns in various formats
    _FEAT_PATTERN = re.compile(
        r"\s+(?:feat(?:uring)?\.?|ft\.?|with|&)\s+.+$",
        re.IGNORECASE,
    )

    @classmethod
    def _clean_title(cls, title: str) -> str:
        """Strip noise suffixes from a track title before embedding in a query."""
        cleaned = cls._NOISE_PATTERN.sub("", title)
        cleaned = cls._FEAT_PATTERN.sub("", cleaned)
        return cleaned.strip()

    @classmethod
    def _build_exact_query(
        cls, title: str, artist: str, additional_artists: list[str]
    ) -> str:
        """
        Tight `ytsearch1:` query — quoted title + primary artist.

        Example: `ytsearch1: "Harder Better Faster Stronger" Daft Punk`

        Quoting the title prevents YouTube from token-splitting it, which
        is the single most impactful accuracy improvement for tracks with
        multi-word titles (which is most tracks). The artist is left
        unquoted so its word order doesn't matter.
        """
        clean = cls._clean_title(title)
        feat_suffix = ""
        if additional_artists:
            feat_suffix = f" ft {additional_artists[0]}"
        return f'ytsearch1: "{clean}" {artist}{feat_suffix}'

    @classmethod
    def _build_loose_query(cls, title: str, artist: str) -> str:
        """
        Broad `ytsearch3:` fallback — unquoted, fewer words.

        Used when the exact query's top result has a duration that's too
        far from the Spotify track (indicating a live version, cover, or
        misidentified result). Returning 3 results lets the engine pick
        the best duration match.
        """
        clean = cls._clean_title(title)
        # Strip punctuation from loose query since unquoted
        clean_loose = re.sub(r"[^\w\s]", " ", clean).strip()
        return f"ytsearch3: {artist} {clean_loose}"

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        """
        Extract (resource_type, resource_id) from any Spotify URL or URI.

        Handles:
          - `https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC`
          - `https://open.spotify.com/intl-de/album/...`
          - `spotify:playlist:37i9dQZF1DXcBWIGoYBM5M`
        """
        url = url.strip()

        # URI format: spotify:type:id
        uri_match = re.match(r"^spotify:(track|album|playlist|artist):([A-Za-z0-9]+)$", url)
        if uri_match:
            return uri_match.group(1), uri_match.group(2)

        # URL format
        url_match = re.search(
            r"open\.spotify\.com/(?:intl-[a-z]{2}/)?"
            r"(track|album|playlist|artist)/([A-Za-z0-9]+)",
            url,
            re.IGNORECASE,
        )
        if url_match:
            return url_match.group(1).lower(), url_match.group(2)

        raise SpotifyEngineError(
            f"Could not parse Spotify URL/URI: {url!r}. "
            "Expected a URL like https://open.spotify.com/track/... "
            "or a URI like spotify:track:..."
        )

    @staticmethod
    def _extract_year(release_date: str) -> Optional[str]:
        """Extract the 4-digit year from Spotify's release_date (YYYY, YYYY-MM, or YYYY-MM-DD)."""
        if release_date and len(release_date) >= 4:
            return release_date[:4]
        return None

    @staticmethod
    def _best_image(images: list[dict]) -> Optional[str]:
        """Return the URL of the highest-resolution image from a Spotify images list."""
        if not images:
            return None
        sorted_images = sorted(
            images,
            key=lambda img: (img.get("width") or 0) * (img.get("height") or 0),
            reverse=True,
        )
        return sorted_images[0].get("url")

    # ------------------------------------------------------------------ #
    # Preview table data (used by main.py before the download starts)
    # ------------------------------------------------------------------ #

    def build_preview_info(self, title: str, tracks: list[SpotifyTrack]) -> dict:
        """
        Return structured data for the pre-download preview table rendered
        in main.py. Kept here so the UI layer doesn't need to know about
        SpotifyTrack internals.
        """
        total_ms = sum(t.duration_ms for t in tracks)
        total_minutes = total_ms // 60_000
        artists = list(dict.fromkeys(t.artist for t in tracks))  # ordered unique

        return {
            "title": title,
            "total_tracks": len(tracks),
            "total_duration_min": total_minutes,
            "primary_artists": artists[:5],
            "has_more_artists": len(artists) > 5,
        }
