# Final Report — CompNova QXDM automation backend

## 1. Implemented behaviour and actual entry points

The repository implements the **offline** part of the QXDM capture +
QCAT text handoff end-to-end.  Every step the prompt calls for is
present as runnable code, and every step the prompt leaves as
*unresolved* is explicitly blocked at the adapter boundary.

| Prompt section | Implementation | Files |
|---|---|---|
| 1. Lifecycle state machine | `LifecycleState` enum + `Orchestrator` driver; states `CREATED → PREFLIGHT_OK → TOOL_READY → TARGET_READY → CONFIG_READY → CAPTURE_CONFIGURED → START_REQUESTED → CAPTURING → STOP_REQUESTED → FINALIZING → CAPTURE_FINALIZED → CONVERTING → PUBLISHING → COMPLETED` plus `BLOCKED / FAILED / INTERRUPTED / CANCELLED`. | `src/qxdm_automation/types.py`, `src/qxdm_automation/orchestration/__init__.py` |
| 2. Vendor adapter boundaries | `QxdmAdapter` / `QcatAdapter` / `QxdmSession` protocols; `UnboundQxdmAdapter` and `UnboundQcatAdapter` raise `UnsupportedCapabilityError` on every method. | `src/qxdm_automation/adapters/__init__.py` |
| 3. Configuration / preflight | `load_run_config`, `RunConfig`/`ToolConfig`/`PathsConfig`/`TimingConfig`/`PolicyConfig`; 250 MB hard cap; `plan()` performs inventory + path + free-space + filename + DMC checks without instantiating vendor objects. | `src/qxdm_automation/config/__init__.py`, `src/qxdm_automation/orchestration/__init__.py` |
| 4. Run ownership / capture lifecycle | `RunLock` (single-owner, JSON-encoded PID + host + session nonce + start time, with `OpenProcess` liveness and `timegm`-based age computation), `ManifestStore` (atomic `.partial` + `os.replace`, `READY` marker), `_SimCaptureWriter` rotation in the simulation adapter. | `src/qxdm_automation/locking/__init__.py`, `src/qxdm_automation/manifest/__init__.py`, `src/qxdm_automation/adapters/__init__.py` |
| 5. QCAT conversion / TXT publication | `QcatConverter` with per-source timeout, structural validation (non-empty output, encoding), and `commit_publication` with two consumer contracts (`run_folder` default and `flat`). | `src/qxdm_automation/conversion/__init__.py`, `src/qxdm_automation/publication/__init__.py` |
| 6. Failure handling / resumption | `monitor_capture` (bounded deadline, application-alive gate, free-space gate, file-size + mtime flush check); `CancellationToken`; `_try_finalize_on_failure` best-effort cleanup; `resume_conversion` re-runs conversion + publication on a finalized run, refuses silently when source sha256 has changed. | `src/qxdm_automation/monitoring/__init__.py`, `src/qxdm_automation/orchestration/__init__.py` |
| 7. Offline verification | 84 pytest tests across unit / integration / offline_sim covering the 15 behaviour categories. | `tests/` |
| 8. Operator CLI | `qxdm-automation {plan,run,resume,inventory}` with `argparse`, JSON output, exit codes 0/1/2/3/5/6/7/130. | `src/qxdm_automation/cli.py` |
| 9. Deliverables | This report + `docs/evidence_binding.md` + `docs/operator_guide.md` + `docs/commissioning_checklist.md` + `README.md` + `pyproject.toml` + `examples/*.json` + 84 passing tests. | `docs/`, `examples/`, `pyproject.toml` |

### Entry points

* `python -m qxdm_automation.cli plan -c examples/offline_sim.json` — preflight
* `python -m qxdm_automation.cli run -c examples/offline_sim.json` — full run
* `python -m qxdm_automation.cli resume -c examples/offline_sim.json` — conversion + publication only
* `python -m qxdm_automation.cli inventory` — inspect adapter capabilities
* `python -m pytest -q` — test suite

## 2. Verification commands and their outcomes

All commands are run from the repository root on a Windows 10 / Python
3.12.10 host.  The captured outputs are committed under `logs/`.

