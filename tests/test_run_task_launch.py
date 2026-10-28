"""Launch permission consumption, without execution or budget admission."""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from brigade import run_control_journal, run_events, run_journal, run_lifecycle, run_projector
from brigade import run_tasks as tasks
from tests import test_run_tasks as support

task_run = support.task_run


def test_concurrent_first_claim_grants_once(task_run):
    run_dir, workspace = task_run
    handle = support.prepared(task_run)
    version = tasks.read_tasks(run_dir)[0].version
    barrier = threading.Barrier(2)
    outcomes, errors = [], []

    def claim():
        try:
            barrier.wait(timeout=5)
            outcomes.append(
                tasks.claim_task_launch(
                    run_dir,
                    workspace=workspace,
                    handle=handle,
                    expected_version=version,
                    seat="worker",
                    dispatch_attempt=1,
                ).outcome
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert outcomes == ["granted"], repr(errors)
    assert len(errors) == 1 and isinstance(errors[0], tasks.TaskJournalError)
    assert "stale" in str(errors[0])
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    current = tasks.read_tasks(run_dir)[0]
    replay = tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=handle,
        expected_version=current.version,
        seat="worker",
        dispatch_attempt=1,
    )
    assert replay.outcome == "already-claimed" and replay.task == current
    assert current.status == "unknown" and current.handle == handle
    assert all(
        getattr(current, field) is None
        for field in (
            "provider_task_id",
            "original_run_id",
            "provider_result_id",
            "result_id",
            "artifact_kind",
            "artifact_digest",
        )
    )
    with pytest.raises(FrozenInstanceError):
        replay.outcome = "granted"
    assert current.launch_claim == tasks.TaskLaunchBinding("worker", 1)
    with pytest.raises(FrozenInstanceError):
        current.launch_claim.dispatch_attempt = 2
    assert path.read_bytes() == before
    events = run_journal.read_journal(path).events
    claims = [e for e in events if e.event_type == "task.launch-claimed"]
    assert len(claims) == 1 and claims[0].event_digest == current.version
    checkpoint = events[claims[0].sequence - 2]
    assert checkpoint.payload["paired_event_type"] == "task.launch-claimed"
    assert checkpoint.payload["pairing_key"] == claims[0].request_digest
    support.accepted(task_run, "two", "request-two")
    second = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="two")
    assert (
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=second,
            expected_version=tasks.read_tasks(run_dir)[1].version,
            seat="worker",
            dispatch_attempt=2,
        ).outcome
        == "granted"
    )


