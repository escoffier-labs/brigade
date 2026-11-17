"""Per-task launch claims, the journal-loss fence and exact-task live controls."""

from __future__ import annotations

import json
import threading
from unittest.mock import Mock

import pytest

from brigade import agents, run_batch_launch, run_control, run_journal, run_lifecycle, run_tasks, run_transport
from brigade.roster import Agent, Roster
from brigade.run_batch_execute import BatchExecution
from brigade.run_transport import Assignment, LaunchRefused
from tests import test_run_tasks as support
from tests.test_run_batch_execute import _batch_run_dir, invoke, setup, task

task_run = support.task_run
__all__ = ["setup"]


def _events(run_dir):
    return run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events


def _recorder(task_run, *, version=None, record_requested=None, refused=None):
    run_dir, workspace = task_run
    handle = support.prepared(task_run)
    assignment = Assignment(worker="worker", task="t", task_id="one", attempt_id=handle.attempt_id)
    seen = refused if refused is not None else []

    def write_requested(attempt):
        run_lifecycle.record_dispatch_fact(
            run_dir, workspace=workspace, event_type="run.dispatch.requested", seat="worker", attempt=attempt
        )
        return attempt

    requested = record_requested or write_requested
    record = run_batch_launch.launch_recorder(
        run_dir=run_dir,
        workspace=workspace,
        assignment=assignment,
        seat="worker",
        prepared=(handle, version or run_tasks.read_tasks(run_dir)[0].version),
        record_requested=requested,
        on_refused=seen.append,
    )
    return record, seen


def test_recorder_claims_before_the_requested_fact(task_run):
    run_dir, _ = task_run
    record, refused = _recorder(task_run)
    assert record(1) == 1
    types = [e.event_type for e in _events(run_dir)]
    assert types.index("task.launch-claimed") < types.index("run.dispatch.requested")
    assert run_tasks.read_tasks(run_dir)[0].launch_claim.dispatch_attempt == 1
    assert refused == []


def test_recorder_refuses_an_already_claimed_task(task_run):
    run_dir, _ = task_run
    record, refused = _recorder(task_run)
    record(1)
    # A retry with the fresh version sees the durable claim and never relaunches.
    again, _ = _recorder(task_run, version=run_tasks.read_tasks(run_dir)[0].version, refused=refused)
    with pytest.raises(LaunchRefused, match="already-claimed"):
        again(1)
    assert refused == ["one"]


def test_recorder_refuses_without_requested_fact_on_journal_error(task_run):
    requested = Mock()
    record, refused = _recorder(task_run, version="0" * 64, record_requested=requested)
    with pytest.raises(LaunchRefused, match="journal rejected the claim"):
        record(1)
    requested.assert_not_called()
    assert refused == ["one"]


def _roster(*seats):
    agents_ = {"chef": Agent(name="chef", cli="codex", role="plan")}
    agents_.update({seat: Agent(name=seat, cli="codex", role="implement") for seat in seats})
    return Roster(orchestrator="chef", agents=agents_, max_workers=2)


def _dispatch(assignments, roster, tmp_path, **kwargs):
    return run_transport.dispatch(
        assignments,
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=kwargs.pop("run_appserver_worker", lambda *a, **kw: agents.AgentResult("", False)),
        event_writer=lambda events_dir, worker, verbose=False: None,
        cwd=tmp_path,
        read_only=True,
        **kwargs,
    )


def test_refusal_never_reaches_route_lease_or_provider(monkeypatch, tmp_path):
    provider, lease, remote = Mock(), Mock(), Mock()
    monkeypatch.setattr(agents, "run_agent", provider)

    def refuse(agent, assignment):
        raise LaunchRefused("task t1 launch refused: already-claimed")

    [result] = _dispatch(
        [Assignment(worker="coder", task="t", task_id="t1", attempt_id="a1")],
        _roster("coder"),
        tmp_path,
        on_dispatch_requested=refuse,
        model_lease=lease,
        remote_transport=remote,
    )
    for untouched in (provider, lease, remote):
        untouched.assert_not_called()
    assert (result.ok, result.status, result.failure_kind, result.task_id) == (False, "refused", "launch-refused", "t1")


def test_dispatch_requested_arguments_depend_on_batch_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "run_agent", lambda *a, **kw: agents.AgentResult("ok", True))
    calls = []
    _dispatch(
        [Assignment(worker="coder", task="plain"), Assignment(worker="coder", task="batch", task_id="t1")],
        _roster("coder"),
        tmp_path,
        on_dispatch_requested=lambda *args: calls.append((args[0].name, len(args))) or len(calls),
    )
    assert sorted(calls) == [("coder", 1), ("coder", 2)]


def test_interrupt_by_task_id_hits_only_that_task(tmp_path):
    registry = run_control.LiveTurnRegistry()
    interrupted, started, release = [], threading.Barrier(3), threading.Event()

    class Thread:
        thread_id = "thread"

        def __init__(self, name):
            self.name = name

        def interrupt(self, turn_id):
            interrupted.append(self.name)

        def steer(self, text, turn_id):
            pass

    def runner(appserver, agent, worker, prompt, *, registry, task_id=None, attempt_id=None, **kw):
        registry.register(worker, Thread(task_id), f"turn-{task_id}", task_id=task_id, attempt_id=attempt_id)
        started.wait(timeout=10)
        release.wait(timeout=10)
        return agents.AgentResult("ok", True)

    assignments = [Assignment(worker="coder", task=f"p{i}", task_id=i, attempt_id=f"{i}-attempt") for i in "ab"]
    worker = threading.Thread(
        target=lambda: _dispatch(
            assignments,
            _roster("coder"),
            tmp_path,
            appserver=object(),
            control_registry=registry,
            run_appserver_worker=runner,
        )
    )
    worker.start()
    started.wait(timeout=10)
    try:
        registry.interrupt(task_id="a")
        with pytest.raises(run_control.ControlError) as ambiguous:
            registry.interrupt("coder")
        assert ambiguous.value.code == "ambiguous-target"
    finally:
        release.set()
        worker.join(timeout=10)
    assert interrupted == ["a"]