### 2.1.  Test suite — 84 passed in 32.54 s

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m pytest -v
```

```
============================= 84 passed in 32.54s =============================
```

Breakdown by file (counts from `pytest --collect-only`):

| File | Count |
|---|---|
| `tests/unit/test_config.py` | 13 |
| `tests/unit/test_manifest.py` | 6 |
| `tests/unit/test_locking.py` | 6 |
| `tests/unit/test_utils.py` | 9 |
| `tests/integration/test_orchestrator_lifecycle.py` | 25 |
| `tests/integration/test_adapters_publication.py` | 11 |
| `tests/integration/test_failure_modes.py` | 9 |
| `tests/offline_sim/test_cli_offline.py` | 5 |
| **Total** | **84** |

Every one of the **15 behaviour categories** from the prompt is exercised
by at least one test:

| # | Behaviour category | Test(s) |
|---|---|---|
| 1 | Lifecycle states advance in order | `test_full_simulated_run`, `test_inventory_uses_unbound` |
| 2 | Preflight refuses unsupported binding | `test_live_mode_without_bindings_blocks_preflight`, `test_plan_never_instantiates_vendor_objects` |
| 3 | Capability report is serialised | `test_capability_report_serializes` |
| 4 | Lock blocks concurrent acquire | `test_lock_blocks_concurrent_acquire`, `test_concurrent_acquire_is_rejected` |
| 5 | Stale lock recovery | `test_stale_lock_recovery`, `test_stale_lock_is_recoverable`, `test_recovery_disabled_blocks_stale_lock` |
| 6 | Manifest persists atomic | `test_manifest_store_save_load_round_trip`, `test_manifest_store_mark_ready`, `test_manifest_round_trip_preserves_binaries_and_conversions` |
| 7 | Capture monitor requires data | `test_monitor_completes_after_data_arrives`, `test_monitor_reports_stall_when_no_data_before_deadline` |
| 8 | Free-space gate | `test_monitor_free_space_gate` |
| 9 | Rotation cap recorded | `test_cap_violation_is_recorded` |
| 10 | QCAT timeout / failure | `test_qcat_timeout_is_recorded`, `test_qcat_failure_prevents_completion`, `test_qcat_empty_output_fails_validation` |
| 11 | Publication contracts | `test_publish_run_folder_writes_ready_marker`, `test_publish_flat_writes_per_run_marker`, `test_commit_publication_rejects_unknown_contract` |
| 12 | Resume does not relaunch QXDM | `test_resume_never_starts_qxdm`, `test_resume_re_runs_conversion_and_publication`, `test_resume_detects_changed_sources`, `test_resume_with_no_manifest_returns_failure` |
| 13 | Cancellation | `test_cancellation_during_capture`, `test_cancel_before_run` |
| 14 | CLI round-trip | `test_cli_plan`, `test_cli_run_offline`, `test_cli_inventory_returns_json`, `test_cli_rejects_invalid_config`, `test_cli_help_runs` |
| 15 | Config validation | all 13 tests in `tests/unit/test_config.py` |

### 2.2.  End-to-end CLI run — `state: COMPLETED`

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m qxdm_automation.cli -v run -c examples/offline_sim.json
```

Result (`logs/cli_run_offline.txt`):

```json
{
  "extras": {},
  "failure_reason": null,
  "manifest_path": "examples\\_runtime\\smoke-offline\\workspace\\smoke-offline\\manifest.json",
  "run_id": "smoke-offline",
  "state": "COMPLETED"
}
```

The manifest (`manifest.json` in the run workspace) records all
13 transitions with timestamps, eight `binaries` records, eight
`conversions` records, and eight `final_outputs`.  The
publication root contains:

```
manifest.json
QXDM_Log_000001__seg-0001.txt
QXDM_Log_000002__seg-0002.txt
QXDM_Log_000003__seg-0003.txt
QXDM_Log_000004__seg-0004.txt
QXDM_Log_000005__seg-0005.txt
QXDM_Log_000006__seg-0006.txt
QXDM_Log_000007__seg-0007.txt
QXDM_Log_000008__seg-0008.txt
READY
```

Each TXT file is 26 927 bytes; the conversion header is the
expected `SYNTHETIC CONVERTER OUTPUT` sentinel.  The orchestrator
ended in `COMPLETED` with no `cap_violations`, no `stop failed`,
and no `flush timeout` notes.

### 2.3.  Plan-only CLI run — `state: PREFLIGHT_OK`

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m qxdm_automation.cli -v plan -c examples/offline_sim.json
```

Result (`logs/cli_plan_offline.txt`): `state: PREFLIGHT_OK`,
`blocking_reasons: []`, `warnings: []`, capability reports
asserted by the simulation adapter, `discovery.found: ["COM-SIM-1"]`.

### 2.4.  Inventory CLI run

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m qxdm_automation.cli -v inventory
```

Result (`logs/cli_inventory.txt`): both QXDM and QCAT
report `binding is not implemented` and `blocking_reasons` non-empty.
This is the expected response when no real binding is wired; the
operator copies the actual signatures from a real QXDM and QCAT
into `src/qxdm_automation/adapters/real_qxdm.py` /
`real_qcat.py` (intentionally absent) and re-runs.

### 2.5.  Resume CLI run

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m qxdm_automation.cli -v resume -c examples/offline_sim.json
```

Result (`logs/cli_resume.txt`): `state: COMPLETED`.  The resume
path re-runs the conversion + publication for the prior run
without relaunching QXDM and without touching the capture
directory.

### 2.6.  Help output

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m qxdm_automation.cli -v --help
```

