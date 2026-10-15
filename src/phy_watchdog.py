"""Cooperative GPU recovery. Safe to import in a worker before importing CUDA.

The installer also copies this self-contained module to Phy's plugin directory
as ``pipeline_gpu_watchdog.py``. Only that module name registers the Qt plugin.
"""
from __future__ import annotations

import csv
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import shutil
import socket
import time
import uuid


SCHEMA = "preprocess-phy-watchdog-v1"
HEARTBEAT_MAX_AGE = 90.0
DEFAULT_SETTINGS = {
    "enabled": False,
    "start_hour": 22,
    "end_hour": 8,
    "wait_seconds": 900,
    "idle_seconds": 1800,
}
CONFIG_BLOCK = '''
# BEGIN PreprocessPipeline GPU watchdog
# Loaded in every new Phy terminal session; closing is opt-in in settings.json.
c.TemplateGUI.plugins = list(c.TemplateGUI.get('plugins', [])) + ['PipelineGpuWatchdog']
_pipeline_plugin_dir = str(__import__('pathlib').Path(__file__).parent / 'plugins')
c.Plugins.dirs = list(c.Plugins.get('dirs', [])) + [_pipeline_plugin_dir]
# END PreprocessPipeline GPU watchdog
'''
logger = logging.getLogger("phy.pipeline_watchdog" if __name__ == "pipeline_gpu_watchdog" else __name__)


def watchdog_root() -> Path:
    return Path.home() / ".phy" / "pipeline-watchdog"


def process_token(pid: int) -> str | None:
    """Identify a live Windows process by creation time, including PID reuse.

    Query rights only: this never opens a process with termination rights. A
    denied query or an unsupported platform disables recovery for that wait.
    """
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
    if not handle:
        return None
    try:
        if kernel.WaitForSingleObject(handle, 0) != 0x102:  # WAIT_TIMEOUT means still running.
            return None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in (created, exited, system, user))):
            return None
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def read_settings(root: Path) -> dict:
    settings = {**DEFAULT_SETTINGS, **json.loads((root / "settings.json").read_text(encoding="utf-8"))}
    if type(settings["enabled"]) is not bool:
        raise ValueError("enabled must be true or false")
    for name in ("start_hour", "end_hour"):
        if type(settings[name]) is not int or not 0 <= settings[name] <= 23:
            raise ValueError(f"{name} must be an hour from 0 through 23")
    for name in ("wait_seconds", "idle_seconds"):
        value = settings[name]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 60:
            raise ValueError(f"{name} must be at least 60 seconds")
    return settings


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_watchdog(config_dir: Path | None = None, *, enabled: bool | None = None) -> dict:
    """Shared GUI/CLI installation; preserve existing Phy plugins and settings."""
    config_dir = (config_dir if config_dir is not None else watchdog_root().parent).expanduser().resolve()
    root = config_dir / "pipeline-watchdog"
    settings_path = root / "settings.json"
    settings = dict(DEFAULT_SETTINGS)
    if settings_path.exists():
        settings.update(read_settings(root))
    if enabled is not None:
        settings["enabled"] = enabled
    plugins = config_dir / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "phy_config.py"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else "c = get_config()\n"
    if "# BEGIN PreprocessPipeline GPU watchdog" not in existing:
        if config_path.exists():
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
            shutil.copy2(config_path, config_path.with_name(f"phy_config.py.watchdog-backup-{stamp}"))
        with config_path.open("w", encoding="utf-8") as stream:
            stream.write(existing.rstrip() + "\n" + CONFIG_BLOCK)
    target = plugins / "pipeline_gpu_watchdog.py"
    shutil.copy2(Path(__file__).resolve(), target)
    _write_json(settings_path, settings)
    return {"plugin": str(target), "settings": str(settings_path), "enabled": settings["enabled"]}


def disable_watchdog(config_dir: Path | None = None) -> None:
    """Stop automatic recovery without reinstalling or removing the plugin."""
    root = config_dir / "pipeline-watchdog" if config_dir is not None else watchdog_root()
    settings = read_settings(root)
    _write_json(root / "settings.json", {**settings, "enabled": False})


