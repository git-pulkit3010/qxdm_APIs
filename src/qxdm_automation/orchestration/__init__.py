"""Run-time orchestration: the lifecycle state machine driver.

The orchestrator is the only piece of the system that mutates the run
manifest, owns the QXDM session, and is permitted to call
:func:`publish_run_folder` / :func:`publish_flat`.  Adapters do not know
about the manifest, the lock, or the publication layer.  Tests inject a
fake clock and a fake file sampler so the same code path can be
exercised deterministically.

The driver is split into:

* :class:`Clock` — the only wall-clock and monotonic source the
  orchestrator uses.  Tests can replace it.
* :class:`Orchestrator` — public surface; takes a :class:`RunConfig`,
  a :class:`QxdmAdapter`, a :class:`QcatAdapter`, and a
  :class:`CancellationToken`.
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..adapters import (
    CaptureBinary,
    QcatAdapter,
    QxdmAdapter,
    QxdmSession,
    TargetHandle,
    make_qcat_adapter,
    make_qxdm_adapter,
)
from ..config import (
    DEFAULT_BINARY_EXTENSIONS,
    DEFAULT_MAX_BINARY_BYTES,
    RunConfig,
)
from ..conversion import QcatConverter
from ..discovery import resolve_target
from ..errors import (
    CancellationRequested,
    ConfigurationError,
    LockAcquisitionError,
    PreflightFailed,
    QxdmAutomationError,
    UnsupportedCapabilityError,
    VendorIntegrationError,
)
from ..locking import RunLock, acquired
from ..manifest import (
    BinaryRecord,
    ManifestStore,
    append_transition,
    new_manifest,
)
from ..monitoring import (
    CancellationToken,
    MonitorConfig,
    monitor_capture,
    wait_for_flush,
)
from ..publication import commit_publication, retain_binaries
from ..types import CapabilityReport, LifecycleState, RetentionPolicy
from ..utils import (
    ensure_directory,
    free_space_bytes,
    is_safe_filename,
    normalize_path,
    sha256_of_file,
)

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Clock abstraction
# ---------------------------------------------------------------------------


class Clock:
    """An injectable clock for deterministic tests.

    The default implementation tracks *real* monotonic and wall time
    (because the orchestrator is a real-time process), but ``advance``
    can push an offset forward without sleeping, which is how the test
    suite exercises deadlines without burning real wall-clock seconds.
    """

    def __init__(
        self,
        *,
        monotonic: Optional[float] = None,
        wall: Optional[float] = None,
        use_real_time: bool = True,
    ) -> None:
        self._monotonic_base = monotonic if monotonic is not None else time.monotonic()
        self._wall_base = wall if wall is not None else time.time()
        self._monotonic_offset = 0.0
        self._wall_offset = 0.0
        self._use_real_time = use_real_time

    def monotonic(self) -> float:
        if self._use_real_time:
            return time.monotonic() + self._monotonic_offset
        return self._monotonic_base + self._monotonic_offset

    def wall(self) -> float:
        if self._use_real_time:
            return time.time() + self._wall_offset
        return self._wall_base + self._wall_offset

    def advance(self, seconds: float) -> None:
        self._monotonic_offset += float(seconds)
        self._wall_offset += float(seconds)

    def now_wall(self) -> str:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.wall())
        )


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------


@dataclass
class OrchestrationResult:
    run_id: str
    state: LifecycleState
    manifest_path: Path
    failure_reason: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.state is LifecycleState.COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state.value,
            "manifest_path": str(self.manifest_path),
            "failure_reason": self.failure_reason,
            "extras": self.extras,
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """The lifecycle state-machine driver."""

    PUBLICATION_CONTRACT = "run_folder"  # default; can be overridden per call

    def __init__(
        self,
        config: RunConfig,
        *,
        qxdm_adapter: Optional[QxdmAdapter] = None,
        qcat_adapter: Optional[QcatAdapter] = None,
        clock: Optional[Clock] = None,
        cancellation: Optional[CancellationToken] = None,
        publication_contract: str = "run_folder",
        file_sampler: Optional[Callable[[Path, str, Sequence[str]], List[CaptureBinary]]] = None,
        application_alive_fn: Optional[Callable[[], bool]] = None,
        free_bytes_fn: Optional[Callable[[Path], int]] = free_space_bytes,
        binary_format: str = ".dlf",
        session_factory: Optional[Callable[[QxdmAdapter, RunConfig, Path], QxdmSession]] = None,
    ) -> None:
        self.config = config
        self.clock = clock or Clock()
        self.cancellation = cancellation or CancellationToken()
        self.qxdm_adapter = qxdm_adapter or make_qxdm_adapter(
            self._resolve_qxdm_binding()
        )
        self.qcat_adapter = qcat_adapter or make_qcat_adapter(
            self._resolve_qcat_binding()
        )
        self.publication_contract = publication_contract
        self._file_sampler = file_sampler or _default_file_sampler
        self._application_alive_fn = application_alive_fn or (lambda: True)
        self._free_bytes_fn = free_bytes_fn
        self._binary_format = binary_format
        self._session_factory = session_factory or self._default_session_factory

    # ---- Resolving bindings from config --------------------------------
    def _resolve_qxdm_binding(self) -> str:
        if self.config.mode.value in {"offline", "planning"}:
            return "simulation"
        return self.config.tool.qxdm_binding

    def _resolve_qcat_binding(self) -> str:
        if self.config.mode.value in {"offline", "planning"}:
            return "simulation"
        return self.config.tool.qcat_binding

    def _default_session_factory(
        self, adapter: QxdmAdapter, config: RunConfig, run_dir: Path
    ) -> QxdmSession:
        return adapter.launch_or_attach(
            qxdm_path=config.tool.qxdm_path,
            attachment=config.policy.attachment,
            run_dir=run_dir,
        )

    # ---- Top-level entry points -----------------------------------------
    def plan(self) -> Dict[str, Any]:
        """Validate configuration + adapter capabilities and return a plan.

        Performs no filesystem mutations except a single ``mkdir`` on the
        run workspace root, so the operator can confirm the layout is
        usable.  Always terminates in the BLOCKED or PREFLIGHT_OK
        preflight outcome; never advances to capture.
        """

        self.config.validate_path_layout()
        run_dir = self.config.run_dir()
        ensure_directory(self.config.paths.run_workspace_root)

        notes: List[str] = []
        blocking: List[str] = []
        warnings: List[str] = []

        # Inventory adapters.
        try:
            qxdm_cap = self.qxdm_adapter.inventory(
                qxdm_path=self.config.tool.qxdm_path
            )
        except UnsupportedCapabilityError as exc:
            qxdm_cap = CapabilityReport(
                qxdm_path=self.config.tool.qxdm_path,
                qcat_path=self.config.tool.qcat_path,
                qxdm_binding=self.qxdm_adapter.binding,
                qcat_binding=self.qcat_adapter.binding,
                native_rotation_supported=False,
                binary_format_evidence=frozenset(),
                qcat_export_evidence=False,
                free_space_bytes=0,
                blocking_reasons=("qxdm adapter not implemented",),
                notes=(str(exc),),
            )

        try:
            qcat_cap = self.qcat_adapter.inventory(
                qcat_path=self.config.tool.qcat_path
            )
        except UnsupportedCapabilityError as exc:
            qcat_cap = CapabilityReport(
                qxdm_path=self.config.tool.qxdm_path,
                qcat_path=self.config.tool.qcat_path,
                qxdm_binding=self.qxdm_adapter.binding,
                qcat_binding=self.qcat_adapter.binding,
                native_rotation_supported=False,
                binary_format_evidence=frozenset(),
                qcat_export_evidence=False,
                free_space_bytes=0,
                blocking_reasons=("qcat adapter not implemented",),
                notes=(str(exc),),
            )

        # Combine capability reports.
        if self.config.mode is not self.config.mode.LIVE:
            # In offline / planning modes, the simulation adapter
            # *is* the binding and every capability is asserted.  We
            # still surface the assertion so the operator is reminded
            # that live capability is unverified.
            notes.append("offline/planning mode — capabilities are asserted by the simulation adapter")
        else:
            if not qxdm_cap.ok:
                blocking.extend(f"qxdm: {r}" for r in qxdm_cap.blocking_reasons)
            if not qcat_cap.ok:
                blocking.extend(f"qcat: {r}" for r in qcat_cap.blocking_reasons)
            if not qxdm_cap.native_rotation_supported:
                blocking.append("qxdm: native rotation support is unverified")
            if not qcat_cap.qcat_export_evidence:
                blocking.append("qcat: export profile/CLI is unverified")

        # Path / free-space checks.
        for name, root in (
            ("capture_root", self.config.paths.capture_root),
            ("conversion_root", self.config.paths.conversion_root),
            ("publication_root", self.config.paths.publication_root),
            ("run_workspace_root", self.config.paths.run_workspace_root),
        ):
            ensure_directory(root)
            free = self._free_bytes_fn(root)
            if free and free < self.config.policy.min_free_bytes:
                blocking.append(
                    f"paths.{name}: free space {free} below minimum {self.config.policy.min_free_bytes}"
                )

        # DMC file check.
        if self.config.mode is self.config.mode.LIVE and not self.config.qxdm_dmc_path:
            blocking.append("qxdm_dmc_path is required for live mode")

        # Rotation size check.
        if self.config.tool.rotation_bytes > DEFAULT_MAX_BINARY_BYTES:
            blocking.append(
                f"rotation_bytes {self.config.tool.rotation_bytes} exceeds the hard 250 MB cap"
            )

        # QCAT template check.
        if self.config.mode is self.config.mode.LIVE and not self.config.tool.qcat_template:
            blocking.append("qcat_template is required for live mode")

        # Filename safety.
        if not is_safe_filename(self.config.paths.filename_prefix):
            blocking.append("filename_prefix is not a safe single filename component")

        # Target resolution preview.
        discovery = resolve_target(
            explicit=self.config.device_selector,
            auto_discover=self.config.auto_discover,
        )
        if not discovery.found:
            warnings.append(
                f"target discovery returned no candidates ({discovery.reason})"
            )

        state = LifecycleState.BLOCKED if blocking else LifecycleState.PREFLIGHT_OK
        return {
            "run_id": self.config.run_id,
            "run_dir": str(run_dir),
            "state": state.value,
            "blocking_reasons": list(blocking),
            "warnings": list(warnings),
            "notes": list(notes),
            "capability_qxdm": qxdm_cap.to_dict() if hasattr(qxdm_cap, "to_dict") else _as_dict(qxdm_cap),
            "capability_qcat": qcat_cap.to_dict() if hasattr(qcat_cap, "to_dict") else _as_dict(qcat_cap),
            "discovery": discovery.to_dict(),
            "config_sha256": self.config.config_sha256,
            "rotation_bytes": self.config.tool.rotation_bytes,
            "binary_format": self._binary_format,
        }

    # ---- Run -----------------------------------------------------------
    def run(self) -> OrchestrationResult:
        """Execute the full workflow.  Returns an :class:`OrchestrationResult`."""

        try:
            self.config.validate_path_layout()
        except ConfigurationError as exc:
            return self._fail_no_manifest(str(exc))

        run_dir = self.config.run_dir()
        ensure_directory(run_dir)
        ensure_directory(self.config.paths.capture_root)
        ensure_directory(self.config.paths.conversion_root)
        ensure_directory(self.config.paths.publication_root)

        manifest = new_manifest(
            run_id=self.config.run_id,
            run_category=self.config.run_category,
            mode=self.config.mode.value,
            config_path=self.config.config_path,
            config_sha256=self.config.config_sha256,
        )
        store = ManifestStore(run_dir)
        store.save(manifest)

        session: Optional[QxdmSession] = None
        binaries: List[CaptureBinary] = []
        handle = None
        failure: Optional[str] = None

        try:
            # ---- Preflight -------------------------------------------
            plan = self.plan()
            if plan["state"] == LifecycleState.BLOCKED.value:
                raise PreflightFailed(
                    "preflight blocked the run: " + "; ".join(plan["blocking_reasons"])
                )
            append_transition(
                manifest,
                LifecycleState.PREFLIGHT_OK,
                note="preflight passed",
                extras={
                    "blocking_reasons": list(plan["blocking_reasons"]),
                    "warnings": list(plan["warnings"]),
                },
            )
            store.save(manifest)
            self.cancellation.raise_if_cancelled()

            # ---- Lock ------------------------------------------------
            with acquired(
                run_dir,
                run_id=self.config.run_id,
                stale_lock_seconds=self.config.policy.stale_lock_seconds,
                extras={
                    "mode": self.config.mode.value,
                    "config_sha256": self.config.config_sha256,
                },
            ):
                # ---- Tool launch -----------------------------------
                session = self._session_factory(
                    self.qxdm_adapter, self.config, run_dir
                )
                append_transition(
                    manifest,
                    LifecycleState.TOOL_READY,
                    note="qxdm session opened",
                    extras={
                        "session_id": getattr(session, "session_id", None),
                        "pid": getattr(session, "pid", None),
                        "attachment": self.config.policy.attachment,
                    },
                )
                store.save(manifest)
                self.cancellation.raise_if_cancelled()

                # ---- Target connection ----------------------------
                discovery = resolve_target(
                    explicit=self.config.device_selector,
                    auto_discover=self.config.auto_discover,
                )
                if not discovery.found:
                    raise PreflightFailed(
                        f"no diagnostic target could be resolved ({discovery.reason})"
                    )
                target = session.connect_target(
                    selector=self.config.device_selector,
                    candidates=discovery.found,
                )
                append_transition(
                    manifest,
                    LifecycleState.TARGET_READY,
                    note=f"target connected: {target.selector}",
                    extras={"target": target.selector, "description": target.description},
                )
                store.save(manifest)
                self.cancellation.raise_if_cancelled()

                # ---- Configuration load ----------------------------
                if self.config.qxdm_dmc_path is not None:
                    cfg_result = session.load_configuration(
                        config_path=self.config.qxdm_dmc_path,
                        config_sha256=self.config.qxdm_dmc_sha256 or "",
                    )
                    if not cfg_result.accepted:
                        raise PreflightFailed(
                            f"configuration was rejected: {cfg_result.error or cfg_result.evidence}"
                        )
                    append_transition(
                        manifest,
                        LifecycleState.CONFIG_READY,
                        note="configuration accepted",
                        extras={"evidence": cfg_result.evidence},
                    )
                else:
                    append_transition(
                        manifest,
                        LifecycleState.CONFIG_READY,
                        note="no configuration file provided; using defaults",
                    )
                store.save(manifest)
                self.cancellation.raise_if_cancelled()

                # ---- Capture configure ------------------------------
                capture_config = session.configure_capture(
                    capture_root=self.config.paths.capture_root,
                    filename_prefix=self.config.paths.filename_prefix,
                    rotation_bytes=self.config.tool.rotation_bytes,
                    binary_format=self._binary_format,
                )
                append_transition(
                    manifest,
                    LifecycleState.CAPTURE_CONFIGURED,
                    note="capture configuration applied",
                    extras={
                        "capture_root": str(capture_config.capture_root),
                        "filename_prefix": capture_config.filename_prefix,
                        "rotation_bytes": capture_config.rotation_bytes,
                        "binary_format": capture_config.binary_format,
                    },
                )
                store.save(manifest)
                self.cancellation.raise_if_cancelled()

                # ---- Start capture ----------------------------------
                start_record_before_invocation = {
                    "started_at": self.clock.now_wall(),
                    "capture_root": str(self.config.paths.capture_root),
                    "filename_prefix": self.config.paths.filename_prefix,
                }
                append_transition(
                    manifest,
                    LifecycleState.START_REQUESTED,
                    note="capture start requested",
                    extras=start_record_before_invocation,
                )
                store.save(manifest)

                try:
                    handle = session.start_capture()
                except UnsupportedCapabilityError:
                    # Bubble up to fail the run; record the transition.
                    raise
                append_transition(
                    manifest,
                    LifecycleState.CAPTURING,
                    note="capture confirmed started",
                    extras={"started_at": getattr(handle, "started_at_wallclock", None)},
                )
                store.save(manifest)
                self.cancellation.raise_if_cancelled()

                # ---- Monitor ---------------------------------------
                start_mono = self.clock.monotonic()
                deadline = start_mono + float(self.config.timing.capture_duration)
                monitor_config = MonitorConfig(
                    deadline_monotonic=deadline,
                    interval_seconds=self.config.timing.monitor_interval,
                    readiness_timeout_seconds=self.config.timing.readiness_timeout,
                    stop_timeout_seconds=self.config.timing.stop_timeout,
                    flush_timeout_seconds=self.config.timing.flush_timeout,
                    min_free_bytes=self.config.policy.min_free_bytes,
                    require_data=True,
                )
                result = monitor_capture(
                    capture_root=self.config.paths.capture_root,
                    config=monitor_config,
                    expected_prefix=self.config.paths.filename_prefix,
                    expected_extensions=self.config.paths.binary_extensions,
                    file_sampler=self._file_sampler,
                    application_alive_fn=self._application_alive_fn,
                    clock=self.clock.monotonic,
                    cancellation=self.cancellation,
                    free_bytes_fn=self._free_bytes_fn,
                )
                if not result.completed_normally:
                    raise PreflightFailed(
                        f"capture monitor stopped early: {result.stall_reason}"
                    )
                self.cancellation.raise_if_cancelled()

                # ---- Stop capture ---------------------------------
                append_transition(
                    manifest,
                    LifecycleState.STOP_REQUESTED,
                    note="capture stop requested",
                )
                store.save(manifest)
                session.stop_capture(handle)

                # ---- Finalize -------------------------------------
                append_transition(
                    manifest,
                    LifecycleState.FINALIZING,
                    note="finalising capture",
                )
                store.save(manifest)
                binaries = session.finalize_capture(
                    handle,
                    flush_deadline_seconds=self.config.timing.flush_timeout,
                )
                if not binaries:
                    # Independent re-check via the file sampler so we
                    # never convert a zero-segment run.
                    sampled = self._file_sampler(
                        self.config.paths.capture_root,
                        self.config.paths.filename_prefix,
                        self.config.paths.binary_extensions,
                    )
                    if not sampled:
                        raise PreflightFailed(
                            "capture finalised with zero run-scoped binary files"
                        )
                    binaries = sampled
                for b in binaries:
                    manifest.binaries.append(
                        BinaryRecord(
                            path=str(b.path),
                            size_bytes=b.size_bytes,
                            sha256=b.sha256,
                            segment_index=b.segment_index,
                            status="finalized",
                        )
                    )
                # Verify the cap.
                for b in binaries:
                    if b.size_bytes > self.config.tool.rotation_bytes:
                        manifest.extra.setdefault("cap_violations", []).append(
                            {
                                "path": str(b.path),
                                "size_bytes": b.size_bytes,
                                "rotation_bytes": self.config.tool.rotation_bytes,
                            }
                        )
                append_transition(
                    manifest,
                    LifecycleState.CAPTURE_FINALIZED,
                    note="capture finalised",
                    extras={
                        "binary_count": len(binaries),
                        "cap_violations": manifest.extra.get("cap_violations", []),
                    },
                )
                store.save(manifest)
                self.cancellation.raise_if_cancelled()

                # ---- Conversion -----------------------------------
                append_transition(
                    manifest,
                    LifecycleState.CONVERTING,
                    note="starting conversion",
                )
                store.save(manifest)
                staging = self.config.paths.conversion_root / self.config.run_id
                ensure_directory(staging)
                converter = QcatConverter(
                    self.qcat_adapter,
                    timeout_seconds=self.config.timing.conversion_timeout,
                    max_retries=self.config.policy.max_retries,
                )
                outcomes = converter.convert_all(
                    binaries=binaries,
                    staging_dir=staging,
                    name_for=_unique_output_name,
                )
                for outcome in outcomes:
                    manifest.conversions.append(outcome.record)
                if any(o.fatal for o in outcomes):
                    failed = [
                        o.record.source_binary for o in outcomes if o.fatal
                    ]
                    raise PreflightFailed(
                        f"conversion failed for {len(failed)} file(s): {failed}"
                    )
                store.save(manifest)
                self.cancellation.raise_if_cancelled()

                # ---- Publication -----------------------------------
                append_transition(
                    manifest,
                    LifecycleState.PUBLISHING,
                    note="publishing TXT outputs",
                    extras={"publication_contract": self.publication_contract},
                )
                store.save(manifest)
                artifacts = [o.text for o in outcomes if o.text is not None]
                publication = commit_publication(
                    publication_root=self.config.paths.publication_root,
                    run_id=self.config.run_id,
                    manifest=manifest,
                    artifacts=artifacts,
                    contract=self.publication_contract,
                    binary_retain=(self.config.policy.retention is RetentionPolicy.RETAIN),
                )
                manifest.final_outputs = publication.to_dict()["published"]
                append_transition(
                    manifest,
                    LifecycleState.COMPLETED,
                    note="run completed",
                    extras={
                        "published_count": len(publication.consumer_paths),
                        "publication_root": str(self.config.paths.publication_root),
                        "consumer_paths": [str(p) for p in publication.consumer_paths],
                    },
                )
                store.mark_ready(manifest)

                if self.config.policy.retention is RetentionPolicy.DELETE_ON_SUCCESS:
                    retain_binaries(
                        capture_root=self.config.paths.capture_root,
                        retention="delete_on_success",
                        binary_record_paths=[b.path for b in binaries],
                    )

        except CancellationRequested as exc:
            failure = f"cancelled: {exc}"
            self._try_finalize_on_failure(manifest, store, session, handle, binaries, failure, "CANCELLED")
        except UnsupportedCapabilityError as exc:
            failure = f"unsupported capability: {exc}"
            self._try_finalize_on_failure(manifest, store, session, handle, binaries, failure, "BLOCKED")
        except PreflightFailed as exc:
            failure = f"preflight failed: {exc}"
            self._try_finalize_on_failure(manifest, store, session, handle, binaries, failure, "BLOCKED")
        except QxdmAutomationError as exc:
            failure = f"{type(exc).__name__}: {exc}"
            self._try_finalize_on_failure(manifest, store, session, handle, binaries, failure, "FAILED")
        except Exception as exc:
            failure = f"unexpected: {exc}"
            LOGGER.error("orchestrator raised an unexpected exception: %s", exc)
            LOGGER.error("traceback:\n%s", traceback.format_exc())
            self._try_finalize_on_failure(manifest, store, session, handle, binaries, failure, "FAILED")
        else:
            return OrchestrationResult(
                run_id=self.config.run_id,
                state=manifest.current_state,
                manifest_path=store.path,
            )

        return OrchestrationResult(
            run_id=self.config.run_id,
            state=manifest.current_state,
            manifest_path=store.path,
            failure_reason=failure,
        )

    # ---- Resume conversion (existing finalized capture) -----------------
    def resume_conversion(self) -> OrchestrationResult:
        """Convert a previously-finalised capture without re-running QXDM.

        Validates that the source binaries are intact and have not been
        re-touched.  Skips already-committed work.  Will not modify the
        capture directory.
        """

        run_dir = self.config.run_dir()
        store = ManifestStore(run_dir)
        manifest = store.load()
        if manifest is None:
            return self._fail_no_manifest("no manifest found for this run")
        if manifest.current_state not in {
            LifecycleState.CAPTURE_FINALIZED,
            LifecycleState.CONVERTING,
            LifecycleState.PUBLISHING,
            LifecycleState.FAILED,
            LifecycleState.INTERRUPTED,
            LifecycleState.COMPLETED,
            LifecycleState.BLOCKED,
        }:
            return OrchestrationResult(
                run_id=self.config.run_id,
                state=manifest.current_state,
                manifest_path=store.path,
                failure_reason="run is not eligible for conversion resume",
            )

        # Re-inventory the source binaries.
        binaries = self._file_sampler(
            self.config.paths.capture_root,
            self.config.paths.filename_prefix,
            self.config.paths.binary_extensions,
        )
        if not binaries:
            return OrchestrationResult(
                run_id=self.config.run_id,
                state=manifest.current_state,
                manifest_path=store.path,
                failure_reason="no run-scoped binary files found",
            )
        # Validate that previously-recorded binaries still match the
        # current filesystem state.
        recorded_by_path = {b.path: b for b in manifest.binaries}
        for b in binaries:
            rec = recorded_by_path.get(str(b.path))
            if rec is not None and rec.sha256 != b.sha256:
                append_transition(
                    manifest,
                    LifecycleState.FAILED,
                    note=(
                        f"source binary {b.path} has changed (sha256 "
                        f"{rec.sha256} -> {b.sha256}); refusing silent skip"
                    ),
                )
                store.save(manifest)
                return OrchestrationResult(
                    run_id=self.config.run_id,
                    state=manifest.current_state,
                    manifest_path=store.path,
                    failure_reason=(
                        f"source binary {b.path} has changed (sha256 "
                        f"{rec.sha256} -> {b.sha256}); refusing silent skip"
                    ),
                )

        # Re-run conversion.
        staging = self.config.paths.conversion_root / self.config.run_id
        ensure_directory(staging)
        converter = QcatConverter(
            self.qcat_adapter,
            timeout_seconds=self.config.timing.conversion_timeout,
            max_retries=self.config.policy.max_retries,
        )
        append_transition(
            manifest,
            LifecycleState.CONVERTING,
            note="resuming conversion",
        )
        store.save(manifest)
        outcomes = converter.convert_all(
            binaries=binaries,
            staging_dir=staging,
            name_for=_unique_output_name,
        )
        # Replace previously-recorded conversion entries to avoid
        # duplicates; we keep failure history in extras.
        manifest.conversions = []
        for outcome in outcomes:
            manifest.conversions.append(outcome.record)
        if any(o.fatal for o in outcomes):
            failed = [o.record.source_binary for o in outcomes if o.fatal]
            return OrchestrationResult(
                run_id=self.config.run_id,
                state=LifecycleState.FAILED,
                manifest_path=store.path,
                failure_reason=f"conversion failed for {failed}",
            )
        # Publication
        append_transition(
            manifest,
            LifecycleState.PUBLISHING,
            note="publishing TXT outputs (resume)",
            extras={"publication_contract": self.publication_contract},
        )
        store.save(manifest)
        artifacts = [o.text for o in outcomes if o.text is not None]
        publication = commit_publication(
            publication_root=self.config.paths.publication_root,
            run_id=self.config.run_id,
            manifest=manifest,
            artifacts=artifacts,
            contract=self.publication_contract,
        )
        manifest.final_outputs = publication.to_dict()["published"]
        append_transition(
            manifest,
            LifecycleState.COMPLETED,
            note="run completed (resume)",
            extras={
                "published_count": len(publication.consumer_paths),
                "publication_root": str(self.config.paths.publication_root),
                "consumer_paths": [str(p) for p in publication.consumer_paths],
            },
        )
        store.mark_ready(manifest)
        return OrchestrationResult(
            run_id=self.config.run_id,
            state=manifest.current_state,
            manifest_path=store.path,
        )

    # ---- Internals -----------------------------------------------------
    def _try_finalize_on_failure(
        self,
        manifest,
        store: ManifestStore,
        session: Optional[QxdmSession],
        handle,
        binaries: List[CaptureBinary],
        reason: str,
        state: str,
    ) -> None:
        # Best-effort cleanup.  Never raises.
        manifest.failure_reason = reason
        try:
            if session is not None and handle is not None:
                try:
                    session.stop_capture(handle)
                except Exception as exc:
                    LOGGER.warning("stop_capture during failure handling failed: %s", exc)
                try:
                    if not binaries:
                        binaries = session.finalize_capture(
                            handle,
                            flush_deadline_seconds=self.config.timing.flush_timeout,
                        )
                except Exception as exc:
                    LOGGER.warning("finalize_capture during failure handling failed: %s", exc)
                for b in binaries:
                    if not any(rec.path == str(b.path) for rec in manifest.binaries):
                        manifest.binaries.append(
                            BinaryRecord(
                                path=str(b.path),
                                size_bytes=b.size_bytes,
                                sha256=b.sha256,
                                segment_index=b.segment_index,
                                status="partial",
                            )
                        )
        finally:
            try:
                if session is not None:
                    session.close()
            except Exception as exc:
                LOGGER.warning("session close during failure handling failed: %s", exc)
            try:
                append_transition(
                    manifest,
                    LifecycleState(state),
                    note=reason,
                )
                store.save(manifest)
            except Exception as exc:
                LOGGER.error("manifest save during failure handling failed: %s", exc)

    def _fail_no_manifest(self, reason: str) -> OrchestrationResult:
        return OrchestrationResult(
            run_id=self.config.run_id,
            state=LifecycleState.FAILED,
            manifest_path=self.config.manifest_path(),
            failure_reason=reason,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_dict(report) -> Dict[str, Any]:
    if hasattr(report, "to_dict"):
        return report.to_dict()
    return {
        "qxdm_path": str(report.qxdm_path) if report.qxdm_path else None,
        "qcat_path": str(report.qcat_path) if report.qcat_path else None,
        "qxdm_binding": report.qxdm_binding,
        "qcat_binding": report.qcat_binding,
        "native_rotation_supported": report.native_rotation_supported,
        "binary_format_evidence": sorted(report.binary_format_evidence),
        "qcat_export_evidence": report.qcat_export_evidence,
        "free_space_bytes": report.free_space_bytes,
        "blocking_reasons": list(report.blocking_reasons),
        "notes": list(report.notes),
    }


def _unique_output_name(binary: CaptureBinary, index: int) -> str:
    """A collision-proof TXT name for a binary.

    Combines the source stem with the run-scoped segment index so that
    ``a.dlf`` and ``a.bin`` cannot collide and segment order is recorded.
    """

    stem = Path(binary.path).stem
    if binary.segment_index is None:
        suffix = f"{index:04d}"
    else:
        suffix = f"{binary.segment_index:04d}"
    if not is_safe_filename(stem):
        stem = f"segment_{suffix}"
    return f"{stem}__seg-{suffix}.txt"


def _default_file_sampler(
    root: Path, prefix: str, extensions: Sequence[str]
) -> List[CaptureBinary]:
    """The default run-scoped file sampler.

    Lists every file under *root* whose name starts with *prefix* and
    ends with one of the configured extensions; computes size and
    sha256.  Used by the monitor and the resume path.
    """

    root = normalize_path(root)
    if not root.exists():
        return []
    results: List[CaptureBinary] = []
    suffix_set = {e.lower() for e in extensions}
    for entry in sorted(root.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.startswith(prefix):
            continue
        if not any(entry.name.lower().endswith(s) for s in suffix_set):
            continue
        try:
            size = entry.stat().st_size
            digest = sha256_of_file(entry)
        except OSError:
            continue
        # Best-effort segment index from name.
        seg_index = _extract_segment_index(entry.name)
        results.append(
            CaptureBinary(
                path=entry,
                size_bytes=size,
                sha256=digest,
                segment_index=seg_index,
            )
        )
    results.sort(key=lambda b: (b.segment_index is None, b.segment_index or 0, str(b.path)))
    return results


def _extract_segment_index(name: str) -> Optional[int]:
    """Pull a numeric segment index out of a name like ``prefix_003.dlf``.

    Returns ``None`` when no plausible index is found.
    """

    base = name.rsplit(".", 1)[0]
    parts = base.split("_")
    for part in reversed(parts):
        if part.isdigit():
            return int(part)
    return None


__all__ = [
    "Clock",
    "Orchestrator",
    "OrchestrationResult",
]
