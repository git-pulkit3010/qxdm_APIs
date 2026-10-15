"""Capture monitor and deadline-driven helpers.

The monitor encapsulates the "is capture healthy?" question.  It consumes
an injectable clock and a list of file-system snapshots, so the test
suite can drive deterministic schedules.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from ..types import CaptureBinary, utcnow
from ..utils import file_size, normalize_path


@dataclass
class MonitorSample:
    timestamp: float
    files: List[CaptureBinary]
    total_bytes: int
    application_alive: bool
    free_bytes: int


@dataclass
class MonitorConfig:
    deadline_monotonic: float
    interval_seconds: float
    readiness_timeout_seconds: float
    stop_timeout_seconds: float
    flush_timeout_seconds: float
    min_free_bytes: int = 0
    require_data: bool = True

    @property
    def readiness_deadline_monotonic(self) -> float:
        return self.deadline_monotonic  # readiness is gated by the deadline


@dataclass
class MonitorResult:
    completed_normally: bool
    captured_segments: List[CaptureBinary]
    samples: List[MonitorSample] = field(default_factory=list)
    stall_reason: Optional[str] = None


class CancellationToken:
    """A simple thread-safe cancellation flag.

    The capture monitor polls this between samples; the CLI sets it when
    the operator hits Ctrl+C.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            from ..errors import CancellationRequested

            raise CancellationRequested("operator cancellation requested")


ClockFn = Callable[[], float]


def monitor_capture(
    *,
    capture_root: Path,
    config: MonitorConfig,
    expected_prefix: str,
    expected_extensions: Sequence[str],
    file_sampler: Callable[[Path, str, Sequence[str]], List[CaptureBinary]],
    application_alive_fn: Callable[[], bool],
    clock: ClockFn = time.monotonic,
    cancellation: Optional[CancellationToken] = None,
    file_size_fn: Callable[[Path], int] = file_size,
    free_bytes_fn: Callable[[Path], int] = lambda p: 0,
) -> MonitorResult:
    """Run the capture monitor until the deadline elapses or cancellation.

    The *file_sampler* is responsible for the run-scoped file inventory at
    each sample.  It is supplied by the orchestrator so this monitor can
    stay decoupled from hashing.
    """

    root = normalize_path(capture_root)
    root.mkdir(parents=True, exist_ok=True)
    start = clock()
    samples: List[MonitorSample] = []
    first_data_at: Optional[float] = None
    last_data_at: Optional[float] = None
    segments: List[CaptureBinary] = []

    while True:
        now = clock()
        cancellation and cancellation.raise_if_cancelled()

        # Snapshot first so we always have a sample that reflects current
        # state, then evaluate the deadline.
        snapshot = file_sampler(root, expected_prefix, expected_extensions)
        sizes = sum(c.size_bytes for c in snapshot)
        free = free_bytes_fn(root)
        sample = MonitorSample(
            timestamp=now,
            files=list(snapshot),
            total_bytes=sizes,
            application_alive=application_alive_fn(),
            free_bytes=free,
        )
        samples.append(sample)

        if snapshot:
            if first_data_at is None:
                first_data_at = now
            last_data_at = now
            segments = list(snapshot)

        # Application health gate.
        if not sample.application_alive:
            return MonitorResult(
                completed_normally=False,
                captured_segments=segments,
                samples=samples,
                stall_reason="application/process is not alive",
            )

        # Free-space gate.
        if config.min_free_bytes and free < config.min_free_bytes:
            return MonitorResult(
                completed_normally=False,
                captured_segments=segments,
                samples=samples,
                stall_reason=f"free space {free} below minimum {config.min_free_bytes}",
            )

        # Deadline evaluation -----------------------------------------
        deadline_reached = now >= config.deadline_monotonic
        if deadline_reached and last_data_at is not None:
            return MonitorResult(
                completed_normally=True,
                captured_segments=segments,
                samples=samples,
            )
        if deadline_reached and not config.require_data:
            return MonitorResult(
                completed_normally=True,
                captured_segments=segments,
                samples=samples,
            )
        if deadline_reached and config.require_data and first_data_at is None:
            return MonitorResult(
                completed_normally=False,
                captured_segments=segments,
                samples=samples,
                stall_reason=(
                    f"deadline reached with no data; first_data_at={first_data_at}"
                ),
            )
        if (
            first_data_at is not None
            and (now - first_data_at) >= config.readiness_timeout_seconds
        ):
            # readiness satisfied; keep monitoring
            pass

        # Sleep until the next interval.
        next_at = now + max(0.0, config.interval_seconds)
        while clock() < next_at and clock() < config.deadline_monotonic:
            cancellation and cancellation.raise_if_cancelled()
            time.sleep(min(0.05, max(0.0, next_at - clock())))


def wait_for_flush(
    *,
    file_sampler: Callable[[Path, str, Sequence[str]], List[CaptureBinary]],
    capture_root: Path,
    expected_prefix: str,
    expected_extensions: Sequence[str],
    timeout_seconds: float,
    stable_observations: int = 3,
    poll_interval_seconds: float = 0.5,
    clock: ClockFn = time.monotonic,
    cancellation: Optional[CancellationToken] = None,
) -> tuple:
    """Wait for file sizes to stabilise for *stable_observations* consecutive samples.

    Returns ``(stable: bool, final_files: list, observed: int)``.
    """

    root = normalize_path(capture_root)
    end = clock() + timeout_seconds
    previous: List[CaptureBinary] = []
    stable = 0
    while True:
        if clock() > end:
            return (False, previous, stable)
        cancellation and cancellation.raise_if_cancelled()
        current = file_sampler(root, expected_prefix, expected_extensions)
        if current and _same_inventory(previous, current):
            stable += 1
            if stable >= stable_observations:
                return (True, current, stable)
        else:
            stable = 0
        previous = current
        time.sleep(poll_interval_seconds)


def _same_inventory(a: Sequence[CaptureBinary], b: Sequence[CaptureBinary]) -> bool:
    """Compare two file inventories by (path, size).

    The mtime check is left to the caller; this function is meant to be
    cheap.
    """

    if len(a) != len(b):
        return False
    a_map = {c.path: c.size_bytes for c in a}
    b_map = {c.path: c.size_bytes for c in b}
    return a_map == b_map


__all__ = [
    "MonitorSample",
    "MonitorConfig",
    "MonitorResult",
    "CancellationToken",
    "monitor_capture",
    "wait_for_flush",
]
