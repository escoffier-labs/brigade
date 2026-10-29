"""Task checkpoint pairs must fit and validate before publishing any bytes."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager

import pytest

from brigade import run_checkpoint, run_events, run_journal, run_lifecycle, run_tasks
from tests import test_run_tasks as support

task_run = support.task_run
_TIMESTAMP = "2026-01-01T00:00:00.000000Z"


def _pair(task_run):
    run_dir, _workspace = task_run
    snapshot = (run_dir / "run.json").read_bytes()
    # Exercise both legacy and stripped authoritative bodies, with a new asset.
    snapshot = snapshot.replace(b'"running"', b'"queued"')
    body_kind = (
        run_checkpoint._BODY_KIND_BASE_STRIPPED if b'"run_journal_authority_requested": true' in snapshot else None
    )
    body = run_checkpoint._strip_journal_metadata_from_base(snapshot) if body_kind else snapshot
    spec = run_tasks.TaskSpec("t" * 128, "w" * 512, "a" * 64)
    payload = {
        "task_id": spec.task_id,
        "seat": spec.seat,
        "payload_digest": spec.payload_digest,
        "spec_digest": spec.spec_digest,
        "request_id": "r" * 512,
    }
    following = ("task.accepted", payload, "new-task-key")
    pairing_key = run_events.request_digest(event_type=following[0], payload=payload, idempotency_key=following[2])
    checkpoint_payload = run_checkpoint._checkpoint_payload(
        body, paired_event_type=following[0], body_kind=body_kind, pairing_key=pairing_key
    )
    checkpoint_key = run_checkpoint._checkpoint_idempotency_key(
        hashlib.sha256(body).hexdigest(),
        paired_event_type=following[0],
        body_kind=body_kind,
        pairing_key=pairing_key,
    )
    return (
        snapshot,
        body_kind,
        pairing_key,
        [(run_checkpoint.CHECKPOINT_EVENT_TYPE, checkpoint_payload, checkpoint_key), following],
    )


def _lines(path, run_id, requests):
    events = run_journal.read_journal_bounded(path).events
    sequence = events[-1].sequence if events else 0
    previous_digest = events[-1].event_digest if events else None
    lines = []
    for event_type, payload, key in requests:
        sequence += 1
        envelope = run_events.build_event(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            idempotency_key=key,
            recorded_at=_TIMESTAMP,
            previous_digest=previous_digest,
        )
        lines.append(run_events.canonical_bytes(envelope) + b"\n")
        previous_digest = envelope["event_digest"]
    return lines


@pytest.mark.parametrize("limit", ["events", "bytes", "line"])
def test_pair_capacity_refusal_precedes_checkpoint_publish(task_run, monkeypatch, limit):
    run_dir, workspace = task_run
    snapshot, body_kind, pairing_key, requests = _pair(task_run)
    journal_path = run_dir / "events/lifecycle.jsonl"
    checkpoint_dir = journal_path.parent / run_checkpoint.CHECKPOINT_DIR_NAME
    before_journal = journal_path.read_bytes()
    before_assets = sorted(checkpoint_dir.iterdir())
    first, second = _lines(journal_path, run_dir.name, requests)
    if limit == "events":
        monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_EVENTS", len(before_journal.splitlines()) + 1)
    elif limit == "bytes":
        monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", len(before_journal) + len(first) + len(second) - 1)
    else:
        assert len(second) > max(len(first), *(len(line) + 1 for line in before_journal.splitlines()))
        monkeypatch.setattr(run_events, "MAX_LINE_BYTES", len(second) - 1)
    message = "^lifecycle journal canonicalization failure$" if limit == "line" else "bound"
    with pytest.raises(run_lifecycle.LifecycleJournalError, match=message):
        run_checkpoint.write_checkpoint(
            run_dir,
            snapshot,
            workspace=workspace,
            paired_event_type="task.accepted",
            body_kind=body_kind,
            pairing_key=pairing_key,
            following_task_request=requests[1],
        )
    assert journal_path.read_bytes() == before_journal
    assert sorted(checkpoint_dir.iterdir()) == before_assets


def test_pair_preflight_is_nonmutating(task_run, monkeypatch):
    run_dir, workspace = task_run
    snapshot, body_kind, pairing_key, requests = _pair(task_run)
    journal_path = run_dir / "events/lifecycle.jsonl"
    before = {p: p.read_bytes() for p in journal_path.parent.rglob("*") if p.is_file()}
    lines = _lines(journal_path, run_dir.name, requests)
    # An exactly fitting pair succeeds, including both terminating newlines.
    monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", len(before[journal_path]) + sum(map(len, lines)))
    run_journal.preflight_new_event_pair(journal_path, run_id=run_dir.name, requests=requests)
    assert {p: p.read_bytes() for p in journal_path.parent.rglob("*") if p.is_file()} == before
    checkpoint = run_checkpoint.write_checkpoint(
        run_dir,
        snapshot,
        workspace=workspace,
        paired_event_type="task.accepted",
        body_kind=body_kind,
        pairing_key=pairing_key,
        following_task_request=requests[1],
    )
    assert checkpoint is not None
    following = run_journal.append_event(
        journal_path,
        run_id=run_dir.name,
        event_type=requests[1][0],
        payload=requests[1][1],
        idempotency_key=requests[1][2],
        expected_previous_sequence=checkpoint.sequence,
        workspace=workspace,
    )
    assert following.previous_digest == checkpoint.event_digest
    assert len(journal_path.read_bytes()) == run_checkpoint.MAX_JOURNAL_BYTES


@pytest.mark.parametrize("existing", [0, 1])
def test_pair_preflight_refuses_existing_keys(task_run, existing):
    run_dir, _workspace = task_run
    _snapshot, _kind, _digest, requests = _pair(task_run)
    journal_path = run_dir / "events/lifecycle.jsonl"
    event_type, payload, key = requests[existing]
    run_journal.append_event(
        journal_path,
        run_id=run_dir.name,
        event_type=event_type,
        payload=payload,
        idempotency_key=key,
        expected_previous_sequence=run_journal.read_journal_bounded(journal_path).events[-1].sequence,
    )
    before = journal_path.read_bytes()
    with pytest.raises(run_journal.RunJournalError, match="key"):
        run_journal.preflight_new_event_pair(journal_path, run_id=run_dir.name, requests=requests)
    assert journal_path.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["invalid-first", "invalid-second", "oversized-second", "type", "digest", "same-key", "one", "three"]
)
def test_pair_preflight_validates_both_lines(task_run, monkeypatch, fault):
    run_dir, _workspace = task_run
    _snapshot, _kind, _digest, requests = _pair(task_run)
    journal_path = run_dir / "events/lifecycle.jsonl"
    before = journal_path.read_bytes()
    if fault.startswith("invalid"):
        requests[0 if fault == "invalid-first" else 1][1]["unknown"] = "invalid"
    elif fault == "oversized-second":
        first, second = _lines(journal_path, run_dir.name, requests)
        assert len(second) > len(first)
        monkeypatch.setattr(run_events, "MAX_LINE_BYTES", len(second) - 1)
    elif fault == "type":
        requests[0][1]["paired_event_type"] = "task.attempt-prepared"
    elif fault == "digest":
        requests[0][1]["pairing_key"] = "b" * 64
    elif fault == "same-key":
        requests[1] = (*requests[1][:2], requests[0][2])
        requests[0][1]["pairing_key"] = run_events.request_digest(
            event_type=requests[1][0], payload=requests[1][1], idempotency_key=requests[1][2]
        )
    elif fault == "one":
        requests.pop()
    else:
        requests.append(requests[-1])
    with pytest.raises((run_journal.RunJournalError, run_events.CanonicalizationError)):
        run_journal.preflight_new_event_pair(journal_path, run_id=run_dir.name, requests=requests)
    assert journal_path.read_bytes() == before


@pytest.mark.parametrize("suffix", [b"partial", b"{}\n"])
def test_pair_preflight_refuses_damaged_history(task_run, suffix):
    run_dir, _workspace = task_run
    _snapshot, _kind, _digest, requests = _pair(task_run)
    journal_path = run_dir / "events/lifecycle.jsonl"
    journal_path.write_bytes(journal_path.read_bytes() + suffix)
    before = journal_path.read_bytes()
    with pytest.raises(run_journal.RunJournalError):
        run_journal.preflight_new_event_pair(journal_path, run_id=run_dir.name, requests=requests)
    assert journal_path.read_bytes() == before


@pytest.mark.parametrize("phase", ["enter", "exit"])
def test_optional_checkpoint_normalizes_lock_failures(task_run, monkeypatch, phase):
    run_dir, workspace = task_run
    snapshot, body_kind, pairing_key, requests = _pair(task_run)
    journal_path = run_dir / "events/lifecycle.jsonl"
    checkpoint_dir = journal_path.parent / run_checkpoint.CHECKPOINT_DIR_NAME
    before = journal_path.read_bytes()
    before_assets = sorted(checkpoint_dir.iterdir())
    mutation = run_journal.journal_mutation

    @contextmanager
    def failing_guard(path):
        if phase == "enter":
            raise OSError("private-lock-detail")
        with mutation(path):
            yield
        raise run_journal.RunJournalError("journal lock close failed")

    monkeypatch.setattr(run_journal, "journal_mutation", failing_guard)
    message = r"^lifecycle journal I/O failure \(OSError\)$" if phase == "enter" else "^journal lock close failed$"
    with pytest.raises(run_lifecycle.LifecycleJournalError, match=message) as failure:
        run_checkpoint.write_checkpoint(
            run_dir,
            snapshot,
            workspace=workspace,
            paired_event_type="task.accepted",
            body_kind=body_kind,
            pairing_key=pairing_key,
            following_task_request=requests[1],
        )
    assert "private-lock-detail" not in str(failure.value)
    if phase == "enter":
        assert journal_path.read_bytes() == before
        assert sorted(checkpoint_dir.iterdir()) == before_assets
    else:
        # A release failure after publication cannot roll back committed bytes.
        assert journal_path.read_bytes().startswith(before)
        report = run_journal.read_journal_bounded(journal_path)
        assert not report.chain_errors and report.partial_tail is None
        assert report.events[-1].event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE
        assert len(report.events) == len(before.splitlines()) + 1
