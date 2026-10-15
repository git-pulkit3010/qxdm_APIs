"""CompNova QXDM automation backend.

Public package surface. The full implementation is organised as follows:

* :mod:`qxdm_automation.errors`           — typed, contextual exception hierarchy.
* :mod:`qxdm_automation.types`            — immutable value types shared by every layer.
* :mod:`qxdm_automation.config`           — schema-validated configuration model.
* :mod:`qxdm_automation.adapters`         — vendor boundary (real adapters raise
  :class:`UnsupportedCapabilityError` until a documented binding exists; the
  behavioural :class:`SimulationQxdmAdapter` exercises the full orchestrator).
* :mod:`qxdm_automation.locking`          — single-owner locks with stale-recovery
  semantics.
* :mod:`qxdm_automation.manifest`         — atomic run manifest persistence.
* :mod:`qxdm_automation.monitoring`       — deadline-driven capture monitor.
* :mod:`qxdm_automation.discovery`        — diagnostic target resolution.
* :mod:`qxdm_automation.conversion`       — QCAT conversion adapter + TXT
  structural validation.
* :mod:`qxdm_automation.publication`      — crash-safe TXT handoff.
* :mod:`qxdm_automation.orchestration`    — lifecycle state machine + driver.

The orchestration is importable and testable on any platform; vendor / Windows
specific dependencies live behind lazy imports in the adapter package.
"""

from __future__ import annotations

__all__ = [
    "errors",
    "types",
    "config",
    "adapters",
    "locking",
    "manifest",
    "monitoring",
    "discovery",
    "conversion",
    "publication",
    "orchestration",
]

__version__ = "0.1.0"
