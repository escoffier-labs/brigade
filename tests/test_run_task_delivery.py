"""Pull delivery and acknowledgement for durable task results (#2049)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from tests import test_run_task_cancel as support

from brigade import cli, run_task_inbox as inbox, run_tasks, runguard

run_site = support.run_site
held_run = support.held_run
facts = support.facts
state_of = support.state_of


def _observe(run_dir, workspace, task_id, status="completed"):
    with runguard.run_lock(workspace, run_dir=run_dir):
        handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id)
        return run_tasks.observe_task(
            run_dir,
            workspace=workspace,
            handle=handle,
            observation=run_tasks.TaskObservation(
                status=status,
                containment="active"
                if status == "waiting-for-children"
                else "unknown"
                if status == "unknown"
                else "quiescent",
            ),
        )


def _next(run_dir, capsys, *extra):
    rc = cli.main(["runs", "tasks", "next", str(run_dir), "--json", *extra])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out) if captured.out else None, captured


def _ack(run_dir, task_id, result_id, capsys):
    rc = cli.main(["runs", "tasks", "ack", str(run_dir), task_id, "--result-id", result_id, "--json"])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out) if captured.out else None, captured


def test_replay_then_ack_offers_next_task_and_keeps_show_readable(run_site, capsys):
    run_dir, workspace = run_site
    first = _observe(run_dir, workspace, "a")
    second = _observe(run_dir, workspace, "b")
    before = (run_dir / "events" / "lifecycle.jsonl").read_bytes()

    rc1, one, _ = _next(run_dir, capsys)
    rc2, two, _ = _next(run_dir, capsys)
    assert rc1 == rc2 == 0
    assert one["task"]["task_id"] == two["task"]["task_id"] == "a"
    assert one["task"]["result_id"] == two["task"]["result_id"] == first.result_id
    assert one["remaining"] == 2
    assert (run_dir / "events" / "lifecycle.jsonl").read_bytes() == before

    assert _ack(run_dir, "a", first.result_id, capsys)[0] == 0
    rc, next_payload, _ = _next(run_dir, capsys)
    assert rc == 0 and next_payload["task"]["task_id"] == "b"
    assert next_payload["task"]["result_id"] == second.result_id
    assert cli.main(["runs", "tasks", "show", str(run_dir), "a", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["task"]["result_id"] == first.result_id


def test_ack_while_owner_holds_lock_is_queued_and_next_skips_it(held_run, capsys):
    run_dir, workspace = held_run
    result = _observe_under_owner(run_dir, workspace, "a")
    _observe_under_owner(run_dir, workspace, "b")
    rc, payload, _ = _ack(run_dir, "a", result.result_id, capsys)
    assert rc == 0 and payload["outcome"] == "queued"
    assert facts(run_dir, "task.acknowledged") == []
    assert _next(run_dir, capsys)[1]["task"]["task_id"] == "b"
    assert inbox.drain(run_dir, workspace)["a"] == "acknowledged"
    assert len(facts(run_dir, "task.acknowledged")) == 1


def _observe_under_owner(run_dir, workspace, task_id):
    handle = run_tasks.prepare_attempt(run_dir, workspace=workspace, task_id=task_id)
    return run_tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=run_tasks.TaskObservation(status="completed", containment="quiescent"),
    )


def test_stale_and_duplicate_acknowledgements_report_closed_outcomes(run_site, capsys):
    run_dir, workspace = run_site
    result = _observe(run_dir, workspace, "a")
    assert _ack(run_dir, "a", "0" * 64, capsys)[1]["outcome"] == "result-mismatch"
    assert _ack(run_dir, "a", result.result_id, capsys)[1]["outcome"] == "acknowledged"
    before = (run_dir / "events" / "lifecycle.jsonl").read_bytes()
    assert _ack(run_dir, "a", result.result_id, capsys)[1]["outcome"] == "already-acknowledged"
    assert (run_dir / "events" / "lifecycle.jsonl").read_bytes() == before


def test_ack_drain_failure_is_a_refusal_not_queued(run_site, capsys, monkeypatch):
    run_dir, workspace = run_site
    result = _observe(run_dir, workspace, "a")

    def fail_drain(_run_dir, _workspace):
        raise inbox.TaskInboxError("journal-unreadable", "injected drain failure")

    monkeypatch.setattr(inbox, "drain", fail_drain)
    rc, payload, captured = _ack(run_dir, "a", result.result_id, capsys)
    assert rc == 2 and payload is None
    assert "journal-unreadable" in captured.err
    assert facts(run_dir, "task.acknowledged") == []


def test_restart_reads_same_journal_and_pending_inbox(run_site, capsys):
    run_dir, workspace = run_site
    result = _observe(run_dir, workspace, "a")
    _observe(run_dir, workspace, "b")
    assert inbox.submit(run_dir, "ack", "a", result_id=result.result_id) == "created"
    process = subprocess.run(
        [sys.executable, "-m", "brigade", "runs", "tasks", "next", str(run_dir), "--json"],
        cwd=workspace,
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["task"]["task_id"] == "b"
    assert state_of(run_dir, "a").acknowledged is False


def test_ack_request_filename_is_bound_to_its_result_id(run_site):
    run_dir, _workspace = run_site
    result_id = "c" * 64
    assert inbox.submit(run_dir, "ack", "a", result_id=result_id) == "created"
    request_dir = run_dir / "task-inbox"
    original = request_dir / inbox.request_name("ack", "a", result_id)
    original.rename(request_dir / inbox.request_name("ack", "a", "d" * 64))
    pending = inbox.pending(run_dir)
    assert pending.requests == ()
    assert len(pending.malformed) == 1


def test_timeout_does_not_change_journal(run_site, capsys):
    run_dir, _workspace = run_site
    before = (run_dir / "events" / "lifecycle.jsonl").read_bytes()
    rc, payload, _ = _next(run_dir, capsys, "--timeout", "0.01")
    assert rc == 3 and payload["timed_out"] is True
    assert (run_dir / "events" / "lifecycle.jsonl").read_bytes() == before


def test_waiting_unknown_are_not_offered_and_all_acknowledged_is_done(run_site, capsys):
    run_dir, workspace = run_site
    waiting = _observe(run_dir, workspace, "a", "waiting-for-children")
    unknown = _observe(run_dir, workspace, "b", "unknown")
    assert waiting.result_id is None and unknown.result_id is None
    rc, _payload, _ = _next(run_dir, capsys, "--timeout", "0.01")
    assert rc == 3

    result_a = state_of(run_dir, "a")
    assert result_a.status == "waiting-for-children"


def test_done_true_after_all_results_are_acknowledged(run_site, capsys):
    run_dir, workspace = run_site
    for task_id in ("a", "b"):
        result = _observe(run_dir, workspace, task_id)
        assert _ack(run_dir, task_id, result.result_id, capsys)[0] == 0
    rc, payload, _ = _next(run_dir, capsys)
    assert rc == 0 and payload["task"] is None and payload["done"] is True
    assert payload["remaining"] == 0
