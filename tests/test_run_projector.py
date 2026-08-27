"""Tests for brigade.run_projector pure snapshot projection."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import run_checkpoint, run_events, run_journal
from brigade.run_projector import (
    DERIVED_FIELDS,
    EVENT_STATUS,
    OWNED_FIELDS,
    PRESERVED_FIELDS,
    PROJECTOR_VERSION,
    EventChainError,
    EventPayloadError,
    ProjectionError,
    ProjectionInputError,
    RunProjection,
    SnapshotEncodingError,
    UnknownSnapshotFieldError,
    encode_snapshot_bytes,
    project_run_snapshot,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "run-lifecycle"
GOLDEN_LIFECYCLE_PATH = FIXTURES / "golden-lifecycle.jsonl"
GOLDEN_BASE_PATH = FIXTURES / "golden-projection.base.json"
GOLDEN_EXPECTED_PATH = FIXTURES / "golden-projection.expected.json"

RUN_ID = "20260727-153045-a1b2c3d4"
OTHER_RUN_ID = "20260727-153045-z9y8x7w6"
RECORDED_AT = "2026-07-27T15:30:45.123456Z"
GOLDEN_FINAL_DIGEST = "176259cd00408b637f1c13496fcdfafea7dd96753fb8a9e76dbea58817442fc7"


def _golden_events() -> list[run_journal.RunEvent]:
    """Load the verified golden lifecycle event sequence from the fixture."""
    report = run_journal.read_journal(GOLDEN_LIFECYCLE_PATH)
    assert report.partial_tail is None
    assert report.chain_errors == []
    return report.events


def _build_event(
    sequence: int,
    event_type: str,
    payload: dict,
    idempotency_key: str,
    recorded_at: str,
    previous_digest: str | None,
    *,
    run_id: str = RUN_ID,
) -> dict:
    """Build a validated run_event.v1 envelope using the real run_events contract."""
    return run_events.build_event(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
        recorded_at=recorded_at,
        previous_digest=previous_digest,
    )


def _minimal_base_snapshot(status: str = "started") -> dict:
    """Return a minimal base snapshot that satisfies the ownership map."""
    return {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "status": status,
        "task": "test-task",
        "orchestrator": "test-orchestrator",
        "roster": "test-roster",
        "worker": "test-worker",
        "scheduler": "immediate",
    }


def _full_base_snapshot() -> dict:
    """Return a base snapshot covering every preserved run.json field."""
    base = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "status": "started",
        "task": "task-id",
        "orchestrator": "orchestrator-id",
        "roster": "roster-id",
        "worker": "worker-id",
        "scheduler": "immediate",
        "dry_run": False,
        "read_only": False,
        "cwd": "/tmp",
        "lock_workspace": True,
        "codex_transport": "app-server",
        "lifecycle_journal_requested": True,
        "approval_reference": {
            "approval_id": "approval-1",
            "source": "daily",
            "fingerprint": "approval-fingerprint",
        },
        "verification_contract": {
            "schema": "brigade.verification_contract.v1",
            "schema_version": 1,
            "budget": {"worker_dispatch_count": 2},
        },
        "run_budget": {
            "schema": "brigade.run_budget.v1",
            "schema_version": 1,
            "ceilings": {"worker_dispatch_count": 2},
        },
        "started_at": "2026-07-27T15:30:00.000000Z",
        "status_started_at": "2026-07-27T15:30:00.000000Z",
        "finished_at": None,
        "duration_seconds": 0,
        "resumed_at": ["2026-07-27T15:31:00.000000Z"],
        "recovery_history": [{"kind": "worker-error"}],
        "recovery_preserved_artifact": "/tmp/run.json.corrupt",
        "code_graph_brief": {"entries": []},
        "drift_impact_brief": {"entries": []},
        "evidence_brief": {"entries": []},
        "brief_budget": 100,
        "route": "route-id",
        "skill_route_policy": "auto",
        "seat_routing": [{"requested_seat": "coder", "outcome": "skip"}],
        "retry_decisions": [
            {
                "attempt": 1,
                "seat": "coder",
                "failure_kind": "network-unavailable",
                "decision": "same-seat-once",
                "reason": "seat coder may retry once after re-probe [network-unavailable]",
            }
        ],
        "health": {"schema": "brigade.seat_health_summary.v1", "healthy": 1},
        "worker_failure_summary": {"domain": "infrastructure", "classes": {}, "seats": []},
        "transport_routing": {"schema": "brigade.transport_routing.v1", "outcome": "fallback"},
        "pre_run_snapshot": {"files": []},
        "git": {"branch": "main"},
        "code_graph_delta": {"changed": []},
        "context_eval": {"score": 0},
        "suspected_noop": False,
        "control_transport": "unix",
        "control_socket": "/tmp/control.sock",
        "active_stage": "planning",
        "active_seats": ["coder"],
        "phase_owner": "coder",
        "error": None,
        "failure_phase": None,
        "failure_kind": None,
        "failure": {"detail": None},
        "transport_warning": None,
        "artifact_collection": None,
        "artifacts": [],
        "handoff": None,
    }
    for field in PRESERVED_FIELDS:
        if field not in base:
            base[field] = None
    return base


def _chain_events(scenario: str) -> list[dict]:
    """Build event sequences that fail chain verification for named reasons."""
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    if scenario == "gap":
        planning = _build_event(
            3,
            "run.planning.started",
            {"detail": "planning"},
            "plan-3",
            "2026-07-27T15:30:46.000000Z",
            created["event_digest"],
        )
        return [created, planning]
    if scenario == "duplicate":
        duplicate = _build_event(
            1,
            "run.created",
            {"status": "started"},
            "create-dup",
            "2026-07-27T15:30:46.000000Z",
            None,
        )
        return [created, duplicate]
    if scenario == "broken_digest":
        planning = _build_event(
            2,
            "run.planning.started",
            {"detail": "planning"},
            "plan-2",
            "2026-07-27T15:30:46.000000Z",
            "0" * 64,
        )
        return [created, planning]
    if scenario == "mixed_run_id":
        other_created = _build_event(
            1,
            "run.created",
            {"status": "started"},
            "create-other",
            RECORDED_AT,
            None,
            run_id=OTHER_RUN_ID,
        )
        planning = _build_event(
            2,
            "run.planning.started",
            {"detail": "planning"},
            "plan-2",
            "2026-07-27T15:30:46.000000Z",
            other_created["event_digest"],
        )
        return [other_created, planning]
    raise ValueError(f"unknown scenario: {scenario}")


def _events_ending_with_failed(*, status: str | None) -> list[dict]:
    """Build a two-event sequence ending with run.failed with the given status."""
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    payload = {"detail": "failed"}
    if status is not None:
        payload["status"] = status
    failed = _build_event(
        2,
        "run.failed",
        payload,
        "failed-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    return [created, failed]


def _events_ending_with_interrupted(*, status: str) -> list[dict]:
    """Build a two-event sequence ending with run.interrupted with the given status."""
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    interrupted = _build_event(
        2,
        "run.interrupted",
        {"status": status},
        "interrupt-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    return [created, interrupted]


def _build_checkpoint_event(
    sequence: int,
    previous_digest: str | None,
    *,
    paired_event_type: str | None = "run.created",
    recorded_at: str = RECORDED_AT,
) -> dict:
    """Build a validated checkpoint envelope."""
    sha = "a" * 64
    payload = {
        "path": f"events/recovery-checkpoints/{sha}.json",
        "sha256": sha,
        "media_type": run_checkpoint.CHECKPOINT_MEDIA_TYPE,
        "byte_size": 128,
        "privacy_class": run_checkpoint.CHECKPOINT_PRIVACY_CLASS,
        "paired_event_type": paired_event_type,
    }
    paired = paired_event_type if paired_event_type is not None else "none"
    return run_events.build_event(
        run_id=RUN_ID,
        sequence=sequence,
        event_type=run_checkpoint.CHECKPOINT_EVENT_TYPE,
        payload=payload,
        idempotency_key=f"checkpoint:{sha}:{paired}",
        recorded_at=recorded_at,
        previous_digest=previous_digest,
    )


def _assert_bounded_projection_error(excinfo: pytest.ExceptionInfo[ProjectionError]) -> None:
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


def test_golden_replay_matches_expected_bytes():
    if not GOLDEN_BASE_PATH.is_file():
        pytest.fail(f"missing golden base fixture: {GOLDEN_BASE_PATH}")
    if not GOLDEN_EXPECTED_PATH.is_file():
        pytest.fail(f"missing golden expected fixture: {GOLDEN_EXPECTED_PATH}")
    base = json.loads(GOLDEN_BASE_PATH.read_text())
    expected_obj = json.loads(GOLDEN_EXPECTED_PATH.read_text())
    expected_obj["projector_version"] = PROJECTOR_VERSION
    expected_obj["kind"] = "work"
    expected = encode_snapshot_bytes(expected_obj)
    projection = project_run_snapshot(base, _golden_events(), journal_present=True)
    assert projection.to_bytes() == expected


def test_repeated_projection_is_byte_deterministic():
    base = _minimal_base_snapshot()
    events = _golden_events()
    first = project_run_snapshot(base, events, journal_present=True)
    second = project_run_snapshot(base, events, journal_present=True)
    assert first.to_bytes() == second.to_bytes()


def test_empty_events_no_journal_preserves_base_status_and_deep_copies():
    base = _minimal_base_snapshot(status="planning")
    base["failure"] = {"detail": "pending"}
    projection = project_run_snapshot(base, [], journal_present=False)
    assert isinstance(projection, RunProjection)
    assert projection.status == "planning"
    assert projection.snapshot["status"] == "planning"
    assert projection.snapshot["failure"] == base["failure"]
    assert projection.snapshot["failure"] is not base["failure"]
    assert projection.last_sequence == 0
    assert projection.last_event_digest is None
    assert projection.journal_present is False


def test_empty_created_journal_reports_presence_with_zero_sequence_and_null_digest():
    base = _minimal_base_snapshot(status="dispatching")
    projection = project_run_snapshot(base, [], journal_present=True)
    assert projection.status == "dispatching"
    assert projection.journal_present is True
    assert projection.last_sequence == 0
    assert projection.last_event_digest is None
    assert projection.snapshot["journal_last_sequence"] == 0
    assert projection.snapshot["journal_last_event_digest"] is None


def test_golden_sequence_derives_ok_sequence_and_digest():
    projection = project_run_snapshot(_minimal_base_snapshot(), _golden_events(), journal_present=True)
    assert projection.status == "ok"
    assert projection.last_sequence == 6
    assert projection.last_event_digest == GOLDEN_FINAL_DIGEST
    assert projection.snapshot["status"] == "ok"
    assert projection.snapshot["projector_version"] == PROJECTOR_VERSION
    assert projection.snapshot["journal_last_sequence"] == 6
    assert projection.snapshot["journal_last_event_digest"] == GOLDEN_FINAL_DIGEST


def test_run_failed_derives_timeout():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_failed(status="timeout"),
        journal_present=True,
    )
    assert projection.status == "timeout"
    assert projection.snapshot["status"] == "timeout"


def test_run_failed_derives_failed():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_failed(status="failed"),
        journal_present=True,
    )
    assert projection.status == "failed"
    assert projection.snapshot["status"] == "failed"


def test_run_failed_bad_or_missing_status_raises_bounded_event_payload_error():
    for status in ("bad-status", None):
        with pytest.raises(EventPayloadError) as excinfo:
            project_run_snapshot(
                _minimal_base_snapshot(),
                _events_ending_with_failed(status=status),
                journal_present=True,
            )
        _assert_bounded_projection_error(excinfo)


def test_run_interrupted_derives_canceled_and_rejects_wrong_status():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_interrupted(status="canceled"),
        journal_present=True,
    )
    assert projection.status == "canceled"
    with pytest.raises(EventPayloadError) as excinfo:
        project_run_snapshot(
            _minimal_base_snapshot(),
            _events_ending_with_interrupted(status="interrupted"),
            journal_present=True,
        )
    _assert_bounded_projection_error(excinfo)


def test_unknown_base_key_raises_bounded_unknown_snapshot_field_error():
    base = _minimal_base_snapshot()
    base["unknown_field"] = "nope"
    with pytest.raises(UnknownSnapshotFieldError) as excinfo:
        project_run_snapshot(base, [], journal_present=False)
    _assert_bounded_projection_error(excinfo)
    assert "unknown_field" in excinfo.value.diagnostic


def test_forged_approval_snapshot_decision_state_fails_closed():
    base = _minimal_base_snapshot()
    base["approval_reference"] = {
        "approval_id": "approval-1",
        "decision_state": "garbage",
    }

    with pytest.raises(ProjectionInputError, match="invalid decision_state"):
        project_run_snapshot(base, [], journal_present=False)


def test_approval_pause_resume_events_are_all_mapped_and_reconstruct_status():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    requested = _build_event(
        2,
        "approval.requested",
        {"approval_id": "a-1", "source": "daily", "contract_fingerprint": "fp-1"},
        "approval-requested-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    paused = _build_event(
        3,
        "run.paused",
        {"approval_id": "a-1", "reason": "approval-required"},
        "pause-1",
        "2026-07-27T15:30:47.000000Z",
        requested["event_digest"],
    )
    granted = _build_event(
        4,
        "approval.granted",
        {
            "approval_id": "a-1",
            "decided_at": "2026-07-27T15:31:00+00:00",
            "decision_state": "approved",
        },
        "approval-granted-1",
        "2026-07-27T15:31:00.000000Z",
        paused["event_digest"],
    )
    consumed = _build_event(
        5,
        "approval.consumed",
        {"approval_id": "a-1", "consuming_run_id": RUN_ID},
        "approval-consumed-1",
        "2026-07-27T15:31:01.000000Z",
        granted["event_digest"],
    )
    resumed = _build_event(
        6,
        "run.resumed",
        {"approval_id": "a-1"},
        "resume-1",
        "2026-07-27T15:31:02.000000Z",
        consumed["event_digest"],
    )
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        [created, requested, paused, granted, consumed, resumed],
        journal_present=True,
    )
    assert projection.status == "running"
    assert projection.snapshot["status"] == "running"
    assert projection.last_sequence == 6
    assert projection.last_event_digest == resumed["event_digest"]


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "approval.rejected",
            {
                "approval_id": "a-1",
                "decided_at": "2026-07-27T15:31:00+00:00",
                "decision_state": "rejected",
            },
        ),
        (
            "approval.held",
            {
                "approval_id": "a-1",
                "decided_at": "2026-07-27T15:31:00+00:00",
                "decision_state": "held",
            },
        ),
    ],
)
def test_terminal_approval_decisions_preserve_compatibility_status(event_type, payload):
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    paused = _build_event(
        2,
        "run.paused",
        {"approval_id": "a-1", "reason": "approval-required"},
        "pause-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    decision = _build_event(
        3,
        event_type,
        payload,
        f"{event_type}-1",
        "2026-07-27T15:31:00.000000Z",
        paused["event_digest"],
    )
    projection = project_run_snapshot(_minimal_base_snapshot(), [created, paused, decision], journal_present=True)
    assert projection.status == "running"
    assert projection.snapshot["status"] == "running"


@pytest.mark.parametrize(
    "scenario",
    ["gap", "duplicate", "broken_digest", "mixed_run_id"],
)
def test_chain_errors_raise_event_chain_error(scenario):
    with pytest.raises(EventChainError) as excinfo:
        project_run_snapshot(_minimal_base_snapshot(), _chain_events(scenario), journal_present=True)
    _assert_bounded_projection_error(excinfo)


def test_invalid_raw_envelope_raises_event_chain_error():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    invalid = {**created, "event_type": "run.not-real"}
    with pytest.raises(EventChainError) as excinfo:
        project_run_snapshot(_minimal_base_snapshot(), [created, invalid], journal_present=True)
    _assert_bounded_projection_error(excinfo)


def test_invalid_envelope_diagnostic_excludes_rejected_value():
    private_marker = "private-marker-568"
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    invalid = {**created, "recorded_at": private_marker}
    with pytest.raises(EventChainError) as excinfo:
        project_run_snapshot(_minimal_base_snapshot(), [invalid], journal_present=True)
    _assert_bounded_projection_error(excinfo)
    assert private_marker not in excinfo.value.diagnostic


def test_dataclasses_replace_mutation_of_typed_run_event_raises_event_chain_error():
    event = _golden_events()[0]
    mutated = replace(event, event_type="run.planning.started")
    with pytest.raises(EventChainError) as excinfo:
        project_run_snapshot(_minimal_base_snapshot(), [mutated], journal_present=True)
    _assert_bounded_projection_error(excinfo)


def test_full_field_fixture_preserves_deep_equality_and_copies_nested_values():
    base = _full_base_snapshot()
    assert len(PRESERVED_FIELDS) == 59
    assert DERIVED_FIELDS == {
        "status",
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    }
    assert EVENT_STATUS == {
        "run.planning.started": "planning",
        "run.dispatching.started": "dispatching",
        "run.dispatch.requested": "dispatching",
        "run.dispatch.observed": "dispatching",
        "run.dispatch.completed": "dispatching",
        "run.dispatch.failed": "dispatching",
        "run.result-processing.started": "result-processing",
        "run.synthesis.started": "synthesizing",
        "run.synthesis.completed": "handoff",
        "run.artifact_collection.started": "artifact-collection",
        "run.paused": "running",
        "run.resumed": "running",
        "run.recovery.started": "running",
    }
    projection = project_run_snapshot(base, [], journal_present=False)
    for field in PRESERVED_FIELDS:
        assert field in projection.snapshot
        assert projection.snapshot[field] == base[field]
    assert projection.snapshot["failure"] is not base["failure"]
    assert projection.snapshot["recovery_history"] is not base["recovery_history"]
    assert projection.snapshot["active_seats"] is not base["active_seats"]
    assert projection.snapshot["code_graph_brief"] is not base["code_graph_brief"]
    assert projection.snapshot["approval_reference"] is not base["approval_reference"]
    assert set(projection.snapshot.keys()) <= OWNED_FIELDS


def test_identified_dispatch_fact_does_not_advance_aggregate_status():
    completed = _build_event(
        1,
        "run.dispatch.completed",
        {"seat": "coder", "attempt": 1, "detail": "completed"},
        "dispatch-worker-1",
        RECORDED_AT,
        None,
    )

    projection = project_run_snapshot(_minimal_base_snapshot("result-processing"), [completed], journal_present=True)

    assert projection.snapshot["status"] == "result-processing"


def test_reprojection_is_byte_idempotent():
    base = _minimal_base_snapshot()
    events = _golden_events()
    first = project_run_snapshot(base, events, journal_present=True)
    second = project_run_snapshot(first.snapshot, events, journal_present=True)
    assert first.to_bytes() == second.to_bytes()


def test_to_bytes_and_encode_snapshot_bytes_match_sorted_two_space_json():
    projection = project_run_snapshot(_minimal_base_snapshot(), _golden_events(), journal_present=True)
    expected = (json.dumps(projection.snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert projection.to_bytes() == expected
    assert encode_snapshot_bytes(projection.snapshot) == expected


def test_object_and_circular_preserved_values_raise_bounded_snapshot_encoding_error():
    for value in (object(), {"self": None}):
        base = _minimal_base_snapshot()
        base["failure"] = value
        if isinstance(value, dict):
            value["self"] = value
        with pytest.raises(SnapshotEncodingError) as excinfo:
            project_run_snapshot(base, [], journal_present=False)
        _assert_bounded_projection_error(excinfo)
        diagnostic = excinfo.value.diagnostic
        assert "object" not in diagnostic
        assert "circular" not in diagnostic
        assert repr(value) not in diagnostic


def test_non_boolean_journal_present_raises_projection_input_error():
    for value in (None, 1):
        with pytest.raises(ProjectionInputError) as excinfo:
            project_run_snapshot(_minimal_base_snapshot(), [], journal_present=value)
        _assert_bounded_projection_error(excinfo)


def test_checkpoint_event_is_status_neutral_and_advances_chain_cursor():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    checkpoint = _build_checkpoint_event(
        2,
        created["event_digest"],
        paired_event_type="run.planning.started",
        recorded_at="2026-07-27T15:30:46.000000Z",
    )
    projection = project_run_snapshot(_minimal_base_snapshot(), [created, checkpoint], journal_present=True)
    assert projection.status == "started"
    assert projection.snapshot["status"] == "started"
    assert projection.last_sequence == 2
    assert projection.last_event_digest == checkpoint["event_digest"]
    assert projection.snapshot["journal_last_sequence"] == 2
    assert projection.snapshot["journal_last_event_digest"] == checkpoint["event_digest"]


def test_checkpoint_first_event_derives_base_status():
    checkpoint = _build_checkpoint_event(1, None, paired_event_type="run.created")
    projection = project_run_snapshot(_minimal_base_snapshot(status="planning"), [checkpoint], journal_present=True)
    assert projection.status == "planning"
    assert projection.snapshot["status"] == "planning"
    assert projection.last_sequence == 1
    assert projection.last_event_digest == checkpoint["event_digest"]


def test_checkpoint_after_unmapped_status_preserves_last_mapped_status():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    planning = _build_event(
        2,
        "run.planning.started",
        {"detail": "planning"},
        "plan-2",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    unmapped_checkpoint = _build_checkpoint_event(
        3,
        planning["event_digest"],
        paired_event_type=None,
        recorded_at="2026-07-27T15:30:47.000000Z",
    )
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        [created, planning, unmapped_checkpoint],
        journal_present=True,
    )
    assert projection.status == "planning"
    assert projection.snapshot["status"] == "planning"
    assert projection.last_sequence == 3
    assert projection.last_event_digest == unmapped_checkpoint["event_digest"]


def test_projector_preserves_research_kind() -> None:
    base = _minimal_base_snapshot()
    base["kind"] = "research"

    projected = project_run_snapshot(base, [], journal_present=False).snapshot

    assert projected["kind"] == "research"


def test_projector_defaults_missing_kind_to_work() -> None:
    projected = project_run_snapshot(_minimal_base_snapshot(), [], journal_present=False).snapshot

    assert projected["kind"] == "work"


def test_projector_version_is_six_and_replaces_stale_v5():
    base = _minimal_base_snapshot()
    base["projector_version"] = 5

    projection = project_run_snapshot(base, [], journal_present=False)

    assert PROJECTOR_VERSION == 6
    assert projection.snapshot["projector_version"] == 6
    assert projection.snapshot["kind"] == "work"


def _events_ending_with_completed(*, status: str | None) -> list[dict]:
    """Build a two-event sequence ending with run.completed with the given status."""
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    payload = {"detail": "done"}
    if status is not None:
        payload["status"] = status
    completed = _build_event(
        2,
        "run.completed",
        payload,
        "completed-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    return [created, completed]


def test_run_completed_derives_ok():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_completed(status="ok"),
        journal_present=True,
    )
    assert projection.status == "ok"
    assert projection.snapshot["status"] == "ok"


def test_run_completed_derives_dry_run():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_completed(status="dry-run"),
        journal_present=True,
    )
    assert projection.status == "dry-run"
    assert projection.snapshot["status"] == "dry-run"


def test_run_completed_bad_or_missing_status_raises_bounded_event_payload_error():
    for status in ("bad-status", None):
        with pytest.raises(EventPayloadError) as excinfo:
            project_run_snapshot(
                _minimal_base_snapshot(),
                _events_ending_with_completed(status=status),
                journal_present=True,
            )
        _assert_bounded_projection_error(excinfo)


def test_run_failed_derives_incomplete():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_failed(status="incomplete"),
        journal_present=True,
    )
    assert projection.status == "incomplete"
    assert projection.snapshot["status"] == "incomplete"


def test_artifact_collection_started_derives_artifact_collection():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    artifact = _build_event(
        2,
        "run.artifact_collection.started",
        {"detail": "collecting"},
        "artifact-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        [created, artifact],
        journal_present=True,
    )
    assert projection.status == "artifact-collection"
    assert projection.snapshot["status"] == "artifact-collection"


@pytest.mark.parametrize(
    "event_type",
    ["control.requested", "control.observed", "control.failed"],
)
def test_control_events_are_status_neutral_and_advance_chain_cursor(event_type):
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    if event_type == "control.requested":
        payload = {
            "op": "steer",
            "worker": "coder",
            "text_digest": "b" * 64,
            "request_id": "req-1",
        }
    elif event_type == "control.observed":
        payload = {"op": "steer", "worker": "coder", "request_id": "req-1", "detail": "ok"}
    else:
        payload = {
            "op": "steer",
            "worker": "coder",
            "request_id": "req-1",
            "error_class": "transport_rejected",
            "detail_digest": "a" * 64,
        }
    control = _build_event(
        2,
        event_type,
        payload,
        f"{event_type}:req-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    projection = project_run_snapshot(_minimal_base_snapshot(), [created, control], journal_present=True)
    assert projection.status == "started"
    assert projection.last_sequence == 2
    assert projection.last_event_digest == control["event_digest"]


def test_run_completed_derives_research_completed():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_completed(status="completed"),
        journal_present=True,
    )
    assert projection.status == "completed"


def test_run_interrupted_derives_research_cancelled():
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        _events_ending_with_interrupted(status="cancelled"),
        journal_present=True,
    )
    assert projection.status == "cancelled"


def test_run_recovery_started_derives_running():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    recovered = _build_event(
        2,
        "run.recovery.started",
        {"detail": "research-reopened"},
        "recover-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    projection = project_run_snapshot(
        _minimal_base_snapshot(),
        [created, recovered],
        journal_present=True,
    )
    assert projection.status == "running"
