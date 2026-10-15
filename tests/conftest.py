"""Shared pytest fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from qxdm_automation.adapters import (
    QcatAdapter,
    QxdmAdapter,
    QxdmSession,
    SimulationQcatAdapter,
    SimulationQxdmAdapter,
)
from qxdm_automation.config import (
    PathsConfig,
    PolicyConfig,
    RunConfig,
    TimingConfig,
    ToolConfig,
)
from qxdm_automation.monitoring import CancellationToken
from qxdm_automation.orchestration import Clock
from qxdm_automation.types import Mode


@dataclass
class FixtureConfig:
    run_id: str
    capture_root: Path
    conversion_root: Path
    publication_root: Path
    run_workspace_root: Path


@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> FixtureConfig:
    base = tmp_path / "qxdm"
    base.mkdir()
    cfg = FixtureConfig(
        run_id="run-test-001",
        capture_root=base / "raw",
        conversion_root=base / "conv",
        publication_root=base / "pub",
        run_workspace_root=base / "ws",
    )
    for p in (
        cfg.capture_root,
        cfg.conversion_root,
        cfg.publication_root,
        cfg.run_workspace_root,
    ):
        p.mkdir(parents=True, exist_ok=True)
    return cfg


def make_run_config(
    workspace: FixtureConfig,
    *,
    mode: Mode = Mode.OFFLINE,
    capture_duration: float = 2.0,
    monitor_interval: float = 0.05,
    rotation_bytes: int = 1024,
    min_free_bytes: int = 0,
    device_selector: Optional[str] = "COM-SIM-1",
    run_id: Optional[str] = None,
    auto_discover: bool = True,
    retention: str = "retain",
    qxdm_dmc_path: Optional[Path] = None,
) -> RunConfig:
    cfg = RunConfig(
        mode=mode,
        config_path=workspace.capture_root.parent / "config.json",
        config_sha256="0" * 64,
        run_id=run_id or workspace.run_id,
        run_category="test",
        tool=ToolConfig(
            qxdm_path=None,
            qxdm_binding="simulation",
            qxdm_args=(),
            qcat_path=None,
            qcat_binding="simulation",
            qcat_template=(
                "{qcat_exe}",
                "--input",
                "{input}",
                "--output",
                "{output}",
            ),
            rotation_bytes=rotation_bytes,
        ),
        paths=PathsConfig(
            capture_root=workspace.capture_root,
            conversion_root=workspace.conversion_root,
            publication_root=workspace.publication_root,
            run_workspace_root=workspace.run_workspace_root,
            filename_prefix="QXDM_Log",
        ),
        timing=TimingConfig(
            capture_duration=capture_duration,
            readiness_timeout=5.0,
            stop_timeout=5.0,
            flush_timeout=5.0,
            conversion_timeout=30.0,
            monitor_interval=monitor_interval,
        ),
        policy=PolicyConfig(
            attachment="owned",
            retention=__import__("qxdm_automation.types", fromlist=["RetentionPolicy"]).RetentionPolicy(retention),
            max_retries=1,
            min_free_bytes=min_free_bytes,
            stale_lock_seconds=3600.0,
        ),
        device_selector=device_selector,
        qxdm_dmc_path=qxdm_dmc_path,
        auto_discover=auto_discover,
    )
    return cfg


@pytest.fixture()
def cancellation() -> CancellationToken:
    return CancellationToken()


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def make_dmc(tmp_path: Path) -> Path:
    p = tmp_path / "sample.dmc"
    p.write_text(json.dumps({"synthetic": True}), encoding="utf-8")
    return p


def seed_capture_files(
    capture_root: Path,
    *,
    prefix: str = "QXDM_Log",
    count: int = 3,
    size: int = 64,
    start_index: int = 1,
    ext: str = ".dlf",
) -> List[Path]:
    """Materialise *count* fake capture segments under *capture_root*.

    Used to test resume / publication / conversion paths without running
    the orchestrator.
    """

    capture_root.mkdir(parents=True, exist_ok=True)
    paths: List[Path] = []
    for i in range(count):
        p = capture_root / f"{prefix}_{start_index + i:06d}{ext}"
        p.write_bytes(bytes(((i * 17 + j) & 0xFF) for j in range(size)))
        paths.append(p)
    return paths


def make_orchestrator(
    workspace: FixtureConfig,
    *,
    mode: Mode = Mode.OFFLINE,
    capture_duration: float = 2.0,
    monitor_interval: float = 0.05,
    rotation_bytes: int = 1024,
    min_free_bytes: int = 0,
    device_selector: Optional[str] = "COM-SIM-1",
    run_id: Optional[str] = None,
    auto_discover: bool = True,
    cancellation: Optional[CancellationToken] = None,
    qxdm_adapter: Optional[QxdmAdapter] = None,
    qcat_adapter: Optional[QcatAdapter] = None,
    qxdm_dmc_path: Optional[Path] = None,
    publication_contract: str = "run_folder",
    retention: str = "retain",
    clock: Optional[Clock] = None,
) -> Tuple[RunConfig, "QXDMIntegrationBundle"]:
    from qxdm_automation.orchestration import Orchestrator

    cfg = make_run_config(
        workspace,
        mode=mode,
        capture_duration=capture_duration,
        monitor_interval=monitor_interval,
        rotation_bytes=rotation_bytes,
        min_free_bytes=min_free_bytes,
        device_selector=device_selector,
        run_id=run_id,
        auto_discover=auto_discover,
        qxdm_dmc_path=qxdm_dmc_path,
        retention=retention,
    )
    qxdm = qxdm_adapter or SimulationQxdmAdapter(
        target_selector=device_selector or "COM-SIM-1",
        rotation_bytes=rotation_bytes,
    )
    qcat = qcat_adapter or SimulationQcatAdapter()
    cancel = cancellation or CancellationToken()
    orch = Orchestrator(
        cfg,
        qxdm_adapter=qxdm,
        qcat_adapter=qcat,
        clock=clock or Clock(),
        cancellation=cancel,
        publication_contract=publication_contract,
    )
    return cfg, QXDMIntegrationBundle(orch=orch, qxdm=qxdm, qcat=qcat, cancel=cancel, clock=orch.clock)


@dataclass
class QXDMIntegrationBundle:
    orch: object
    qxdm: QxdmAdapter
    qcat: QcatAdapter
    cancel: CancellationToken
    clock: Clock
