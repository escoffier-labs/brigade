"""Run-owned seat-health policy: summaries, retry dispositions, and quarantine.

``seat_health`` answers whether a seat is healthy. This module decides what a
run does with that answer and with typed worker failures: summaries for
``run.json``, same-seat-once retry bounds, quarantine, and operator messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import seat_health
from .worker_failure import (
    FailureClass,
    RetryDisposition,
    WorkerFailure,
    normalized_failure,
    worker_result_failure,
)

HEALTH_SUMMARY_SCHEMA = "brigade.seat_health_summary.v1"
TRANSPORT_ROUTING_SCHEMA = "brigade.transport_routing.v1"


@dataclass
class SeatQuarantineState:
    """Per-run seat quarantine and same-seat retry counters."""

    quarantined: set[str] = field(default_factory=set)
    same_seat_retries: dict[str, int] = field(default_factory=dict)
    retry_decisions: list[dict[str, object]] = field(default_factory=list)

    def is_quarantined(self, seat: str) -> bool:
        return seat in self.quarantined

    def quarantine(self, seat: str) -> None:
        self.quarantined.add(seat)

    def same_seat_attempts(self, seat: str) -> int:
        return self.same_seat_retries.get(seat, 0)

    def record_same_seat_retry(self, seat: str) -> int:
        count = self.same_seat_retries.get(seat, 0) + 1
        self.same_seat_retries[seat] = count
        return count


@dataclass(frozen=True)
class RetryDecision:
    """What to do after a failed worker attempt has been persisted."""

    action: str
    retry: RetryDisposition
    failure_class: str
    detail: str

    @property
    def should_retry_same_seat(self) -> bool:
        return self.action == "same-seat-once"

    @property
    def should_quarantine(self) -> bool:
        return self.action in {"quarantine", "fallback", "never", "after-remediation"}


def failure_for_worker_result(result: Any) -> WorkerFailure | None:
    """Normalize a live WorkerResult into the closed taxonomy."""
    if result.ok:
        return None
    status = result.status
    return normalized_failure(
        failure_phase=result.failure_phase,
        failure_kind=result.failure_kind,
        detail=result.detail,
        timed_out=result.timed_out,
        status=status if isinstance(status, str) else "",
    )


def decide_retry(
    failure: WorkerFailure | None,
    *,
    seat: str,
    state: SeatQuarantineState,
) -> RetryDecision:
    """Apply the closed retry dispositions for one persisted failed attempt."""
    attempt = sum(1 for entry in state.retry_decisions if entry.get("seat") == seat) + 1

    def record(action: str, failure_class: str, detail: str) -> RetryDecision:
        state.retry_decisions.append(
            {
                "attempt": attempt,
                "seat": seat,
                "failure_kind": failure_class,
                "decision": action,
                "reason": detail,
            }
        )
        return RetryDecision(action=action, retry=retry, failure_class=failure_class, detail=detail)

    if failure is None:
        retry = RetryDisposition.NEVER
        state.quarantine(seat)
        return record(
            "never",
            FailureClass.UNCLASSIFIED.value,
            f"seat {seat} failed without a typed failure; quarantined",
        )
    retry = failure.retry
    failure_class = failure.failure_class.value
    if state.is_quarantined(seat):
        return record(
            "quarantine",
            failure_class,
            f"seat {seat} is already quarantined for this run",
        )
    if retry is RetryDisposition.SAME_SEAT_ONCE:
        if state.same_seat_attempts(seat) >= 1:
            state.quarantine(seat)
            return record(
                "quarantine",
                failure_class,
                f"seat {seat} already used its one same-seat retry; quarantined",
            )
        state.record_same_seat_retry(seat)
        return record(
            "same-seat-once",
            failure_class,
            f"seat {seat} may retry once after re-probe [{failure_class}]",
        )
    state.quarantine(seat)
    if retry is RetryDisposition.FALLBACK:
        action = "fallback"
        detail = f"seat {seat} quarantined [{failure_class}]; use a declared fallback only"
    elif retry is RetryDisposition.AFTER_REMEDIATION:
        action = "after-remediation"
        detail = f"seat {seat} quarantined [{failure_class}]; operator remediation required"
    else:
        action = "never"
        detail = f"seat {seat} quarantined [{failure_class}]; no retry"
    return record(action, failure_class, detail)


def seat_health_summary(
    results: Sequence[seat_health.SeatHealthResult] | None,
    *,
    receipt: str = "seat-health.json",
    routing_decisions: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, object] | None:
    """Build the additive ``brigade.seat_health_summary.v1`` block for run.json."""
    if results is None:
        return None
    healthy = sum(1 for result in results if result.status == "healthy")
    degraded = sum(1 for result in results if result.status == "degraded")
    unhealthy = sum(1 for result in results if result.status == "unhealthy")
    fallbacks_selected = 0
    if routing_decisions is not None:
        fallbacks_selected = sum(1 for decision in routing_decisions if decision.get("outcome") == "fallback")
    return {
        "schema": HEALTH_SUMMARY_SCHEMA,
        "receipt": receipt,
        "healthy": healthy,
        "degraded": degraded,
        "unhealthy": unhealthy,
        "fallbacks_selected": fallbacks_selected,
    }


def worker_failure_summary(
    results: Iterable[Any] | Iterable[Mapping[str, Any]],
) -> dict[str, object] | None:
    """Aggregate typed worker failures for run.json."""
    classes: dict[str, int] = {}
    seats: list[str] = []
    domain = "infrastructure"
    for result in results:
        if isinstance(result, Mapping):
            if result.get("ok") is True:
                continue
            failure = worker_result_failure(dict(result))
            raw_seat = result.get("worker")
            seat = raw_seat if isinstance(raw_seat, str) else "unknown"
        else:
            if result.ok:
                continue
            failure = failure_for_worker_result(result)
            seat = result.worker
        if failure is None:
            key = FailureClass.UNCLASSIFIED.value
        else:
            key = failure.failure_class.value
            domain = failure.domain.value
        classes[key] = classes.get(key, 0) + 1
        if seat not in seats:
            seats.append(seat)
    if not classes:
        return None
    return {
        "domain": domain,
        "classes": dict(sorted(classes.items())),
        "seats": seats,
    }


def format_incomplete_warning(
    results: Sequence[Any],
    *,
    routing_decisions: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Human summary naming seats and typed failure classes."""
    failed = [result for result in results if not result.ok]
    if not failed:
        return "warning: run incomplete: worker(s) failed; see worker-results.json"
    parts: list[str] = []
    for result in failed:
        failure = failure_for_worker_result(result)
        label = failure.failure_class.value if failure is not None else FailureClass.UNCLASSIFIED.value
        parts.append(f"{result.worker} failed [{label}]")
    lines = [f"warning: run incomplete: {'; '.join(parts)}"]
    if routing_decisions:
        for decision in routing_decisions:
            if decision.get("outcome") != "fallback":
                continue
            requested = decision.get("requested_seat")
            effective = decision.get("effective_seat")
            cause = decision.get("typed_cause") or "unclassified"
            if isinstance(requested, str) and isinstance(effective, str):
                lines.append(f"declared fallback {effective} selected for {requested} [{cause}]")
    lines.append("receipt: worker-results.json")
    return "\n".join(lines)


