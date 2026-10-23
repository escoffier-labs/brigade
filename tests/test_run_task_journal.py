"""Task journal refusal, pairing and durability boundaries."""

from __future__ import annotations

import json

import pytest

from brigade import aboyeur, run_checkpoint, run_events, run_journal, run_lifecycle, run_shadow, runguard
from brigade import run_tasks as tasks
from tests import test_run_tasks as support

accepted = support.accepted
prepared = support.prepared
task_run = support.task_run


def test_owner_required_even_for_replay(task_run):
    run_dir, workspace = task_run
    accepted(task_run)
    with pytest.raises(tasks.TaskJournalError):
        tasks.accept_task(
            run_dir,
            workspace=workspace / "other",
            request_id="request-one",
            spec=tasks.TaskSpec("one", "worker", "a" * 64),
        )


@pytest.mark.parametrize("damage", ["partial", "chain", "unsupported"])
def test_damaged_journal_never_returns_or_appends_task(task_run, damage):
    run_dir, workspace = task_run
    prepared(task_run)
    journal = run_dir / "events/lifecycle.jsonl"
    raw = journal.read_bytes()
    if damage == "partial":
        journal.write_bytes(raw + b"{")
    else:
        lines = raw.splitlines()
        item = json.loads(lines[-1])
        item["event_digest" if damage == "chain" else "schema_version"] = "broken"
        lines[-1] = json.dumps(item).encode()
        journal.write_bytes(b"\n".join(lines) + b"\n")
    before = journal.read_bytes()
    with pytest.raises(tasks.TaskJournalError):
        tasks.read_tasks(run_dir)
    with pytest.raises(tasks.TaskJournalError):
        tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    assert journal.read_bytes() == before


def test_checkpoint_only_crash_refuses_without_creating_attempt(task_run, monkeypatch):
    run_dir, workspace = task_run
    accepted(task_run)
    real_append = run_lifecycle._append_owner_event

    def crash(path, **kwargs):
        if kwargs["event_type"] == "task.attempt-prepared":
            raise OSError("simulated append crash")
        return real_append(path, **kwargs)

    monkeypatch.setattr(run_lifecycle, "_append_owner_event", crash)
    with pytest.raises(tasks.TaskJournalError):
        tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    monkeypatch.setattr(run_lifecycle, "_append_owner_event", real_append)
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    with pytest.raises(tasks.TaskJournalError, match="incomplete"):
        tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    with pytest.raises(tasks.TaskJournalError):
        tasks.read_tasks(run_dir)
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before


def test_fsync_ambiguity_refuses_then_replays_same_attempt(task_run, monkeypatch):
    run_dir, workspace = task_run
    handle = prepared(task_run)
    real_sync = run_journal._sync_journal_fd

    def fail(*args, **kwargs):
        raise run_journal.IndeterminateOutcomeError("simulated durability failure")

    monkeypatch.setattr(run_journal, "_sync_journal_fd", fail)
    with pytest.raises(tasks.TaskJournalError):
        tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    monkeypatch.setattr(run_journal, "_sync_journal_fd", real_sync)
    assert tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one") == handle


@pytest.mark.parametrize(
    "event_type,payload",
    [
        (
            "task.accepted",
            {"task_id": "one", "seat": "worker", "request_id": "r", "payload_digest": "a" * 64, "spec_digest": "bad"},
        ),
        (
            "task.accepted",
            {
                "task_id": "one",
                "seat": "worker",
                "request_id": "r",
                "payload_digest": "a" * 64,
                "spec_digest": "b" * 64,
                "prompt": "private",
            },
        ),
        ("task.attempt-prepared", {"task_id": "one", "attempt_id": "a" * 64, "extra": "value"}),
        ("task.observed", {"task_id": "one", "attempt_id": "a" * 64, "status": "invented"}),
        ("task.acknowledged", {"task_id": "one", "result_id": 1}),
    ],
)
def test_task_event_schema_is_closed_and_typed(event_type, payload):
    with pytest.raises(run_events.CanonicalizationError):
        run_events.validate_event_request(event_type=event_type, payload=payload, idempotency_key="test")


def test_task_facts_preserve_aggregate_status_and_checkpoint_recovery(task_run):
    run_dir, workspace = task_run
    prepared(task_run)
    before = json.loads((run_dir / "run.json").read_text())["status"]
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, json.loads((run_dir / "run.json").read_text()))
    assert repaired["status"] == before == "running"
    events = run_journal.read_journal_bounded(run_dir / "events/lifecycle.jsonl").events
    checkpoints = [e for e in events if e.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE]
    assert checkpoints[-1].payload["pairing_key"] == events[-1].request_digest


