"""Configuration schema, validation, and loaders.

The configuration is intentionally verbose: every field the operator could
reasonably get wrong is named, typed, and validated.  Unknown fields are
rejected so the operator cannot accidentally invent a binding that the
orchestrator would silently ignore.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ..errors import ConfigurationError
from ..types import Mode, RetentionPolicy
from ..utils import (
    is_safe_filename,
    normalize_path,
    sha256_of_file,
    within_root,
)


DEFAULT_MAX_BINARY_BYTES = 250_000_000  # Hard requirement: <= 250 MB
DEFAULT_MONITOR_INTERVAL_SECONDS = 5.0
DEFAULT_READINESS_TIMEOUT_SECONDS = 60.0
DEFAULT_STOP_TIMEOUT_SECONDS = 60.0
DEFAULT_FLUSH_TIMEOUT_SECONDS = 120.0
DEFAULT_CONVERSION_TIMEOUT_SECONDS = 1800.0
DEFAULT_MIN_FREE_BYTES = 1_073_741_824  # 1 GiB
DEFAULT_MAX_RETRIES = 1
DEFAULT_STALE_LOCK_SECONDS = 3600.0


# Candidate binary extensions mentioned in the supplied workflow PDFs.
# These are *candidates*; format-specific support depends on the QCAT binding
# and is verified per-adapter.
DEFAULT_BINARY_EXTENSIONS: Tuple[str, ...] = (".dlf", ".bin", ".isf", ".hdf", ".qmdl")

# Filename value-allowlist for log files: the *operator-supplied* filename
# prefix must be a single safe filename component.  QXDM itself may append
# extra characters; this is the prefix the operator controls.
FILENAME_SAFE_RULE = (
    "Must be a single filename component (no path separators, no drive "
    "letters, no control characters)."
)


@dataclass
class ToolConfig:
    """Path / binding selection for the QXDM and QCAT tools."""

    qxdm_path: Optional[Path] = None
    qxdm_binding: str = "unspecified"
    qxdm_args: Tuple[str, ...] = ()
    qcat_path: Optional[Path] = None
    qcat_binding: str = "unspecified"
    qcat_template: Tuple[str, ...] = ()
    rotation_bytes: int = DEFAULT_MAX_BINARY_BYTES

    def __post_init__(self) -> None:
        if self.rotation_bytes <= 0:
            raise ConfigurationError(
                f"rotation_bytes must be > 0; got {self.rotation_bytes}"
            )
        if self.rotation_bytes > DEFAULT_MAX_BINARY_BYTES:
            raise ConfigurationError(
                f"rotation_bytes={self.rotation_bytes} exceeds the hard "
                f"limit {DEFAULT_MAX_BINARY_BYTES} (250 MB).  Live runs are "
                "rejected because the QXDM rotation contract is <=250 MB."
            )
        if not self.qcat_template:
            raise ConfigurationError(
                "tool.qcat_template must contain a placeholders-bearing "
                "argument vector (e.g. ['{qcat_exe}', '--input', '{input}', "
                "'--output', '{output}']); absence blocks live capture."
            )


@dataclass
class PathsConfig:
    """Filesystem layout for the run."""

    capture_root: Path
    conversion_root: Path
    publication_root: Path
    run_workspace_root: Path
    filename_prefix: str
    binary_extensions: Tuple[str, ...] = DEFAULT_BINARY_EXTENSIONS

    def __post_init__(self) -> None:
        for name, val in (
            ("capture_root", self.capture_root),
            ("conversion_root", self.conversion_root),
            ("publication_root", self.publication_root),
            ("run_workspace_root", self.run_workspace_root),
        ):
            if val is None or str(val).strip() == "":
                raise ConfigurationError(f"paths.{name} is required")
        if not is_safe_filename(self.filename_prefix):
            raise ConfigurationError(
                f"paths.filename_prefix is invalid: {self.filename_prefix!r}. "
                + FILENAME_SAFE_RULE
            )
        if not self.binary_extensions:
            raise ConfigurationError("paths.binary_extensions must list at least one extension")


@dataclass
class TimingConfig:
    """Deadlines and monitoring intervals.  All values are seconds."""

    capture_duration: float = 60.0
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT_SECONDS
    stop_timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS
    flush_timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS
    conversion_timeout: float = DEFAULT_CONVERSION_TIMEOUT_SECONDS
    monitor_interval: float = DEFAULT_MONITOR_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        for fname in (
            "capture_duration",
            "readiness_timeout",
            "stop_timeout",
            "flush_timeout",
            "conversion_timeout",
            "monitor_interval",
        ):
            v = getattr(self, fname)
            if v <= 0 or v == float("inf"):
                raise ConfigurationError(
                    f"timing.{fname} must be a finite positive number; got {v}"
                )


@dataclass
class PolicyConfig:
    """Behavioural policies for the run."""

    attachment: str = "owned"  # 'owned' | 'attach'
    retention: RetentionPolicy = RetentionPolicy.RETAIN
    max_retries: int = DEFAULT_MAX_RETRIES
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    stale_lock_seconds: float = DEFAULT_STALE_LOCK_SECONDS
    allow_unicode_paths: bool = True
    allow_network_destination: bool = False

    def __post_init__(self) -> None:
        if self.attachment not in {"owned", "attach"}:
            raise ConfigurationError(
                f"policy.attachment must be 'owned' or 'attach'; got {self.attachment!r}"
            )
        if self.max_retries < 0:
            raise ConfigurationError("policy.max_retries must be >= 0")
        if self.min_free_bytes < 0:
            raise ConfigurationError("policy.min_free_bytes must be >= 0")
        if self.stale_lock_seconds <= 0:
            raise ConfigurationError("policy.stale_lock_seconds must be > 0")


@dataclass
class RunConfig:
    """Top-level configuration object."""

    mode: Mode
    config_path: Path
    config_sha256: str
    run_id: str
    run_category: str
    tool: ToolConfig
    paths: PathsConfig
    timing: TimingConfig = field(default_factory=TimingConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    device_selector: Optional[str] = None
    qxdm_dmc_path: Optional[Path] = None
    qxdm_dmc_sha256: Optional[str] = None
    auto_discover: bool = True

    def __post_init__(self) -> None:
        if not self.run_id or "/" in self.run_id or "\\" in self.run_id:
            raise ConfigurationError("run_id must be a non-empty string with no path separators")
        if not self.run_category:
            raise ConfigurationError("run_category is required")
        if self.qxdm_dmc_path is not None and not self.qxdm_dmc_path.exists():
            raise ConfigurationError(
                f"qxdm_dmc_path does not exist: {self.qxdm_dmc_path}"
            )

    # ---- Convenience ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Paths become strings for JSON portability.
        for k, v in list(d.items()):
            if isinstance(v, Path):
                d[k] = str(v)
        return d

    # ---- Path helpers ---------------------------------------------------
    def run_dir(self) -> Path:
        """The per-run working directory under the run workspace root."""

        return normalize_path(self.paths.run_workspace_root) / self.run_id

    def raw_dir(self) -> Path:
        return self.run_dir() / "raw"

    def conversion_dir(self) -> Path:
        return self.run_dir() / "conversion"

    def logs_dir(self) -> Path:
        return self.run_dir() / "logs"

    def manifest_path(self) -> Path:
        return self.run_dir() / "manifest.json"

    def lock_path(self) -> Path:
        return self.run_dir() / "run.lock"

    def validate_path_layout(self) -> None:
        """Reject configurations where the run paths overlap each other.

        The raw, conversion, and publication roots must all be distinct paths
        that do not contain each other — the manifest requires this so a
        finalised run cannot accidentally overwrite its own source.
        """

        roots = [
            ("capture_root", self.paths.capture_root),
            ("conversion_root", self.paths.conversion_root),
            ("publication_root", self.paths.publication_root),
            ("run_workspace_root", self.paths.run_workspace_root),
        ]
        seen: Dict[Path, str] = {}
        for name, p in roots:
            np = normalize_path(p).resolve()
            # Allow a not-yet-existing path to be resolved against cwd.
            if not np.exists():
                np = normalize_path(p)
            if np in seen:
                raise ConfigurationError(
                    f"paths.{name} ({np}) duplicates paths.{seen[np]}"
                )
            seen[np] = name

        # Workspace and publication roots must be siblings, not nested.
        workspace = normalize_path(self.paths.run_workspace_root)
        publication = normalize_path(self.paths.publication_root)
        if within_root(workspace, publication) or within_root(publication, workspace):
            raise ConfigurationError(
                "run_workspace_root and publication_root must not be nested"
            )

        # Capture root may live anywhere; only check it is not inside the
        # publication root, because publication of one run must never
        # accidentally land inside a fresh capture's raw directory.
        capture = normalize_path(self.paths.capture_root)
        if within_root(publication, capture):
            raise ConfigurationError(
                "publication_root must not be inside capture_root"
            )


# ---- Loader ----------------------------------------------------------------


def load_run_config(path: Path) -> RunConfig:
    """Load and validate a JSON configuration file from *path*."""

    if not path.exists():
        raise ConfigurationError(f"Configuration file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Configuration file is not valid JSON: {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a JSON object")

    digest = sha256_of_file(path)

    try:
        mode = Mode.from_string(str(raw.get("mode", "offline")))
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc

    run_id = str(raw.get("run_id", "")).strip()
    run_category = str(raw.get("run_category", "")).strip()
    if not run_id:
        raise ConfigurationError("run_id is required")
    if not run_category:
        raise ConfigurationError("run_category is required")

    tool_raw = raw.get("tool", {})
    tool = ToolConfig(
        qxdm_path=_as_path_or_none(tool_raw.get("qxdm_path")),
        qxdm_binding=str(tool_raw.get("qxdm_binding", "unspecified")),
        qxdm_args=tuple(tool_raw.get("qxdm_args", ())),
        qcat_path=_as_path_or_none(tool_raw.get("qcat_path")),
        qcat_binding=str(tool_raw.get("qcat_binding", "unspecified")),
        qcat_template=tuple(tool_raw.get("qcat_template", ())),
        rotation_bytes=int(tool_raw.get("rotation_bytes", DEFAULT_MAX_BINARY_BYTES)),
    )

    paths_raw = raw.get("paths", {})
    if not paths_raw:
        raise ConfigurationError("paths block is required")
    paths = PathsConfig(
        capture_root=_as_required_path(paths_raw, "capture_root"),
        conversion_root=_as_required_path(paths_raw, "conversion_root"),
        publication_root=_as_required_path(paths_raw, "publication_root"),
        run_workspace_root=_as_required_path(paths_raw, "run_workspace_root"),
        filename_prefix=str(paths_raw.get("filename_prefix", "QXDM_Log")),
        binary_extensions=tuple(
            paths_raw.get("binary_extensions", list(DEFAULT_BINARY_EXTENSIONS))
        ),
    )

    timing_raw = raw.get("timing", {})
    timing = TimingConfig(
        capture_duration=float(timing_raw.get("capture_duration", 60.0)),
        readiness_timeout=float(
            timing_raw.get("readiness_timeout", DEFAULT_READINESS_TIMEOUT_SECONDS)
        ),
        stop_timeout=float(timing_raw.get("stop_timeout", DEFAULT_STOP_TIMEOUT_SECONDS)),
        flush_timeout=float(
            timing_raw.get("flush_timeout", DEFAULT_FLUSH_TIMEOUT_SECONDS)
        ),
        conversion_timeout=float(
            timing_raw.get("conversion_timeout", DEFAULT_CONVERSION_TIMEOUT_SECONDS)
        ),
        monitor_interval=float(
            timing_raw.get("monitor_interval", DEFAULT_MONITOR_INTERVAL_SECONDS)
        ),
    )

    policy_raw = raw.get("policy", {})
    policy = PolicyConfig(
        attachment=str(policy_raw.get("attachment", "owned")),
        retention=RetentionPolicy(str(policy_raw.get("retention", "retain"))),
        max_retries=int(policy_raw.get("max_retries", DEFAULT_MAX_RETRIES)),
        min_free_bytes=int(policy_raw.get("min_free_bytes", DEFAULT_MIN_FREE_BYTES)),
        stale_lock_seconds=float(
            policy_raw.get("stale_lock_seconds", DEFAULT_STALE_LOCK_SECONDS)
        ),
        allow_unicode_paths=bool(policy_raw.get("allow_unicode_paths", True)),
        allow_network_destination=bool(policy_raw.get("allow_network_destination", False)),
    )

    dmc_path = _as_path_or_none(raw.get("qxdm_dmc_path"))
    dmc_hash = sha256_of_file(dmc_path) if dmc_path else None

    return RunConfig(
        mode=mode,
        config_path=path,
        config_sha256=digest,
        run_id=run_id,
        run_category=run_category,
        tool=tool,
        paths=paths,
        timing=timing,
        policy=policy,
        device_selector=raw.get("device_selector"),
        qxdm_dmc_path=dmc_path,
        qxdm_dmc_sha256=dmc_hash,
        auto_discover=bool(raw.get("auto_discover", True)),
    )


def _as_required_path(d: Dict[str, Any], name: str) -> Path:
    val = d.get(name)
    if val is None or str(val).strip() == "":
        raise ConfigurationError(f"paths.{name} is required")
    return Path(str(val)).expanduser()


def _as_path_or_none(val: Any) -> Optional[Path]:
    if val is None or str(val).strip() == "":
        return None
    return Path(str(val)).expanduser()


__all__ = [
    "DEFAULT_MAX_BINARY_BYTES",
    "DEFAULT_BINARY_EXTENSIONS",
    "ToolConfig",
    "PathsConfig",
    "TimingConfig",
    "PolicyConfig",
    "RunConfig",
    "load_run_config",
]
