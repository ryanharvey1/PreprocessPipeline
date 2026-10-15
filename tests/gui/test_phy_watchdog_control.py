from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QGroupBox

from src.phy_watchdog import _write_json, install_watchdog, read_settings
from src.preprocess.gui.phy_watchdog import PhyWatchdogControl


class PhyWatchdogControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_dir = Path(self.temporary.name) / "phy"
        platform = patch("src.preprocess.gui.phy_watchdog.os", SimpleNamespace(name="nt"))
        platform.start()
        self.addCleanup(platform.stop)
        self.widgets = []
        self.addCleanup(self.close_widgets)

    def close_widgets(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def control(self):
        control = PhyWatchdogControl(config_dir=self.config_dir)
        self.widgets.append(control)
        return control

    def test_button_installs_enables_disables_and_restores_saved_state(self):
        control = self.control()
        self.assertFalse(self.config_dir.exists(), "Opening the GUI must not install anything")
        self.assertEqual(control.button.text(), "Enable automatic Phy closing")
        messages = []
        control.status_message.connect(messages.append)
        control.button.click()
        self.assertTrue(read_settings(self.config_dir / "pipeline-watchdog")["enabled"])
        self.assertTrue((self.config_dir / "plugins" / "pipeline_gpu_watchdog.py").is_file())
        self.assertIn("Disable", control.button.text())
        self.assertIn("10 p.m.", control.button.toolTip())
        self.assertIn("15 minutes", control.button.toolTip())
        self.assertIn("30 minutes", control.button.toolTip())
        self.assertIn("Restart open Phy", control.status.text())
        reopened = self.control()
        self.assertIn("Disable", reopened.button.text())
        reopened.button.click()
        self.assertFalse(read_settings(self.config_dir / "pipeline-watchdog")["enabled"])
        control.refresh()
        self.assertIn("Enable", control.button.text())
        self.assertIn("enabled", messages[0])

    def test_enabling_preserves_custom_settings_and_existing_plugins(self):
        install_watchdog(self.config_dir)
        root = self.config_dir / "pipeline-watchdog"
        _write_json(root / "settings.json", {
            **read_settings(root), "start_hour": 23, "end_hour": 7,
            "wait_seconds": 1200, "idle_seconds": 3600,
        })
        config = self.config_dir / "phy_config.py"
        original = config.read_text(encoding="utf-8")
        control = self.control()
        control.button.click()
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        self.assertIn("11 p.m.", control.button.toolTip())
        self.assertIn("7 a.m.", control.button.toolTip())
        self.assertIn("20 minutes", control.button.toolTip())
        self.assertIn("60 minutes", control.button.toolTip())

    def test_failed_install_stays_off_and_reports_error(self):
        control = self.control()
        with patch("src.preprocess.gui.phy_watchdog.install_watchdog", side_effect=PermissionError("access denied")):
            control.button.click()
        self.assertIn("Enable", control.button.text())
        self.assertIn("Could not enable", control.status.text())
        self.assertIn("access denied", control.status.text())
        self.assertFalse(self.config_dir.exists())

    def test_incomplete_install_can_be_repaired(self):
        install_watchdog(self.config_dir, enabled=True)
        (self.config_dir / "plugins" / "pipeline_gpu_watchdog.py").unlink()
        control = self.control()
        self.assertIn("incomplete", control.status.text())
        self.assertIn("Enable", control.button.text())
        control.button.click()
        self.assertIn("Disable", control.button.text())

    def test_failed_disable_still_shows_enabled(self):
        install_watchdog(self.config_dir, enabled=True)
        control = self.control()
        with patch("src.preprocess.gui.phy_watchdog.disable_watchdog", side_effect=PermissionError("access denied")):
            control.button.click()
        self.assertTrue(read_settings(self.config_dir / "pipeline-watchdog")["enabled"])
        self.assertIn("Disable", control.button.text())
        self.assertIn("Could not disable", control.status.text())

    def test_main_window_contains_control_in_manual_curation(self):
        from src.preprocess.gui.app import MainWindow
        from src.preprocess.gui.config_model import PipelineGuiSettings

        with patch("src.preprocess.gui.phy_watchdog.watchdog_root", return_value=self.config_dir / "pipeline-watchdog"), \
             patch("src.preprocess.gui.app._load_default_settings", return_value=PipelineGuiSettings()):
            window = MainWindow()
        self.widgets.append(window)
        box = next(box for box in window.findChildren(QGroupBox) if box.title() == "Manual Curation")
        self.assertIs(box.findChild(PhyWatchdogControl), window.phy_watchdog_control)
        self.assertTrue(window.phy_watchdog_control.button.isEnabled())
        self.assertFalse(self.config_dir.exists())


if __name__ == "__main__":
    unittest.main()
