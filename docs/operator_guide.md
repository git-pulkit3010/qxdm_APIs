# Operator Guide

This document explains how to install, configure, and run the QXDM
capture + QCAT text handoff on a Windows host.  The guide is written
for the operator who has access to a real QXDM / QCAT installation; the
*offline* path it describes works on any host with Python 3.10+.

> **Illustrative machine paths.**  The Windows paths used in this
> document are **illustrative**.  Replace every `D:\QXDM\…` path with
> the value of the operator's `paths.*` configuration block.

## 1. Installation

### 1.1.  Python and the package

The package targets CPython 3.10 or later.

```powershell
PS> python --version
Python 3.12.10
PS> pip install -e .
```

The optional `vendor` extra installs the Windows-only libraries the
*real* QXDM / QCAT bindings may need (`pywin32`, `psutil`); install it
only after the documented vendor binding is wired in.

```powershell
PS> pip install -e .[vendor]
```

### 1.2.  Working directory

The repository layout is:

```
qxdm_automation_backend/
├── src/
│   └── qxdm_automation/         # importable Python package
├── tests/                       # pytest suite
├── docs/                        # evidence / operator / commissioning
├── examples/                    # ready-to-use configuration samples
│   ├── offline_sim.json         # offline simulation config
│   ├── planning_only.json       # planning-only config
│   └── sample.dmc               # synthetic DMC used in tests
├── logs/                        # operator-supplied; the harness writes here
├── pyproject.toml
└── README.md
```

## 2. Configuration

The configuration is a single JSON document.  See
`examples/offline_sim.json` for a working sample.  The schema is
implemented by `qxdm_automation.config` and validated at load time.

| Key | Type | Required | Notes |
|---|---|---|---|
| `mode` | `offline \| planning \| live` | yes | Never enters `live` by default. |
| `run_id` | string | yes | Single filename component, no path separators. |
| `run_category` | string | yes | Free-form category used by consumers. |
| `tool.qxdm_path` | path | live mode | Absolute path to the QXDM executable. |
| `tool.qxdm_binding` | string | live mode | Logical binding name. |
| `tool.qcat_path` | path | live mode | Absolute path to the QCAT executable. |
| `tool.qcat_binding` | string | live mode | Logical binding name. |
| `tool.rotation_bytes` | int | yes | Hard cap ≤ 250,000,000. |
| `tool.qcat_template` | string list | live mode | Argument vector with placeholders. |
| `paths.capture_root` | path | yes | Where QXDM writes the binaries. |
| `paths.conversion_root` | path | yes | Staging area for QCAT. |
| `paths.publication_root` | path | yes | Final TXT handoff location. |
| `paths.run_workspace_root` | path | yes | Per-run lock and manifest. |
| `paths.filename_prefix` | string | yes | Single filename component. |
| `paths.binary_extensions` | list | no | Default `.dlf .bin .isf .hdf .qmdl`. |
| `timing.*` | seconds | yes | Deadlines for capture / stop / flush / convert. |
| `policy.attachment` | `owned \| attach` | no | Default `owned`. |
| `policy.retention` | `retain \| delete_on_success` | no | Default `retain`. |
| `policy.min_free_bytes` | int | no | Default 1 GiB. |
| `policy.stale_lock_seconds` | float | no | Default 3600. |
| `device_selector` | string | recommended | Explicit COM port / device. |
| `qxdm_dmc_path` | path | live mode | The DMC file the operator supplies. |
| `auto_discover` | bool | no | Default `true`. |

### 2.1.  QCAT argument template

`tool.qcat_template` is a list of argument tokens.  The orchestrator
does **not** invent placeholders; the operator must declare the ones
the adapter recognises.  The minimum documented placeholders are:

| Placeholder | Substituted with |
|---|---|
| `{qcat_exe}` | the `tool.qcat_path` value |
| `{input}` | absolute path of a single binary source |
| `{output}` | absolute path of the corresponding TXT |

A template with no placeholders is a configuration error: preflight
will block the run.

## 3. CLI

The `qxdm-automation` entry point exposes four sub-commands:

