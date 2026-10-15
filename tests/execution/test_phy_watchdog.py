from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from scripts.install_phy_watchdog import install
from src import phy_watchdog as watchdog
from src.execution.gpu_selection import GpuDevice, GpuProcess, activate_least_used_gpu


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 10, 15, 3).timestamp()
        self.settings = {**watchdog.DEFAULT_SETTINGS, "enabled": True}
        token_patch = patch.object(watchdog, "process_token", return_value="test-worker-token")
        token_patch.start()
        self.addCleanup(token_patch.stop)
        watchdog._write_json(self.root / "settings.json", self.settings)
        self.wait = {
            "schema": watchdog.SCHEMA, "hostname": socket.gethostname(),
            "updated_at": self.now, "wait_seconds": 1000,
            "gpu_pids": [os.getpid()], "log_path": "sorting/gpu-selection.jsonl",
            "worker_pid": os.getpid(), "worker_token": "test-worker-token",
        }

    def publish(self, **changes):
        watchdog._write_json(self.root / "gpu-waits" / "wait.json", {**self.wait, **changes})

    def matches(self, **changes):
        return watchdog.eligible_waits(
            self.root, {**self.settings, **changes}, pid=os.getpid(), now=self.now,
        )

    def test_only_fresh_local_matching_gpu_waits_qualify(self):
        self.publish()
        self.assertEqual(len(self.matches()), 1)
        for changes in (
            {"updated_at": self.now - 91}, {"updated_at": self.now + 1},
            {"wait_seconds": 899}, {"hostname": "other-host"},
            {"gpu_pids": [os.getpid() + 1]}, {"schema": "unknown"},
            {"wait_seconds": float("nan")}, {"updated_at": float("inf")},
            {"worker_token": "reused-pid-token"}, {"worker_token": None},
        ):
            with self.subTest(changes=changes):
                self.publish(**changes)
                self.assertEqual(self.matches(), [])

    def test_dead_or_unqueryable_worker_cannot_trigger_recovery(self):
        self.publish()
        with patch.object(watchdog, "process_token", return_value=None):
            self.assertEqual(self.matches(), [])

    def test_backup_failure_or_disappearing_wait_prevents_close(self):
        session = self.session()
        with patch.object(watchdog, "backup_curation", side_effect=OSError("backup failed")):
            self.run_tick(session, lambda *args: self.fail("must not save without a backup"))
        self.assertFalse(self.closed)
        session.activity()
        session.last_activity = time.monotonic() - 2000
        with patch.dict(sys.modules, {"phy": SimpleNamespace(emit=self.save(session))}), \
             patch.object(session, "_waits", side_effect=[[self.wait], []]):
            session.tick()
        self.assertFalse(self.closed)
        self.assertIn("saved_kept_open", (self.root / "events.jsonl").read_text())

    def test_disabled_daytime_and_custom_window(self):
        self.publish()
        self.assertEqual(self.matches(enabled=False), [])
        self.now = datetime(2026, 10, 15, 12).timestamp()
        self.publish(updated_at=self.now)
        self.assertEqual(self.matches(), [])
        self.assertEqual(len(self.matches(start_hour=9, end_hour=17)), 1)

    def test_invalid_settings_fail_closed(self):
        for changes in ({"enabled": "yes"}, {"idle_seconds": -1}, {"start_hour": 24},
                        {"wait_seconds": float("nan")}):
            with self.subTest(changes=changes):
                watchdog._write_json(self.root / "settings.json", {**self.settings, **changes})
                with self.assertRaises(ValueError):
                    watchdog.read_settings(self.root)

    def test_publisher_clears_selected_exception_and_disabled_waits(self):
        event = {"status": "waiting", "busy_percent": 10, "gpus": [
            {"memory_percent": 30, "processes": [{"pid": os.getpid()}]},
            {"memory_percent": 2, "processes": [{"pid": 123}]},
        ]}
        with patch.dict(os.environ, {"SLURM_JOB_ID": ""}):
            with watchdog.GpuWaitPublisher(self.root / "gpu.jsonl", root=self.root) as publisher:
                publisher.update(event)
                self.assertEqual(json.loads(publisher.path.read_text())["gpu_pids"], [os.getpid()])
                publisher.update({**event, "status": "selected"})
                self.assertFalse(publisher.path.exists())
                publisher.update(event)
                watchdog._write_json(self.root / "settings.json", {**self.settings, "enabled": False})
                publisher.update(event)
                self.assertFalse(publisher.path.exists())
            watchdog._write_json(self.root / "settings.json", self.settings)
            with self.assertRaises(RuntimeError):
                with watchdog.GpuWaitPublisher(self.root / "gpu.jsonl", root=self.root) as publisher:
                    publisher.update(event)
                    raise RuntimeError("worker failed")
            self.assertFalse(publisher.path.exists())
        with patch.dict(os.environ, {"SLURM_JOB_ID": "123"}):
            with watchdog.GpuWaitPublisher(self.root / "gpu.jsonl", root=self.root) as publisher:
                publisher.update(event)
                self.assertFalse(publisher.path.exists())

    def test_gpu_gate_publishes_then_cleans_up_without_importing_phy(self):
        busy = GpuDevice(0, "GPU-test", "test", 30, 100, 30, 0,
                         (GpuProcess(os.getpid(), "test", "python.exe", None),))
        free = GpuDevice(0, "GPU-test", "test", 2, 100, 2, 0, ())
        snapshots = iter([[busy], [free]])
        def sleeper(_seconds):
            heartbeats = list((self.root / "gpu-waits").glob("*.json"))
            self.assertEqual(len(heartbeats), 1)
        phy_was_loaded = "phy" in sys.modules
        with patch.object(watchdog, "watchdog_root", return_value=self.root), \
             patch.dict(os.environ, {"SLURM_JOB_ID": ""}):
            result = activate_least_used_gpu(log_path=self.root / "gpu.jsonl",
                                            query=lambda: next(snapshots), sleeper=sleeper)
        self.assertEqual(result["selected_gpu"]["uuid"], "GPU-test")
        self.assertEqual(list((self.root / "gpu-waits").glob("*.json")), [])
        self.assertEqual("phy" in sys.modules, phy_was_loaded)

    def session(self):
        dataset = self.root / "dataset"
        dataset.mkdir()
        np.save(dataset / "spike_clusters.npy", np.array([0, 0, 1]))
        (dataset / "cluster_group.tsv").write_text("cluster_id\tgroup\n0\tgood\n1\tnoise\n")
        (dataset / "cluster_info.tsv").write_text("cluster_id\tgroup\n0\tgood\n1\tnoise\n")
        self.dirty = True
        self.closed = False
        supervisor = SimpleNamespace(
            clustering=SimpleNamespace(spike_clusters=np.array([0, 1, 1])),
            cluster_meta=SimpleNamespace(fields=["group"]),
            get_labels=lambda field: {0: "good", 1: "noise"},
            is_dirty=lambda: self.dirty, _is_busy=False,
            task_logger=SimpleNamespace(has_finished=lambda: True),
        )
        def close():
            self.closed = True
            return True
        controller = SimpleNamespace(supervisor=supervisor, dir_path=dataset)
        gui = SimpleNamespace(close=close)
        app = SimpleNamespace(activeModalWidget=lambda: None, activePopupWidget=lambda: None)
        session = watchdog.RecoverySession(controller, gui, app, root=self.root)
        session.last_activity = time.monotonic() - 2000
        # Same-host GPU waits are synthetic; no live user process is touched.
        self.publish(updated_at=time.time())
        return session

    def run_tick(self, session, emit):
        with patch.dict(sys.modules, {"phy": SimpleNamespace(emit=emit)}), \
             patch.object(watchdog, "eligible_waits", return_value=[self.wait]):
            session.tick()

    def save(self, session):
        def emit(event, gui):
            self.assertEqual(event, "request_save")
            self.assertIs(gui, session.gui)
            np.save(session.controller.dir_path / "spike_clusters.npy",
                    session.controller.supervisor.clustering.spike_clusters)
            self.dirty = False
        return emit

    def test_save_verified_before_close_and_previous_curation_backed_up(self):
        session = self.session()
        self.run_tick(session, self.save(session))
        self.assertTrue(self.closed)
        backups = list((session.controller.dir_path / ".phy" / "pipeline-watchdog-backups").iterdir())
        np.testing.assert_array_equal(np.load(backups[0] / "spike_clusters.npy"), [0, 0, 1])
        events = [json.loads(line)["status"] for line in (self.root / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["saving", "saved_close_requested", "window_closed"])

    def test_save_failure_never_closes_and_does_not_retry_without_activity(self):
        session = self.session()
        def fail(*args):
            raise OSError("disk full")
        self.run_tick(session, fail)
        self.assertFalse(self.closed)
        self.run_tick(session, self.save(session))
        self.assertFalse(self.closed)
        self.assertIn("recovery_failed", (self.root / "events.jsonl").read_text())

    def test_false_clean_flag_with_unsaved_assignments_never_closes(self):
        session = self.session()
        def incomplete_save(*args):
            self.dirty = False
        self.run_tick(session, incomplete_save)
        self.assertFalse(self.closed)

    def test_unsaved_metadata_never_closes(self):
        session = self.session()
        session.controller.supervisor.get_labels = lambda field: {0: "mua", 1: "noise"}
        self.run_tick(session, self.save(session))
        self.assertFalse(self.closed)

    def test_activity_during_save_keeps_window_open(self):
        session = self.session()
        def save_with_input(*args):
            self.save(session)(*args)
            session.activity()
        self.run_tick(session, save_with_input)
        self.assertFalse(self.closed)
        self.assertIn("saved_kept_open", (self.root / "events.jsonl").read_text())

    def test_busy_modal_pending_or_recent_input_prevents_save(self):
        session = self.session()
        def unexpected_save(*args):
            self.fail("save must not be requested")
        for state in ("busy", "modal", "pending", "input"):
            with self.subTest(state=state):
                session.controller.supervisor._is_busy = state == "busy"
                session.app.activeModalWidget = lambda: object() if state == "modal" else None
                session.controller.supervisor.task_logger.has_finished = lambda: state != "pending"
                if state == "input":
                    session.activity()
                self.run_tick(session, unexpected_save)
                self.assertFalse(session.attempted)
                self.assertFalse(self.closed)

    def test_installer_preserves_plugins_and_is_idempotent_and_disabled_by_default(self):
        config_dir = self.root / "phy-config"
        config_dir.mkdir()
        config = config_dir / "phy_config.py"
        original = "c = get_config()\nc.TemplateGUI.plugins = ['Existing']\nc.Plugins.dirs = ['custom']\n"
        config.write_text(original)
        installed = install(config_dir)
        self.assertFalse(installed["enabled"])
        install(config_dir, enabled=True)
        self.assertEqual(config.read_text().count("# BEGIN PreprocessPipeline GPU watchdog"), 1)
        self.assertIn("['Existing']", config.read_text())
        backup = next(config_dir.glob("phy_config.py.watchdog-backup-*"))
        self.assertEqual(backup.read_text(), original)
        self.assertTrue(json.loads((config_dir / "pipeline-watchdog" / "settings.json").read_text())["enabled"])
        self.assertTrue(Path(installed["plugin"]).is_file())


if __name__ == "__main__":
    unittest.main()
