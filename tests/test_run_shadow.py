"""Regression tests for shadow projection comparison (issue #568 slice 4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brigade import aboyeur, localio, proc, run_checkpoint, run_journal, run_lifecycle, runguard
from brigade import roster as roster_mod
from brigade import run_projector
from brigade import run_shadow  # RED: module does not exist yet
from brigade.run_shadow import (  # RED: constants land in Task 2
    REASON_ERROR_RECORDED,
    REASON_EVIDENCE_PROJECTOR_VERSION_STALE,
    REASON_EVIDENCE_SCHEMA_MISMATCH,
    REASON_EVIDENCE_UNREADABLE,
    REASON_JOURNAL_AHEAD,
    REASON_JOURNAL_UNREADABLE,
    REASON_MISMATCH_RECORDED,
    REASON_NO_COMPARISONS,
    REASON_NO_EVIDENCE,
    REASON_NO_JOURNAL,
)

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


def _minimal_event_payload(event_type: str) -> dict[str, object]:
    if event_type == "run.created":
        return {"status": "started"}
    if event_type == "run.completed":
        return {"status": "ok"}
    if event_type == "run.failed":
        return {"status": "failed", "detail": "failed"}
    if event_type == "run.interrupted":
        return {"status": "canceled"}
    return {}


def _append_forged_checkpoint_status_pair(
    repo: Path,
    run_dir: Path,
    *,
    paired_event_type: str | None,
    status_event_type: str,
    snapshot_status: str,
) -> None:
    """Append a checkpoint+status pair that advances the tail by exactly two."""
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        prev_seq = report.events[-1].sequence
        run_json_bytes = (run_dir / "run.json").read_bytes()
        sha = hashlib.sha256(run_json_bytes).hexdigest()
        paired = paired_event_type if paired_event_type is not None else "none"
        checkpoint_payload = {
            "path": f"events/{run_checkpoint.CHECKPOINT_DIR_NAME}/{sha}.json",
            "sha256": sha,
            "media_type": run_checkpoint.CHECKPOINT_MEDIA_TYPE,
            "byte_size": len(run_json_bytes),
            "privacy_class": run_checkpoint.CHECKPOINT_PRIVACY_CLASS,
            "paired_event_type": paired_event_type,
        }
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type=run_checkpoint.CHECKPOINT_EVENT_TYPE,
            payload=checkpoint_payload,
            idempotency_key=f"checkpoint-forged:{sha}:{paired}",
            expected_previous_sequence=prev_seq,
        )
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type=status_event_type,
            payload=_minimal_event_payload(status_event_type),
            idempotency_key=f"lifecycle:forged:{status_event_type}",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(
            run_dir / "run.json",
            _apply_lifecycle_request(run_dir, _run_payload(snapshot_status)),
        )


def _gap_records(data: dict[str, object]) -> list[dict[str, object]]:
    recent = data.get("recent_records")
    if not isinstance(recent, list):
        return []
    return [
        record
        for record in recent
        if isinstance(record, dict) and record.get("outcome") == "error" and record.get("category") == "comparison-gap"
    ]


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    yield
    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.delenv("BRIGADE_LIFECYCLE_JOURNAL", raising=False)


def test_module_imports():
    assert run_shadow.SHADOW_SCHEMA == "brigade.run_shadow.v1"


def test_flag_off_creates_no_journal_and_no_artifact(disabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json_locked(repo, run_dir, "started")

    assert (run_dir / "run.json").is_file()
    assert not (run_dir / "events").exists()
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_NO_JOURNAL in report.reasons


def test_requested_but_not_activated_creates_no_artifact(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap: request only

    assert (run_dir / "run.json").is_file()
    assert not (run_dir / "events").exists()
    assert not run_shadow.shadow_artifact_path(run_dir).exists()


def test_first_locked_write_records_match(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")  # pre-lock bootstrap
    _write_run_json_locked(repo, run_dir, "started")  # in-lock: run.created

    artifact = run_shadow.shadow_artifact_path(run_dir)
    assert artifact.is_file()
    data = json.loads(artifact.read_text())
    assert data["schema"] == "brigade.run_shadow.v1"
    assert data["schema_version"] == 1
    assert data["run_id"] == _RUN_ID
    assert data["projector_version"] == run_projector.PROJECTOR_VERSION
    assert data["comparisons"] == 1
    assert data["matches"] == 1
    assert data["mismatches"] == 0
    assert data["lags"] == 0
    assert data["errors"] == 0
    assert data["last_compared_sequence"] == 2
    assert data["last_outcome"] == "match"
    assert data["last_shadow_digest"] == data["last_projected_digest"]
    assert data["last_differing_fields"] == []
    assert data["last_error_category"] is None
    assert len(data["recent_records"]) == 1
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()


def test_artifact_collection_after_handoff_is_mapped_no_lag(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = [
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
        "artifact-collection",
    ]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["lags"] == 0
    assert data["mismatches"] == 0
    assert data["errors"] == 0
    assert data["last_outcome"] == "match"
    # artifact-collection is now mapped, so its write appends a checkpoint
    # plus the run.artifact_collection.started status event: 7 mapped writes
    # x (checkpoint + status) = 14 events at the tail.
    assert data["last_compared_sequence"] == 14
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()


def test_mapped_ok_after_lag_restores_readiness(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    # "running" remains an unmapped status, so it produces a lag; the
    # subsequent mapped "ok" restores readiness.
    chain = [
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
        "running",
        "ok",
    ]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["lags"] == 1
    assert data["matches"] == 7
    assert data["last_outcome"] == "match"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()


def test_forged_mapped_status_divergence_is_mismatch(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created, seq 1

    # Forge a legacy candidate that claims "ok" while the journal tail is
    # run.created (started). The projector derives "started". The shadow
    # candidate keeps "ok". The bytes differ on status with a mapped legacy
    # status, so this is a mismatch, not a lag.
    run_before = (run_dir / "run.json").read_bytes()
    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "ok",
            "task": "forged",
        },
    )

    assert (run_dir / "run.json").read_bytes() == run_before  # legacy writer untouched
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["mismatches"] == 1
    assert data["last_outcome"] == "mismatch"
    assert data["last_differing_fields"] == ["status"]
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_MISMATCH_RECORDED in report.reasons


def test_unmapped_event_type_records_projection_error(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created

    # Append a registered-but-unmapped event directly to the journal.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.paused",
            payload={},
            idempotency_key="lifecycle:paused-test",
            expected_previous_sequence=report.events[-1].sequence,
        )

    # Next locked write advances run.json (fail open) and records an error.
    run_before = (run_dir / "run.json").read_bytes()
    _write_run_json_locked(repo, run_dir, "planning")
    assert (run_dir / "run.json").read_bytes() != run_before  # legacy writer advanced

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_outcome"] == "error"
    assert data["last_error_category"] == "projection-error:UnmappedEventTypeError"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_partial_tail_records_journal_unreadable(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created

    # Corrupt the journal by appending a truncated line.
    with _journal_path(run_dir).open("a") as handle:
        handle.write("{not-json\n")

    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "planning",
            "task": "direct",
        },
    )

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_outcome"] == "error"
    assert data["last_error_category"] == "journal-unreadable"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons


def test_crash_window_records_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # checkpoint seq 1, run.created seq 2
    _write_run_json_locked(repo, run_dir, "planning")  # checkpoint seq 3, planning seq 4

    # Simulate a crash: append run.dispatch.requested (seq 5) directly to the
    # journal, write run.json directly so the shadow hook does NOT run, then
    # trigger a later locked write that does run the hook.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-test",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "schema_version": 1,
                "status": "dispatching",
                "task": "crash-window",
            },
        )

    # Later locked write: shadow hook runs and detects the gap (tail seq 7
    # vs prior last_compared_sequence 4, advanced by more than one checkpoint+status write).
    _write_run_json_locked(repo, run_dir, "result-processing")

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    # a comparison-gap error record exists
    gap_records = [r for r in data["recent_records"] if r["outcome"] == "error" and r["category"] == "comparison-gap"]
    assert gap_records
    # Counter coherence: every recorded state change (including the gap side
    # record) counts as one comparison, so the invariant always holds.
    assert data["comparisons"] == data["matches"] + data["mismatches"] + data["lags"] + data["errors"]
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_absent_prior_evidence_with_tail_above_one_records_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # Build a journal with two events but no shadow artifact, simulating a
    # crash that lost the first comparison.
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # checkpoint seq 1, run.created seq 2
    _write_run_json_locked(repo, run_dir, "planning")  # checkpoint seq 3, planning seq 4
    # Remove the artifact so the next comparison sees no prior evidence.
    run_shadow.shadow_artifact_path(run_dir).unlink()
    # Advance the journal one more step without the hook running.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-gap",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "schema_version": 1,
                "status": "dispatching",
                "task": "gap",
            },
        )

    # Direct call: prior evidence absent, tail seq == 5 > 2 -> comparison-gap.
    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "dispatching",
            "task": "gap",
        },
    )

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    gap_records = [r for r in data["recent_records"] if r["outcome"] == "error" and r["category"] == "comparison-gap"]
    assert gap_records
    assert data["comparisons"] == data["matches"] + data["mismatches"] + data["lags"] + data["errors"]
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_corrupt_artifact_quarantined_and_records_evidence_unreadable(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1
    artifact = run_shadow.shadow_artifact_path(run_dir)
    assert artifact.is_file()

    # Corrupt the artifact bytes.
    artifact.write_bytes(b"{not-json\n")
    _write_run_json_locked(repo, run_dir, "planning")  # triggers shadow hook

    # The corrupt file was renamed out of the way.
    quarantined = list((run_dir / "events").glob("shadow-comparison.json.corrupt-*"))
    assert quarantined
    data = json.loads(artifact.read_text())
    # An evidence-unreadable error plus the new comparison are both recorded.
    corrupt_records = [
        r for r in data["recent_records"] if r["outcome"] == "error" and r["category"] == "evidence-unreadable"
    ]
    assert corrupt_records
    # Counter coherence: the evidence-unreadable side record counts as a
    # comparison, so the invariant always holds even when corruption and the
    # main record land in the same atomic write.
    assert data["comparisons"] == data["matches"] + data["mismatches"] + data["lags"] + data["errors"]
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_journal_ahead_of_evidence_blocks_gate(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1
    # Advance the journal directly without running the shadow hook.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.planning.started",
            payload={},
            idempotency_key="lifecycle:planning-ahead",
            expected_previous_sequence=report.events[-1].sequence,
        )

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_AHEAD in report.reasons


def test_gate_reason_coverage(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # No journal at all.
    assert REASON_NO_JOURNAL in run_shadow.check_projection_readiness(run_dir).reasons

    # Journal present, no artifact.
    run_dir.joinpath("events").mkdir()
    run_lifecycle._journal_path(run_dir).write_text("")  # empty journal file
    report = run_shadow.check_projection_readiness(run_dir)
    assert REASON_NO_EVIDENCE in report.reasons

    # Wrong schema.
    localio.write_json(
        run_shadow.shadow_artifact_path(run_dir),
        {
            "schema": "brigade.other.v1",
            "schema_version": 1,
            "run_id": _RUN_ID,
            "projector_version": run_projector.PROJECTOR_VERSION,
            "comparisons": 1,
            "matches": 1,
            "mismatches": 0,
            "lags": 0,
            "errors": 0,
            "last_outcome": "match",
            "last_compared_sequence": None,
            "last_compared_event_digest": None,
            "last_shadow_digest": None,
            "last_projected_digest": None,
            "last_differing_fields": None,
            "last_error_category": None,
            "last_recorded_at": None,
            "recent_records": [],
        },
    )
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons

    # Wrong run_id.
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    data["schema"] = "brigade.run_shadow.v1"
    data["run_id"] = "other"
    data["projector_version"] = run_projector.PROJECTOR_VERSION
    run_shadow.shadow_artifact_path(run_dir).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons

    # Zero comparisons.
    data["run_id"] = _RUN_ID
    data["comparisons"] = 0
    data["projector_version"] = run_projector.PROJECTOR_VERSION
    run_shadow.shadow_artifact_path(run_dir).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_NO_COMPARISONS in run_shadow.check_projection_readiness(run_dir).reasons


def test_gate_never_raises_on_garbage_artifact(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    run_dir.joinpath("events").mkdir()
    run_lifecycle._journal_path(run_dir).write_text("")
    run_shadow.shadow_artifact_path(run_dir).write_bytes(b"\x00\x01\x02 not json at all")

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_EVIDENCE_UNREADABLE in report.reasons


def test_byte_identical_rewrite_is_noop(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # run.created
    _write_run_json_locked(repo, run_dir, "planning")  # run.planning.started
    artifact = run_shadow.shadow_artifact_path(run_dir)
    bytes_before = artifact.read_bytes()

    # A byte-identical rewrite (same status, no field changes) produces no
    # new journal event and leaves the shadow and projected encodings
    # byte-identical, so the digests are unchanged. Under the spec
    # idempotency key (tail sequence, shadow digest, projected digest,
    # outcome, category) this is a complete no-op: no counters move, no
    # record is appended, the artifact is not rewritten.
    _write_run_json_locked(repo, run_dir, "planning")

    assert artifact.read_bytes() == bytes_before
    data = json.loads(artifact.read_text())
    assert data["comparisons"] == 2
    assert data["matches"] == 2


def test_changed_preserved_detail_records_fresh_match_at_same_tail(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # checkpoint seq 1, run.created seq 2
    _write_run_json_locked(repo, run_dir, "planning")  # checkpoint seq 3, planning seq 4
    artifact = run_shadow.shadow_artifact_path(run_dir)
    bytes_before = artifact.read_bytes()
    digests_before = (
        json.loads(artifact.read_text())["last_shadow_digest"],
        json.loads(artifact.read_text())["last_projected_digest"],
    )

    # A same-status write with a changed preserved detail field produces no
    # new journal event (slice 2 skips same-status writes), so the journal
    # tail is unchanged. But the preserved field is copied from the base on
    # both sides, so the shadow and projected encodings both change in
    # lockstep. The digests differ from the prior record, so the
    # idempotency key does not match and a fresh match is recorded at the
    # same journal tail.
    _write_run_json_locked(repo, run_dir, "planning", error="a new detail string")

    assert artifact.read_bytes() != bytes_before
    data = json.loads(artifact.read_text())
    assert data["comparisons"] == 3
    assert data["matches"] == 3
    assert data["last_compared_sequence"] == 5  # checkpoint appended for detail refresh
    assert (data["last_shadow_digest"], data["last_projected_digest"]) != digests_before
    assert data["last_shadow_digest"] == data["last_projected_digest"]


def test_full_mapped_chain_records_seven_matches(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = ["started", "planning", "dispatching", "result-processing", "synthesizing", "handoff", "ok"]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["comparisons"] == 7
    assert data["matches"] == 7
    assert data["mismatches"] == 0
    assert data["lags"] == 0
    assert data["errors"] == 0
    assert data["last_compared_sequence"] == 14
    assert data["last_outcome"] == "match"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()


PRIVATE_MARKER = "PRIVATE-MARKER-DO-NOT-LEAK"


def test_private_error_field_never_enters_artifact(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "failed", error=PRIVATE_MARKER)

    artifact_bytes = run_shadow.shadow_artifact_path(run_dir).read_bytes()
    assert PRIVATE_MARKER.encode() not in artifact_bytes
    # No raw status string appears in the artifact: status divergences are
    # recorded as the field name plus digests, never the value.
    data = json.loads(artifact_bytes)
    for record in data["recent_records"]:
        assert "status" not in record or record.get("differing_fields") is not None
    # The artifact never carries a top-level status field.
    assert "status" not in data


def test_artifact_permissions_and_encoding(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    artifact = run_shadow.shadow_artifact_path(run_dir)
    mode = artifact.stat().st_mode & 0o777
    assert mode == 0o600
    events_mode = (run_dir / "events").stat().st_mode & 0o777
    assert events_mode == 0o700
    raw = artifact.read_bytes()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    # sorted keys, two-space indent
    text = raw.decode("utf-8")
    assert text.startswith('{\n  "')
    assert '  "schema"' in text


def test_recent_records_caps_at_16_while_counters_stay_cumulative(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    # Drive 20 distinct mapped transitions by alternating dispatching/result-processing.
    _write_run_json_locked(repo, run_dir, "started")  # seq 1
    for i in range(19):
        status = "dispatching" if i % 2 == 0 else "result-processing"
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["comparisons"] == 20
    assert data["matches"] + data["mismatches"] + data["lags"] + data["errors"] == 20
    assert len(data["recent_records"]) == 16


def test_differing_fields_caps_at_max_with_distinct_names():
    # Drive more than 16 genuinely distinct differing field names through the
    # _differing_fields helper directly. The list must cap at
    # MAX_DIFFERING_FIELDS using the first MAX_DIFFERING_FIELDS-1 sorted names
    # plus the literal truncation token "...".
    shadow = {f"field_{i:02d}": i for i in range(20)}
    projected = {f"field_{i:02d}": i + 1 for i in range(20)}
    differing = run_shadow._differing_fields(shadow, projected)
    assert len(differing) == run_shadow.MAX_DIFFERING_FIELDS
    assert differing[-1] == "..."
    assert differing[:-1] == sorted(f"field_{i:02d}" for i in range(run_shadow.MAX_DIFFERING_FIELDS - 1))


def test_failing_evidence_write_through_writer_path_is_swallowed(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1

    # Break only the shadow artifact write. The legacy run.json write uses
    # localio.write_text_atomic directly in aboyeur._write_json; the shadow
    # evidence write goes through run_shadow._write_artifact. Patch the
    # shadow-only helper to raise.
    def raising_write(run_dir_arg, data):
        raise OSError("disk full")

    monkeypatch.setattr(run_shadow, "_write_artifact", raising_write)

    run_before = (run_dir / "run.json").read_bytes()
    _write_run_json_locked(repo, run_dir, "planning")  # legacy write must still advance
    assert (run_dir / "run.json").read_bytes() != run_before
    # The previous artifact is left in place (the failed write never replaced it).
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["comparisons"] == 1


def test_run_journal_error_in_shadow_is_contained(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1

    # The shadow path reads the journal through read_journal_bounded; the
    # legacy lifecycle path (record_lifecycle_transition) still reads through
    # read_journal. Raise only on the bounded read so the legacy write still
    # commits, then the shadow hook sees the RunJournalError and must contain
    # it as journal-unreadable.
    def raising_bounded(path):
        raise run_journal.RunJournalError("simulated chain failure")

    monkeypatch.setattr(run_journal, "read_journal_bounded", raising_bounded)

    run_before = (run_dir / "run.json").read_bytes()
    _write_run_json_locked(repo, run_dir, "planning")  # legacy write must still advance
    assert (run_dir / "run.json").read_bytes() != run_before
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_error_category"] == "journal-unreadable"


def test_gate_reports_journal_unreadable_for_partial_tail(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1, clean artifact

    # Corrupt the journal with a partial tail (no terminating newline).
    with _journal_path(run_dir).open("a") as handle:
        handle.write("{not-json")

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons


def test_gate_reports_journal_unreadable_for_chain_errors(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1, clean artifact

    # Append a complete-but-malformed line (has newline) -> chain_error.
    with _journal_path(run_dir).open("a") as handle:
        handle.write("not-json-at-all\n")

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons


@pytest.mark.parametrize(
    "exc_factory",
    [OSError, run_journal.RunJournalError],
    ids=["oserror", "runjournalerror"],
)
def test_gate_reports_journal_unreadable_when_read_raises(enabled, tmp_path, monkeypatch, exc_factory):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 1, clean artifact

    def raising_bounded(path):
        raise exc_factory("simulated journal read failure")

    monkeypatch.setattr(run_journal, "read_journal_bounded", raising_bounded)

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons
    assert REASON_EVIDENCE_UNREADABLE not in report.reasons


def test_journal_run_id_mismatch_records_error(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # Build a journal whose first (and only) verified event carries a run_id
    # that differs from the run directory name. The slice-2 writer derives
    # run_id from the directory name, so this cannot fire through the normal
    # path; it is defense in depth for direct calls.
    foreign = "20260101-000000-foreign1"
    run_journal.append_event(
        _journal_path(run_dir),
        run_id=foreign,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="lifecycle:foreign-created",
        expected_previous_sequence=0,
    )

    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "started",
            "task": "direct",
        },
    )

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_error_category"] == "journal-run-id-mismatch"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_same_tail_distinct_unmapped_shadow_states_count_distinct_lags(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = [
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
    ]
    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    # "running" remains an unmapped status. Two shadow candidates with the
    # same unmapped status but distinct preserved detail (task) produce
    # distinct shadow_digests at the same journal tail (seq 12), so they are
    # distinct lag states and must both be counted -- idempotency keys on
    # (tail sequence, shadow digest, projected digest, outcome, category),
    # not on the journal tail alone.
    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "running",
            "task": "direct-a",
        },
    )
    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "running",
            "task": "direct-b",
        },
    )

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["lags"] == 2
    assert data["last_outcome"] == "lag"
    assert data["last_compared_sequence"] == 12


def test_non_run_json_write_leaves_artifact_untouched(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match
    artifact = run_shadow.shadow_artifact_path(run_dir)
    bytes_before = artifact.read_bytes()

    # A non-run.json write through aboyeur._write_json must not trigger the
    # shadow comparison hook.
    aboyeur._write_json(run_dir / "roster.json", {"orchestrator": "chef"})

    assert artifact.read_bytes() == bytes_before


def test_invalid_plus_two_suffix_null_paired_records_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _append_forged_checkpoint_status_pair(
        repo,
        run_dir,
        paired_event_type=None,
        status_event_type="run.planning.started",
        snapshot_status="planning",
    )

    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "planning",
            "task": "forged-null-paired",
        },
    )

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert _gap_records(data)
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_invalid_plus_two_suffix_mismatched_paired_records_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _append_forged_checkpoint_status_pair(
        repo,
        run_dir,
        paired_event_type="run.planning.started",
        status_event_type="run.dispatch.requested",
        snapshot_status="dispatching",
    )

    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "dispatching",
            "task": "forged-mismatched-paired",
        },
    )

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert _gap_records(data)
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_invalid_plus_two_suffix_unmapped_successor_records_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _append_forged_checkpoint_status_pair(
        repo,
        run_dir,
        paired_event_type="run.created",
        status_event_type="run.paused",
        snapshot_status="planning",
    )

    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "planning",
            "task": "forged-unmapped-successor",
        },
    )

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert _gap_records(data)
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_second_mapped_write_does_not_record_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # checkpoint seq 1, run.created seq 2
    _write_run_json_locked(repo, run_dir, "planning")  # checkpoint seq 3, planning seq 4

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    gap_records = [r for r in data["recent_records"] if r["outcome"] == "error" and r["category"] == "comparison-gap"]
    assert not gap_records
    assert data["errors"] == 0
    assert data["matches"] == 2
    assert data["last_compared_sequence"] == 4
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()


def test_version_one_shadow_artifact_is_stale(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    stale_seq = data["last_compared_sequence"]
    data["projector_version"] = 1
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    stale_bytes = artifact.read_bytes()

    # Version door: a noncurrent projector version closes the gate with the
    # dedicated stale reason BEFORE the schema check fires.
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_EVIDENCE_PROJECTOR_VERSION_STALE in report.reasons
    assert REASON_EVIDENCE_SCHEMA_MISMATCH not in report.reasons

    # Quarantine: the next shadow comparison atomically renames the stale
    # artifact to a private .stale-projector-v2-<timestamp> sibling under
    # events before a fresh current-v3 artifact is written.
    _write_run_json_locked(repo, run_dir, "planning")

    quarantined = list((run_dir / "events").glob(".stale-projector-v2-*"))
    assert quarantined
    assert quarantined[0].read_bytes() == stale_bytes
    assert artifact.read_bytes() != stale_bytes
    fresh = json.loads(artifact.read_text())
    assert fresh["projector_version"] == run_projector.PROJECTOR_VERSION
    # Fresh v3 counters: no carried stale counters or recent records.
    assert fresh["comparisons"] == 1
    assert fresh["matches"] == 1
    assert fresh["mismatches"] == 0
    assert fresh["lags"] == 0
    assert fresh["errors"] == 0
    assert len(fresh["recent_records"]) == 1
    # The stale artifact's verified tail is reused as the gap baseline.
    assert fresh["last_compared_sequence"] != stale_seq


def test_current_projector_version_artifact_with_checkpoint_tail_reads_ready_when_bytes_match(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    events = _events(run_dir)
    assert events[-1].event_type == "run.created"
    assert events[0].event_type == "run.snapshot.checkpointed"

    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    assert data["projector_version"] == run_projector.PROJECTOR_VERSION
    assert data["last_compared_sequence"] == 2
    assert data["last_outcome"] == "match"

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()


def _make_stale_artifact(run_dir: Path, *, stale_version: int) -> tuple[int, str | None]:
    """Rewrite the current artifact with a noncurrent projector version.

    Returns (stale_last_compared_sequence, stale_last_compared_event_digest).
    """
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    seq = data["last_compared_sequence"]
    digest = data["last_compared_event_digest"]
    data["projector_version"] = stale_version
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return seq, digest


def test_stale_projector_version_gate_returns_only_stale_reason(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    # Wrong projector_version but otherwise valid. The version door fires
    # BEFORE the schema check and returns only the stale reason.
    _make_stale_artifact(run_dir, stale_version=2)
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert report.reasons == (REASON_EVIDENCE_PROJECTOR_VERSION_STALE,)
    assert REASON_EVIDENCE_SCHEMA_MISMATCH not in report.reasons
    assert REASON_NO_COMPARISONS not in report.reasons


def test_schema_only_mismatch_still_returns_schema_mismatch(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    # Correct projector_version, wrong schema -> schema mismatch (not stale).
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["schema"] = "brigade.other.v1"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons
    assert REASON_EVIDENCE_PROJECTOR_VERSION_STALE not in run_shadow.check_projection_readiness(run_dir).reasons

    # Correct projector_version, wrong schema_version -> schema mismatch.
    data = json.loads(artifact.read_text())
    data["schema"] = "brigade.run_shadow.v1"
    data["schema_version"] = 999
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons
    assert REASON_EVIDENCE_PROJECTOR_VERSION_STALE not in run_shadow.check_projection_readiness(run_dir).reasons

    # Correct projector_version, wrong run_id -> schema mismatch.
    data = json.loads(artifact.read_text())
    data["schema_version"] = 1
    data["run_id"] = "other-run"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons
    assert REASON_EVIDENCE_PROJECTOR_VERSION_STALE not in run_shadow.check_projection_readiness(run_dir).reasons


def test_stale_quarantine_writes_fresh_v3_counters(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 2
    _write_run_json_locked(repo, run_dir, "planning")  # match, seq 4

    artifact = run_shadow.shadow_artifact_path(run_dir)
    stale_data = json.loads(artifact.read_text())
    assert stale_data["comparisons"] == 2
    assert stale_data["matches"] == 2

    _make_stale_artifact(run_dir, stale_version=2)
    stale_bytes = artifact.read_bytes()

    _write_run_json_locked(repo, run_dir, "dispatching")  # triggers quarantine + fresh v3

    quarantined = list((run_dir / "events").glob(".stale-projector-v2-*"))
    assert quarantined
    assert quarantined[0].read_bytes() == stale_bytes

    fresh = json.loads(artifact.read_text())
    assert fresh["projector_version"] == run_projector.PROJECTOR_VERSION
    # Fresh v3 counters: stale counters and recent records are NOT carried.
    assert fresh["comparisons"] == 1
    assert fresh["matches"] == 1
    assert fresh["mismatches"] == 0
    assert fresh["lags"] == 0
    assert fresh["errors"] == 0
    assert len(fresh["recent_records"]) == 1
    assert fresh["recent_records"][0]["outcome"] == "match"


def test_crash_window_after_quarantine_reads_as_no_evidence(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 2
    _write_run_json_locked(repo, run_dir, "planning")  # match, seq 4

    _make_stale_artifact(run_dir, stale_version=2)
    artifact = run_shadow.shadow_artifact_path(run_dir)

    # Simulate the crash window: quarantine the stale artifact (atomic rename
    # to the private sibling) but do NOT write a fresh current-v3 artifact.
    stamp = "20260730T000000000000Z"
    artifact.replace(artifact.with_name(f".stale-projector-v2-{stamp}"))
    assert not artifact.exists()

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_NO_EVIDENCE in report.reasons
    assert REASON_EVIDENCE_PROJECTOR_VERSION_STALE not in report.reasons


def test_stale_quarantine_reuses_gap_baseline_no_false_gap_on_normal_advance(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # checkpoint 1, created 2
    _write_run_json_locked(repo, run_dir, "planning")  # checkpoint 3, planning 4

    stale_seq, _ = _make_stale_artifact(run_dir, stale_version=2)
    assert stale_seq == 4

    # A normal checkpoint+status advance (dispatching) from baseline 4 to
    # tail 6 must NOT create a false comparison gap: the stale baseline is
    # reused, and the (5,6) pair is a valid checkpoint+status pair.
    _write_run_json_locked(repo, run_dir, "dispatching")

    fresh = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    gap_records = _gap_records(fresh)
    assert not gap_records
    assert fresh["errors"] == 0
    assert fresh["comparisons"] == 1
    assert fresh["matches"] == 1
    assert fresh["last_compared_sequence"] == 6
    assert list((run_dir / "events").glob(".stale-projector-v2-*"))


def test_stale_quarantine_larger_advance_records_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # checkpoint 1, created 2
    _write_run_json_locked(repo, run_dir, "planning")  # checkpoint 3, planning 4

    stale_seq, _ = _make_stale_artifact(run_dir, stale_version=2)
    assert stale_seq == 4

    # Skip a shadow step: append run.dispatch.requested (seq 5) directly,
    # write run.json directly so the shadow hook does NOT run.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-stale-gap",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "schema_version": 1,
                "status": "dispatching",
                "task": "stale-gap",
            },
        )

    # Later locked write (result-processing) runs the hook: tail seq 7 vs the
    # reused stale baseline 4 is an unexplained advance (7 != 4+2), so a
    # fresh comparison-gap error is recorded on the fresh v3 artifact.
    _write_run_json_locked(repo, run_dir, "result-processing")

    fresh = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    gap_records = _gap_records(fresh)
    assert gap_records
    assert fresh["errors"] >= 1
    assert fresh["comparisons"] == fresh["matches"] + fresh["mismatches"] + fresh["lags"] + fresh["errors"]
    assert list((run_dir / "events").glob(".stale-projector-v2-*"))


def test_current_v3_mismatch_is_never_reset_by_later_match(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 2

    # Record a mismatch against the current v3 artifact (forged status
    # divergence with a mapped legacy status) at the same journal tail.
    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "ok",
            "task": "forged-mismatch",
        },
    )
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["mismatches"] == 1
    assert data["last_outcome"] == "mismatch"

    # A subsequent matching comparison (normal checkpoint+status advance from
    # seq 2 to seq 4, no gap) must NOT reset the recorded mismatch: counters on
    # a current-v3 artifact only accumulate, never zero out.
    _write_run_json_locked(repo, run_dir, "planning")  # match, seq 4

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["mismatches"] == 1
    assert data["matches"] == 2
    assert data["last_outcome"] == "match"


def test_current_v3_error_is_never_reset_by_later_match(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 2

    # Record a journal-unreadable error against the current v3 artifact via a
    # direct call (a locked write would fail inside write_checkpoint before
    # the shadow hook runs, so the error path is exercised directly). The
    # journal tail stays at seq 2 throughout so the later clean comparison
    # does not trip the absent-prior gap guard.
    with _journal_path(run_dir).open("a") as handle:
        handle.write("{not-json\n")
    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "planning",
            "task": "direct-error",
        },
    )
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_error_category"] == "journal-unreadable"

    # Repair the journal corruption; the verified tail is back at seq 2.
    raw = _journal_path(run_dir).read_text()
    _journal_path(run_dir).write_text(raw.split("{not-json")[0])

    # A clean direct comparison matching the journal tail (started) records a
    # match without resetting the prior error: prior_seq is None (the error
    # record carried no sequence), tail_seq is 2, and the (1,2) pair is a
    # valid checkpoint+status pair, so no false comparison-gap fires.
    run_shadow.record_shadow_comparison(
        run_dir,
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "started",
            "task": "direct-match",
        },
    )
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_outcome"] == "match"
    assert data["matches"] == 2


def test_bounded_journal_reads_map_bound_failure_to_journal_unreadable(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # clean current-v3 artifact

    # A bound-exceeded journal read must map to journal-unreadable on both the
    # comparison path and the readiness path. read_journal_bounded is the
    # only journal reader run_shadow uses; the legacy lifecycle path keeps
    # using unbounded read_journal, so the legacy write still commits.
    def raising_bounded(path):
        raise run_journal.RunJournalError("bound exceeded: journal above MAX_JOURNAL_BYTES")

    monkeypatch.setattr(run_journal, "read_journal_bounded", raising_bounded)

    # Comparison path: legacy write still advances, shadow records
    # journal-unreadable.
    run_before = (run_dir / "run.json").read_bytes()
    _write_run_json_locked(repo, run_dir, "planning")
    assert (run_dir / "run.json").read_bytes() != run_before
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert data["errors"] >= 1
    assert data["last_error_category"] == "journal-unreadable"

    # Readiness path: bound failure closes the gate with journal-unreadable.
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons
    assert REASON_EVIDENCE_UNREADABLE not in report.reasons


def _forge_stale_baseline(run_dir: Path, *, stale_version: int, baseline_seq: object, baseline_digest: object) -> None:
    """Rewrite the current artifact with a noncurrent projector version and a
    forged last_compared_sequence / last_compared_event_digest pair."""
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["projector_version"] = stale_version
    data["last_compared_sequence"] = baseline_seq
    data["last_compared_event_digest"] = baseline_digest
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def test_quarantine_replace_failure_leaves_stale_artifact_intact(enabled, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # match, seq 2
    _write_run_json_locked(repo, run_dir, "planning")  # match, seq 4

    _make_stale_artifact(run_dir, stale_version=2)
    artifact = run_shadow.shadow_artifact_path(run_dir)
    stale_bytes = artifact.read_bytes()
    assert not list((run_dir / "events").glob(".stale-projector-v2-*"))

    # Force only the quarantine atomic rename (Path.replace on the stale
    # artifact path) to fail. The fresh evidence write goes through
    # localio.write_text_atomic which uses os.replace on a temp file, so it
    # is unaffected by this targeted Path.replace patch.
    real_replace = Path.replace

    def failing_replace(self, target):
        if str(self) == str(artifact):
            raise OSError("rename not permitted")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)

    # The write path must not raise even though quarantine failed.
    _write_run_json_locked(repo, run_dir, "dispatching")

    # Quarantine failed: the active stale artifact is byte-identical.
    assert artifact.read_bytes() == stale_bytes
    # No quarantine sibling was created.
    assert not list((run_dir / "events").glob(".stale-projector-v2-*"))


def test_forged_stale_digest_cannot_suppress_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # ck 1, created 2
    _write_run_json_locked(repo, run_dir, "planning")  # ck 3, planning 4

    # Forge a stale artifact whose baseline claims seq 6 with a digest that
    # does not match the real event at seq 6. Without verification this
    # baseline would make the tail-7 advance look like a single step (+1)
    # and suppress the comparison gap; with verification the digest
    # mismatch drops the baseline and the gap is recorded.
    _forge_stale_baseline(
        run_dir,
        stale_version=2,
        baseline_seq=6,
        baseline_digest="forged-digest-not-matching-event-6",
    )

    # Crash window: append run.dispatch.requested (seq 5) directly and write
    # run.json directly so the shadow hook does NOT run.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-forged-digest",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "schema_version": 1,
                "status": "dispatching",
                "task": "forged-digest",
            },
        )

    # Later locked write (result-processing) appends ck 6 + status 7 and runs
    # the hook. The forged baseline must NOT suppress the gap.
    _write_run_json_locked(repo, run_dir, "result-processing")

    fresh = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert _gap_records(fresh)
    assert fresh["errors"] >= 1
    assert fresh["comparisons"] == fresh["matches"] + fresh["mismatches"] + fresh["lags"] + fresh["errors"]
    assert list((run_dir / "events").glob(".stale-projector-v2-*"))


def test_forged_stale_sequence_out_of_range_cannot_suppress_comparison_gap(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # ck 1, created 2
    _write_run_json_locked(repo, run_dir, "planning")  # ck 3, planning 4

    # Forge a stale artifact whose baseline claims seq 999 (out of range).
    # Without verification this baseline would make tail 7 <= prior and
    # suppress the comparison gap; with verification the out-of-range
    # sequence drops the baseline and the gap is recorded.
    _forge_stale_baseline(
        run_dir,
        stale_version=2,
        baseline_seq=999,
        baseline_digest="forged-digest",
    )

    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-forged-seq",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "schema_version": 1,
                "status": "dispatching",
                "task": "forged-seq",
            },
        )

    _write_run_json_locked(repo, run_dir, "result-processing")

    fresh = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert _gap_records(fresh)
    assert fresh["errors"] >= 1
    assert fresh["comparisons"] == fresh["matches"] + fresh["mismatches"] + fresh["lags"] + fresh["errors"]
    assert list((run_dir / "events").glob(".stale-projector-v2-*"))


def test_malformed_last_outcome_is_unreadable_not_schema_mismatch(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    run_dir.joinpath("events").mkdir()
    run_lifecycle._journal_path(run_dir).write_text("")  # empty journal file

    # A current-version artifact with comparisons >= 1, zero counters, and a
    # bogus last_outcome. The version door and schema check both pass; the
    # no-comparisons branch passes; the malformed last_outcome must close the
    # gate as evidence-unreadable, NOT schema mismatch.
    localio.write_json(
        run_shadow.shadow_artifact_path(run_dir),
        {
            "schema": "brigade.run_shadow.v1",
            "schema_version": 1,
            "run_id": _RUN_ID,
            "projector_version": run_projector.PROJECTOR_VERSION,
            "comparisons": 1,
            "matches": 0,
            "mismatches": 0,
            "lags": 0,
            "errors": 0,
            "last_outcome": "bogus",
            "last_compared_sequence": None,
            "last_compared_event_digest": None,
            "last_shadow_digest": None,
            "last_projected_digest": None,
            "last_differing_fields": None,
            "last_error_category": None,
            "last_recorded_at": None,
            "recent_records": [],
        },
    )
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_EVIDENCE_UNREADABLE in report.reasons
    assert REASON_EVIDENCE_SCHEMA_MISMATCH not in report.reasons

    # A None last_outcome is equally malformed.
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    data["last_outcome"] = None
    run_shadow.shadow_artifact_path(run_dir).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_EVIDENCE_UNREADABLE in report.reasons
    assert REASON_EVIDENCE_SCHEMA_MISMATCH not in report.reasons


def test_forged_boolean_sequence_cannot_be_carried_as_stale_baseline(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # ck 1, created 2
    _write_run_json_locked(repo, run_dir, "planning")  # ck 3, planning 4

    # bool is an int subclass, so a naive ``isinstance(baseline_seq, int)``
    # guard would accept ``True`` (=1) and carry it as the stale baseline.
    # Forge ``True`` with the REAL digest of event 1 so every check except the
    # bool rejection would pass; without the explicit bool guard this baseline
    # is carried and the artifact's last_compared_baseline cursor is corrupted
    # by a boolean. _verify_stale_baseline must reject it outright.
    real_event_1_digest = _events(run_dir)[0].event_digest
    assert run_shadow._verify_stale_baseline(run_dir, _RUN_ID, True, real_event_1_digest) is None
    assert run_shadow._verify_stale_baseline(run_dir, _RUN_ID, False, real_event_1_digest) is None

    # End-to-end: the forged boolean baseline must not suppress the
    # comparison-gap recorded when a later locked write runs the hook after a
    # crash window skipped a shadow step.
    _forge_stale_baseline(
        run_dir,
        stale_version=2,
        baseline_seq=True,
        baseline_digest=real_event_1_digest,
    )

    # Crash window: append run.dispatch.requested (seq 5) directly and write
    # run.json directly so the shadow hook does NOT run.
    with runguard.run_lock(repo, run_dir=run_dir):
        report = run_journal.read_journal(_journal_path(run_dir))
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=_RUN_ID,
            event_type="run.dispatch.requested",
            payload={},
            idempotency_key="lifecycle:dispatch-forged-bool",
            expected_previous_sequence=report.events[-1].sequence,
        )
        localio.write_json(
            run_dir / "run.json",
            {
                "schema": "brigade.run.v1",
                "schema_version": 1,
                "status": "dispatching",
                "task": "forged-bool",
            },
        )

    # Later locked write (result-processing) appends ck 6 + status 7 and runs
    # the hook. The forged boolean baseline must NOT be carried, so the
    # comparison-gap fires (it would also fire if carried, but the direct
    # _verify_stale_baseline assertions above are the RED driver for the
    # bool rejection fix).
    _write_run_json_locked(repo, run_dir, "result-processing")

    fresh = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert _gap_records(fresh)
    assert fresh["errors"] >= 1
    # The carried cursor is never a bool: it is either None (rejected) or the
    # real int tail sequence written by the main record.
    assert not isinstance(fresh["last_compared_sequence"], bool)
    assert isinstance(fresh["last_compared_sequence"], int)
    assert fresh["comparisons"] == fresh["matches"] + fresh["mismatches"] + fresh["lags"] + fresh["errors"]
    assert list((run_dir / "events").glob(".stale-projector-v2-*"))


def test_real_journal_above_max_bytes_records_journal_unreadable(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # Build a clean current-version artifact with a real parsed legacy
    # snapshot (ck 1, run.created 2). This is the "previously parsed legacy
    # snapshot" the integration test replays after the journal grows.
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    legacy_snapshot = json.loads((run_dir / "run.json").read_text())
    artifact = run_shadow.shadow_artifact_path(run_dir)
    clean_data = json.loads(artifact.read_text())
    assert clean_data["last_outcome"] == "match"
    assert clean_data["errors"] == 0

    # Grow the real journal past run_checkpoint.MAX_JOURNAL_BYTES (8 MiB) by
    # appending padding bytes. read_journal_bounded fstat-checks the file
    # size before any allocation and raises RunJournalError("bound exceeded:
    # journal above MAX_JOURNAL_BYTES") -- no monkeypatch, the real bound.
    padding = b"\n" * (run_checkpoint.MAX_JOURNAL_BYTES + 1024)
    with _journal_path(run_dir).open("ab") as handle:
        handle.write(padding)
    assert _journal_path(run_dir).stat().st_size > run_checkpoint.MAX_JOURNAL_BYTES

    # Replay the previously parsed legacy snapshot through the comparison
    # path. The bounded read raises, so the shadow hook must classify the
    # failure as journal-unreadable on the active artifact.
    run_shadow.record_shadow_comparison(run_dir, legacy_snapshot)

    data = json.loads(artifact.read_text())
    assert data["errors"] >= 1
    assert data["last_outcome"] == "error"
    assert data["last_error_category"] == "journal-unreadable"

    # The readiness gate re-reads the journal through the same bounded reader
    # and must close with REASON_JOURNAL_UNREADABLE (not evidence-unreadable).
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons
    assert REASON_EVIDENCE_UNREADABLE not in report.reasons
