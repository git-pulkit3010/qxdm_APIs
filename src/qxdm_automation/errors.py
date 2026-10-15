"""Typed exception hierarchy for the QXDM automation backend.

The hierarchy is deliberately narrow and contextual.  Every exception carries a
``code`` attribute so logs, manifests, and CLI exit handling can match on a
stable identifier rather than on a message string.  Errors that originate from a
vendor integration are :class:`VendorIntegrationError` subclasses so the
orchestrator can distinguish "the tool refused" from "we got the configuration
wrong" without parsing text.
"""

from __future__ import annotations

from typing import Optional


class QxdmAutomationError(Exception):
    """Root of the public error hierarchy."""

    code: str = "qxdm_automation_error"

    def __init__(self, message: str, *, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.cause = cause


class ConfigurationError(QxdmAutomationError):
    """Operator-supplied configuration is invalid or incomplete."""

    code = "configuration_error"


class PreflightFailed(QxdmAutomationError):
    """Capability / environment preflight detected an unrecoverable issue."""

    code = "preflight_failed"


class UnsupportedCapabilityError(QxdmAutomationError):
    """A vendor capability is required but the adapter does not implement it.

    The orchestrator converts this to a typed failure that aborts *before* any
    irreversible live operation.  Simulation adapters never raise this; real
    QXDM / QCAT adapters raise it on every method until a documented binding is
    wired in.
    """

    code = "unsupported_capability"


class VendorIntegrationError(QxdmAutomationError):
    """A vendor integration call failed for a vendor-specific reason."""

    code = "vendor_integration_error"


class TargetResolutionError(QxdmAutomationError):
    """The diagnostic target could not be unambiguously resolved."""

    code = "target_resolution_error"


class AttachmentError(QxdmAutomationError):
    """The tool/process could not be launched or attached to."""

    code = "attachment_error"


class CaptureStartError(QxdmAutomationError):
    """The capture start sequence failed or could not be confirmed."""

    code = "capture_start_error"


class CaptureVerificationError(QxdmAutomationError):
    """The capture did not produce the evidence the orchestrator requires."""

    code = "capture_verification_error"


class CaptureFinalizationError(QxdmAutomationError):
    """The capture could not be cleanly finalised."""

    code = "capture_finalization_error"


class ConversionError(QxdmAutomationError):
    """A QCAT conversion step failed."""

    code = "conversion_error"


class PublicationError(QxdmAutomationError):
    """The TXT handoff failed."""

    code = "publication_error"


class LockAcquisitionError(QxdmAutomationError):
    """The single-owner lock could not be obtained."""

    code = "lock_acquisition_error"


class StaleLockError(QxdmAutomationError):
    """A lock was found and rejected as stale after a graceful probe failed.

    Stale locks are not silently stolen; the orchestrator must surface them.
    """

    code = "stale_lock_error"


class CancellationRequested(QxdmAutomationError):
    """A cancellation was observed.  Treated as a soft failure by the driver."""

    code = "cancelled"


__all__ = [
    "QxdmAutomationError",
    "ConfigurationError",
    "PreflightFailed",
    "UnsupportedCapabilityError",
    "VendorIntegrationError",
    "TargetResolutionError",
    "AttachmentError",
    "CaptureStartError",
    "CaptureVerificationError",
    "CaptureFinalizationError",
    "ConversionError",
    "PublicationError",
    "LockAcquisitionError",
    "StaleLockError",
    "CancellationRequested",
]
