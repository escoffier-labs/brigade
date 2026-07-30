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
import stat
from pathlib import Path

import pytest

from brigade import (
    aboyeur,
    localio,
    proc,
    run_checkpoint,
    run_events,
    run_journal,
    run_lifecycle,
    runguard,
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
        "run.dispatch.requested",
        "run.dispatch.completed",
        "run.dispatch.requested",
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
        "run.dispatch.requested",
        "run.snapshot.checkpointed",
        "run.dispatch.completed",
        "run.dispatch.requested",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6, 7]
    assert events[3].previous_digest == events[2].event_digest
    assert events[6].previous_digest == events[5].event_digest


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
        "run.dispatch.requested",
        "run.artifact_collection.started",
        "run.dispatch.requested",
    ]
    events = _events(run_dir)
    # The second "dispatching" produces identical run.json bytes to the first,
    # so its checkpoint replays; only the status event appends.
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.snapshot.checkpointed",
        "run.dispatch.requested",
        "run.snapshot.checkpointed",
        "run.artifact_collection.started",
        "run.dispatch.requested",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6, 7]
    # The second run.dispatch.requested links to the artifact-collection
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
        "run.dispatch.completed",
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
