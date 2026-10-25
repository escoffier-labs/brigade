"""No-follow retention and validated runs."""

import json
import os
import time

import pytest

from brigade import cli, run_cassette, run_dirfd
from brigade import run_cassette_store as store
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording


def _run(path):
    _record(path)
    (path / "run.json").write_text(json.dumps({"run_id": path.name, "status": "dispatching"}))
    return path


def _expire(run):
    timestamp = time.time() - 40 * 86400
    os.utime(run, (timestamp, timestamp))


@pytest.mark.parametrize("component", ["memory", "outcome", "lock", "hardlink"])
def test_recording_retention_never_writes_through_outcome_links(tmp_path, component, capsys):
    old = _run(tmp_path / ".brigade" / "runs" / "old")
    _expire(old)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "untouched"
    target.write_bytes(b"external data")
    if component in {"memory", "outcome"}:
        link = tmp_path / "memory" if component == "memory" else tmp_path / "memory" / "outcome"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(outside, target_is_directory=True)
    else:
        lock = tmp_path / "memory" / "outcome" / ".records.lock"
        lock.parent.mkdir(parents=True)
        if component == "lock":
            target.unlink()  # A dangling link must not create the outside file either.
            lock.symlink_to(target)
        else:
            os.link(target, lock)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    capsys.readouterr()

    assert _record(old.parent / "current") is not None

    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before
    assert any(d["privacy_class"] == "private" for d in _documents(old))
    assert "cassette retention skipped:" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["missing", "invalid", "array", "symlink", "hardlink", "oversized"])
def test_retention_count_excludes_directories_without_safe_run_metadata(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(store, "KEEP_RUNS", 2)
    root = tmp_path / ".brigade" / "runs"
    old, current = (_run(root / name) for name in ("old", "current"))
    debris = root / "import-staging"
    debris.mkdir()
    metadata = debris / "run.json"
    if kind == "invalid":
        metadata.write_text("{")
    elif kind == "array":
        metadata.write_text("[]")
    elif kind in {"symlink", "hardlink"}:
        if kind == "symlink":
            metadata.symlink_to(current / "run.json")
        else:
            os.link(current / "run.json", metadata)
    elif kind == "oversized":
        metadata.write_bytes(b" " * (store.MAX_ENTRY_BYTES + 1))
    now = time.time()
    for index, path in enumerate((old, current, debris)):
        os.utime(path, (now + index, now + index))

    assert store.retain_private(tmp_path, root, current=current) == 0
    assert any(d["privacy_class"] == "private" for d in _documents(old))


def test_outcome_reference_scan_refuses_parent_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "records.jsonl").write_text("{}\n")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "outcome").symlink_to(outside, target_is_directory=True)
    assert store._pinned_runs(tmp_path) is None


@pytest.mark.parametrize("component", ["events", "run-parent"])
def test_recorder_journal_operations_refuse_symlink_parents(tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "lifecycle.jsonl").write_bytes(b"")
    run = tmp_path / "runs" / "current"
    if component == "events":
        run.mkdir(parents=True)
        (run / "events").symlink_to(outside, target_is_directory=True)
    else:
        (tmp_path / "runs").symlink_to(outside, target_is_directory=True)
        run.mkdir()
        (run / "events").mkdir()
        (run / "events" / "lifecycle.jsonl").write_bytes(b"")
    before = sorted(str(p.relative_to(outside)) for p in outside.rglob("*"))
    assert _record(run) is None
    assert sorted(str(p.relative_to(outside)) for p in outside.rglob("*")) == before


def test_purge_latest_ignores_unsafe_metadata_and_import_debris(tmp_path):
    root = tmp_path / ".brigade" / "runs"
    run = _run(root / "actual")
    debris = root / "import-staging"
    debris.mkdir()
    (debris / "run.json").symlink_to(run / "run.json")
    assert run_cassette.purge_cli("latest", cwd=tmp_path) == 0
    assert not _documents(run)
    assert (debris / "run.json").is_symlink()


def test_purge_explicit_directory_requires_safe_run_metadata(tmp_path, capsys):
    run = tmp_path / "not-a-run"
    _record(run)
    assert run_cassette.purge_cli(str(run), cwd=tmp_path) == 2
    assert "cassette purge refused unsafe sidecars" in capsys.readouterr().err
    assert any(d["privacy_class"] == "private" for d in _documents(run))


@pytest.mark.parametrize("bound_stat_refusal", [False, True], ids=["native", "nt-refusal"])
def test_recorder_refuses_symlinked_final_journal(tmp_path, monkeypatch, bound_stat_refusal):
    run = tmp_path / "run"
    (run / "events").mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"")
    (run / "events" / "lifecycle.jsonl").symlink_to(outside)
    if bound_stat_refusal:
        original_stat = run_dirfd.BoundRunDir.stat

        def refuse_journal_stat(bound, components, name):
            if components == ("events",) and name == "lifecycle.jsonl":
                # NT no-follow metadata lookup refuses reparse points rather
                # than returning the symlink metadata that POSIX provides.
                raise OSError("refusing reparse point")
            return original_stat(bound, components, name)

        monkeypatch.setattr(run_dirfd.BoundRunDir, "stat", refuse_journal_stat)
    assert _record(run) is None
    assert not any(d["privacy_class"] == "private" for d in _documents(run))
    assert not list((run / "events" / "cassettes").glob("*"))
    assert outside.read_bytes() == b""


def test_purge_accepts_current_directory_with_valid_metadata(tmp_path, monkeypatch):
    run = _run(tmp_path / "run")
    monkeypatch.chdir(run)
    assert run_cassette.purge_cli(".", cwd=run) == 0
    assert not _documents(run)


def test_purge_bare_name_uses_explicit_workspace_not_process_directory(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    intended = _run(workspace / ".brigade" / "runs" / "current")
    unrelated = _run(tmp_path / "elsewhere" / "current")
    before = _documents(unrelated)
    monkeypatch.chdir(unrelated.parent)

    assert cli.main(["runs", "cassette", "purge", "current", "--cwd", str(workspace)]) == 0

    assert not _documents(intended)
    assert _documents(unrelated) == before


def test_run_inventory_byte_cap_refuses_retention_and_latest_purge(tmp_path, monkeypatch, capsys):
    old = _run(tmp_path / ".brigade" / "runs" / "old")
    _expire(old)
    monkeypatch.setattr(store, "MAX_RUN_SCAN_BYTES", 1, raising=False)
    assert store.retain_private(tmp_path, old.parent, current=old.parent / "current") == 0
    assert "cassette retention skipped:" in capsys.readouterr().err
    assert run_cassette.purge_cli("latest", cwd=tmp_path) == 2
    assert any(d["privacy_class"] == "private" for d in _documents(old))
