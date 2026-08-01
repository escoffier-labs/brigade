"""Regression tests for opt-in lifecycle journaling (issue #568 slice 2).

Covers the corrected lifecycle-journaling contract: the per-run request field
is the durable opt-in marker until journaling activates under the run lock,
active run-lock ownership is verified against the workspace recorded in the run
receipt (custom ``--output-dir`` layouts included), an unlocked writer on an
active journal fails closed with ``run.json`` unchanged, status transitions are
recorded (detail refreshes are not), transition identity is keyed on the
prior snapshot digest so crash/retry replays the committed event while a
later recurrence appends, lifecycle payloads never carry raw run.json error
strings, and raw OSError/CanonicalizationError surface only as bounded generic
categories that block the snapshot. Every journaling append runs under the real
``runguard.run_lock`` context.
"""

from __future__ import annotations

import json
import signal
import stat
import threading
from pathlib import Path

import pytest

from brigade import (
    aboyeur,
    agents,
    localio,
    proc,
    run_checkpoint,
    run_events,
    run_journal,
    run_lifecycle,
    run_projector,
    runguard,
    run_transport,
)
from brigade import roster as roster_mod

_REQUEST_FIELD = "lifecycle_journal_requested"


def _git(repo: Path, *args: str) -> proc.Result:
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr
    return result


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


_RUN_ID = "20260728-160000-abcd1234"


def _run_dir(repo: Path, run_id: str = _RUN_ID) -> Path:
    run_dir = repo / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


def _minimal_roster() -> roster_mod.Roster:
    return roster_mod.Roster(
        orchestrator="chef",
        agents={"chef": roster_mod.Agent("chef", "codex", "plan")},
    )


def _run_payload(status: str, *, error: str | None = None, lock_workspace: Path | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"schema": "brigade.run.v1", "status": status}
    if error is not None:
        payload["error"] = error
    if lock_workspace is not None:
        payload["lock_workspace"] = str(lock_workspace)
    return payload


def _apply_lifecycle_request(run_dir: Path, payload: dict[str, object]) -> dict[str, object]:
    run_json = run_dir / "run.json"
    if run_json.is_file():
        existing = json.loads(run_json.read_text())
        if existing.get(_REQUEST_FIELD) is True:
            payload[_REQUEST_FIELD] = True
    elif run_lifecycle.is_lifecycle_journaling_enabled():
        payload[_REQUEST_FIELD] = True
    return payload


def _write_run_json(
    run_dir: Path,
    status: str,
    *,
    error: str | None = None,
    lock_workspace: Path | None = None,
) -> None:
    """Write run.json the way aboyeur does (lifecycle append then atomic snapshot)."""
    payload = _apply_lifecycle_request(
        run_dir,
        _run_payload(status, error=error, lock_workspace=lock_workspace),
    )
    aboyeur._write_json(run_dir / "run.json", payload)


