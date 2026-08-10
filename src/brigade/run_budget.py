"""Run budget ceilings, durable lifecycle events, and restart-safe projection.

Issue #593 first enforceable slice.

Taxonomy decision (locked for this slice):
  Budget exhaustion and operator cancellation are **terminal lifecycle / policy
  states**, not members of the #576 worker ``FailureClass`` enum. They surface
  as run-owned kinds ``budget-exhausted`` and ``operator-cancelled``. They must
  not be treated as infrastructure under #580, and they must not be forced into
  the worker-failure taxonomy (which would incorrectly neutralize preventable
  planning or policy exhaustion).

Enforceable dimensions (hard gates before new work starts):
  - ``wall_clock_seconds``
  - ``worker_dispatch_count``

Observed dimensions (recorded when an adapter reports reliable data; never
universal hard gates in this slice):
  - ``model_call_count``, ``tool_call_count``
  - ``input_tokens``, ``output_tokens``
  - ``estimated_cost_micros``, ``provider_usage_units``

Child frame / durable child-run allocation belongs to #594 and is out of scope.
Missing child-allocation fields default to "no child budget" and remain
readable.

Lifecycle events use stable idempotency keys and at-least-once recovery
semantics. Payloads carry safe summaries and classified artifact digests only
— never raw diagnostics, prompts, tool args, or provider bodies.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from brigade import run_events
from brigade.run_events import MAX_DIAGNOSTIC_LEN, MAX_IDEMPOTENCY_KEY_LEN

RUN_BUDGET_SCHEMA = "brigade.run_budget.v1"
RUN_BUDGET_SCHEMA_VERSION = 1

# Run-owned policy terminal kinds (not FailureClass / not infrastructure).
POLICY_KIND_BUDGET_EXHAUSTED = "budget-exhausted"
POLICY_KIND_OPERATOR_CANCELLED = "operator-cancelled"
POLICY_TERMINAL_KINDS = frozenset(
    {
        POLICY_KIND_BUDGET_EXHAUSTED,
        POLICY_KIND_OPERATOR_CANCELLED,
    }
)

ENFORCEABLE_DIMENSIONS = frozenset({"wall_clock_seconds", "worker_dispatch_count"})
OBSERVED_DIMENSIONS = frozenset(
    {
        "model_call_count",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "estimated_cost_micros",
        "provider_usage_units",
    }
)
ALL_DIMENSIONS = ENFORCEABLE_DIMENSIONS | OBSERVED_DIMENSIONS

BUDGET_MODES = frozenset({"enforceable", "observed"})
USAGE_SOURCES = frozenset({"estimated", "provider_reconciled"})
TRANSPORT_CAPABILITIES = frozenset({"interrupt", "process_cancel", "none"})
TRANSPORT_RESULTS = frozenset(
    {
        "interrupted",
        "not_interruptible",
        "partial",
        "unsupported",
        "requested",
    }
)
REASON_CLASSES = frozenset(
    {
        "threshold",
        "reservation_denied",
        "exhausted",
        "operator_cancel",
        "budget_cancel",
        "usage_reconcile",
        "unknown_dimension",
        "schema_incompatible",
    }
)

EVENT_THRESHOLD = "run_budget.threshold_reached"
EVENT_DENIED = "run_budget.reservation_denied"
EVENT_EXHAUSTED = "run_budget.exhausted"
EVENT_CANCEL_REQUESTED = "run_budget.cancel_requested"
EVENT_CANCELLED = "run_budget.cancelled"
EVENT_RECONCILED = "run_budget.usage_reconciled"

RUN_BUDGET_EVENT_TYPES = frozenset(
    {
        EVENT_THRESHOLD,
        EVENT_DENIED,
        EVENT_EXHAUSTED,
        EVENT_CANCEL_REQUESTED,
        EVENT_CANCELLED,
        EVENT_RECONCILED,
    }
)

DEFAULT_THRESHOLD_PCT = 80

# Payload allowlists mirrored into run_events.EVENT_TYPES.
RUN_BUDGET_EVENT_PAYLOADS: dict[str, frozenset[str]] = {
    EVENT_THRESHOLD: frozenset({"dimension", "mode", "declared", "used", "remaining", "threshold_pct", "reason_class"}),
    EVENT_DENIED: frozenset({"dimension", "mode", "declared", "used", "remaining", "request_id", "reason_class"}),
    EVENT_EXHAUSTED: frozenset({"dimension", "mode", "declared", "used", "remaining", "reason_class"}),
    EVENT_CANCEL_REQUESTED: frozenset({"request_id", "reason_class", "transport_capability", "dimension"}),
    EVENT_CANCELLED: frozenset(
        {
            "request_id",
            "reason_class",
            "transport_capability",
            "transport_result",
            "active_remaining",
            "dimension",
        }
    ),
    EVENT_RECONCILED: frozenset(
        {
            "dimension",
            "mode",
            "usage_source",
            "estimated_used",
            "provider_used",
            "used",
            "request_id",
            "reason_class",
        }
    ),
}


class BudgetError(RuntimeError):
    """Bounded run-budget failure with a stable ``code``."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostic = _bound(message)


