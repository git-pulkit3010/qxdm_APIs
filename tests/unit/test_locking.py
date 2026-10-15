"""Unit tests for the single-owner run lock."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from qxdm_automation.errors import LockAcquisitionError
from qxdm_automation.locking import RunLock, acquired


def test_lock_acquire_and_release(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    lock = RunLock(run_dir, stale_lock_seconds=3600)
    rec = lock.acquire(run_id="r1")
    assert lock.path.exists()
    assert rec.pid == os.getpid()
    lock.release()
    assert not lock.path.exists()


def test_second_acquire_blocks_live_process(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    lock = RunLock(run_dir, stale_lock_seconds=3600)
    lock.acquire(run_id="r1")
    lock2 = RunLock(run_dir, stale_lock_seconds=3600)
    with pytest.raises(LockAcquisitionError):
        lock2.acquire(run_id="r2")


def test_stale_lock_is_recoverable(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    # Plant a lock whose PID is not running.
    stale = {
        "pid": 999999,
        "host": "some-other-host",  # different host so we can't steal
        "started_at": "2000-01-01T00:00:00Z",
        "run_id": "r1",
        "session_nonce": "x",
        "recovered_from": None,
        "extras": {},
    }
    (run_dir / "run.lock").write_text(json.dumps(stale))
    lock = RunLock(run_dir, stale_lock_seconds=3600)
    # Recovery is allowed by default
    new_rec = lock.acquire(run_id="r2", allow_recovery=True)
    assert new_rec.recovered_from is not None
    assert new_rec.pid == os.getpid()


def test_recovery_disabled_blocks_stale_lock(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    stale = {
        "pid": 999999,
        "host": "different",
        "started_at": "2000-01-01T00:00:00Z",
        "run_id": "r1",
        "session_nonce": "x",
        "recovered_from": None,
        "extras": {},
    }
    (run_dir / "run.lock").write_text(json.dumps(stale))
    lock = RunLock(run_dir, stale_lock_seconds=3600)
    with pytest.raises(LockAcquisitionError):
        lock.acquire(run_id="r2", allow_recovery=False)


def test_inspect_unreadable_lock(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "run.lock").write_text("{not json", encoding="utf-8")
    lock = RunLock(run_dir, stale_lock_seconds=3600)
    insp = lock.inspect()
    assert insp.exists
    assert insp.record is None
    assert "unreadable" in insp.reason


def test_context_manager_releases(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    with acquired(run_dir, run_id="r1", stale_lock_seconds=3600) as lock:
        assert lock.path.exists()
    assert not lock.path.exists()
