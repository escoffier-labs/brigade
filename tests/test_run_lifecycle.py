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
from pathlib import Path

import pytest

from brigade import aboyeur, localio, proc, run_events, run_journal, run_lifecycle, runguard
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
    assert sorted(path.name for path in (run_dir / "events").iterdir()) == ["lifecycle.jsonl"]


def test_in_lock_write_appends_run_created(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap: request only
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: append run.created

    events = _events(run_dir)
    assert len(events) == 1
    assert events[0].event_type == "run.created"
    assert events[0].payload == {"status": "started"}
    assert events[0].sequence == 1
    assert events[0].previous_digest is None


def test_recording_without_matching_lock_fails_closed(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap: request only
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: run.created
    run_before = (run_dir / "run.json").read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()

    # Activated run, but no lock held: the write fails closed and neither the
    # journal nor the run.json snapshot advances.
    with pytest.raises(run_lifecycle.LifecycleJournalError):
        _write_run_json(run_dir, "planning")

    assert (run_dir / "run.json").read_bytes() == run_before
    assert _journal_path(run_dir).read_bytes() == journal_before
    assert [e.event_type for e in _events(run_dir)] == ["run.created"]


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
    assert [e.event_type for e in events] == ["run.created"]


def test_long_custom_output_dir_final_component_journals(enabled, tmp_path):
    repo = _repo(tmp_path)
    long_run_id = "x" * 200
    run_dir = tmp_path / "custom-output" / long_run_id
    run_dir.mkdir(parents=True)

    _write_run_json(run_dir, "started", lock_workspace=repo)
    _write_run_json_locked(repo, run_dir, "started", lock_workspace=repo)

    events = _events(run_dir)
    assert len(events) == 1
    assert events[0].event_type == "run.created"
    assert len(events[0].idempotency_key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN
    assert events[0].idempotency_key.startswith("lifecycle:")


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
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: run.created

    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)
    _write_run_json_locked(repo, run_dir, "planning")

    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.created", "run.planning.started"]


def test_same_status_refresh_is_noop(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # same-status refresh

    events = _events(run_dir)
    assert len(events) == 1
    assert events[0].event_type == "run.created"


def test_same_status_with_changed_detail_appends_nothing(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    secret_error = "SECRET_TOKEN=/super/private/path"

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "failed", error=secret_error)
    journal_before = _journal_path(run_dir).read_bytes()

    # A detail refresh is not a status transition: the journal must not grow,
    # but the run.json snapshot refresh still proceeds.
    _write_run_json_locked(repo, run_dir, "failed", error="different failure")

    assert _journal_path(run_dir).read_bytes() == journal_before
    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.created", "run.failed"]
    assert events[1].payload == {"status": "failed", "detail": "failed"}
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

    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.created",
        "run.dispatch.requested",
        "run.dispatch.completed",
        "run.dispatch.requested",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    assert events[3].previous_digest == events[2].event_digest


def test_unmapped_intermediate_status_still_appends_the_second_a(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dispatching")
    # Unmapped status: no event, but run.json still advances to it.
    _write_run_json_locked(repo, run_dir, "artifact-collection")
    # A-unmapped-B-A: the second dispatching is a real transition from the
    # artifact-collection snapshot and must append even though the journal
    # tail payload matches.
    _write_run_json_locked(repo, run_dir, "dispatching")

    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "dispatching"
    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.created",
        "run.dispatch.requested",
        "run.dispatch.requested",
    ]
    assert [e.sequence for e in events] == [1, 2, 3]
    assert events[2].previous_digest == events[1].event_digest


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
    assert len(events) == 2
    assert [e.event_type for e in events] == ["run.created", "run.dispatch.completed"]
    assert replay.event_id == events[1].event_id
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_distinct_statuses_produce_distinct_sequence_linked_events(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "planning")
    _write_run_json_locked(repo, run_dir, "failed", error="boom")

    events = _events(run_dir)
    assert [e.sequence for e in events] == [1, 2, 3]
    assert events[0].previous_digest is None
    assert events[1].previous_digest == events[0].event_digest
    assert events[2].previous_digest == events[1].event_digest
    assert events[2].payload == {"status": "failed", "detail": "failed"}
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

        events = _events(run_dir)
        assert [e.event_type for e in events] == ["run.created", expected_type]
        tail = events[1]
        assert tail.payload == {"status": status, "detail": status}
        allowed = run_events.EVENT_TYPES[expected_type]
        assert set(tail.payload.keys()) <= allowed
        assert f"boom {status}" not in _journal_path(run_dir).read_text()


def test_unmapped_status_writes_run_json_without_journal_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dry-run")
    _write_run_json_locked(repo, run_dir, "artifact-collection")

    assert (run_dir / "run.json").is_file()
    assert _events(run_dir) == [_events(run_dir)[0]]


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
    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.created"]


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
    events = _events(run_dir)
    assert [e.event_type for e in events] == ["run.created"]


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
