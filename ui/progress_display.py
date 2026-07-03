"""
ui/progress_display.py

Thread-safe progress tracking for concurrent downloads.

Architecture notes
------------------
rich.progress.Progress is internally protected by an RLock, so calling
`.update()` and `.add_task()` concurrently from multiple ThreadPoolExecutor
workers is safe — we don't need an outer lock for those operations.

What we DO need our own lock for: the _completed counter (used to decide
when to advance the overall bar) and the task-slot pool (used so each
worker thread owns exactly one named progress bar slot for the duration of
its active download rather than adding unbounded tasks to the list).

The "fallback to print" mode (when supports_rich_rendering() is False —
e.g. CI pipelines or piped output) is handled transparently: callers use
the same API and simply get one-line status prints instead of live bars.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from rich.columns import Columns
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

from ui.console import EMOJI, console, supports_rich_rendering
from utils.logger import get_logger

logger = get_logger()


class _EtaOrSpeedColumn(ProgressColumn):
    """
    Shows ETA when the total size is known, or speed-only when streaming
    (total unknown). Avoids the ugly '?:??:??' that standard TimeRemainingColumn
    emits when total_bytes isn't populated yet.
    """

    def render(self, task) -> Text:
        if task.total is not None and task.speed:
            remaining = (task.total - task.completed) / task.speed if task.speed else None
            if remaining is not None:
                mins, secs = divmod(int(remaining), 60)
                return Text(f"ETA {mins}:{secs:02d}", style="muted")
        if task.speed:
            return Text(f"{task.speed / 1024:.0f} KB/s", style="muted")
        return Text("…", style="muted")


def _build_progress() -> Progress:
    """Construct the styled Progress instance shared across all workers."""
    return Progress(
        SpinnerColumn(spinner_name="dots", style="accent"),
        TextColumn("[accent]{task.description}[/accent]", table_column=None),
        BarColumn(bar_width=None, style="accent", complete_style="success"),
        DownloadColumn(),
        TransferSpeedColumn(),
        _EtaOrSpeedColumn(),
        console=console,
        expand=True,
        refresh_per_second=8,
    )


class ProgressTracker:
    """
    Manages per-track progress bars and one overall bar for a download session.

    Usage (by the download engine)
    --------------------------------
        tracker = ProgressTracker(total_tracks=len(entries))
        with tracker:
            task_id = tracker.add_task("My Track Title")
            tracker.update(task_id, downloaded=512_000, total=3_000_000, speed=200_000)
            tracker.complete(task_id)

    :param total_tracks: Total number of tracks being processed this session.
        Used to populate the "N/M tracks" overall bar.
    :param session_label: Short string shown in the overall bar description.
    """

    def __init__(self, total_tracks: int, session_label: str = "Downloading") -> None:
        self.total_tracks = total_tracks
        self.session_label = session_label
        self._rich_mode = supports_rich_rendering()
        self._progress: Optional[Progress] = None
        self._overall_task: Optional[TaskID] = None
        self._lock = threading.Lock()
        self._completed_count = 0
        self._start_time = time.monotonic()

    # ------------------------------------------------------------------ #
    # Context manager
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "ProgressTracker":
        if self._rich_mode:
            self._progress = _build_progress()
            self._progress.start()
            self._overall_task = self._progress.add_task(
                f"{EMOJI['download']} {self.session_label}",
                total=self.total_tracks,
            )
        return self

    def __exit__(self, *_) -> None:
        if self._progress is not None:
            self._progress.stop()

    # ------------------------------------------------------------------ #
    # Per-track task lifecycle
    # ------------------------------------------------------------------ #

    def add_task(self, title: str) -> Optional[TaskID]:
        """
        Register a new download task and return its task ID.

        Returns None in non-rich (fallback) mode — callers pass this
        through to update/complete without checking, since those methods
        are also no-ops in fallback mode.
        """
        truncated = (title[:45] + "…") if len(title) > 48 else title
        if self._rich_mode and self._progress is not None:
            return self._progress.add_task(
                f"{EMOJI['music']} {truncated}",
                total=None,   # indeterminate until first hook call supplies total_bytes
                visible=True,
            )
        else:
            console.print(f"[muted]{EMOJI['download']} Starting:[/muted] {truncated}")
            return None

    def update(
        self,
        task_id: Optional[TaskID],
        *,
        downloaded: int,
        total: Optional[int],
        speed: Optional[float],
    ) -> None:
        """
        Update download progress for `task_id`. Safe to call from any thread.

        :param downloaded: Bytes downloaded so far.
        :param total: Total file size in bytes, or None if unknown.
        :param speed: Current speed in bytes/sec, or None.
        """
        if not self._rich_mode or self._progress is None or task_id is None:
            return
        update_kwargs: dict = {"completed": downloaded}
        if total is not None:
            update_kwargs["total"] = total
        self._progress.update(task_id, **update_kwargs)

    def complete(self, task_id: Optional[TaskID], *, skipped: bool = False) -> None:
        """
        Mark a task as finished (success or deliberate skip) and advance
        the overall bar by one.
        """
        self._finish_task(task_id, failed=False)

    def fail(self, task_id: Optional[TaskID]) -> None:
        """Mark a task as failed (error colour) and advance the overall bar."""
        self._finish_task(task_id, failed=True)

    def _finish_task(self, task_id: Optional[TaskID], *, failed: bool) -> None:
        if self._rich_mode and self._progress is not None and task_id is not None:
            # Set to 100 % and hide — keeps the bar list from accumulating
            # finished rows while downloads are still running.
            try:
                task = next(t for t in self._progress.tasks if t.id == task_id)
                self._progress.update(
                    task_id,
                    completed=task.total if task.total else 1,
                    total=task.total if task.total else 1,
                    visible=False,
                )
            except StopIteration:
                pass

        with self._lock:
            self._completed_count += 1
            completed = self._completed_count

        if self._rich_mode and self._progress is not None and self._overall_task is not None:
            label = (
                f"[danger]{EMOJI['cross']} Failed[/danger]"
                if failed
                else f"[success]{EMOJI['check']} Done[/success]"
            )
            self._progress.update(
                self._overall_task,
                advance=1,
                description=(
                    f"{EMOJI['download']} {self.session_label} "
                    f"({completed}/{self.total_tracks})"
                ),
            )
        else:
            symbol = EMOJI["cross"] if failed else EMOJI["check"]
            console.print(f"[muted]{symbol} {completed}/{self.total_tracks} tracks done.[/muted]")

    # ------------------------------------------------------------------ #
    # Session stats
    # ------------------------------------------------------------------ #

    def elapsed_seconds(self) -> float:
        """Wall-clock seconds since this tracker's context was entered."""
        return time.monotonic() - self._start_time
