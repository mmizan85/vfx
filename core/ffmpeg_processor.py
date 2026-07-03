"""
core/ffmpeg_processor.py

All FFmpeg post-processing lives here: deciding whether a downloaded file
already satisfies a preset's requirements (and skipping re-encoding when
it does), the actual transcode when it doesn't, and embedding ID3/MP4
metadata + thumbnail artwork into the final file.

Design notes
------------
- We shell out to the `ffmpeg`/`ffprobe` binaries directly via subprocess
  rather than the `ffmpeg-python` wrapper. Our needs are a fixed set of
  single-input/single-output operations per preset, not arbitrary filter
  graphs — direct subprocess calls are easier to log (we can record the
  *exact* command that failed on track 247 of a 500-track playlist) and
  easier to unit-test than a wrapper's lazily-built command graph.

- Metadata/thumbnail embedding is done with `mutagen`, not FFmpeg's own
  `-metadata` flags. FFmpeg's tag-writing behavior is inconsistent across
  containers/codecs (MP3 ID3v2 versioning, MP4 atom naming, embedded
  cover art conventions all differ), whereas mutagen has purpose-built,
  well-tested support for each format's actual tagging convention.

- Re-encoding is conditional, not automatic. Forcing every download
  through a lossy re-encode would be slow and degrade quality for files
  that already satisfy a preset (e.g. a Computer/TV download that's
  already 1080p H.264 MP4 needs no transcode at all). The Feature Phone
  preset is the one deliberate exception: its target hardware is narrow
  enough that we always force an exact-dimension re-encode regardless of
  source, per the spec's "STRICT REQUIREMENT" framing.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from core.presets import MediaKind, Preset
from utils.logger import get_logger

logger = get_logger()


class FFmpegProcessorError(Exception):
    """Raised when an FFmpeg/FFprobe invocation fails or returns unusable output."""


# ============================================================================
# Probing
# ============================================================================


@dataclass
class MediaProbeResult:
    """Structured subset of an `ffprobe` report, covering what VFX's
    transcode decisions actually need."""

    duration_seconds: Optional[float]
    format_name: Optional[str]
    video_codec: Optional[str]
    audio_codec: Optional[str]
    width: Optional[int]
    height: Optional[int]
    video_bitrate_kbps: Optional[int]
    audio_bitrate_kbps: Optional[int]

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


# ============================================================================
# Metadata contract
# ============================================================================


@dataclass
class TrackMetadata:
    """Tag fields written into the final file. All fields are optional
    except `title` — partial metadata (e.g. no album art available) is
    common and should degrade gracefully rather than fail the embed."""

    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    year: Optional[str] = None
    track_number: Optional[int] = None
    genre: Optional[str] = None


class EmbedStatus(str, Enum):
    """Outcome of an embed_metadata() call, for the caller's logging/summary."""

    EMBEDDED = "embedded"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FAILED = "failed"


# ============================================================================
# FFmpegProcessor
# ============================================================================