Result (`logs/cli_help.txt`): argparse shows the four
sub-commands.  The CLI rejects invalid configurations with exit
code 2 and JSON on stderr (asserted by
`test_cli_rejects_invalid_config`).

## 3. Vendor operations still unresolved or untested

The following are **not** exercised against real Qualcomm binaries
because the supplied materials do not provide the binding evidence.
Each item below records the precise missing artefact.

1. **Real QXDM launch + attach.**  No COM ProgID / SDK header / CLI
   is documented in the supplied PDFs; both PDFs mark every
   keyboard sequence as *build-specific*.  The real adapter file
   `src/qxdm_automation/adapters/real_qxdm.py` is intentionally
   absent.
2. **Real QCAT command line.**  The two PDFs label the converter
   command as a placeholder; no specific executable path, exit
   code, or output format is documented.  The real adapter file
   `src/qxdm_automation/adapters/real_qcat.py` is intentionally
   absent.
3. **DMC file format and acceptance evidence.**  No vendor
   specification for the operator-supplied `.dmc` file is in
   scope.  The orchestrator hashes the file and passes
   `accepted: bool` back, but the *real* acceptance signal
   depends on the binding.
4. **Native rotation byte semantics.**  Both PDFs only describe
   `MAX_FILE_SIZE_MB = 250`; neither proves the tool enforces a
   250,000,000-byte hard cap.  The orchestrator records any
   segment that exceeds the cap, but it cannot itself *cause*
   the cap to be honoured until a binding exists.
5. **Format-aware rotation.**  The prompt forbids splitting
   Qualcomm binary data at arbitrary byte offsets.  The
   `native_rotation_supported` flag in `CapabilityReport` is the
   guard, but no real adapter currently sets it to `true`.
6. **Decode fidelity.**  The simulation QCAT produces a clearly
   labelled `SYNTHETIC` payload.  No real QCAT export has been
   decoded; lossless capture and the consumer's expected TXT
   layout are both unverified.
7. **Process-liveness cross-host behaviour.**  `_pid_alive` uses
   Windows `OpenProcess`; on non-Windows hosts the lock layer
   degrades to "always alive", which is the documented offline
   behaviour.
8. **The "API references (to be verified).md" document.**  Named
   in the prompt but not transferred into the chat.  Without it,
   the binding matrix is the only source of truth for the live
   integration.

## 4. First operator commands

### 4.1.  Offline demonstration (no Qualcomm software required)

```powershell
PS> git clone <repo>
PS> cd qxdm_automation_backend
PS> python -m pip install -e .
PS> $env:PYTHONPATH = "$PWD\src"
PS> python -m qxdm_automation.cli -v run -c examples\offline_sim.json
PS> Get-Content .\examples\_runtime\smoke-offline\published\smoke-offline\READY
PS> python -m pytest -q
```

Expected: `state: COMPLETED`, eight TXT files, READY marker
present, 84 tests pass.

### 4.2.  Live preflight (before plugging in a real QXDM)

```powershell
PS> $env:PYTHONPATH = "$PWD\src"
PS> python -m qxdm_automation.cli -v inventory
PS> python -m qxdm_automation.cli -v plan -c <live-config>.json
```

Expected: both commands surface `binding is not implemented`
blocking reasons.  No QXDM, QCAT, or device is touched.

### 4.3.  Live run (after a binding has been wired in)

```powershell
PS> $env:PYTHONPATH = "$PWD\src"
PS> python -m qxdm_automation.cli -v run -c <live-config>.json
```

Expected: `state: COMPLETED` with a populated manifest and READY
marker on the publication root.  The first live capture should
be short and attended; see `docs/commissioning_checklist.md`.

## 5. Commissioning steps before unattended live capture

Follow `docs/commissioning_checklist.md` in order.  The minimum
blockers are:

1. Capture the seven items in
   `docs/evidence_binding.md §4` and write them into the lab run
   book.
2. Wire a real binding into
   `src/qxdm_automation/adapters/real_qxdm.py` and
   `real_qcat.py`; re-run `qxdm-automation inventory` until
   `blocking_reasons` is empty.
3. Run a *short* real capture with a known-good device and DMC.
4. Verify decode fidelity against the consumer's expected TXT
   layout (one record compared manually is the minimum).
5. Run the failure-path drills (cancel, external stop, device
   disconnect, second-run lock rejection, post-failure resume).
6. Sign the commissioning checklist; the operator is then
   authorised to use the pipeline for unattended capture.

## 6. Final test output

Captured for the report under `logs/test_output_final.txt` and
`logs/cli_run_offline.txt`, `logs/cli_plan_offline.txt`,
`logs/cli_inventory.txt`, `logs/cli_resume.txt`,
`logs/cli_help.txt`, `logs/published_layout.txt`.  The headline
result is **84 passed in 32.54 s**, and the offline CLI run ends
in `state: COMPLETED` with eight binary segments, eight TXT
outputs, and a `READY` marker in the publication root.
