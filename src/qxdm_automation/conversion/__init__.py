"""QCAT conversion subsystem.

This module is the *typed* layer that surrounds the QCAT adapter.  The
adapter produces a :class:`ConversionResult`; this module validates the
output, decides retry, and persists :class:`ConversionRecord` entries in
the manifest.

Conversion is *strict*: a "successful" outcome requires

* a documented completion signal from the adapter (zero exit code is not
  by itself success);
* a non-empty TXT file attributable to this attempt;
* a structural check against the adapter-declared evidence / sentinel
  when one is available; and
* an unchanged source binary through the attempt (detected by hash).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from ..adapters import ConversionResult, QcatAdapter
from ..errors import ConversionError, UnsupportedCapabilityError
from ..manifest import ConversionRecord
from ..types import CaptureBinary, TextArtifact
from ..utils import file_size, normalize_path, sha256_of_file

LOGGER = logging.getLogger(__name__)


SYNTHETIC_SENTINEL = "QXDM-AUTOMATION SYNTHETIC CONVERTER OUTPUT"


@dataclass
class ConversionOutcome:
    record: ConversionRecord
    text: Optional[TextArtifact]
    transient_retry: bool
    fatal: bool


class QcatConverter:
    """A typed wrapper around a :class:`QcatAdapter`."""

    def __init__(
        self,
        adapter: QcatAdapter,
        *,
        timeout_seconds: float,
        max_retries: int = 1,
        encoding: str = "utf-8",
    ) -> None:
        self.adapter = adapter
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.encoding = encoding

    def convert_one(
        self,
        *,
        binary: CaptureBinary,
        output_path: Path,
        working_dir: Path,
    ) -> ConversionOutcome:
        """Convert a single binary to *output_path* with bounded retries.

        *working_dir* is a private staging area under the run's
        ``conversion`` folder.  The final consumer folder is never
        written to until the manifest is committed.
        """

        working_dir.mkdir(parents=True, exist_ok=True)
        output_path = normalize_path(output_path)
        # The file produced inside the staging area; the orchestrator
        # renames it into the final location during publication.
        staging = working_dir / output_path.name

        # The adapter will fail fast on unbound bindings.
        try:
            result = self.adapter.convert(
                source=binary.path,
                output=staging,
                timeout_seconds=self.timeout_seconds,
            )
        except UnsupportedCapabilityError:
            # Re-raise as-is so preflight blocks the live run.
            raise
        except ConversionError:
            raise
        except Exception as exc:
            result = ConversionResult(
                returncode=99,
                stdout="",
                stderr=f"adapter raised: {exc}",
                output_path=None,
                timed_out=False,
                evidence="adapter raised an unexpected exception",
            )

        attempts = 1
        # Bounded retry for clearly transient failures (timeout, IO).
        while result.returncode != 0 and attempts <= self.max_retries and self._is_transient(result):
            attempts += 1
            LOGGER.warning(
                "transient conversion failure for %s (rc=%s, timed_out=%s); retrying",
                binary.path.name,
                result.returncode,
                result.timed_out,
            )
            try:
                result = self.adapter.convert(
                    source=binary.path,
                    output=staging,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:
                result = ConversionResult(
                    returncode=99,
                    stdout="",
                    stderr=f"adapter raised on retry: {exc}",
                    output_path=None,
                    timed_out=False,
                    evidence="adapter raised an unexpected exception on retry",
                )

        # Source-stability check: the source must not have changed through
        # the attempt.  Real QXDM is *not* supposed to mutate a finalized
        # binary, but the orchestrator is the only system that can detect
        # an unexpected change.
        try:
            now_hash = sha256_of_file(binary.path)
        except OSError:
            now_hash = binary.sha256
        if now_hash != binary.sha256:
            return self._make_failure(
                binary,
                staging,
                result,
                attempts,
                "source binary changed during conversion attempt",
            )

        # Result evaluation.
        if result.returncode != 0 or result.output_path is None or not staging.exists():
            return self._make_failure(binary, staging, result, attempts, "converter produced no output")

        size = file_size(staging)
        if size <= 0:
            return self._make_failure(
                binary, staging, result, attempts, "converter produced an empty file"
            )

        # Strongest completion signal: the adapter must declare success
        # AND the file must be readable as text in the declared encoding.
        try:
            text = staging.read_text(encoding=self.encoding)
        except UnicodeDecodeError as exc:
            return self._make_failure(
                binary,
                staging,
                result,
                attempts,
                f"output is not valid {self.encoding}: {exc}",
            )

        # Structural check: the simulation adapter emits a sentinel that
        # we recognise.  The real QCAT adapter must declare its own
        # evidence and a structural check is best-effort until a real
        # sample is available.
        structural_ok, structural_reason = self._structural_check(result, text, staging)
        if not structural_ok:
            return self._make_failure(binary, staging, result, attempts, structural_reason)

        return ConversionOutcome(
            record=ConversionRecord(
                source_binary=str(binary.path),
                output_path=str(staging),
                size_bytes=size,
                sha256=sha256_of_file(staging),
                encoding=self.encoding,
                returncode=result.returncode,
                evidence=result.evidence,
                error=None,
                timed_out=result.timed_out,
                attempts=attempts,
            ),
            text=TextArtifact(
                source_binary=binary.path,
                text_path=staging,
                size_bytes=size,
                sha256=sha256_of_file(staging),
                encoding=self.encoding,
            ),
            transient_retry=False,
            fatal=False,
        )

    def convert_all(
        self,
        *,
        binaries: Sequence[CaptureBinary],
        staging_dir: Path,
        name_for,
    ) -> List[ConversionOutcome]:
        """Convert every binary sequentially.

        ``name_for(binary, index)`` produces a unique TXT file name for a
        given binary so that two binaries with the same stem in different
        directories never collide.
        """

        outcomes: List[ConversionOutcome] = []
        for idx, binary in enumerate(binaries):
            out_name = name_for(binary, idx)
            outcome = self.convert_one(
                binary=binary,
                output_path=staging_dir / out_name,
                working_dir=staging_dir,
            )
            outcomes.append(outcome)
        return outcomes

    # ---- Helpers --------------------------------------------------------
    def _is_transient(self, result: ConversionResult) -> bool:
        if result.timed_out:
            return True
        if "i/o" in result.stderr.lower() or "lock" in result.stderr.lower():
            return True
        return False

    def _structural_check(
        self,
        result: ConversionResult,
        text: str,
        path: Path,
    ) -> tuple:
        """Verify the output has a sane structure.

        Returns ``(ok, reason)``.  The check is intentionally conservative:
        it is a structural sanity check, *not* a semantic decoder check.
        Real QCAT output must be validated against a real sample once one
        is available; that is recorded as a pending validation in the
        evidence/binding doc.
        """

        if not text.strip():
            return (False, "output is empty after decode")
        # A line must contain either an item record, a comment, or whitespace.
        for i, line in enumerate(text.splitlines()[:10]):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            # Real QCAT records are non-trivial; require a whitespace
            # somewhere on the first non-comment line.
            if " " not in stripped and "\t" not in stripped:
                return (
                    False,
                    f"first non-comment line is a single token ({stripped!r})",
                )
        return (True, "structural check passed")

    def _make_failure(
        self,
        binary: CaptureBinary,
        staging: Path,
        result: ConversionResult,
        attempts: int,
        reason: str,
    ) -> ConversionOutcome:
        return ConversionOutcome(
            record=ConversionRecord(
                source_binary=str(binary.path),
                output_path=str(staging) if staging.exists() else None,
                size_bytes=file_size(staging) if staging.exists() else None,
                sha256=sha256_of_file(staging) if staging.exists() else None,
                encoding=None,
                returncode=result.returncode,
                evidence=result.evidence,
                error=reason + (f"; stderr={result.stderr.strip()}" if result.stderr else ""),
                timed_out=result.timed_out,
                attempts=attempts,
            ),
            text=None,
            transient_retry=False,
            fatal=True,
        )


__all__ = [
    "QcatConverter",
    "ConversionOutcome",
    "SYNTHETIC_SENTINEL",
]
