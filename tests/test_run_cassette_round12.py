"""Recorder transparency, run ownership, and stable retention age."""

import json
import os
import time
from datetime import datetime, timezone

import pytest

from brigade import agents, proc, run_cassette, run_journal, run_lifecycle, run_shadow, runguard
from brigade import run_cassette_store as store
from brigade.roster import Agent, Roster
from brigade.run_seat import SeatInvoker
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording


def test_seat_invoker_completion_error_reaches_scheduler_unchanged(tmp_path, monkeypatch):
    error = run_lifecycle.LifecycleJournalError("lifecycle journal append requires the active run lock")
    observed = []
    original = run_cassette.record_dispatch_result

    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except run_lifecycle.LifecycleJournalError as exc:
            observed.append(exc)
            raise

    def completed(agent, attempt):
        raise error

    monkeypatch.setattr(run_cassette, "record_dispatch_result", observe)
    monkeypatch.setattr(agents, "run_agent", lambda *a, **kw: agents.AgentResult(text="good answer", ok=True))
    run = tmp_path / ".brigade" / "runs" / "current"
    invoker = SeatInvoker(
        roster=Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="read")}),
        cwd=tmp_path,
        run_id=run.name,
        process_registry=proc.ProcessRegistry(),
        output_dir=run,
        on_dispatch_requested=lambda agent: 1,
        on_dispatch_completed=completed,
    )
    result = invoker.invoke(seat="worker", phase="research.plan", prompt="task", timeout=10)
    assert observed == [error]
    # Main's scheduler converts ordinary exceptions, preserving their detail.
    assert not result.ok and result.detail == str(error)
    assert result.failure_kind is None
    assert str(error) not in json.dumps(_documents(run))


def test_lost_owner_cannot_append_cassette_or_shadow_fact(tmp_path, monkeypatch):
    run = tmp_path / ".brigade" / "runs" / "current"
    journal = run / "events" / "lifecycle.jsonl"
    run_journal.ensure_journal(journal)
    (run / "run.json").write_text(json.dumps({"lifecycle_journal_requested": True}))
    shadows = []
    monkeypatch.setattr(run_shadow, "record_shadow_comparison", lambda *args: shadows.append(args))
    with runguard.run_lock(tmp_path, run_dir=run):
        assert runguard.is_active_run_owner(tmp_path, run)
    before = journal.read_bytes()
    assert (
        run_cassette.record_result(
            run_dir=run,
            workspace=tmp_path,
            agent=Agent(name="worker", cli="codex", role="read"),
            task="task",
            prompt="prompt",
            result=agents.AgentResult(text="answer", ok=True),
            read_only=True,
            sandbox="read-only",
        )
        is None
    )
    assert journal.read_bytes() == before
    assert not shadows
    assert not list((run / "events" / "cassettes").glob("*"))


@pytest.mark.parametrize("reason", ["age", "count"])
def test_retention_uses_started_at_despite_run_directory_rewrite(tmp_path, monkeypatch, reason):
    monkeypatch.setattr(store, "KEEP_RUNS", 2 if reason == "count" else 50)
    root = tmp_path / ".brigade" / "runs"
    now = time.time()
    runs = [root / name for name in ("old", "newer", "current")]
    for index, run in enumerate(runs):
        _record(run)
        age_days = 40 if reason == "age" and index == 0 else 3 - index
        started = now - age_days * 86400
        (run / "run.json").write_text(
            json.dumps({"run_id": run.name, "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat()})
        )
        # An old run was rewritten most recently, while newer runs look old on disk.
        modified = now + 100 if index == 0 else now - 60 * 86400
        os.utime(run, (modified, modified))
    store.retain_private(tmp_path, root, current=runs[-1])
    assert not any(d["privacy_class"] == "private" for d in _documents(runs[0]))
    assert any(d["privacy_class"] == "private" for d in _documents(runs[1]))
