"""Regression coverage for coordinator policy and atomic outcome pins."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest

from brigade import aboyeur, agents, cli, outcome, outcome_cmd
from brigade import run_cassette_store as store
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording
from tests.test_run_worktree import _git_repo


@pytest.mark.parametrize("mode,classes", [("off", set()), ("metadata-only", {"scrubbed", "public-metadata"})])
def test_worktree_uses_coordinator_recording_policy(tmp_path, monkeypatch, mode, classes):
    repo = _git_repo(tmp_path)
    (repo / ".brigade" / "config.json").write_text(
        json.dumps({"version": 1, "depth": "repo", "harnesses": ["codex"], "run": {"record": mode}})
    )
    seen = []

    def worker(*args, **kwargs):
        cwd = kwargs["cwd"]
        assert cwd != repo
        assert not (cwd / ".brigade" / "config.json").exists()
        (cwd / "tracked.txt").write_text("worker change\n")
        seen.append(cwd)
        return agents.AgentResult(text="private-worktree-answer", ok=True, transport="exec")

    def no_control_socket(self):
        raise aboyeur.run_control.ControlError("control socket unavailable under test")

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(agents, "run_agent", worker)
    monkeypatch.setattr(aboyeur.run_control.ControlServer, "start", no_control_socket)
    run = tmp_path / "custom-runs" / mode
    assert (
        cli.main(
            [
                "run",
                "inspect task",
                "--cwd",
                str(repo),
                "--output-dir",
                str(run),
                "--worktree",
                "--worker",
                "coder",
                "--no-code-graph",
                "--no-route",
            ]
        )
        == 0
    )
    assert len(seen) == 1
    assert {d["privacy_class"] for d in _documents(run)} == classes
    assert "private-worktree-answer" not in json.dumps(_documents(run))
    if mode == "off":
        assert not (run / "events" / "cassettes").exists()


def test_retention_waits_for_inflight_outcome_pin(tmp_path, monkeypatch):
    run = tmp_path / ".brigade" / "runs" / "old"
    _record(run)
    (run / "run.json").write_text(json.dumps({"run_id": run.name}))
    expired = time.time() - 40 * 86400
    os.utime(run, (expired, expired))
    writer_locked, retention_started, writer_done = Event(), Event(), Event()
    retention_thread = get_ident()
    acquire = outcome_cmd._acquire_file_lock
    validate = outcome_cmd._validate_completed_ledger
    pinned_runs = store._pinned_runs

    def acquire_lock(handle, **kwargs):
        if get_ident() == retention_thread:
            retention_started.set()
        return acquire(handle, **kwargs)

    def validate_ledger(path):
        digest = validate(path)
        writer_locked.set()
        assert retention_started.wait(5)
        return digest

    def read_pins(workspace):
        pins = pinned_runs(workspace)
        retention_started.set()
        assert writer_done.wait(5)
        return pins

    def capture():
        try:
            outcome_cmd.append_records(
                tmp_path,
                [
                    outcome.OutcomeRecord(
                        "brigade-work", "skill", "task", "run", 0, str(run / "run.json"), "2026-10-20T12:00:00Z"
                    )
                ],
            )
        finally:
            writer_done.set()

    monkeypatch.setattr(outcome_cmd, "_acquire_file_lock", acquire_lock)
    monkeypatch.setattr(outcome_cmd, "_validate_completed_ledger", validate_ledger)
    monkeypatch.setattr(store, "_pinned_runs", read_pins)
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(capture)
        try:
            assert writer_locked.wait(5)
            removed = store.retain_private(tmp_path, run.parent, current=run.parent / "new")
        finally:
            retention_started.set()
        writer.result(timeout=5)
    assert removed == 0
    assert any(d["privacy_class"] == "private" for d in _documents(run))
