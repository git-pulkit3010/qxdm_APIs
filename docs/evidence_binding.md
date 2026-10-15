# Evidence / Binding Matrix

This document records what the supplied evidence **does** establish, what is
**inferred**, and what is **unresolved** for the QXDM capture + QCAT text
handoff.  Every row in the matrix below maps an operation the orchestrator
performs to a source, an explicit vendor binding, and the verification
status.

> **Reading guide.**  "Documented" = the source explicitly describes the
> behaviour (even if unverified live).  "Inferred" = a reasonable
> extrapolation from the source, **not** a vendor contract.  "Unresolved" =
> no evidence in the supplied materials; the adapter must raise
> `UnsupportedCapabilityError` and the orchestrator must abort before
> irreversible live operations.

## 1. Source inventory

| Source | Size | Status | Notes |
|---|---|---|---|
| `Code prompt.md` | 29,874 bytes | authoritative | The prompt this implementation follows. |
| `QXDM logging workflow_v5.x.pdf` | 458,883 bytes | candidate | Extracted text in `_extracted/QXDM logging workflow_v5.x.txt`. |
| `QXDM logging workflow_V6.x.pdf` | 451,610 bytes | candidate | Extracted text in `_extracted/QXDM logging workflow_V6.x.txt`. |
| `API references (to be verified).md` | not transferred | gap | Prompt notes it exists; not provided in this run. |
| `TMO_QXDM_LogProcess_Automation_0.3.pptx` | not transferred | gap | Prompt notes scope, not method signatures. |
| `AgenticAInstein_0.3.pptx` | not transferred | gap | Context only; no executable contract. |
| Public QXDM Professional quick start | web | documented | Purpose-level evidence; not a signature. |
| Public QXDM 5G training page | web | documented | QCAT post-processing context; not a signature. |

The two supplied PDFs are **the same workflow at different vintages**.  The
v6.x document explicitly states "the exact menu/control IDs differ between
QXDM 5.x builds" and labels its converter command as a placeholder.  The v5
document makes the same caveats.

The "API references" markdown was named in the prompt but **not transferred
to the chat**; the prompt itself labels it "to be verified".

## 2. Per-operation binding matrix

| Operation | Source | Available evidence | Proposed binding | Status |
|---|---|---|---|---|
| Launch QXDM | both PDFs (5.x p. 3, 6.x p. 4) | `Application(backend='win32').start(QXDM_EXE)` via pywinauto | Treated as a vendor adapter boundary; no method signature inferred. | Unresolved. |
| Attach to QXDM main window | both PDFs (5.x p. 4, 6.x p. 6) | `app.window(title_re=...).wait('visible', timeout=30)` | Adapter-specific; orchestrator owns lifecycle. | Unresolved. |
| Load DMC configuration | both PDFs (5.x p. 5, 6.x p. 7) | `send_keys('%f'); send_keys('l'); send_keys(path); send_keys('{ENTER}')`; explicitly marked build-specific. | Orchestrator hashes the file and passes the path + sha256 to the adapter; the adapter decides the actual loading mechanism. | Unresolved. |
| Discover / connect COM port | both PDFs (5.x p. 6, 6.x p. 2) | `Get-CimInstance Win32_SerialPort` filtered by `qualcomm`/`diag`/`qdss`/`diagnostic` substrings. | Implemented in `discovery.py`; the orchestrator treats broad strings as **hints only**, not as proof of the correct endpoint. | Documented. |
| Configure logging | both PDFs (5.x p. 10, 6.x p. 9) | Keyboard sequence: `send_keys(path); send_keys('{TAB}'); send_keys(prefix); send_keys('{TAB}'); send_keys(MAX_LOG_SIZE_MB); ...`. Explicitly build-specific. | Adapter boundary: `configure_capture(capture_root, filename_prefix, rotation_bytes, binary_format)`. | Unresolved. |
| Set rotation to 250 MB | both PDFs (5.x p. 1, 6.x p. 1) | `MAX_FILE_SIZE_MB = 250`; sample divides by 1024² when reporting size. Neither PDF proves a 250,000,000-byte hard cap is enforced by the tool. | Orchestrator uses a hard `rotation_bytes <= 250,000,000` and records any cap violation. | Documented (the 250 setting); inferred (the byte cap). |
| Start logging | both PDFs (5.x p. 12, 6.x p. 11) | `send_keys('%f'); send_keys('s')`; verify by enumerating new files in the capture directory. | Orchestrator records `START_REQUESTED` before invoking, then `CAPTURING` after the adapter confirms. | Documented (verification heuristic); unresolved (binding). |
| Verify capture is producing data | both PDFs (5.x p. 12, 6.x p. 11) | Loop until a new file appears or 30 s timeout. | Orchestrator requires *both* a non-empty file and bounded growth; a new empty file is insufficient. | Documented. |
| Stop logging | both PDFs (5.x p. 14, 6.x p. 13) | `send_keys('%f'); send_keys('t')` followed by an unbounded size-stability loop in v6.x. | Orchestrator records `STOP_REQUESTED`, calls `stop_capture`, then `finalize_capture` with a bounded deadline. | Documented (intent); inferred (finalisation). |
| Wait for files to flush | both PDFs (5.x p. 15, 6.x p. 14) | Size-stable for 3 observations at 2 s each. | Orchestrator's `wait_for_flush` requires stability plus mtime equality; size alone is not sufficient. | Documented. |
| Exit QXDM | both PDFs (5.x p. 15, 6.x p. 15) | `send_keys('%{F4}')` then `send_keys('n')` for the save dialog. | Orchestrator only closes the session it owns; idle attachment is documented but not exercised. | Documented. |
| Convert binary to text | both PDFs (5.x p. 17, 6.x p. 17) | `QCAT.exe <input> <output>` — **explicitly labelled a placeholder** in v6.x. | Treated as a guarded adapter; the orchestrator requires a documented template with placeholders, and the adapter surfaces completion evidence. | Unresolved. |
| Candidate binary extensions | both PDFs (5.x p. 11, 6.x p. 10) | `*.dlf`, `*.bin`, `*.isf`, `*.hdf`, `*.qmdl` — *candidates*. | Configuration allows the operator to override; the orchestrator never infers format support from a filename alone. | Documented (candidates). |
| Process / device ownership | prompt section 5 | "Track PID plus suitable process/session identity to avoid PID-reuse mistakes." | `RunLock` records PID + host + session nonce + start time; `_pid_alive` uses Windows `OpenProcess` with the right bitness. | Documented. |
| Native rotation support | prompt section 5 | "Native format-aware rotation is required. Never split Qualcomm binary data at arbitrary byte offsets." | The real QXDM adapter must declare `native_rotation_supported=True`; otherwise the run is blocked at preflight. | Inferred. |

