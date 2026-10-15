"""Vendor adapter boundaries.

Every vendor integration is hidden behind a small typed protocol.  The
*real* QXDM and QCAT adapters raise :class:`UnsupportedCapabilityError`
on every method until a documented binding is wired in; this is the
required pattern: a ``live`` run is rejected by the adapter itself, *not*
by stub methods that return success.

The :class:`SimulationQxdmAdapter` and :class:`SimulationQcatAdapter`
exercise the full orchestrator with deterministic state machines, fake
binary files, and a synthetic converter fixture.  Real runs must never
instantiate the simulation adapters.

GUI automation is **not** implemented in this run.  See
``docs/operator_guide.md`` for the inspection procedure needed before a
GUI fallback can be built for a specific QXDM build.
"""

from __future__ import annotations

import abc
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from ..errors import (
    AttachmentError,
    CaptureFinalizationError,
    CaptureStartError,
    CaptureVerificationError,
    ConfigurationError,
    ConversionError,
    PreflightFailed,
    TargetResolutionError,
    UnsupportedCapabilityError,
    VendorIntegrationError,
)
from ..types import CapabilityReport, CaptureBinary
from ..utils import sha256_of_file

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class QxdmAdapter(abc.ABC):
    """Vendor-level operations for QXDM.

    Method names are *internal contracts*.  They are not claims about
    Qualcomm API signatures.  Each method has a documented return shape
    that the orchestrator depends on.
    """

    binding: str

    @abc.abstractmethod
    def launch_or_attach(
        self,
        *,
        qxdm_path: Optional[Path],
        attachment: str,
        run_dir: Path,
    ) -> "QxdmSession":
        """Launch (or attach to) a QXDM instance.

        *attachment* is ``"owned"`` (the orchestrator owns the lifecycle of
        the process) or ``"attach"`` (attach to a user-owned instance; the
        orchestrator must not close it on exit).

        Returns a :class:`QxdmSession` whose :attr:`session_id` is unique
        enough to distinguish PID + start-time + a session nonce.
        """

    @abc.abstractmethod
    def inventory(
        self,
        *,
        qxdm_path: Optional[Path],
    ) -> CapabilityReport:
        """Inspect what the local install can do without launching the tool.

        Must not start the vendor application.  Configuration validation
        relies on this so the operator cannot accidentally instantiate
        a vendor automation object.
        """


class QxdmSession(abc.ABC):
    """A live QXDM session owned by the orchestrator.

    The session is closed by :meth:`close` exactly once.  After ``close``
    any further method call raises :class:`VendorIntegrationError`.
    """

    session_id: str
    pid: Optional[int]
    closed: bool

    @abc.abstractmethod
    def connect_target(
        self,
        *,
        selector: Optional[str],
        candidates: Sequence[str],
    ) -> "TargetHandle":
        """Resolve and connect to one diagnostic target.

        ``selector`` is the operator-supplied explicit choice (e.g. ``"COM15"``).
        ``candidates`` is a list of discovery hints — the adapter decides
        whether the hints are sufficient to pick one unambiguous target.
        """

    @abc.abstractmethod
    def load_configuration(
        self,
        *,
        config_path: Path,
        config_sha256: str,
    ) -> "ConfigurationResult":
        """Load a QXDM configuration file and verify acceptance."""

    @abc.abstractmethod
    def configure_capture(
        self,
        *,
        capture_root: Path,
        filename_prefix: str,
        rotation_bytes: int,
        binary_format: str,
    ) -> "CaptureConfiguration":
        """Configure the binary capture (path, prefix, rotation, format)."""

    @abc.abstractmethod
    def start_capture(self) -> "CaptureHandle":
        """Start capture.  Records the start request *before* invoking."""

    @abc.abstractmethod
    def stop_capture(self, handle: "CaptureHandle") -> None:
        """Stop the running capture."""

    @abc.abstractmethod
    def finalize_capture(
        self,
        handle: "CaptureHandle",
        *,
        flush_deadline_seconds: float,
    ) -> List[CaptureBinary]:
        """Wait for finalisation and enumerate run-scoped binary files."""

    @abc.abstractmethod
    def close(self) -> None:
        """Close the session.  Idempotent."""


