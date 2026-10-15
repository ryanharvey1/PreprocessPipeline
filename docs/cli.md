# CLI execution guide

Run commands from the repository root in the activated environment described in
[README](../README.md#installation). Commands below use the same Python modules
on Linux and Windows. See [Parameter reference](parameters.md) for JSON keys,
defaults, units, and channel numbering.

## Choose an execution route

| Route | Configuration | Behavior |
| --- | --- | --- |
| `python -m src.preprocess.gui.run_pipeline` | GUI pipeline JSON | Direct foreground preprocessing/sorting/postprocessing. Useful inside a terminal or your own batch job. |
| `preprocess-run-controller` | An existing persistent Run directory | Submit, reconcile, cancel, or retry separate Local/Slurm Stages. |
| `python -m src.preprocess.sorter_runner` | Sorter YAML/JSON plus command options | Sort an existing binary without running session preprocessing or postprocessing. |

The installed controller command is also available as
`python -m src.execution.controller`. `preprocess-stage-worker` is launched by
the controller and is not the normal way to start a session.

## Recover an overnight GPU wait caused by Phy (Windows)

An optional cooperative Phy plugin can save and close an idle Phy session when
a persistent local Sorting Stage is waiting for the GPU. It works when Phy was
launched from another terminal; both processes must use the same Windows user
and default `~/.phy/` configuration directory. It uses no model/API or separate
scheduled agent. Direct foreground sorter routes do not publish recovery signals.

In the GUI, open **Ephys > Manual Curation** and click **Enable automatic Phy
closing**. The button installs and enables the plugin in one step. It then
becomes **Disable automatic Phy closing**; clicking it switches recovery off
without removing your plugins. Hover over the button or its on/off status for an
explanation and the saved hours and wait/idle durations, even while disabled.
This is a setting for your Windows user and persists across GUI launches and
session configurations. Opening the GUI alone does not install
or enable recovery. Restart any already-open Phy sessions after first enabling;
new pipeline runs from the updated GUI publish recovery signals automatically.

Alternatively, from this checkout, using a Python environment on your PATH:

```powershell
python scripts/install_phy_watchdog.py
```

Installation preserves existing plugins, backs up `~/.phy/phy_config.py`, and
starts **disabled**. Enable it explicitly when ready:

```powershell
python scripts/install_phy_watchdog.py --enable
```

After CLI installation, restart Phy and the pipeline GUI/controller. Already open
Phy sessions cannot load the plugin retroactively, and already running workers
cannot pick up the new publisher code. Subsequent Phy launches using the default configuration
load it automatically, including ordinary `phy template-gui params.py` commands.
Re-run the installer after updating the watchdog source to refresh its installed
copy.

Settings are in `~/.phy/pipeline-watchdog/settings.json`:

```json
{
  "enabled": true,
  "start_hour": 22,
  "end_hour": 8,
  "wait_seconds": 900,
  "idle_seconds": 1800
}
```

Hours use the computer's local clock; the default window is 10 p.m. through
8 a.m., ending at 8 a.m. Equal start/end hours allow any time of day. Wait and
idle durations must each be at least 60 seconds. Changes take effect without
restarting a loaded plugin. Disable closing with the GUI's **Disable automatic
Phy closing** button, or:

```powershell
python scripts/install_phy_watchdog.py --disable
```

The plugin only acts when a fresh same-host GPU-wait heartbeat names its own
process PID on a busy GPU, the waiting worker is still alive with the same process
creation time, the wait has lasted at least 15 minutes, Phy has had
no input or curation changes for 30 minutes, and it has no busy/pending action or
modal/popup dialog. GPU utilization alone does not establish idleness. Windows
may omit process details; if Phy's exact PID is absent, recovery does not act.
Slurm workers do not publish signals.

Before closing, it copies `spike_clusters.npy` and `cluster_*.tsv` into
`<Phy dataset>/.phy/pipeline-watchdog-backups/<timestamp-id>/`, emits Phy's normal
Save event (including plugin save callbacks), and compares saved assignments and
labels with the live supervisor. Save, backup, or verification failures leave
Phy open. It makes at most one recovery attempt per idle period, never kills a
process, and never chooses an answer in a save dialog. It rechecks activity and
the wait after saving. Other programs may continue holding the GPU; the sorting
worker continues its normal polling and starts only if its existing GPU gate
permits it. The GPU admission threshold is unchanged.

Each backup contains only the saved spike assignments and cluster tables, not
the recording, templates, or waveforms. Its size is the combined size of
`spike_clusters.npy` and `cluster_*.tsv`; for example, 10 million spikes stored as
32-bit cluster IDs take about 40 MB plus the tables. Backups accumulate without
automatic pruning and are included when copying the entire session. They live
inside Phy's cache directory, so clearing `.phy` or rerunning Kilosort into the
same output directory can remove them. They are recovery copies rather than
permanent archival backups. Sorting and postprocessing read the normal output
files and ignore the nested backup folder.

Review morning results in `~/.phy/pipeline-watchdog/events.jsonl`. Each event
includes the dataset, Phy PID, originating GPU log, and/or backup/error details.
The Sorting Attempt's `gpu-selection.jsonl` records whether the GPU subsequently
became available. Heartbeats expire after 90 seconds and are removed on selection
or normal worker failure. Dead workers and reused worker PIDs are rejected even
before their heartbeats expire. A cancellation or failure racing the final check
may still result in a verified save and close, without starting a sorter early.

Validation with installed Phy, using disposable data and a hidden Qt window:

```powershell
# Run with the Python environment used by Phy (PyQt5), not the pipeline's PySide6.
python scripts/check_phy_watchdog.py
```

This checks plugin discovery, a real Phy Supervisor merge/label save, the
BaseController save handler, backups, and Qt closing. It does not exercise a full
TemplateGUI on a real recording or prove that closing releases GPU memory on a
particular Windows driver.

## Direct single-day run

The easiest starting point is a JSON file saved through the GUI's **Save config**.
Alternatively, save this example as `session.json`, replacing the data/XML/output
paths and checking the channel settings:

```json
{
  "basepath": "/data/session01",
  "local_root": "/scratch/preprocess",
  "xml_path": "/data/session01/session01.xml",
  "preprocess": {
    "run_sorter": true,
    "sorter": "Kilosort",
    "sorter_path": "sorter/KiloSort1",
    "sorter_config_path": "sorter/Kilosort1_config.yaml",
    "reject_channels": [],
    "digital_inputs": true,
    "analog_inputs": false,
    "make_lfp": true,
    "state_score": false,
    "preprocess_worker_count": 4,
    "sorter_worker_count": 4,
    "overwrite": false
  },
  "postprocess": {
    "worker_count": 4,
    "overwrite": false
  }
}
```

This example disables optional state scoring and uses four requested workers.
Omitted fields retain GUI JSON defaults. Supply an acquisition-matching XML
with channel groups; preprocessing prepares the channel map from it. For Windows,
paths can be written as `"D:/data/session01"` and `"D:/scratch/preprocess"`.

```bash
python -m src.preprocess.gui.run_pipeline --config session.json --mode all
```

The direct command accepts just two required options:

| Option | Meaning |
| --- | --- |
| `--config` | Path to a GUI pipeline JSON file. |
| `--mode` | `all`, `preprocess`, `postprocess`, or `noise_label`. |

| Mode | Direct execution |
| --- | --- |
| `all` | Preprocess, sort if enabled, then attempt postprocessing. If sorting is disabled, an existing compatible sorting is needed for the postprocess step. |
| `preprocess` | Preprocess and also sort if `preprocess.run_sorter=true`. |
| `postprocess` | Process existing sorting and its matching recording. |
| `noise_label` | Update noise labels from existing quality metrics without full curation. |

To produce preprocessing outputs without sorting, set `preprocess.run_sorter`
to `false` and run:

```bash
python -m src.preprocess.gui.run_pipeline --config session.json --mode preprocess
```

Success exits with code 0 and prints a final JSON line prefixed by
`__PREPROCESS_GUI_RESULT__`. A processing exception exits with code 1 and prints
`__PREPROCESS_GUI_ERROR__` plus a traceback. Output includes
`<local_root>/<basename>/preprocess.log`; compatible completed files are reused
when overwrite is false. The command does not perform the final storage copy.

## Direct multi-day run

Add these top-level keys to the single-day JSON:

```json
{
  "multi_day_enabled": true,
  "multi_day_session_paths": ["/data/Day01", "/data/Day02"],
  "multi_day_selected_subepoch_paths": [],
  "multi_day_name": "animal_days01_02"
}
```

The snippet is a configuration fragment, not a separate complete input file.
Keep the `local_root`, XML, preprocessing, and postprocessing settings in the
same file. Use at least two day directories. Empty selected-subepoch paths
include all discovered subepochs; otherwise provide full source subepoch paths.
Review session/subepoch order and channel identity before concatenating.

```bash
python -m src.preprocess.gui.run_pipeline --config multiday.json --mode all
```

Mixed Intan/Open Ephys/WILD inputs still need compatible recording rates and
ephys channel layouts. ADC channels are not extra ephys channels to add to the
channel map. The multi-day staging output includes a manifest and selected
subepochs CSV so that the merged sample order can be inspected.

## Postprocess an external sorting

Save, for example, `postprocess.json`:

```json
{
  "basepath": "/data/session01",
  "local_root": "/scratch/preprocess",
  "xml_path": "/data/session01/session01.xml",
  "chanmap_path": "/scratch/preprocess/session01/chanMap.mat",
  "preprocess": {"reject_channels": [3]},
  "postprocess": {
    "sorting_phy_folder": "/scratch/preprocess/session01/Kilosort_external",
    "dat_path": "/scratch/preprocess/session01/session01.dat",
    "apply_preprocess": false,
    "exclude_cluster_groups": ["noise"],
    "worker_count": 4,
    "overwrite": false
  }
}
```

```bash
python -m src.preprocess.gui.run_pipeline --config postprocess.json --mode postprocess
```

Use the exact recording that underlies the sorting: channel columns, sampling
rate, sample order, and time origin must agree. Matching `params.py` metadata
also supplies dtype/byte offset. Enable `apply_preprocess` only for a raw input
that needs the configured filtering/reference. Full binary channel IDs remain
consistent through preprocessing, sorting, Phy, and CellExplorer metadata.

To revise the noise thresholds after metrics have been generated, edit the
`postprocess.noise_thresholds` object and run:

```bash
python -m src.preprocess.gui.run_pipeline --config postprocess.json --mode noise_label
```

This operation changes cluster-group labels. It requires the existing metrics
for the resolved postprocess output; it does not compute missing metrics.

## Persistent Local and Slurm Runs

Stage workers use Matplotlib's `Agg` backend to save diagnostic figures without
a display server. This overrides an inherited interactive backend such as
`QtAgg`, so event export and other saved plots can run on Slurm compute nodes.

Persistent execution separates preprocessing, sorting, and postprocessing into
Stages with saved analysis/configuration snapshots and per-attempt logs. The GUI
can create these Runs. To create one without opening the GUI, first add an
`execution` object to the pipeline JSON, for example:

```json
{
  "execution": {
    "requested_backend": "local",
    "workspace": "/scratch/preprocess",
    "preprocess": {"cpus": 4, "memory_mb": 32768, "gpu_count": 0},
    "sorting": {"cpus": 4, "memory_mb": 65536, "gpu_count": 1},
    "postprocess": {"cpus": 4, "memory_mb": 32768, "gpu_count": 0}
  }
}
```

Choose resource values for the actual job; the example is not a required
allocation. For Slurm, use `"requested_backend": "slurm"` and the site's
partition/account/GPU settings. All worker nodes must see the same paths and
environment. Auto backend selection can fall back to local; explicit Slurm
reports unavailable capabilities instead.

Save this small creation script as `create_run.py` in the repository root:

```python
from pathlib import Path

from src.execution.controller import create_run, resolve_backend
from src.execution.models import RequestedBackend
from src.preprocess.gui.config_model import PipelineGuiSettings

settings = PipelineGuiSettings.load(Path("session.json"))
resolved, capabilities = resolve_backend(
    RequestedBackend(settings.execution.requested_backend.lower()),
    require_sacct=settings.execution.require_sacct,
)
execution = settings.execution.to_execution_config(resolved_backend=resolved)
run_dir = create_run(
    settings=settings,
    execution=execution,
    mode="all",
    capabilities=capabilities,
)
print(run_dir)
```

```bash
python create_run.py
preprocess-run-controller --run-dir /scratch/preprocess/.pipeline/REPLACE_WITH_RUN_ID
```

Use the Run directory printed by the script. Run creation saves intent and claims
the session; the controller command submits it. No `--config` creation flag exists
on the controller. Creation supports `all`, `preprocess`, or `postprocess`.
In persistent `all` mode, disabling sorting also omits the postprocess Stage;
create a `postprocess` Run explicitly to work on an existing sorting.

### Monitor, cancel, and retry

```bash
preprocess-run-controller --run-dir /scratch/preprocess/.pipeline/REPLACE_WITH_RUN_ID --reconcile
preprocess-run-controller --run-dir /scratch/preprocess/.pipeline/REPLACE_WITH_RUN_ID --cancel
preprocess-run-controller --run-dir /scratch/preprocess/.pipeline/REPLACE_WITH_RUN_ID --cancel-stage sorting
preprocess-run-controller --run-dir /scratch/preprocess/.pipeline/REPLACE_WITH_RUN_ID --retry sorting --cpus 8 --memory-mb 65536
```

These are alternative actions, not commands to run in sequence. Reconcile updates
`state.json`; read it and `controller.log` for status, because controller output
is redirected to that log. Stage attempts contain `stdout.log` and `stderr.log`.
`analysis_config.json` and `execution_config.json` record the saved settings.

Retry is allowed for failed, cancelled, or blocked enabled Stages. It preserves
the saved analysis and can update resource allocation. For changes to channels,
filters, sorter algorithms, or other analysis settings, create a new Run. Closing
the GUI does not cancel a persistent Run; cancellation is an explicit request.

| Controller option | Meaning |
| --- | --- |
| `--run-dir` | Required existing Run directory. |
| No action flag | Submit the Run. |
| `--reconcile` | Refresh state from attempts/backend status. |
| `--cancel` | Request cancellation of the Run. |
| `--cancel-stage` | Request cancellation of `preprocess`, `sorting`, or `postprocess`. |
| `--retry` | Retry an enabled `preprocess`, `sorting`, or `postprocess` Stage. |
| `--cpus`, `--memory-mb` | Retry allocation overrides. |
| `--walltime-minutes` | Retry time-limit override in minutes. |
| `--unlimited-walltime` | Remove the explicit retry time limit; scheduler policy still applies. Mutually exclusive with `--walltime-minutes`. |
| `--gpu-gres-type`, `--gpu-constraint` | Retry GPU placement overrides. |
| `--partition`, `--account`, `--qos`, `--reservation` | Retry Slurm scheduling overrides. |

Only one action flag may be selected. Resource overrides are applied by `--retry`.

## Sort an existing binary

The standalone sorter command uses a sorter config file, not the pipeline JSON:

```bash
python -m src.preprocess.sorter_runner --sorter kilosort4 --config sorter/Kilosort4_config.yaml --dat-path /scratch/preprocess/session01/session01.dat --xml-path /scratch/preprocess/session01/session01.xml --chanmap /scratch/preprocess/session01/chanMap.mat --kilosort4-path sorter/Kilosort4 --output-folder /scratch/preprocess/session01/Kilosort4_manual
```

For Kilosort1, use `--sorter kilosort`, the Kilosort1 config, and
`--kilosort1-path sorter/KiloSort1`. Keep that repository directory's capitalization
on Linux. Kilosort2.5 uses `--sorter kilosort2.5` and
`--kilosort2-5-path sorter/Kilosort2.5`. MATLAB-based versions also need MATLAB.

MATLAB launch scripts, process-pool job files, and temporary logs use a private
directory created for each worker under the system temporary directory (including
`TMPDIR` when configured). They do not share a fixed `/tmp/preprocess_pipeline`
directory across users. The MATLAB log is copied into the sorter output directory
as `matlab_run.log`.

To choose the runtime location explicitly, set `PREPROCESS_RUNTIME_DIR` to a
directory you own that is writable on the compute node before submitting or
retrying the Run. For example:

```bash
export PREPROCESS_RUNTIME_DIR=/scratch/myuser/preprocess-runtime
mkdir -p -m 700 "$PREPROCESS_RUNTIME_DIR"
SBATCH_EXPORT=ALL preprocess-run-controller --run-dir /scratch/preprocess/.pipeline/REPLACE_WITH_RUN_ID --retry sorting
```

`SBATCH_EXPORT=ALL` passes the environment override to Slurm workers. Retrying
sorting reuses completed preprocessing and schedules a new postprocessing attempt.

| Option | Default | Meaning |
| --- | --- | --- |
| `--sorter` | `kilosort` | Sorter name: `kilosort`, `kilosort2.5`, or `kilosort4`. |
| `--config` | Sorter-specific file | Sorter YAML/YML/JSON algorithm configuration. |
| `--dat-path` | Required | Input binary path. This command does not perform session preprocessing. |
| `--xml-path` | Required at execution | Session XML. Although argparse marks it optional, the implementation requires it. |
| `--chanmap` | Unspecified | Probe geometry/connected-channel map. Supply the session map to retain channel exclusions and geometry. |
| `--sampling-frequency` | From XML | Recording rate, Hz. |
| `--num-channels` | From XML | Full binary channel count, including retained bad columns. |
| `--active-channels` | Derived | Comma-separated original 0-based channel IDs to sort. |
| `--exclude-channels` | Unspecified | Comma-separated original 0-based channel IDs to exclude. |
| `--dtype` | `int16` | Binary sample dtype. |
| `--gain-to-uV` | `0.195` | Conversion gain to µV per stored sample unit. |
| `--offset-to-uV` | `0.0` | Conversion offset in µV. |
| `--output-folder` | Required | Sorter output directory. |
| `--remove-existing-folder` | Off | Delete an existing sorter output folder before running. |
| `--kilosort1-path` | `sorter/KiloSort1` | Kilosort1 installation, matching the checked-in directory's capitalization. |
| `--kilosort2-5-path` | `sorter/Kilosort2.5` | Kilosort2.5 installation. |
| `--kilosort4-path` | `sorter/Kilosort4` | Kilosort4 repository containing the Python package. |
| `--matlab-path` | Discovery | MATLAB executable or bin directory. |
| `--matlab-max-workers` | Automatic | MATLAB process-pool worker cap. |
| `--docker-image` | Unspecified | Optional container image forwarded to the sorter runner. |
| `--sorter-verbose` | Off | Enable verbose sorter logs. |
| `--keep-temp-wh-dat` | Off | Keep the MATLAB sorter's `temp_wh.dat` instead of cleaning it up. |

Subset sorting keeps exported Phy channel metadata in the full binary's original
channel numbering. An empty set of eligible active channels is reported as
`no_active_channels` and sorting is skipped.

## Physical channel maps and existing recordings

A selected `chanMap.mat` supplies the physical geometry for preprocessing,
sorting, and postprocessing. Its channel IDs, x/y/z coordinates, shank and probe
IDs, face IDs, and custom metadata are preserved. GUI and XML exclusions only
disable entries in `connected`; loading a map does not rebuild its geometry from
the probe-template controls.

Without a selected map, preprocessing first uses the raw session's map. Explicit
probe-template assignments with an empty map path request generation; the GUI's
Generate chanMap action also requests generation. Editing geometry clears the
selected map, while editing bad channels retains it. Preview and local-CMR
preflight use the same map that the worker will use.

Persistent Runs save an immutable copy at `snapshots/chanMap.input.mat` and
include its checksum in preprocessing fingerprints. New preprocessing results
record the geometry used to produce the recording. Sorting and postprocessing
refuse an older result without that evidence or a map changed after preprocessing.

Runs made before this fix may have replaced a custom map with default geometry.
If a processed map differs from its selected source, changing only `chanMap.mat`
cannot repair local CMR or artifact detection that already used the wrong
geometry. Use a new local output root and rerun from raw data with the original
map selected. Alternatively, explicit overwrite archives the old preprocessing
products before producing replacements; existing sorter folders are retained.

The pipeline rejects incompatible cached maps when overwrite is disabled. It
does not silently repair a map and reuse the associated processed recording.

## Behavior and CellExplorer

The ephys CLI and persistent Stage list do not export tracking behavior or launch
MATLAB CellExplorer processing. Use the GUI's Behavior calibration/export actions
and CellExplorer launch action for those steps. Camera acquisition settings in
the pipeline JSON can still control the exported synchronization inputs.

Behavior export produces `<basename>.animal.behavior.mat`. Postprocessing produces
Phy-compatible sorting/metrics; CellExplorer processing is a separate action.
