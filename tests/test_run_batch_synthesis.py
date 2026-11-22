"""Optional batch synthesis: one budgeted, receipted call separate from tasks."""

import json
from unittest.mock import Mock

import pytest

from brigade import agents, cli, run_batch_synthesis
from brigade.aboyeur import planning
from brigade.run_batch import SCHEMA
from tests._home import set_home
from tests.test_run_batch_execute import task

# Reuse the guarded-orchestrator batch harness fixture from the execution tests.
pytest_plugins = ("tests.test_run_batch_execute",)

SYNTHESIS = {"text": "Summarize the results", "seat": "writer"}


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    set_home(monkeypatch, tmp_path / "home")


def add_writer(root):
    roster = root / ".brigade" / "roster.toml"
    roster.write_text(roster.read_text() + '\n[agents.writer]\ncli = "codex"\nrole = "review"\n')


def run(setup, capsys, tasks, *, synthesis=None, extra_args=()):
    root, _ = setup
    payload = {"schema": SCHEMA, "request_id": "req", "tasks": tasks}
    if synthesis is not None:
        payload["synthesis"] = synthesis
    path = root / "batch.json"
    path.write_text(json.dumps(payload))
    rc = cli.main(["run", "batch", str(path), "--cwd", str(root), "--json", *extra_args])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out), captured.err


def run_dir_of(setup, result):
    return setup[0] / ".brigade" / "runs" / result["run_id"]


def synth_receipt(run_dir):
    return json.loads((run_dir / "batch-synthesis.json").read_text())


def envelopes(run_dir):
    path = run_dir / "message-envelopes.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def synthesized(setup, monkeypatch, *, ok=True, text="joined answer", crash=False):
    root, calls = setup
    receipts = []

    def fake(roster, prompt, *, seat=None, **kwargs):
        receipts.append(seat)
        calls.append("synthesis")
        if crash:
            raise KeyboardInterrupt
        return agents.AgentResult(
            text=text,
            ok=ok,
            detail="" if ok else "synthesis failed",
            failure_phase=None if ok else "synthesis",
            failure_kind=None if ok else "agent-error",
        )

    monkeypatch.setattr(planning, "_run_orchestrator", fake)
    return receipts


def task_calls(calls):
    return [call for call in calls if isinstance(call, str) and call.startswith("You are Brigade worker")]


def test_absent_synthesis_makes_no_call_and_keeps_output(setup, capsys):
    rc, result, err = run(setup, capsys, [task("a")])
    assert rc == 0, err
    assert result["synthesis"] is None
    run_dir = run_dir_of(setup, result)
    assert not (run_dir / "batch-synthesis.json").exists()
    # The per-task summary consumed by existing callers is unchanged.
    summary = json.loads((run_dir / "synthesis.json").read_text())["result"]
    assert "a: completed" in summary["text"] and summary["ok"] is True


