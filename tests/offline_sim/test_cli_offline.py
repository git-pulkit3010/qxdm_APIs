"""Tests for the operator CLI's offline and validation paths."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from qxdm_automation.cli import main


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


def _write_minimal_config(path: Path, *, run_id: str = "cli-test", mode: str = "offline") -> Path:
    cfg = {
        "mode": mode,
        "run_id": run_id,
        "run_category": "cli",
        "tool": {
            "qxdm_binding": "simulation",
            "qcat_binding": "simulation",
            "rotation_bytes": 256,
            "qcat_template": ["{qcat_exe}", "--input", "{input}", "--output", "{output}"],
        },
        "paths": {
            "capture_root": str(path / "raw"),
            "conversion_root": str(path / "conv"),
            "publication_root": str(path / "pub"),
            "run_workspace_root": str(path / "ws"),
            "filename_prefix": "QXDM_Log",
        },
        "timing": {
            "capture_duration": 1.0,
            "readiness_timeout": 5.0,
            "stop_timeout": 5.0,
            "flush_timeout": 5.0,
            "conversion_timeout": 30.0,
            "monitor_interval": 0.05,
        },
        "policy": {"min_free_bytes": 0},
        "device_selector": "COM-SIM-1",
    }
    cfg_path = path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg_path


def test_cli_plan(tmp_path: Path) -> None:
    cfg = _write_minimal_config(tmp_path, run_id="plan-test")
    rc = main(["plan", "-c", str(cfg)])
    assert rc == 0


def test_cli_run_offline(tmp_path: Path) -> None:
    cfg = _write_minimal_config(tmp_path, run_id="cli-run")
    rc = main(["run", "-c", str(cfg)])
    assert rc == 0
    # The publication folder must contain a READY marker.
    pub_run = tmp_path / "pub" / "cli-run"
    assert (pub_run / "READY").exists()


def test_cli_inventory_returns_json() -> None:
    rc = main(["inventory"])
    assert rc == 0


def test_cli_rejects_invalid_config(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"mode": "offline"}), encoding="utf-8")
    rc = main(["run", "-c", str(bad)])
    assert rc == 2  # configuration error


def test_cli_help_runs(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