class GpuWaitPublisher:
    """Publish only current, opted-in local GPU waits; never affect sorting."""

    def __init__(self, log_path: Path, *, root: Path | None = None) -> None:
        self.root = root if root is not None else watchdog_root()
        self.path = self.root / "gpu-waits" / (uuid.uuid4().hex + ".json")
        self.log_path = str(log_path.resolve())
        self.started = time.monotonic()
        try:
            self.worker_token = process_token(os.getpid())
        except Exception:
            self.worker_token = None  # Optional recovery must never block GPU admission.
        self._warned = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.clear()

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            # A stale heartbeat expires even if cleanup is unavailable.
            logger.warning("Could not remove Phy watchdog heartbeat", exc_info=True)

    def update(self, event: dict) -> None:
        try:
            if (
                event["status"] != "waiting"
                or os.environ.get("SLURM_JOB_ID")
                or not read_settings(self.root)["enabled"]
                or self.worker_token is None
            ):
                self.clear()
                return
            _write_json(self.path, {
                "schema": SCHEMA,
                "hostname": socket.gethostname(),
                "worker_pid": os.getpid(),
                "worker_token": self.worker_token,
                "updated_at": time.time(),
                "wait_seconds": time.monotonic() - self.started,
                "log_path": self.log_path,
                "gpu_pids": sorted({
                    process["pid"]
                    for gpu in event["gpus"]
                    if gpu["memory_percent"] >= event["busy_percent"]
                    for process in gpu["processes"]
                }),
            })
        except FileNotFoundError:
            self.clear()  # Not installed/configured: the default is no recovery.
        except Exception as exc:
            self.clear()
            if not self._warned:
                print(f"[Phy watchdog] Recovery unavailable: {exc}", flush=True)
                self._warned = True


def eligible_waits(root: Path, settings: dict, *, pid: int, now: float) -> list[dict]:
    """Require a fresh same-host wait naming this Phy's exact GPU process PID."""
    if not settings["enabled"]:
        return []
    hour = datetime.fromtimestamp(now).hour
    start, end = settings["start_hour"], settings["end_hour"]
    overnight = start <= hour < end if start < end else hour >= start or hour < end
    if not overnight:
        return []
    matches = []
    for path in (root / "gpu-waits").glob("*.json"):
        try:
            wait = json.loads(path.read_text(encoding="utf-8"))
            age = now - float(wait["updated_at"])
            duration = float(wait["wait_seconds"])
            if (
                wait["schema"] == SCHEMA
                and wait["hostname"] == socket.gethostname()
                and math.isfinite(age) and 0 <= age <= HEARTBEAT_MAX_AGE
                and math.isfinite(duration) and duration >= settings["wait_seconds"]
                and pid in wait["gpu_pids"]
                and isinstance(wait["worker_token"], str)
                and process_token(wait["worker_pid"]) == wait["worker_token"]
            ):
                matches.append(wait)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return matches


def backup_curation(dataset: Path) -> Path:
    """Keep the last on-disk curation before asking Phy to save new edits."""
    destination = dataset / ".phy" / "pipeline-watchdog-backups" / (
        datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    destination.mkdir(parents=True, exist_ok=False)
    paths = {dataset / "spike_clusters.npy", *dataset.glob("cluster_*.tsv")}
    for path in sorted(paths):
        if path.is_file():
            shutil.copy2(path, destination / path.name)
    if not (destination / "spike_clusters.npy").is_file():
        raise RuntimeError("Cannot back up spike_clusters.npy; leaving Phy open")
    return destination


def verify_curation(controller) -> None:
    """Check primary clustering and label outputs against the live supervisor."""
    import numpy as np

    supervisor = controller.supervisor
    dataset = Path(controller.dir_path)
    expected = supervisor.clustering.spike_clusters
    saved = np.load(dataset / "spike_clusters.npy", mmap_mode="r", allow_pickle=False)
    try:
        if saved.shape != expected.shape:
            raise RuntimeError("Saved spike assignments have the wrong shape")
        for start in range(0, len(expected), 1_000_000):
            if not np.array_equal(saved[start:start + 1_000_000], expected[start:start + 1_000_000]):
                raise RuntimeError("Saved spike assignments differ from Phy's current edits")
    finally:
        del saved  # Release the Windows mmap before Phy exits.
    for field in supervisor.cluster_meta.fields:
        if field == "next_cluster":
            continue
        with (dataset / f"cluster_{field}.tsv").open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream, delimiter="\t")
            values = {int(row["cluster_id"]): row[field] for row in rows}
        expected_labels = {
            int(cluster_id): str(value)
            for cluster_id, value in supervisor.get_labels(field).items()
            if value is not None  # Phylib omits None metadata when writing TSV files.
        }
        if values != expected_labels:
            raise RuntimeError(f"Saved {field} labels differ from Phy's current edits")
    if not (dataset / "cluster_info.tsv").is_file() or supervisor.is_dirty():
        raise RuntimeError("Phy has not finished saving its curation")