## 3. Public Qualcomm context (informational, not signature-level)

| URL | What it establishes | What it does **not** establish |
|---|---|---|
| <https://www.qualcomm.com/documents/qxdm-professional-qualcomm-extensible-diagnostic-monitor> | QXDM Professional is a Windows tool that captures diagnostic data from a connected device. | Method signatures, COM ProgIDs, SDK headers, or a binary-to-text command. |
| <https://academy.qualcomm.com/course-catalog/QXDM-5G-Log-Analysis-Training> | QXDM5 collection, QUTS device management, and QCAT post-processing are part of the workflow. | QXDM automation API, the format of QCAT's CLI, or what "config" means internally. |

## 4. Unresolved requirements for live operation

A live run requires the following artefacts that the supplied materials do
**not** provide.  Each item below is the precise minimum the integration
needs to make the corresponding adapter bound.

1. **QXDM launch binding**
   * Build, version, and bitness of the local QXDM.
   * Registration: COM ProgID, in-process SDK handle, or documented CLI.
   * Apartment/thread model if COM is applicable; matching Python bitness
     if the bridge mandates it.

2. **QXDM attach / target connection**
   * How the tool enumerates devices (real QXDM uses its own service, not
     necessarily a serial port).
   * The argument type (string vs. number vs. structured device ID) the
     connect call expects.
   * How a "connection" is read back; whether the tool returns a status
     object or only updates internal state.

3. **Configuration acceptance**
   * The format the operator's `.dmc` file must use.
   * Whether masks are read back; if not, the strongest available
     acceptance signal.
   * Behaviour when the file is corrupt or unsigned.

4. **Capture setup, rotation, and format**
   * Whether the tool has a documented binary rotation limit in bytes.
   * Whether the tool rotates natively or the orchestrator must split
     files (the prompt forbids the latter unless explicitly approved).
   * The accepted binary formats and how to select one.

5. **Start / stop / finalise semantics**
   * Whether `start_capture` is synchronous or fire-and-forget.
   * How to detect that capture is genuinely writing (the prompt's
     "running logger vs. useful capture" distinction).
   * What `stop_capture` actually does and what a clean shutdown looks
     like.
   * What guarantees the tool provides that files are fully flushed to
     disk.

6. **QCAT export**
   * The actual command line or automation entry point.  The two PDFs
     show a placeholder; this is unresolved.
   * The output format, encoding, and the line layout the consumer
     expects.
   * The completion signal: exit code is not sufficient; what does
     "fully exported" mean in QCAT's terms?

7. **License / device driver prerequisites**
   * Qualcomm activation flow (the TMO deck calls these "customer
     supplied").
   * Windows device driver requirements for the diagnostic port.

Until the above are answered, the operator must keep the run in
`offline` (with the simulation adapter) or `planning` (preflight only)
modes.  The orchestrator surfaces every gap at preflight and refuses to
start capture.

## 5. Documents that close these gaps

* Run `qxdm-automation inventory` to inspect tool paths and what the
  *adapters' inventory methods* report.
* The QXDM build's own type library (if COM is available) can be
  inspected through the `inventory` adapter; the operator pastes the
  method signatures back into
  `src/qxdm_automation/adapters/real_qxdm.py` (see the missing-module
  note below).
* The first live capture should be a *short* run on a *known-good* target
  with a *known-good* DMC; only then should the orchestrator be trusted
  with multi-segment production runs.

## 6. Missing module

The file `src/qxdm_automation/adapters/real_qxdm.py` is intentionally
absent: it would be the only place that imports a vendor-specific
binding (e.g. `win32com.client`).  Adding it requires the artefact
described in section 4.  Until then, the live adapter
(`UnboundQxdmAdapter` / `UnboundQcatAdapter`) is the only real binding
shipped, and it raises `UnsupportedCapabilityError` on every method.