def format_reroute_summary(routing_decisions: Sequence[Mapping[str, Any]] | None) -> str | None:
    """One-line run summary naming every admission reroute, or None."""
    if not routing_decisions:
        return None
    parts: list[str] = []
    for decision in routing_decisions:
        requested = decision.get("requested_seat")
        effective = decision.get("effective_seat")
        cause = decision.get("typed_cause") or "unclassified"
        outcome = decision.get("outcome") or "fallback"
        if outcome == "fallback" and isinstance(requested, str) and isinstance(effective, str):
            parts.append(f"{requested} -> {effective} [{cause}]")
        elif isinstance(requested, str):
            parts.append(f"{requested} skipped [{cause}]")
    if not parts:
        return None
    return f"note: seat reroute: {'; '.join(parts)}"


def format_worker_seat_resolution(*, requested: str, effective: str, typed_cause: str) -> str:
    """Direct ``--worker`` notice when a declared fallback is selected."""
    return f"warning: requested worker {requested}; effective seat {effective} [{typed_cause}]"


def transport_fallback_decision(
    *,
    requested_transport: str,
    effective_transport: str,
    cause: str,
    detail: str,
) -> dict[str, object]:
    """Queryable routing decision for app-server → exec (and similar) fallbacks."""
    return {
        "schema": TRANSPORT_ROUTING_SCHEMA,
        "requested_transport": requested_transport,
        "effective_transport": effective_transport,
        "outcome": "fallback",
        "typed_cause": cause,
        "detail": detail[:240],
    }


def apply_health_fields(
    payload: dict[str, object],
    *,
    health: Mapping[str, object] | None = None,
    worker_failures: Mapping[str, object] | None = None,
    transport_routing: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Copy additive health fields onto a run payload without dropping legacy keys."""
    if health is not None:
        payload["health"] = dict(health)
    if worker_failures is not None:
        payload["worker_failure_summary"] = dict(worker_failures)
    if transport_routing is not None:
        payload["transport_routing"] = dict(transport_routing)
    return payload