def test_synthesis_runs_once_on_requested_seat_after_tasks(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    rc, result, err = run(setup, capsys, [task("a"), task("b")], synthesis=SYNTHESIS)
    assert rc == 0, err
    assert seats == ["writer"]
    assert len(task_calls(calls)) == 2 and calls[-1] == "synthesis"
    run_dir = run_dir_of(setup, result)
    receipt = synth_receipt(run_dir)
    assert receipt["status"] == "completed"
    assert receipt["seat"] == "writer"
    assert [entry["task_id"] for entry in receipt["inputs"]] == ["a", "b"]
    assert all(entry["result_id"] for entry in receipt["inputs"])
    assert receipt["output_ref"] == "final.txt"
    assert receipt["output_bytes"] == len((run_dir / "final.txt").read_bytes())
    assert result["synthesis"]["status"] == "completed"
    assert result["synthesis"]["seat"] == "writer"
    assert all(entry["status"] == "completed" for entry in result["tasks"].values())
    synthesis = [entry for entry in envelopes(run_dir) if entry["phase"] == "synthesis"]
    request = next(entry for entry in synthesis if entry["envelope"]["source"]["kind"] == "synthesis-request")
    answer = next(entry for entry in synthesis if entry["envelope"]["source"]["kind"] == "synthesis-result")
    assert request["to_seat"] == "writer" and answer["from_seat"] == "writer"
    # Exactly one dispatch unit is reserved for the synthesis seat.
    events = [
        json.loads(line) for line in (run_dir / "events" / "lifecycle.jsonl").read_text().splitlines() if line.strip()
    ]
    requested = [
        event
        for event in events
        if event["event_type"] == "run.dispatch.requested" and event["payload"]["seat"] == "writer"
    ]
    assert len(requested) == 1


def test_synthesis_failure_keeps_task_results_and_exits_one(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    synthesized(setup, monkeypatch, ok=False, text="")
    rc, result, err = run(setup, capsys, [task("a"), task("b")], synthesis=SYNTHESIS)
    assert rc == 1
    assert "synthesis: failed" in err
    assert all(entry["status"] == "completed" for entry in result["tasks"].values())
    run_dir = run_dir_of(setup, result)
    assert synth_receipt(run_dir)["status"] == "failed"
    # The task-derived summary still reports the completed tasks.
    summary = json.loads((run_dir / "synthesis.json").read_text())["result"]
    assert summary["ok"] is True and "a: completed" in summary["text"]
    assert "a: completed" in (run_dir / "final.txt").read_text()


def test_budget_exhaustion_refuses_synthesis_without_calling(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    budget = root / "budget.json"
    budget.write_text(
        json.dumps(
            {
                "schema": "brigade.run_budget.v1",
                "schema_version": 1,
                "ceilings": {"worker_dispatch_count": 1},
            }
        )
    )
    rc, result, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS, extra_args=("--run-budget", str(budget)))
    assert rc == 1
    assert "synthesis: refused-budget" in err
    assert seats == []
    assert len(calls) == 1
    assert result["synthesis"]["status"] == "refused-budget"
    assert result["tasks"]["a"]["status"] == "completed"
    assert synth_receipt(run_dir_of(setup, result))["status"] == "refused-budget"


def test_retry_after_completed_synthesis_makes_no_second_call(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 0, err
    assert seats == ["writer"]
    rc, second, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 0, err
    assert seats == ["writer"]
    assert len(task_calls(calls)) == 1
    assert second == first


def test_settled_retry_runs_synthesis_only(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch)
    original = run_batch_synthesis.perform
    monkeypatch.setattr(run_batch_synthesis, "perform", Mock(side_effect=KeyboardInterrupt))
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1
    assert first["tasks"]["a"]["status"] == "completed"
    run_dir = run_dir_of(setup, first)
    assert not (run_dir / "batch-synthesis.json").exists()
    assert not (run_dir / "batch-synthesis-dispatch.json").exists()
    monkeypatch.setattr(run_batch_synthesis, "perform", original)
    rc, second, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 0, err
    # Only the owed synthesis call ran; the task never launched again.
    assert len(task_calls(calls)) == 1
    assert seats == ["writer"]
    assert second["tasks"]["a"]["status"] == "completed"
    assert synth_receipt(run_dir)["status"] == "completed"


def test_reserved_dispatch_without_receipt_is_indeterminate(setup, capsys, monkeypatch):
    root, calls = setup
    add_writer(root)
    seats = synthesized(setup, monkeypatch, crash=True)
    rc, first, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1
    assert first["tasks"]["a"]["status"] == "completed"
    run_dir = run_dir_of(setup, first)
    assert (run_dir / "batch-synthesis-dispatch.json").is_file()
    rc, second, err = run(setup, capsys, [task("a")], synthesis=SYNTHESIS)
    assert rc == 1
    # The reserved call may have run, so it is never called a second time.
    assert seats == ["writer"]
    assert synth_receipt(run_dir)["status"] == "indeterminate"
    assert second["synthesis"]["status"] == "indeterminate"
    assert "synthesis: indeterminate" in err


def test_unadmitted_synthesis_seat_refuses_before_any_launch(setup, capsys, monkeypatch):
    root, calls = setup
    monkeypatch.setattr(planning, "_run_orchestrator", Mock(side_effect=AssertionError("synthesis called")))
    rc, result, err = run(setup, capsys, [task("a")], synthesis={"text": "Summarize", "seat": "ghost"})
    assert rc == 2
    assert result["error"]["code"] == "invalid-seat"
    assert calls == []
    assert not list(root.glob(".brigade/runs/*/batch-result-*.json"))
