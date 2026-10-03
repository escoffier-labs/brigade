"""Cross-boundary qualification of journal acceptance, replay and projection."""

from copy import deepcopy
import errno
import json
import os
import select
import subprocess
import sys

import pytest

from brigade import aboyeur, run_checkpoint, run_events, run_journal, run_lifecycle, run_projector, runguard

RUN_ID = "20260727-153045-a1b2c3d4"
RECORDED_AT = "2026-07-27T15:30:45.123456Z"


def _append(path, *, event_type="run.created", payload=None, key="create", previous=0):
    return run_journal.append_event(
        path,
        run_id=RUN_ID,
        event_type=event_type,
        payload={"status": "started"} if payload is None else payload,
        idempotency_key=key,
        expected_previous_sequence=previous,
        recorded_at=RECORDED_AT,
    )


def _cancel_payload():
    return {
        "transport_capability": "interrupt",
        "transport_result": "interrupted",
        "active_remaining": 0,
        "active_seats": [],
        "outcomes": [{"seat": "coder", "transport_capability": "interrupt", "transport_result": "interrupted"}],
    }


def test_caller_nested_mutation_does_not_change_returned_accepted_event(tmp_path):
    path = tmp_path / "events" / "lifecycle.jsonl"
    payload = _cancel_payload()
    event = _append(path, event_type="run_budget.cancelled", payload=payload)
    accepted = deepcopy(event.to_dict())
    before = path.read_bytes()

    payload["outcomes"][0]["seat"] = "reviewer"
    payload["active_seats"].append("reviewer")

    assert event.to_dict() == accepted
    assert run_events.validate_event(event.to_dict()) == []
    assert path.read_bytes() == before
    assert run_journal.read_journal_bounded(path).events[0].to_dict() == accepted


def test_returned_nested_dictionary_cannot_mutate_event_or_replay(tmp_path):
    path = tmp_path / "events" / "lifecycle.jsonl"
    event = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    accepted = deepcopy(event.to_dict())
    exported = event.to_dict()
    exported["payload"]["outcomes"][0]["seat"] = "reviewer"

    assert event.to_dict() == accepted
    replay = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    assert replay.to_dict() == accepted
    assert (
        run_projector.project_run_snapshot({"status": "started"}, [event], journal_present=True).last_event_digest
        == event.event_digest
    )


