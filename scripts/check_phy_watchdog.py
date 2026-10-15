"""Disposable integration check. Run with the Python used by Phy (PyQt5).

Uses real Phy plugin loading, Supervisor, BaseController saving, and a hidden Qt
window. It never opens a real recording or changes the user's Phy configuration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import MethodType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    import numpy as np
    from phy import connect, emit, unconnect
    from phy.apps.base import BaseController
    from phy.cluster.supervisor import Supervisor, TaskLogger
    from phy.gui import GUI
    from phy.gui.qt import QApplication
    from phy.utils.config import load_master_config
    from phy.utils.plugin import attach_plugins
    from phylib.io.model import save_metadata

    from scripts.install_phy_watchdog import install

    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="phy-watchdog-check-") as temporary:
        root = Path(temporary)
        config_dir = root / "config"
        install(config_dir)
        settings = load_master_config(config_dir=config_dir)
        assert settings.TemplateGUI.plugins == ["PipelineGpuWatchdog"]
        assert str(config_dir / "plugins") in settings.Plugins.dirs

        dataset = root / "dataset"
        dataset.mkdir()
        original = np.array([0, 0, 1, 1], dtype=np.int64)
        np.save(dataset / "spike_clusters.npy", original)
        save_metadata(dataset / "cluster_group.tsv", "group", {0: "good", 1: "noise"})
        supervisor = Supervisor(spike_clusters=original.copy(), cluster_groups={0: "good", 1: "noise"})
        supervisor._is_busy = False
        supervisor.task_logger = TaskLogger(supervisor=supervisor)
        controller = SimpleNamespace(gui_name="TemplateGUI", dir_path=dataset, supervisor=supervisor)
        controller._save_cluster_info = MethodType(BaseController._save_cluster_info, controller)
        controller.model = SimpleNamespace(
            save_spike_clusters=lambda values: np.save(dataset / "spike_clusters.npy", values),
            save_metadata=lambda name, values: save_metadata(
                dataset / f"cluster_{name}.tsv", name,
                {cluster_id: value for cluster_id, value in values.items() if value is not None},
            ),
        )
        controller.attached_plugins = attach_plugins(controller, config_dir=config_dir)
        assert controller.attached_plugins == ["PipelineGpuWatchdog"]

        class Window(GUI):
            closed_after_save = False

            def closeEvent(self, event):
                assert not supervisor.is_dirty(), "Attempted to close before saving"
                self.closed_after_save = True
                super().closeEvent(event)

        gui = Window(name="WatchdogCheck", config_dir=config_dir, view_creator={}, default_views=[])

        @connect(sender=gui)
        def on_request_save(sender):
            supervisor.save()

        def on_save_clustering(sender, spike_clusters, groups, *labels):
            BaseController.on_save_clustering(controller, sender, spike_clusters, groups, *labels)

        connect(on_save_clustering, sender=supervisor)
        emit("gui_ready", controller, gui)
        session, input_filter, timer, on_cluster = controller._pipeline_gpu_watchdog
        module = sys.modules["pipeline_gpu_watchdog"]
        session.root = config_dir / "pipeline-watchdog"
        module._write_json(session.root / "settings.json", {
            **module.DEFAULT_SETTINGS, "enabled": True, "start_hour": 0, "end_hour": 0,
        })

        # Real merge and label edits, not a manually set dirty flag.
        supervisor.merge([0, 1])
        cluster_id = int(supervisor.clustering.cluster_ids[0])
        supervisor.label("group", "mua", cluster_ids=[cluster_id])
        assert supervisor.is_dirty()
        session.last_activity = time.monotonic() - 2000
        module._write_json(session.root / "gpu-waits" / "synthetic.json", {
            "schema": module.SCHEMA, "hostname": module.socket.gethostname(),
            "updated_at": time.time(), "wait_seconds": 1000,
            "gpu_pids": [os.getpid()], "log_path": "synthetic-gpu-selection.jsonl",
            "worker_pid": os.getpid(), "worker_token": module.process_token(os.getpid()),
        })
        session.tick()
        assert gui.closed_after_save, (session.root / "events.jsonl").read_text()
        np.testing.assert_array_equal(np.load(dataset / "spike_clusters.npy"),
                                      supervisor.clustering.spike_clusters)
        assert "mua" in (dataset / "cluster_group.tsv").read_text()
        backups = list((dataset / ".phy" / "pipeline-watchdog-backups").iterdir())
        np.testing.assert_array_equal(np.load(backups[0] / "spike_clusters.npy"), original)
        events = [json.loads(line)["status"] for line in (session.root / "events.jsonl").read_text().splitlines()]
        assert events == ["saving", "saved_close_requested", "window_closed"], events
        timer.stop()
        app.removeEventFilter(input_filter)
        unconnect(on_request_save, on_save_clustering, on_cluster, controller, supervisor)
        gui.deleteLater()
        app.processEvents()
        print("PASS: installed Phy plugin loading, real merge/label save, verification, backup, Qt close")


if __name__ == "__main__":
    main()