```powershell
PS> qxdm-automation --help
PS> qxdm-automation plan  -c <config.json>
PS> qxdm-automation run   -c <config.json> [--publication-contract run_folder|flat]
PS> qxdm-automation resume -c <config.json> [--publication-contract run_folder|flat]
PS> qxdm-automation inventory [--qxdm-path <path>] [--qcat-path <path>]
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Successful run / OK plan. |
| 1 | Run failed at capture / conversion / publication. |
| 2 | Configuration error. |
| 3 | Plan / preflight blocked the run. |
| 5 | Vendor capability is not implemented. |
| 6 | Generic orchestration error. |
| 7 | Unexpected error. |
| 130 | Cancellation. |

## 4. Run lifecycle (on-disk layout)

```
<run_workspace_root>/<run_id>/
├── run.lock                  # single-owner JSON lock
├── manifest.json             # full state, transitions, binaries, conversions
└── READY                     # only when the run completed

<capture_root>/<prefix>_NNNNNN.dlf   # raw binaries (retained by default)

<conversion_root>/<run_id>/
└── <prefix>_NNNNNN__seg-NNNN.txt    # QCAT staging outputs

<publication_root>/<run_id>/         # default "run_folder" contract
├── READY                              # consumer reads this first
├── manifest.json                      # a copy of the run manifest
└── <prefix>_NNNNNN__seg-NNNN.txt      # published TXT files
```

The flat publication contract writes files directly under
`<publication_root>/` with a `READY.<run_id>` marker and a
`published.json` accumulator.

## 5. Cancellation

Press **Ctrl-C** in the terminal running `qxdm-automation run`.  The
orchestrator initiates a bounded stop / finalise cleanup, records the
cancellation, and exits with code 130.  A cancelled run is **not** a
completed run and is not eligible for the consumer's normal handoff.

## 6. Retention

The default policy is `retain`: the raw binaries are kept under
`paths.capture_root`.  The `delete_on_success` policy removes them
*only* after the run folder has been published and the consumer's
`READY` marker is in place.

## 7. Conversion-only resume

When a run fails after capture finalisation (e.g. a transient QCAT
failure), the operator can re-run the conversion + publication step
without starting a new capture:

```powershell
PS> qxdm-automation resume -c <config.json>
```

The resume path will not launch QXDM, will not reconnect the device, and
will not start a second capture.  It refuses to silently skip a source
whose sha256 has changed since the original run.

## 8. GUI fallback (NOT implemented in this run)

The two PDFs explicitly mark every keyboard sequence as build-specific.
A GUI fallback is **not** part of this delivery.  When the time comes to
add one, the following inspection procedure must be followed **before**
any control ID is committed:

1. Launch QXDM manually on the operator's host.
2. Run `python -c "from pywinauto import Desktop; [print(w.window_text()) for w in Desktop(backend='win32').windows() if 'QXDM' in w.window_text().upper()]"`.
3. For each candidate QXDM window, run `w.print_control_identifiers()`.
4. Paste the output into the evidence/binding matrix; only then update
   `src/qxdm_automation/adapters/real_qxdm_gui.py`.

Do not invent control IDs.  Do not script blind `Alt+F` sequences
across builds.  Do not auto-respond "No" to save dialogs without
inspecting the dialog text.

## 9. Logs

The operator is responsible for the harness logs at
`logs/claude_stdout.log` and `logs/claude_stderr.log`.  The run itself
emits JSON to stdout (one document at exit) and writes per-run
operational state to `manifest.json`.  The `LOG_FILE` field in the
supplied workflow PDFs is **not** the same as the orchestrator's
manifest: the manifest is the durable, machine-readable record.

## 10. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Exit code 2 with "configuration error" | Invalid JSON or missing field. | Re-run `qxdm-automation plan -c …` to see the structured plan. |
| Exit code 3 with "qxdm binding is not implemented" | Real adapter not yet wired. | Confirm `mode=offline` or supply a real binding. |
| Exit code 5 with "vendor capability missing" | Operator-supplied binding matches no shipped adapter. | Check the binding name; consult the evidence/binding matrix. |
| Run is "BLOCKED" with "no diagnostic target" | `device_selector` is wrong or auto-discovery returned nothing. | Reconnect the device, re-run `plan` and inspect the `discovery` block. |
| Conversion fails with "source binary changed" | A previous run wrote the same name; the resume path refuses to overwrite. | Move the previous run to a different `run_id`; do not tamper with sources between runs. |
