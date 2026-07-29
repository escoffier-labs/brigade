"""Tests for brigade.run_projector pure snapshot projection."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import run_events, run_journal
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
    UnmappedEventTypeError,
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
        "started_at": "2026-07-27T15:30:00.000000Z",
        "status_started_at": "2026-07-27T15:30:00.000000Z",
        "finished_at": None,
        "duration_seconds": 0,
        "code_graph_brief": {"entries": []},
        "drift_impact_brief": {"entries": []},
        "evidence_brief": {"entries": []},
        "brief_budget": 100,
        "route": "route-id",
        "skill_route_policy": "auto",
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


def _assert_bounded_projection_error(excinfo: pytest.ExceptionInfo[ProjectionError]) -> None:
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


def test_golden_replay_matches_expected_bytes():
    if not GOLDEN_BASE_PATH.is_file():
        pytest.fail(f"missing golden base fixture: {GOLDEN_BASE_PATH}")
    if not GOLDEN_EXPECTED_PATH.is_file():
        pytest.fail(f"missing golden expected fixture: {GOLDEN_EXPECTED_PATH}")
    base = json.loads(GOLDEN_BASE_PATH.read_text())
    expected = GOLDEN_EXPECTED_PATH.read_bytes()
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


def test_registered_unmapped_run_paused_raises_bounded_unmapped_event_type_error():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    paused = _build_event(
        2,
        "run.paused",
        {"approval_id": "a-1", "reason": "wait"},
        "pause-1",
        "2026-07-27T15:30:46.000000Z",
        created["event_digest"],
    )
    with pytest.raises(UnmappedEventTypeError) as excinfo:
        project_run_snapshot(_minimal_base_snapshot(), [created, paused], journal_present=True)
    _assert_bounded_projection_error(excinfo)


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


def test_dataclasses_replace_mutation_of_typed_run_event_raises_event_chain_error():
    event = _golden_events()[0]
    mutated = replace(event, event_type="run.planning.started")
    with pytest.raises(EventChainError) as excinfo:
        project_run_snapshot(_minimal_base_snapshot(), [mutated], journal_present=True)
    _assert_bounded_projection_error(excinfo)


def test_full_field_fixture_preserves_deep_equality_and_copies_nested_values():
    base = _full_base_snapshot()
    assert len(PRESERVED_FIELDS) == 41
    assert DERIVED_FIELDS == {
        "status",
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    }
    assert EVENT_STATUS == {
        "run.planning.started": "planning",
        "run.dispatch.requested": "dispatching",
        "run.dispatch.completed": "result-processing",
        "run.synthesis.started": "synthesizing",
        "run.synthesis.completed": "handoff",
    }
    projection = project_run_snapshot(base, [], journal_present=False)
    for field in PRESERVED_FIELDS:
        assert field in projection.snapshot
        assert projection.snapshot[field] == base[field]
    assert projection.snapshot["failure"] is not base["failure"]
    assert projection.snapshot["active_seats"] is not base["active_seats"]
    assert projection.snapshot["code_graph_brief"] is not base["code_graph_brief"]
    assert set(projection.snapshot.keys()) <= OWNED_FIELDS


def test_reprojection_is_byte_idempotent():
    base = _minimal_base_snapshot()
    events = _golden_events()
    first = project_run_snapshot(base, events, journal_present=True)
    second = project_run_snapshot(first.snapshot, events, journal_present=True)
    assert first.to_bytes() == second.to_bytes()


def test_to_bytes_and_encode_snapshot_bytes_match_sorted_two_space_json():
    projection = project_run_snapshot(_minimal_base_snapshot(), _golden_events(), journal_present=True)
    expected = (
        json.dumps(projection.snapshot, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
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
