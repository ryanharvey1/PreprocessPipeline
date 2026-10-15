"""User-level overnight Phy recovery control, shared across session configs."""
from __future__ import annotations

from html import escape
import os
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from src.phy_watchdog import DEFAULT_SETTINGS, disable_watchdog, install_watchdog, read_settings, watchdog_root


class PhyWatchdogControl(QWidget):
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, config_dir: Path | None = None) -> None:
        super().__init__(parent)
        self.config_dir = config_dir if config_dir is not None else watchdog_root().parent
        self._enabled = False
        self._notice = ""
        self._description = (
            "Leaving Phy open can prevent spike sorting from starting because it keeps the "
            "graphics card (GPU) in use. This option automatically backs up your curation files, "
            "saves your edits and checks that they were saved, then closes Phy so sorting can continue."
        )
        self.button = QPushButton("Enable automatic Phy closing")
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.button)
        layout.addWidget(self.status, 1)
        self.button.clicked.connect(self._toggle)
        self.refresh()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(10000)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start()

    def refresh(self) -> None:
        settings = dict(DEFAULT_SETTINGS)
        self._set_tooltip(settings)
        if os.name != "nt":
            self.button.setEnabled(False)
            self.status.setText("Automatic Phy closing is available on Windows.")
            return
        self._enabled = False
        root = self.config_dir / "pipeline-watchdog"
        try:
            settings = read_settings(root)
            configured = (
                (self.config_dir / "plugins" / "pipeline_gpu_watchdog.py").is_file()
                and "# BEGIN PreprocessPipeline GPU watchdog" in
                (self.config_dir / "phy_config.py").read_text(encoding="utf-8")
            )
            self._enabled = settings["enabled"] and configured
            if settings["enabled"] and not configured:
                text = "Setup incomplete. Click Enable to finish setting this up."
            elif self._enabled:
                text = "On."
            else:
                text = "Off."
        except FileNotFoundError:
            text = "Off."
        except (OSError, ValueError, TypeError) as exc:
            text = f"Could not read recovery settings: {exc}"
        self._set_tooltip(settings)
        self.button.setText(
            "Disable automatic Phy closing" if self._enabled else "Enable automatic Phy closing"
        )
        self.status.setText(text + (" " + self._notice if self._notice else ""))

    def _set_tooltip(self, settings: dict) -> None:
        def clock(hour: int) -> str:
            return f"{hour % 12 or 12} {'a.m.' if hour < 12 else 'p.m.'}"

        hours = (
            "At any time of day" if settings["start_hour"] == settings["end_hour"] else
            f"Only from {clock(settings['start_hour'])} to {clock(settings['end_hour'])} (this computer's time)"
        )
        conditions = (
            f"{hours}, after sorting has waited at least {settings['wait_seconds'] / 60:g} minutes "
            f"for the GPU and Phy has been unused for at least {settings['idle_seconds'] / 60:g} minutes."
        )
        tooltip = (
            f"<qt><p>{escape(self._description)}</p><p>{escape(conditions)}</p>"
            "<p>If saving or checking your edits fails, Phy stays open.</p>"
            "<p>This setting is remembered for your Windows account and also works with Phy "
            "opened from another terminal. After first enabling, restart any Phy windows "
            "already open. New sorting runs use this automatically.</p></qt>"
        )
        self.button.setToolTip(tooltip)
        self.status.setToolTip(tooltip)

    def _toggle(self) -> None:
        enabling = not self._enabled
        try:
            if enabling:
                install_watchdog(self.config_dir, enabled=True)
                self._notice = "Restart open Phy windows."
                message = "Automatic overnight Phy closing enabled. " + self._notice
            else:
                disable_watchdog(self.config_dir)
                self._notice = ""
                message = "Automatic overnight Phy closing disabled."
        except Exception as exc:
            self._notice = f"Could not {'enable' if enabling else 'disable'} recovery: {exc}"
            message = self._notice
        self.refresh()
        self.status_message.emit(message)