def test_caller_mutation_during_write_cannot_change_accepted_return(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    payload = _cancel_payload()
    original = deepcopy(payload)
    real_write = os.write

    def mutate_caller_after_write(fd, data):
        written = real_write(fd, data)
        payload["outcomes"][0]["seat"] = "reviewer"
        return written

    monkeypatch.setattr(os, "write", mutate_caller_after_write)
    event = _append(path, event_type="run_budget.cancelled", payload=payload)
    assert event.payload == original
    assert run_events.validate_event(event.to_dict()) == []
    assert run_journal.read_journal_bounded(path).events[0].to_dict() == event.to_dict()


@pytest.mark.parametrize("source", ["append", "read", "lookup", "replay"])
def test_mutable_returned_payload_cannot_rewrite_persisted_history(tmp_path, source):
    path = tmp_path / "events" / "lifecycle.jsonl"
    event = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    accepted = deepcopy(event.to_dict())
    before = path.read_bytes()
    if source == "read":
        event = run_journal.read_journal_bounded(path).events[0]
    elif source == "lookup":
        event = run_journal.lookup_idempotent_event(
            path, event_type="run_budget.cancelled", payload=_cancel_payload(), idempotency_key="create"
        )
    elif source == "replay":
        event = _append(path, event_type="run_budget.cancelled", payload=_cancel_payload())
    assert event is not None
    event.payload["outcomes"][0]["seat"] = "reviewer"

    assert path.read_bytes() == before
    assert run_journal.read_journal_bounded(path).events[0].to_dict() == accepted
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot({"status": "started"}, [event], journal_present=True)


_SAME_KEY_CHILD = r"""
import json
import sys
from pathlib import Path
from brigade import run_journal

path = Path(sys.argv[1])
detail = sys.argv[2]
report = run_journal.read_journal_bounded(path)
assert not report.chain_errors and report.partial_tail is None
tail = report.events[-1].sequence if report.events else 0
print(tail, flush=True)
assert sys.stdin.readline().strip() == "go"
try:
    event = run_journal.append_event(
        path, run_id=sys.argv[3], event_type="run.planning.started",
        payload={"detail": detail}, idempotency_key="shared-request",
        expected_previous_sequence=tail, recorded_at=sys.argv[4],
    )
    print(json.dumps({"event": event.to_dict()}), flush=True)
except run_journal.IdempotencyConflict as exc:
    print(json.dumps({"conflict": exc.existing_event_id}), flush=True)
"""


@pytest.mark.skipif(run_journal.fcntl is None, reason="cross-process locking requires POSIX flock")
@pytest.mark.parametrize("conflicting", [False, True])
def test_same_key_process_race_has_one_record_and_consistent_result(tmp_path, conflicting):
    path = tmp_path / "events" / "lifecycle.jsonl"
    run_journal.ensure_journal(path)
    children = []
    try:
        for detail in ("alpha", "beta" if conflicting else "alpha"):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", _SAME_KEY_CHILD, str(path), detail, RUN_ID, RECORDED_AT],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        # Both processes have read the identical head before either may append.
        for child in children:
            assert select.select([child.stdout], [], [], 15)[0], "child did not reach head barrier"
            assert child.stdout.readline().strip() == "0"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == []
    assert report.partial_tail is None
    assert len(report.events) == 1
    accepted = report.events[0]
    assert accepted.sequence == 1 and accepted.previous_digest is None
    assert all(result["event"] == accepted.to_dict() for result in results if "event" in result)
    if conflicting:
        assert sum("conflict" in result for result in results) == 1
        assert next(result["conflict"] for result in results if "conflict" in result) == accepted.event_id
    else:
        assert results == [{"event": accepted.to_dict()}] * 2
    before = path.read_bytes()
    replay = _append(path, event_type=accepted.event_type, payload=accepted.payload, key="shared-request", previous=0)
    assert replay.to_dict() == accepted.to_dict()
    assert path.read_bytes() == before


def test_short_write_recovery_then_retry_accepts_exactly_once(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    before = path.read_bytes()
    projection = run_projector.project_run_snapshot({"status": "started"}, [first], journal_present=True)
    real_write = os.write
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", lambda fd, data: real_write(fd, data[: len(data) // 2]))
        with pytest.raises(run_journal.PartialWriteError):
            _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)

    interrupted = path.read_bytes()
    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == []
    assert report.events == [first]
    assert report.partial_tail == interrupted[len(before) :]
    assert (
        run_projector.project_run_snapshot({"status": "started"}, report.events, journal_present=True).to_bytes()
        == projection.to_bytes()
    )
    with pytest.raises(run_journal.PartialTailError):
        _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    assert path.read_bytes() == interrupted

    recovery = run_journal.recover_partial_tail(path, tmp_path / "quarantine")
    assert recovery.quarantine_path.read_bytes() == report.partial_tail
    assert path.read_bytes() == before
    second = _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    accepted = path.read_bytes()
    assert (
        _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
        == second
    )
    assert path.read_bytes() == accepted
    assert second.sequence == 2 and second.previous_digest == first.event_digest
    rebuilt = run_projector.project_run_snapshot(
        projection.snapshot, run_journal.read_journal_bounded(path).events, journal_present=True
    )
    assert rebuilt.status == "planning" and rebuilt.last_sequence == 2


@pytest.mark.parametrize("failure", ["short_write", "file_fsync", "directory_fsync"])
def test_failed_quarantine_durability_preserves_partial_journal(tmp_path, monkeypatch, failure):
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    before = path.read_bytes() + b'{"incomplete":'
    path.write_bytes(before)
    quarantine = tmp_path / "quarantine"

    def fail_fsync(fd):
        raise OSError(errno.EIO, "injected quarantine fsync failure")

    def fail_directory_fsync(directory):
        raise run_journal.RunJournalError("injected quarantine directory fsync failure")

    real_write = os.write
    with monkeypatch.context() as patch:
        if failure == "short_write":
            patch.setattr(os, "write", lambda fd, data: real_write(fd, data[: len(data) // 2]))
            error = run_journal.PartialWriteError
        elif failure == "file_fsync":
            patch.setattr(os, "fsync", fail_fsync)
            error = OSError
        else:
            patch.setattr(run_journal, "_fsync_directory", fail_directory_fsync)
            error = run_journal.RunJournalError
        with pytest.raises(error):
            run_journal.recover_partial_tail(path, quarantine)
    assert path.read_bytes() == before
    report = run_journal.read_journal_bounded(path)
    assert report.events == [first] and report.partial_tail == b'{"incomplete":'
    recovery = run_journal.recover_partial_tail(path, quarantine)
    assert recovery.quarantine_path.read_bytes() == report.partial_tail
    assert path.read_bytes() == before[: -len(report.partial_tail)]


def test_derived_drift_rebuilds_but_invalid_authority_fails_closed(tmp_path):
    path = tmp_path / "events" / "lifecycle.jsonl"
    _append(path)
    _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    authoritative = path.read_bytes()
    report = run_journal.read_journal_bounded(path)
    base = {"status": "started", "failure": {"detail": None}}
    expected = run_projector.project_run_snapshot(base, report.events, journal_present=True)
    drifted = {
        **base,
        "status": "failed",
        "projector_version": -1,
        "journal_present": False,
        "journal_last_sequence": 999,
        "journal_last_event_digest": "0" * 64,
    }
    rebuilt = run_projector.project_run_snapshot(drifted, report.events, journal_present=True)
    assert rebuilt.to_bytes() == expected.to_bytes()
    assert path.read_bytes() == authoritative

    corrupted = authoritative.replace(b'"detail":"planning"', b'"detail":"tampered"')
    assert corrupted != authoritative
    path.write_bytes(corrupted)
    rejected = run_journal.read_journal_bounded(path)
    assert rejected.chain_errors
    assert [event.sequence for event in rejected.events] == [1]
    with pytest.raises(run_journal.ChainIntegrityError):
        _append(path, event_type="run.completed", payload={"status": "completed"}, key="complete", previous=2)
    with pytest.raises(run_journal.ChainIntegrityError):
        run_journal.lookup_idempotent_event(
            path, event_type="run.created", payload={"status": "started"}, idempotency_key="create"
        )
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(
            base, [json.loads(line) for line in corrupted.splitlines()], journal_present=True
        )
    assert path.read_bytes() == corrupted


def test_prewrite_error_leaves_journal_and_projection_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    first = _append(path)
    before = path.read_bytes()
    projection = run_projector.project_run_snapshot({"status": "started"}, [first], journal_present=True)

    def fail_write(fd, data):
        raise OSError(errno.EIO, "injected journal write failure")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="injected journal write failure"):
        _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
    assert path.read_bytes() == before
    report = run_journal.read_journal_bounded(path)
    assert report.events == [first] and report.chain_errors == [] and report.partial_tail is None
    assert (
        run_projector.project_run_snapshot({"status": "started"}, report.events, journal_present=True).to_bytes()
        == projection.to_bytes()
    )


def test_failed_fsync_leaves_snapshot_unchanged_but_complete_journal_line_visible(tmp_path, monkeypatch):
    """Qualification limit: a failed fsync is not proof that no bytes persisted."""
    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot_path = run_dir / "run.json"
    payload = {
        "schema": "brigade.run.v1",
        "status": "started",
        "lock_workspace": str(repo),
        "lifecycle_journal_requested": True,
    }
    with runguard.run_lock(repo, run_dir=run_dir):
        aboyeur._write_json(snapshot_path, payload)
    path = run_dir / "events" / "lifecycle.jsonl"
    before_snapshot = snapshot_path.read_bytes()
    before_journal = path.read_bytes()
    real_fsync = os.fsync
    journal_inode = path.stat()

    def fail_journal_fsync(fd):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (journal_inode.st_dev, journal_inode.st_ino):
            raise OSError(errno.EIO, "injected journal fsync failure")
        return real_fsync(fd)

    with runguard.run_lock(repo, run_dir=run_dir):
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_journal_fsync)
            with pytest.raises(run_lifecycle.LifecycleJournalError):
                aboyeur._write_json(snapshot_path, {**payload, "status": "planning"})

    assert snapshot_path.read_bytes() == before_snapshot
    # This is the confirmed gap in the stronger failed-append invariant.
    # No successful return occurred, yet the complete checkpoint line is
    # visible to readers. Its persistence across power loss is unknown.
    assert path.read_bytes() != before_journal
    report = run_journal.read_journal_bounded(path)
    assert report.chain_errors == [] and report.partial_tail is None
    assert len(report.events) == len(before_journal.splitlines()) + 1


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_fsync_error_retry_requires_new_sync_without_second_append(tmp_path, monkeypatch, failure):
    """Visible bytes need a fresh acknowledgment barrier, without rollback."""
    path = tmp_path / "events" / "lifecycle.jsonl"
    _append(path)
    before = path.read_bytes()

    def fail_sync(*args):
        raise OSError(errno.EIO, "injected journal sync failure")

    with monkeypatch.context() as patch:
        if failure == "file":
            patch.setattr(os, "fsync", fail_sync)
            error = OSError
        else:
            patch.setattr(run_journal, "_fsync_directory", fail_sync)
            error = (OSError, run_journal.RunJournalError)
        with pytest.raises(error):
            _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)

        uncertain = path.read_bytes()
        assert uncertain != before
        report = run_journal.read_journal_bounded(path)
        assert report.chain_errors == [] and report.partial_tail is None
        assert len(report.events) == 2
        for _ in range(2):
            with pytest.raises(run_journal.RunJournalError):
                _append(path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1)
            assert path.read_bytes() == uncertain

    syncs = []
    real_fsync = os.fsync

    def track_sync(fd):
        info = os.fstat(fd)
        syncs.append("file" if info.st_ino == path.stat().st_ino else "directory")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", track_sync)
        replay = _append(
            path, event_type="run.planning.started", payload={"detail": "planning"}, key="plan", previous=1
        )
    assert syncs == (["file", "directory"] if os.name == "posix" else ["file"])
    assert replay.to_dict() == report.events[-1].to_dict()
    assert path.read_bytes() == uncertain


def test_creation_directory_sync_failure_cannot_be_bypassed_by_new_append(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"

    def fail_directory(directory):
        raise run_journal.RunJournalError("injected creation directory sync failure")

    with monkeypatch.context() as patch:
        patch.setattr(run_journal, "_fsync_directory", fail_directory)
        with pytest.raises(run_journal.RunJournalError):
            _append(path)
        assert path.read_bytes() == b""
        with pytest.raises(run_journal.RunJournalError):
            _append(path)
        visible = run_journal.read_journal_bounded(path).events
        assert len(visible) == 1
    before = path.read_bytes()
    assert _append(path) == visible[0]
    assert path.read_bytes() == before


@pytest.mark.parametrize("api", ["generic", "status", "checkpoint"])
def test_owner_replay_sync_failure_is_bounded_and_recovery_keeps_original_event(tmp_path, monkeypatch, api):
    repo = tmp_path / "repo"
    run_dir = repo / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    snapshot = {"status": "started", "lock_workspace": str(repo), "lifecycle_journal_requested": True}
    snapshot_path = run_dir / "run.json"
    snapshot_path.write_text(json.dumps(snapshot))
    path = run_dir / "events" / "lifecycle.jsonl"

    def record():
        if api == "generic":
            return run_lifecycle.record_lifecycle_event(
                run_dir,
                event_type="approval.requested",
                payload={"approval_id": "approval-1", "source": "daily", "contract_fingerprint": "contract-1"},
                idempotency_key="approval:requested:stable",
                workspace=repo,
            )
        if api == "status":
            return run_lifecycle.record_lifecycle_transition(
                run_dir,
                status="planning",
                workspace=repo,
                incoming_snapshot={"detail": "changed refresh detail"},
            )
        return run_checkpoint.write_checkpoint(
            run_dir,
            json.dumps(snapshot).encode(),
            workspace=repo,
            paired_event_type="run.planning.started",
        )

    with runguard.run_lock(repo, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=repo)
        original = record()
        assert original is not None
        if api == "status":
            snapshot_path.write_text(json.dumps({**snapshot, "status": "planning"}))
        before = path.read_bytes()
        journal_inode = path.stat()
        real_fsync = os.fsync

        def fail_journal_sync(fd):
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (journal_inode.st_dev, journal_inode.st_ino):
                raise OSError(errno.EIO, "injected owner replay fsync failure")
            return real_fsync(fd)

        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_journal_sync)
            with pytest.raises(run_lifecycle.LifecycleJournalError):
                record()
        assert path.read_bytes() == before
        assert record().to_dict() == original.to_dict()
        assert path.read_bytes() == before


def test_read_only_lookup_and_journal_read_do_not_sync(tmp_path, monkeypatch):
    path = tmp_path / "events" / "lifecycle.jsonl"
    original = _append(path)
    before = path.read_bytes()

    def forbidden_sync(*args):
        raise AssertionError("read-only API attempted sync")

    monkeypatch.setattr(os, "fsync", forbidden_sync)
    monkeypatch.setattr(run_journal, "_fsync_directory", forbidden_sync)
    assert (
        run_journal.lookup_idempotent_event(
            path,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key="create",
        )
        == original
    )
    assert run_journal.read_journal_bounded(path).events == [original]
    assert path.read_bytes() == before
