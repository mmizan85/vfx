"""
core/youtube_engine.py

The core download engine: wraps yt-dlp for every non-Spotify URL (and
for Spotify tracks once the SpotifyEngine has resolved them to a YouTube
search URL), and coordinates the full post-processing pipeline (FFmpeg
transcode + metadata embedding).

Design decisions worth documenting
------------------------------------

1. `extract_info()` not `download()`: Using `ydl.extract_info(url, download=True)`
   returns the info dict post-download, which is the only reliable way to find
   the actual output file path (via `requested_downloads[0]['filepath']`). The
   `ydl.download([url])` API returns a status int and nothing else.

2. Flat extraction → per-entry dispatch: For playlists we do a cheap
   `extract_flat=True` first pass to enumerate entries (no downloading,
   fast), then pass each entry URL individually to the ThreadPoolExecutor.
   This allows per-track duplicate checks before a download starts, and
   gives each track its own progress bar.

3. Merge-then-transcode pipeline: yt-dlp's internal FFmpeg call handles
   stream merging (DASH video + audio → mkv); our FFmpegProcessor handles
   any user-visible re-encoding (resolution cap, codec change, Feature Phone
   exact dimensions). Never re-encoding for Original Quality means the
   internal merge is the only FFmpeg pass that happens for that preset.

4. Self-healing: Catches `ExtractorError` (cipher/signature issues, site
   changes) and triggers a `yt_dlp -U` auto-update once per session, then
   retries the failed URL. The per-URL retry (via tenacity) handles transient
   `DownloadError` (network flaps, 429s) separately.

5. AI Agent Directive features:
   - Synced lyrics: passes `writeautomaticsub + subtitlesformat=lrc` to
     yt-dlp for audio downloads when enabled, producing `.lrc` files
     alongside tracks from YouTube Music / YouTube.
   - Cookie-based auth: injects `cookiesfrombrowser` or `cookiefile` into
     every download for age-restricted and private content.
"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yt_dlp
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import ConfigManager
from core.ffmpeg_processor import FFmpegProcessor, FFmpegProcessorError, TrackMetadata
from core.history_db import DownloadHistory, DownloadStatus
from core.presets import MediaKind, Preset
from ui.console import EMOJI, console
from ui.progress_display import ProgressTracker
from utils.logger import get_logger
from utils.path_manager import PathManager

logger = get_logger()


class YouTubeEngineError(Exception):
    """Raised for unrecoverable engine-level failures (not per-track failures)."""


@dataclass
class DownloadResult:
    """Outcome of one track-download attempt."""

    url: str
    title: str
    status: DownloadStatus
    output_path: Optional[Path] = None
    error_message: Optional[str] = None
    file_size_bytes: int = 0
    elapsed_seconds: float = 0.0
    preset_key: str = ""
    artist: Optional[str] = None
    source_id: Optional[str] = None


class YouTubeEngine:
    """
    Downloads media from YouTube and any other yt-dlp-supported site.

    :param config_manager: Provides live config (thread count, cookies,
        rate limit, feature toggles, etc.). Read at download time so
        the user's most recent `--config` edits are always respected.
    :param ffmpeg_processor: Pre-constructed FFmpegProcessor pointing at the
        verified FFmpeg binary from the startup self-check.
    :param history: Shared DownloadHistory instance (thread-safe internally).
    :param progress_tracker: Active ProgressTracker context (must already
        be entered via `with tracker:`).
    :param path_manager: PathManager for the current download root.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        ffmpeg_processor: FFmpegProcessor,
        history: DownloadHistory,
        progress_tracker: ProgressTracker,
        path_manager: PathManager,
    ) -> None:
        self.config_manager = config_manager
        self.ffmpeg = ffmpeg_processor
        self.history = history
        self.tracker = progress_tracker
        self.path_manager = path_manager
        self._self_healed_this_session = False  # Only auto-update yt-dlp once per run

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def download(
        self,
        url: str,
        preset: Preset,
        session_id: str,
        *,
        metadata_override: Optional[TrackMetadata] = None,
    ) -> list[DownloadResult]:
        """
        Download `url` (single track or playlist) using `preset`.

        :param url: YouTube/generic URL, or a `ytsearch1:` query string
            produced by SpotifyEngine.
        :param preset: The resolved Preset (Quick Preset or Manual Mode).
        :param session_id: Current session identifier for history recording.
        :param metadata_override: Pre-fetched TrackMetadata (from Spotify)
            to embed instead of what yt-dlp extracts from YouTube.
        :returns: One DownloadResult per track attempted.
        """
        cfg = self.config_manager.config

        try:
            playlist_title, entries = self._extract_entries(url)
        except YouTubeEngineError as exc:
            logger.error("Entry extraction failed for %s: %s", url, exc)
            console.print(f"[danger]{EMOJI['cross']} Could not read URL: {exc}[/danger]")
            return []

        # Resolve output directory: playlist gets its own sub-folder,
        # single tracks go straight into the base download root.
        base = Path(cfg.defaults.download_path)
        is_playlist = len(entries) > 1
        if is_playlist and playlist_title:
            playlist_id = self._extract_playlist_id(url)
            output_dir = self.path_manager.resolve_playlist_directory(playlist_title, playlist_id)
            console.print(
                f"\n[accent]{EMOJI['folder']} Playlist:[/accent] {playlist_title} "
                f"[muted]({len(entries)} tracks → {output_dir})[/muted]\n"
            )
        else:
            self.path_manager.ensure_base_exists()
            output_dir = base

        thread_count = min(cfg.defaults.thread_count, len(entries))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = {
                executor.submit(
                    self._download_single,
                    entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id', '')}",
                    entry.get("title") or entry.get("id") or "Unknown Track",
                    entry.get("id"),
                    preset,
                    output_dir,
                    session_id,
                    metadata_override,
                ): entry
                for entry in entries
            }
            results: list[DownloadResult] = []
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    entry = futures[future]
                    title = entry.get("title", "Unknown")
                    logger.exception("Worker thread raised unexpectedly for %s: %s", title, exc)
                    result = DownloadResult(
                        url=entry.get("url", ""),
                        title=title,
                        status=DownloadStatus.FAILED,
                        error_message=f"Internal error: {exc}",
                        preset_key=preset.key,
                    )
                results.append(result)

        return results

    # ------------------------------------------------------------------ #
    # Playlist / single-entry extraction
    # ------------------------------------------------------------------ #

    def _extract_entries(self, url: str) -> tuple[Optional[str], list[dict]]:
        """
        Do a cheap flat extraction to get the entry list.
        Returns (playlist_title_or_None, list_of_entry_dicts).
        """
        flat_params = {
            "extract_flat": True,
            "flat_playlist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
        }
        if self.config_manager.config.cookies_from_browser:
            flat_params["cookiesfrombrowser"] = (
                self.config_manager.config.cookies_from_browser,
            )
        elif self.config_manager.config.cookies_file_path:
            flat_params["cookiefile"] = self.config_manager.config.cookies_file_path

        try:
            with yt_dlp.YoutubeDL(flat_params) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise YouTubeEngineError(str(exc)) from exc

        if info is None:
            raise YouTubeEngineError("yt-dlp returned no info for the given URL.")

        if "entries" in info:
            # Playlist / channel
            entries = [e for e in (info.get("entries") or []) if e]
            return info.get("title"), entries
        else:
            # Single track — wrap in a list so the rest of the pipeline
            # is uniform regardless of input type.
            return None, [
                {
                    "url": info.get("webpage_url") or url,
                    "id": info.get("id"),
                    "title": info.get("title"),
                }
            ]

    @staticmethod
    def _extract_playlist_id(url: str) -> Optional[str]:
        """Pull the playlist/list query parameter from a YouTube URL."""
        import urllib.parse
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return params.get("list", [None])[0]

    # ------------------------------------------------------------------ #
    # Single-track download (runs inside a worker thread)
    # ------------------------------------------------------------------ #

    def _download_single(
        self,
        url: str,
        title: str,
        source_id: Optional[str],
        preset: Preset,
        output_dir: Path,
        session_id: str,
        metadata_override: Optional[TrackMetadata],
    ) -> DownloadResult:
        """
        Download one track, post-process it, and record the outcome.
        Never raises — all exceptions are caught and returned as a FAILED
        DownloadResult so they don't crash the ThreadPoolExecutor.
        """
        cfg = self.config_manager.config
        task_id = self.tracker.add_task(title)
        start = time.monotonic()

        # ---- Duplicate check (history DB first, then filesystem) ----
        if cfg.defaults.skip_existing_files and source_id:
            prior = self.history.was_downloaded(source_id)
            if prior and prior.file_path and Path(prior.file_path).exists():
                elapsed = time.monotonic() - start
                self.tracker.complete(task_id, skipped=True)
                self.history.record_skip(url, title, session_id, source_id=source_id, file_path=prior.file_path)
                console.print(f"[muted]{EMOJI['refresh']} Skipping already-downloaded: {title}[/muted]")
                return DownloadResult(
                    url=url, title=title, status=DownloadStatus.SKIPPED,
                    output_path=Path(prior.file_path), preset_key=preset.key,
                    source_id=source_id, elapsed_seconds=time.monotonic() - start,
                )

        # ---- Attempt download with retry ----
        try:
            info_dict = self._download_with_retry(url, preset, output_dir, task_id)
        except yt_dlp.utils.ExtractorError as exc:
            # Site-level extraction failure — try auto-healing once
            healed = self._try_self_heal(exc)
            if healed:
                try:
                    info_dict = self._download_with_retry(url, preset, output_dir, task_id)
                except Exception as retry_exc:
                    return self._fail(url, title, source_id, preset, session_id,
                                      task_id, start, f"Extraction failed after update: {retry_exc}")
            else:
                return self._fail(url, title, source_id, preset, session_id,
                                  task_id, start, f"Extraction error: {exc}")
        except yt_dlp.utils.DownloadError as exc:
            return self._fail(url, title, source_id, preset, session_id,
                              task_id, start, f"Download error: {exc}")
        except Exception as exc:  # noqa: BLE001
            return self._fail(url, title, source_id, preset, session_id,
                              task_id, start, f"Unexpected error: {exc}")

        # ---- Locate the downloaded file ----
        output_file = self._find_output_file(info_dict, output_dir)
        if output_file is None or not output_file.exists():
            return self._fail(url, title, source_id, preset, session_id,
                              task_id, start, "Downloaded file could not be located.")

        # ---- FFmpeg post-processing (transcode if needed) ----
        try:
            output_file = self._postprocess(output_file, preset, info_dict, metadata_override)
        except FFmpegProcessorError as exc:
            # Post-processing failure doesn't void the download — the raw
            # file still exists and is usable. Log the warning and continue.
            logger.warning("Post-processing failed for %s: %s", title, exc)
            console.print(f"[warning]{EMOJI['warning']} Post-processing issue for {title}: {exc}[/warning]")

        # ---- Record success ----
        file_size = output_file.stat().st_size if output_file.exists() else 0
        elapsed = time.monotonic() - start
        actual_title = info_dict.get("title") or title
        self.tracker.complete(task_id)
        self.history.record_success(
            url, actual_title, session_id,
            source_id=source_id,
            artist=info_dict.get("uploader") or info_dict.get("artist"),
            file_path=str(output_file),
            file_size_bytes=file_size,
            preset_key=preset.key,
            duration_seconds=elapsed,
        )
        return DownloadResult(
            url=url, title=actual_title, status=DownloadStatus.SUCCESS,
            output_path=output_file, file_size_bytes=file_size,
            elapsed_seconds=elapsed, preset_key=preset.key,
            artist=info_dict.get("uploader") or info_dict.get("artist"),
            source_id=source_id,
        )

    def _fail(
        self,
        url: str,
        title: str,
        source_id: Optional[str],
        preset: Preset,
        session_id: str,
        task_id,
        start: float,
        message: str,
    ) -> DownloadResult:
        """Record and return a failed-download result."""
        logger.error("Download failed for %s (%s): %s", title, url, message)
        self.tracker.fail(task_id)
        self.history.record_failure(url, title, session_id, message,
                                    source_id=source_id, preset_key=preset.key)
        return DownloadResult(
            url=url, title=title, status=DownloadStatus.FAILED,
            error_message=message, elapsed_seconds=time.monotonic() - start,
            preset_key=preset.key, source_id=source_id,
        )

    # ------------------------------------------------------------------ #
    # yt-dlp invocation (with tenacity retry for transient network errors)
    # ------------------------------------------------------------------ #

    def _download_with_retry(
        self,
        url: str,
        preset: Preset,
        output_dir: Path,
        task_id,
    ) -> dict:
        """
        Invoke yt-dlp with the correct params for `preset` and return the
        info dict. Tenacity retries on transient DownloadErrors (network
        timeouts, 429s, temporary server errors) with exponential back-off,
        but NOT on ExtractorErrors (those represent site-level issues that
        a bare retry won't fix — they go to the self-healing path instead).
        """

        @retry(
            retry=retry_if_exception_type(yt_dlp.utils.DownloadError),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1.5, min=2, max=30),
            reraise=True,
        )
        def _attempt() -> dict:
            hook = self._make_progress_hook(task_id)
            params = self._build_ytdlp_params(preset, output_dir, hook)
            with yt_dlp.YoutubeDL(params) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise yt_dlp.utils.DownloadError("yt-dlp returned no info dict")
                return info

        return _attempt()

    def _build_ytdlp_params(
        self, preset: Preset, output_dir: Path, progress_hook
    ) -> dict:
        """
        Translate a Preset + current config into a complete yt-dlp options dict.

        Key decisions:
        - `outtmpl` is a plain string (not the newer dict form) — both are
          supported by this yt-dlp version and the string form is simpler.
        - `merge_output_format` is set to 'mkv' for all video presets so
          yt-dlp always has a safe container for DASH stream merging,
          regardless of the source codec pair. We then transcode to the
          target container ourselves if needed.
        - Rate limit is converted from KB/s (our config unit) to bytes/s
          (yt-dlp's expected unit).
        """
        cfg = self.config_manager.config

        params: dict = {
            "format": preset.format_selector,
            "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [progress_hook],
            "retries": 6,
            "fragment_retries": 6,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "ignoreerrors": False,
        }

        # Merge format for video (audio is single-stream, no merge needed)
        if preset.kind == MediaKind.VIDEO:
            params["merge_output_format"] = "mkv"

        # Thumbnail — written to disk so we can embed it via mutagen afterward
        if cfg.defaults.embed_thumbnail:
            params["writethumbnail"] = True
            params["postprocessors"] = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}]

        # Subtitles
        if cfg.defaults.write_subtitles:
            params["writesubtitles"] = True
            params["subtitleslangs"] = ["en", "en-US"]

        # AI Agent Directive feature #1 — Synced lyrics
        # yt-dlp can fetch YouTube's auto-generated captions in LRC format,
        # which are timestamped line-by-line lyrics playable in most music
        # players that support the .lrc format (foobar2000, Poweramp, etc.).
        if cfg.defaults.fetch_synced_lyrics and preset.kind == MediaKind.AUDIO:
            params["writeautomaticsub"] = True
            params["subtitlesformat"] = "lrc"
            params.setdefault("subtitleslangs", ["en", "en-US"])

        # Rate limiting: config stores KB/s, yt-dlp expects bytes/s
        if cfg.defaults.rate_limit_kbps:
            params["ratelimit"] = cfg.defaults.rate_limit_kbps * 1024

        # AI Agent Directive feature #2 — Cookie-based auth for age-restricted /
        # private content. Injecting cookies transparently means users can
        # download any content they could watch in their own browser, without
        # VFX needing to know or store their credentials.
        if cfg.cookies_from_browser:
            # Tuple format: (browser_name, profile, keyring, container)
            # Minimum: just the browser name — yt-dlp uses the default profile.
            params["cookiesfrombrowser"] = (cfg.cookies_from_browser,)
        elif cfg.cookies_file_path:
            params["cookiefile"] = cfg.cookies_file_path

        # Custom FFmpeg location (must match the binary VFX already verified)
        if cfg.ffmpeg_path:
            params["ffmpeg_location"] = cfg.ffmpeg_path

        return params

    def _make_progress_hook(self, task_id) -> callable:
        """
        Return a closure that feeds yt-dlp's progress callbacks into the
        ProgressTracker. Each track gets its own hook (with its own task_id
        captured in the closure) so concurrent workers update independent bars.
        """
        tracker = self.tracker

        def hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                speed = d.get("speed")
                tracker.update(task_id, downloaded=downloaded, total=total, speed=speed)
            elif status == "finished":
                # 'finished' fires once the raw fragment/file is written but
                # before any merging or postprocessing — we call complete()
                # later after our own postprocessing pipeline is done, so
                # just update to full here without hiding the bar yet.
                downloaded = d.get("total_bytes") or d.get("downloaded_bytes") or 0
                tracker.update(task_id, downloaded=downloaded, total=downloaded, speed=None)

        return hook

    # ------------------------------------------------------------------ #
    # File location helpers
    # ------------------------------------------------------------------ #

    def _find_output_file(self, info_dict: dict, output_dir: Path) -> Optional[Path]:
        """
        Locate the final output file from yt-dlp's info dict.

        Tries three resolution paths in order of reliability:
        1. `requested_downloads[0]['filepath']` — the authoritative path set
           by yt-dlp after merging / postprocessing.
        2. `filepath` on the top-level info dict — set for single-stream
           downloads that never go through the merge step.
        3. Glob search in output_dir using the title — last resort for
           edge-case extractors that don't populate either field.
        """
        # Path 1: post-merge info
        requested = info_dict.get("requested_downloads") or []
        if requested:
            fp = requested[0].get("filepath")
            if fp and Path(fp).exists():
                return Path(fp)

        # Path 2: top-level filepath
        fp = info_dict.get("filepath")
        if fp and Path(fp).exists():
            return Path(fp)

        # Path 3: title-based glob (handles rare extractor edge cases)
        title = info_dict.get("title", "")
        if title:
            from utils.path_manager import sanitize_filename
            safe = sanitize_filename(title, max_length=100)
            for candidate in output_dir.glob(f"{safe}.*"):
                if self.path_manager.is_already_downloaded(candidate, min_valid_size_bytes=1024):
                    logger.debug("Found output via glob for %s: %s", title, candidate)
                    return candidate

        logger.warning("Could not locate output file for '%s' in %s", info_dict.get("title"), output_dir)
        return None

    def _find_thumbnail(self, media_path: Path) -> Optional[Path]:
        """
        Look for a thumbnail file that yt-dlp wrote adjacent to `media_path`.

        yt-dlp writes thumbnails as `<stem>.jpg` (after our FFmpegThumbnailsConvertor
        postprocessor forces jpg format). Falls back to `.webp` in case the
        conversion postprocessor wasn't applied.
        """
        for ext in (".jpg", ".webp", ".png"):
            candidate = media_path.with_suffix(ext)
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # Post-processing pipeline
    # ------------------------------------------------------------------ #

    def _postprocess(
        self,
        input_path: Path,
        preset: Preset,
        info_dict: dict,
        metadata_override: Optional[TrackMetadata],
    ) -> Path:
        """
        Run the FFmpeg transcode (if needed) and embed metadata + art.

        Returns the final output path (may differ from input if transcoded).
        """
        cfg = self.config_manager.config
        probe = self.ffmpeg.probe(input_path)

        # ---- Transcode (conditional) ----
        if self.ffmpeg.needs_transcode(probe, preset):
            target_ext = f".{preset.target_container}"
            output_path = input_path.with_suffix(target_ext)
            if output_path == input_path:
                # e.g. source is already .mp3 but needs bitrate change —
                # write to a temp name, then replace the original.
                output_path = input_path.with_suffix(f".vfx_tmp{target_ext}")
            self.ffmpeg.transcode(input_path, output_path, preset, source_probe=probe)
            if output_path != input_path and input_path.exists():
                input_path.unlink(missing_ok=True)
            input_path = output_path

        # ---- Metadata embedding ----
        if cfg.defaults.embed_metadata or cfg.defaults.embed_thumbnail:
            meta = metadata_override or self._extract_metadata_from_info(info_dict)
            thumbnail = self._find_thumbnail(input_path) if cfg.defaults.embed_thumbnail else None
            self.ffmpeg.embed_metadata(
                input_path, meta,
                thumbnail_path=thumbnail if cfg.defaults.embed_thumbnail else None,
            )
            # Clean up the standalone thumbnail file after embedding so it
            # doesn't litter the download folder.
            if thumbnail and thumbnail.exists():
                thumbnail.unlink(missing_ok=True)

        return input_path

    @staticmethod
    def _extract_metadata_from_info(info_dict: dict) -> TrackMetadata:
        """Build a TrackMetadata from yt-dlp's info dict fields."""
        upload_date = info_dict.get("upload_date") or ""
        year = upload_date[:4] if len(upload_date) >= 4 else info_dict.get("release_year")
        return TrackMetadata(
            title=info_dict.get("title") or "Unknown",
            artist=info_dict.get("artist") or info_dict.get("uploader") or info_dict.get("channel"),
            album=info_dict.get("album") or info_dict.get("playlist_title"),
            year=str(year) if year else None,
            genre=info_dict.get("genre"),
        )

    # ------------------------------------------------------------------ #
    # Self-healing: auto-update yt-dlp on ExtractorError
    # ------------------------------------------------------------------ #

    def _try_self_heal(self, exc: yt_dlp.utils.ExtractorError) -> bool:
        """
        Attempt to auto-update yt-dlp when an ExtractorError suggests the
        extractor is stale (e.g. YouTube cipher/signature changes).

        Only runs once per VFX session to avoid spamming package updates.
        Returns True if the update succeeded (caller should retry the download).
        """
        if self._self_healed_this_session:
            return False

        self._self_healed_this_session = True
        error_lower = str(exc).lower()
        # Only trigger for errors that are genuinely fixable by updating the
        # extractor — not for things like geo-blocks or deleted videos.
        healable_signals = {"sign", "cipher", "js", "nsig", "player", "format"}
        if not any(sig in error_lower for sig in healable_signals):
            return False

        console.print(
            f"\n[warning]{EMOJI['wrench']} Extraction error detected — "
            f"attempting yt-dlp self-update...[/warning]"
        )
        logger.info("Triggering yt-dlp self-update due to ExtractorError: %s", exc)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "yt_dlp", "-U"],
                capture_output=True, text=True, timeout=120, check=False,
            )
            if result.returncode == 0:
                console.print(f"[success]{EMOJI['check']} yt-dlp updated — retrying download.[/success]\n")
                logger.info("yt-dlp self-update succeeded: %s", result.stdout.strip()[:200])
                return True
            else:
                logger.warning("yt-dlp self-update failed (exit %d): %s", result.returncode, result.stderr[:300])
                return False
        except (subprocess.TimeoutExpired, OSError) as update_exc:
            logger.warning("yt-dlp self-update process error: %s", update_exc)
            return False
