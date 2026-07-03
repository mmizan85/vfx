#!/usr/bin/env python3
"""
main.py

VFX (Video Flow X-Downloader) — interactive CLI entry point.

Now fully wired: startup checks → URL detection → Spotify preview →
preset selection → ThreadPoolExecutor download loop → FFmpeg post-
processing → live progress bars → summary panel.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import NoReturn, Optional

import questionary
from questionary import Style as QStyle
from rich.align import Align
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.config import ConfigError, ConfigManager
from core.presets import QUICK_PRESETS, MediaKind, Preset, build_manual_preset, get_preset_by_key
from ui.console import EMOJI, console
from utils.logger import get_logger
from utils.system_check import check_ffmpeg, install_hint
from utils.url_detector import MediaSource, SpotifyResourceType, detect, is_playlist_like

logger = get_logger()

__version__ = "1.0.0"

_QSTYLE = QStyle(
    [
        ("qmark", "fg:#00d7ff bold"),
        ("question", "bold"),
        ("answer", "fg:#00ff87 bold"),
        ("pointer", "fg:#00d7ff bold"),
        ("highlighted", "fg:#00d7ff bold"),
        ("selected", "fg:#00ff87"),
    ]
)


# ============================================================================
# Banner
# ============================================================================

def _print_banner() -> None:
    vfx_art = r"""
██╗   ██╗███████╗██╗  ██╗
██║   ██║██╔════╝╚██╗██╔╝
██║   ██║█████╗   ╚███╔╝ 
╚██╗ ██╔╝██╔══╝   ██╔██╗ 
 ╚████╔╝ ██║     ██╔╝ ██╗
  ╚═══╝  ╚═╝     ╚═╝  ╚═╝
