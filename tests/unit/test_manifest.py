"""Unit tests for the manifest store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qxdm_automation.manifest import (
    ManifestStore,
    RunManifest,
    append_transition,
    new_manifest,
)
from qxdm_automation.types import LifecycleState


def test_new_manifest_initial_state() -> None:
    m = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=Path("/tmp/c.json"),
        config_sha256="abc",
    )
    assert m.current_state is LifecycleState.CREATED
    assert m.transitions == []
    assert m.created_at


def test_append_transition_updates_state() -> None:
    m = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=Path("/tmp/c.json"),
        config_sha256="abc",
    )
    append_transition(m, LifecycleState.PREFLIGHT_OK, note="ok")
    assert m.current_state is LifecycleState.PREFLIGHT_OK
    assert len(m.transitions) == 1
    assert m.transitions[0].note == "ok"


def test_manifest_store_save_load_round_trip(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = ManifestStore(run_dir)
    m = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=tmp_path / "config.json",
        config_sha256="abc",
    )
    append_transition(m, LifecycleState.PREFLIGHT_OK, note="ok")
    store.save(m)
    assert store.path.exists()
    loaded = store.load()
    assert loaded is not None
    assert loaded.current_state is LifecycleState.PREFLIGHT_OK
    assert loaded.transitions[0].note == "ok"


def test_manifest_store_mark_ready(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = ManifestStore(run_dir)
    m = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=tmp_path / "config.json",
        config_sha256="abc",
    )
    append_transition(m, LifecycleState.COMPLETED)
    store.mark_ready(m)
    assert store.is_ready()
    ready_data = json.loads(store.ready_path.read_text())
    assert ready_data["state"] == "COMPLETED"
    assert ready_data["run_id"] == "r1"


def test_manifest_store_clear(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = ManifestStore(run_dir)
    m = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=tmp_path / "config.json",
        config_sha256="abc",
    )
    store.mark_ready(m)
    store.clear()
    assert not store.path.exists()
    assert not store.ready_path.exists()


def test_manifest_round_trip_preserves_binaries_and_conversions(tmp_path: Path) -> None:
    from qxdm_automation.manifest import BinaryRecord, ConversionRecord

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    store = ManifestStore(run_dir)
    m = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=tmp_path / "config.json",
        config_sha256="abc",
    )
    m.binaries.append(
        BinaryRecord(
            path="/x",
            size_bytes=1,
            sha256="z",
            segment_index=0,
            status="finalized",
        )
    )
    m.conversions.append(
        ConversionRecord(
            source_binary="/x",
            output_path="/y",
            size_bytes=1,
            sha256="w",
            encoding="utf-8",
            returncode=0,
            evidence="ok",
        )
    )
    store.save(m)
    loaded = store.load()
    assert loaded is not None
    assert loaded.binaries[0].path == "/x"
    assert loaded.conversions[0].output_path == "/y"