class RecoverySession:
    """Runs on Phy's Qt thread, never in the sorting worker or another process."""

    def __init__(self, controller, gui, app, *, root: Path | None = None) -> None:
        self.controller, self.gui, self.app = controller, gui, app
        self.root = root if root is not None else watchdog_root()
        self.last_activity = time.monotonic()
        self.attempted = False

    def activity(self) -> None:
        self.last_activity = time.monotonic()
        self.attempted = False

    def _waits(self) -> list[dict]:
        settings = read_settings(self.root)
        if time.monotonic() - self.last_activity < settings["idle_seconds"]:
            return []
        return eligible_waits(self.root, settings, pid=os.getpid(), now=time.time())

    def _audit(self, status: str, **details) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        event = {
            "schema": SCHEMA, "observed_at": datetime.now().astimezone().isoformat(),
            "hostname": socket.gethostname(), "phy_pid": os.getpid(),
            "dataset": str(self.controller.dir_path), "status": status, **details,
        }
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        logger.info("[Phy watchdog] %s: %s", status, self.controller.dir_path)

    def _busy(self) -> bool:
        supervisor = self.controller.supervisor
        return (
            self.app.activeModalWidget() is not None
            or self.app.activePopupWidget() is not None
            or getattr(supervisor, "_is_busy", True)
            or not supervisor.task_logger.has_finished()
        )

    def tick(self) -> None:
        if self.attempted:
            return
        try:
            waits = self._waits()
            if not waits or self._busy():
                return
            self.attempted = True  # One attempt per idle period, including failures.
            self._audit("saving", gpu_logs=[wait["log_path"] for wait in waits])
            backup = backup_curation(Path(self.controller.dir_path))
            from phy import emit

            # This is Phy's normal File > Save route, including other plugins.
            emit("request_save", self.gui)
            verify_curation(self.controller)
            # Saving may process Qt events: respect activity or a cancelled wait.
            if not self._waits() or self._busy():
                self._audit("saved_kept_open", backup=str(backup))
                return
            self._audit("saved_close_requested", backup=str(backup))
            accepted = self.gui.close()  # Never terminate/kill or dismiss a dialog.
            try:
                self._audit("window_closed" if accepted else "close_refused", backup=str(backup))
            except OSError:
                logger.exception("[Phy watchdog] Could not record the close result")
        except FileNotFoundError as exc:
            if self.attempted:
                self._failed(exc)
        except Exception as exc:
            self.attempted = True
            self._failed(exc)

    def _failed(self, exc: Exception) -> None:
        logger.exception("[Phy watchdog] Recovery failed; leaving Phy open")
        try:
            self._audit("recovery_failed", error=f"{type(exc).__name__}: {exc}")
        except OSError:
            logger.exception("[Phy watchdog] Could not write recovery audit")


def register_plugin():
    """Delay all Phy/Qt imports; workers must not initialize CUDA via Phy."""
    from phy import IPlugin, connect
    from phy.gui.qt import QApplication, QEvent, QObject, QTimer

    class PipelineGpuWatchdog(IPlugin):
        def attach_to_controller(self, controller):
            @connect(sender=controller)
            def on_gui_ready(sender, gui):
                app = QApplication.instance()
                session = RecoverySession(controller, gui, app)

                class InputFilter(QObject):
                    def eventFilter(self, obj, event):
                        if event.spontaneous() and event.type() in (
                            QEvent.KeyPress, QEvent.MouseButtonPress,
                            QEvent.MouseButtonRelease, QEvent.MouseMove, QEvent.Wheel,
                            QEvent.TouchBegin, QEvent.TouchUpdate,
                        ):
                            session.activity()
                        return False

                input_filter = InputFilter(gui)
                app.installEventFilter(input_filter)
                timer = QTimer(gui)
                timer.setInterval(5000)
                timer.timeout.connect(session.tick)
                timer.start()

                @connect(sender=controller.supervisor)
                def on_cluster(sender, *args, **kwargs):
                    session.activity()

                # Keep Qt wrappers and event callbacks alive for this GUI.
                controller._pipeline_gpu_watchdog = (session, input_filter, timer, on_cluster)

    return PipelineGpuWatchdog


# Phy discovers the installer-created file by its basename, in any terminal.
if __name__ == "pipeline_gpu_watchdog":
    PipelineGpuWatchdog = register_plugin()