class QcatAdapter(abc.ABC):
    """QCAT export adapter."""

    binding: str

    @abc.abstractmethod
    def inventory(
        self,
        *,
        qcat_path: Optional[Path],
    ) -> CapabilityReport:
        """Inspect QCAT capabilities without launching the converter."""

    @abc.abstractmethod
    def convert(
        self,
        *,
        source: Path,
        output: Path,
        timeout_seconds: float,
    ) -> "ConversionResult":
        """Convert a single source binary to a TXT file.

        Returns a :class:`ConversionResult` whose ``output_path`` is the
        actual file produced (or ``None`` if no file was produced).  The
        adapter must also return a textual evidence trail listing the
        strongest completion signal it observed.
        """


# ---------------------------------------------------------------------------
# Value objects exchanged across the boundary
# ---------------------------------------------------------------------------


@dataclass
class TargetHandle:
    selector: str
    description: str


@dataclass
class ConfigurationResult:
    accepted: bool
    evidence: str
    error: Optional[str] = None


@dataclass
class CaptureConfiguration:
    capture_root: Path
    filename_prefix: str
    rotation_bytes: int
    binary_format: str


@dataclass
class CaptureHandle:
    started_at_monotonic: float
    started_at_wallclock: str
    capture_dir: Path
    filename_prefix: str


@dataclass
class ConversionResult:
    returncode: int
    stdout: str
    stderr: str
    output_path: Optional[Path]
    timed_out: bool
    evidence: str


# ---------------------------------------------------------------------------
# Real adapters — every method raises UnsupportedCapabilityError
# ---------------------------------------------------------------------------


class UnboundQxdmAdapter(QxdmAdapter):
    """Adapter for QXDM.  No method is implemented until a binding is wired.

    The adapter deliberately does not even *look* at a QXDM binary, so that
    ordinary configuration validation cannot accidentally start the tool.
    """

    binding = "unspecified"

    def __init__(self, binding: str = "unspecified") -> None:
        self.binding = binding

    def _unsupported(self, op: str) -> "UnsupportedCapabilityError":
        return UnsupportedCapabilityError(
            f"QXDM adapter has no documented binding for operation: {op!r}. "
            "Inspect the local QXDM installation, capture the registered "
            "type-library / COM ProgID / SDK signatures, and wire them into "
            "qxdm_automation.adapters.real_qxdm before live runs."
        )

    def launch_or_attach(
        self,
        *,
        qxdm_path: Optional[Path],
        attachment: str,
        run_dir: Path,
    ) -> QxdmSession:
        raise self._unsupported("launch_or_attach")

    def inventory(
        self,
        *,
        qxdm_path: Optional[Path],
    ) -> CapabilityReport:
        return CapabilityReport(
            qxdm_path=qxdm_path,
            qcat_path=None,
            qxdm_binding=self.binding,
            qcat_binding="unspecified",
            native_rotation_supported=False,
            binary_format_evidence=frozenset(),
            qcat_export_evidence=False,
            free_space_bytes=0,
            blocking_reasons=("qxdm binding is not implemented",),
            notes=("no documented QXDM signature is available",),
        )


class UnboundQcatAdapter(QcatAdapter):
    """Adapter for QCAT.  No method is implemented until a binding is wired."""

    binding = "unspecified"

    def __init__(self, binding: str = "unspecified") -> None:
        self.binding = binding

    def _unsupported(self, op: str) -> "UnsupportedCapabilityError":
        return UnsupportedCapabilityError(
            f"QCAT adapter has no documented binding for operation: {op!r}. "
            "Inspect the local QCAT install, capture the documented CLI / "
            "automation entry point and switch list, and wire them into "
            "qxdm_automation.adapters.real_qcat before live runs."
        )

    def inventory(
        self,
        *,
        qcat_path: Optional[Path],
    ) -> CapabilityReport:
        return CapabilityReport(
            qxdm_path=None,
            qcat_path=qcat_path,
            qxdm_binding="unspecified",
            qcat_binding=self.binding,
            native_rotation_supported=False,
            binary_format_evidence=frozenset(),
            qcat_export_evidence=False,
            free_space_bytes=0,
            blocking_reasons=("qcat binding is not implemented",),
            notes=("no documented QCAT signature or CLI is available",),
        )

    def convert(
        self,
        *,
        source: Path,
        output: Path,
        timeout_seconds: float,
    ) -> ConversionResult:
        raise self._unsupported("convert")


