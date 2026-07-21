"""Tests for brigade.localio shared helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import localio


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
    blob.write_text("hello\n")
    assert localio.file_sha256(blob) == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"


def test_canonical_json_digest_excludes_top_level_keys_only():
    base = {"a": 1, "nested": {"digests": "evidence-digest-1"}, "digests": {"receipt_sha256": "x"}}
    edited = {"a": 1, "nested": {"digests": "evidence-digest-TAMPERED"}, "digests": {"receipt_sha256": "y"}}

    base_digest = localio.canonical_json_digest(base, exclude_keys={"digests"})
    edited_digest = localio.canonical_json_digest(edited, exclude_keys={"digests"})

    assert base_digest != edited_digest
