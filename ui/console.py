"""
ui/console.py

Single shared `rich.Console` instance plus the VFX color/emoji palette.

Every UI module imports `console` from here rather than instantiating its
own `Console()`. This guarantees consistent width detection, consistent
theme colors, and (critically) that `rich.progress` live displays never
collide with a second, independently-buffered Console writing to the
same terminal at the same time.
"""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Theme: maps semantic style names -> actual colors, so the rest of the
# codebase writes "[accent]...[/accent]" instead of hardcoded hex codes.
# Swapping the whole app's palette later means editing only this dict.
# ---------------------------------------------------------------------------
VFX_THEME = Theme(
    {
        "accent": "bold cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "danger": "bold red",
        "muted": "dim white",
        "title": "bold magenta",
        "highlight": "bold white on dark_blue",
        "brand": "bold bright_cyan",
    }
)

console = Console(theme=VFX_THEME, highlight=False)

# ---------------------------------------------------------------------------
# Emoji palette — centralized so the visual language stays consistent
# across menus, tables, and progress output without hunting through files.
# ---------------------------------------------------------------------------
def get_safe_width(minimum: int = 60, maximum: int = 200) -> int:
    """
    Return the current terminal width, clamped to a sane range.

    rich.Console already re-queries terminal size on every render call,
    so most of VFX's UI is responsive "for free" as long as nothing
    hardcodes a `width=`. This helper exists for the call sites that need
    an explicit number up front — e.g. deciding whether to render a wide
    multi-column preset table or a condensed fallback — rather than
    letting rich wrap text within whatever width it detects.
    """
    try:
        width = console.size.width
    except Exception:
        width = 80
    return max(minimum, min(width, maximum))


def is_narrow_terminal(threshold: int = 80) -> bool:
    """True if the terminal is narrower than `threshold` columns."""
    return get_safe_width(minimum=1, maximum=10_000) < threshold


def supports_rich_rendering() -> bool:
    """
    False for piped/redirected output, dumb terminals, or other
    non-interactive contexts (CI logs, `vfx > out.txt`). VFX uses this to
    decide whether to show animated spinners/progress bars or fall back
    to simple line-by-line status prints that won't litter a log file
    with raw escape codes.
    """
    return console.is_terminal and not console.is_dumb_terminal


EMOJI = {
    "rocket": "🚀",
    "desktop": "🖥️",
    "computer": "💻",
    "mobile": "📱",
    "feature_phone": "📞",
    "headphones": "🎧",
    "gear": "⚙️",
    "refresh": "🔄",
    "wrench": "🛠️",
    "check": "✅",
    "cross": "❌",
    "warning": "⚠️",
    "music": "🎵",
    "video": "🎬",
    "spotify": "🟢",
    "youtube": "▶️",
    "link": "🔗",
    "folder": "📁",
    "clock": "⏱️",
    "sparkles": "✨",
    "fire": "🔥",
    "download": "⬇️",
}