# ---------------------------------------------------------------------------
# Simulation adapters — exercise the full orchestrator without a real tool
# ---------------------------------------------------------------------------


@dataclass
class SimulatedTarget:
    selector: str
    description: str
    segments: List["SimulatedSegment"] = field(default_factory=list)
    data_written: int = 0
    paused: bool = False
    crashed: bool = False


@dataclass
class SimulatedSegment:
    index: int
    path: Path
    size: int
    finalized: bool = False


class SimulationQxdmAdapter(QxdmAdapter):
    """A behavioural fake of QXDM.

    Emits deterministic fake binary segments, applies the configured
    rotation, and exposes hooks for failure injection
    (``crash_after_start``, ``refuse_config``, ``no_data``, ``resume_after_pause``,
    etc.).
    """

    binding = "simulation"

    def __init__(
        self,
        *,
        target_selector: str = "COM-SIM-1",
        rotation_bytes: int = 4096,
        refuse_config: bool = False,
        crash_after_start: bool = False,
        no_data: bool = False,
        resume_after_pause: bool = False,
        stop_fails: bool = False,
        write_delay_seconds: float = 0.0,
        reject_all_targets: bool = False,
    ) -> None:
        self._target_selector = target_selector
        self._default_rotation = rotation_bytes
        self._refuse_config = refuse_config
        self._crash_after_start = crash_after_start
        self._no_data = no_data
        self._resume_after_pause = resume_after_pause
        self._stop_fails = stop_fails
        self._write_delay_seconds = write_delay_seconds
        self._reject_all_targets = reject_all_targets
        self._session_counter = 0

    # -- inventory ---------------------------------------------------------
    def inventory(
        self,
        *,
        qxdm_path: Optional[Path],
    ) -> CapabilityReport:
        return CapabilityReport(
            qxdm_path=qxdm_path or Path("SIMULATED_QXDM.exe"),
            qcat_path=None,
            qxdm_binding=self.binding,
            qcat_binding="simulation",
            native_rotation_supported=True,
            binary_format_evidence=frozenset({".dlf", ".bin", ".isf", ".hdf", ".qmdl"}),
            qcat_export_evidence=True,
            free_space_bytes=10**12,
            notes=("simulation adapter — capabilities are asserted, not measured",),
        )

    # -- launch ------------------------------------------------------------
    def launch_or_attach(
        self,
        *,
        qxdm_path: Optional[Path],
        attachment: str,
        run_dir: Path,
    ) -> QxdmSession:
        self._session_counter += 1
        session_id = f"sim-{os.getpid()}-{self._session_counter}-{int(time.time() * 1e6)}"
        return _SimQxdmSession(
            adapter=self,
            session_id=session_id,
            run_dir=run_dir,
            attachment=attachment,
        )


