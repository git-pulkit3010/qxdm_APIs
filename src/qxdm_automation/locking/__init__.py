"""Single-owner lock with stale-recovery semantics.

A live run mutates the QXDM instance, the device, the file system, and the
output folder.  Two concurrent invocations must not both own the same
device.  We use a file-based lock stored in the run directory:

* The lock is a JSON file written atomically with a PID, host, start time,
  and a session nonce.
* Acquisition is non-blocking (we never wait silently).
* If the existing lock belongs to a *live* process on this host, we refuse
  to steal it and surface :class:`LockAcquisitionError`.
* If the existing lock is older than ``stale_lock_seconds`` *or* the
  recorded PID is no longer running, the orchestrator treats it as
  recoverable.  It does not delete the file; it records the recovery in
  the new lock's ``recovered_from`` field.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import time
from calendar import timegm
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from ..errors import LockAcquisitionError
from ..utils import atomic_write_text, normalize_path

try:  # Python 3.9+
    from typing import Final
except ImportError:  # pragma: no cover
    Final = object  # type: ignore[assignment]


def _pid_alive(pid: int) -> bool:
    """Return True if a process with *pid* is running on this host.

    Implementation note: on Windows we use a best-effort ``OpenProcess``
    probe via :mod:`ctypes`.  On POSIX, signal 0 is the traditional
    probe.  The function never raises.
    """

    if pid is None or pid <= 0:
        return False
    system = platform.system()
    if system == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
            )
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                if not ok:
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        import signal

        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False
    except Exception:
        return False


@dataclass
class LockRecord:
    """The contents of a lock file."""

    pid: int
    host: str
    started_at: str
    run_id: str
    session_nonce: str
    recovered_from: Optional[dict] = None
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "host": self.host,
            "started_at": self.started_at,
            "run_id": self.run_id,
            "session_nonce": self.session_nonce,
            "recovered_from": self.recovered_from,
            "extras": self.extras,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LockRecord":
        return cls(
            pid=int(d["pid"]),
            host=str(d["host"]),
            started_at=str(d["started_at"]),
            run_id=str(d["run_id"]),
            session_nonce=str(d["session_nonce"]),
            recovered_from=d.get("recovered_from"),
            extras=dict(d.get("extras", {})),
        )


@dataclass
class LockInspection:
    """The result of inspecting an existing lock file."""

    exists: bool
    record: Optional[LockRecord]
    age_seconds: Optional[float]
    live: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "exists": self.exists,
            "record": self.record.to_dict() if self.record else None,
            "age_seconds": self.age_seconds,
            "live": self.live,
            "reason": self.reason,
        }


class RunLock:
    """A file-based single-owner lock.

    The lock file is written under ``run_dir/run.lock``.  A :class:`RunLock`
    instance is *not* thread-safe by itself; it is intended to be used from
    a single owner process.
    """

    FILENAME: Final = "run.lock"

    def __init__(self, run_dir: Path, *, stale_lock_seconds: float) -> None:
        self.run_dir = normalize_path(run_dir)
        self.stale_lock_seconds = float(stale_lock_seconds)
        self._path = self.run_dir / self.FILENAME
        self._record: Optional[LockRecord] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record(self) -> Optional[LockRecord]:
        return self._record

    # ---- Inspection -----------------------------------------------------
    def inspect(self) -> LockInspection:
        if not self._path.exists():
            return LockInspection(
                exists=False,
                record=None,
                age_seconds=None,
                live=False,
                reason="no lock file",
            )
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            return LockInspection(
                exists=True,
                record=None,
                age_seconds=None,
                live=False,
                reason=f"unreadable lock: {exc}",
            )
        rec = LockRecord.from_dict(data)
        try:
            started = timegm(time.strptime(rec.started_at, "%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            started = time.time()
        age = time.time() - started
        alive = _pid_alive(rec.pid) and rec.host == socket.gethostname()
        if alive and age < self.stale_lock_seconds:
            reason = "lock held by a live process on this host"
        elif alive:
            reason = (
                f"lock is held by a live process but is older than "
                f"{self.stale_lock_seconds:.0f}s — eligible for recovery"
            )
        elif not alive and rec.host == socket.gethostname():
            reason = "lock owner PID is no longer alive — eligible for recovery"
        else:
            reason = (
                f"lock owner is on a different host ({rec.host!r}); cannot "
                "verify liveness"
            )
        return LockInspection(
            exists=True,
            record=rec,
            age_seconds=age,
            live=alive and rec.host == socket.gethostname(),
            reason=reason,
        )

    # ---- Acquisition ----------------------------------------------------
    def acquire(
        self,
        *,
        run_id: str,
        session_nonce: Optional[str] = None,
        allow_recovery: bool = True,
        extras: Optional[dict] = None,
    ) -> LockRecord:
        """Acquire the lock.

        Raises :class:`LockAcquisitionError` if the lock is held by a live
        process on this host.  If the existing lock is stale (the PID is
        gone or it is older than the configured threshold), a new lock is
        written and the previous record is preserved in the new lock's
        ``recovered_from`` field.
        """

        inspection = self.inspect()
        recovered: Optional[dict] = None

        if inspection.exists:
            if inspection.live and not (
                inspection.age_seconds is not None
                and inspection.age_seconds > self.stale_lock_seconds
            ):
                raise LockAcquisitionError(
                    f"run lock is held: {inspection.reason}"
                )
            if not allow_recovery:
                raise LockAcquisitionError(
                    f"run lock exists and recovery is disabled: {inspection.reason}"
                )
            recovered = inspection.to_dict()

        nonce = session_nonce or f"{os.getpid()}-{time.time_ns()}"
        rec = LockRecord(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            run_id=run_id,
            session_nonce=nonce,
            recovered_from=recovered,
            extras=dict(extras or {}),
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, json.dumps(rec.to_dict(), indent=2, sort_keys=True))
        self._record = rec
        return rec

    def release(self) -> None:
        """Release the lock if it belongs to this process.

        The orchestrator only ever releases its own lock — we never delete
        somebody else's lock file.
        """

        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        if int(data.get("pid", -1)) != os.getpid():
            return
        try:
            self._path.unlink()
        except OSError:
            pass
        self._record = None


@contextmanager
def acquired(
    run_dir: Path,
    *,
    run_id: str,
    stale_lock_seconds: float,
    allow_recovery: bool = True,
    extras: Optional[dict] = None,
) -> Iterator[RunLock]:
    """Context manager that acquires and releases a :class:`RunLock`."""

    lock = RunLock(run_dir, stale_lock_seconds=stale_lock_seconds)
    lock.acquire(
        run_id=run_id,
        allow_recovery=allow_recovery,
        extras=extras,
    )
    try:
        yield lock
    finally:
        lock.release()


__all__ = [
    "RunLock",
    "LockRecord",
    "LockInspection",
    "acquired",
]
