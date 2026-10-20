"""Bug diagnostic budgets do not constrain public receipt exports."""

from __future__ import annotations

import json

import pytest

from brigade import bug_collect
from brigade.localio import write_json


def _run(target, *, run_id="run-1"):
    directory = target / ".brigade" / "runs" / run_id
    write_json(directory / "run.json", {"started_at": "2026-01-01T00:00:00Z"})
    write_json(directory / "roster.json", {"agents": {"worker": {}}})
    write_json(directory / "worker-results.json", {"results": [{"worker": "worker", "ok": True}]})
    return directory


def _verify_receipt(target, run_id):
    path = target / ".brigade" / "work" / "verify-runs" / run_id / "receipt.json"
    write_json(
        path,
        {
            "run_id": run_id,
            "status": "completed",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "commands": [{"command": "pytest -q"}],
        },
    )
    return path


@pytest.mark.parametrize("kind", ["oversize", "invalid-utf8", "deep-json"])
def test_bug_receipt_reader_bounds_bytes_before_decoding(tmp_path, monkeypatch, kind):
    import io
    from pathlib import Path

    payload = {
        "oversize": b'{"log":"' + b"x" * (256 * 1024) + b'"}',
        "invalid-utf8": b"\xff",
        "deep-json": b"[" * 2000 + b"]" * 2000,
    }[kind]
    reads = []

    class ReceiptInput(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 256 * 1024 + 1
            reads.append(size)
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *a, **k: ReceiptInput(payload))
    assert bug_collect._object(tmp_path / "receipt.json") is None
    assert len(reads) == 1


@pytest.mark.parametrize("source", ["runs", "verify"])
def test_bug_diagnostics_caps_files_and_reads_newest_first(tmp_path, monkeypatch, source):
    import os

    directories = []
    for i in range(4):
        if source == "runs":
            directory = _run(tmp_path, run_id=f"run-{i}")
        else:
            directory = _verify_receipt(tmp_path, f"verify-{i}").parent
        os.utime(directory, (100 + i, 100 + i))
        directories.append(directory)
    monkeypatch.setattr(bug_collect, "_RECEIPT_DIR_LIMIT", 2, raising=False)
    original = bug_collect._object
    reads = []

    def read(path):
        reads.append(path.parent)
        return original(path)

    monkeypatch.setattr(bug_collect, "_object", read)
    value = bug_collect.recent_failures(tmp_path)
    assert value == {"runs": [], "verify": None}
    assert list(dict.fromkeys(reads)) == directories[:1:-1]
    assert len(reads) == (6 if source == "runs" else 2)


def test_bug_receipt_directory_enumeration_has_work_budget(tmp_path, monkeypatch):
    import os

    root = tmp_path / ".brigade" / "runs"
    root.mkdir(parents=True)
    for i in range(5):
        (root / f"run-{i}").mkdir()
    original = os.scandir
    visited = []

    class Entries:
        def __enter__(self):
            self.entries = original(root)
            return self

        def __exit__(self, *args):
            self.entries.close()

        def __iter__(self):
            for entry in self.entries:
                visited.append(entry.name)
                assert len(visited) <= 2, "directory scan exceeded the entry budget"
                yield entry

    monkeypatch.setattr(bug_collect, "_DIRECTORY_ENTRY_LIMIT", 2, raising=False)
    monkeypatch.setattr(os, "scandir", lambda path: Entries() if path == root else original(path))
    bug_collect.recent_failures(tmp_path)
    assert len(visited) == 2


@pytest.mark.parametrize("source", ["runs", "verify"])
def test_bug_diagnostics_caps_row_work(tmp_path, monkeypatch, source):
    monkeypatch.setattr(bug_collect, "_PROJECTION_ROW_LIMIT", 2)
    if source == "runs":
        path = _run(tmp_path) / "worker-results.json"
        key = "results"
        failure = {"worker": "worker", "ok": False}
    else:
        path = _verify_receipt(tmp_path, "verify-1")
        key = "commands"
        failure = {"command": "pytest -q", "status": "failed"}
    receipt = json.loads(path.read_text())
    receipt[key] *= 2
    receipt[key].append(failure)
    path.write_text(json.dumps(receipt))
    assert bug_collect.recent_failures(tmp_path) == {"runs": [], "verify": None}


def test_latest_failed_verify_uses_latest_command_time(tmp_path):
    first = _verify_receipt(tmp_path, "verify-first")
    receipt = json.loads(first.read_text())
    receipt["commands"] = [
        {"command": "pytest -q", "status": "failed", "started_at": f"2026-01-0{i}T00:00:00Z"} for i in (1, 4)
    ]
    write_json(first, receipt)
    second = _verify_receipt(tmp_path, "verify-second")
    other = json.loads(second.read_text())
    other.update(status="failed", started_at="2026-01-03T00:00:00Z")
    write_json(second, other)
    assert bug_collect.recent_failures(tmp_path)["verify"]["id"] == "verify-first"
