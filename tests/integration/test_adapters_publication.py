"""Tests for the publication, conversion, and adapter behaviour that does
not require the full orchestrator lifecycle.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from qxdm_automation.adapters import (
    QcatAdapter,
    QxdmAdapter,
    SimulationQcatAdapter,
    SimulationQxdmAdapter,
    UnboundQcatAdapter,
    UnboundQxdmAdapter,
)
from qxdm_automation.conversion import QcatConverter
from qxdm_automation.errors import (
    LockAcquisitionError,
    PublicationError,
    UnsupportedCapabilityError,
    VendorIntegrationError,
)
from qxdm_automation.locking import RunLock
from qxdm_automation.manifest import (
    BinaryRecord,
    ConversionRecord,
    ManifestStore,
    RunManifest,
    append_transition,
    new_manifest,
)
from qxdm_automation.publication import (
    PublicationOutcome,
    commit_publication,
    publish_flat,
    publish_run_folder,
    retain_binaries,
)
from qxdm_automation.types import (
    CapabilityReport,
    CaptureBinary,
    LifecycleState,
    TextArtifact,
)
from qxdm_automation.utils import (
    is_safe_filename,
    normalize_path,
    sha256_of_file,
)

from ..conftest import seed_capture_files


# ---------------------------------------------------------------------------
# Adapter inventory paths
# ---------------------------------------------------------------------------


def test_unbound_qxdm_inventory_reports_blocking(tmp_path: Path) -> None:
    adapter = UnboundQxdmAdapter()
    cap = adapter.inventory(qxdm_path=tmp_path / "QXDM.exe")
    assert cap.qxdm_binding == "unspecified"
    assert cap.blocking_reasons


def test_unbound_qxdm_launch_raises(tmp_path: Path) -> None:
    adapter = UnboundQxdmAdapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.launch_or_attach(
            qxdm_path=tmp_path / "QXDM.exe",
            attachment="owned",
            run_dir=tmp_path,
        )


def test_unbound_qcat_convert_raises(tmp_path: Path) -> None:
    adapter = UnboundQcatAdapter()
    with pytest.raises(UnsupportedCapabilityError):
        adapter.convert(
            source=tmp_path / "a.dlf",
            output=tmp_path / "a.txt",
            timeout_seconds=10.0,
        )


def test_simulation_qxdm_inventory_evidence() -> None:
    adapter = SimulationQxdmAdapter()
    cap = adapter.inventory(qxdm_path=None)
    assert cap.native_rotation_supported
    assert ".dlf" in cap.binary_format_evidence
    assert not cap.blocking_reasons


# ---------------------------------------------------------------------------
# Conversion subsystem
# ---------------------------------------------------------------------------


def test_converter_produces_text_for_every_source(tmp_path: Path) -> None:
    binaries = []
    for i in range(3):
        p = tmp_path / f"src_{i}.dlf"
        p.write_bytes(bytes(((i * 11 + j) & 0xFF) for j in range(64)))
        binaries.append(
            CaptureBinary(
                path=p,
                size_bytes=64,
                sha256=sha256_of_file(p),
                segment_index=i,
            )
        )
    converter = QcatConverter(SimulationQcatAdapter(), timeout_seconds=10.0)
    outcomes = converter.convert_all(
        binaries=binaries,
        staging_dir=tmp_path / "staging",
        name_for=lambda b, i: f"seg_{i}.txt",
    )
    assert all(not o.fatal for o in outcomes)
    assert all(o.text is not None for o in outcomes)
    assert all(o.text.text_path.exists() for o in outcomes)


def test_converter_marks_fatal_for_failing_qcat(tmp_path: Path) -> None:
    p = tmp_path / "bad.dlf"
    p.write_bytes(b"x" * 64)
    binary = CaptureBinary(
        path=p, size_bytes=64, sha256=sha256_of_file(p), segment_index=0
    )
    converter = QcatConverter(
        SimulationQcatAdapter(fail_on=("bad.dlf",)),
        timeout_seconds=10.0,
    )
    outcomes = converter.convert_all(
        binaries=[binary],
        staging_dir=tmp_path / "staging",
        name_for=lambda b, i: "out.txt",
    )
    assert outcomes[0].fatal
    assert outcomes[0].text is None
    assert "refused" in (outcomes[0].record.error or "")


def test_converter_detects_changed_source(tmp_path: Path) -> None:
    p = tmp_path / "src.dlf"
    p.write_bytes(b"x" * 64)
    binary = CaptureBinary(
        path=p, size_bytes=64, sha256=sha256_of_file(p), segment_index=0
    )
    converter = QcatConverter(
        SimulationQcatAdapter(delay_seconds=0.1),
        timeout_seconds=10.0,
    )
    import threading

    def tamper():
        time.sleep(0.05)
        p.write_bytes(b"y" * 64)

    threading.Thread(target=tamper, daemon=True).start()
    outcomes = converter.convert_all(
        binaries=[binary],
        staging_dir=tmp_path / "staging",
        name_for=lambda b, i: "out.txt",
    )
    assert outcomes[0].fatal
    assert "changed" in (outcomes[0].record.error or "").lower()


# ---------------------------------------------------------------------------
# Publication subsystem
# ---------------------------------------------------------------------------


def test_publish_run_folder_writes_ready_marker(tmp_path: Path) -> None:
    binaries = seed_capture_files(tmp_path / "raw", count=2)
    artifacts = []
    for i, b in enumerate(binaries):
        text = tmp_path / f"art_{i}.txt"
        text.write_text(f"text {i}", encoding="utf-8")
        artifacts.append(
            TextArtifact(
                source_binary=b,
                text_path=text,
                size_bytes=text.stat().st_size,
                sha256=sha256_of_file(text),
                encoding="utf-8",
            )
        )
    manifest = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=tmp_path / "config.json",
        config_sha256="x",
    )
    append_transition(manifest, LifecycleState.COMPLETED)
    pub_root = tmp_path / "pub"
    outcome = publish_run_folder(
        publication_root=pub_root,
        run_id="r1",
        artifacts=artifacts,
        manifest=manifest,
    )
    assert outcome.ready
    assert (pub_root / "r1" / "READY").exists()
    assert (pub_root / "r1" / "manifest.json").exists()


def test_publish_flat_writes_per_run_marker(tmp_path: Path) -> None:
    binaries = seed_capture_files(tmp_path / "raw", count=1)
    text = tmp_path / "art.txt"
    text.write_text("text", encoding="utf-8")
    artifact = TextArtifact(
        source_binary=binaries[0],
        text_path=text,
        size_bytes=text.stat().st_size,
        sha256=sha256_of_file(text),
        encoding="utf-8",
    )
    manifest = new_manifest(
        run_id="r1",
        run_category="test",
        mode="offline",
        config_path=tmp_path / "config.json",
        config_sha256="x",
    )
    append_transition(manifest, LifecycleState.COMPLETED)
    outcome = publish_flat(
        publication_root=tmp_path / "pub",
        run_id="r1",
        artifacts=[artifact],
        manifest=manifest,
    )
    assert outcome.ready
    assert (tmp_path / "pub" / "READY.r1").exists()
    history = json.loads((tmp_path / "pub" / "published.json").read_text())
    assert history["entries"][0]["run_id"] == "r1"


def test_commit_publication_rejects_unknown_contract(tmp_path: Path) -> None:
    with pytest.raises(PublicationError):
        commit_publication(
            publication_root=tmp_path,
            run_id="r1",
            manifest=new_manifest(
                run_id="r1",
                run_category="test",
                mode="offline",
                config_path=tmp_path / "c.json",
                config_sha256="x",
            ),
            artifacts=[],
            contract="not-a-contract",
        )


def test_retain_binaries_deletes_only_with_explicit_policy(tmp_path: Path) -> None:
    binaries = seed_capture_files(tmp_path / "raw", count=2)
    # retain: no-op
    deleted = retain_binaries(
        capture_root=tmp_path / "raw",
        retention="retain",
        binary_record_paths=binaries,
    )
    assert deleted == []
    # delete_on_success: all removed
    deleted = retain_binaries(
        capture_root=tmp_path / "raw",
        retention="delete_on_success",
        binary_record_paths=binaries,
    )
    assert len(deleted) == 2
    assert not any(p.exists() for p in binaries)
