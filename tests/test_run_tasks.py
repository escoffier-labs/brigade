"""Public task API identity, observation and consumption contracts."""

from __future__ import annotations

import importlib
import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from brigade import aboyeur, runguard
from brigade import run_tasks as tasks


@pytest.fixture(params=[False, True], ids=["legacy", "authoritative"])
def task_run(tmp_path, monkeypatch, request):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "test-task-run"
    run_dir.mkdir(parents=True)
    payload = {
        "schema": "brigade.run.v1",
        "status": "running",
        "lock_workspace": str(workspace),
        "lifecycle_journal_requested": True,
    }
    if request.param:
        payload["run_journal_authority_requested"] = True
        monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    payload["approval_reference"] = {
        k: "test-reference"
        for k in (
            "approval_id",
            "source",
            "fingerprint",
            "source_fingerprint",
            "contract_fingerprint",
            "evidence_fingerprint",
        )
    }
    with runguard.run_lock(workspace, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", payload)
        yield run_dir, workspace


def accepted(task_run, task_id="one", request_id="request-one"):
    run_dir, workspace = task_run
    return tasks.accept_task(
        run_dir, workspace=workspace, request_id=request_id, spec=tasks.TaskSpec(task_id, "worker", "a" * 64)
    )


def prepared(task_run):
    accepted(task_run)
    run_dir, workspace = task_run
    return tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")


def test_acceptance_and_attempt_replay_survive_module_reload(task_run):
    run_dir, workspace = task_run
    first = accepted(task_run)
    assert first.attempt_id is None
    attempt = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    importlib.reload(tasks)
    assert asdict(accepted(task_run)) == asdict(attempt)
    assert asdict(tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")) == asdict(attempt)
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        first.request_id = "changed"


def test_acceptance_conflicts_and_same_seat_distinct_tasks(task_run):
    run_dir, workspace = task_run
    accepted(task_run)
    with pytest.raises(tasks.TaskJournalError):
        tasks.accept_task(
            run_dir, workspace=workspace, request_id="request-one", spec=tasks.TaskSpec("two", "worker", "b" * 64)
        )
    with pytest.raises(tasks.TaskJournalError):
        accepted(task_run, request_id="different")
    accepted(task_run, task_id="two", request_id="request-two")
    assert [r.handle.spec.task_id for r in tasks.read_tasks(run_dir)] == ["one", "two"]


def test_immutable_provider_identity_and_terminal_consumption(task_run):
    run_dir, workspace = task_run
    handle = prepared(task_run)
    running = tasks.TaskObservation("running", provider_task_id="provider-one", original_run_id="original-one")
    tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=running)
    with pytest.raises(tasks.TaskJournalError):
        tasks.observe_task(
            run_dir, workspace=workspace, handle=handle, observation=replace(running, original_run_id="backing-two")
        )
    completed = tasks.TaskObservation(
        "completed", provider_result_id="result-one", artifact_kind="commit", artifact_digest="b" * 64
    )
    result = tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=completed)
    assert result.result_id and not result.acknowledged
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    assert tasks.read_tasks(run_dir)[0] == result
    assert tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=completed) == result
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    ack = tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id=result.result_id)
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    assert tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id=result.result_id) == ack
    assert tasks.read_tasks(run_dir)[0].acknowledged
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    with pytest.raises(tasks.TaskJournalError):
        tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation("failed"))
    with pytest.raises(tasks.TaskJournalError):
        tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id="c" * 64)


def test_wait_states_timeout_and_active_children(task_run):
    run_dir, workspace = task_run
    handle = prepared(task_run)
    for state in ["running", "waiting-for-input", "unknown", "queued"]:
        result = tasks.observe_task(
            run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation(state)
        )
        assert result.status == state
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    assert (
        tasks.observe_task(
            run_dir,
            workspace=workspace,
            handle=handle,
            observation=tasks.TaskObservation("cancelled", wait_timed_out=True),
        )
        == result
    )
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    waiting = tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation("completed", containment="active"),
    )
    assert waiting.status == "waiting-for-children" and waiting.result_id is None
    terminal = tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation("completed", containment="quiescent"),
    )
    later = tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation("completed", containment="active"),
    )
    assert later.result_id == terminal.result_id and later.status == "completed"
    assert later.containment == "active"