def test_record_run_start_does_not_opt_in_existing_legacy_run(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    localio.write_json(run_dir / "run.json", _run_payload("started", lock_workspace=repo))
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")

    aboyeur.record_run_start(
        run_dir,
        task="legacy run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )

    receipt = json.loads((run_dir / "run.json").read_text())
    assert _REQUEST_FIELD not in receipt
    assert not _journal_path(run_dir).exists()


def _write_run_json_locked(
    repo: Path,
    run_dir: Path,
    status: str,
    *,
    error: str | None = None,
    lock_workspace: Path | None = None,
) -> None:
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json(run_dir, status, error=error, lock_workspace=lock_workspace)


def _events(run_dir: Path) -> list[run_journal.RunEvent]:
    return run_journal.read_journal(_journal_path(run_dir)).events


def _status_events(run_dir: Path) -> list[run_journal.RunEvent]:
    """Lifecycle status-transition events, excluding recovery checkpoint events."""
    return [e for e in _events(run_dir) if e.event_type != run_checkpoint.CHECKPOINT_EVENT_TYPE]


def _checkpoint_events(run_dir: Path) -> list[run_journal.RunEvent]:
    return [e for e in _events(run_dir) if e.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE]


def _checkpoint_dir(run_dir: Path) -> Path:
    return run_dir / "events" / run_checkpoint.CHECKPOINT_DIR_NAME


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    yield
    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)


def test_flag_off_is_byte_compatible_and_creates_no_journal(disabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json_locked(repo, run_dir, "started")

    assert (run_dir / "run.json").is_file()
    assert not (run_dir / "events").exists()
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "started"
    assert _REQUEST_FIELD not in meta


def test_pre_lock_new_run_bootstrap_writes_request_without_journal(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # The pre-lock bootstrap writes run.json before runguard.run_lock is held:
    # it records the durable request but must not create the journal.
    _write_run_json(run_dir, "started")

    assert (run_dir / "run.json").is_file()
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta[_REQUEST_FIELD] is True
    assert not (run_dir / "events").exists()


def test_no_journal_file_until_lock_held(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    assert _journal_path(run_dir).is_file()
    assert sorted(path.name for path in (run_dir / "events").iterdir()) == [
        "lifecycle.jsonl",
        "recovery-checkpoints",
        "shadow-comparison.json",
    ]


def test_in_lock_write_appends_run_created(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap: request only
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: checkpoint then run.created

    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.snapshot.checkpointed", "run.created"]
    assert [e.sequence for e in events] == [1, 2]
    assert events[1].payload == {"status": "started"}
    assert events[0].previous_digest is None
    assert events[1].previous_digest == events[0].event_digest


def test_recording_without_matching_lock_fails_closed(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap: request only
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: checkpoint + run.created
    run_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()

    # Activated run, but no lock held: the write fails closed and neither the
    # journal nor the run.json snapshot advances. write_checkpoint skips its
    # publish/append under the missing lock so no spurious checkpoint event
    # is appended before record_lifecycle_transition raises.
    with pytest.raises(run_lifecycle.LifecycleJournalError):
        _write_run_json(run_dir, "planning")

    assert (run_dir / "run.json").read_bytes() == run_before
    assert _journal_path(run_dir).read_bytes() == journal_before
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created"]


def test_unmapped_status_on_active_journal_without_lock_fails_closed(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # activate journal
    run_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    files_before = sorted(_checkpoint_dir(run_dir).iterdir())

    # No lock held on the active journal. An unmapped status has no status
    # event, but it must NOT mutate the snapshot unlocked: when the journal is
    # active, missing matching ownership must fail closed for every status.
    # The bounded error surfaces before any checkpoint/status append and before
    # run.json changes.
    with pytest.raises((run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError)):
        _write_run_json(run_dir, "artifact-collection")

    assert (run_dir / "run.json").read_bytes() == run_before
    assert _journal_path(run_dir).read_bytes() == journal_before
    assert sorted(_checkpoint_dir(run_dir).iterdir()) == files_before
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created"]


def test_custom_output_dir_appends_under_matching_lock(enabled, tmp_path):
    repo = _repo(tmp_path)
    # A custom --output-dir layout: not under <workspace>/.brigade/runs.
    run_dir = tmp_path / "custom-output" / _RUN_ID
    run_dir.mkdir(parents=True)

    # The receipt payload carries lock_workspace, exactly as record_run_start
    # writes it; lock ownership resolves from that, not from the run layout.
    _write_run_json(run_dir, "started", lock_workspace=repo)  # pre-lock bootstrap
    _write_run_json_locked(repo, run_dir, "started", lock_workspace=repo)

    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.snapshot.checkpointed", "run.created"]


def test_long_custom_output_dir_final_component_journals(enabled, tmp_path):
    repo = _repo(tmp_path)
    long_run_id = "x" * 200
    run_dir = tmp_path / "custom-output" / long_run_id
    run_dir.mkdir(parents=True)

    _write_run_json(run_dir, "started", lock_workspace=repo)
    _write_run_json_locked(repo, run_dir, "started", lock_workspace=repo)

    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.snapshot.checkpointed", "run.created"]
    created = [e for e in events if e.event_type == "run.created"][0]
    assert len(created.idempotency_key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN
    assert created.idempotency_key.startswith("lifecycle:")
    checkpoint = [e for e in events if e.event_type == "run.snapshot.checkpointed"][0]
    assert len(checkpoint.idempotency_key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN
    assert checkpoint.idempotency_key.startswith("checkpoint:")


def test_mismatched_workspace_skips_journal_until_correct_lock(enabled, tmp_path):
    repo = _repo(tmp_path)
    other = tmp_path / "other-workspace"
    other.mkdir()
    run_dir = tmp_path / "custom-output" / _RUN_ID
    run_dir.mkdir(parents=True)

    _write_run_json(run_dir, "started", lock_workspace=other)  # pre-lock bootstrap
    assert not (run_dir / "events").exists()

    # The lock is held at `repo` but the receipt claims `other`: journaling is
    # still only requested, so the write proceeds without creating a journal.
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json(run_dir, "started", lock_workspace=other)

    assert not (run_dir / "events").exists()
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "started"
    assert meta[_REQUEST_FIELD] is True

    # Once journaling is active, a mismatched workspace fails closed.
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json(run_dir, "started", lock_workspace=repo)
    assert _journal_path(run_dir).is_file()
    run_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json(run_dir, "planning", lock_workspace=other)
    assert (run_dir / "run.json").read_bytes() == run_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_legacy_run_dir_stays_snapshot_only_with_flag_on(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    # Legacy run directory: run.json present, no events/ journal.
    localio.write_json(run_dir / "run.json", {"schema": "brigade.run.v1", "status": "started"})
    assert not (run_dir / "events").exists()

    _write_run_json_locked(repo, run_dir, "planning")

    assert (run_dir / "run.json").is_file()
    assert not (run_dir / "events").exists()
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "planning"
    assert _REQUEST_FIELD not in meta


def test_activated_run_continues_without_env_flag(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap: request only
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: checkpoint + run.created

    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)
    _write_run_json_locked(repo, run_dir, "planning")

    assert [e.event_type for e in _status_events(run_dir)] == ["run.created", "run.planning.started"]


def test_same_status_refresh_is_noop(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # same-status refresh

    # The status event does not repeat (same prior status), and the checkpoint
    # replays: identical run.json bytes + identical paired_event_type derive
    # the same checkpoint idempotency key, so no new file or event.
    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.snapshot.checkpointed", "run.created"]
    assert len(_checkpoint_events(run_dir)) == 1
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created"]


def test_same_status_with_changed_detail_appends_no_status_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    secret_error = "SECRET_TOKEN=/super/private/path"

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "failed", error=secret_error)
    status_before = [e.event_type for e in _status_events(run_dir)]

    # A detail refresh is not a status transition: no new status event appends,
    # but the run.json snapshot refresh still proceeds. The checkpoint event
    # DOES append because the run.json bytes changed (new error string), so the
    # full journal grows by one checkpoint event with a fresh content hash.
    _write_run_json_locked(repo, run_dir, "failed", error="different failure")

    assert [e.event_type for e in _status_events(run_dir)] == status_before
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created", "run.failed"]
    failed = [e for e in _status_events(run_dir) if e.event_type == "run.failed"][0]
    assert failed.payload == {"status": "failed", "detail": "failed"}
    journal_text = _journal_path(run_dir).read_text()
    assert secret_error not in journal_text
    assert "/super/private/path" not in journal_text
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "failed"
    assert meta["error"] == "different failure"


def test_aba_recurrence_appends_all_three_occurrences(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dispatching")
    _write_run_json_locked(repo, run_dir, "result-processing")
    _write_run_json_locked(repo, run_dir, "dispatching")  # A-B-A recurrence

    assert [e.event_type for e in _status_events(run_dir)] == [
        "run.created",
        "run.dispatching.started",
        "run.result-processing.started",
        "run.dispatching.started",
    ]
    # Checkpoints interleave before each status event, but a checkpoint
    # replays when the run.json bytes + paired_event_type repeat. The second
    # "dispatching" write produces identical run.json bytes to the first, so
    # its checkpoint replays and only the status event appends.
    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.snapshot.checkpointed",
        "run.dispatching.started",
        "run.snapshot.checkpointed",
        "run.result-processing.started",
        "run.dispatching.started",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6, 7]
    assert events[3].previous_digest == events[2].event_digest
    assert events[6].previous_digest == events[5].event_digest


def test_dispatch_facts_pair_each_real_attempt_without_reusing_pending_identity(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started", lock_workspace=repo)
    _write_run_json_locked(repo, run_dir, "started", lock_workspace=repo)
    _write_run_json_locked(repo, run_dir, "dispatching", lock_workspace=repo)

    with runguard.run_lock(repo, run_dir=run_dir):
        first = run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.requested",
            seat="coder",
        )
        second = run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.requested",
            seat="coder",
        )
        assert first is not None
        assert second is not None
        assert first.payload["attempt"] == 1
        assert second.payload["attempt"] == 2
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.observed",
            seat="coder",
            attempt=1,
        )
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.failed",
            seat="coder",
            attempt=1,
        )

    events = _events(run_dir)
    facts = [event for event in events if event.event_type.startswith("run.dispatch.")]
    assert [(event.event_type, event.payload["attempt"]) for event in facts[-4:]] == [
        ("run.dispatch.requested", 1),
        ("run.dispatch.requested", 2),
        ("run.dispatch.observed", 1),
        ("run.dispatch.failed", 1),
    ]
    dispatch_checkpoints = [
        event
        for event in events
        if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE
        and event.payload.get("paired_event_type", "").startswith("run.dispatch.")
        and "pairing_key" in event.payload
    ]
    assert len(dispatch_checkpoints) == 4
    assert len({event.payload["pairing_key"] for event in dispatch_checkpoints}) == 4
    assert run_lifecycle.pending_dispatch_requests(events) == [("coder", 2)]


def test_artifact_collection_intermediate_appends_the_second_a(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dispatching")
    # Mapped artifact-collection status: a checkpoint event appends (paired
    # with run.artifact_collection.started) and the status event appends, and
    # run.json still advances to it.
    _write_run_json_locked(repo, run_dir, "artifact-collection")
    # A-B-A: the second dispatching is a real transition from the
    # artifact-collection snapshot and must append even though the journal
    # tail status payload matches.
    _write_run_json_locked(repo, run_dir, "dispatching")

    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "dispatching"
    assert [e.event_type for e in _status_events(run_dir)] == [
        "run.created",
        "run.dispatching.started",
        "run.artifact_collection.started",
        "run.dispatching.started",
    ]
    events = _events(run_dir)
    # The second "dispatching" produces identical run.json bytes to the first,
    # so its checkpoint replays; only the status event appends.
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.snapshot.checkpointed",
        "run.dispatching.started",
        "run.snapshot.checkpointed",
        "run.artifact_collection.started",
        "run.dispatching.started",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6, 7]
    # The second run.dispatching.started links to the artifact-collection
    # status event immediately before it (its checkpoint replayed, so no
    # checkpoint sits between them).
    assert events[6].previous_digest == events[5].event_digest
    # The artifact-collection checkpoint is paired with run.artifact_collection.started.
    assert events[4].payload["paired_event_type"] == "run.artifact_collection.started"


def test_retry_after_interruption_reuses_committed_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    # Simulate append-before-snapshot interruption: record the transition
    # (fsync happens inside append_event) without advancing run.json.
    with runguard.run_lock(repo, run_dir=run_dir):
        run_lifecycle.record_lifecycle_transition(run_dir, status="result-processing", workspace=repo)
    journal_before = _journal_path(run_dir).read_bytes()

    # Retry the same transition (e.g. the caller re-entered after a crash
    # between journal fsync and run.json replacement): the prior snapshot is
    # unchanged, so the derived idempotency key matches and the committed
    # event is returned without a second append.
    with runguard.run_lock(repo, run_dir=run_dir):
        replay = run_lifecycle.record_lifecycle_transition(run_dir, status="result-processing", workspace=repo)

    assert replay is not None
    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.result-processing.started",
    ]
    assert replay.event_id == events[2].event_id
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_distinct_statuses_produce_distinct_sequence_linked_events(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "planning")
    _write_run_json_locked(repo, run_dir, "failed", error="boom")

    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.snapshot.checkpointed",
        "run.planning.started",
        "run.snapshot.checkpointed",
        "run.failed",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6]
    assert events[0].previous_digest is None
    assert events[1].previous_digest == events[0].event_digest
    assert events[5].previous_digest == events[4].event_digest
    failed = [e for e in events if e.event_type == "run.failed"][0]
    assert failed.payload == {"status": "failed", "detail": "failed"}
    assert "boom" not in _journal_path(run_dir).read_text()


def test_terminal_status_transitions_produce_allowlisted_events(enabled, tmp_path):
    repo = _repo(tmp_path)
    cases = [
        ("failed", "run.failed"),
        ("canceled", "run.interrupted"),
        ("timeout", "run.failed"),
    ]
    for index, (status, expected_type) in enumerate(cases):
        run_dir = _run_dir(repo, f"20260728-16000{index}-beef{index:04d}")

        _write_run_json(run_dir, "started")
        _write_run_json_locked(repo, run_dir, "started")
        _write_run_json_locked(repo, run_dir, status, error=f"boom {status}")

        status_events = _status_events(run_dir)
        assert [e.event_type for e in status_events] == ["run.created", expected_type]
        tail = status_events[1]
        assert tail.payload == {"status": status, "detail": status}
        allowed = run_events.EVENT_TYPES[expected_type]
        assert set(tail.payload.keys()) <= allowed
        assert f"boom {status}" not in _journal_path(run_dir).read_text()


def test_mapped_dry_run_and_artifact_collection_append_status_events(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dry-run")
    _write_run_json_locked(repo, run_dir, "artifact-collection")

    assert (run_dir / "run.json").is_file()
    # dry-run and artifact-collection are now mapped: each appends its status
    # event, and each distinct run.json snapshot still publishes a checkpoint.
    assert [e.event_type for e in _status_events(run_dir)] == [
        "run.created",
        "run.completed",
        "run.artifact_collection.started",
    ]
    assert len(_checkpoint_events(run_dir)) == 3


def test_recovery_writer_does_not_append(enabled, tmp_path):
    """The stale-lock recovery writer uses localio directly and must not append."""
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    payload = {"schema": "brigade.run.v1", "status": "failed", "error": "stale owner"}
    localio.write_json(run_dir / "run.json", payload)

    assert (run_dir / "run.json").is_file()
    assert not (run_dir / "events").exists()


def test_raw_oserror_is_bounded_and_blocks_run_json(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    assert _journal_path(run_dir).is_file()

    before = (run_dir / "run.json").read_bytes()

    def raise_oserror(path: Path) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(run_journal, "ensure_journal", raise_oserror)

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError) as excinfo:
            _write_run_json(run_dir, "planning")

    # The raw exception text could carry a path or private value, so only a
    # generic bounded category surfaces.
    assert "simulated disk failure" not in str(excinfo.value)
    assert (run_dir / "run.json").read_bytes() == before
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created"]


def test_canonicalization_error_is_bounded_and_blocks_run_json(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    assert _journal_path(run_dir).is_file()

    before = (run_dir / "run.json").read_bytes()

    def raise_canon(*args: object, **kwargs: object) -> dict:
        raise run_events.CanonicalizationError("simulated canonicalization failure")

    monkeypatch.setattr(run_events, "build_event", raise_canon)

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError) as excinfo:
            _write_run_json(run_dir, "planning")

    assert "simulated canonicalization failure" not in str(excinfo.value)
    assert (run_dir / "run.json").read_bytes() == before
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created"]


def test_journal_corruption_blocks_run_json_advance(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    assert _journal_path(run_dir).is_file()

    _journal_path(run_dir).write_text("not-a-json-line\n")
    before = (run_dir / "run.json").read_bytes()

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json(run_dir, "planning")

    assert (run_dir / "run.json").read_bytes() == before
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "started"


# -- Issue #568 slice 5 Task 2: prepare_lifecycle_journal / write_checkpoint --------


def test_prepare_lifecycle_journal_private_journal_under_matching_held_run_lock(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    # Pre-lock bootstrap records the durable request without creating the journal.
    _write_run_json(run_dir, "started")
    assert not _journal_path(run_dir).exists()

    with runguard.run_lock(repo, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=repo, incoming_snapshot=None)

    journal = _journal_path(run_dir)
    assert journal.is_file()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700
    # prepare only activates; it appends no event.
    assert _events(run_dir) == []


def test_prepare_lifecycle_journal_noop_when_journal_exists(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    with runguard.run_lock(repo, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=repo)
    journal_before = _journal_path(run_dir).read_bytes()
    mtime_before = _journal_path(run_dir).stat().st_mtime_ns

    with runguard.run_lock(repo, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=repo)

    assert _journal_path(run_dir).read_bytes() == journal_before
    assert _journal_path(run_dir).stat().st_mtime_ns == mtime_before


def test_prepare_lifecycle_journal_bounded_io_error(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")

    def raise_oserror(path: Path) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(run_journal, "ensure_journal", raise_oserror)

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError) as excinfo:
            run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=repo)
    assert "simulated disk failure" not in str(excinfo.value)
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_record_lifecycle_transition_requires_existing_journal(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")  # request only, no journal

    # Journaling was durably requested, the matching lock is held, so
    # prepare_lifecycle_journal should have activated the journal. Its absence
    # after preparation is a broken flow: record_lifecycle_transition must
    # fail closed with a bounded LifecycleJournalError rather than silently
    # returning None and letting run.json advance past an unowned transition.
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError) as excinfo:
            run_lifecycle.record_lifecycle_transition(run_dir, status="planning", workspace=repo)
    assert len(str(excinfo.value)) <= run_events.MAX_DIAGNOSTIC_LEN
    assert not _journal_path(run_dir).exists()


def test_first_activated_mapped_write_checkpoint_seq1_then_status_seq2(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.snapshot.checkpointed", "run.created"]
    assert [e.sequence for e in events] == [1, 2]
    assert events[0].payload["paired_event_type"] == "run.created"
    assert events[1].previous_digest == events[0].event_digest


def test_mapped_artifact_collection_activated_write_appends_status_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # activate + run.created
    # A mapped artifact-collection status under the active journal: checkpoint
    # event appends (paired run.artifact_collection.started) and the status
    # event appends.
    _write_run_json_locked(repo, run_dir, "artifact-collection")

    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.snapshot.checkpointed",
        "run.artifact_collection.started",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    assert events[2].payload["paired_event_type"] == "run.artifact_collection.started"
    assert [e.event_type for e in _status_events(run_dir)] == [
        "run.created",
        "run.artifact_collection.started",
    ]


# -- Issue #568 slice 6 Task 2: STATUS_EVENT_TYPE rows for dry-run, incomplete,
#    artifact-collection (previously unmapped, now mapped). --


def test_status_event_type_maps_dry_run_incomplete_artifact_collection():
    assert run_lifecycle.STATUS_EVENT_TYPE.get("dry-run") == "run.completed"
    assert run_lifecycle.STATUS_EVENT_TYPE.get("incomplete") == "run.failed"
    assert run_lifecycle.STATUS_EVENT_TYPE.get("artifact-collection") == "run.artifact_collection.started"


def test_dry_run_status_appends_run_completed(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dry-run")

    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == ["run.created", "run.completed"]
    completed = [e for e in status_events if e.event_type == "run.completed"][0]
    assert completed.payload == {"status": "dry-run", "detail": "dry-run"}


def test_incomplete_status_appends_run_failed(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "incomplete", error="partial work")

    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == ["run.created", "run.failed"]
    failed = [e for e in status_events if e.event_type == "run.failed"][0]
    assert failed.payload == {"status": "incomplete", "detail": "incomplete"}
    assert "partial work" not in _journal_path(run_dir).read_text()


def test_artifact_collection_status_appends_run_artifact_collection_started(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "artifact-collection")

    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == [
        "run.created",
        "run.artifact_collection.started",
    ]
    started = [e for e in status_events if e.event_type == "run.artifact_collection.started"][0]
    # run.artifact_collection.started allowlist is {"detail"} only (no status).
    assert started.payload == {"detail": "artifact-collection"}


def test_combined_no_longer_skip_chain_appends_all_three_status_events(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = ["started", "dry-run", "started", "incomplete", "started", "artifact-collection"]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == [
        "run.created",
        "run.completed",
        "run.created",
        "run.failed",
        "run.created",
        "run.artifact_collection.started",
    ]


def test_checkpoint_failure_leaves_journal_and_run_json_unchanged(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # activate
    run_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()

    def raise_checkpoint(run_dir_arg, run_json_bytes):
        raise run_checkpoint.CheckpointError("simulated publish failure", category="link")

    monkeypatch.setattr(run_checkpoint, "publish_checkpoint_file", raise_checkpoint)

    # CheckpointError fails BEFORE the lifecycle status append and BEFORE
    # run.json replacement: neither the journal nor run.json advances.
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
            _write_run_json(run_dir, "planning")
    assert excinfo.value.category == "link"
    assert (run_dir / "run.json").read_bytes() == run_before
    assert _journal_path(run_dir).read_bytes() == journal_before
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created"]


# -- Issue #568 slice 5 Task 2 sendback: CheckpointError -> RetainRunLockError ----


_RECEIPT_UPDATE_HELPERS = [
    "record_artifact_collection",
    "record_run_termination",
    "record_dispatch_stage",
    "record_result_processing",
]


def _call_receipt_update_helper(helper_name: str, output_dir: Path) -> None:
    if helper_name == "record_artifact_collection":
        aboyeur.record_artifact_collection(
            output_dir,
            status="failed",
            failure_phase="artifact-validation",
            failure_kind="invalid-patch",
            detail="changes.patch failed validation",
        )
    elif helper_name == "record_run_termination":
        aboyeur.record_run_termination(
            output_dir,
            status="failed",
            failure_phase="dispatch",
            failure_kind="unexpected-error",
            detail="provider failed",
            seat="coder",
        )
    elif helper_name == "record_dispatch_stage":
        aboyeur.record_dispatch_stage(output_dir, stage=1, seats=("coder",))
    elif helper_name == "record_result_processing":
        aboyeur.record_result_processing(output_dir, seat="coder")


@pytest.mark.parametrize("helper_name", _RECEIPT_UPDATE_HELPERS)
def test_receipt_update_helper_translates_checkpoint_error_to_retain_run_lock(
    enabled, tmp_path, monkeypatch, helper_name
):
    """A CheckpointError during a terminal/phase receipt-update write inside a held
    runguard.run_lock must surface as a bounded RetainRunLockError (never a raw
    CheckpointError) and must retain the matching run lock, so a crash mid
    receipt-update keeps the lock held for stale-lock recovery and the bounded
    category diagnostic is all that escapes the public helper.
    """
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started", lock_workspace=repo)
    _write_run_json_locked(repo, run_dir, "started", lock_workspace=repo)  # activate journal
    run_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()

    def raise_checkpoint(run_dir_arg: Path, run_json_bytes: bytes) -> Path:
        raise run_checkpoint.CheckpointError("simulated publish failure", category="link")

    monkeypatch.setattr(run_checkpoint, "publish_checkpoint_file", raise_checkpoint)

    lock_path = runguard.lock_path(repo)
    assert not lock_path.exists()

    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        with runguard.run_lock(repo, run_dir=run_dir):
            _call_receipt_update_helper(helper_name, run_dir)

    # The public helper raises a bounded RetainRunLockError, never the raw
    # CheckpointError type. The original bounded CheckpointError diagnostic is
    # chained as the cause (preserved chaining behavior, matching the existing
    # OSError / LifecycleJournalError translation at the same boundary).
    assert not isinstance(excinfo.value, run_checkpoint.CheckpointError)
    assert isinstance(excinfo.value.__cause__, run_checkpoint.CheckpointError)
    assert excinfo.value.__cause__.category == "link"
    # The matching run lock is retained for stale-lock recovery.
    assert lock_path.is_dir()
    # Neither the journal nor run.json advanced past the failure.
    assert (run_dir / "run.json").read_bytes() == run_before
    assert _journal_path(run_dir).read_bytes() == journal_before
    assert [e.event_type for e in _status_events(run_dir)] == ["run.created"]


# -- Issue #568 slice 6, Task 7: journal-authority enrollment and write path -----

from brigade import run_shadow  # noqa: E402

_AUTHORITY_FIELD = "run_journal_authority_requested"


def _apply_authority_request(run_dir: Path, payload: dict[str, object]) -> dict[str, object]:
    run_json = run_dir / "run.json"
    if run_json.is_file():
        existing = json.loads(run_json.read_text())
        if existing.get(_AUTHORITY_FIELD) is True:
            payload[_AUTHORITY_FIELD] = True
        if existing.get(_REQUEST_FIELD) is True:
            payload[_REQUEST_FIELD] = True
    elif run_lifecycle.is_lifecycle_journaling_enabled():
        payload[_REQUEST_FIELD] = True
    return payload


def _write_run_json_authority(run_dir: Path, status: str, **kwargs) -> None:
    payload = _apply_authority_request(run_dir, _run_payload(status, **kwargs))
    aboyeur._write_json(run_dir / "run.json", payload)


def test_authority_enrollment_persists_run_journal_authority_requested(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["run_journal_authority_requested"] is True
    assert meta["lifecycle_journal_requested"] is True
    assert not _journal_path(run_dir).exists()


def test_not_yet_authoritative_run_writes_legacy_body_when_gate_not_ready(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    # Authority-requested projection uses the POST-parity readiness report
    # (finding 2): a ready first comparison projects that same write, and a
    # not-ready post-parity gate falls back to the legacy body. Force a
    # genuine not-ready post-parity gate so the fallback path is exercised
    # without relying on the old pre-parity decision.
    monkeypatch.setattr(
        run_shadow,
        "check_projection_readiness",
        lambda run_dir: run_shadow.ReadinessReport(ready=False, reasons=(run_shadow.REASON_MISMATCH_RECORDED,)),
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert "projector_version" not in meta
    assert meta["status"] == "planning"


def test_first_write_match_authorizes_first_projected_snapshot(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
        _write_run_json_authority(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["projector_version"] == run_projector.PROJECTOR_VERSION
    assert meta["journal_present"] is True
    assert meta["status"] == "planning"


def test_authoritative_run_fail_closed_on_projection_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
        _write_run_json_authority(run_dir, "planning")
    meta_before = (run_dir / "run.json").read_bytes()
    from brigade import run_projector

    monkeypatch.setattr(
        run_projector,
        "project_run_snapshot",
        lambda *_a, **_kw: (_ for _ in ()).throw(run_projector.EventPayloadError("forged")),
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before


def test_existing_lifecycle_only_run_remains_non_authoritative(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    existing = _run_payload("started", lock_workspace=repo)
    existing[_REQUEST_FIELD] = True
    localio.write_json(run_dir / "run.json", existing)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert "run_journal_authority_requested" not in meta
    assert "projector_version" not in meta


def _enroll_and_authorize(repo, run_dir, monkeypatch):
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
        _write_run_json_authority(run_dir, "planning")
    return json.loads((run_dir / "run.json").read_text())


def test_unpaired_journal_ahead_fails_closed_before_status_append(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    with runguard.run_lock(repo, run_dir=run_dir):
        tail = run_journal.read_journal(_journal_path(run_dir)).events[-1].sequence
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=run_dir.name,
            event_type="run.completed",
            payload={"status": "ok", "detail": "ok"},
            idempotency_key="complete-1",
            expected_previous_sequence=tail,
            recorded_at="2026-07-27T15:31:46.000000Z",
        )
    run_json_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="journal-ahead"):
            _write_run_json_authority(run_dir, "ok")
    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_record_approval_pause_commits_events_snapshot_and_releases_lock(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    approval_reference = {
        "approval_id": "approval-1",
        "source": "daily",
        "fingerprint": "approval-fingerprint-1",
        "source_fingerprint": "source-fingerprint-1",
        "contract_fingerprint": "contract-fingerprint-1",
        "evidence_fingerprint": "evidence-fingerprint-1",
    }

    with runguard.run_lock(repo, run_dir=run_dir):
        aboyeur.record_approval_pause(run_dir, approval_reference)
        assert runguard.is_active_run_owner(repo, run_dir)

    assert runguard.run_lock_state(repo, run_dir) == "absent"
    events = _events(run_dir)
    assert [event.event_type for event in events[-4:]] == [
        run_checkpoint.CHECKPOINT_EVENT_TYPE,
        "approval.requested",
        run_checkpoint.CHECKPOINT_EVENT_TYPE,
        "run.paused",
    ]
    _, requested, _, paused = events[-4:]
    assert requested.payload == {
        "approval_id": "approval-1",
        "source": "daily",
        "contract_fingerprint": "contract-fingerprint-1",
    }
    assert paused.payload == {
        "approval_id": "approval-1",
        "reason": "approval-required",
    }
    snapshot = json.loads((run_dir / "run.json").read_text())
    assert snapshot["status"] == "running"
    assert snapshot["approval_reference"] == {
        **approval_reference,
        "decision_state": "pending",
    }
    assert snapshot["journal_last_sequence"] == paused.sequence
    assert snapshot["journal_last_event_digest"] == paused.event_digest


def test_status_neutral_approval_event_is_checkpoint_paired_and_recoverable(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)

    with runguard.run_lock(repo, run_dir=run_dir):
        requested = run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="approval.requested",
            payload={
                "approval_id": "approval-1",
                "source": "daily",
                "contract_fingerprint": "contract-fingerprint-1",
            },
            idempotency_key="approval:requested:test",
            workspace=repo,
        )

    events = _events(run_dir)
    checkpoint = events[-2]
    assert checkpoint.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE
    assert checkpoint.payload["paired_event_type"] == "approval.requested"
    assert requested == events[-1]
    assert run_shadow.check_projection_readiness(run_dir).ready is True

    (run_dir / "run.json").unlink()
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert repaired["status"] == "planning"
    assert repaired["journal_last_sequence"] == requested.sequence
    assert repaired["journal_last_event_digest"] == requested.event_digest


def test_status_neutral_event_replay_and_conflict_do_not_append_checkpoint(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    payload = {
        "approval_id": "approval-1",
        "source": "daily",
        "contract_fingerprint": "contract-fingerprint-1",
    }

    with runguard.run_lock(repo, run_dir=run_dir):
        recorded = run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="approval.requested",
            payload=payload,
            idempotency_key="approval:requested:stable",
            workspace=repo,
        )
        _write_run_json_authority(run_dir, "dispatching", lock_workspace=repo)
    journal_path = _journal_path(run_dir)
    journal_before = journal_path.read_bytes()
    checkpoint_count = len(_checkpoint_events(run_dir))

    with runguard.run_lock(repo, run_dir=run_dir):
        replayed = run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="approval.requested",
            payload=payload,
            idempotency_key="approval:requested:stable",
            workspace=repo,
        )
    assert replayed == recorded
    assert journal_path.read_bytes() == journal_before
    assert len(_checkpoint_events(run_dir)) == checkpoint_count

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="idempotency key"):
            run_lifecycle.record_lifecycle_event(
                run_dir,
                event_type="approval.requested",
                payload={**payload, "source": "tool"},
                idempotency_key="approval:requested:stable",
                workspace=repo,
            )
    assert journal_path.read_bytes() == journal_before
    assert len(_checkpoint_events(run_dir)) == checkpoint_count


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("approval.forged", {"approval_id": "approval-1"}),
        (
            "approval.rejected",
            {
                "approval_id": "approval-1",
                "decided_at": "2026-07-30T18:00:00+00:00",
                "decision_state": "approved",
            },
        ),
    ],
)
def test_invalid_approval_event_request_has_zero_journal_or_checkpoint_mutation(
    tmp_path,
    monkeypatch,
    event_type,
    payload,
):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    journal_path = _journal_path(run_dir)
    journal_before = journal_path.read_bytes()
    checkpoint_count = len(_checkpoint_events(run_dir))

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="canonicalization"):
            run_lifecycle.record_lifecycle_event(
                run_dir,
                event_type=event_type,
                payload=payload,
                idempotency_key="approval:invalid:test",
                workspace=repo,
            )

    assert journal_path.read_bytes() == journal_before
    assert len(_checkpoint_events(run_dir)) == checkpoint_count


def test_approval_reference_rejects_unknown_decision_state():
    reference = {
        "approval_id": "approval-1",
        "source": "daily",
        "fingerprint": "approval-fingerprint-1",
        "source_fingerprint": "source-fingerprint-1",
        "contract_fingerprint": "contract-fingerprint-1",
        "evidence_fingerprint": "evidence-fingerprint-1",
        "decision_state": "garbage",
    }

    with pytest.raises(run_lifecycle.LifecycleJournalError, match="invalid decision_state"):
        run_lifecycle.normalize_approval_reference(reference)


def test_approval_idempotency_key_is_bounded_and_reference_only():
    approval_id = "private-approval-id-" + ("x" * 500)

    key = run_lifecycle.approval_idempotency_key(
        approval_id,
        "consumed",
        scope="run-approval-1",
    )

    assert len(key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN
    assert approval_id not in key
    assert key == run_lifecycle.approval_idempotency_key(
        approval_id,
        "consumed",
        scope="run-approval-1",
    )
    assert key != run_lifecycle.approval_idempotency_key(
        approval_id,
        "consumed",
        scope="run-approval-2",
    )


def test_current_version_mismatch_fail_closed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["mismatches"] = 1
    data["last_outcome"] = "mismatch"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    meta_before = (run_dir / "run.json").read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before


def test_current_version_error_fail_closed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["errors"] = 1
    data["last_outcome"] = "error"
    data["last_error_category"] = "journal-unreadable"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    meta_before = (run_dir / "run.json").read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before


def test_bounded_journal_failure_after_authority_fail_closed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    journal = _journal_path(run_dir)
    journal.write_bytes(journal.read_bytes() + b"x" * (run_checkpoint.MAX_JOURNAL_BYTES + 1))
    meta_before = (run_dir / "run.json").read_bytes()
    journal_before = journal.read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before
    assert journal.read_bytes() == journal_before


def test_authority_fail_closed_on_incomplete_projection_metadata(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    del meta["journal_last_event_digest"]
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authority_fail_closed_on_saved_sequence_not_matching_verified_prefix(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["journal_last_sequence"] = meta["journal_last_sequence"] + 99
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authority_fail_closed_on_saved_digest_not_matching_verified_prefix(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["journal_last_event_digest"] = "b" * 64
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authoritative_write_order_checkpoint_lifecycle_parity_readiness_replace(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
    calls: list[str] = []
    real_checkpoint = run_checkpoint.write_checkpoint
    real_transition = run_lifecycle.record_lifecycle_transition
    real_shadow = run_shadow.record_shadow_comparison
    real_readiness = run_shadow.check_projection_readiness
    real_atomic = localio.write_text_atomic

    def checkpoint_spy(*a, **kw):
        calls.append("checkpoint")
        return real_checkpoint(*a, **kw)

    def transition_spy(*a, **kw):
        calls.append("lifecycle")
        return real_transition(*a, **kw)

    def shadow_spy(*a, **kw):
        calls.append("parity")
        return real_shadow(*a, **kw)

    def readiness_spy(*a, **kw):
        calls.append("readiness")
        return real_readiness(*a, **kw)

    def atomic_spy(path, payload, **kw):
        # Only the run.json atomic replace is the observable "replace" step;
        # record_shadow_comparison also writes the shadow artifact through
        # write_text_atomic, which is internal to parity and not part of the
        # authority write order.
        if Path(path).name == "run.json":
            calls.append("replace")
        return real_atomic(path, payload, **kw)

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", checkpoint_spy)
    monkeypatch.setattr(run_lifecycle, "record_lifecycle_transition", transition_spy)
    monkeypatch.setattr(run_shadow, "record_shadow_comparison", shadow_spy)
    monkeypatch.setattr(run_shadow, "check_projection_readiness", readiness_spy)
    monkeypatch.setattr(localio, "write_text_atomic", atomic_spy)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "planning")
    # The first "started" write already projects (finding 2: a ready first
    # post-parity comparison projects that same write), so this "planning"
    # write is genuinely authoritative. The prior authority gate fires a
    # readiness check BEFORE the checkpoint/lifecycle append (finding 1:
    # fail closed before append), then the observable five-step order runs:
    # checkpoint, lifecycle, parity, post-parity readiness, replace.
    assert calls == [
        "readiness",
        "checkpoint",
        "lifecycle",
        "parity",
        "readiness",
        "replace",
    ]


def test_first_authority_write_checkpoint_seq1_then_status_seq2(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
    events = run_journal.read_journal(_journal_path(run_dir)).events
    assert events[0].event_type == "run.snapshot.checkpointed"
    assert events[0].sequence == 1
    assert events[0].payload.get("body_kind") == "base-stripped"
    assert events[1].event_type == "run.created"
    assert events[1].sequence == 2


def test_direct_write_json_failure_raises_lifecycle_journal_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    from brigade import run_projector

    monkeypatch.setattr(
        run_projector,
        "project_run_snapshot",
        lambda *_a, **_kw: (_ for _ in ()).throw(run_projector.EventPayloadError("forged")),
    )
    payload = _apply_authority_request(run_dir, _run_payload("dispatching"))
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            aboyeur._write_json(run_dir / "run.json", payload)


# -- Issue #568 slice 6, Task 7 corrections: review findings 1-4 ---------------


def _forge_shadow_artifact(run_dir: Path, **overrides) -> None:
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data.update(overrides)
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _assert_authoritative_prior_gate_forged_evidence_fails_closed(run_dir, repo, monkeypatch, *, forge_kwargs):
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    run_json_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    events_before = [e.sequence for e in _events(run_dir)]
    _forge_shadow_artifact(run_dir, **forge_kwargs)
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    # Forged evidence fails closed BEFORE the checkpoint/lifecycle append: no
    # new journal event is appended and run.json is not replaced.
    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert _journal_path(run_dir).read_bytes() == journal_before
    assert [e.sequence for e in _events(run_dir)] == events_before


def test_authoritative_prior_gate_forged_artifact_schema_fails_closed_before_append(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _assert_authoritative_prior_gate_forged_evidence_fails_closed(
        run_dir, repo, monkeypatch, forge_kwargs={"schema": "brigade.forged.v1"}
    )


def test_authoritative_prior_gate_forged_artifact_run_id_fails_closed_before_append(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _assert_authoritative_prior_gate_forged_evidence_fails_closed(
        run_dir, repo, monkeypatch, forge_kwargs={"run_id": "forged-run-id"}
    )


def test_authoritative_prior_gate_forged_artifact_last_digest_fails_closed_before_append(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    # Forged last_compared_event_digest at the correct sequence: the old
    # _authority_prior_decision checked tail.sequence but not the digest, so
    # this forgery authorized projection. check_projection_readiness reports
    # REASON_JOURNAL_AHEAD, but the cursor does not verify against the journal
    # event at that sequence, so the journal-ahead exception does not apply
    # and the gate fails closed.
    _assert_authoritative_prior_gate_forged_evidence_fails_closed(
        run_dir, repo, monkeypatch, forge_kwargs={"last_compared_event_digest": "f" * 64}
    )


def test_authoritative_prior_gate_forged_artifact_journal_cursor_fails_closed_before_append(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    # Forged last_compared_sequence beyond the journal tail: REASON_JOURNAL_AHEAD
    # is reported, but the cursor is out of range so the journal-ahead
    # exception does not apply and the gate fails closed.
    _assert_authoritative_prior_gate_forged_evidence_fails_closed(
        run_dir, repo, monkeypatch, forge_kwargs={"last_compared_sequence": 999}
    )


def test_authoritative_prior_gate_malformed_last_outcome_fails_closed_before_append(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _assert_authoritative_prior_gate_forged_evidence_fails_closed(
        run_dir, repo, monkeypatch, forge_kwargs={"last_outcome": "bogus"}
    )


def test_authoritative_prior_gate_malformed_last_outcome_none_fails_closed_before_append(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _assert_authoritative_prior_gate_forged_evidence_fails_closed(
        run_dir, repo, monkeypatch, forge_kwargs={"last_outcome": None}
    )


def test_first_authority_started_write_projects_on_first_write(tmp_path, monkeypatch):
    # Finding 2: authority-requested first-write authorization uses the
    # post-parity readiness report. A ready first comparison projects that
    # same write, so the single first "started" write after enrollment already
    # carries the current projector version, journal_present true, and the journal has
    # checkpoint seq1 + run.created seq2.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["projector_version"] == run_projector.PROJECTOR_VERSION
    assert meta["journal_present"] is True
    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
    ]
    assert [e.sequence for e in events] == [1, 2]


def test_authority_requested_post_parity_not_ready_falls_back_to_legacy(tmp_path, monkeypatch):
    # Finding 2: a requested run whose post-parity gate is not ready still
    # falls back to legacy (never fail-closed).
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="authority run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    monkeypatch.setattr(
        run_shadow,
        "check_projection_readiness",
        lambda run_dir: run_shadow.ReadinessReport(ready=False, reasons=(run_shadow.REASON_NO_COMPARISONS,)),
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert "projector_version" not in meta
    assert meta["status"] == "planning"


def test_legacy_run_does_not_call_prior_authority_gate(tmp_path, monkeypatch):
    # Finding 3: legacy runs skip prior-decision work entirely; the prior
    # authority gate (check_projection_readiness) is never called and legacy
    # bytes/order are preserved.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    localio.write_json(run_dir / "run.json", _run_payload("started", lock_workspace=repo))

    def raise_if_called(run_dir):
        raise AssertionError("check_projection_readiness must not be called for legacy runs")

    monkeypatch.setattr(run_shadow, "check_projection_readiness", raise_if_called)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert "run_journal_authority_requested" not in meta
    assert "projector_version" not in meta
    assert meta["status"] == "planning"


def test_authoritative_prior_ready_post_parity_not_ready_fails_closed(tmp_path, monkeypatch):
    # Finding 4: for an authoritative normal write whose prior gate was ready,
    # require the post-parity readiness report to remain ready before
    # projecting. Prior ready + post-parity not ready -> fail closed, no
    # replace.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    run_json_before = (run_dir / "run.json").read_bytes()
    # Counter-based stub: the prior gate (1st call) returns ready so the write
    # proceeds to checkpoint/lifecycle/parity; the post-parity gate (2nd call)
    # returns not-ready so the authoritative normal write fails closed.
    calls = {"n": 0}

    def two_phase(run_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            return run_shadow.ReadinessReport(ready=True, reasons=())
        return run_shadow.ReadinessReport(ready=False, reasons=(run_shadow.REASON_MISMATCH_RECORDED,))

    monkeypatch.setattr(run_shadow, "check_projection_readiness", two_phase)
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == run_json_before


def test_authoritative_prior_status_changing_pair_catch_up_fails_closed(tmp_path, monkeypatch):
    # A structurally covered checkpoint/status pair is still unsafe to catch
    # up against the persisted pre-write snapshot when the event changes the
    # projected status. The catch-up records a mismatch and fails before the
    # next status pair is appended.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    with runguard.run_lock(repo, run_dir=run_dir):
        snapshot = (run_dir / "run.json").read_bytes()
        run_checkpoint.write_checkpoint(
            run_dir,
            snapshot,
            workspace=repo,
            paired_event_type="run.completed",
            body_kind=run_checkpoint._BODY_KIND_BASE_STRIPPED,
        )
        tail = run_journal.read_journal(_journal_path(run_dir)).events[-1].sequence
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=run_dir.name,
            event_type="run.completed",
            payload={"status": "ok", "detail": "ok"},
            idempotency_key="complete-1",
            expected_previous_sequence=tail,
            recorded_at="2026-07-27T15:31:46.000000Z",
        )
    run_json_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="catch-up not ready"):
            _write_run_json_authority(run_dir, "ok")
    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert _journal_path(run_dir).read_bytes() == journal_before


# -- Issue #568 slice 6, Task 7 final corrections: findings 1-3 regressions -----


def test_authority_fail_closed_on_journal_present_false_with_projection_metadata(tmp_path, monkeypatch):
    # Finding 1: once any projection metadata exists, journal_present must be
    # exactly True. A false value is invalid authoritative metadata and must
    # raise a bounded LifecycleJournalError BEFORE the checkpoint/lifecycle
    # append, with no run.json replace and no journal change.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["journal_present"] = False
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authority_fail_closed_on_journal_present_wrong_typed_with_projection_metadata(tmp_path, monkeypatch):
    # Finding 1: journal_present must be exactly True (bool), not a truthy
    # string or int. A wrong-typed value is invalid authoritative metadata.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["journal_present"] = "true"
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authority_fail_closed_on_mixed_run_id_chain_valid_prefix(tmp_path, monkeypatch):
    # Finding 2: the bounded verified prefix must reject ANY event whose run_id
    # differs from run_dir.name, not only the event at the saved cursor. A
    # chain-valid journal that mixes in a forged-run-id event must fail closed
    # before append with no run.json replace and no journal change, even when
    # the saved cursor points at a legitimate same-run event.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    journal = _journal_path(run_dir)
    # Append a chain-valid event carrying a FOREIGN run_id directly to the
    # journal. The digest chain stays valid (run_id is part of the event
    # envelope, not the chain link), but the prefix is no longer exclusively
    # this run's.
    with runguard.run_lock(repo, run_dir=run_dir):
        tail = run_journal.read_journal(journal).events[-1].sequence
        run_journal.append_event(
            journal,
            run_id="foreign-run-id",
            event_type="run.completed",
            payload={"status": "ok", "detail": "ok"},
            idempotency_key="foreign-1",
            expected_previous_sequence=tail,
            recorded_at="2026-07-27T15:31:46.000000Z",
        )
    # Point the saved cursor at the last SAME-run event (the planning
    # transition) so the old cursor-only check would pass; the mixed prefix
    # must still be rejected.
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    same_run_events = [e for e in _events(run_dir) if e.run_id == run_dir.name]
    cursor_event = same_run_events[-1]
    meta["journal_last_sequence"] = cursor_event.sequence
    meta["journal_last_event_digest"] = cursor_event.event_digest
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = journal.read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert journal.read_bytes() == journal_before


def test_authoritative_pair_catch_up_with_current_mismatch_fails_closed(tmp_path, monkeypatch):
    # A valid one-pair lag is eligible for catch-up, but the catch-up must not
    # ignore an arbitrary parity mismatch.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    with runguard.run_lock(repo, run_dir=run_dir):
        _append_dispatch_pair_without_parity(
            run_dir,
            repo,
            event_type="run.dispatch.completed",
            seat="coder",
            attempt=1,
        )
    run_json_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    real_record_shadow = run_shadow.record_shadow_comparison

    def force_mismatch(run_dir_arg, legacy_snapshot):
        real_record_shadow(run_dir_arg, legacy_snapshot)
        artifact = run_shadow.shadow_artifact_path(run_dir_arg)
        data = json.loads(artifact.read_text())
        records = data.get("recent_records") or []
        if records and records[-1].get("outcome") == run_shadow.OUTCOME_MATCH:
            records[-1]["outcome"] = run_shadow.OUTCOME_MISMATCH
            records[-1]["differing_fields"] = ["forged"]
            data["matches"] = int(data.get("matches", 0) or 0) - 1
            data["mismatches"] = int(data.get("mismatches", 0) or 0) + 1
            data["last_outcome"] = run_shadow.OUTCOME_MISMATCH
            data["last_differing_fields"] = ["forged"]
            artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    monkeypatch.setattr(run_shadow, "record_shadow_comparison", force_mismatch)
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "result-processing")
    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert _journal_path(run_dir).read_bytes() == journal_before


# -- Task 7 review correction: post-parity reasons / strict counters / aggregate


def _forge_after_parity(run_dir_arg, legacy_snapshot, *, forge):
    """Run the real shadow comparison, then forge the artifact per ``forge``.

    Used by the one-pair catch-up regressions. The real comparison catches up
    the eligible dispatch pair, then the forge layer mutates only the aggregate
    fields the post-catch-up readiness check must reject.
    """
    real_record_shadow = run_shadow.record_shadow_comparison

    def _wrapper(run_dir_inner, legacy_inner):
        real_record_shadow(run_dir_inner, legacy_inner)
        artifact = run_shadow.shadow_artifact_path(run_dir_inner)
        data = json.loads(artifact.read_text())
        forge(data)
        artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    return _wrapper


def _enroll_authorize_and_advance(repo, run_dir, monkeypatch):
    """Enroll, authorize, then append one dispatch pair without parity."""
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    with runguard.run_lock(repo, run_dir=run_dir):
        _append_dispatch_pair_without_parity(
            run_dir,
            repo,
            event_type="run.dispatch.completed",
            seat="coder",
            attempt=1,
        )
    return (run_dir / "run.json").read_bytes()


def test_pair_catch_up_rejects_forged_extra_journal_unreadable_reason(tmp_path, monkeypatch):
    # A forged aggregate error after catch-up must fail closed.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    run_json_before = _enroll_authorize_and_advance(repo, run_dir, monkeypatch)

    def forge(data):
        data["last_error_category"] = "journal-unreadable"

    monkeypatch.setattr(
        run_shadow,
        "record_shadow_comparison",
        _forge_after_parity(run_dir, {}, forge=forge),
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "ok")
    assert (run_dir / "run.json").read_bytes() == run_json_before


def test_pair_catch_up_rejects_bool_mismatches_counter(tmp_path, monkeypatch):
    # Correction 2: ``mismatches`` must be a non-bool int equal to 0. A bool
    # ``False`` satisfies ``False == 0`` and must NOT be accepted.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    run_json_before = _enroll_authorize_and_advance(repo, run_dir, monkeypatch)

    def forge(data):
        data["mismatches"] = False

    monkeypatch.setattr(
        run_shadow,
        "record_shadow_comparison",
        _forge_after_parity(run_dir, {}, forge=forge),
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "ok")
    assert (run_dir / "run.json").read_bytes() == run_json_before


def test_pair_catch_up_rejects_bool_errors_counter(tmp_path, monkeypatch):
    # Correction 2: ``errors`` must be a non-bool int equal to 1. A bool
    # ``True`` satisfies ``True == 1`` and must NOT be accepted.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    run_json_before = _enroll_authorize_and_advance(repo, run_dir, monkeypatch)

    def forge(data):
        data["errors"] = True

    monkeypatch.setattr(
        run_shadow,
        "record_shadow_comparison",
        _forge_after_parity(run_dir, {}, forge=forge),
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "ok")
    assert (run_dir / "run.json").read_bytes() == run_json_before


def test_pair_catch_up_rejects_non_none_last_error_category(tmp_path, monkeypatch):
    # Correction 3: the final match aggregate's ``last_error_category`` must
    # be None. A forger sets it to "comparison-gap" (which is NOT
    # "journal-unreadable", so the post-parity reasons stay exactly
    # (REASON_ERROR_RECORDED,) and correction 1 does not catch it) while the
    # penultimate record's category remains "comparison-gap". The gate must
    # fail closed so an unrelated current aggregate error category cannot
    # piggyback on the journal-ahead exception.
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    run_json_before = _enroll_authorize_and_advance(repo, run_dir, monkeypatch)

    def forge(data):
        data["last_error_category"] = "comparison-gap"

    monkeypatch.setattr(
        run_shadow,
        "record_shadow_comparison",
        _forge_after_parity(run_dir, {}, forge=forge),
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "ok")
    assert (run_dir / "run.json").read_bytes() == run_json_before


@pytest.mark.parametrize("sabotage", ["remove", "rename", "symlink"])
def test_dispatch_fact_fails_closed_when_enrolled_journal_is_not_regular(
    tmp_path,
    monkeypatch,
    sabotage,
):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    _write_run_json(run_dir, "started", lock_workspace=repo)
    _write_run_json_locked(repo, run_dir, "dispatching", lock_workspace=repo)
    journal = _journal_path(run_dir)
    displaced = journal.with_name("lifecycle.displaced")
    if sabotage == "remove":
        journal.unlink()
    elif sabotage == "rename":
        journal.rename(displaced)
    else:
        journal.rename(displaced)
        journal.symlink_to(displaced.name)

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="journal"):
            run_lifecycle.record_dispatch_fact(
                run_dir,
                workspace=repo,
                event_type="run.dispatch.requested",
                seat="coder",
            )


def test_transport_does_not_invoke_after_enrolled_journal_disappears(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    _write_run_json(run_dir, "started", lock_workspace=repo)
    _write_run_json_locked(repo, run_dir, "dispatching", lock_workspace=repo)
    _journal_path(run_dir).unlink()
    external_calls: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
        external_calls.append(cli_ref)
        return agents.AgentResult(text="must not run", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    roster = roster_mod.Roster(
        orchestrator="chef",
        agents={
            "chef": roster_mod.Agent("chef", "codex", "plan"),
            "coder": roster_mod.Agent("coder", "codex", "code"),
        },
        max_workers=1,
    )

    def requested(agent):
        try:
            return run_lifecycle.record_dispatch_fact(
                run_dir,
                workspace=repo,
                event_type="run.dispatch.requested",
                seat=agent.name,
            )
        except run_lifecycle.LifecycleJournalError as exc:
            raise runguard.RetainRunLockError("dispatch fact unavailable") from exc

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(runguard.RetainRunLockError, match="dispatch fact unavailable"):
            run_transport.dispatch(
                [run_transport.Assignment(worker="coder", task="do work")],
                roster,
                build_prompt=lambda agent, assignment, **kwargs: assignment.task,
                run_appserver_worker=lambda *args, **kwargs: agents.AgentResult(text="", ok=False),
                event_writer=lambda *args, **kwargs: None,
                cwd=repo,
                on_dispatch_requested=requested,
            )

    assert external_calls == []


@pytest.mark.parametrize(
    ("failure_kind", "detail"),
    [
        ("keyboard-interrupt", "run canceled by user"),
        ("signal-15", "run terminated by SIGTERM"),
    ],
)
def test_dispatch_pair_is_adjacent_when_interrupt_writer_crosses_barrier(
    tmp_path,
    monkeypatch,
    failure_kind,
    detail,
):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir,
        task="barrier run",
        cwd=repo,
        roster=_minimal_roster(),
        read_only=False,
        lock_workspace=repo,
    )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json(run_dir, "dispatching", lock_workspace=repo)

    checkpoint_written = threading.Event()
    release_worker = threading.Event()
    interrupt_done = threading.Event()
    errors: list[BaseException] = []
    real_write_checkpoint = run_checkpoint.write_checkpoint

    def checkpoint_barrier(*args, **kwargs):
        event = real_write_checkpoint(*args, **kwargs)
        if kwargs.get("pairing_key") is not None:
            checkpoint_written.set()
            assert release_worker.wait(timeout=5)
        return event

    monkeypatch.setattr(run_checkpoint, "write_checkpoint", checkpoint_barrier)

    def worker_writer():
        try:
            run_lifecycle.record_dispatch_fact(
                run_dir,
                workspace=repo,
                event_type="run.dispatch.requested",
                seat="coder",
            )
        except BaseException as exc:
            errors.append(exc)

    def interrupt_writer():
        try:
            aboyeur.record_run_termination(
                run_dir,
                status="canceled",
                failure_phase="dispatch",
                failure_kind=failure_kind,
                detail=detail,
                seat="coder",
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            interrupt_done.set()

    with runguard.run_lock(repo, run_dir=run_dir):
        worker = threading.Thread(target=worker_writer)
        worker.start()
        assert checkpoint_written.wait(timeout=5)
        interrupt = threading.Thread(target=interrupt_writer)
        interrupt.start()
        assert not interrupt_done.wait(timeout=0.1)
        release_worker.set()
        worker.join(timeout=5)
        interrupt.join(timeout=5)

    assert not worker.is_alive()
    assert not interrupt.is_alive()
    assert errors == []
    events = _events(run_dir)
    dispatch_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE and event.payload.get("pairing_key")
    )
    assert events[dispatch_index + 1].event_type == "run.dispatch.requested"
    assert events[dispatch_index + 1].payload["seat"] == "coder"
    assert events[dispatch_index + 1].payload["attempt"] == 1
    (run_dir / "run.json").unlink()
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert repaired["status"] == "canceled"


@pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"), reason="pthread signal masks unavailable")
def test_checkpoint_event_pair_restores_exact_signal_mask_and_rejects_reentry(monkeypatch):
    previous_mask = {signal.SIGINT}
    calls: list[tuple[int, set[signal.Signals]]] = []

    def fake_sigmask(operation, mask):
        calls.append((operation, set(mask)))
        return previous_mask

    monkeypatch.setattr(signal, "pthread_sigmask", fake_sigmask)
    with run_lifecycle.checkpoint_event_pair():
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="reentry"):
            with run_lifecycle.checkpoint_event_pair():
                pass

    assert calls == [
        (signal.SIG_BLOCK, {signal.SIGTERM}),
        (signal.SIG_SETMASK, previous_mask),
    ]


def test_authoritative_dispatch_pairs_keep_shadow_ready_through_synthesis(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)

    with runguard.run_lock(repo, run_dir=run_dir):
        requested = run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.requested",
            seat="coder",
        )
        assert requested is not None
        attempt = requested.payload["attempt"]
        assert isinstance(attempt, int)
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.observed",
            seat="coder",
            attempt=attempt,
        )
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.completed",
            seat="coder",
            attempt=attempt,
        )
        _write_run_json_authority(run_dir, "result-processing")
        _write_run_json_authority(run_dir, "synthesizing")

    shadow = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert shadow["errors"] == 0
    assert shadow["mismatches"] == 0
    assert shadow["last_outcome"] == run_shadow.OUTCOME_MATCH
    readiness = run_shadow.check_projection_readiness(run_dir)
    assert readiness.ready is True
    assert readiness.reasons == ()


def test_authoritative_dispatch_recovers_one_pair_parity_crash_before_next_pair(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    real_shadow = run_shadow.record_shadow_comparison

    with runguard.run_lock(repo, run_dir=run_dir):
        monkeypatch.setattr(run_shadow, "record_shadow_comparison", lambda *args, **kwargs: None)
        requested = run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.requested",
            seat="coder",
        )
        assert requested is not None
        attempt = requested.payload["attempt"]
        assert isinstance(attempt, int)
        monkeypatch.setattr(run_shadow, "record_shadow_comparison", real_shadow)
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.observed",
            seat="coder",
            attempt=attempt,
        )

    shadow = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert shadow["errors"] == 0
    assert shadow["mismatches"] == 0
    assert shadow["last_outcome"] == run_shadow.OUTCOME_MATCH
    assert run_shadow.check_projection_readiness(run_dir).ready is True


@pytest.mark.parametrize(
    "terminal_event_type",
    ["run.dispatch.completed", "run.dispatch.failed"],
)
def test_authoritative_status_write_catches_up_final_dispatch_pair_before_append(
    tmp_path,
    monkeypatch,
    terminal_event_type,
):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    real_shadow = run_shadow.record_shadow_comparison

    with runguard.run_lock(repo, run_dir=run_dir):
        requested = run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.requested",
            seat="coder",
        )
        assert requested is not None
        attempt = requested.payload["attempt"]
        assert isinstance(attempt, int)
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.observed",
            seat="coder",
            attempt=attempt,
        )
        monkeypatch.setattr(run_shadow, "record_shadow_comparison", lambda *args, **kwargs: None)
        run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type=terminal_event_type,
            seat="coder",
            attempt=attempt,
        )
        monkeypatch.setattr(run_shadow, "record_shadow_comparison", real_shadow)

        _write_run_json_authority(run_dir, "result-processing")
        _write_run_json_authority(run_dir, "synthesizing")

    events = _events(run_dir)
    tail = events[-1]
    shadow = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    receipt = json.loads((run_dir / "run.json").read_text())
    assert shadow["errors"] == 0
    assert shadow["mismatches"] == 0
    assert shadow["last_outcome"] == run_shadow.OUTCOME_MATCH
    assert shadow["last_compared_sequence"] == tail.sequence
    assert shadow["last_compared_event_digest"] == tail.event_digest
    assert receipt["journal_last_sequence"] == tail.sequence
    assert receipt["journal_last_event_digest"] == tail.event_digest
    assert run_shadow.check_projection_readiness(run_dir).ready is True


def _append_dispatch_pair_without_parity(
    run_dir: Path,
    repo: Path,
    *,
    event_type: str,
    seat: str,
    attempt: int,
    include_pairing_key: bool = True,
) -> None:
    snapshot = (run_dir / "run.json").read_bytes()
    pairing_key = run_checkpoint.dispatch_pairing_key(event_type, seat, attempt)
    run_checkpoint.write_checkpoint(
        run_dir,
        snapshot,
        workspace=repo,
        paired_event_type=event_type,
        body_kind=run_checkpoint._BODY_KIND_BASE_STRIPPED,
        pairing_key=pairing_key if include_pairing_key else None,
    )
    report = run_journal.read_journal(_journal_path(run_dir))
    run_journal.append_event(
        _journal_path(run_dir),
        run_id=run_dir.name,
        event_type=event_type,
        payload={"seat": seat, "attempt": attempt, "detail": event_type.rsplit(".", 1)[-1]},
        idempotency_key=f"test-uncompared:{event_type}:{seat}:{attempt}",
        expected_previous_sequence=report.events[-1].sequence,
    )


@pytest.mark.parametrize("gap_kind", ["two-pair", "forged-cursor", "missing-pairing-key"])
def test_authoritative_status_write_rejects_uncovered_or_forged_dispatch_gap(
    tmp_path,
    monkeypatch,
    gap_kind,
):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)

    with runguard.run_lock(repo, run_dir=run_dir):
        _append_dispatch_pair_without_parity(
            run_dir,
            repo,
            event_type="run.dispatch.requested",
            seat="coder",
            attempt=1,
            include_pairing_key=gap_kind != "missing-pairing-key",
        )
        if gap_kind == "two-pair":
            _append_dispatch_pair_without_parity(
                run_dir,
                repo,
                event_type="run.dispatch.observed",
                seat="coder",
                attempt=1,
            )
        elif gap_kind == "forged-cursor":
            artifact_path = run_shadow.shadow_artifact_path(run_dir)
            artifact = json.loads(artifact_path.read_text())
            artifact["last_compared_event_digest"] = "f" * 64
            artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")

        run_json_before = (run_dir / "run.json").read_bytes()
        journal_before = _journal_path(run_dir).read_bytes()
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "result-processing")

    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authority_dispatch_checkpoint_is_base_stripped_and_recovers_exact_tail(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)

    with runguard.run_lock(repo, run_dir=run_dir):
        requested = run_lifecycle.record_dispatch_fact(
            run_dir,
            workspace=repo,
            event_type="run.dispatch.requested",
            seat="coder",
        )
    assert requested is not None
    events = _events(run_dir)
    dispatch_checkpoint = events[-2]
    assert dispatch_checkpoint.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE
    assert dispatch_checkpoint.payload["body_kind"] == "base-stripped"
    assert dispatch_checkpoint.payload["pairing_key"]

    (run_dir / "run.json").unlink()
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)
    events = _events(run_dir)
    assert repaired["journal_last_sequence"] == events[-1].sequence
    assert repaired["journal_last_event_digest"] == events[-1].event_digest
    assert repaired["projector_version"] == run_projector.PROJECTOR_VERSION


# -- Issue #651 step 2: owner lifecycle appends retry a stale tail -----------


def test_owner_lifecycle_append_retries_stale_tail_and_preserves_idempotency(enabled, tmp_path, monkeypatch):
    """A StaleSequenceError from the journal tail is retried with the same request.

    The owner append path must re-read and verify the chain head, then retry
    the SAME payload and idempotency key (backoff 0.01 * 2**retry_index), so a
    single cross-process append interleaving does not surface as a failure.
    """
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    calls: list[tuple] = []
    sleeps: list[float] = []
    stale_budget = {"count": 1}
    real_append = run_journal.append_event
    real_read = run_journal.read_journal

    def tracking_append(journal_path, **kwargs):
        if kwargs["event_type"] == "run.planning.started":
            calls.append(
                (
                    "append",
                    kwargs["idempotency_key"],
                    kwargs["expected_previous_sequence"],
                )
            )
            if stale_budget["count"]:
                stale_budget["count"] -= 1
                raise run_journal.StaleSequenceError("stale sequence: injected stale tail")
        return real_append(journal_path, **kwargs)

    def tracking_read(journal_path):
        report = real_read(journal_path)
        calls.append(("read", report.events[-1].sequence if report.events else 0))
        return report

    monkeypatch.setattr(run_journal, "append_event", tracking_append)
    monkeypatch.setattr(run_journal, "read_journal", tracking_read)
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    try:
        with runguard.run_lock(repo, run_dir=run_dir):
            event = run_lifecycle.record_lifecycle_transition(run_dir, status="planning", workspace=repo)
    except run_lifecycle.LifecycleJournalError as exc:
        pytest.fail(f"owner lifecycle append did not retry the stale tail: {exc}")

    assert event is not None
    append_attempts = [call for call in calls if call[0] == "append"]
    assert len(append_attempts) == 2, f"expected the initial attempt plus one retry, got {append_attempts}"
    assert append_attempts[0][1] == append_attempts[1][1], "retry must reuse the original idempotency key"
    first_attempt_index = calls.index(append_attempts[0])
    second_attempt_index = calls.index(append_attempts[1], first_attempt_index + 1)
    fresh_reads = [call for call in calls[first_attempt_index + 1 : second_attempt_index] if call[0] == "read"]
    assert fresh_reads, "the retry must re-read the journal chain head before re-appending"
    assert sleeps == [0.01]

    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == ["run.created", "run.planning.started"]
    committed = [e for e in status_events if e.idempotency_key == append_attempts[0][1]]
    assert len(committed) == 1, "exactly one event may be committed for the retried append"
    assert event.event_id == committed[0].event_id
    assert run_journal.read_journal(_journal_path(run_dir)).chain_errors == []


def test_owner_lifecycle_append_stale_retries_exhausted_blocks_run_json(enabled, tmp_path, monkeypatch):
    """Exhausted stale retries surface a bounded category and block run.json.

    After the initial attempt plus three retries all hit StaleSequenceError,
    the owner append path raises LifecycleJournalError with the bounded
    category ``lifecycle_append_stale_exhausted`` and the run.json snapshot
    does not advance.
    """
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    run_json_before = (run_dir / "run.json").read_bytes()

    attempts: list[dict] = []
    sleeps: list[float] = []
    real_append = run_journal.append_event

    def always_stale(journal_path, **kwargs):
        if kwargs["event_type"] == "run.planning.started":
            attempts.append(kwargs)
            raise run_journal.StaleSequenceError("stale sequence: injected stale tail")
        return real_append(journal_path, **kwargs)

    monkeypatch.setattr(run_journal, "append_event", always_stale)
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError, match="lifecycle_append_stale_exhausted"):
            _write_run_json(run_dir, "planning")

    assert len(attempts) == 4, f"expected the initial attempt plus three retries, got {len(attempts)}"
    assert len({kwargs["idempotency_key"] for kwargs in attempts}) == 1, (
        "every attempt must reuse the original idempotency key"
    )
    assert sleeps == [0.01, 0.02, 0.04], f"expected exponential backoff sleeps, got {sleeps}"
    assert (run_dir / "run.json").read_bytes() == run_json_before, "run.json must not advance past the bounded failure"
