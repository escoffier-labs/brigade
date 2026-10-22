"""Cassette facts must not strand a run after the worker process dies."""

import json

import pytest

from brigade import aboyeur, agents, cli, run_journal, run_transport, runguard
from brigade import run_cassette_store as store
from brigade.roster import Agent, Roster
from tests.test_run_cassette import _documents
from tests.test_run_cassette import auto_recording as auto_recording
from tests.test_runs_cmd import _write_lock_owner


@pytest.mark.parametrize("tail", ["recorded", "repeated", "stopped", "uncovered"])
@pytest.mark.parametrize("missing_receipt", [False, True])
def test_recover_after_dispatch_cassette_without_next_transition(tmp_path, monkeypatch, capsys, tail, missing_receipt):
    monkeypatch.setattr(agents, "run_agent", lambda *a, **kw: agents.AgentResult(text="worker answer", ok=True))
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run.mkdir(parents=True)
    journal = run / "events" / "lifecycle.jsonl"
    with runguard.run_lock(tmp_path, run_dir=run):
        aboyeur._write_json(
            run / "run.json",
            {
                "schema": "brigade.run.v1",
                "status": "dispatching",
                "cwd": str(tmp_path),
                "task": "coordinator task",
                "lifecycle_journal_requested": True,
            },
        )
        if tail == "uncovered":
            # A cassette must not hide an earlier uncheckpointed status fact.
            report = run_journal.read_journal(journal)
            run_journal.append_event(
                journal,
                run_id=run.name,
                event_type="run.result-processing.started",
                payload={"detail": "result-processing"},
                idempotency_key="uncovered-status",
                expected_previous_sequence=report.events[-1].sequence,
                workspace=tmp_path,
            )
        if tail == "stopped":
            monkeypatch.setattr(store, "MAX_ENTRY_BYTES", 128)
        for _ in range(2 if tail == "repeated" else 1):
            result = run_transport.dispatch(
                [run_transport.Assignment(worker="worker", task="coordinator task")],
                Roster(orchestrator="chef", agents={"worker": Agent(name="worker", cli="codex", role="write")}),
                build_prompt=lambda *a, **kw: "worker prompt",
                run_appserver_worker=lambda *a, **kw: None,
                event_writer=lambda *a, **kw: None,
                cwd=tmp_path,
                output_dir=run,
                direct=True,
            )[0]
            assert result.ok and result.text == "worker answer"
        assert run_journal.read_journal(journal).events[-1].event_type == "run.cassette.recorded"
        if tail == "stopped":
            assert any(d.get("status") == "stopped" for d in _documents(run))

    # Retain exactly the artifacts present at the crash, with a dead owner.
    _write_lock_owner(tmp_path, run)
    if missing_receipt:
        (run / "run.json").unlink()
    before = journal.read_bytes()
    rc = cli.main(["runs", "recover", str(run), "--cwd", str(tmp_path)])
    if tail == "uncovered":
        assert rc == 2
        assert "journal tail is not covered by the latest checkpoint" in capsys.readouterr().err
        assert journal.read_bytes() == before
        return
    assert rc == 0, capsys.readouterr().err
    recovered = json.loads((run / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "coordinator task"
    assert not (tmp_path / ".brigade" / "run.lock").exists()