def test_orphan_task_pair_remains_refused_after_unrelated_fact(task_run, monkeypatch):
    run_dir, workspace = task_run
    real_append = run_lifecycle._append_owner_event

    def crash(path, **kwargs):
        if kwargs["event_type"] == "task.accepted":
            raise OSError("simulated checkpoint-only crash")
        return real_append(path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(run_lifecycle, "_append_owner_event", crash)
        with pytest.raises(tasks.TaskJournalError):
            accepted(task_run)
    with pytest.raises(run_checkpoint.CheckpointError, match="task checkpoint pair is incomplete"):
        run_checkpoint.recover_from_checkpoint(run_dir, json.loads((run_dir / "run.json").read_bytes()))
    run_lifecycle.record_lifecycle_event(
        run_dir, workspace=workspace, event_type="run.redaction.recorded", payload={}, idempotency_key="masking-fact"
    )
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    with pytest.raises(tasks.TaskJournalError, match="incomplete-pair"):
        tasks.read_tasks(run_dir)
    with pytest.raises(tasks.TaskJournalError, match="incomplete-pair"):
        accepted(task_run)
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before
    assert (
        run_checkpoint.recover_from_checkpoint(run_dir, json.loads((run_dir / "run.json").read_bytes()))["status"]
        == "running"
    )


def test_replayed_old_checkpoint_cannot_cover_new_fact(task_run, monkeypatch):
    run_dir, workspace = task_run
    accepted(task_run)
    events = run_journal.read_journal_bounded(run_dir / "events/lifecycle.jsonl").events
    checkpoint = next(e for e in events if e.payload.get("paired_event_type") == "task.accepted")
    monkeypatch.setattr(run_checkpoint, "write_checkpoint", lambda *a, **kw: checkpoint)
    before = (run_dir / "events/lifecycle.jsonl").read_bytes()
    spec = tasks.TaskSpec("two", "worker", "b" * 64)
    with pytest.raises(run_lifecycle.LifecycleJournalError, match="checkpoint is not at journal tail"):
        run_lifecycle.record_lifecycle_event(
            run_dir,
            workspace=workspace,
            event_type="task.accepted",
            payload={
                "task_id": spec.task_id,
                "seat": spec.seat,
                "payload_digest": spec.payload_digest,
                "spec_digest": spec.spec_digest,
                "request_id": "request-two",
            },
            idempotency_key="new-fact",
        )
    assert (run_dir / "events/lifecycle.jsonl").read_bytes() == before


def test_task_checkpoint_requires_pairing_key(task_run):
    from dataclasses import replace

    run_dir, _ = task_run
    prepared(task_run)
    events = run_journal.read_journal_bounded(run_dir / "events/lifecycle.jsonl").events
    checkpoint = events[-2]
    payload = dict(checkpoint.payload)
    payload.pop("pairing_key")
    unkeyed = replace(checkpoint, payload=payload)
    with pytest.raises(run_checkpoint.CheckpointError, match="task pairing identity"):
        run_checkpoint._verify_coverage([*events[:-2], unkeyed, events[-1]], unkeyed, {"status": "running"})


def test_task_event_requires_v2_envelope():
    envelope = run_events.build_event(
        run_id="example-run",
        sequence=1,
        event_type="task.attempt-prepared",
        payload={"task_id": "one", "attempt_id": "a" * 64},
        idempotency_key="example",
        recorded_at="2026-10-23T00:00:00.000000Z",
        previous_digest=None,
    )
    envelope["schema_version"] = 1
    envelope["event_digest"] = run_events.compute_event_digest(envelope)
    envelope["event_id"] = run_events.make_event_id(
        run_id="example-run", sequence=1, event_digest=envelope["event_digest"]
    )
    assert any("unknown event_type" in e for e in run_events.validate_event(envelope))


def test_cross_process_reader_conservatively_refuses_dispatch_gap(task_run, monkeypatch):
    """A reader can see the gap, but the same read succeeds once its fact lands."""
    import subprocess
    import sys

    run_dir, workspace = task_run
    prepared(task_run)
    real_append = run_lifecycle._append_owner_event
    observed = []
    code = """
import sys
from pathlib import Path
from brigade.run_tasks import read_tasks, TaskJournalError
try:
    read_tasks(Path(sys.argv[1]))
except TaskJournalError as exc:
    print(str(exc))
else:
    raise AssertionError('dispatch gap was presented as a durable task read')
"""

    def read_in_gap(path, **kwargs):
        if kwargs["event_type"] == "run.dispatch.requested":
            result = subprocess.run(
                [sys.executable, "-c", code, str(run_dir)], capture_output=True, text=True, timeout=20
            )
            assert result.returncode == 0, result.stderr
            observed.append(result.stdout.strip())
        return real_append(path, **kwargs)

    monkeypatch.setattr(run_lifecycle, "_append_owner_event", read_in_gap)
    run_lifecycle.record_dispatch_fact(run_dir, workspace=workspace, event_type="run.dispatch.requested", seat="worker")
    assert observed == ["task checkpoint refused: incomplete-pair"]
    assert tasks.read_tasks(run_dir)[0].status == "queued"


@pytest.fixture
def authoritative_task_run(tmp_path):
    """Enroll through real status writes, retaining the lifecycle owner lock."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / "authoritative-task-run"
    run_dir.mkdir(parents=True)
    snapshot = {
        "schema": "brigade.run.v1",
        "status": "started",
        "lock_workspace": str(workspace),
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    with runguard.run_lock(workspace, run_dir=run_dir):
        aboyeur._write_json(run_dir / "run.json", snapshot)
        snapshot["status"] = "planning"
        aboyeur._write_json(run_dir / "run.json", snapshot)
        assert aboyeur._resolve_authority_state(run_dir) == "authoritative"
        assert run_shadow.check_projection_readiness(run_dir).ready
        assert json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())["errors"] == 0
        yield run_dir, workspace


def test_task_pairs_keep_already_authoritative_run_writable(authoritative_task_run):
    run_dir, workspace = authoritative_task_run
    # A durable request alone does not prove that the prior authority gate runs.
    assert aboyeur._resolve_authority_state(run_dir) == "authoritative"
    accepted(authoritative_task_run)
    handle = tasks.prepare_attempt(run_dir, workspace=workspace, task_id="one")
    result = tasks.observe_task(
        run_dir,
        workspace=workspace,
        handle=handle,
        observation=tasks.TaskObservation("completed", artifact_kind="unavailable"),
    )
    assert result.result_id is not None
    tasks.acknowledge_result(run_dir, workspace=workspace, task_id="one", result_id=result.result_id)
    snapshot = json.loads((run_dir / "run.json").read_text())
    snapshot["status"] = "dispatching"
    aboyeur._write_json(run_dir / "run.json", snapshot)
    assert json.loads((run_dir / "run.json").read_text())["status"] == "dispatching"
    assert tasks.read_tasks(run_dir)[0].acknowledged
    assert aboyeur._resolve_authority_state(run_dir) == "authoritative"
    assert run_shadow.check_projection_readiness(run_dir).ready
    artifact = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert artifact["errors"] == 0
    assert artifact["mismatches"] == 0


@pytest.mark.parametrize("damage", ["missing", "mismatched", "nonstring", "wrong-event-type", "keyed-neutral"])
def test_forged_task_pair_cannot_authorize_next_write(authoritative_task_run, damage):
    run_dir, workspace = authoritative_task_run
    journal_path = run_dir / "events/lifecycle.jsonl"
    events = run_journal.read_journal_bounded(journal_path).events
    payload = {
        "task_id": "one",
        "seat": "worker",
        "payload_digest": "a" * 64,
        "spec_digest": tasks.TaskSpec("one", "worker", "a" * 64).spec_digest,
        "request_id": "request-one",
    }
    event_type = "run.redaction.recorded" if damage == "keyed-neutral" else "task.accepted"
    if damage == "keyed-neutral":
        payload = {}
    key = run_events.request_digest(event_type=event_type, payload=payload, idempotency_key="forged-task")
    checkpoint_payload = dict(
        next(e for e in reversed(events) if e.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE).payload
    )
    checkpoint_payload["paired_event_type"] = "task.attempt-prepared" if damage == "wrong-event-type" else event_type
    checkpoint_payload["pairing_key"] = key
    if damage == "missing":
        checkpoint_payload.pop("pairing_key")
    elif damage == "mismatched":
        checkpoint_payload["pairing_key"] = "b" * 64
    elif damage == "nonstring":
        checkpoint_payload["pairing_key"] = 123
    checkpoint = run_journal.append_event(
        journal_path,
        run_id=run_dir.name,
        event_type=run_checkpoint.CHECKPOINT_EVENT_TYPE,
        payload=checkpoint_payload,
        idempotency_key="forged-checkpoint",
        expected_previous_sequence=events[-1].sequence,
        workspace=workspace,
    )
    run_journal.append_event(
        journal_path,
        run_id=run_dir.name,
        event_type=event_type,
        payload=payload,
        idempotency_key="forged-task",
        expected_previous_sequence=checkpoint.sequence,
        workspace=workspace,
    )
    # The chain is valid and exactly one pair ahead, so refusal must come from
    # pair validation, rather than a broken envelope or an unrelated gate.
    assert not run_journal.read_journal_bounded(journal_path).chain_errors
    assert run_shadow.check_projection_readiness(run_dir).reasons == (run_shadow.REASON_JOURNAL_AHEAD,)
    before = journal_path.read_bytes()
    snapshot_bytes = (run_dir / "run.json").read_bytes()
    snapshot = json.loads(snapshot_bytes)
    snapshot["status"] = "dispatching"
    with pytest.raises(run_lifecycle.LifecycleJournalError, match="prior gate not ready: journal-ahead"):
        aboyeur._write_json(run_dir / "run.json", snapshot)
    assert journal_path.read_bytes() == before
    assert (run_dir / "run.json").read_bytes() == snapshot_bytes
