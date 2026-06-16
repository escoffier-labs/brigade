"""Tests for brigade.localio shared helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import localio


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
