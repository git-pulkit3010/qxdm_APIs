"""Diagnostic target discovery.

Discovery is treated as a *hint*, not a binding.  The orchestrator
expects either an explicit operator-supplied selector or a list of
candidates that the QXDM adapter can disambiguate.  Broad strings
(``"Qualcomm"``, ``"DIAG"``, ``"QDSS"``) are passed in to the adapter as
"here are some hints"; the adapter decides whether they are enough.
"""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..errors import TargetResolutionError
from ..types import utcnow


@dataclass
class DiscoveryResult:
    found: List[str]
    source: str  # "explicit" | "auto" | "none"
    reason: str
    timestamp: str

    def to_dict(self) -> dict:
        return {
            "found": list(self.found),
            "source": self.source,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


def discover_candidates() -> DiscoveryResult:
    """Return a list of diagnostic-port *hints* by enumerating Windows serial devices.

    Only returns a non-empty list when the OS is Windows and the
    enumeration succeeds; otherwise returns an empty list.  Each
    candidate is the DeviceID (e.g. ``COM15``).
    """

    if platform.system() != "Windows":
        return DiscoveryResult(
            found=[],
            source="none",
            reason="non-Windows host — no COM enumeration",
            timestamp=utcnow().isoformat(),
        )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_SerialPort | "
                "Select-Object DeviceID,Name,Description | "
                "ConvertTo-Json",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return DiscoveryResult(
            found=[],
            source="none",
            reason=f"serial enumeration failed: {exc}",
            timestamp=utcnow().isoformat(),
        )
    if result.returncode != 0:
        return DiscoveryResult(
            found=[],
            source="none",
            reason=f"serial enumeration non-zero: {result.returncode}",
            timestamp=utcnow().isoformat(),
        )
    text = result.stdout.strip()
    if not text:
        return DiscoveryResult(
            found=[],
            source="none",
            reason="no serial devices",
            timestamp=utcnow().isoformat(),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return DiscoveryResult(
            found=[],
            source="none",
            reason="serial enumeration JSON parse error",
            timestamp=utcnow().isoformat(),
        )
    if isinstance(data, dict):
        data = [data]
    candidates: List[str] = []
    for item in data:
        device = item.get("DeviceID", "")
        name = item.get("Name", "")
        desc = item.get("Description", "")
        combined = f"{name} {desc}".lower()
        if any(h in combined for h in ("qualcomm", "diag", "qdss", "diagnostic")):
            if device:
                candidates.append(str(device))
    return DiscoveryResult(
        found=candidates,
        source="auto",
        reason="Win32 serial enumeration",
        timestamp=utcnow().isoformat(),
    )


def resolve_target(
    *,
    explicit: Optional[str],
    auto_discover: bool,
) -> DiscoveryResult:
    """Resolve one diagnostic target.

    The operator-supplied *explicit* selector takes precedence.  When it
    is not set, the orchestrator may call :func:`discover_candidates` and
    pass the list into the QXDM adapter.
    """

    if explicit:
        return DiscoveryResult(
            found=[explicit],
            source="explicit",
            reason="operator-supplied selector",
            timestamp=utcnow().isoformat(),
        )
    if not auto_discover:
        return DiscoveryResult(
            found=[],
            source="none",
            reason="auto-discovery disabled and no explicit selector",
            timestamp=utcnow().isoformat(),
        )
    return discover_candidates()


__all__ = [
    "DiscoveryResult",
    "discover_candidates",
    "resolve_target",
]
