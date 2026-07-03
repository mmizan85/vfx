"""
ui/summary_panel.py

Renders the post-download "Summary Panel" described in the spec.

Everything here is pure presentation: it reads from a `SessionSummary`
(built by DownloadHistory.get_session_summary) and renders it. No I/O,
no engine logic, no side effects — just rich tables and panels.
"""

from __future__ import annotations

import time
from pathlib import Path

from rich.align import Align
from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.history_db import DownloadStatus, SessionSummary
from ui.console import EMOJI, console
from utils.logger import get_logger

logger = get_logger()


def _fmt_bytes(n: int) -> str:
    """Human-readable byte count (B → KB → MB → GB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1024:.1f} KB"
    if n < 1_073_741_824:
        return f"{n / 1_048_576:.1f} MB"
    return f"{n / 1_073_741_824:.2f} GB"


def _fmt_duration(seconds: float) -> str:
    """Human-readable elapsed duration (e.g. '2m 34s')."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def render_summary(summary: SessionSummary, elapsed_seconds: float) -> None:
    """
    Render the full post-download summary to the terminal.

    :param summary: Session summary built by DownloadHistory.get_session_summary().
    :param elapsed_seconds: Wall-clock seconds the download session took,
        provided by ProgressTracker.elapsed_seconds() since DownloadHistory
        stores per-track processing time rather than the overall session clock.
    """
    console.print()

    # ------------------------------------------------------------------ #
    # Stat badges (success / failed / skipped counters)
    # ------------------------------------------------------------------ #
    def _badge(label: str, count: int, style: str) -> Panel:
        inner = Text()
        inner.append(f"{count}\n", style=f"bold {style}")
        inner.append(label, style=f"dim {style}")
        return Panel(
            Align.center(inner),
            border_style=style,
            padding=(0, 2),
            expand=True,
        )

    badges = Columns(
        [
            _badge(f"{EMOJI['check']} Success", summary.successful, "green"),
            _badge(f"{EMOJI['cross']} Failed", summary.failed, "red"),
            _badge(f"{EMOJI['refresh']} Skipped", summary.skipped, "yellow"),
            _badge(f"{EMOJI['clock']} Time", 1, "cyan"),  # placeholder, replaced below
        ],
        equal=True,
        expand=True,
    )

    # Rebuild with real time instead of placeholder
    badges = Columns(
        [
            _badge(f"{EMOJI['check']} Success", summary.successful, "green"),
            _badge(f"{EMOJI['cross']} Failed", summary.failed, "red"),
            _badge(f"{EMOJI['refresh']} Skipped", summary.skipped, "yellow"),
            Panel(
                Align.center(
                    Text.from_markup(
                        f"[bold cyan]{_fmt_duration(elapsed_seconds)}[/bold cyan]\n"
                        f"[dim cyan]{EMOJI['clock']} Total Time[/dim cyan]"
                    )
                ),
                border_style="cyan",
                padding=(0, 2),
                expand=True,
            ),
        ],
        equal=True,
        expand=True,
    )

    console.print(
        Panel(
            badges,
            title=f"{EMOJI['sparkles']} Download Complete — Session Summary",
            border_style="brand",
            padding=(1, 0),
        )
    )

    # ------------------------------------------------------------------ #
    # Quick stats bar
    # ------------------------------------------------------------------ #
    stats_table = Table.grid(expand=True)
    stats_table.add_column(style="muted", justify="right")
    stats_table.add_column(style="white", justify="left")
    stats_table.add_column(style="muted", justify="right")
    stats_table.add_column(style="white", justify="left")
    stats_table.add_row(
        "Total tracks:", str(summary.total_attempts),
        "Data downloaded:", _fmt_bytes(summary.total_bytes),
    )
    stats_table.add_row(
        "Session ID:", f"[muted]{summary.session_id}[/muted]",
        "Average speed:", _estimate_avg_speed(summary, elapsed_seconds),
    )
    console.print(Panel(stats_table, border_style="muted", padding=(0, 2)))

    # ------------------------------------------------------------------ #
    # Per-track results table (only rendered if there are entries)
    # ------------------------------------------------------------------ #
    if not summary.entries:
        return

    track_table = Table(
        show_header=True,
        header_style="accent",
        show_lines=True,
        expand=True,
    )
    track_table.add_column("#", width=4, justify="right", style="muted")
    track_table.add_column("Title", min_width=20, ratio=3)
    track_table.add_column("Artist", min_width=12, ratio=2)
    track_table.add_column("Status", width=12, justify="center")
    track_table.add_column("Size", width=9, justify="right")
    track_table.add_column("Duration", width=8, justify="right")
    track_table.add_column("Output Path", min_width=20, ratio=3, overflow="fold")

    for idx, entry in enumerate(summary.entries, start=1):
        status_text, row_style = _status_display(entry.status)
        size_str = _fmt_bytes(entry.file_size_bytes) if entry.file_size_bytes else "—"
        dur_str = _fmt_duration(entry.duration_seconds) if entry.duration_seconds else "—"
        path_str = _truncate_path(entry.file_path) if entry.file_path else (entry.error_message or "—")

        track_table.add_row(
            str(idx),
            entry.title or "—",
            entry.artist or "—",
            status_text,
            size_str,
            dur_str,
            path_str,
            style=row_style,
        )

    console.print(
        Panel(
            track_table,
            title=f"{EMOJI['music']} Track Results",
            border_style="accent",
        )
    )
    console.print()


def _status_display(status: DownloadStatus) -> tuple[str, str]:
    """Return (rich-formatted status string, row style) for a history entry."""
    if status == DownloadStatus.SUCCESS:
        return f"[success]{EMOJI['check']} Success[/success]", ""
    if status == DownloadStatus.FAILED:
        return f"[danger]{EMOJI['cross']} Failed[/danger]", "dim"
    return f"[warning]{EMOJI['refresh']} Skipped[/warning]", "dim"


def _truncate_path(path_str: str, max_len: int = 60) -> str:
    """Show just filename + parent dir when the full path is very long."""
    p = Path(path_str)
    short = f"…/{p.parent.name}/{p.name}"
    if len(path_str) <= max_len:
        return path_str
    return short if len(short) <= max_len else p.name


def _estimate_avg_speed(summary: SessionSummary, elapsed: float) -> str:
    """Rough average speed across the whole session."""
    if elapsed <= 0 or summary.total_bytes == 0:
        return "—"
    bps = summary.total_bytes / elapsed
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1_048_576:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1_048_576:.1f} MB/s"