@pytest.mark.parametrize("authoritative", [False, True])
def test_claim_replay_after_owner_restart_never_grants(tmp_path, authoritative):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade/runs/restart-run"
    run_dir.mkdir(parents=True)
    code = r"""
import json, os, sys
from pathlib import Path
from brigade import aboyeur, runguard, run_tasks as tasks
workspace, run_dir = map(Path, sys.argv[1:3])
mode, authoritative = sys.argv[3:]
if authoritative == 'True':
    os.environ['BRIGADE_RUN_JOURNAL_AUTHORITY'] = '1'
with runguard.run_lock(workspace, run_dir=run_dir):
    if mode == 'first':
        payload = {'schema': 'brigade.run.v1', 'status': 'started',
                   'lock_workspace': str(workspace), 'lifecycle_journal_requested': True}
        if authoritative == 'True':
            payload['run_journal_authority_requested'] = True
        aboyeur._write_json(run_dir / 'run.json', payload)
        tasks.accept_task(run_dir, workspace=workspace, request_id='req',
                          spec=tasks.TaskSpec('one', 'worker', 'a' * 64))
        handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id='one')
        old = tasks.read_tasks(run_dir)[0].version
        decision = tasks.claim_task_launch(run_dir, workspace=workspace, handle=handle,
                          expected_version=old, seat='worker', dispatch_attempt=1)
        assert decision.outcome == 'granted'
        print(old)
    else:
        current = tasks.read_tasks(run_dir)[0]
        old = sys.stdin.read().strip()
        try:
            tasks.claim_task_launch(run_dir, workspace=workspace, handle=current.handle,
                          expected_version=old, seat='worker', dispatch_attempt=1)
        except tasks.TaskJournalError as exc:
            assert 'stale' in str(exc)
        else:
            raise AssertionError('lost response reconstructed a grant')
        decision = tasks.claim_task_launch(run_dir, workspace=workspace, handle=current.handle,
                          expected_version=current.version, seat='worker', dispatch_attempt=1)
        assert decision.outcome == 'already-claimed'
"""
    first = subprocess.run(
        [sys.executable, "-c", code, str(workspace), str(run_dir), "first", str(authoritative)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert first.returncode == 0, first.stderr
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    second = subprocess.run(
        [sys.executable, "-c", code, str(workspace), str(run_dir), "second", str(authoritative)],
        input=first.stdout,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert second.returncode == 0, second.stderr
    assert path.read_bytes() == before
    assert sum(e.event_type == "task.launch-claimed" for e in run_journal.read_journal(path).events) == 1
    current = tasks.read_tasks(run_dir)[0]
    with pytest.raises(tasks.TaskJournalError, match="active run owner"):
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=current.handle,
            expected_version=current.version,
            seat="worker",
            dispatch_attempt=1,
        )
    assert path.read_bytes() == before


def test_claim_rejects_foreign_owner_and_stale_task_version(task_run, tmp_path):
    run_dir, workspace = task_run
    handle = support.prepared(task_run)
    current = tasks.read_tasks(run_dir)[0]
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    other_workspace = tmp_path / "foreign"
    other_workspace.mkdir()
    with pytest.raises(tasks.TaskJournalError, match="active run owner"):
        tasks.claim_task_launch(
            run_dir,
            workspace=other_workspace,
            handle=handle,
            expected_version=current.version,
            seat="worker",
            dispatch_attempt=1,
        )
    wrong_run = run_dir.parent / "other-run"
    (wrong_run / "events").mkdir(parents=True)
    with pytest.raises(tasks.TaskJournalError, match="active run owner"):
        tasks.claim_task_launch(
            wrong_run,
            workspace=workspace,
            handle=handle,
            expected_version=current.version,
            seat="worker",
            dispatch_attempt=1,
        )
    # A real other PID cannot mutate while this process owns the run.
    foreign = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; from brigade import run_tasks as t; "
            "r,w=map(Path,sys.argv[1:]); c=t.read_tasks(r)[0]; "
            "t.claim_task_launch(r,workspace=w,handle=c.handle,expected_version=c.version,seat='worker',dispatch_attempt=1)",
            str(run_dir),
            str(workspace),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert foreign.returncode != 0 and "active run owner" in foreign.stderr
    assert path.read_bytes() == before
    support.accepted(task_run, "two", "request-two")
    other = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="two")
    tasks.observe_task(run_dir, workspace=workspace, handle=other, observation=tasks.TaskObservation("running"))
    run_control_journal.execute_control_request(
        run_dir,
        op="interrupt",
        request_id="control-one",
        send=lambda payload: {"ok": True},
    )
    assert tasks.read_tasks(run_dir)[0].version == current.version
    for invalid in [None, True, "bad", "f" * 64]:
        before = path.read_bytes()
        with pytest.raises(tasks.TaskJournalError, match="stale"):
            tasks.claim_task_launch(
                run_dir,
                workspace=workspace,
                handle=handle,
                expected_version=invalid,
                seat="worker",
                dispatch_attempt=1,
            )
        assert path.read_bytes() == before
    assert (
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=current.version,
            seat="worker",
            dispatch_attempt=1,
        ).outcome
        == "granted"
    )
    fresh = tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation("queued"))
    before = path.read_bytes()
    with pytest.raises(tasks.TaskJournalError, match="stale"):
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=current.version,
            seat="worker",
            dispatch_attempt=1,
        )
    assert fresh.version != current.version and path.read_bytes() == before


