"""Crash-safe TXT handoff.

The publication layer is the boundary between the run's working area and
the downstream consumer.  The default contract is:

* A run-specific folder is created under the operator-supplied
  *publication root*.
* All TXT outputs and a copy of the manifest are staged inside that
  folder under a unique per-run name.
* Only when every TXT has been verified (hash, size, encoding) and the
  manifest is fully written is a ``READY`` marker placed next to the
  folder.  Consumers process only folders that carry a ``READY`` marker.

An alternative *flat* contract preserves per-file atomicity for
consumers that poll a single directory:

* Each TXT is published to a unique name that does not overwrite an
  existing file.
* A separate ``published.json`` accumulator lists the published files
  and a per-run ``READY.run-<id>`` marker indicates the run is complete.

The orchestrator chooses between the two contracts based on the run
configuration; both honour the rule that *partial* publication never
looks like a *complete* publication.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from ..errors import PublicationError
from ..manifest import RunManifest
from ..types import TextArtifact, utcnow
from ..utils import (
    atomic_write_text,
    copy_file_atomic,
    is_safe_filename,
    normalize_path,
    sha256_of_file,
    within_root,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class PublicationOutcome:
    published: List[TextArtifact]
    consumer_paths: List[Path]
    run_folder: Optional[Path]
    ready: bool
    notes: List[str]

    def to_dict(self) -> dict:
        return {
            "published": [
                {
                    "source_binary": str(t.source_binary),
                    "text_path": str(t.text_path),
                    "size_bytes": t.size_bytes,
                    "sha256": t.sha256,
                    "encoding": t.encoding,
                }
                for t in self.published
            ],
            "consumer_paths": [str(p) for p in self.consumer_paths],
            "run_folder": str(self.run_folder) if self.run_folder else None,
            "ready": self.ready,
            "notes": list(self.notes),
        }


def _safe_name(name: str) -> str:
    if not is_safe_filename(name):
        raise PublicationError(f"refusing to publish with unsafe filename: {name!r}")
    return name


def _verify_artifact(art: TextArtifact) -> None:
    if not art.text_path.exists():
        raise PublicationError(f"text artifact missing: {art.text_path}")
    size = art.text_path.stat().st_size
    if size != art.size_bytes:
        raise PublicationError(
            f"size mismatch for {art.text_path}: declared {art.size_bytes}, "
            f"actual {size}"
        )
    digest = sha256_of_file(art.text_path)
    if digest != art.sha256:
        raise PublicationError(
            f"sha256 mismatch for {art.text_path}: declared {art.sha256}, "
            f"actual {digest}"
        )


def publish_run_folder(
    *,
    publication_root: Path,
    run_id: str,
    artifacts: Sequence[TextArtifact],
    manifest: RunManifest,
    binary_retain: bool = True,
) -> PublicationOutcome:
    """Publish a complete run into a run-specific folder under *publication_root*."""

    publication_root = normalize_path(publication_root)
    publication_root.mkdir(parents=True, exist_ok=True)
    run_folder = publication_root / _safe_name(run_id)
    if run_folder.exists():
        # Never overwrite an existing run folder.  Use a unique suffix.
        suffix = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        run_folder = publication_root / _safe_name(f"{run_id}-{suffix}")
    run_folder.mkdir(parents=True, exist_ok=True)

    published: List[TextArtifact] = []
    for art in artifacts:
        _verify_artifact(art)
        target = run_folder / _safe_name(art.text_path.name)
        copy_file_atomic(art.text_path, target)
        digest = sha256_of_file(target)
        if digest != art.sha256:
            raise PublicationError(
                f"hash drift after publication of {target}: expected "
                f"{art.sha256}, got {digest}"
            )
        published.append(
            TextArtifact(
                source_binary=art.source_binary,
                text_path=target,
                size_bytes=art.size_bytes,
                sha256=digest,
                encoding=art.encoding,
            )
        )

    # Manifest + ready marker.  Written *after* every TXT to make sure
    # partial publication never looks like a complete run.
    manifest_path = run_folder / "manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    ready = run_folder / "READY"
    atomic_write_text(
        ready,
        json.dumps(
            {
                "marked_at": utcnow().isoformat(),
                "run_id": run_id,
                "state": manifest.current_state.value,
            }
        ),
    )

    return PublicationOutcome(
        published=published,
        consumer_paths=[p.text_path for p in published],
        run_folder=run_folder,
        ready=True,
        notes=[f"raw binary retention: {'yes' if binary_retain else 'no'}"],
    )


def publish_flat(
    *,
    publication_root: Path,
    run_id: str,
    artifacts: Sequence[TextArtifact],
    manifest: RunManifest,
    binary_retain: bool = True,
) -> PublicationOutcome:
    """Publish individual TXT files into *publication_root*.

    Used by consumers that scan a single flat directory.  Each file is
    placed under a unique, run-scoped name; an aggregator
    ``published.json`` is updated to record what was published in this
    run, and a ``READY.<run_id>`` marker indicates completeness.
    """

    publication_root = normalize_path(publication_root)
    publication_root.mkdir(parents=True, exist_ok=True)

    published: List[TextArtifact] = []
    consumer_paths: List[Path] = []
    for art in artifacts:
        _verify_artifact(art)
        # The on-disk name combines the source stem with the run id so
        # the consumer can attribute files and re-runs cannot overwrite.
        base = art.text_path.stem
        candidate = f"{_safe_name(run_id)}__{_safe_name(base)}.txt"
        target = publication_root / candidate
        if target.exists():
            target = publication_root / f"{_safe_name(run_id)}__{_safe_name(base)}-{int(time.time())}.txt"
        copy_file_atomic(art.text_path, target)
        digest = sha256_of_file(target)
        if digest != art.sha256:
            raise PublicationError(
                f"hash drift after publication of {target}"
            )
        published.append(
            TextArtifact(
                source_binary=art.source_binary,
                text_path=target,
                size_bytes=art.size_bytes,
                sha256=digest,
                encoding=art.encoding,
            )
        )
        consumer_paths.append(target)

    # Append the new published files to a per-directory accumulator.
    accumulator = publication_root / "published.json"
    if accumulator.exists():
        try:
            history = json.loads(accumulator.read_text(encoding="utf-8"))
        except Exception:
            history = {"entries": []}
    else:
        history = {"entries": []}
    history.setdefault("entries", []).append(
        {
            "run_id": run_id,
            "published_at": utcnow().isoformat(),
            "files": [
                {
                    "path": str(p.text_path),
                    "source_binary": str(p.source_binary),
                    "size_bytes": p.size_bytes,
                    "sha256": p.sha256,
                    "encoding": p.encoding,
                }
                for p in published
            ],
        }
    )
    atomic_write_text(accumulator, json.dumps(history, indent=2, sort_keys=True))

    # Per-run READY marker.  Consumers that respect the flat contract
    # should only consider a run complete when its marker exists.
    marker = publication_root / f"READY.{_safe_name(run_id)}"
    atomic_write_text(
        marker,
        json.dumps(
            {
                "marked_at": utcnow().isoformat(),
                "run_id": run_id,
                "state": manifest.current_state.value,
            }
        ),
    )

    return PublicationOutcome(
        published=published,
        consumer_paths=consumer_paths,
        run_folder=None,
        ready=True,
        notes=[f"raw binary retention: {'yes' if binary_retain else 'no'}"],
    )


def commit_publication(
    *,
    publication_root: Path,
    run_id: str,
    manifest: RunManifest,
    artifacts: Sequence[TextArtifact],
    contract: str,
    binary_retain: bool = True,
) -> PublicationOutcome:
    """Pick the publication contract and dispatch."""

    publication_root = normalize_path(publication_root)
    if contract == "run_folder":
        return publish_run_folder(
            publication_root=publication_root,
            run_id=run_id,
            artifacts=artifacts,
            manifest=manifest,
            binary_retain=binary_retain,
        )
    if contract == "flat":
        return publish_flat(
            publication_root=publication_root,
            run_id=run_id,
            artifacts=artifacts,
            manifest=manifest,
            binary_retain=binary_retain,
        )
    raise PublicationError(f"unknown publication contract: {contract!r}")


def retain_binaries(
    *,
    capture_root: Path,
    retention: str,
    binary_record_paths: Iterable[Path],
) -> List[Path]:
    """Apply the configured retention policy to the raw binaries.

    The default ``retain`` policy is a no-op (returns an empty list).
    The ``delete_on_success`` policy removes every path in
    *binary_record_paths*; failures are recorded but do not raise, so
    the publication is not aborted by a partial delete.
    """

    if retention == "retain":
        return []
    if retention != "delete_on_success":
        raise PublicationError(f"unknown retention policy: {retention!r}")
    deleted: List[Path] = []
    for path in binary_record_paths:
        try:
            os.remove(path)
            deleted.append(path)
        except OSError as exc:
            LOGGER.warning("retention: failed to delete %s: %s", path, exc)
    return deleted


__all__ = [
    "PublicationOutcome",
    "publish_run_folder",
    "publish_flat",
    "commit_publication",
    "retain_binaries",
]