class _SimQxdmSession(QxdmSession):
    def __init__(
        self,
        *,
        adapter: SimulationQxdmAdapter,
        session_id: str,
        run_dir: Path,
        attachment: str,
    ) -> None:
        self._adapter = adapter
        self.session_id = session_id
        self.pid = os.getpid()  # simulation: share the orchestrator PID
        self.closed = False
        self._run_dir = run_dir
        self._attachment = attachment
        self._target: Optional[SimulatedTarget] = None
        self._config: Optional[CaptureConfiguration] = None
        self._handle: Optional[CaptureHandle] = None
        self._writer: Optional[_SimCaptureWriter] = None

    # -- target ------------------------------------------------------------
    def connect_target(
        self,
        *,
        selector: Optional[str],
        candidates: Sequence[str],
    ) -> TargetHandle:
        if self.closed:
            raise VendorIntegrationError("session is closed")
        if self._adapter._reject_all_targets:
            raise TargetResolutionError(
                "simulation: rejecting all targets (reject_all_targets=True)"
            )
        chosen = selector or self._adapter._target_selector
        if chosen not in {self._adapter._target_selector}:
            # If a candidate list is supplied, we accept matches; otherwise
            # we fall back to the configured selector.
            if chosen in candidates:
                pass
            else:
                raise TargetResolutionError(
                    f"selector {chosen!r} does not match the simulated target "
                    f"{self._adapter._target_selector!r} and is not in the "
                    f"candidate list {list(candidates)!r}"
                )
        self._target = SimulatedTarget(selector=chosen, description="simulated QXDM target")
        return TargetHandle(selector=chosen, description="simulated QXDM target")

    # -- configuration -----------------------------------------------------
    def load_configuration(
        self,
        *,
        config_path: Path,
        config_sha256: str,
    ) -> ConfigurationResult:
        if self.closed:
            raise VendorIntegrationError("session is closed")
        if self._adapter._refuse_config:
            return ConfigurationResult(
                accepted=False,
                evidence="simulation: refused by adapter",
                error="configuration explicitly rejected by simulation",
            )
        return ConfigurationResult(
            accepted=True,
            evidence=f"configuration hash {config_sha256[:12]} accepted by simulation",
        )

    # -- capture setup -----------------------------------------------------
    def configure_capture(
        self,
        *,
        capture_root: Path,
        filename_prefix: str,
        rotation_bytes: int,
        binary_format: str,
    ) -> CaptureConfiguration:
        if self.closed:
            raise VendorIntegrationError("session is closed")
        # Honour the operator's override of the simulation rotation size.
        effective_rotation = (
            rotation_bytes if rotation_bytes > 0 else self._adapter._default_rotation
        )
        self._config = CaptureConfiguration(
            capture_root=capture_root,
            filename_prefix=filename_prefix,
            rotation_bytes=effective_rotation,
            binary_format=binary_format,
        )
        return self._config

    # -- capture start -----------------------------------------------------
    def start_capture(self) -> CaptureHandle:
        if self.closed:
            raise VendorIntegrationError("session is closed")
        if self._config is None:
            raise CaptureStartError("capture was started before configure_capture")
        if self._adapter._crash_after_start:
            # Simulate a crash that prevents the start from being confirmed.
            self.closed = True
            raise CaptureStartError("simulation: QXDM process crashed during start")
        # Always record the start request *before* invoking.
        handle = CaptureHandle(
            started_at_monotonic=time.monotonic(),
            started_at_wallclock=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            capture_dir=self._config.capture_root,
            filename_prefix=self._config.filename_prefix,
        )
        if not self._adapter._no_data:
            # Start a background writer that actually produces files in
            # the capture root, so the production monitor sees real
            # activity.  Without this the monitor would never observe
            # any data and the run would stall.
            self._writer = _SimCaptureWriter(
                capture_root=self._config.capture_root,
                filename_prefix=self._config.filename_prefix,
                rotation_bytes=self._config.rotation_bytes,
                write_delay_seconds=self._adapter._write_delay_seconds,
            )
            self._writer.start()
        self._handle = handle
        return handle

    # -- stop --------------------------------------------------------------
    def stop_capture(self, handle: CaptureHandle) -> None:
        if self.closed:
            raise VendorIntegrationError("session is closed")
        if self._adapter._stop_fails:
            raise CaptureFinalizationError("simulation: stop failed")
        if self._writer is not None:
            self._writer.stop()
            self._writer.join(timeout=2.0)

    # -- finalise ----------------------------------------------------------
    def finalize_capture(
        self,
        handle: CaptureHandle,
        *,
        flush_deadline_seconds: float,
    ) -> List[CaptureBinary]:
        if self.closed:
            raise VendorIntegrationError("session is closed")
        if self._config is None or self._target is None:
            raise CaptureFinalizationError("capture was finalised before configuration")
        if self._adapter._no_data:
            return []
        capture_root = handle.capture_dir
        capture_root.mkdir(parents=True, exist_ok=True)
        # If the writer did not produce anything (e.g. write_delay was
        # too high), fall back to a one-shot materialisation.
        if self._writer is not None and not self._writer.files_produced:
            self._writer.write_one_segment()
        # Enumerate every run-scoped file.
        results: List[CaptureBinary] = []
        for entry in sorted(capture_root.iterdir()):
            if not entry.is_file():
                continue
            if not entry.name.startswith(self._config.filename_prefix):
                continue
            try:
                size = entry.stat().st_size
                digest = sha256_of_file(entry)
            except OSError:
                continue
            results.append(
                CaptureBinary(
                    path=entry,
                    size_bytes=size,
                    sha256=digest,
                    segment_index=_extract_segment_index(entry.name),
                )
            )
        results.sort(key=lambda b: (b.segment_index is None, b.segment_index or 0, str(b.path)))
        return results

    # -- close -------------------------------------------------------------
    def close(self) -> None:
        if self.closed:
            return
        if self._writer is not None:
            try:
                self._writer.stop()
                self._writer.join(timeout=2.0)
            except Exception:
                pass
        self.closed = True