class BudgetCompatibilityError(BudgetError):
    """Unknown schema version or dimension; recovery must stop."""


class BudgetPolicyError(BudgetError):
    """Enforceable ceiling blocked new work (denial or exhaustion)."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        dimension: str,
        exhausted: bool,
        request_id: str,
    ) -> None:
        super().__init__(message, code=code)
        self.dimension = dimension
        self.exhausted = exhausted
        self.request_id = request_id


def _bound(msg: str) -> str:
    if len(msg) <= MAX_DIAGNOSTIC_LEN:
        return msg
    return msg[: MAX_DIAGNOSTIC_LEN - 1] + "\u2026"


def _idempotency_key(*parts: str) -> str:
    key = ":".join(parts)
    if len(key) <= MAX_IDEMPOTENCY_KEY_LEN:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    keep = MAX_IDEMPOTENCY_KEY_LEN - 17
    return f"{key[:keep]}:{digest}"


def mode_for_dimension(dimension: str) -> str:
    if dimension in ENFORCEABLE_DIMENSIONS:
        return "enforceable"
    if dimension in OBSERVED_DIMENSIONS:
        return "observed"
    raise BudgetCompatibilityError(
        _bound(f"unknown budget dimension {dimension!r}"),
        code="unknown_dimension",
    )


@dataclass(frozen=True)
class RunBudgetDeclaration:
    """Declared ceilings and observation policy for one run."""

    schema_version: int = RUN_BUDGET_SCHEMA_VERSION
    wall_clock_seconds: int | None = None
    worker_dispatch_count: int | None = None
    threshold_pct: int = DEFAULT_THRESHOLD_PCT
    # Observed-only declared caps (informational; never hard-gated here).
    observed_caps: Mapping[str, int | None] = field(default_factory=dict)

    def ceiling(self, dimension: str) -> int | None:
        if dimension == "wall_clock_seconds":
            return self.wall_clock_seconds
        if dimension == "worker_dispatch_count":
            return self.worker_dispatch_count
        if dimension in OBSERVED_DIMENSIONS:
            return self.observed_caps.get(dimension)
        raise BudgetCompatibilityError(
            _bound(f"unknown budget dimension {dimension!r}"),
            code="unknown_dimension",
        )

    def to_payload(self) -> dict[str, Any]:
        observed = {key: self.observed_caps.get(key) for key in sorted(OBSERVED_DIMENSIONS)}
        return {
            "schema": RUN_BUDGET_SCHEMA,
            "schema_version": self.schema_version,
            "ceilings": {
                "wall_clock_seconds": self.wall_clock_seconds,
                "worker_dispatch_count": self.worker_dispatch_count,
            },
            "observed": observed,
            "threshold_pct": self.threshold_pct,
        }


def parse_declaration(payload: Mapping[str, Any] | None) -> RunBudgetDeclaration:
    """Parse a run_budget declaration; unknown versions/dimensions fail closed."""
    if payload is None:
        return RunBudgetDeclaration()
    if not isinstance(payload, Mapping):
        raise BudgetCompatibilityError("run_budget declaration must be an object", code="schema_incompatible")
    schema = payload.get("schema")
    if schema is not None and schema != RUN_BUDGET_SCHEMA:
        raise BudgetCompatibilityError(
            _bound(f"unsupported run_budget schema {schema!r}"),
            code="schema_incompatible",
        )
    version = payload.get("schema_version", RUN_BUDGET_SCHEMA_VERSION)
    if version != RUN_BUDGET_SCHEMA_VERSION:
        raise BudgetCompatibilityError(
            _bound(f"unsupported run_budget schema_version {version!r}"),
            code="schema_incompatible",
        )
    ceilings = payload.get("ceilings", {})
    if ceilings is None:
        ceilings = {}
    if not isinstance(ceilings, Mapping):
        raise BudgetCompatibilityError("ceilings must be an object", code="schema_incompatible")
    unknown_ceilings = set(ceilings.keys()) - ENFORCEABLE_DIMENSIONS
    # Child allocation keys are owned by #594; tolerate absence, refuse unknown.
    if unknown_ceilings:
        raise BudgetCompatibilityError(
            _bound(f"unknown enforceable budget dimensions: {sorted(unknown_ceilings)}"),
            code="unknown_dimension",
        )
    observed_raw = payload.get("observed", {})
    if observed_raw is None:
        observed_raw = {}
    if not isinstance(observed_raw, Mapping):
        raise BudgetCompatibilityError("observed must be an object", code="schema_incompatible")
    unknown_observed = set(observed_raw.keys()) - OBSERVED_DIMENSIONS
    if unknown_observed:
        raise BudgetCompatibilityError(
            _bound(f"unknown observed budget dimensions: {sorted(unknown_observed)}"),
            code="unknown_dimension",
        )
    threshold = payload.get("threshold_pct", DEFAULT_THRESHOLD_PCT)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not (1 <= threshold <= 100):
        raise BudgetCompatibilityError("threshold_pct must be an integer 1..100", code="schema_incompatible")

    def _opt_pos(value: object, *, field_name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BudgetCompatibilityError(
                _bound(f"{field_name} must be a positive integer when set"),
                code="schema_incompatible",
            )
        return value

    observed_caps = {
        key: _opt_pos(observed_raw.get(key), field_name=f"observed.{key}")
        for key in OBSERVED_DIMENSIONS
        if key in observed_raw
    }
    return RunBudgetDeclaration(
        schema_version=int(version),
        wall_clock_seconds=_opt_pos(ceilings.get("wall_clock_seconds"), field_name="wall_clock_seconds"),
        worker_dispatch_count=_opt_pos(ceilings.get("worker_dispatch_count"), field_name="worker_dispatch_count"),
        threshold_pct=threshold,
        observed_caps=observed_caps,
    )


def declaration_from_verification_budget(budget: Mapping[str, Any] | None) -> RunBudgetDeclaration:
    """Bridge #500 VerificationBudget into a run_budget declaration.

    ``wall_clock_seconds`` / ``worker_dispatch_count`` are preferred when set.
    Otherwise ``latency_seconds`` maps to the enforceable wall-clock ceiling so
    existing contracts become enforceable without a template rewrite.
    ``token_budget`` stays observed-only.
    """
    if not isinstance(budget, Mapping):
        return RunBudgetDeclaration()
    wall = budget.get("wall_clock_seconds")
    if wall is None:
        wall = budget.get("latency_seconds")
    dispatch = budget.get("worker_dispatch_count")
    observed: dict[str, int | None] = {}
    token = budget.get("token_budget")
    if isinstance(token, int) and not isinstance(token, bool) and token >= 0:
        # Informational observed cap on total tokens (input+output not split here).
        observed["input_tokens"] = token
    payload = {
        "schema": RUN_BUDGET_SCHEMA,
        "schema_version": RUN_BUDGET_SCHEMA_VERSION,
        "ceilings": {
            "wall_clock_seconds": wall if isinstance(wall, int) and not isinstance(wall, bool) else None,
            "worker_dispatch_count": (
                dispatch if isinstance(dispatch, int) and not isinstance(dispatch, bool) else None
            ),
        },
        "observed": observed,
        "threshold_pct": DEFAULT_THRESHOLD_PCT,
    }
    return parse_declaration(payload)


@dataclass(frozen=True)
class CancelReceipt:
    request_id: str
    reason_class: str
    transport_capability: str
    transport_result: str
    active_remaining: int
    dimension: str


@dataclass(frozen=True)
class BudgetProjection:
    """Restart-safe allocation view rebuilt from declaration + journal events."""

    declaration: RunBudgetDeclaration
    used: dict[str, int] = field(default_factory=dict)
    estimated_used: dict[str, int] = field(default_factory=dict)
    provider_used: dict[str, int] = field(default_factory=dict)
    thresholds_emitted: frozenset[tuple[str, int]] = field(default_factory=frozenset)
    exhausted_dimensions: frozenset[str] = field(default_factory=frozenset)
    denied_request_ids: frozenset[str] = field(default_factory=frozenset)
    cancel_receipts: tuple[CancelReceipt, ...] = ()
    terminal_policy: str | None = None  # budget_exhausted | operator_cancelled

    def remaining(self, dimension: str) -> int | None:
        ceiling = self.declaration.ceiling(dimension)
        if ceiling is None:
            return None
        return max(0, ceiling - self.used.get(dimension, 0))

    def to_payload(self) -> dict[str, Any]:
        """Safe summary for receipts/readers — no raw diagnostics."""
        used_payload = {dim: int(self.used.get(dim, 0)) for dim in sorted(ALL_DIMENSIONS)}
        remaining_payload: dict[str, int | None] = {dim: self.remaining(dim) for dim in sorted(ENFORCEABLE_DIMENSIONS)}
        modes = {dim: mode_for_dimension(dim) for dim in sorted(ALL_DIMENSIONS)}
        return {
            "schema": RUN_BUDGET_SCHEMA,
            "schema_version": self.declaration.schema_version,
            "declaration": self.declaration.to_payload(),
            "modes": modes,
            "used": used_payload,
            "estimated_used": {k: int(v) for k, v in sorted(self.estimated_used.items())},
            "provider_used": {k: int(v) for k, v in sorted(self.provider_used.items())},
            "remaining": remaining_payload,
            "exhausted_dimensions": sorted(self.exhausted_dimensions),
            "terminal_policy": self.terminal_policy,
            "cancel_receipts": [
                {
                    "request_id": receipt.request_id,
                    "reason_class": receipt.reason_class,
                    "transport_capability": receipt.transport_capability,
                    "transport_result": receipt.transport_result,
                    "active_remaining": receipt.active_remaining,
                    "dimension": receipt.dimension,
                }
                for receipt in self.cancel_receipts
            ],
        }


def _event_type(event: Any) -> str | None:
    if isinstance(event, Mapping):
        value = event.get("event_type")
        return value if isinstance(value, str) else None
    value = getattr(event, "event_type", None)
    return value if isinstance(value, str) else None


def _event_payload(event: Any) -> Mapping[str, Any]:
    if isinstance(event, Mapping):
        payload = event.get("payload")
        return payload if isinstance(payload, Mapping) else {}
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def project_budget_state(
    declaration: RunBudgetDeclaration,
    events: Sequence[Any],
) -> BudgetProjection:
    """Rebuild allocation from durable lifecycle events (restart-safe)."""
    used: dict[str, int] = {dim: 0 for dim in ALL_DIMENSIONS}
    estimated_used: dict[str, int] = {}
    provider_used: dict[str, int] = {}
    thresholds: set[tuple[str, int]] = set()
    exhausted: set[str] = set()
    denied: set[str] = set()
    cancels: list[CancelReceipt] = []
    terminal_policy: str | None = None

    for event in events:
        event_type = _event_type(event)
        if event_type not in RUN_BUDGET_EVENT_TYPES:
            continue
        payload = _event_payload(event)
        dimension = payload.get("dimension")
        if isinstance(dimension, str) and dimension not in ALL_DIMENSIONS and dimension != "":
            raise BudgetCompatibilityError(
                _bound(f"unknown budget dimension in journal: {dimension!r}"),
                code="unknown_dimension",
            )

        if event_type == EVENT_THRESHOLD:
            if isinstance(dimension, str) and isinstance(payload.get("threshold_pct"), int):
                thresholds.add((dimension, int(payload["threshold_pct"])))
            if (
                isinstance(dimension, str)
                and dimension != "worker_dispatch_count"
                and isinstance(payload.get("used"), int)
                and not isinstance(payload.get("used"), bool)
            ):
                used[dimension] = max(used.get(dimension, 0), int(payload["used"]))
        elif event_type == EVENT_DENIED:
            request_id = payload.get("request_id")
            if isinstance(request_id, str):
                denied.add(request_id)
            if (
                isinstance(dimension, str)
                and dimension != "worker_dispatch_count"
                and isinstance(payload.get("used"), int)
                and not isinstance(payload.get("used"), bool)
            ):
                used[dimension] = max(used.get(dimension, 0), int(payload["used"]))
        elif event_type == EVENT_EXHAUSTED:
            if isinstance(dimension, str):
                exhausted.add(dimension)
                if (
                    dimension != "worker_dispatch_count"
                    and isinstance(payload.get("used"), int)
                    and not isinstance(payload.get("used"), bool)
                ):
                    used[dimension] = max(used.get(dimension, 0), int(payload["used"]))
            terminal_policy = "budget_exhausted"
        elif event_type == EVENT_CANCEL_REQUESTED:
            reason = payload.get("reason_class")
            if reason == "operator_cancel":
                terminal_policy = "operator_cancelled"
            elif reason == "budget_cancel" and terminal_policy is None:
                terminal_policy = "budget_exhausted"
        elif event_type == EVENT_CANCELLED:
            receipt = CancelReceipt(
                request_id=str(payload.get("request_id") or ""),
                reason_class=str(payload.get("reason_class") or "budget_cancel"),
                transport_capability=str(payload.get("transport_capability") or "none"),
                transport_result=str(payload.get("transport_result") or "unsupported"),
                active_remaining=int(payload["active_remaining"])
                if isinstance(payload.get("active_remaining"), int)
                and not isinstance(payload.get("active_remaining"), bool)
                else 0,
                dimension=str(payload.get("dimension") or ""),
            )
            cancels.append(receipt)
            if receipt.reason_class == "operator_cancel":
                terminal_policy = "operator_cancelled"
            elif receipt.reason_class == "budget_cancel" and terminal_policy is None:
                terminal_policy = "budget_exhausted"
        elif event_type == EVENT_RECONCILED:
            if not isinstance(dimension, str):
                continue
            usage_source = payload.get("usage_source")
            est = payload.get("estimated_used")
            if isinstance(est, int) and not isinstance(est, bool):
                estimated_used[dimension] = est
            prov = payload.get("provider_used")
            if isinstance(prov, int) and not isinstance(prov, bool):
                provider_used[dimension] = prov
            # Enforceable used counters are coordinator-owned (dispatch.requested
            # count / wall-clock elapsed). Observed dimensions take reconcile.
            if dimension in ENFORCEABLE_DIMENSIONS:
                continue
            used_val = payload.get("used")
            if isinstance(used_val, int) and not isinstance(used_val, bool):
                used[dimension] = max(used.get(dimension, 0), used_val)
            if usage_source == "provider_reconciled" and dimension in provider_used:
                used[dimension] = provider_used[dimension]

    # Enforceable dispatch usage: count durable run.dispatch.requested facts.
    dispatch_used = 0
    for event in events:
        if _event_type(event) == "run.dispatch.requested":
            dispatch_used += 1
    if dispatch_used:
        used["worker_dispatch_count"] = max(used.get("worker_dispatch_count", 0), dispatch_used)

    return BudgetProjection(
        declaration=declaration,
        used=used,
        estimated_used=estimated_used,
        provider_used=provider_used,
        thresholds_emitted=frozenset(thresholds),
        exhausted_dimensions=frozenset(exhausted),
        denied_request_ids=frozenset(denied),
        cancel_receipts=tuple(cancels),
        terminal_policy=terminal_policy,
    )


@dataclass(frozen=True)
class ReservationDecision:
    allowed: bool
    dimension: str
    request_id: str
    exhausted: bool
    projection: BudgetProjection
    events: tuple[dict[str, Any], ...]  # event_type + payload + idempotency_key triples as dicts


def evaluate_dispatch_reservation(
    projection: BudgetProjection,
    *,
    request_id: str,
    now: datetime,
    started_at: datetime | None,
    units: int = 1,
) -> ReservationDecision:
    """Decide whether a new worker dispatch may start.

    Checks wall-clock first, then dispatch count. Does not mutate projection;
    returns events the caller must append (idempotent under recovery).
    """
    if units <= 0:
        raise BudgetError("reservation units must be positive", code="reservation_invalid")
    declaration = projection.declaration
    events: list[dict[str, Any]] = []

    # Wall-clock: observed elapsed against enforceable ceiling.
    wall_ceiling = declaration.wall_clock_seconds
    if wall_ceiling is not None and started_at is not None:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        elapsed = max(0, int((now - started_at).total_seconds()))
        used = dict(projection.used)
        used["wall_clock_seconds"] = max(used.get("wall_clock_seconds", 0), elapsed)
        wall_proj = BudgetProjection(
            declaration=declaration,
            used=used,
            estimated_used=dict(projection.estimated_used),
            provider_used=dict(projection.provider_used),
            thresholds_emitted=projection.thresholds_emitted,
            exhausted_dimensions=projection.exhausted_dimensions,
            denied_request_ids=projection.denied_request_ids,
            cancel_receipts=projection.cancel_receipts,
            terminal_policy=projection.terminal_policy,
        )
        events.extend(
            _maybe_threshold_events(wall_proj, dimension="wall_clock_seconds", used=elapsed, declared=wall_ceiling)
        )
        if elapsed >= wall_ceiling:
            exhausted_payload = {
                "dimension": "wall_clock_seconds",
                "mode": "enforceable",
                "declared": wall_ceiling,
                "used": elapsed,
                "remaining": 0,
                "reason_class": "exhausted",
            }
            events.append(
                {
                    "event_type": EVENT_EXHAUSTED,
                    "payload": exhausted_payload,
                    "idempotency_key": _idempotency_key(EVENT_EXHAUSTED, "wall_clock_seconds"),
                }
            )
            denied_payload = {
                "dimension": "wall_clock_seconds",
                "mode": "enforceable",
                "declared": wall_ceiling,
                "used": elapsed,
                "remaining": 0,
                "request_id": request_id,
                "reason_class": "reservation_denied",
            }
            events.append(
                {
                    "event_type": EVENT_DENIED,
                    "payload": denied_payload,
                    "idempotency_key": _idempotency_key(EVENT_DENIED, request_id),
                }
            )
            return ReservationDecision(
                allowed=False,
                dimension="wall_clock_seconds",
                request_id=request_id,
                exhausted=True,
                projection=wall_proj,
                events=tuple(events),
            )
        projection = wall_proj

    dispatch_ceiling = declaration.worker_dispatch_count
    if dispatch_ceiling is not None:
        current = projection.used.get("worker_dispatch_count", 0)
        events.extend(
            _maybe_threshold_events(
                projection,
                dimension="worker_dispatch_count",
                used=current,
                declared=dispatch_ceiling,
            )
        )
        if current + units > dispatch_ceiling:
            remaining = max(0, dispatch_ceiling - current)
            exhausted = current >= dispatch_ceiling
            if exhausted or remaining == 0:
                events.append(
                    {
                        "event_type": EVENT_EXHAUSTED,
                        "payload": {
                            "dimension": "worker_dispatch_count",
                            "mode": "enforceable",
                            "declared": dispatch_ceiling,
                            "used": current,
                            "remaining": 0,
                            "reason_class": "exhausted",
                        },
                        "idempotency_key": _idempotency_key(EVENT_EXHAUSTED, "worker_dispatch_count"),
                    }
                )
                exhausted = True
            events.append(
                {
                    "event_type": EVENT_DENIED,
                    "payload": {
                        "dimension": "worker_dispatch_count",
                        "mode": "enforceable",
                        "declared": dispatch_ceiling,
                        "used": current,
                        "remaining": remaining,
                        "request_id": request_id,
                        "reason_class": "reservation_denied",
                    },
                    "idempotency_key": _idempotency_key(EVENT_DENIED, request_id),
                }
            )
            return ReservationDecision(
                allowed=False,
                dimension="worker_dispatch_count",
                request_id=request_id,
                exhausted=exhausted,
                projection=projection,
                events=tuple(events),
            )
        # Allowed: emit threshold for post-reserve level when crossing.
        next_used = current + units
        events.extend(
            _maybe_threshold_events(
                projection,
                dimension="worker_dispatch_count",
                used=next_used,
                declared=dispatch_ceiling,
            )
        )

    return ReservationDecision(
        allowed=True,
        dimension="worker_dispatch_count",
        request_id=request_id,
        exhausted=False,
        projection=projection,
        events=tuple(events),
    )


def _maybe_threshold_events(
    projection: BudgetProjection,
    *,
    dimension: str,
    used: int,
    declared: int,
) -> list[dict[str, Any]]:
    pct = projection.declaration.threshold_pct
    if declared <= 0:
        return []
    if used * 100 < declared * pct:
        return []
    if (dimension, pct) in projection.thresholds_emitted:
        return []
    remaining = max(0, declared - used)
    return [
        {
            "event_type": EVENT_THRESHOLD,
            "payload": {
                "dimension": dimension,
                "mode": mode_for_dimension(dimension),
                "declared": declared,
                "used": used,
                "remaining": remaining,
                "threshold_pct": pct,
                "reason_class": "threshold",
            },
            "idempotency_key": _idempotency_key(EVENT_THRESHOLD, dimension, str(pct)),
        }
    ]


def build_cancel_requested_event(
    *,
    request_id: str,
    reason_class: str,
    transport_capability: str,
    dimension: str,
) -> dict[str, Any]:
    if reason_class not in {"operator_cancel", "budget_cancel"}:
        raise BudgetError("invalid cancel reason_class", code="cancel_invalid")
    if transport_capability not in TRANSPORT_CAPABILITIES:
        raise BudgetError("invalid transport_capability", code="cancel_invalid")
    return {
        "event_type": EVENT_CANCEL_REQUESTED,
        "payload": {
            "request_id": request_id,
            "reason_class": reason_class,
            "transport_capability": transport_capability,
            "dimension": dimension,
        },
        "idempotency_key": _idempotency_key(EVENT_CANCEL_REQUESTED, request_id),
    }


def build_cancelled_event(
    *,
    request_id: str,
    reason_class: str,
    transport_capability: str,
    transport_result: str,
    active_remaining: int,
    dimension: str,
) -> dict[str, Any]:
    if transport_result not in TRANSPORT_RESULTS:
        raise BudgetError("invalid transport_result", code="cancel_invalid")
    if not isinstance(active_remaining, int) or isinstance(active_remaining, bool) or active_remaining < 0:
        raise BudgetError("active_remaining must be a non-negative integer", code="cancel_invalid")
    return {
        "event_type": EVENT_CANCELLED,
        "payload": {
            "request_id": request_id,
            "reason_class": reason_class,
            "transport_capability": transport_capability,
            "transport_result": transport_result,
            "active_remaining": active_remaining,
            "dimension": dimension,
        },
        "idempotency_key": _idempotency_key(EVENT_CANCELLED, request_id),
    }


def build_usage_reconciled_event(
    *,
    dimension: str,
    usage_source: str,
    used: int,
    request_id: str,
    estimated_used: int | None = None,
    provider_used: int | None = None,
) -> dict[str, Any]:
    if dimension not in ALL_DIMENSIONS:
        raise BudgetCompatibilityError(
            _bound(f"unknown budget dimension {dimension!r}"),
            code="unknown_dimension",
        )
    if usage_source not in USAGE_SOURCES:
        raise BudgetError("invalid usage_source", code="reconcile_invalid")
    mode = mode_for_dimension(dimension)
    # Enforceable dimensions may only reconcile as observed telemetry when an
    # adapter reports them; hard gates still own reservation.
    return {
        "event_type": EVENT_RECONCILED,
        "payload": {
            "dimension": dimension,
            "mode": mode,
            "usage_source": usage_source,
            "estimated_used": estimated_used,
            "provider_used": provider_used,
            "used": used,
            "request_id": request_id,
            "reason_class": "usage_reconcile",
        },
        "idempotency_key": _idempotency_key(EVENT_RECONCILED, dimension, usage_source, request_id),
    }


def validate_run_budget_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    """Extra closed-enum checks invoked from run_events._validate_payload."""
    if event_type not in RUN_BUDGET_EVENT_TYPES:
        return
    dimension = payload.get("dimension")
    if dimension is not None:
        if not isinstance(dimension, str) or (dimension and dimension not in ALL_DIMENSIONS):
            raise run_events.CanonicalizationError(_bound(f"invalid run_budget dimension {dimension!r}"))
    mode = payload.get("mode")
    if mode is not None and mode not in BUDGET_MODES:
        raise run_events.CanonicalizationError(_bound(f"invalid run_budget mode {mode!r}"))
    reason = payload.get("reason_class")
    if reason is not None and reason not in REASON_CLASSES:
        raise run_events.CanonicalizationError(_bound(f"invalid run_budget reason_class {reason!r}"))
    if event_type == EVENT_RECONCILED:
        source = payload.get("usage_source")
        if source not in USAGE_SOURCES:
            raise run_events.CanonicalizationError("run_budget.usage_reconciled usage_source is invalid")
    if event_type in {EVENT_CANCEL_REQUESTED, EVENT_CANCELLED}:
        cap = payload.get("transport_capability")
        if cap not in TRANSPORT_CAPABILITIES:
            raise run_events.CanonicalizationError("run_budget cancel transport_capability is invalid")
    if event_type == EVENT_CANCELLED:
        result = payload.get("transport_result")
        if result not in TRANSPORT_RESULTS:
            raise run_events.CanonicalizationError("run_budget.cancelled transport_result is invalid")
        active = payload.get("active_remaining")
        if not isinstance(active, int) or isinstance(active, bool) or active < 0:
            raise run_events.CanonicalizationError("run_budget.cancelled active_remaining is invalid")


AppendEvent = Callable[[str, Mapping[str, Any], str], Any]


@dataclass
class BudgetCoordinator:
    """Coordinator-owned remaining allocation for one run.

    Callers that hold the run lock append events through ``append_event``.
    Projection is rebuilt from the journal on ``reload`` so restarts are safe.
    """

    declaration: RunBudgetDeclaration
    append_event: AppendEvent
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    _projection: BudgetProjection = field(init=False)
    _started_at: datetime | None = None
    _events_cache: list[Any] = field(default_factory=list)
    _pending_dispatch_units: int = 0

    def __post_init__(self) -> None:
        self._projection = BudgetProjection(declaration=self.declaration)

    def set_started_at(self, started_at: datetime | None) -> None:
        self._started_at = started_at

    def reload(self, events: Sequence[Any]) -> BudgetProjection:
        self._events_cache = list(events)
        self._projection = project_budget_state(self.declaration, events)
        # Journal dispatch.requested facts supersede in-process reservations.
        self._pending_dispatch_units = 0
        return self._projection

    @property
    def projection(self) -> BudgetProjection:
        if self._pending_dispatch_units <= 0:
            return self._projection
        used = dict(self._projection.used)
        used["worker_dispatch_count"] = used.get("worker_dispatch_count", 0) + self._pending_dispatch_units
        return replace(self._projection, used=used)

    def _commit(self, event_specs: Sequence[Mapping[str, Any]]) -> None:
        for spec in event_specs:
            self.append_event(str(spec["event_type"]), dict(spec["payload"]), str(spec["idempotency_key"]))
            synthetic = {"event_type": spec["event_type"], "payload": dict(spec["payload"])}
            self._events_cache.append(synthetic)
        self._projection = project_budget_state(self.declaration, self._events_cache)

    def reserve_worker_dispatch(self, *, request_id: str, units: int = 1) -> ReservationDecision:
        """Reserve enforceable dispatch units before new work starts."""
        decision = evaluate_dispatch_reservation(
            self.projection,
            request_id=request_id,
            now=self.clock(),
            started_at=self._started_at,
            units=units,
        )
        if decision.events:
            self._commit(decision.events)
        if not decision.allowed:
            raise BudgetPolicyError(
                _bound(
                    f"run budget reservation denied for {decision.dimension}"
                    + (" (exhausted)" if decision.exhausted else "")
                ),
                code="reservation_denied" if not decision.exhausted else "budget_exhausted",
                dimension=decision.dimension,
                exhausted=decision.exhausted,
                request_id=request_id,
            )
        self._pending_dispatch_units += units
        return decision

    def request_cancel(
        self,
        *,
        request_id: str,
        reason_class: str,
        transport_capability: str,
        dimension: str,
        cancel_fn: Callable[[], tuple[str, int]] | None = None,
    ) -> CancelReceipt:
        """Best-effort cancel of active work with durable cancel receipts.

        ``cancel_fn`` returns ``(transport_result, active_remaining)``.
        """
        requested = build_cancel_requested_event(
            request_id=request_id,
            reason_class=reason_class,
            transport_capability=transport_capability,
            dimension=dimension,
        )
        self._commit([requested])
        transport_result = "unsupported"
        active_remaining = 0
        if cancel_fn is not None:
            transport_result, active_remaining = cancel_fn()
        cancelled = build_cancelled_event(
            request_id=request_id,
            reason_class=reason_class,
            transport_capability=transport_capability,
            transport_result=transport_result,
            active_remaining=active_remaining,
            dimension=dimension,
        )
        self._commit([cancelled])
        receipt = CancelReceipt(
            request_id=request_id,
            reason_class=reason_class,
            transport_capability=transport_capability,
            transport_result=transport_result,
            active_remaining=active_remaining,
            dimension=dimension,
        )
        return receipt

    def reconcile_usage(
        self,
        *,
        dimension: str,
        usage_source: str,
        used: int,
        request_id: str,
        estimated_used: int | None = None,
        provider_used: int | None = None,
    ) -> None:
        """Record observed usage; estimated and provider values stay distinct."""
        if dimension in ENFORCEABLE_DIMENSIONS and mode_for_dimension(dimension) == "enforceable":
            # Wall-clock / dispatch remain coordinator-owned; still allow
            # observed reconciliation records labeled with their mode.
            pass
        else:
            # Observed dimensions are never hard-gated here.
            _ = mode_for_dimension(dimension)
        event = build_usage_reconciled_event(
            dimension=dimension,
            usage_source=usage_source,
            used=used,
            request_id=request_id,
            estimated_used=estimated_used,
            provider_used=provider_used,
        )
        self._commit([event])


def terminal_status_for_policy(terminal_policy: str | None) -> tuple[str, str]:
    """Return ``(run.json status, failure_kind)`` for a policy terminal."""
    if terminal_policy == "operator_cancelled":
        return "canceled", POLICY_KIND_OPERATOR_CANCELLED
    if terminal_policy == "budget_exhausted":
        return "failed", POLICY_KIND_BUDGET_EXHAUSTED
    raise BudgetError(f"unknown terminal policy {terminal_policy!r}", code="terminal_invalid")