def test_claim_binding_cannot_be_reassigned(task_run):
    run_dir, workspace = task_run
    handle = support.prepared(task_run)
    current = tasks.read_tasks(run_dir)[0]
    path = run_dir / "events/lifecycle.jsonl"
    for wrong in [
        replace(handle, run_id="other"),
        replace(handle, request_id="other"),
        replace(handle, attempt_id="f" * 64),
        replace(handle, spec=tasks.TaskSpec("one", "worker", "b" * 64)),
    ]:
        before = path.read_bytes()
        with pytest.raises(tasks.TaskJournalError):
            tasks.claim_task_launch(
                run_dir,
                workspace=workspace,
                handle=wrong,
                expected_version=current.version,
                seat="worker",
                dispatch_attempt=1,
            )
        assert path.read_bytes() == before
    for seat, ordinal in [
        ("other", 1),
        ("bad seat", 1),
        ("worker", True),
        ("worker", None),
        ("worker", 0),
        ("worker", -1),
        ("worker", 1.0),
        ("worker", "1"),
    ]:
        before = path.read_bytes()
        with pytest.raises(tasks.TaskJournalError):
            tasks.claim_task_launch(
                run_dir,
                workspace=workspace,
                handle=handle,
                expected_version=current.version,
                seat=seat,
                dispatch_attempt=ordinal,
            )
        assert path.read_bytes() == before
    tasks.claim_task_launch(
        run_dir, workspace=workspace, handle=handle, expected_version=current.version, seat="worker", dispatch_attempt=1
    )
    # A later dispatch is allowed and must not invalidate replay of the earlier claim.
    run_lifecycle.record_dispatch_fact(
        run_dir,
        workspace=workspace,
        event_type="run.dispatch.requested",
        seat="worker",
        attempt=1,
    )
    claimed = tasks.read_tasks(run_dir)[0]
    assert (
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=claimed.version,
            seat="worker",
            dispatch_attempt=1,
        ).outcome
        == "already-claimed"
    )
    support.accepted(task_run, "two", "request-two")
    other = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="two")
    for target, version, ordinal in [(other, tasks.read_tasks(run_dir)[1].version, 1), (handle, claimed.version, 2)]:
        before = path.read_bytes()
        with pytest.raises(tasks.TaskJournalError):
            tasks.claim_task_launch(
                run_dir,
                workspace=workspace,
                handle=target,
                expected_version=version,
                seat="worker",
                dispatch_attempt=ordinal,
            )
        assert path.read_bytes() == before
    run_lifecycle.record_dispatch_fact(
        run_dir,
        workspace=workspace,
        event_type="run.dispatch.requested",
        seat="worker",
        attempt=3,
    )
    before = path.read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=other,
            expected_version=tasks.read_tasks(run_dir)[1].version,
            seat="worker",
            dispatch_attempt=3,
        )
    assert path.read_bytes() == before
    terminal = tasks.observe_task(
        run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation("failed")
    )
    before = path.read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=terminal.version,
            seat="worker",
            dispatch_attempt=2,
        )
    assert (
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=terminal.version,
            seat="worker",
            dispatch_attempt=1,
        ).outcome
        == "terminal"
    )
    assert path.read_bytes() == before


