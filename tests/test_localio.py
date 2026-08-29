"""Tests for brigade.localio shared helpers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from brigade import localio, run_dirfd, run_journal


def test_read_json_dict_invalid_utf8_returns_none(tmp_path: Path):
    path = tmp_path / "invalid-utf8.json"
    path.write_bytes(b'{"value":"\xff"}')

    assert localio.read_json_dict(path) is None


def test_write_json_round_trips_and_is_sorted(tmp_path: Path):
    path = tmp_path / "nested" / "receipt.json"
    localio.write_json(path, {"b": 2, "a": 1})
    assert json.loads(path.read_text()) == {"a": 1, "b": 2}
    # key-sorted with a trailing newline keeps receipts diff-stable
    assert path.read_text() == '{\n  "a": 1,\n  "b": 2\n}\n'


def test_write_json_is_atomic_and_leaves_original_intact_on_failure(tmp_path: Path, monkeypatch):
    path = tmp_path / "receipt.json"
    localio.write_json(path, {"ok": True})
    original = path.read_text()

    # Force the atomic swap to fail after the temp file is written. The existing
    # receipt must survive intact (no torn or truncated write) and the temp file
    # must be cleaned up rather than left as a turd in the directory.
    def _boom(src, dst):
        raise OSError("simulated disk full")

    monkeypatch.setattr(localio.os, "replace", _boom)
    with pytest.raises(OSError):
        localio.write_json(path, {"ok": False, "padding": "x" * 4096})

    assert path.read_text() == original
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name != "receipt.json")
    assert leftovers == []


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX durability guarantee")
def test_write_text_atomic_fsyncs_parent_after_replace(tmp_path: Path, monkeypatch):
    path = tmp_path / "receipt.txt"
    calls: list[tuple[str, bool]] = []
    real_replace = localio.os.replace
    real_fsync = localio.os.fsync

    def tracking_replace(source, destination):
        calls.append(("replace", False))
        return real_replace(source, destination)

    def tracking_fsync(fd):
        info = os.fstat(fd)
        calls.append(("fsync", stat.S_ISDIR(info.st_mode)))
        return real_fsync(fd)

    monkeypatch.setattr(localio.os, "replace", tracking_replace)
    monkeypatch.setattr(localio.os, "fsync", tracking_fsync)

    localio.write_text_atomic(path, "durable\n")

    replace_index = calls.index(("replace", False))
    assert any(kind == "fsync" and is_directory for kind, is_directory in calls[replace_index + 1 :])


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is a POSIX symlink hardening flag"
)
def test_write_text_atomic_opens_parent_without_following_symlinks(tmp_path: Path, monkeypatch):
    path = tmp_path / "receipt.txt"
    real_open = localio.os.open
    flags_seen: list[int] = []

    def tracking_open(target, flags, *args, **kwargs):
        if target == path.parent:
            flags_seen.append(flags)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(localio.os, "open", tracking_open)

    localio.write_text_atomic(path, "durable\n")

    assert flags_seen and flags_seen[-1] & os.O_NOFOLLOW


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is a POSIX symlink hardening flag"
)
def test_write_text_atomic_allows_directory_symlink_parent(tmp_path: Path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlinked_parent = tmp_path / "symlinked-parent"
    symlinked_parent.symlink_to(real_parent, target_is_directory=True)
    path = symlinked_parent / "receipt.txt"

    localio.write_text_atomic(path, "durable through symlink\n")

    assert path.read_text() == "durable through symlink\n"
    assert (real_parent / "receipt.txt").read_text() == "durable through symlink\n"


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX durability guarantee")
def test_run_journal_fsync_directory_refuses_directory_symlink(tmp_path: Path):
    real_directory = tmp_path / "real-directory"
    real_directory.mkdir()
    symlinked_directory = tmp_path / "symlinked-directory"
    symlinked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(run_journal.RunJournalError, match="refusing symlinked path"):
        run_journal._fsync_directory(symlinked_directory)


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is a POSIX durability guarantee")
def test_write_text_atomic_reports_directory_fsync_failure_after_replace(tmp_path: Path, monkeypatch):
    path = tmp_path / "receipt.txt"
    real_fsync = localio.os.fsync
    real_close = localio.os.close

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory fsync failure")
        return real_fsync(fd)

    def also_fail_directory_close(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("simulated directory close failure")
        return real_close(fd)

    monkeypatch.setattr(localio.os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(localio.os, "close", also_fail_directory_close)

    with pytest.raises(OSError, match="simulated directory fsync failure"):
        localio.write_text_atomic(path, "published\n")

    assert path.read_text() == "published\n"


def test_write_text_atomic_skips_parent_fsync_when_platform_has_no_support(tmp_path: Path, monkeypatch):
    path = tmp_path / "receipt.txt"
    monkeypatch.setattr(localio, "_supports_directory_fsync", lambda: False)
    real_fsync = localio.os.fsync

    def reject_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise AssertionError("directory fsync should be skipped")
        return real_fsync(fd)

    monkeypatch.setattr(localio.os, "fsync", reject_directory_fsync)

    localio.write_text_atomic(path, "portable\n")

    assert path.read_text() == "portable\n"


def test_canonical_json_digest_excludes_top_level_keys_and_hashes_files(tmp_path: Path):
    payload = {
        "b": 2,
        "a": {"keep": True, "digest": "kept-as-content"},
        "items": [{"digest": "kept-as-content"}, {"value": "kept"}],
        "digests": {"receipt_sha256": "ignore"},
    }
    expected = localio.canonical_json_digest(
        {
            "a": {"keep": True, "digest": "kept-as-content"},
            "b": 2,
            "items": [{"digest": "kept-as-content"}, {"value": "kept"}],
        }
    )

    assert localio.canonical_json_digest(payload, exclude_keys={"digest", "digests"}) == expected

    blob = tmp_path / "blob.txt"
    blob.write_bytes(b"hello\n")  # bytes: write_text emits CRLF on Windows
    assert localio.file_sha256(blob) == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"


def test_canonical_json_digest_excludes_top_level_keys_only():
    base = {"a": 1, "nested": {"digests": "evidence-digest-1"}, "digests": {"receipt_sha256": "x"}}
    edited = {"a": 1, "nested": {"digests": "evidence-digest-TAMPERED"}, "digests": {"receipt_sha256": "y"}}

    base_digest = localio.canonical_json_digest(base, exclude_keys={"digests"})
    edited_digest = localio.canonical_json_digest(edited, exclude_keys={"digests"})

    assert base_digest != edited_digest


def test_write_text_atomic_writes_lf_not_platform_newline(tmp_path: Path):
    """Regression: on Windows, text mode without newline= translated LF to CRLF.

    run.json is written through this helper, and run_checkpoint compares it byte
    for byte against a canonical form that only ever contains LF, so a CRLF file
    could never match and every dispatch failed with "base bytes differ from
    writer canonical form".
    """
    path = tmp_path / "run.json"
    localio.write_text_atomic(path, json.dumps({"a": 1, "b": 2}, indent=2, sort_keys=True) + "\n")

    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.count(b"\n") == 4


def test_write_text_atomic_unchanged_without_a_binding(tmp_path: Path):
    path = tmp_path / "nested" / "receipt.txt"
    payload = "line\nsecond\n"
    assert run_dirfd.active_binding_for(path) is None
    localio.write_text_atomic(path, payload)
    assert path.read_bytes() == payload.encode("utf-8")
    assert b"\r\n" not in path.read_bytes()
    metadata = path.stat()
    assert stat.S_ISREG(metadata.st_mode)


def test_write_bytes_atomic_unchanged_without_a_binding(tmp_path: Path):
    path = tmp_path / "nested" / "blob.bin"
    payload = b"\x00\xffhello\n"
    assert run_dirfd.active_binding_for(path) is None
    localio.write_bytes_atomic(path, payload)
    assert path.read_bytes() == payload


def test_write_text_exclusive_writes_lf_not_platform_newline(tmp_path: Path):
    """The exclusive writer shares write_text_atomic's newline exposure."""
    path = tmp_path / "once.json"
    localio.write_text_exclusive(path, "first\nsecond\n")

    assert path.read_bytes() == b"first\nsecond\n"
