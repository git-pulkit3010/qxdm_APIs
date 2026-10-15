# Live Commissioning Checklist

This checklist turns the offline-tested software into a production
capture pipeline.  Every step **must** be answered with evidence and
recorded; do not proceed past a failure.  A pass on the offline test
suite is **not** authorisation to run unattended capture on real
hardware.

## 1. Lab readiness

- [ ] Windows 10/11 host with the operator's administrative user.
- [ ] QXDM Professional installed and **licensed** on the host.
- [ ] QCAT (or the equivalent Qualcomm post-processing tool) installed
      and licensed.
- [ ] The diagnostic device's Windows driver installed and visible in
      Device Manager; the COM port or QDSS endpoint recorded.
- [ ] The DMC file the operator intends to load is on disk and its
      sha256 is recorded.
- [ ] The destination consumer's folder exists, is writable from the
      orchestrator's user, and is **distinct** from the capture root
      and the run workspace.
- [ ] The output volume has at least **2 × 250 MB** of free space
      per expected segment, plus headroom for TXT expansion.

## 2. Inventory

- [ ] `qxdm-automation inventory --qxdm-path <…> --qcat-path <…>`
      returns a JSON document.
- [ ] The `qxdm_path` and `qcat_path` values match the binaries on
      disk; if not, the inventory path is wrong.
- [ ] `native_rotation_supported` is **true**; if not, stop — the
      operator must supply a build that rotates natively, or escalate.
- [ ] `binary_format_evidence` lists at least one extension the
      operator's QXDM emits (`.dlf`, `.qmdl`, etc.).
- [ ] `qcat_export_evidence` is **true**; if not, stop.
- [ ] `blocking_reasons` is empty.

## 3. Vendor binding evidence

Record the answers to the seven unresolved items from
`docs/evidence_binding.md §4`:

- [ ] QXDM build, version, bitness, and registration mechanism
      (COM ProgID / SDK / CLI).
- [ ] How the build enumerates devices.
- [ ] Argument type the connect call expects.
- [ ] Configuration acceptance evidence (read-back, error code, log).
- [ ] Rotation behaviour and the unit (bytes, KB, MB) the tool honours.
- [ ] Start / stop / finalise semantics and the strongest read-back.
- [ ] QCAT command line and the completion signal.

> These seven items **must** be resolved before any unattended live
> run.  Document the source of each answer (vendor help, type-library
> dump, sample output).  If any item is still "unknown", the run must
> be attended.

## 4. Configuration

- [ ] The configuration file lives in version control.
- [ ] `mode` is `live` only after the binding evidence above is
      captured.
- [ ] `tool.rotation_bytes` is `≤ 250_000_000`.
- [ ] `tool.qcat_template` contains only placeholders the QCAT
      adapter recognises, and the path is double-quoted in the JSON.
- [ ] `paths.*` are distinct, non-overlapping, and on volumes with
      documented durability behaviour.
- [ ] `policy.min_free_bytes` is non-zero.
- [ ] `policy.attachment` is `owned` unless the user already has a
      QXDM session they intend to keep running.

## 5. Preflight

- [ ] `qxdm-automation plan -c <config>` returns exit code 0.
- [ ] The plan JSON's `blocking_reasons` array is empty.
- [ ] The plan's `discovery.found` array contains the expected
      device; if the operator used a `device_selector`, it is
      returned as the single entry.
- [ ] The plan's `notes` array contains no "unsupported" entries.

## 6. Short real capture

Run a *short* real capture with a known-good device:

```powershell
PS> qxdm-automation run -c <config>
```

- [ ] Exit code 0.
- [ ] `<run_workspace_root>\<run_id>\manifest.json` contains
      transitions for every lifecycle state.
- [ ] `<publication_root>\<run_id>\READY` exists.
- [ ] The consumer opened the published TXT and reported the file
      shape it expected.
- [ ] No "cap_violation" entries in the manifest.
- [ ] No "stop failed" or "flush timeout" notes in the manifest.

## 7. Multi-segment threshold-crossing run

Re-run with a long enough capture to force at least one rotation:

- [ ] At least two segments appear in the capture root.
- [ ] The published TXT count equals the binary count.
- [ ] The manifest records `cap_violations == []` for every segment.
- [ ] Capture gap detector (operator-supplied) reports a continuous
      capture window; the manifest's monitor samples confirm
      application aliveness throughout.

## 8. Decode fidelity

- [ ] The published TXT files open with the consumer's expected
      encoding (typically UTF-8).
- [ ] The consumer's decoder runs end-to-end without errors.
- [ ] At least one record (e.g. a 0x108C log packet) is manually
      compared against the raw binary to confirm the decoder did not
      silently drop fields.
- [ ] The output is **not** assumed to be lossless; the operator
      records the evidence that fidelity was actually verified, not
      merely that the run completed.

## 9. Failure-path commissioning

- [ ] Cancel a running capture with Ctrl-C; verify the manifest
      transitions to `CANCELLED` and the consumer's folder does not
      contain a partial `READY` marker.
- [ ] Stop QXDM externally; verify the orchestrator reports a
      failure and writes a partial manifest.
- [ ] Disconnect the device during capture; verify the monitor
      reports `application/process is not alive` or a stalled
      capture.
- [ ] Try a second `qxdm-automation run` while the first is
      still alive; verify the second is rejected by the lock.
- [ ] Re-run `qxdm-automation resume` after a failed conversion;
      verify the published TXT count and hashes match the original
      capture.

## 10. Documentation

- [ ] The actual QCAT command line is pasted into the operator
      guide and the evidence/binding matrix.
- [ ] The actual DMC file's sha256 is recorded at the lab level.
- [ ] The list of file extensions QXDM actually produces is recorded.
- [ ] The list of TXT files the consumer actually accepted is recorded.

## 11. Sign-off

Operator name:
Date:
QXDM build / version:
QCAT build / version:
First successful live run: attached
Outstanding open items: 0

> Do not proceed to unattended capture unless every box above is
> signed and the evidence is committed to the lab's run book.