def test_claim_requires_pristine_prepared_history(task_run):
    run_dir, workspace = task_run
    handle = support.accepted(task_run)
    path = run_dir / "events/lifecycle.jsonl"
    before = path.read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=handle,
            expected_version=tasks.read_tasks(run_dir)[0].version,
            seat="worker",
            dispatch_attempt=1,
        )
    assert path.read_bytes() == before
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    pristine = tasks.read_tasks(run_dir)[0]
    tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation("queued"))
    assert tasks.read_tasks(run_dir)[0] == pristine  # no-op emits no fact
    granted = tasks.claim_task_launch(
        run_dir,
        workspace=workspace,
        handle=handle,
        expected_version=pristine.version,
        seat="worker",
        dispatch_attempt=1,
    )
    assert granted.outcome == "granted"
    for state in ["running", "queued", "completed"]:
        result = tasks.observe_task(
            run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation(state)
        )
        assert result.launch_claim == granted.task.launch_claim
    ack = tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id=result.result_id)
    assert ack.launch_claim == granted.task.launch_claim
    assert tasks.read_tasks(run_dir)[0] == ack
    events = run_journal.read_journal(path).events
    assert all("launch_claim" not in e.payload for e in events if e.event_type == "task.observed")
    support.accepted(task_run, "two", "request-two")
    other = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="two")
    for state in ["running", "queued"]:
        tasks.observe_task(run_dir, workspace=workspace, handle=other, observation=tasks.TaskObservation(state))
    before = path.read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=other,
            expected_version=tasks.read_tasks(run_dir)[1].version,
            seat="worker",
            dispatch_attempt=2,
        )
    assert path.read_bytes() == before
    terminal = tasks.observe_task(
        run_dir, workspace=workspace, handle=other, observation=tasks.TaskObservation("cancelled")
    )
    before = path.read_bytes()
    assert (
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=other,
            expected_version=terminal.version,
            seat="worker",
            dispatch_attempt=2,
        ).outcome
        == "terminal"
    )
    assert path.read_bytes() == before and tasks.read_tasks(run_dir)[1].launch_claim is None
    with pytest.raises(tasks.TaskJournalError, match="binding"):
        tasks.claim_task_launch(
            run_dir,
            workspace=workspace,
            handle=other,
            expected_version=terminal.version,
            seat="worker",
            dispatch_attempt=1,
        )
    assert path.read_bytes() == before

    # Canonical rebuilding proves chain integrity independently of task folding.
    real = [e for e in events if e.event_type in {"task.accepted", "task.attempt-prepared", "task.launch-claimed"}]
    observations = [e for e in events if e.event_type == "task.observed"]
    claim_payload = real[-1].payload
    latest = run_journal.read_journal(path).events
    other_prepared = [
        e
        for e in latest
        if e.event_type in {"task.accepted", "task.attempt-prepared"} and e.payload["task_id"] == "two"
    ]
    other_payload = {**claim_payload, "task_id": "two", "attempt_id": other.attempt_id}
    prior_dispatch = real[0]
    # This event is canonicalized again below, so its old envelope is irrelevant.
    prior_dispatch = replace(
        prior_dispatch, event_type="run.dispatch.requested", payload={"seat": "worker", "attempt": 1}
    )
    for prefix, payload in [
        (real, claim_payload),  # duplicate, even with identical identity
        (real, {**claim_payload, "dispatch_attempt": 2}),
        ([], claim_payload),  # before acceptance
        (real[:1], claim_payload),  # before preparation
        (real[:2], {**claim_payload, "seat": "other"}),
        (real[:2], {**claim_payload, "attempt_id": "f" * 64}),
        ([*real[:2], *observations[:1]], claim_payload),
        ([*real[:2], *observations], claim_payload),
        ([prior_dispatch, *real[:2]], claim_payload),
        ([*real, *other_prepared], other_payload),  # run-wide injectivity
    ]:
        rebuilt = []
        facts = [(e.event_type, e.payload) for e in prefix] + [("task.launch-claimed", payload)]
        for sequence, (event_type, fact_payload) in enumerate(facts, 1):
            rebuilt.append(
                run_journal.RunEvent(
                    **run_events.build_event(
                        run_id=run_dir.name,
                        sequence=sequence,
                        event_type=event_type,
                        payload=fact_payload,
                        idempotency_key=f"forged-{sequence}",
                        recorded_at="2026-07-27T15:30:45.000000Z",
                        previous_digest=rebuilt[-1].event_digest if rebuilt else None,
                    )
                )
            )
        projection = run_projector.project_run_snapshot(
            {"schema": "brigade.run.v1", "status": "running"},
            rebuilt,
            journal_present=True,
        )
        assert projection.last_sequence == len(rebuilt)
        with pytest.raises(tasks.TaskJournalError):
            tasks._fold(run_dir.name, rebuilt)
