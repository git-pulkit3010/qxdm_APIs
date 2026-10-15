"""Tests for the failure-handling and lock contention paths."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from qxdm_automation.adapters import (
    SimulationQcatAdapter,
    SimulationQxdmAdapter,
    UnboundQcatAdapter,
    UnboundQxdmAdapter,
)
from qxdm_automation.config import RunConfig
from qxdm_automation.errors import LockAcquisitionError
from qxdm_automation.locking import RunLock, acquired
from qxdm_automation.manifest import ManifestStore
from qxdm_automation.monitoring import (
    CancellationToken,
    MonitorConfig,
    monitor_capture,
    wait_for_flush,
)
from qxdm_automation.orchestration import Clock, Orchestrator
from qxdm_automation.types import CapabilityReport, CaptureBinary, LifecycleState, Mode
from qxdm_automation.utils import sha256_of_file

from ..conftest import make_orchestrator, seed_capture_files


# ---------------------------------------------------------------------------
# Monitoring tests with an injected clock and sampler
# ---------------------------------------------------------------------------


def test_monitor_completes_after_data_arrives(tmp_path: Path) -> None:
    counter = {"n": 0}
    # Use real time so the monitor's internal sleep advances the
    # clock; the deadline is anchored to the real monotonic clock.
    base = time.monotonic()
    deadline = base + 1.0

    def sampler(root, prefix, exts):
        counter["n"] += 1
        if counter["n"] < 2:
            return []
        p = root / f"data_{counter['n']}.dlf"
        p.write_bytes(b"x" * 64)
        return [
            CaptureBinary(
                path=p, size_bytes=64, sha256=sha256_of_file(p), segment_index=counter["n"]
            )
        ]

    def alive():
        return True

    config = MonitorConfig(
        deadline_monotonic=deadline,
        interval_seconds=0.05,
        readiness_timeout_seconds=0.5,
        stop_timeout_seconds=0.5,
        flush_timeout_seconds=0.5,
        min_free_bytes=0,
        require_data=True,
    )
    result = monitor_capture(
        capture_root=tmp_path,
        config=config,
        expected_prefix="data_",
        expected_extensions=(".dlf",),
        file_sampler=sampler,
        application_alive_fn=alive,
        clock=time.monotonic,
    )
    assert result.completed_normally
    assert result.captured_segments


def test_monitor_reports_stall_when_no_data_before_deadline(tmp_path: Path) -> None:
    base = time.monotonic()
    deadline = base + 0.5

    def sampler(root, prefix, exts):
        return []

    def alive():
        return True

    config = MonitorConfig(
        deadline_monotonic=deadline,
        interval_seconds=0.05,
        readiness_timeout_seconds=10.0,
        stop_timeout_seconds=10.0,
        flush_timeout_seconds=10.0,
        min_free_bytes=0,
        require_data=True,
    )
    result = monitor_capture(
        capture_root=tmp_path,
        config=config,
        expected_prefix="x",
        expected_extensions=(".dlf",),
        file_sampler=sampler,
        application_alive_fn=alive,
        clock=time.monotonic,
    )
    assert not result.completed_normally
    assert "no data" in (result.stall_reason or "")


def test_monitor_free_space_gate(tmp_path: Path) -> None:
    base = time.monotonic()
    deadline = base + 5.0

    def sampler(root, prefix, exts):
        return []

    def alive():
        return True

    def free_bytes(p):
        return 0

    config = MonitorConfig(
        deadline_monotonic=deadline,
        interval_seconds=0.05,
        readiness_timeout_seconds=0.5,
        stop_timeout_seconds=0.5,
        flush_timeout_seconds=0.5,
        min_free_bytes=10_000_000,
        require_data=True,
    )
    result = monitor_capture(
        capture_root=tmp_path,
        config=config,
        expected_prefix="x",
        expected_extensions=(".dlf",),
        file_sampler=sampler,
        application_alive_fn=alive,
        clock=time.monotonic,
        free_bytes_fn=free_bytes,
    )
    assert not result.completed_normally
    assert "free space" in (result.stall_reason or "")


def test_wait_for_flush_returns_stable(tmp_path: Path) -> None:
    p = tmp_path / "stable.dlf"
    p.write_bytes(b"hello")

    def sampler(root, prefix, exts):
        return [
            CaptureBinary(
                path=p, size_bytes=p.stat().st_size, sha256=sha256_of_file(p), segment_index=0
            )
        ]

    stable, files, obs = wait_for_flush(
        file_sampler=sampler,
        capture_root=tmp_path,
        expected_prefix="stable",
        expected_extensions=(".dlf",),
        timeout_seconds=2.0,
        stable_observations=2,
        poll_interval_seconds=0.05,
    )
    assert stable
    assert files


# ---------------------------------------------------------------------------
# Lock contention / cooperative release
# ---------------------------------------------------------------------------


def test_lock_blocks_concurrent_acquire(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    with acquired(run_dir, run_id="a", stale_lock_seconds=3600):
        other = RunLock(run_dir, stale_lock_seconds=3600)
        with pytest.raises(LockAcquisitionError):
            other.acquire(run_id="b")


def test_lock_released_by_context_manager(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    with acquired(run_dir, run_id="a", stale_lock_seconds=3600):
        pass
    # Now a fresh acquire from a different RunLock must succeed.
    other = RunLock(run_dir, stale_lock_seconds=3600)
    other.acquire(run_id="b")
    other.release()


# ---------------------------------------------------------------------------
# Capability report metadata
# ---------------------------------------------------------------------------


def test_capability_report_serializes() -> None:
    cap = CapabilityReport(
        qxdm_path=Path("qxdm.exe"),
        qcat_path=Path("qcat.exe"),
        qxdm_binding="x",
        qcat_binding="y",
        native_rotation_supported=True,
        binary_format_evidence=frozenset({".dlf"}),
        qcat_export_evidence=True,
        free_space_bytes=100,
        blocking_reasons=("x",),
        notes=("y",),
    )
    d = cap.to_dict()
    assert d["qxdm_path"].endswith("qxdm.exe")
    assert d["qcat_path"].endswith("qcat.exe")
    assert ".dlf" in d["binary_format_evidence"]
    assert "x" in d["blocking_reasons"]


# ---------------------------------------------------------------------------
# Orchestrator: resume on missing manifest fails clearly
# ---------------------------------------------------------------------------


def test_resume_with_no_manifest_returns_failure(tmp_workspace) -> None:
    cfg, bundle = make_orchestrator(tmp_workspace, capture_duration=0.5, monitor_interval=0.05)
    result = bundle.orch.resume_conversion()
    assert not result.success
    assert "no manifest" in (result.failure_reason or "").lower()


# ---------------------------------------------------------------------------
# Orchestrator: cancel before start aborts cleanly
# ---------------------------------------------------------------------------


def test_cancel_before_run(tmp_workspace) -> None:
    cfg, bundle = make_orchestrator(tmp_workspace, capture_duration=1.0, monitor_interval=0.05)
    bundle.cancel.cancel()
    result = bundle.orch.run()
    # Cancellation before lock acquisition surfaces as a failed run.
    assert not result.success
    assert result.state in {LifecycleState.CANCELLED, LifecycleState.BLOCKED, LifecycleState.FAILED}
