"""Tests for run budget enforcement and lifecycle events (#593)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brigade import outcome, run_budget, run_events, run_projector, worker_failure
from brigade.receipt_schema import RUN_EVENT_SCHEMA, RUN_EVENT_SCHEMA_VERSION

RUN_ID = "20260810-210000-budget593"
RECORDED_AT = "2026-08-10T21:00:00.000000Z"


def _build_event(sequence: int, event_type: str, payload: dict, key: str, recorded_at: str, previous: str | None):
    return run_events.build_event(
        run_id=RUN_ID,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        idempotency_key=key,
        recorded_at=recorded_at,
        previous_digest=previous,
    )


@pytest.mark.parametrize(
    "event_type",
    sorted(run_budget.RUN_BUDGET_EVENT_TYPES),
)
def test_run_budget_event_types_registered_with_closed_payloads(event_type):
    assert event_type in run_events.EVENT_TYPES
    assert run_events.EVENT_TYPES[event_type] == run_budget.RUN_BUDGET_EVENT_PAYLOADS[event_type]


def test_run_budget_events_reject_raw_diagnostic_payload_keys():
    with pytest.raises(run_events.CanonicalizationError) as exc:
        run_events.build_event(
            run_id=RUN_ID,
            sequence=1,
            event_type="run_budget.exhausted",
            payload={
                "dimension": "wall_clock_seconds",
                "mode": "enforceable",
                "declared": 30,
                "used": 30,
                "remaining": 0,
                "reason_class": "exhausted",
                "stack_trace": "traceback",
            },
            idempotency_key="run_budget.exhausted:wall_clock_seconds",
            recorded_at=RECORDED_AT,
            previous_digest=None,
        )
    assert "forbidden" in str(exc.value) or "unknown payload" in str(exc.value)
    assert len(str(exc.value)) <= run_events.MAX_DIAGNOSTIC_LEN


def test_run_budget_events_are_status_neutral_in_projector():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    exhausted = _build_event(
        2,
        "run_budget.exhausted",
        {
            "dimension": "worker_dispatch_count",
            "mode": "enforceable",
            "declared": 1,
            "used": 1,
            "remaining": 0,
            "reason_class": "exhausted",
        },
        "run_budget.exhausted:worker_dispatch_count",
        "2026-08-10T21:00:01.000000Z",
        created["event_digest"],
    )
    projection = run_projector.project_run_snapshot(
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "started",
            "task": "fixture",
            "dry_run": False,
            "read_only": False,
        },
        [created, exhausted],
        journal_present=True,
    )
    assert projection.status == "started"
    assert projection.last_sequence == 2


def test_wall_clock_and_dispatch_enforced_before_new_work():
    declaration = run_budget.RunBudgetDeclaration(wall_clock_seconds=10, worker_dispatch_count=1)
    started = datetime(2026, 8, 10, 21, 0, 0, tzinfo=timezone.utc)
    projection = run_budget.BudgetProjection(declaration=declaration)

    # First dispatch allowed.
    first = run_budget.evaluate_dispatch_reservation(
        projection,
        request_id="req-1",
        now=started + timedelta(seconds=1),
        started_at=started,
        units=1,
    )
    assert first.allowed is True

    # After one dispatch is accounted, second is denied/exhausted.
    used = dict(projection.used)
    used["worker_dispatch_count"] = 1
    after_one = run_budget.BudgetProjection(declaration=declaration, used=used)
    second = run_budget.evaluate_dispatch_reservation(
        after_one,
        request_id="req-2",
        now=started + timedelta(seconds=2),
        started_at=started,
        units=1,
    )
    assert second.allowed is False
    assert second.exhausted is True
    assert second.dimension == "worker_dispatch_count"
    assert any(spec["event_type"] == "run_budget.reservation_denied" for spec in second.events)
    assert any(spec["event_type"] == "run_budget.exhausted" for spec in second.events)

    # Wall-clock exhaustion blocks even with dispatch remaining.
    wall_decl = run_budget.RunBudgetDeclaration(wall_clock_seconds=5, worker_dispatch_count=10)
    wall = run_budget.evaluate_dispatch_reservation(
        run_budget.BudgetProjection(declaration=wall_decl),
        request_id="req-wall",
        now=started + timedelta(seconds=5),
        started_at=started,
        units=1,
    )
    assert wall.allowed is False
    assert wall.dimension == "wall_clock_seconds"
    assert wall.exhausted is True


def test_threshold_denial_exhaustion_cancel_reconcile_idempotent_under_recovery():
    recorded: list[tuple[str, str]] = []

    def append_event(event_type: str, payload: dict, idempotency_key: str):
        recorded.append((event_type, idempotency_key))
        return {"event_type": event_type, "payload": payload, "idempotency_key": idempotency_key}

    declaration = run_budget.RunBudgetDeclaration(
        worker_dispatch_count=2,
        threshold_pct=50,
    )
    started = datetime(2026, 8, 10, 21, 0, 0, tzinfo=timezone.utc)
    clock_times = [
        started + timedelta(seconds=1),
        started + timedelta(seconds=2),
        started + timedelta(seconds=3),
    ]
    clock_iter = iter(clock_times)

    coordinator = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
        clock=lambda: next(clock_iter, started + timedelta(seconds=9)),
    )
    coordinator.set_started_at(started)

    coordinator.reserve_worker_dispatch(request_id="req-a")
    # Threshold should fire once at 50% (1/2).
    threshold_keys = [key for etype, key in recorded if etype == "run_budget.threshold_reached"]
    assert threshold_keys == ["run_budget.threshold_reached:worker_dispatch_count:50"]

    coordinator.reserve_worker_dispatch(request_id="req-b")
    with pytest.raises(run_budget.BudgetPolicyError) as denied:
        coordinator.reserve_worker_dispatch(request_id="req-c")
    assert denied.value.exhausted is True

    # Replay the same denial/exhaustion keys — idempotent event specs.
    projection = coordinator.projection
    replay = run_budget.evaluate_dispatch_reservation(
        projection,
        request_id="req-c",
        now=started + timedelta(seconds=4),
        started_at=started,
        units=1,
    )
    assert {spec["idempotency_key"] for spec in replay.events} <= {
        "run_budget.exhausted:worker_dispatch_count",
        "run_budget.reservation_denied:req-c",
        "run_budget.threshold_reached:worker_dispatch_count:50",
    }

    receipt = coordinator.request_cancel(
        request_id="cancel-1",
        reason_class="budget_cancel",
        transport_capability="process_cancel",
        dimension="worker_dispatch_count",
        cancel_fn=lambda: ("partial", 1),
    )
    assert receipt.transport_capability == "process_cancel"
    assert receipt.transport_result == "partial"
    assert receipt.active_remaining == 1

    coordinator.reconcile_usage(
        dimension="input_tokens",
        usage_source="estimated",
        used=100,
        request_id="recon-est",
        estimated_used=100,
    )
    coordinator.reconcile_usage(
        dimension="input_tokens",
        usage_source="provider_reconciled",
        used=90,
        request_id="recon-prov",
        estimated_used=100,
        provider_used=90,
    )

    # Restart-safe projection from the in-memory event cache.
    rebuilt = run_budget.project_budget_state(declaration, coordinator._events_cache)
    assert rebuilt.terminal_policy == "budget_exhausted"
    assert rebuilt.estimated_used["input_tokens"] == 100
    assert rebuilt.provider_used["input_tokens"] == 90
    assert rebuilt.used["input_tokens"] == 90  # provider wins for observed
    assert len(rebuilt.cancel_receipts) == 1
    assert rebuilt.cancel_receipts[0].active_remaining == 1

    # Re-append with same idempotency keys is safe at the coordinator event-spec layer.
    cancel_again = run_budget.build_cancelled_event(
        request_id="cancel-1",
        reason_class="budget_cancel",
        transport_capability="process_cancel",
        transport_result="partial",
        active_remaining=1,
        dimension="worker_dispatch_count",
    )
    assert cancel_again["idempotency_key"] == "run_budget.cancelled:cancel-1"


def test_observed_dimensions_are_labeled_and_not_hard_gated():
    declaration = run_budget.RunBudgetDeclaration(
        worker_dispatch_count=5,
        observed_caps={"input_tokens": 10},
    )
    projection = run_budget.BudgetProjection(
        declaration=declaration,
        used={"input_tokens": 100, "worker_dispatch_count": 0},
    )
    # Observed overage does not deny dispatch.
    decision = run_budget.evaluate_dispatch_reservation(
        projection,
        request_id="obs-1",
        now=datetime(2026, 8, 10, 21, 0, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 8, 10, 21, 0, 0, tzinfo=timezone.utc),
        units=1,
    )
    assert decision.allowed is True
    assert run_budget.mode_for_dimension("input_tokens") == "observed"
    assert run_budget.mode_for_dimension("model_call_count") == "observed"
    assert run_budget.mode_for_dimension("wall_clock_seconds") == "enforceable"


def test_unknown_dimension_and_schema_version_fail_closed():
    with pytest.raises(run_budget.BudgetCompatibilityError) as exc:
        run_budget.parse_declaration(
            {
                "schema": run_budget.RUN_BUDGET_SCHEMA,
                "schema_version": 99,
                "ceilings": {},
            }
        )
    assert exc.value.code == "schema_incompatible"
    assert len(exc.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN

    with pytest.raises(run_budget.BudgetCompatibilityError):
        run_budget.parse_declaration(
            {
                "schema": run_budget.RUN_BUDGET_SCHEMA,
                "schema_version": 1,
                "ceilings": {"child_frame_count": 3},
            }
        )

    with pytest.raises(run_budget.BudgetCompatibilityError):
        run_budget.project_budget_state(
            run_budget.RunBudgetDeclaration(),
            [
                {
                    "event_type": "run_budget.exhausted",
                    "payload": {
                        "dimension": "child_frame_count",
                        "mode": "enforceable",
                        "declared": 1,
                        "used": 1,
                        "remaining": 0,
                        "reason_class": "exhausted",
                    },
                }
            ],
        )


def test_verification_budget_bridge_maps_latency_to_wall_clock_tokens_observed():
    declaration = run_budget.declaration_from_verification_budget(
        {
            "latency_seconds": 45,
            "token_budget": 1000,
            "worker_dispatch_count": 3,
        }
    )
    assert declaration.wall_clock_seconds == 45
    assert declaration.worker_dispatch_count == 3
    assert declaration.observed_caps.get("input_tokens") == 1000

    explicit = run_budget.declaration_from_verification_budget(
        {
            "latency_seconds": 45,
            "wall_clock_seconds": 90,
            "worker_dispatch_count": 2,
        }
    )
    assert explicit.wall_clock_seconds == 90


def test_budget_exhaustion_and_operator_cancel_are_policy_terminals_not_infra():
    assert worker_failure.run_failure_is_infrastructure_at_read_time("budget-exhausted", "dispatch") is False
    assert worker_failure.run_failure_is_infrastructure_at_read_time("operator-cancelled", "dispatch") is False
    # capacity-exhausted remains infrastructure (provider/seat capacity), distinct from policy budget.
    assert worker_failure.run_failure_is_infrastructure_at_read_time("capacity-exhausted", "dispatch") is True

    assert (
        outcome.run_capture_signal_value(
            "failed",
            infrastructure_only=False,
            verifier_failed=False,
        )
        == -1
    )
    assert "budget-exhausted" in worker_failure.RUN_POLICY_TERMINAL_FAILURE_KINDS
    assert "operator-cancelled" in worker_failure.RUN_POLICY_TERMINAL_FAILURE_KINDS

    # Worker taxonomy still excludes canceled statuses.
    assert (
        worker_failure.normalized_failure(
            status="canceled",
            failure_kind="operator-cancelled",
            failure_phase="dispatch",
            detail="operator cancelled",
        )
        is None
    )


def test_missing_child_allocation_defaults_remain_readable():
    declaration = run_budget.parse_declaration(
        {
            "schema": run_budget.RUN_BUDGET_SCHEMA,
            "schema_version": 1,
            "ceilings": {"wall_clock_seconds": 30},
        }
    )
    projection = run_budget.project_budget_state(declaration, [])
    payload = projection.to_payload()
    assert payload["remaining"]["wall_clock_seconds"] == 30
    assert payload["remaining"]["worker_dispatch_count"] is None
    assert payload["terminal_policy"] is None
    # No child allocation keys required.
    assert "child" not in payload["declaration"]["ceilings"]


def test_estimated_and_provider_usage_remain_distinct_in_projection_payload():
    declaration = run_budget.RunBudgetDeclaration()
    events = [
        {
            "event_type": "run_budget.usage_reconciled",
            "payload": {
                "dimension": "estimated_cost_micros",
                "mode": "observed",
                "usage_source": "estimated",
                "estimated_used": 5000,
                "provider_used": None,
                "used": 5000,
                "request_id": "e1",
                "reason_class": "usage_reconcile",
            },
        },
        {
            "event_type": "run_budget.usage_reconciled",
            "payload": {
                "dimension": "estimated_cost_micros",
                "mode": "observed",
                "usage_source": "provider_reconciled",
                "estimated_used": 5000,
                "provider_used": 4200,
                "used": 4200,
                "request_id": "e2",
                "reason_class": "usage_reconcile",
            },
        },
    ]
    projection = run_budget.project_budget_state(declaration, events)
    assert projection.estimated_used["estimated_cost_micros"] == 5000
    assert projection.provider_used["estimated_cost_micros"] == 4200
    assert projection.used["estimated_cost_micros"] == 4200
    payload = projection.to_payload()
    assert payload["estimated_used"]["estimated_cost_micros"] == 5000
    assert payload["provider_used"]["estimated_cost_micros"] == 4200


def test_dispatch_requested_facts_drive_restart_safe_dispatch_usage():
    declaration = run_budget.RunBudgetDeclaration(worker_dispatch_count=2)
    events = [
        {"event_type": "run.dispatch.requested", "payload": {"seat": "a", "attempt": 1, "detail": ""}},
        {"event_type": "run.dispatch.requested", "payload": {"seat": "b", "attempt": 1, "detail": ""}},
    ]
    projection = run_budget.project_budget_state(declaration, events)
    assert projection.used["worker_dispatch_count"] == 2
    assert projection.remaining("worker_dispatch_count") == 0
    denied = run_budget.evaluate_dispatch_reservation(
        projection,
        request_id="after-restart",
        now=datetime(2026, 8, 10, 21, 0, 5, tzinfo=timezone.utc),
        started_at=datetime(2026, 8, 10, 21, 0, 0, tzinfo=timezone.utc),
        units=1,
    )
    assert denied.allowed is False
    assert denied.exhausted is True


def test_build_event_rejects_invalid_run_budget_reason_class():
    with pytest.raises(run_events.CanonicalizationError):
        run_events.build_event(
            run_id=RUN_ID,
            sequence=1,
            event_type="run_budget.threshold_reached",
            payload={
                "dimension": "wall_clock_seconds",
                "mode": "enforceable",
                "declared": 10,
                "used": 8,
                "remaining": 2,
                "threshold_pct": 80,
                "reason_class": "not-a-reason",
            },
            idempotency_key="bad-reason",
            recorded_at=RECORDED_AT,
            previous_digest=None,
        )


def test_terminal_status_helpers():
    assert run_budget.terminal_status_for_policy("budget_exhausted") == (
        "failed",
        "budget-exhausted",
    )
    assert run_budget.terminal_status_for_policy("operator_cancelled") == (
        "canceled",
        "operator-cancelled",
    )


def test_schema_constants_stable():
    assert run_budget.RUN_BUDGET_SCHEMA == "brigade.run_budget.v1"
    assert run_budget.RUN_BUDGET_SCHEMA_VERSION == 1
    assert RUN_EVENT_SCHEMA == "brigade.run_event.v1"
    assert RUN_EVENT_SCHEMA_VERSION == 1


def test_parallel_same_stage_reservations_respect_dispatch_ceiling():
    """Two threads observing the same used count must not both exceed the ceiling."""
    import threading

    recorded: list[tuple[str, str]] = []
    lock = threading.Lock()

    def append_event(event_type: str, payload: dict, idempotency_key: str):
        with lock:
            recorded.append((event_type, idempotency_key))
        return {"event_type": event_type, "payload": payload, "idempotency_key": idempotency_key}

    declaration = run_budget.RunBudgetDeclaration(worker_dispatch_count=1)
    started = datetime(2026, 8, 10, 21, 0, 0, tzinfo=timezone.utc)
    coordinator = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
        clock=lambda: started + timedelta(seconds=1),
    )
    coordinator.set_started_at(started)

    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(seat: str) -> None:
        barrier.wait()
        request_id = run_budget.dispatch_reservation_request_id(seat, 1)
        try:
            coordinator.reserve_worker_dispatch(request_id=request_id)
            results.append(f"ok:{seat}")
        except run_budget.BudgetPolicyError:
            results.append(f"denied:{seat}")

    threads = [threading.Thread(target=worker, args=("alpha",)), threading.Thread(target=worker, args=("beta",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for item in results if item.startswith("ok:")) == 1
    assert any(item.startswith("denied:") for item in results)
    assert coordinator.projection.used["worker_dispatch_count"] == 1


def test_stable_reservation_identity_is_idempotent_across_retry_and_restart():
    """Crash/retry before durable requested must not double-reserve the same dispatch."""
    recorded: list[dict] = []

    def append_event(event_type: str, payload: dict, idempotency_key: str):
        recorded.append({"event_type": event_type, "payload": dict(payload), "idempotency_key": idempotency_key})
        return recorded[-1]

    declaration = run_budget.RunBudgetDeclaration(worker_dispatch_count=2)
    started = datetime(2026, 8, 10, 21, 0, 0, tzinfo=timezone.utc)
    coordinator = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
        clock=lambda: started + timedelta(seconds=1),
    )
    coordinator.set_started_at(started)

    request_id = run_budget.dispatch_reservation_request_id("coder", 1)
    first = coordinator.reserve_worker_dispatch(request_id=request_id)
    assert first.allowed is True and first.replayed is False
    assert coordinator.projection.used["worker_dispatch_count"] == 1

    # In-process retry of the same stable identity must not consume another unit.
    replay = coordinator.reserve_worker_dispatch(request_id=request_id)
    assert replay.allowed is True and replay.replayed is True
    assert coordinator.projection.used["worker_dispatch_count"] == 1

    # Simulate crash before run.dispatch.requested: rebuild from durable events only.
    # Pending units are lost; the same stable identity re-reserves exactly once.
    restarted = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
        clock=lambda: started + timedelta(seconds=2),
    )
    restarted.set_started_at(started)
    restarted.reload(recorded)  # no dispatch.requested yet
    assert restarted.projection.used["worker_dispatch_count"] == 0

    after_restart = restarted.reserve_and_record_dispatch(
        request_id=request_id,
        seat="coder",
        attempt=1,
        record_requested=lambda: 1,
    )
    assert after_restart == 1
    assert restarted.projection.used["worker_dispatch_count"] == 1

    # Second restart sees durable requested and treats the identity as already reserved.
    final = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
        clock=lambda: started + timedelta(seconds=3),
    )
    final.set_started_at(started)
    final.reload(list(restarted._events_cache))
    assert final.projection.used["worker_dispatch_count"] == 1
    replayed = final.reserve_worker_dispatch(request_id=request_id)
    assert replayed.replayed is True
    assert final.projection.used["worker_dispatch_count"] == 1

    # Crash after durable requested must not mint a second reservation for the
    # still-open seat/attempt when reserve_and_record_dispatch is replayed.
    again = final.reserve_and_record_dispatch(
        request_id=request_id,
        seat="coder",
        attempt=1,
        record_requested=lambda: (_ for _ in ()).throw(AssertionError("must not re-record")),
    )
    assert again == 1
    assert final.projection.used["worker_dispatch_count"] == 1


def test_reserve_and_record_rolls_back_when_durable_write_fails():
    recorded: list[dict] = []

    def append_event(event_type: str, payload: dict, idempotency_key: str):
        recorded.append({"event_type": event_type, "payload": dict(payload), "idempotency_key": idempotency_key})
        return recorded[-1]

    declaration = run_budget.RunBudgetDeclaration(worker_dispatch_count=1)
    started = datetime(2026, 8, 10, 21, 0, 0, tzinfo=timezone.utc)
    coordinator = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
        clock=lambda: started + timedelta(seconds=1),
    )
    coordinator.set_started_at(started)
    request_id = run_budget.dispatch_reservation_request_id("coder", 1)

    with pytest.raises(OSError, match="disk full"):
        coordinator.reserve_and_record_dispatch(
            request_id=request_id,
            seat="coder",
            attempt=1,
            record_requested=lambda: (_ for _ in ()).throw(OSError("disk full")),
        )
    assert coordinator.projection.used.get("worker_dispatch_count", 0) == 0

    recorded_attempt = coordinator.reserve_and_record_dispatch(
        request_id=request_id,
        seat="coder",
        attempt=1,
        record_requested=lambda: 1,
    )
    assert recorded_attempt == 1
    assert coordinator.projection.used["worker_dispatch_count"] == 1


def test_operator_cancelled_live_ctrl_c_kind_is_not_infrastructure():
    assert worker_failure.run_failure_is_infrastructure_at_read_time("operator-cancelled", "dispatch") is False
    assert (
        outcome.run_capture_signal_value(
            "canceled",
            infrastructure_only=False,
            verifier_failed=False,
        )
        == 0
    )
    # Even if a reader incorrectly set infrastructure_only, canceled is outside
    # the #580 neutralization status set.
    assert (
        outcome.run_capture_signal_value(
            "canceled",
            infrastructure_only=True,
            verifier_failed=False,
        )
        == 0
    )
