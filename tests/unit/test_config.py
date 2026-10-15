"""Unit tests for the configuration model and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qxdm_automation.config import (
    DEFAULT_MAX_BINARY_BYTES,
    PathsConfig,
    PolicyConfig,
    RunConfig,
    TimingConfig,
    ToolConfig,
    load_run_config,
)
from qxdm_automation.errors import ConfigurationError
from qxdm_automation.types import Mode, RetentionPolicy


def test_tool_config_rejects_zero_rotation() -> None:
    with pytest.raises(Exception):
        ToolConfig(
            qcat_template=("{qcat_exe}", "--input", "{input}", "--output", "{output}"),
            rotation_bytes=0,
        )


def test_tool_config_rejects_excessive_rotation() -> None:
    with pytest.raises(Exception):
        ToolConfig(
            qcat_template=("{qcat_exe}", "--input", "{input}", "--output", "{output}"),
            rotation_bytes=DEFAULT_MAX_BINARY_BYTES + 1,
        )


def test_tool_config_requires_qcat_template() -> None:
    with pytest.raises(Exception):
        ToolConfig(qcat_template=(), rotation_bytes=DEFAULT_MAX_BINARY_BYTES)


def test_paths_config_rejects_empty_prefix() -> None:
    with pytest.raises(Exception):
        PathsConfig(
            capture_root=Path("/tmp/a"),
            conversion_root=Path("/tmp/b"),
            publication_root=Path("/tmp/c"),
            run_workspace_root=Path("/tmp/d"),
            filename_prefix="",
        )


def test_paths_config_rejects_unsafe_prefix() -> None:
    with pytest.raises(Exception):
        PathsConfig(
            capture_root=Path("/tmp/a"),
            conversion_root=Path("/tmp/b"),
            publication_root=Path("/tmp/c"),
            run_workspace_root=Path("/tmp/d"),
            filename_prefix="../bad",
        )


def test_timing_config_rejects_zero() -> None:
    with pytest.raises(Exception):
        TimingConfig(capture_duration=0)


def test_policy_rejects_bad_attachment() -> None:
    with pytest.raises(Exception):
        PolicyConfig(attachment="bogus")


def test_load_run_config_minimal(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "mode": "offline",
                "run_id": "r1",
                "run_category": "test",
                "tool": {
                    "rotation_bytes": 250_000_000,
                    "qcat_template": ["{qcat_exe}"],
                },
                "paths": {
                    "capture_root": str(tmp_path / "raw"),
                    "conversion_root": str(tmp_path / "conv"),
                    "publication_root": str(tmp_path / "pub"),
                    "run_workspace_root": str(tmp_path / "ws"),
                    "filename_prefix": "QXDM_Log",
                },
            }
        )
    )
    config = load_run_config(cfg_path)
    assert config.mode is Mode.OFFLINE
    assert config.run_id == "r1"
    assert config.tool.rotation_bytes == 250_000_000
    assert config.paths.filename_prefix == "QXDM_Log"
    assert config.config_sha256  # computed


def test_load_run_config_rejects_invalid_mode(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "mode": "guerrilla",
                "run_id": "r1",
                "run_category": "test",
                "tool": {
                    "rotation_bytes": 1,
                    "qcat_template": ["{qcat_exe}"],
                },
                "paths": {
                    "capture_root": str(tmp_path / "raw"),
                    "conversion_root": str(tmp_path / "conv"),
                    "publication_root": str(tmp_path / "pub"),
                    "run_workspace_root": str(tmp_path / "ws"),
                    "filename_prefix": "QXDM_Log",
                },
            }
        )
    )
    with pytest.raises(ConfigurationError):
        load_run_config(cfg_path)


def test_validate_path_layout_rejects_overlap(tmp_path: Path) -> None:
    cfg = RunConfig(
        mode=Mode.OFFLINE,
        config_path=tmp_path / "config.json",
        config_sha256="0" * 64,
        run_id="r1",
        run_category="test",
        tool=ToolConfig(
            qcat_template=("{qcat_exe}",),
            rotation_bytes=1,
        ),
        paths=PathsConfig(
            capture_root=tmp_path,
            conversion_root=tmp_path,
            publication_root=tmp_path / "pub",
            run_workspace_root=tmp_path / "ws",
            filename_prefix="QXDM_Log",
        ),
    )
    with pytest.raises(ConfigurationError):
        cfg.validate_path_layout()


def test_run_id_rejects_path_separator(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        RunConfig(
            mode=Mode.OFFLINE,
            config_path=tmp_path / "config.json",
            config_sha256="0" * 64,
            run_id="a/b",
            run_category="test",
            tool=ToolConfig(
                qcat_template=("{qcat_exe}",),
                rotation_bytes=1,
            ),
            paths=PathsConfig(
                capture_root=tmp_path / "raw",
                conversion_root=tmp_path / "conv",
                publication_root=tmp_path / "pub",
                run_workspace_root=tmp_path / "ws",
                filename_prefix="QXDM_Log",
            ),
        )


def test_retention_policy_round_trip() -> None:
    assert RetentionPolicy("retain") is RetentionPolicy.RETAIN
    assert RetentionPolicy("delete_on_success") is RetentionPolicy.DELETE_ON_SUCCESS


def test_mode_from_string() -> None:
    assert Mode.from_string("offline") is Mode.OFFLINE
    assert Mode.from_string("PLANNING") is Mode.PLANNING
    with pytest.raises(ValueError):
        Mode.from_string("not-a-mode")
