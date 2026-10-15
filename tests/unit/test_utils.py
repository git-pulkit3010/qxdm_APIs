"""Unit tests for utility helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from qxdm_automation.utils import (
    atomic_write_bytes,
    atomic_write_text,
    copy_file_atomic,
    free_space_bytes,
    is_safe_filename,
    iter_files_matching,
    normalize_path,
    sha256_of_bytes,
    sha256_of_file,
    within_root,
)


def test_is_safe_filename() -> None:
    assert is_safe_filename("QXDM_Log")
    assert is_safe_filename("with-dashes-and_underscores")
    assert not is_safe_filename("../bad")
    assert not is_safe_filename("a/b")
    assert not is_safe_filename("C:\\bad")
    assert not is_safe_filename("a:b")
    assert not is_safe_filename("a*b")
    assert not is_safe_filename("")
    assert not is_safe_filename(".")
    assert not is_safe_filename("..")
    assert not is_safe_filename("a ")
    assert not is_safe_filename("a.")


def test_sha256_of_bytes_deterministic() -> None:
    assert sha256_of_bytes(b"abc") == sha256_of_bytes(b"abc")
    assert sha256_of_bytes(b"abc") != sha256_of_bytes(b"abd")


def test_sha256_of_file(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello world")
    assert sha256_of_file(p) == sha256_of_bytes(b"hello world")


def test_atomic_write_text(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    atomic_write_text(target, "hi", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "hi"
    assert not (target.with_name(target.name + ".partial")).exists()


def test_atomic_write_bytes(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    atomic_write_bytes(target, b"\x00\x01")
    assert target.read_bytes() == b"\x00\x01"
    assert not (target.with_name(target.name + ".partial")).exists()


def test_copy_file_atomic_same_volume(tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(b"some data")
    dst = tmp_path / "dst.bin"
    copy_file_atomic(src, dst)
    assert dst.read_bytes() == b"some data"
    assert not (dst.with_name(dst.name + ".partial")).exists()


def test_within_root() -> None:
    root = Path("/tmp")
    assert within_root(Path("/tmp/a"), root)
    assert not within_root(Path("/etc"), root)


def test_iter_files_matching(tmp_path: Path) -> None:
    (tmp_path / "a.dlf").write_bytes(b"a")
    (tmp_path / "b.DLF").write_bytes(b"b")
    (tmp_path / "c.bin").write_bytes(b"c")
    (tmp_path / "d.txt").write_text("d")
    matched = sorted(p.name for p in iter_files_matching(tmp_path, ["*.dlf"]))
    assert matched == ["a.dlf", "b.DLF"]


def test_free_space_bytes_smoke(tmp_path: Path) -> None:
    fs = free_space_bytes(tmp_path)
    assert fs >= 0