def _extract_segment_index(name: str) -> Optional[int]:
    """Best-effort: pull a numeric segment index from a name.

    Recognises both ``prefix_000.dlf`` and ``prefix.000.dlf`` shapes.  The
    simulation writer uses ``prefix_<counter>.dlf`` with a six-digit
    counter so the sort order is stable even for large counts.
    """

    base = name.rsplit(".", 1)[0]
    parts = base.split("_")
    for part in reversed(parts):
        if part.isdigit():
            return int(part)
    return None


class _SimCaptureWriter:
    """Background writer that produces fake binary segments in capture_root.

    The writer rotates at *rotation_bytes*, uses *filename_prefix*, and
    produces at most *max_segments* segments.  Each segment is a
    deterministic byte stream so the resulting file is reproducible.
    """

    MAX_SEGMENTS = 8

    def __init__(
        self,
        *,
        capture_root: Path,
        filename_prefix: str,
        rotation_bytes: int,
        write_delay_seconds: float,
        max_segments: int = MAX_SEGMENTS,
    ) -> None:
        self._capture_root = Path(capture_root)
        self._prefix = filename_prefix
        self._rotation = max(1, int(rotation_bytes))
        self._write_delay = max(0.0, float(write_delay_seconds))
        self._max_segments = max(1, int(max_segments))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._counter = 0
        self._files_produced = 0
        self._lock = threading.Lock()

    @property
    def files_produced(self) -> int:
        with self._lock:
            return self._files_produced

    def start(self) -> None:
        self._capture_root.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name="sim-qxdm-writer", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def write_one_segment(self) -> None:
        """Synchronously materialise a single segment (used as a fallback)."""
        with self._lock:
            self._counter += 1
            idx = self._counter
        path = self._capture_root / f"{self._prefix}_{idx:06d}.dlf"
        data = bytes(((idx * 31 + j) & 0xFF) for j in range(self._rotation))
        path.write_bytes(data)
        with self._lock:
            self._files_produced += 1

    def _run(self) -> None:
        # Write chunks until the writer is told to stop or we exceed the
        # segment cap.  We bias towards producing a few small segments so
        # the monitor observes file activity.
        try:
            while not self._stop.is_set():
                self.write_one_segment()
                if self._files_produced >= self._max_segments:
                    break
                if self._write_delay:
                    self._stop.wait(self._write_delay)
                else:
                    # Yield to the monitor; very short so tests are fast.
                    self._stop.wait(0.02)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("sim writer raised: %s", exc)