"""
    banner_text = Text()
    banner_text.append(vfx_art, style="bold bright_cyan")
    banner_text.append("Video Flow X-Downloader\n", style="title")
    banner_text.append("\n📩🧾'Change yourself, and society will change.'", style="title")
    console.print()
    console.print(Align.center(Panel(Align.center(banner_text), border_style="brand", padding=(1, 5))))
    console.print(Align.center(f"[muted]v{__version__} · {EMOJI['rocket']} ready when you are[/muted]"))
    console.print()


# ============================================================================
# Startup checks
# ============================================================================

def _run_startup_checks(config_manager: ConfigManager) -> None:
    """FFmpeg self-check + optional yt-dlp auto-update."""
    try:
        config = config_manager.config
    except ConfigError as exc:
        console.print(Panel(f"{EMOJI['cross']} {exc}", title="Configuration Error", border_style="danger"))
        sys.exit(1)

    # FFmpeg detection
    with console.status(f"[muted]{EMOJI['wrench']} Checking for FFmpeg…[/muted]", spinner="dots"):
        ffmpeg_status = check_ffmpeg(explicit_path=config.ffmpeg_path)

    if not ffmpeg_status.found:
        console.print(
            Panel(
                f"{EMOJI['warning']} FFmpeg not found.\n\n[muted]{install_hint()}[/muted]\n\n"
                f"Or set an explicit path via [accent]vfx --config[/accent].",
                title=f"{EMOJI['wrench']} Self-Healing Check",
                border_style="warning",
            )
        )
        proceed = questionary.confirm(
            "Continue anyway? (Re-encoding and thumbnail embedding will fail)",
            default=False, style=_QSTYLE,
        ).ask()
        if not proceed:
            sys.exit(1)

    # Optional yt-dlp startup auto-update (AI Agent Directive feature)
    if config.auto_update_ytdlp:
        _auto_update_ytdlp()


def _auto_update_ytdlp() -> None:
    """Run `python -m yt_dlp -U` at startup if the user has enabled it."""
    import subprocess
    console.print(f"[muted]{EMOJI['refresh']} Auto-updating yt-dlp…[/muted]")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "-U"],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode == 0:
            last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "done"
            console.print(f"[success]{EMOJI['check']} yt-dlp: {last_line}[/success]")
        else:
            logger.warning("yt-dlp -U exited %d: %s", result.returncode, result.stderr[:200])
    except Exception as exc:
        logger.warning("yt-dlp auto-update failed: %s", exc)


# ============================================================================
# URL handling & Spotify preview
# ============================================================================

def _prompt_for_url() -> str:
    url = questionary.text(
        f"{EMOJI['link']} Paste a Spotify, YouTube, or other media URL:",
        style=_QSTYLE,
    ).ask()
    if url is None:
        _abort("No URL provided.")
    return url.strip()


def _abort(message: str) -> NoReturn:
    console.print(f"\n[muted]{EMOJI['cross']} {message} Exiting.[/muted]\n")
    sys.exit(0)


def _handle_url_detection(url: str, config_manager: ConfigManager) -> None:
    result = detect(url)

    if result.source == MediaSource.INVALID:
        console.print(
            Panel(
                f"{EMOJI['cross']} That doesn't look like a valid URL.\n"
                "VFX supports Spotify, YouTube, YouTube Music, and most yt-dlp-supported sites.",
                border_style="danger",
            )
        )
        sys.exit(1)

    if result.source == MediaSource.SPOTIFY:
        _handle_spotify_preflight(result, config_manager)
    else:
        source_label = {
            MediaSource.YOUTUBE: f"{EMOJI['youtube']} YouTube",
            MediaSource.YOUTUBE_MUSIC: f"{EMOJI['music']} YouTube Music",
            MediaSource.GENERIC: f"{EMOJI['link']} Generic (yt-dlp supported site)",
        }.get(result.source, "Unknown")
        console.print(f"\n{EMOJI['check']} Detected source: [accent]{source_label}[/accent]")
        if is_playlist_like(result):
            console.print(f"[muted]{EMOJI['sparkles']} This looks like a playlist.[/muted]")


def _handle_spotify_preflight(result, config_manager: ConfigManager) -> None:
    if not config_manager.is_spotify_configured():
        console.print(
            Panel(
                f"{EMOJI['warning']} This is a Spotify link but no API credentials are configured.\n\n"
                f"Run [accent]vfx --config[/accent] to add your Client ID and Secret from "
                f"[link]https://developer.spotify.com/dashboard[/link].",
                title=f"{EMOJI['spotify']} Spotify Credentials Required",
                border_style="warning",
            )
        )
        sys.exit(1)

    resource_type = result.spotify_type.value if result.spotify_type else "unknown"
    console.print(
        f"\n{EMOJI['check']} Detected source: [accent]{EMOJI['spotify']} Spotify ({resource_type})[/accent]"
    )


def _spotify_preview_table(info: dict) -> None:
    """Render a beautiful preview table before downloading a Spotify resource."""
    table = Table(title=f"{EMOJI['spotify']} Spotify Preview", show_lines=False, expand=False)
    table.add_column("Field", style="accent", no_wrap=True)
    table.add_column("Value", style="white")

    table.add_row("Title", info["title"])
    table.add_row("Total Tracks", str(info["total_tracks"]))
    table.add_row("Total Duration", f"~{info['total_duration_min']} min")
    artists_str = ", ".join(info["primary_artists"])
    if info["has_more_artists"]:
        artists_str += " …and more"
    table.add_row("Artists", artists_str)

    console.print()
    console.print(table)
    console.print()


# ============================================================================
# Interactive selection menu
# ============================================================================

def _render_preset_table() -> None:
    table = Table(title=f"{EMOJI['sparkles']} Quick Presets", show_lines=True)
    table.add_column("", width=3, justify="center")
    table.add_column("Preset", style="accent")
    table.add_column("Output", style="white")
    table.add_column("Details", style="muted")

    for preset in QUICK_PRESETS:
        output = preset.target_container.upper()
        if preset.kind == MediaKind.AUDIO:
            details = f"{preset.audio_bitrate_kbps}kbps"
        elif preset.max_height:
            details = f"≤{preset.max_height}p"
        else:
            details = "source resolution"
        table.add_row(preset.emoji, preset.label, output, details)
    console.print(table)


def _select_preset(config_manager: ConfigManager) -> Preset:
    """
    Return the chosen Preset.  If a default_preset_key is configured,
    returns it immediately without showing any menu.
    """
    default_key = config_manager.config.defaults.default_preset_key
    if default_key:
        preset = get_preset_by_key(default_key)
        if preset:
            console.print(
                f"\n[muted]{EMOJI['gear']} Using default preset:[/muted] "
                f"{preset.emoji} [accent]{preset.label}[/accent]\n"
            )
            return preset

    mode = questionary.select(
        "How would you like to download this?",
        choices=[
            f"{EMOJI['rocket']}  Quick Presets (recommended)",
            f"{EMOJI['gear']}  Manual Mode (step-by-step control)",
        ],
        style=_QSTYLE,
    ).ask()
    if mode is None:
        _abort("No mode selected.")

    if "Quick Presets" in mode:
        return _select_quick_preset()
    return _select_manual_preset()


def _select_quick_preset() -> Preset:
    _render_preset_table()
    choice = questionary.select(
        "Choose a preset:",
        choices=[questionary.Choice(p.menu_title, value=p.key) for p in QUICK_PRESETS],
        style=_QSTYLE,
    ).ask()
    if choice is None:
        _abort("No preset selected.")
    return next(p for p in QUICK_PRESETS if p.key == choice)


def _select_manual_preset() -> Preset:
    media_type = questionary.select(
        "Step 1/3 — Media type:",
        choices=[f"{EMOJI['video']}  Video", f"{EMOJI['headphones']}  Audio only"],
        style=_QSTYLE,
    ).ask()
    if media_type is None:
        _abort("No media type selected.")
    is_audio = "Audio" in media_type

    if is_audio:
        quality = questionary.select(
            "Step 2/3 — Audio quality:",
            choices=[
                "320 kbps =>> (Best Quality / Hi-Fi Audio)",
                "256 kbps =>> (High Quality / Balanced)",
                "192 kbps =>> (Medium / Standard Quality)",
                "128 kbps =>> (Small Size / Low Bandwidth)"
            ],
            style=_QSTYLE,
        ).ask()
        
        fmt = questionary.select(
            "Step 3/3 — Output format:",
            choices=[
                "MP3 =>> (Universal Compatibility - Works on All Devices)",
                "M4A =>> (High Efficiency - Best for Apple & Modern Android)",
                "FLAC =>> (Lossless Audio - Large File Size)",
                "OPUS =>> (Ultra Modern Codec - Best at Low Bitrates)"
            ],
            style=_QSTYLE,
        ).ask()
    else:
        quality = questionary.select(
            "Step 2/3 — Video quality:",
            choices=[
                "2160p =>> (4K - Ultra HD / Best for Big Screens)",
                "1440p =>> (2K - Quad HD / For High-end Displays)",
                "1080p =>>(Full HD - Clear & Popular Choice)",
                "720p  =>> (HD - Good Quality & Moderate Size)",
                "480p  =>> (SD - Standard Quality / Saves Data)",
                "Lowest available =>> (Saves Maximum Storage & Data)"
            ],
            style=_QSTYLE,
        ).ask()
        
        fmt = questionary.select(
            "Step 3/3 — Output container:",
            choices=[
                "MP4 =>> (Universal Format - Plays on All Devices / Mobiles)",
                "MKV =>> (Advanced Container - Supports Multi-Audio/Subtitles)",
                "WEBM =>> (High Compression - Optimized for Web / Browsers)",
                "3GP =>> (Legacy - Specially for Feature Phones / Button Mobiles)"
            ],
            style=_QSTYLE,
        ).ask()

    if quality is None or fmt is None:
        _abort("Selection incomplete.")
        
    
    clean_quality = quality.split(" ")[0]
    clean_fmt = fmt.split(" ")[0]
    
    
    if "Lowest" in quality:
        clean_quality = "Lowest available"
    elif "320" in quality:
        clean_quality = "320 kbps (best)"
    elif "256" in quality:
        clean_quality = "256 kbps"
    elif "192" in quality:
        clean_quality = "192 kbps"
    elif "128" in quality:
        clean_quality = "128 kbps (smallest)"
        
    if "FLAC" in fmt:
        clean_fmt = "FLAC (if source supports lossless)"
        
    kind = MediaKind.AUDIO if is_audio else MediaKind.VIDEO
    return build_manual_preset(kind, clean_quality, clean_fmt)


def _confirm_selection(url: str, preset: Preset) -> None:
    console.print()
    summary = Table(title=f"{EMOJI['check']} Confirm Selection", show_header=False, box=None)
    summary.add_column(style="muted")
    summary.add_column(style="white")
    summary.add_row("URL", url[:80] + ("…" if len(url) > 80 else ""))
    summary.add_row("Preset", f"{preset.emoji} {preset.label}")
    summary.add_row("Output", preset.target_container.upper())
    console.print(summary)
    console.print()

    confirmed = questionary.confirm("Start download?", default=True, style=_QSTYLE).ask()
    if not confirmed:
        _abort("Cancelled by user.")


# ============================================================================
# Download dispatch — Spotify and YouTube paths
# ============================================================================

def _dispatch_download(
    url: str,
    preset: Preset,
    config_manager: ConfigManager,
    session_start: float,
) -> None:
    """
    Build all required engine objects and run the download session.
    Handles both Spotify and YouTube/generic URLs.
    """
    from core.ffmpeg_processor import FFmpegProcessor
    from core.history_db import DownloadHistory
    from core.youtube_engine import YouTubeEngine
    from ui.progress_display import ProgressTracker
    from ui.summary_panel import render_summary
    from utils.path_manager import PathManager
    from utils.system_check import check_ffmpeg

    cfg = config_manager.config
    result = detect(url)

    # ---- Build shared engine objects ----
    ffmpeg_status = check_ffmpeg(explicit_path=cfg.ffmpeg_path)
    ffmpeg_proc = FFmpegProcessor(
        ffmpeg_path=ffmpeg_status.path or "ffmpeg",
        timeout_seconds=600,
    )

    history = DownloadHistory()
    session_id = history.new_session_id()

    base_path = Path(cfg.defaults.download_path)
    path_manager = PathManager(base_path)
    path_manager.ensure_base_exists()

    # ---- Spotify path ----
    if result.source == MediaSource.SPOTIFY:
        _run_spotify_session(
            url, preset, config_manager,
            ffmpeg_proc, history, session_id, path_manager,
        )
    else:
        # YouTube / generic yt-dlp path
        _run_youtube_session(
            url, preset, config_manager,
            ffmpeg_proc, history, session_id, path_manager,
        )

    # ---- Render summary ----
    summary = history.get_session_summary(session_id)
    elapsed = time.monotonic() - session_start
    render_summary(summary, elapsed)
    history.close()


def _run_youtube_session(
    url: str,
    preset: Preset,
    config_manager: ConfigManager,
    ffmpeg_proc,
    history,
    session_id: str,
    path_manager,
) -> None:
    """Run a YouTube / generic yt-dlp download session."""
    from core.youtube_engine import YouTubeEngine, YouTubeEngineError
    from ui.progress_display import ProgressTracker

    # Quick flat extraction to know how many tracks we're dealing with
    with console.status(f"[muted]{EMOJI['gear']} Fetching media info…[/muted]", spinner="dots"):
        try:
            import yt_dlp
            flat_params = {
                "extract_flat": True, "flat_playlist": True,
                "quiet": True, "no_warnings": True, "socket_timeout": 20,
            }
            cfg = config_manager.config
            if cfg.cookies_from_browser:
                flat_params["cookiesfrombrowser"] = (cfg.cookies_from_browser,)
            elif cfg.cookies_file_path:
                flat_params["cookiefile"] = cfg.cookies_file_path

            with yt_dlp.YoutubeDL(flat_params) as ydl:
                flat_info = ydl.extract_info(url, download=False)
        except Exception as exc:
            console.print(f"[danger]{EMOJI['cross']} Could not fetch media info: {exc}[/danger]")
            logger.error("Flat extraction failed for %s: %s", url, exc)
            return

    if flat_info is None:
        console.print(f"[danger]{EMOJI['cross']} No info returned for that URL.[/danger]")
        return

    entries = flat_info.get("entries") or [flat_info]
    total = len([e for e in entries if e])
    console.print(
        f"\n[accent]{EMOJI['music']} Found:[/accent] "
        f"[white]{flat_info.get('title', 'Media')}[/white] "
        f"[muted]({total} track{'s' if total != 1 else ''})[/muted]"
    )

    with ProgressTracker(total_tracks=total, session_label="Downloading") as tracker:
        engine = YouTubeEngine(
            config_manager=config_manager,
            ffmpeg_processor=ffmpeg_proc,
            history=history,
            progress_tracker=tracker,
            path_manager=path_manager,
        )
        engine.download(url, preset, session_id)


def _run_spotify_session(
    url: str,
    preset: Preset,
    config_manager: ConfigManager,
    ffmpeg_proc,
    history,
    session_id: str,
    path_manager,
) -> None:
    """
    Resolve Spotify metadata → build ytsearch queries → download via YouTube engine.
    Each track gets: Spotify metadata embedded (not YouTube's), and the Spotify
    track ID stored as source_id for accurate duplicate detection.
    """
    from core.ffmpeg_processor import TrackMetadata
    from core.spotify_engine import SpotifyEngine, SpotifyEngineError
    from core.youtube_engine import YouTubeEngine
    from ui.progress_display import ProgressTracker

    cfg = config_manager.config

    # ---- Resolve Spotify metadata ----
    with console.status(f"[muted]{EMOJI['spotify']} Fetching Spotify metadata…[/muted]", spinner="dots"):
        try:
            spotify = SpotifyEngine(cfg.spotify.client_id, cfg.spotify.client_secret)
            collection_title, tracks = spotify.resolve(url)
        except SpotifyEngineError as exc:
            console.print(
                Panel(
                    f"{EMOJI['cross']} {exc}",
                    title=f"{EMOJI['spotify']} Spotify Error",
                    border_style="danger",
                )
            )
            logger.error("Spotify resolve failed for %s: %s", url, exc)
            return

    # ---- Preview table ----
    preview = spotify.build_preview_info(collection_title, tracks)
    _spotify_preview_table(preview)

    confirmed = questionary.confirm(
        f"Download {preview['total_tracks']} tracks from \"{collection_title}\"?",
        default=True, style=_QSTYLE,
    ).ask()
    if not confirmed:
        _abort("Cancelled by user.")

    # ---- Download each track via YouTubeEngine ----
    with ProgressTracker(total_tracks=len(tracks), session_label=f"{EMOJI['spotify']} Spotify") as tracker:
        engine = YouTubeEngine(
            config_manager=config_manager,
            ffmpeg_processor=ffmpeg_proc,
            history=history,
            progress_tracker=tracker,
            path_manager=path_manager,
        )

        # Resolve output directory for the whole playlist/album
        if len(tracks) > 1:
            resource_id = detect(url).resource_id
            output_dir = path_manager.resolve_playlist_directory(collection_title, resource_id)
        else:
            path_manager.ensure_base_exists()
            output_dir = Path(cfg.defaults.download_path)

        from concurrent.futures import ThreadPoolExecutor, as_completed
        thread_count = min(cfg.defaults.thread_count, len(tracks))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = {}
            for track in tracks:
                meta = TrackMetadata(
                    title=track.title,
                    artist=track.display_artist,
                    album=track.album,
                    year=track.year,
                    track_number=track.track_number,
                )
                # Use the exact query first; the loose fallback is passed as
                # the URL and the engine will use it as a fallback if the
                # exact result's duration is too far off.
                future = executor.submit(
                    engine._download_single,
                    track.search_query_exact,
                    track.title,
                    track.spotify_id,
                    preset,
                    output_dir,
                    session_id,
                    meta,
                )
                futures[future] = track

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    t = futures[future]
                    logger.exception("Unexpected error for Spotify track %s: %s", t.title, exc)
                    history.record_failure(
                        t.search_query_exact, t.title, session_id,
                        f"Internal error: {exc}", source_id=t.spotify_id,
                    )
                    tracker.fail(None)


# ============================================================================
# Argument parsing & main
# ============================================================================

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vfx",
        description=f"{EMOJI['rocket']} VFX — download from Spotify, YouTube, and more.",
    )
    parser.add_argument(
        "url", nargs="?", default=None,
        help="Media URL (Spotify, YouTube, or any yt-dlp-supported site). "
             "Omit to be prompted interactively.",
    )
    parser.add_argument(
        "--config", action="store_true",
        help="Open the interactive configuration panel.",
    )
    parser.add_argument("--version", action="version", version=f"vfx {__version__}")
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    config_manager = ConfigManager()
    session_start = time.monotonic()

    _print_banner()

    if args.config:
        from ui.config_panel import ConfigPanel
        ConfigPanel(config_manager).run()
        return

    _run_startup_checks(config_manager)

    url = args.url.strip() if args.url else _prompt_for_url()
    _handle_url_detection(url, config_manager)

    preset = _select_preset(config_manager)
    _confirm_selection(url, preset)
    _dispatch_download(url, preset, config_manager, session_start)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(f"\n\n[muted]{EMOJI['cross']} Interrupted. Goodbye![/muted]\n")
        sys.exit(130)
    except Exception as exc:
        logger.exception("Unhandled exception in main(): %s", exc)
        console.print(
            f"\n[danger]{EMOJI['cross']} Something went wrong. "
            f"Check logs/vfx.log for details.[/danger]\n"
        )
        sys.exit(1)
