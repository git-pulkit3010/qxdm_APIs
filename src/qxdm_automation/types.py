"""Shared immutable value types used across the automation stack.

The lifecycle states are the single source of truth for run progress; every
component (state machine, manifest, lock file, CLI status output) must import
them from here so the same string never drifts between subsystems.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import FrozenSet, Optional


def utcnow() -> datetime:
    """Return the current UTC wall-clock time."""

    return datetime.now(timezone.utc)


class LifecycleState(str, enum.Enum):
    """Mandatory capture lifecycle.

    Forward-only transitions, with a terminal cluster of completed / failed /
    cancelled / blocked.  The orchestrator is the only writer; the manifest
    module serialises these as strings to keep JSON portable.
    """

    CREATED = "CREATED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    TOOL_READY = "TOOL_READY"
    TARGET_READY = "TARGET_READY"
    CONFIG_READY = "CONFIG_READY"
    CAPTURE_CONFIGURED = "CAPTURE_CONFIGURED"
    START_REQUESTED = "START_REQUESTED"
    CAPTURING = "CAPTURING"
    STOP_REQUESTED = "STOP_REQUESTED"
    FINALIZING = "FINALIZING"
    CAPTURE_FINALIZED = "CAPTURE_FINALIZED"
    CONVERTING = "CONVERTING"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"

    # Terminal failure / cancellation states.
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def is_successful_terminal(self) -> bool:
        return self is LifecycleState.COMPLETED


_TERMINAL_STATES: FrozenSet[LifecycleState] = frozenset(
    {
        LifecycleState.COMPLETED,
        LifecycleState.BLOCKED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
        LifecycleState.INTERRUPTED,
    }
)


class Mode(str, enum.Enum):
    """Operator-declared execution mode.

    * ``offline`` — never touch a real QXDM/QCAT; the simulation adapter
      exercises the orchestration end-to-end on the current host.
    * ``planning`` — pure dry-run.  Validates configuration, capability
      preflight, and produces a run plan + capability report; does not create
      a run directory or start anything.
    * ``live`` — invokes real vendor adapters.  Blocked before a documented
      binding exists; the adapter itself is what raises the typed error.
    """

    OFFLINE = "offline"
    PLANNING = "planning"
    LIVE = "live"

    @classmethod
    def from_string(cls, value: str) -> "Mode":
        try:
            return cls(value.strip().lower())
        except ValueError as exc:
            raise ValueError(
                f"unknown mode {value!r}; expected one of {[m.value for m in cls]}"
            ) from exc


class RetentionPolicy(str, enum.Enum):
    """What to do with the raw binaries once TXT handoff succeeds."""

    RETAIN = "retain"  # default — never delete
    DELETE_ON_SUCCESS = "delete_on_success"  # only after publication confirmed


@dataclass(frozen=True)
class CaptureBinary:
    """Information about a single raw capture binary file."""

    path: Path
    size_bytes: int
    sha256: str
    segment_index: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "segment_index": self.segment_index,
        }


@dataclass(frozen=True)
class TextArtifact:
    """Information about a single published TXT output."""

    source_binary: Path
    text_path: Path
    size_bytes: int
    sha256: str
    encoding: str

    def to_dict(self) -> dict:
        return {
            "source_binary": str(self.source_binary),
            "text_path": str(self.text_path),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "encoding": self.encoding,
        }


@dataclass(frozen=True)
class CapabilityReport:
    """The result of a capability preflight."""

    qxdm_path: Optional[Path]
    qcat_path: Optional[Path]
    qxdm_binding: str
    qcat_binding: str
    native_rotation_supported: bool
    binary_format_evidence: FrozenSet[str]
    qcat_export_evidence: bool
    free_space_bytes: int
    blocking_reasons: tuple = field(default_factory=tuple)
    notes: tuple = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.blocking_reasons

    def to_dict(self) -> dict:
        return {
            "qxdm_path": str(self.qxdm_path) if self.qxdm_path else None,
            "qcat_path": str(self.qcat_path) if self.qcat_path else None,
            "qxdm_binding": self.qxdm_binding,
            "qcat_binding": self.qcat_binding,
            "native_rotation_supported": self.native_rotation_supported,
            "binary_format_evidence": sorted(self.binary_format_evidence),
            "qcat_export_evidence": self.qcat_export_evidence,
            "free_space_bytes": self.free_space_bytes,
            "blocking_reasons": list(self.blocking_reasons),
            "notes": list(self.notes),
        }


__all__ = [
    "utcnow",
    "LifecycleState",
    "Mode",
    "RetentionPolicy",
    "CaptureBinary",
    "TextArtifact",
    "CapabilityReport",
]
