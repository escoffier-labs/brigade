"""Task journal refusal, pairing and durability boundaries."""

from __future__ import annotations

import json

import pytest

from brigade import run_checkpoint, run_events, run_journal, run_lifecycle
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
