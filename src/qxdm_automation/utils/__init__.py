"""Small reusable helpers used across the package.

This module deliberately keeps zero state.  Anything that depends on the
filesystem, the network, or a clock takes the relevant object as an argument so
tests can swap it out.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Union

PathLike = Union[str, os.PathLike]


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_INVALID_DRIVE_CHARS = re.compile(r"[/:]")


def normalize_path(path: PathLike) -> Path:
    """Return a fully-resolved :class:`Path` with consistent separators.

    We do **not** call :meth:`Path.resolve` here because that would fail for
    paths that do not yet exist (e.g. a fresh run directory).  We only
    normalise what is on disk.
    """

    return Path(os.path.normpath(os.fspath(path)))


def is_safe_filename(name: str) -> bool:
    """A conservative filename check.

    Rejects path separators, drive letters, NUL, and control characters.  The
    operator may supply a single filename component only.
    """

    if not name or name in {".", ".."}:
        return False
    if _INVALID_FILENAME_CHARS.search(name):
        return False
    if _INVALID_DRIVE_CHARS.search(name):
        return False
    if name.endswith(" ") or name.endswith("."):
        return False
    return True


def sha256_of_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Stream-hash a file without loading it into memory."""

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_size(path: Path) -> int:
    """Return the file size, or 0 if the file does not exist."""

    try:
        return path.stat().st_size
    except OSError:
        return 0


def free_space_bytes(path: Path) -> int:
    """Return the free bytes on the volume containing *path*."""

    path = normalize_path(path)
    if not path.exists():
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def ensure_directory(path: Path) -> None:
    """Create *path* (and parents) if it does not exist.  Idempotent."""

    path.mkdir(parents=True, exist_ok=True)


def within_root(path: Path, root: Path) -> bool:
    """Return True if *path* is inside *root* after normalisation.

    Used to detect path-escape attempts in output publication.
    """

    path = normalize_path(path)
    root = normalize_path(root)
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically.

    Writes to a sibling ``.partial`` then renames.  If the rename fails, the
    partial file is removed.
    """

    ensure_directory(path.parent)
    tmp = path.with_name(path.name + ".partial")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_directory(path.parent)
    tmp = path.with_name(path.name + ".partial")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def copy_file_atomic(src: Path, dst: Path) -> None:
    """Copy *src* over *dst* using a same-volume rename when possible.

    The contract is: after return, *dst* contains the bytes of *src*, and
    *dst* is either absent or fully replaced.  This is *not* atomic across
    volumes; on cross-volume moves we copy through a sibling ``.partial`` and
    only rename once the bytes are flushed.
    """

    ensure_directory(dst.parent)
    src = normalize_path(src)
    dst = normalize_path(dst)
    tmp = dst.with_name(dst.name + ".partial")
    try:
        # ``shutil.copyfile`` opens the destination in write-binary mode
        # and closes it.  We re-open in r+b so we can fsync the descriptor
        # we just wrote through; on Windows the read-only handle returned
        # by the previous close is no longer valid.
        shutil.copyfile(src, tmp)
        with open(tmp, "r+b") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class AtomicCounter:
    """A monotonically increasing integer safe for use across threads.

    Used by simulation adapters to keep segment indexes deterministic.
    """

    _lock: threading.Lock
    _value: int

    @classmethod
    def starting_at(cls, value: int = 0) -> "AtomicCounter":
        return cls(_lock=threading.Lock(), _value=value)

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


@contextmanager
def temporary_directory(
    parent: Optional[Path] = None,
    *,
    prefix: str = "qxdm_",
    cleanup: Callable[[Path], None] = shutil.rmtree,
    ignore_errors: bool = True,
) -> Iterator[Path]:
    """Context manager that creates and removes a temporary directory.

    Provided as a single utility so the rest of the package doesn't have to
    import :mod:`tempfile` directly (and can be swapped for a deterministic
    factory in tests).
    """

    import tempfile

    if parent is not None:
        ensure_directory(parent)
    tmp = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent) if parent else None))
    try:
        yield tmp
    finally:
        try:
            cleanup(tmp)
        except Exception:
            if not ignore_errors:
                raise


def iter_files_matching(
    root: Path,
    patterns: Iterable[str],
    *,
    case_insensitive: bool = True,
) -> Iterator[Path]:
    """Yield files under *root* whose name matches any of the glob patterns.

    Patterns are matched against the basename only (so callers can pass
    ``"*.dlf"``).  Case sensitivity is controlled by *case_insensitive*
    (defaulting to True because Windows file systems are case-insensitive).
    """

    suffixes = []
    for pat in patterns:
        if pat.startswith("*."):
            suffixes.append(pat[1:].lower() if case_insensitive else pat[1:])
        else:
            suffixes.append(None)  # type: ignore[arg-type]
    suffixes_set = {s for s in suffixes if s}

    for entry in root.iterdir():
        if not entry.is_file():
            continue
        name_lower = entry.name.lower()
        for s in suffixes:
            if s is None:
                # Use glob
                from fnmatch import fnmatch

                if any(
                    fnmatch(entry.name, p)
                    for p in patterns
                    if not p.startswith("*.")
                ):
                    yield entry
                    break
            else:
                if name_lower.endswith(s):
                    yield entry
                    break


__all__ = [
    "PathLike",
    "normalize_path",
    "is_safe_filename",
    "sha256_of_file",
    "sha256_of_bytes",
    "file_size",
    "free_space_bytes",
    "ensure_directory",
    "within_root",
    "atomic_write_text",
    "atomic_write_bytes",
    "copy_file_atomic",
    "AtomicCounter",
    "temporary_directory",
    "iter_files_matching",
]