def _lifecycle(setup, result):
    return _events(setup[0] / ".brigade" / "runs" / result["run_id"])


def test_same_seat_tasks_get_distinct_claims_and_attempts(setup, capsys):
    rc, result, err = invoke(setup, capsys, [task("a"), task("b")])
    assert rc == 0, err
    events = _lifecycle(setup, result)
    claims = [e.payload for e in events if e.event_type == "task.launch-claimed"]
    requested = [e.payload for e in events if e.event_type == "run.dispatch.requested"]
    assert sorted(c["task_id"] for c in claims) == ["a", "b"]
    assert len({c["attempt_id"] for c in claims}) == 2
    assert sorted(c["dispatch_attempt"] for c in claims) == [1, 2] == sorted(r["attempt"] for r in requested)

    def position(kind, key, attempt):
        return next(i for i, e in enumerate(events) if e.event_type == kind and e.payload.get(key) == attempt)

    for claim in claims:
        attempt = claim["dispatch_attempt"]
        assert position("task.launch-claimed", "dispatch_attempt", attempt) < position(
            "run.dispatch.requested", "attempt", attempt
        )


def test_crash_after_claim_before_requested_never_relaunches(setup, capsys, monkeypatch):
    original = run_lifecycle.record_dispatch_fact

    def crash(*args, **kwargs):
        if kwargs["event_type"] == "run.dispatch.requested":
            raise KeyboardInterrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(run_lifecycle, "record_dispatch_fact", crash)
    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc == 1, err
    assert setup[1] == []
    monkeypatch.setattr(run_lifecycle, "record_dispatch_fact", original)
    assert invoke(setup, capsys, [task("a")])[0] == 1
    assert setup[1] == []
    types = [e.event_type for e in _lifecycle(setup, result)]
    assert types.count("task.launch-claimed") == 1
    assert "run.dispatch.requested" not in types


def test_claim_journal_error_refuses_with_a_receipt_and_no_provider_call(setup, capsys, monkeypatch):
    def broken(*args, **kwargs):
        raise run_tasks.TaskJournalError("journal unavailable")

    monkeypatch.setattr(run_tasks, "claim_task_launch", broken)
    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc != 0, err
    assert setup[1] == []
    assert result["tasks"]["a"]["status"] == "queued" and result["tasks"]["a"]["result_ref"] is None
    receipt = json.loads((_batch_run_dir(setup[0]) / "worker-results.json").read_text())
    assert "launch refused" in json.dumps(receipt) and "launch-refused" in json.dumps(receipt)


@pytest.mark.parametrize("stage", ["after-launch", "before-launch", "unreadable"])
def test_lost_journal_refuses_the_request(setup, capsys, monkeypatch, stage):
    dispatch = BatchExecution.dispatch
    if stage == "before-launch":
        # Accepted into the journal, then stopped before any worker started.
        monkeypatch.setattr(BatchExecution, "dispatch", Mock(side_effect=KeyboardInterrupt))
    rc, result, err = invoke(setup, capsys, [task("a")])
    assert rc == (1 if stage == "before-launch" else 0), err
    journal = _batch_run_dir(setup[0]) / "events" / "lifecycle.jsonl"
    if stage == "unreadable":
        journal.write_text("{broken")
    else:
        journal.unlink()
    monkeypatch.setattr(BatchExecution, "dispatch", dispatch)
    launched = len(setup[1])
    rc, refused, _ = invoke(setup, capsys, [task("a")])
    assert (rc, refused["error"]["code"]) == (2, "request-unknown")
    assert len(setup[1]) == launched
    # A new request id is the supported way forward and still proceeds.
    assert invoke(setup, capsys, [task("a")], request="other")[0] == 0


@pytest.mark.parametrize("also_remove", ["batch-request.json", "batch-journal-enrolled.json"])
def test_lost_journal_with_another_file_removed_still_refuses(setup, capsys, also_remove):
    rc, _, err = invoke(setup, capsys, [task("a")])
    assert rc == 0, err
    run_dir = _batch_run_dir(setup[0])
    (run_dir / "events" / "lifecycle.jsonl").unlink()
    (run_dir / also_remove).unlink()
    launched = len(setup[1])
    rc, refused, _ = invoke(setup, capsys, [task("a")])
    assert (rc, refused["error"]["code"]) == (2, "request-unknown")
    assert len(setup[1]) == launched


def test_batch_run_cannot_be_resumed(setup, capsys):
    from brigade import run_resume

    rc, _, err = invoke(setup, capsys, [task("a")])
    assert rc == 0, err
    assert run_resume.resume(_batch_run_dir(setup[0])) == 2
    assert "cannot be resumed" in capsys.readouterr().err


def test_batch_run_with_marker_and_sentinel_removed_still_cannot_be_resumed(setup, capsys):
    from brigade import run_resume

    rc, _, err = invoke(setup, capsys, [task("a")])
    assert rc == 0, err
    run_dir = _batch_run_dir(setup[0])
    (run_dir / "batch-request.json").unlink()
    (run_dir / "batch-journal-enrolled.json").unlink()
    assert run_resume.resume(run_dir) == 2
    assert "cannot be resumed" in capsys.readouterr().err
