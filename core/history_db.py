"""
core/history_db.py

Persistent download history, backed by TinyDB (a lightweight, dependency-
light local JSON document store — no server process, no schema migration
tooling needed for a single-user CLI tool).

This backs two distinct features from the spec:

1. Smart Resume & Duplicate Prevention: cross-references a track's stable
   source identity (e.g. a YouTube video ID or Spotify track ID) against
   past successful downloads. This catches a case PathManager's file-
   existence check alone cannot: the user deleted or moved the output
   file afterward, but VFX still shouldn't silently re-download it
   without at least being able to tell the user "this was already
   fetched on <date>" if asked.

2. The post-download summary table: every attempt in a session — success,
   failure, or skip — is recorded here, so the summary can be rendered
   from one structured source instead of threading ad-hoc counters
   through the download loop by hand.

Thread-safety: the upcoming download engine runs multiple workers
concurrently via ThreadPoolExecutor, and TinyDB's default JSON storage is
not safe for concurrent writes from multiple threads (two threads writing
at once can interleave and corrupt the file). Every write path here is
serialized through a single `threading.Lock`.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from tinydb import Query, TinyDB
from tinydb.storages import JSONStorage

from utils.logger import get_logger

logger = get_logger()

DEFAULT_HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "history.json"


class HistoryError(Exception):
    """Raised when the history database cannot be read or written."""


class DownloadStatus(str, Enum):
    """Outcome of a single download attempt, as recorded in history."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"  # Duplicate-prevention skip, not a failure


@dataclass
class HistoryEntry:
    """One row of download history — one track, one attempt."""

    source_url: str
    title: str
    status: DownloadStatus
    session_id: str
    source_id: Optional[str] = None  # Stable ID (YouTube video ID, Spotify track ID, etc.)
    artist: Optional[str] = None
    file_path: Optional[str] = None
    file_size_bytes: Optional[int] = None
    preset_key: Optional[str] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None
    downloaded_at: float = field(default_factory=time.time)
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        data = dict(data)
        data["status"] = DownloadStatus(data["status"])
        return cls(**data)


@dataclass
class SessionSummary:
    """Aggregate stats for a single run — feeds the post-download summary table."""

    session_id: str
    total_attempts: int
    successful: int
    failed: int
    skipped: int
    total_bytes: int
    total_duration_seconds: float
    entries: list[HistoryEntry] = field(default_factory=list)

    @property
    def total_size_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)


class DownloadHistory:
    """
    Thread-safe wrapper around a TinyDB-backed history store.

    One instance is shared across all worker threads in a download
    session; every public write method acquires `self._lock` before
    touching the underlying TinyDB table.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DEFAULT_HISTORY_PATH
        self._lock = threading.Lock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = TinyDB(self.db_path, storage=JSONStorage)
        except OSError as exc:
            logger.error("Could not open history database at %s: %s", self.db_path, exc)
            raise HistoryError(f"Could not open history database at {self.db_path}: {exc}") from exc

    def close(self) -> None:
        """Flush and close the underlying TinyDB file handle."""
        with self._lock:
            try:
                self._db.close()
            except OSError as exc:
                logger.warning("Error closing history database: %s", exc)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    def record(self, entry: HistoryEntry) -> None:
        """
        Append one history entry. Safe to call concurrently from multiple
        download worker threads.
        """
        with self._lock:
            try:
                self._db.insert(entry.to_dict())
            except OSError as exc:
                # A failed history WRITE must never be allowed to look like
                # a failed DOWNLOAD to the caller — log and swallow.
                logger.error("Could not write history entry for %s: %s", entry.source_url, exc)

    def record_success(
        self,
        source_url: str,
        title: str,
        session_id: str,
        *,
        source_id: Optional[str] = None,
        artist: Optional[str] = None,
        file_path: Optional[str] = None,
        file_size_bytes: Optional[int] = None,
        preset_key: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """Convenience wrapper for the common successful-download case."""
        self.record(
            HistoryEntry(
                source_url=source_url,
                title=title,
                status=DownloadStatus.SUCCESS,
                session_id=session_id,
                source_id=source_id,
                artist=artist,
                file_path=file_path,
                file_size_bytes=file_size_bytes,
                preset_key=preset_key,
                duration_seconds=duration_seconds,
            )
        )

    def record_failure(
        self,
        source_url: str,
        title: str,
        session_id: str,
        error_message: str,
        *,
        source_id: Optional[str] = None,
        preset_key: Optional[str] = None,
    ) -> None:
        """Convenience wrapper for the common failed-download case."""
        self.record(
            HistoryEntry(
                source_url=source_url,
                title=title,
                status=DownloadStatus.FAILED,
                session_id=session_id,
                source_id=source_id,
                preset_key=preset_key,
                error_message=error_message,
            )
        )

    def record_skip(
        self,
        source_url: str,
        title: str,
        session_id: str,
        *,
        source_id: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> None:
        """Convenience wrapper for a duplicate-prevention skip."""
        self.record(
            HistoryEntry(
                source_url=source_url,
                title=title,
                status=DownloadStatus.SKIPPED,
                session_id=session_id,
                source_id=source_id,
                file_path=file_path,
            )
        )

    # ------------------------------------------------------------------ #
    # Reading / duplicate lookup
    # ------------------------------------------------------------------ #

    def was_downloaded(self, source_id: str) -> Optional[HistoryEntry]:
        """
        Return the most recent successful download for `source_id`, or
        None if it has never been successfully downloaded.

        :param source_id: A stable source identifier (YouTube video ID,
            Spotify track ID, etc.) — NOT the destination file path,
            since that can change between runs while the source identity
            stays constant.
        """
        with self._lock:
            try:
                query = Query()
                matches = self._db.search(
                    (query.source_id == source_id) & (query.status == DownloadStatus.SUCCESS.value)
                )
            except OSError as exc:
                logger.warning("History lookup failed for source_id=%s: %s", source_id, exc)
                return None

        if not matches:
            return None
        most_recent = max(matches, key=lambda m: m.get("downloaded_at", 0))
        return HistoryEntry.from_dict(most_recent)

    def get_session_summary(self, session_id: str) -> SessionSummary:
        """Aggregate every entry recorded under `session_id` into a summary."""
        with self._lock:
            try:
                query = Query()
                raw_entries = self._db.search(query.session_id == session_id)
            except OSError as exc:
                logger.warning("Could not build session summary for %s: %s", session_id, exc)
                raw_entries = []

        entries = [HistoryEntry.from_dict(e) for e in raw_entries]
        successful = sum(1 for e in entries if e.status == DownloadStatus.SUCCESS)
        failed = sum(1 for e in entries if e.status == DownloadStatus.FAILED)
        skipped = sum(1 for e in entries if e.status == DownloadStatus.SKIPPED)
        total_bytes = sum(e.file_size_bytes or 0 for e in entries if e.status == DownloadStatus.SUCCESS)
        total_duration = sum(e.duration_seconds or 0.0 for e in entries)

        return SessionSummary(
            session_id=session_id,
            total_attempts=len(entries),
            successful=successful,
            failed=failed,
            skipped=skipped,
            total_bytes=total_bytes,
            total_duration_seconds=total_duration,
            entries=entries,
        )

    @staticmethod
    def new_session_id() -> str:
        """Generate a fresh session ID for a new download run."""
        return uuid.uuid4().hex[:12]