class FFmpegProcessor:
    """
    Owns every FFmpeg/FFprobe invocation and all metadata-embedding logic
    for a download session.

    :param ffmpeg_path: Path to the ffmpeg binary (resolved by
        utils.system_check.check_ffmpeg before this class is constructed).
    :param ffprobe_path: Path to ffprobe. Defaults to swapping "ffmpeg"
        for "ffprobe" in `ffmpeg_path`, which holds for every standard
        FFmpeg distribution (the two binaries always ship side-by-side).
    :param timeout_seconds: Hard ceiling per subprocess call, so a hung
        encode (corrupt input, broken pipe) can never freeze the whole
        download session — see the "crash-proof" mandate.
    """

    def __init__(
        self,
        ffmpeg_path: str,
        ffprobe_path: Optional[str] = None,
        timeout_seconds: int = 600,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path or ffmpeg_path.replace("ffmpeg", "ffprobe")
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ #
    # Probing
    # ------------------------------------------------------------------ #

    def probe(self, file_path: Path) -> MediaProbeResult:
        """
        Inspect a media file's actual streams via ffprobe.

        :raises FFmpegProcessorError: if ffprobe fails, times out, or
            returns output that can't be parsed as the expected JSON shape.
        """
        command = [
            self.ffprobe_path,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(file_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("ffprobe failed for %s: %s", file_path, exc)
            raise FFmpegProcessorError(f"Could not probe {file_path.name}: {exc}") from exc

        if result.returncode != 0:
            logger.error("ffprobe exited %d for %s: %s", result.returncode, file_path, result.stderr[:500])
            raise FFmpegProcessorError(f"ffprobe could not read {file_path.name} (corrupt or unsupported file)")

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise FFmpegProcessorError(f"ffprobe returned unparseable output for {file_path.name}") from exc

        return self._parse_probe_json(data)

    @staticmethod
    def _parse_probe_json(data: dict) -> MediaProbeResult:
        """Extract the fields VFX cares about, tolerating any missing keys."""
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        def _safe_int(value) -> Optional[int]:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _safe_float(value) -> Optional[float]:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        video_bitrate_kbps = None
        if video_stream and video_stream.get("bit_rate"):
            video_bitrate_kbps = _safe_int(video_stream["bit_rate"])
            video_bitrate_kbps = video_bitrate_kbps // 1000 if video_bitrate_kbps else None
        elif video_stream and not audio_stream and fmt.get("bit_rate"):
            # Video-only file with no per-stream bitrate reported (some
            # containers omit it) — the format-level total is then a
            # reasonable proxy since there's no other stream to conflate it with.
            video_bitrate_kbps = _safe_int(fmt.get("bit_rate"))
            video_bitrate_kbps = video_bitrate_kbps // 1000 if video_bitrate_kbps else None

        audio_bitrate_kbps = None
        if audio_stream and audio_stream.get("bit_rate"):
            audio_bitrate_kbps = _safe_int(audio_stream["bit_rate"])
            audio_bitrate_kbps = audio_bitrate_kbps // 1000 if audio_bitrate_kbps else None
        elif audio_stream and not video_stream and fmt.get("bit_rate"):
            # Audio-only file with no per-stream bitrate — this is the common
            # case for webm/opus, which is how yt-dlp's bestaudio format
            # almost always arrives. Without this fallback, the "don't
            # fake-upscale a low-bitrate source" protection in
            # _build_audio_command would silently never trigger for the
            # single most common real-world input.
            audio_bitrate_kbps = _safe_int(fmt.get("bit_rate"))
            audio_bitrate_kbps = audio_bitrate_kbps // 1000 if audio_bitrate_kbps else None

        return MediaProbeResult(
            duration_seconds=_safe_float(fmt.get("duration")),
            format_name=fmt.get("format_name"),
            video_codec=video_stream.get("codec_name") if video_stream else None,
            audio_codec=audio_stream.get("codec_name") if audio_stream else None,
            width=_safe_int(video_stream.get("width")) if video_stream else None,
            height=_safe_int(video_stream.get("height")) if video_stream else None,
            video_bitrate_kbps=video_bitrate_kbps,
            audio_bitrate_kbps=audio_bitrate_kbps,
        )

    # ------------------------------------------------------------------ #
    # Transcode decision
    # ------------------------------------------------------------------ #

    # Codecs considered broadly compatible enough that we don't force a
    # re-encode purely to "normalize" them — only resolution/container
    # mismatches trigger a transcode for these presets.
    _ACCEPTABLE_VIDEO_CODECS = frozenset({"h264", "hevc"})

    def needs_transcode(self, probe: MediaProbeResult, preset: Preset) -> bool:
        """
        Decide whether `probe`'s file already satisfies `preset`, or
        needs an FFmpeg pass.

        Deliberately conservative: re-encoding is slow and lossy, so we
        only do it when the source genuinely doesn't meet the preset's
        requirements — except Feature Phone, whose hardware constraints
        are strict enough that we always force the exact target.
        """
        if preset.key == "original_quality":
            return False

        if preset.key == "feature_phone":
            return True

        if preset.kind == MediaKind.VIDEO:
            if probe.height is None or probe.width is None:
                return True  # Unknown dimensions — safer to re-encode than guess.
            if preset.max_height is not None and probe.height > preset.max_height:
                return True
            if probe.video_codec not in self._ACCEPTABLE_VIDEO_CODECS:
                return True
            if probe.format_name and preset.target_container not in probe.format_name:
                return True
            return False

        # Audio presets
        if probe.format_name and preset.target_container not in probe.format_name:
            return True
        return False

    # ------------------------------------------------------------------ #
    # Transcoding
    # ------------------------------------------------------------------ #

    def transcode(
        self,
        input_path: Path,
        output_path: Path,
        preset: Preset,
        source_probe: Optional[MediaProbeResult] = None,
    ) -> Path:
        """
        Re-encode `input_path` to satisfy `preset`, writing to `output_path`.

        :param source_probe: Pass a previously-computed probe to avoid a
            redundant ffprobe call; if omitted, the input is probed here.
        :raises FFmpegProcessorError: if the ffmpeg invocation fails.
        """
        probe = source_probe or self.probe(input_path)
        command = self._build_ffmpeg_command(input_path, output_path, preset, probe)

        logger.info("Transcoding %s -> %s via: %s", input_path.name, output_path.name, " ".join(command))

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("FFmpeg transcode timed out for %s after %ds", input_path.name, self.timeout_seconds)
            raise FFmpegProcessorError(
                f"Transcoding {input_path.name} took longer than {self.timeout_seconds}s and was aborted."
            ) from exc
        except OSError as exc:
            logger.error("FFmpeg invocation failed for %s: %s", input_path.name, exc)
            raise FFmpegProcessorError(f"Could not run FFmpeg on {input_path.name}: {exc}") from exc

        if result.returncode != 0:
            logger.error("FFmpeg exited %d for %s: %s", result.returncode, input_path.name, result.stderr[-1000:])
            raise FFmpegProcessorError(
                f"FFmpeg failed to transcode {input_path.name} (exit code {result.returncode})"
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise FFmpegProcessorError(f"FFmpeg reported success but produced no output for {input_path.name}")

        return output_path

    def _build_ffmpeg_command(
        self, input_path: Path, output_path: Path, preset: Preset, probe: MediaProbeResult
    ) -> list[str]:
        """Dispatch to the video or audio command builder based on preset kind."""
        if preset.kind == MediaKind.VIDEO:
            return self._build_video_command(input_path, output_path, preset)
        return self._build_audio_command(input_path, output_path, preset, probe)

    def _build_video_command(self, input_path: Path, output_path: Path, preset: Preset) -> list[str]:
        base = [self.ffmpeg_path, "-y", "-i", str(input_path)]

        if preset.key == "feature_phone":
            # STRICT exact-dimension output: scale to fit within the target
            # box, then pad with black bars to hit the exact WxH. Many real
            # feature-phone decoders reject non-standard dimensions outright,
            # so "fits within" is not good enough here — it must be exact.
            width, height = (320, 240) if preset.max_height == 240 else (176, 144)
            video_filter = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
            )
            return base + [
                "-vf",
                video_filter,
                "-c:v",
                "libx264",
                "-profile:v",
                "baseline",  # Maximum decoder compatibility on old hardware
                "-level",
                "3.0",
                "-pix_fmt",
                "yuv420p",  # Baseline profile mandates 4:2:0 chroma — without
                # forcing this explicitly, a 4:2:2/4:4:4 source (some screen
                # captures, some unusual encodes) makes libx264 hard-fail.
                "-b:v",
                f"{preset.video_bitrate_kbps}k",
                "-maxrate",
                f"{preset.video_bitrate_kbps}k",
                "-bufsize",
                f"{(preset.video_bitrate_kbps or 128) * 2}k",
                "-r",
                "15",  # Low framerate matches the era's decoder limits
                "-c:a",
                "aac",  # NOT amr_nb: rarely compiled into standard FFmpeg builds
                "-b:a",
                f"{preset.audio_bitrate_kbps}k",
                "-ar",
                "22050",
                "-ac",
                "1",
                "-movflags",
                "+faststart",
                str(output_path),
            ]

        # Computer/TV, Mobile Phone: cap resolution (only scales DOWN, never
        # up — "-2" keeps width even-divisible, required by most H.264 profiles)
        video_filter = f"scale=-2:'min({preset.max_height},ih)'"
        bitrate = preset.video_bitrate_kbps or 6000
        return base + [
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",  # Universal player compatibility regardless of source chroma
            "-maxrate",
            f"{bitrate}k",
            "-bufsize",
            f"{bitrate * 2}k",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    def _build_audio_command(
        self, input_path: Path, output_path: Path, preset: Preset, probe: MediaProbeResult
    ) -> list[str]:
        base = [self.ffmpeg_path, "-y", "-i", str(input_path), "-vn"]  # -vn: strip any embedded thumbnail video stream

        codec_by_container = {
            "mp3": ["-c:a", "libmp3lame"],
            "m4a": ["-c:a", "aac"],
            "opus": ["-c:a", "libopus"],
            "flac": ["-c:a", "flac"],
        }
        codec_args = codec_by_container.get(preset.target_container, ["-c:a", "libmp3lame"])

        # Don't pretend to "upscale" quality: if the source is already at or
        # below the target bitrate, re-encoding AT the target rate just
        # bloats the file without restoring detail that was never there.
        effective_bitrate = preset.audio_bitrate_kbps
        if probe.audio_bitrate_kbps and probe.audio_bitrate_kbps < preset.audio_bitrate_kbps:
            effective_bitrate = probe.audio_bitrate_kbps

        bitrate_args = [] if preset.target_container == "flac" else ["-b:a", f"{effective_bitrate}k"]

        return base + codec_args + bitrate_args + [str(output_path)]

    # ------------------------------------------------------------------ #
    # Metadata + thumbnail embedding (mutagen)
    # ------------------------------------------------------------------ #

    def embed_metadata(
        self,
        file_path: Path,
        metadata: TrackMetadata,
        thumbnail_path: Optional[Path] = None,
    ) -> EmbedStatus:
        """
        Write tags (and embedded artwork, if provided) into `file_path`
        in-place, using the container-appropriate mutagen API.

        Never raises — tagging failures are logged and reported via the
        return value, since a failed tag-embed should never be treated as
        a failed *download* (the media file itself is still good).
        """
        suffix = file_path.suffix.lower()
        try:
            if suffix == ".mp3":
                self._embed_mp3(file_path, metadata, thumbnail_path)
            elif suffix in (".m4a", ".mp4"):
                self._embed_mp4(file_path, metadata, thumbnail_path)
            elif suffix == ".flac":
                self._embed_flac(file_path, metadata, thumbnail_path)
            elif suffix == ".opus":
                self._embed_opus(file_path, metadata, thumbnail_path)
            else:
                logger.warning("No metadata embedder for extension %s (%s)", suffix, file_path.name)
                return EmbedStatus.UNSUPPORTED_FORMAT
        except Exception as exc:  # noqa: BLE001 — tagging must never crash the download loop
            logger.error("Metadata embed failed for %s: %s", file_path, exc)
            return EmbedStatus.FAILED

        logger.info("Metadata embedded for %s", file_path.name)
        return EmbedStatus.EMBEDDED

    @staticmethod
    def _read_thumbnail(thumbnail_path: Optional[Path]) -> Optional[bytes]:
        if thumbnail_path is None or not thumbnail_path.exists():
            return None
        try:
            return thumbnail_path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read thumbnail %s: %s", thumbnail_path, exc)
            return None

    def _embed_mp3(self, file_path: Path, metadata: TrackMetadata, thumbnail_path: Optional[Path]) -> None:
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TRCK

        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()

        tags["TIT2"] = TIT2(encoding=3, text=metadata.title)
        if metadata.artist:
            tags["TPE1"] = TPE1(encoding=3, text=metadata.artist)
        if metadata.album:
            tags["TALB"] = TALB(encoding=3, text=metadata.album)
        if metadata.year:
            tags["TDRC"] = TDRC(encoding=3, text=metadata.year)
        if metadata.track_number:
            tags["TRCK"] = TRCK(encoding=3, text=str(metadata.track_number))
        if metadata.genre:
            tags["TCON"] = TCON(encoding=3, text=metadata.genre)

        image_bytes = self._read_thumbnail(thumbnail_path)
        if image_bytes:
            tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_bytes)

        tags.save(file_path, v2_version=3)

    def _embed_mp4(self, file_path: Path, metadata: TrackMetadata, thumbnail_path: Optional[Path]) -> None:
        from mutagen.mp4 import MP4, MP4Cover

        audio = MP4(file_path)
        audio["\xa9nam"] = [metadata.title]
        if metadata.artist:
            audio["\xa9ART"] = [metadata.artist]
        if metadata.album:
            audio["\xa9alb"] = [metadata.album]
        if metadata.year:
            audio["\xa9day"] = [metadata.year]
        if metadata.track_number:
            audio["trkn"] = [(metadata.track_number, 0)]
        if metadata.genre:
            audio["\xa9gen"] = [metadata.genre]

        image_bytes = self._read_thumbnail(thumbnail_path)
        if image_bytes:
            audio["covr"] = [MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)]

        audio.save()

    def _embed_flac(self, file_path: Path, metadata: TrackMetadata, thumbnail_path: Optional[Path]) -> None:
        from mutagen.flac import FLAC, Picture

        audio = FLAC(file_path)
        audio["title"] = metadata.title
        if metadata.artist:
            audio["artist"] = metadata.artist
        if metadata.album:
            audio["album"] = metadata.album
        if metadata.year:
            audio["date"] = metadata.year
        if metadata.track_number:
            audio["tracknumber"] = str(metadata.track_number)
        if metadata.genre:
            audio["genre"] = metadata.genre

        image_bytes = self._read_thumbnail(thumbnail_path)
        if image_bytes:
            picture = Picture()
            picture.data = image_bytes
            picture.type = 3
            picture.mime = "image/jpeg"
            audio.clear_pictures()
            audio.add_picture(picture)

        audio.save()

    def _embed_opus(self, file_path: Path, metadata: TrackMetadata, thumbnail_path: Optional[Path]) -> None:
        import base64

        from mutagen.flac import Picture
        from mutagen.oggopus import OggOpus

        audio = OggOpus(file_path)
        audio["title"] = metadata.title
        if metadata.artist:
            audio["artist"] = metadata.artist
        if metadata.album:
            audio["album"] = metadata.album
        if metadata.year:
            audio["date"] = metadata.year
        if metadata.genre:
            audio["genre"] = metadata.genre

        image_bytes = self._read_thumbnail(thumbnail_path)
        if image_bytes:
            picture = Picture()
            picture.data = image_bytes
            picture.type = 3
            picture.mime = "image/jpeg"
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]

        audio.save()
