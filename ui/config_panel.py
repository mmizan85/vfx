"""
ui/config_panel.py

Interactive configuration panel (the `--config` flag's destination).

This module is pure presentation + input-gathering. It never writes to
config.json directly — every change is routed through a `ConfigManager`
instance's mutator methods, and `save()` is only called after the user
explicitly confirms. This means a user can wander through every setting,
back out, and nothing on disk changes until they choose "Save & Exit".
"""

from __future__ import annotations

import questionary
from questionary import Style as QStyle
from rich.panel import Panel
from rich.table import Table

from core.config import ConfigError, ConfigManager
from core.presets import QUICK_PRESETS
from ui.console import EMOJI, console
from utils.logger import get_logger

logger = get_logger()

# Custom questionary style so prompts visually match the rich theme
# rather than questionary's flat default colors.
_QMARK_STYLE = QStyle(
    [
        ("qmark", "fg:#00d7ff bold"),
        ("question", "bold"),
        ("answer", "fg:#00ff87 bold"),
        ("pointer", "fg:#00d7ff bold"),
        ("highlighted", "fg:#00d7ff bold"),
        ("selected", "fg:#00ff87"),
    ]
)


class ConfigPanel:
    """
    Renders the interactive settings menu and dispatches edits to a
    `ConfigManager`. Instantiate once per `--config` invocation.
    """

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """
        Main loop: show current settings, present an action menu, repeat
        until the user exits. Exiting without choosing "Save" discards
        in-memory changes (since they were never written to disk anyway —
        ConfigManager.save() is the only thing that persists state).
        """
        while True:
            self._render_summary()
            action = questionary.select(
                "What would you like to configure?",
                choices=[
                    f"{EMOJI['spotify']}  Spotify API Credentials",
                    f"{EMOJI['folder']}  Default Download Path",
                    f"{EMOJI['gear']}  Concurrency (thread count)",
                    f"{EMOJI['rocket']}  Default Preset Selection",
                    f"{EMOJI['clock']}  Rate Limit (speed cap)",
                    f"{EMOJI['sparkles']}  Feature Toggles (metadata, lyrics, auto-update...)",
                    f"{EMOJI['link']}  Cookies / Age-Restricted Content",
                    f"{EMOJI['wrench']}  FFmpeg path override",
                    f"{EMOJI['check']}  Save & Exit",
                    f"{EMOJI['cross']}  Discard & Exit",
                ],
                style=_QMARK_STYLE,
            ).ask()

            if action is None or action.endswith("Discard & Exit"):
                console.print(f"\n[muted]{EMOJI['cross']} No changes saved.[/muted]\n")
                return

            if action.endswith("Spotify API Credentials"):
                self._edit_spotify_credentials()
            elif action.endswith("Default Download Path"):
                self._edit_download_path()
            elif action.endswith("Concurrency (thread count)"):
                self._edit_thread_count()
            elif action.endswith("Default Preset Selection"):
                self._edit_default_preset()
            elif action.endswith("Rate Limit (speed cap)"):
                self._edit_rate_limit()
            elif "Feature Toggles" in action:
                self._edit_toggles()
            elif action.endswith("Cookies / Age-Restricted Content"):
                self._edit_cookies()
            elif action.endswith("FFmpeg path override"):
                self._edit_ffmpeg_path()
            elif action.endswith("Save & Exit"):
                self._save_and_exit()
                return

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render_summary(self) -> None:
        """Print a rich Table snapshot of current (in-memory) settings."""
        cfg = self.config_manager.config
        table = Table(title=f"{EMOJI['gear']} VFX Configuration", show_lines=False, expand=False)
        table.add_column("Setting", style="accent", no_wrap=True)
        table.add_column("Value", style="white")

        spotify_status = (
            f"{EMOJI['check']} Configured"
            if cfg.spotify.is_configured
            else f"{EMOJI['warning']} Not configured"
        )
        table.add_row("Spotify API", spotify_status)
        table.add_row("Download Path", cfg.defaults.download_path)
        table.add_row("Thread Count", str(cfg.defaults.thread_count))
        preset_label = "Always ask"
        if cfg.defaults.default_preset_key:
            matched = next((p for p in QUICK_PRESETS if p.key == cfg.defaults.default_preset_key), None)
            preset_label = f"{matched.emoji} {matched.label}" if matched else cfg.defaults.default_preset_key
        table.add_row("Default Preset", preset_label)
        rate_label = f"{cfg.defaults.rate_limit_kbps} KB/s" if cfg.defaults.rate_limit_kbps else "Unlimited"
        table.add_row("Rate Limit", rate_label)
        table.add_row("Embed Metadata", "Yes" if cfg.defaults.embed_metadata else "No")
        table.add_row("Embed Thumbnail", "Yes" if cfg.defaults.embed_thumbnail else "No")
        table.add_row("Synced Lyrics", "Yes" if cfg.defaults.fetch_synced_lyrics else "No")
        table.add_row("Skip Existing Files", "Yes" if cfg.defaults.skip_existing_files else "No")
        table.add_row("Auto-Update yt-dlp", "Yes" if cfg.auto_update_ytdlp else "No")
        cookie_label = "None"
        if cfg.cookies_from_browser:
            cookie_label = f"Browser: {cfg.cookies_from_browser}"
        elif cfg.cookies_file_path:
            cookie_label = f"File: {cfg.cookies_file_path}"
        table.add_row("Cookie Source", cookie_label)
        table.add_row("FFmpeg Path", cfg.ffmpeg_path or "(auto-detect)")
        table.add_row("Config File", self.config_manager.describe_source())

        console.print()
        console.print(table)

    # ------------------------------------------------------------------ #
    # Individual edit flows
    # ------------------------------------------------------------------ #

    def _edit_spotify_credentials(self) -> None:
        console.print(
            Panel(
                "Create a free app at [link]https://developer.spotify.com/dashboard[/link] "
                "to get a Client ID and Secret. These are read-only catalog credentials — "
                "VFX never asks for your Spotify password.",
                title=f"{EMOJI['spotify']} Spotify Setup",
                border_style="accent",
            )
        )
        client_id = questionary.text("Client ID:", style=_QMARK_STYLE).ask()
        if client_id is None:
            return
        client_secret = questionary.password("Client Secret:", style=_QMARK_STYLE).ask()
        if client_secret is None:
            return

        if not client_id.strip() or not client_secret.strip():
            console.print(f"[warning]{EMOJI['warning']} Both fields are required — no changes made.[/warning]")
            return

        self.config_manager.set_spotify_credentials(client_id, client_secret)
        console.print(f"[success]{EMOJI['check']} Spotify credentials updated (not yet saved).[/success]")

    def _edit_download_path(self) -> None:
        current = self.config_manager.config.defaults.download_path
        new_path = questionary.path(
            "Default download folder:", default=current, style=_QMARK_STYLE
        ).ask()
        if new_path is None or not new_path.strip():
            return
        self.config_manager.set_download_path(new_path)
        console.print(f"[success]{EMOJI['check']} Download path updated (not yet saved).[/success]")

    def _edit_thread_count(self) -> None:
        current = self.config_manager.config.defaults.thread_count
        raw = questionary.text(
            f"Concurrent download threads (1-16) [current: {current}]:",
            style=_QMARK_STYLE,
        ).ask()
        if raw is None or not raw.strip():
            return
        try:
            value = int(raw.strip())
            self.config_manager.set_thread_count(value)
            console.print(f"[success]{EMOJI['check']} Thread count set to {value} (not yet saved).[/success]")
        except ValueError as exc:
            logger.warning("Invalid thread count input %r: %s", raw, exc)
            console.print(f"[danger]{EMOJI['cross']} Invalid value — must be a whole number from 1-16.[/danger]")

    def _edit_default_preset(self) -> None:
        """Pick a preset to auto-apply on every download, or 'Always ask'."""
        current = self.config_manager.config.defaults.default_preset_key
        ask_sentinel = "__always_ask__"  # Distinct from None so cancellation (which

        # questionary also represents as None) can never be confused with the
        # user explicitly choosing "Always ask" — conflating the two would
        # silently wipe an existing preset whenever someone just hits Esc.
        choices = [questionary.Choice("Always ask (show the menu every time)", value=ask_sentinel)]
        choices += [questionary.Choice(p.menu_title, value=p.key) for p in QUICK_PRESETS]

        selection = questionary.select(
            "Default preset to auto-apply (skips the menu on every download):",
            choices=choices,
            default=current or ask_sentinel,
            style=_QMARK_STYLE,
        ).ask()
        if selection is None:
            return  # Cancelled — leave the existing setting untouched.

        resolved_key = None if selection == ask_sentinel else selection
        self.config_manager.set_default_preset(resolved_key)
        label = "Always ask" if resolved_key is None else resolved_key
        console.print(f"[success]{EMOJI['check']} Default preset set to: {label} (not yet saved).[/success]")

    def _edit_rate_limit(self) -> None:
        current = self.config_manager.config.defaults.rate_limit_kbps
        raw = questionary.text(
            f"Speed cap in KB/s, minimum 64 (current: {current or 'unlimited'}). "
            f"Leave blank for unlimited:",
            style=_QMARK_STYLE,
        ).ask()
        if raw is None:
            return
        raw = raw.strip()
        if not raw:
            self.config_manager.set_rate_limit(None)
            console.print(f"[success]{EMOJI['check']} Rate limit cleared — unlimited speed (not yet saved).[/success]")
            return
        try:
            value = int(raw)
            self.config_manager.set_rate_limit(value)
            console.print(f"[success]{EMOJI['check']} Rate limit set to {value} KB/s (not yet saved).[/success]")
        except ValueError as exc:
            logger.warning("Invalid rate limit input %r: %s", raw, exc)
            console.print(f"[danger]{EMOJI['cross']} Invalid value: {exc}[/danger]")

    def _edit_toggles(self) -> None:
        cfg = self.config_manager.config
        defaults = cfg.defaults
        selected = questionary.checkbox(
            "Toggle features (checked = enabled):",
            choices=[
                questionary.Choice("Embed ID3/MP4 metadata tags", checked=defaults.embed_metadata),
                questionary.Choice("Embed thumbnail / album art", checked=defaults.embed_thumbnail),
                questionary.Choice("Write subtitles when available", checked=defaults.write_subtitles),
                questionary.Choice(
                    "Fetch synced (.lrc) lyrics for audio downloads", checked=defaults.fetch_synced_lyrics
                ),
                questionary.Choice(
                    "Skip files that already exist (duplicate prevention)",
                    checked=defaults.skip_existing_files,
                ),
                questionary.Choice("Auto-update yt-dlp on startup", checked=cfg.auto_update_ytdlp),
            ],
            style=_QMARK_STYLE,
        ).ask()
        if selected is None:
            return

        self.config_manager.toggle_metadata_embedding("Embed ID3/MP4 metadata tags" in selected)
        self.config_manager.toggle_thumbnail_embedding("Embed thumbnail / album art" in selected)
        self.config_manager.config.defaults.write_subtitles = "Write subtitles when available" in selected
        self.config_manager.toggle_synced_lyrics("Fetch synced (.lrc) lyrics for audio downloads" in selected)
        self.config_manager.toggle_skip_existing(
            "Skip files that already exist (duplicate prevention)" in selected
        )
        self.config_manager.toggle_auto_update_ytdlp("Auto-update yt-dlp on startup" in selected)
        console.print(f"[success]{EMOJI['check']} Toggles updated (not yet saved).[/success]")

    def _edit_cookies(self) -> None:
        """
        Configure the cookie source used for age-restricted/private content.
        Browser-extraction and a manual cookies.txt file are mutually
        exclusive — yt-dlp only accepts one source, so the panel enforces
        that invariant rather than letting both linger in config.json.
        """
        cfg = self.config_manager.config
        current_mode = "browser" if cfg.cookies_from_browser else ("file" if cfg.cookies_file_path else "none")

        cookie_mode_choices = [
            questionary.Choice("None — public content only", value="none"),
            questionary.Choice("Extract from an installed browser", value="browser"),
            questionary.Choice("Use a cookies.txt file", value="file"),
        ]
        mode = questionary.select(
            "Cookie source for age-restricted or private content:",
            choices=cookie_mode_choices,
            default=current_mode,
            style=_QMARK_STYLE,
        ).ask()
        if mode is None:
            return

        if mode == "none":
            self.config_manager.set_browser_cookies(None)
            self.config_manager.set_cookies_file(None)
            console.print(f"[success]{EMOJI['check']} Cookie source cleared (not yet saved).[/success]")
            return

        if mode == "browser":
            browser = questionary.select(
                "Which browser?",
                choices=["chrome", "firefox", "edge", "brave", "opera", "vivaldi", "chromium", "safari"],
                default=cfg.cookies_from_browser,
                style=_QMARK_STYLE,
            ).ask()
            if browser is None:
                return
            try:
                self.config_manager.set_browser_cookies(browser)
                console.print(
                    f"[success]{EMOJI['check']} Cookie source set to browser: {browser} (not yet saved).[/success]"
                )
            except ValueError as exc:
                console.print(f"[danger]{EMOJI['cross']} {exc}[/danger]")
            return

        # mode == "file"
        path = questionary.path(
            "Path to cookies.txt:", default=cfg.cookies_file_path or "", style=_QMARK_STYLE
        ).ask()
        if path is None or not path.strip():
            return
        self.config_manager.set_cookies_file(path)
        console.print(f"[success]{EMOJI['check']} Cookie source set to file: {path.strip()} (not yet saved).[/success]")

    def _edit_ffmpeg_path(self) -> None:
        current = self.config_manager.config.ffmpeg_path or ""
        new_path = questionary.text(
            "FFmpeg binary path (leave blank for auto-detect):",
            default=current,
            style=_QMARK_STYLE,
        ).ask()
        if new_path is None:
            return
        self.config_manager.set_ffmpeg_path(new_path.strip() or None)
        console.print(f"[success]{EMOJI['check']} FFmpeg path override updated (not yet saved).[/success]")

    def _save_and_exit(self) -> None:
        try:
            self.config_manager.save()
            console.print(f"\n[success]{EMOJI['sparkles']} Configuration saved successfully![/success]\n")
        except ConfigError as exc:
            logger.error("Failed to save config from panel: %s", exc)
            console.print(f"\n[danger]{EMOJI['cross']} Could not save configuration: {exc}[/danger]\n")