@pytest.mark.parametrize("state", ["failed", "interrupted", "cancelled"])
def test_terminal_states_remain_distinct(task_run, state):
    run_dir, workspace = task_run
    handle = prepared(task_run)
    result = tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation(state))
    assert result.status == state and result.result_id
    assert tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one") == handle


def test_foreign_or_unprepared_handle_refused(task_run):
    run_dir, workspace = task_run
    handle = accepted(task_run)
    with pytest.raises(tasks.TaskJournalError):
        tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation("running"))
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    with pytest.raises(tasks.TaskJournalError):
        tasks.observe_task(
            run_dir,
            workspace=workspace,
            handle=replace(handle, run_id="other"),
            observation=tasks.TaskObservation("running"),
        )


def test_journal_contains_only_references(task_run):
    run_dir, _ = task_run
    prepared(task_run)
    payloads = [json.loads(line)["payload"] for line in (run_dir / "events/lifecycle.jsonl").read_text().splitlines()]
    task_payloads = [p for p in payloads if "task_id" in p]
    assert task_payloads and all("prompt" not in p and "provider_response" not in p for p in task_payloads)
    with pytest.raises(tasks.TaskJournalError):
        tasks.TaskSpec("raw task text with spaces", "worker", "a" * 64)


def test_same_running_observation_can_resume_after_input(task_run):
    """A repeated state after a real transition is a new fact, not old poll replay."""
    run_dir, workspace = task_run
    handle = prepared(task_run)
    running = tasks.TaskObservation("running", provider_task_id="provider-one")
    tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=running)
    tasks.observe_task(
        run_dir, workspace=workspace, handle=handle, observation=tasks.TaskObservation("waiting-for-input")
    )
    resumed = tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=running)
    assert resumed.status == "running"
    assert tasks.read_tasks(run_dir)[0].status == "running"
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    assert tasks.observe_task(run_dir, workspace=workspace, handle=handle, observation=running) == resumed
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before


def test_replay_and_conflict_after_process_restart(tmp_path):
    """A new owner process reconstructs identity solely from durable run facts."""
    import subprocess
    import sys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade/runs/restart-task-run"
    run_dir.mkdir(parents=True)
    spec = tasks.TaskSpec("one", "worker", "a" * 64)
    with runguard.run_lock(workspace, run_dir=run_dir):
        aboyeur._write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "status": "started",
                "lock_workspace": str(workspace),
                "lifecycle_journal_requested": True,
            },
        )
        tasks.accept_task(run_dir, workspace=workspace, request_id="request-one", spec=spec)
        handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    # The prior process no longer owns this run. Even an identical replay must refuse.
    with pytest.raises(tasks.TaskJournalError, match="active run owner"):
        tasks.accept_task(run_dir, workspace=workspace, request_id="request-one", spec=spec)
    code = """
import json, sys
from dataclasses import asdict
from pathlib import Path
from brigade import runguard, run_tasks as tasks
workspace, run_dir = map(Path, sys.argv[1:])
with runguard.run_lock(workspace, run_dir=run_dir):
    handle = tasks.accept_task(run_dir, workspace=workspace, request_id='request-one',
                              spec=tasks.TaskSpec('one', 'worker', 'a' * 64))
    assert tasks.prepare_attempt(run_dir, workspace=workspace, task_id='one') == handle
    for request_id, spec in [
        ('request-one', tasks.TaskSpec('one', 'worker', 'b' * 64)),
        ('request-two', tasks.TaskSpec('one', 'worker', 'a' * 64)),
    ]:
        try:
            tasks.accept_task(run_dir, workspace=workspace, request_id=request_id, spec=spec)
        except tasks.TaskJournalError:
            pass
        else:
            raise AssertionError('conflicting identity accepted after restart')
    print(json.dumps(asdict(handle)))
"""
    replay = subprocess.run(
        [sys.executable, "-c", code, str(workspace), str(run_dir)], capture_output=True, text=True, timeout=20
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout) == asdict(handle)
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    assert tasks.read_tasks(run_dir)[0].handle == handle