class SimulationQcatAdapter(QcatAdapter):
    """A behavioural fake of QCAT.

    Writes a deterministic plain-text payload with a leading sentinel so
    the structural validator can confirm format independently of the real
    QCAT output.  The fixture is clearly labelled SYNTHETIC.
    """

    binding = "simulation"

    def __init__(
        self,
        *,
        fail_on: Optional[Iterable[str]] = None,
        delay_seconds: float = 0.0,
        output_corruption: bool = False,
        timeout_simulation: bool = False,
    ) -> None:
        self._fail_on = set(fail_on or ())
        self._delay_seconds = delay_seconds
        self._output_corruption = output_corruption
        self._timeout_simulation = timeout_simulation

    def inventory(
        self,
        *,
        qcat_path: Optional[Path],
    ) -> CapabilityReport:
        return CapabilityReport(
            qxdm_path=None,
            qcat_path=qcat_path or Path("SIMULATED_QCAT.exe"),
            qxdm_binding="simulation",
            qcat_binding=self.binding,
            native_rotation_supported=True,
            binary_format_evidence=frozenset({".dlf", ".bin", ".isf", ".hdf", ".qmdl"}),
            qcat_export_evidence=True,
            free_space_bytes=10**12,
            notes=("simulation adapter — capabilities are asserted, not measured",),
        )

    def convert(
        self,
        *,
        source: Path,
        output: Path,
        timeout_seconds: float,
    ) -> ConversionResult:
        if self._timeout_simulation:
            time.sleep(min(0.2, timeout_seconds * 0.5))
            return ConversionResult(
                returncode=-1,
                stdout="",
                stderr="simulation: conversion timed out",
                output_path=None,
                timed_out=True,
                evidence="simulated timeout",
            )
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        if source.name in self._fail_on:
            return ConversionResult(
                returncode=2,
                stdout="",
                stderr=f"simulation: refused to convert {source.name}",
                output_path=None,
                timed_out=False,
                evidence="simulated refused",
            )
        try:
            data = source.read_bytes()
        except OSError as exc:
            return ConversionResult(
                returncode=3,
                stdout="",
                stderr=f"unable to read source: {exc}",
                output_path=None,
                timed_out=False,
                evidence="simulated read failure",
            )
        # Build the synthetic TXT.  The sentinel allows the structural
        # validator to recognise the format without claiming real QCAT
        # compatibility.
        header = (
            "# QXDM-AUTOMATION SYNTHETIC CONVERTER OUTPUT\n"
            f"# source: {source.name}\n"
            f"# size_bytes: {len(data)}\n"
            f"# sha256: {sha256_of_file(source)}\n"
            "# WARNING: this file was produced by a simulation adapter.  It\n"
            "# is not a real QCAT export and does not prove decoder fidelity.\n"
        )
        body = "\n".join(
            f"ITEM 0x{((i * 13) & 0xFFFF):04X}  payload=0x{((b + i) & 0xFF):02X}  ts=2026-01-01T00:00:{(i % 60):02d}Z"
            for i, b in enumerate(data[:512])
        )
        if self._output_corruption:
            body = body[: max(0, len(body) // 2)]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(header + body + "\n", encoding="utf-8")
        return ConversionResult(
            returncode=0,
            stdout=f"wrote {output}",
            stderr="",
            output_path=output,
            timed_out=False,
            evidence="simulation: wrote synthetic text output",
        )


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------


def make_qxdm_adapter(binding: str) -> QxdmAdapter:
    """Return the QXDM adapter for the given binding name.

    Unknown bindings are treated as *unbound* (every method raises
    :class:`UnsupportedCapabilityError`).
    """

    if binding == "simulation":
        return SimulationQxdmAdapter()
    return UnboundQxdmAdapter(binding=binding)


def make_qcat_adapter(binding: str) -> QcatAdapter:
    if binding == "simulation":
        return SimulationQcatAdapter()
    return UnboundQcatAdapter(binding=binding)


__all__ = [
    "QxdmAdapter",
    "QxdmSession",
    "QcatAdapter",
    "TargetHandle",
    "ConfigurationResult",
    "CaptureConfiguration",
    "CaptureHandle",
    "ConversionResult",
    "UnboundQxdmAdapter",
    "UnboundQcatAdapter",
    "SimulationQxdmAdapter",
    "SimulationQcatAdapter",
    "make_qxdm_adapter",
    "make_qcat_adapter",
]
