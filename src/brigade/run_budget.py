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

Hard ceilings apply only when a run carries a persisted ``run_budget``
declaration or ``verification_contract.budget`` with those fields. Ordinary
``brigade run``, dogfood, and model-trial paths without a declaration remain
unbounded for backward compatibility (no invented defaults, CLI flags, or
timeout→budget mapping).

Observed dimensions (recorded when an adapter reports reliable data; never
universal hard gates in this slice):
  - ``model_call_count``, ``tool_call_count``
  - ``input_tokens``, ``output_tokens`` (explicit split dimensions only)
  - ``estimated_cost_micros``, ``provider_usage_units``

Legacy VerificationContract ``token_budget`` is an **aggregate** token ceiling
(paired with receipt ``tokens_used``). It is preserved as declaration
``token_budget`` and must not be reinterpreted as ``input_tokens``.

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
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from brigade import run_events
from brigade.run_events import MAX_DIAGNOSTIC_LEN, MAX_IDEMPOTENCY_KEY_LEN

RUN_BUDGET_SCHEMA = "brigade.run_budget.v1"
RUN_BUDGET_SCHEMA_VERSION = 1
RUN_BUDGET_PROJECTION_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "declaration",
        "modes",
        "used",
        "estimated_used",
        "provider_used",
        "remaining",
        "exhausted_dimensions",
        "terminal_policy",
        "cancel_receipts",
    }
)

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
TRANSPORT_CAPABILITIES = frozenset({"interrupt", "process_cancel", "none", "mixed"})
TRANSPORT_RESULTS = frozenset(
    {
        "interrupted",
        "not_interruptible",
        "partial",
        "unsupported",
        "requested",
        "still_active",
        "error",
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

# VerificationBudget fields bridged into run_budget. Unknown keys (including
# child-allocation dimensions owned by #594) fail closed rather than drop.
VERIFICATION_BUDGET_FIELDS = frozenset(
    {
        "latency_seconds",
        "token_budget",
        "wall_clock_seconds",
        "worker_dispatch_count",
        "input_tokens",
        "output_tokens",
    }
)
VERIFICATION_BUDGET_META_FIELDS = frozenset({"schema", "schema_version"})

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
            "active_seats",
            "outcomes",
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
    # Aggregate #500 token ceiling (receipt tokens_used). Not input_tokens.
    token_budget: int | None = None
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
        payload: dict[str, Any] = {
            "schema": RUN_BUDGET_SCHEMA,
            "schema_version": self.schema_version,
            "ceilings": {
                "wall_clock_seconds": self.wall_clock_seconds,
                "worker_dispatch_count": self.worker_dispatch_count,
            },
            "observed": observed,
            "threshold_pct": self.threshold_pct,
        }
        if self.token_budget is not None:
            payload["token_budget"] = self.token_budget
        return payload


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
    if isinstance(version, bool) or not isinstance(version, int) or version != RUN_BUDGET_SCHEMA_VERSION:
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
    token_budget_raw = payload.get("token_budget")
    token_budget: int | None
    if token_budget_raw is None:
        token_budget = None
    elif isinstance(token_budget_raw, bool) or not isinstance(token_budget_raw, int) or token_budget_raw < 0:
        raise BudgetCompatibilityError(
            _bound("token_budget must be a non-negative integer when set"),
            code="schema_incompatible",
        )
    else:
        token_budget = token_budget_raw
    return RunBudgetDeclaration(
        schema_version=version,
        wall_clock_seconds=_opt_pos(ceilings.get("wall_clock_seconds"), field_name="wall_clock_seconds"),
        worker_dispatch_count=_opt_pos(ceilings.get("worker_dispatch_count"), field_name="worker_dispatch_count"),
        threshold_pct=threshold,
        token_budget=token_budget,
        observed_caps=observed_caps,
    )


def load_declaration_file(path: Path) -> dict[str, Any]:
    """Load one operator-supplied, versioned run-budget declaration file.

    The parsed mapping is returned verbatim after strict validation so the exact
    declaration can be persisted into every run artifact without synthesizing
    defaults or changing it into a verification-contract budget.
    """
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise BudgetCompatibilityError(
            f"could not read run budget declaration: {exc}", code="schema_incompatible"
        ) from exc
    except json.JSONDecodeError as exc:
        raise BudgetCompatibilityError("run budget declaration is not valid JSON", code="schema_incompatible") from exc
    return validate_explicit_declaration(payload)


def validate_explicit_declaration(payload: object) -> dict[str, Any]:
    """Validate a supplied declaration without filling in compatibility defaults."""
    if not isinstance(payload, Mapping):
        raise BudgetCompatibilityError("run budget declaration must be an object", code="schema_incompatible")
    required = {"schema", "schema_version"}
    missing = required - set(payload)
    if missing:
        raise BudgetCompatibilityError(
            _bound(f"run budget declaration is missing required fields: {sorted(missing)}"),
            code="schema_incompatible",
        )
    allowed = required | {"ceilings", "observed", "threshold_pct", "token_budget"}
    unknown = set(payload) - allowed
    if unknown:
        raise BudgetCompatibilityError(
            _bound(f"run budget declaration has unknown fields: {sorted(unknown)}"),
            code="schema_incompatible",
        )
    parse_declaration(payload)
    return dict(payload)


def declaration_has_budget_content(declaration: RunBudgetDeclaration) -> bool:
    """True when a declaration carries at least one known budget field."""
    if declaration.wall_clock_seconds is not None or declaration.worker_dispatch_count is not None:
        return True
    if declaration.token_budget is not None:
        return True
    return any(value is not None for value in declaration.observed_caps.values())


def declaration_from_persisted_artifact(payload: Mapping[str, Any]) -> RunBudgetDeclaration:
    """Parse a persisted run_budget declaration or its projected outer envelope.

    A projected envelope is authoritative state, so validate its schema and
    closed field set before selecting its nested declaration. A direct
    declaration remains accepted for compatibility with pre-projection runs.
    """
    if not isinstance(payload, Mapping):
        raise BudgetCompatibilityError("run_budget must be an object", code="schema_incompatible")
    if "declaration" not in payload:
        return parse_declaration(payload)

    unknown = set(payload.keys()) - RUN_BUDGET_PROJECTION_FIELDS
    if unknown:
        raise BudgetCompatibilityError(
            _bound(f"unknown persisted run_budget envelope fields: {sorted(unknown)}"),
            code="schema_incompatible",
        )
    schema = payload.get("schema")
    if schema != RUN_BUDGET_SCHEMA:
        raise BudgetCompatibilityError(
            _bound(f"unsupported persisted run_budget schema {schema!r}"),
            code="schema_incompatible",
        )
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != RUN_BUDGET_SCHEMA_VERSION:
        raise BudgetCompatibilityError(
            _bound(f"unsupported persisted run_budget schema_version {version!r}"),
            code="schema_incompatible",
        )
    declaration = payload.get("declaration")
    if not isinstance(declaration, Mapping):
        raise BudgetCompatibilityError(
            "run_budget.declaration must be an object when set",
            code="schema_incompatible",
        )
    return parse_declaration(declaration)


def declaration_from_verification_budget(budget: Mapping[str, Any] | None) -> RunBudgetDeclaration:
    """Bridge #500 VerificationBudget into a run_budget declaration.

    ``wall_clock_seconds`` / ``worker_dispatch_count`` are preferred when set.
    Otherwise ``latency_seconds`` maps to the enforceable wall-clock ceiling so
    existing contracts become enforceable without a template rewrite.
    ``token_budget`` stays observed-only and aggregate (receipt ``tokens_used``);
    it is never reinterpreted as ``input_tokens``. Explicit ``input_tokens`` /
    ``output_tokens`` caps bridge into ``observed_caps`` only when those fields
    are set on the verification budget.

    Present-but-malformed ceiling or token fields, unknown dimensions (including
    child-only keys), and unsupported schema/version markers fail closed with
    ``BudgetCompatibilityError`` rather than silently becoming undeclared.
    """
    if budget is None:
        return RunBudgetDeclaration()
    if not isinstance(budget, Mapping):
        raise BudgetCompatibilityError(
            "verification budget must be an object",
            code="schema_incompatible",
        )

    unknown = set(budget.keys()) - VERIFICATION_BUDGET_FIELDS - VERIFICATION_BUDGET_META_FIELDS
    if unknown:
        raise BudgetCompatibilityError(
            _bound(f"unknown verification budget dimensions: {sorted(unknown)}"),
            code="unknown_dimension",
        )
    if "schema" in budget and budget.get("schema") != RUN_BUDGET_SCHEMA:
        raise BudgetCompatibilityError(
            _bound(f"unsupported verification budget schema {budget.get('schema')!r}"),
            code="schema_incompatible",
        )
    version = budget.get("schema_version")
    if "schema_version" in budget and (
        isinstance(version, bool) or not isinstance(version, int) or version != RUN_BUDGET_SCHEMA_VERSION
    ):
        raise BudgetCompatibilityError(
            _bound(f"unsupported verification budget schema_version {version!r}"),
            code="schema_incompatible",
        )

    def _opt_pos(value: object, *, field_name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BudgetCompatibilityError(
                _bound(f"{field_name} must be a positive integer when set"),
                code="schema_incompatible",
            )
        return value

    def _opt_nonneg(value: object, *, field_name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BudgetCompatibilityError(
                _bound(f"{field_name} must be a non-negative integer when set"),
                code="schema_incompatible",
            )
        return value

    wall_raw = budget.get("wall_clock_seconds")
    if wall_raw is None:
        wall = _opt_pos(budget.get("latency_seconds"), field_name="latency_seconds")
    else:
        wall = _opt_pos(wall_raw, field_name="wall_clock_seconds")
    dispatch = _opt_pos(budget.get("worker_dispatch_count"), field_name="worker_dispatch_count")
    observed: dict[str, int | None] = {}
    for key in ("input_tokens", "output_tokens"):
        if key in budget:
            observed[key] = _opt_pos(budget.get(key), field_name=key)
    token_budget = _opt_nonneg(budget.get("token_budget"), field_name="token_budget")
    payload: dict[str, Any] = {
        "schema": RUN_BUDGET_SCHEMA,
        "schema_version": RUN_BUDGET_SCHEMA_VERSION,
        "ceilings": {
            "wall_clock_seconds": wall,
            "worker_dispatch_count": dispatch,
        },
        "observed": observed,
        "threshold_pct": DEFAULT_THRESHOLD_PCT,
    }
    if token_budget is not None:
        payload["token_budget"] = token_budget
    return parse_declaration(payload)


@dataclass(frozen=True)
class CancelOutcome:
    """Bounded, provider-safe cancellation observation for one seat or transport."""

    seat: str
    transport_capability: str
    transport_result: str


@dataclass(frozen=True)
class CancellationReport:
    """Observed cancellation state used to build one durable receipt."""

    outcomes: tuple[CancelOutcome, ...]
    active_seats: tuple[str, ...]

    @property
    def transport_capability(self) -> str:
        capabilities = {outcome.transport_capability for outcome in self.outcomes}
        return next(iter(capabilities)) if len(capabilities) == 1 else "mixed"

    @property
    def transport_result(self) -> str:
        results = {outcome.transport_result for outcome in self.outcomes}
        if not results:
            return "unsupported"
        if "error" in results:
            return "error"
        if self.active_seats:
            return "partial" if "interrupted" in results else "still_active"
        return "interrupted" if results == {"interrupted"} else "partial"


@dataclass(frozen=True)
class CancelReceipt:
    request_id: str
    reason_class: str
    transport_capability: str
    transport_result: str
    active_remaining: int
    dimension: str
    outcomes: tuple[CancelOutcome, ...] = ()
    active_seats: tuple[str, ...] = ()


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
                    "outcomes": [
                        {
                            "seat": outcome.seat,
                            "transport_capability": outcome.transport_capability,
                            "transport_result": outcome.transport_result,
                        }
                        for outcome in receipt.outcomes
                    ],
                    "active_seats": list(receipt.active_seats),
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
            raw_outcomes = payload.get("outcomes")
            outcomes: tuple[CancelOutcome, ...] = ()
            if isinstance(raw_outcomes, list):
                outcomes = tuple(
                    CancelOutcome(
                        seat=str(item.get("seat") or ""),
                        transport_capability=str(item.get("transport_capability") or "none"),
                        transport_result=str(item.get("transport_result") or "unsupported"),
                    )
                    for item in raw_outcomes
                    if isinstance(item, Mapping)
                )
            raw_active_seats = payload.get("active_seats")
            active_seats = (
                tuple(seat for seat in raw_active_seats if isinstance(seat, str))
                if isinstance(raw_active_seats, list)
                else ()
            )
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
                outcomes=outcomes,
                active_seats=active_seats,
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
    replayed: bool = False


def dispatch_reservation_request_id(seat: str, attempt: int) -> str:
    """Stable reservation identity for one seat attempt (retry/crash safe)."""
    if not isinstance(seat, str) or not seat:
        raise BudgetError("seat must be a non-empty string", code="reservation_invalid")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise BudgetError("attempt must be a positive integer", code="reservation_invalid")
    return f"dispatch:{seat}:{attempt}"


def parse_dispatch_reservation_request_id(request_id: str) -> tuple[str, int] | None:
    """Return ``(seat, attempt)`` when ``request_id`` uses the stable dispatch form."""
    if not isinstance(request_id, str) or not request_id.startswith("dispatch:"):
        return None
    parts = request_id.split(":")
    if len(parts) != 3:
        return None
    _, seat, attempt_raw = parts
    if not seat:
        return None
    try:
        attempt = int(attempt_raw)
    except ValueError:
        return None
    if attempt < 1:
        return None
    return seat, attempt


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
    outcomes: Sequence[CancelOutcome] = (),
    active_seats: Sequence[str] = (),
) -> dict[str, Any]:
    if transport_result not in TRANSPORT_RESULTS:
        raise BudgetError("invalid transport_result", code="cancel_invalid")
    if not isinstance(active_remaining, int) or isinstance(active_remaining, bool) or active_remaining < 0:
        raise BudgetError("active_remaining must be a non-negative integer", code="cancel_invalid")
    if (outcomes or active_seats) and active_remaining != len(active_seats):
        raise BudgetError("active_remaining must equal the observed active seat set", code="cancel_invalid")
    if len(outcomes) > 16 or len(active_seats) > 16:
        raise BudgetError("cancel outcomes exceed the bounded receipt limit", code="cancel_invalid")
    safe_outcomes = []
    for outcome in outcomes:
        if not isinstance(outcome, CancelOutcome):
            raise BudgetError("cancel outcome is invalid", code="cancel_invalid")
        if not outcome.seat or len(outcome.seat) > 80:
            raise BudgetError("cancel outcome seat is invalid", code="cancel_invalid")
        if outcome.transport_capability not in TRANSPORT_CAPABILITIES:
            raise BudgetError("cancel outcome capability is invalid", code="cancel_invalid")
        if outcome.transport_result not in TRANSPORT_RESULTS:
            raise BudgetError("cancel outcome result is invalid", code="cancel_invalid")
        safe_outcomes.append(
            {
                "seat": outcome.seat,
                "transport_capability": outcome.transport_capability,
                "transport_result": outcome.transport_result,
            }
        )
    safe_active_seats = list(active_seats)
    if len(set(safe_active_seats)) != len(safe_active_seats) or any(
        not isinstance(seat, str) or not seat or len(seat) > 80 for seat in safe_active_seats
    ):
        raise BudgetError("active seat set is invalid", code="cancel_invalid")
    payload: dict[str, Any] = {
        "request_id": request_id,
        "reason_class": reason_class,
        "transport_capability": transport_capability,
        "transport_result": transport_result,
        "active_remaining": active_remaining,
        "dimension": dimension,
    }
    if outcomes or active_seats:
        payload["active_seats"] = safe_active_seats
        payload["outcomes"] = safe_outcomes
    return {
        "event_type": EVENT_CANCELLED,
        "payload": payload,
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
        active_seats = payload.get("active_seats")
        outcomes = payload.get("outcomes")
        if active_seats is not None:
            if not isinstance(active_seats, list) or len(active_seats) > 16 or active != len(active_seats):
                raise run_events.CanonicalizationError("run_budget.cancelled active_seats is invalid")
            if len(set(active_seats)) != len(active_seats) or any(
                not isinstance(seat, str) or not seat or len(seat) > 80 for seat in active_seats
            ):
                raise run_events.CanonicalizationError("run_budget.cancelled active_seats is invalid")
        if outcomes is not None:
            if not isinstance(outcomes, list) or len(outcomes) > 16:
                raise run_events.CanonicalizationError("run_budget.cancelled outcomes is invalid")
            for outcome in outcomes:
                if not isinstance(outcome, Mapping) or set(outcome) != {
                    "seat",
                    "transport_capability",
                    "transport_result",
                }:
                    raise run_events.CanonicalizationError("run_budget.cancelled outcome is invalid")
                if (
                    not isinstance(outcome["seat"], str)
                    or not outcome["seat"]
                    or len(outcome["seat"]) > 80
                    or outcome["transport_capability"] not in TRANSPORT_CAPABILITIES
                    or outcome["transport_result"] not in TRANSPORT_RESULTS
                ):
                    raise run_events.CanonicalizationError("run_budget.cancelled outcome is invalid")


AppendEvent = Callable[[str, Mapping[str, Any], str], Any]


@dataclass
class BudgetCoordinator:
    """Coordinator-owned remaining allocation for one run.

    Callers that hold the run lock append events through ``append_event``.
    Projection is rebuilt from the journal on ``reload`` so restarts are safe.

    ``reserve_worker_dispatch`` is synchronized so parallel same-stage workers
    cannot both observe the same used count and exceed a ceiling. Reservation
    request ids are idempotent: a retry/crash replay of the same stable id does
    not double-reserve.
    """

    declaration: RunBudgetDeclaration
    append_event: AppendEvent
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    _projection: BudgetProjection = field(init=False)
    _started_at: datetime | None = None
    _events_cache: list[Any] = field(default_factory=list)
    _pending_dispatch_units: int = 0
    _reserved_request_ids: set[str] = field(default_factory=set)
    # Identities already handed to a caller in this process for external launch.
    # Distinct from ``_reserved_request_ids`` so restart can reclaim a durable
    # pending identity once, while concurrent same-seat callers allocate anew.
    _launch_claims: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        self._projection = BudgetProjection(declaration=self.declaration)

    def set_started_at(self, started_at: datetime | None) -> None:
        self._started_at = started_at

    def reload(self, events: Sequence[Any]) -> BudgetProjection:
        with self._lock:
            self._events_cache = list(events)
            self._projection = project_budget_state(self.declaration, events)
            # Journal dispatch.requested facts supersede in-process reservations.
            self._pending_dispatch_units = 0
            self._launch_claims = set()
            reserved: set[str] = set()
            for event in events:
                event_type = _event_type(event)
                payload = _event_payload(event)
                if event_type != "run.dispatch.requested":
                    continue
                seat = payload.get("seat")
                attempt = payload.get("attempt")
                if (
                    isinstance(seat, str)
                    and seat
                    and isinstance(attempt, int)
                    and not isinstance(attempt, bool)
                    and attempt > 0
                ):
                    reserved.add(dispatch_reservation_request_id(seat, attempt))
            self._reserved_request_ids = reserved
            return self._projection

    @property
    def projection(self) -> BudgetProjection:
        with self._lock:
            return self._projection_unlocked()

    def _projection_unlocked(self) -> BudgetProjection:
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

    def _reserve_unlocked(self, *, request_id: str, units: int = 1) -> ReservationDecision:
        if request_id in self._projection.denied_request_ids:
            exhausted = bool(self._projection.exhausted_dimensions)
            raise BudgetPolicyError(
                _bound(f"run budget reservation previously denied for {request_id}"),
                code="budget_exhausted" if exhausted else "reservation_denied",
                dimension=next(iter(self._projection.exhausted_dimensions), "worker_dispatch_count"),
                exhausted=exhausted,
                request_id=request_id,
            )
        if request_id in self._reserved_request_ids:
            # Idempotent replay of the same stable dispatch identity.
            return ReservationDecision(
                allowed=True,
                dimension="worker_dispatch_count",
                request_id=request_id,
                exhausted=False,
                projection=self._projection_unlocked(),
                events=(),
                replayed=True,
            )
        decision = evaluate_dispatch_reservation(
            self._projection_unlocked(),
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
        self._reserved_request_ids.add(request_id)
        self._pending_dispatch_units += units
        return decision

    def reserve_worker_dispatch(self, *, request_id: str, units: int = 1) -> ReservationDecision:
        """Reserve enforceable dispatch units before new work starts."""
        with self._lock:
            return self._reserve_unlocked(request_id=request_id, units=units)

    def _unreserve_unlocked(self, *, request_id: str, units: int = 1) -> None:
        self._reserved_request_ids.discard(request_id)
        if self._pending_dispatch_units > 0:
            self._pending_dispatch_units = max(0, self._pending_dispatch_units - units)

    def _has_durable_dispatch_requested(self, seat: str, attempt: int) -> bool:
        for event in self._events_cache:
            if _event_type(event) != "run.dispatch.requested":
                continue
            payload = _event_payload(event)
            if payload.get("seat") == seat and payload.get("attempt") == attempt:
                return True
        return False

    def _open_pending_attempt_unlocked(self, seat: str) -> int | None:
        """Return the oldest open requested attempt for ``seat``, if any."""
        pending: list[int] = []
        closed: set[int] = set()
        for event in self._events_cache:
            event_type = _event_type(event)
            payload = _event_payload(event)
            if payload.get("seat") != seat:
                continue
            attempt = payload.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                continue
            if event_type == "run.dispatch.requested":
                pending.append(attempt)
            elif event_type in {"run.dispatch.observed", "run.dispatch.completed", "run.dispatch.failed"}:
                closed.add(attempt)
        for attempt in pending:
            if attempt not in closed:
                return attempt
        return None

    def _max_attempt_unlocked(self, seat: str) -> int:
        attempts: list[int] = []
        for event in self._events_cache:
            payload = _event_payload(event)
            if payload.get("seat") != seat:
                continue
            attempt = payload.get("attempt")
            if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
                attempts.append(attempt)
        return max(attempts, default=0)

    def _ensure_wall_clock_allows_unlocked(self, *, request_id: str) -> None:
        """Deny authorization when the wall-clock ceiling is already exhausted.

        Used by restart reclaim before handing a durable pending identity back
        to the caller for external launch. Dispatch-count is not re-checked:
        the durable ``run.dispatch.requested`` already owns that unit.
        """
        if self.declaration.wall_clock_seconds is None:
            return
        wall_only = replace(
            self._projection_unlocked(), declaration=replace(self.declaration, worker_dispatch_count=None)
        )
        decision = evaluate_dispatch_reservation(
            wall_only,
            request_id=request_id,
            now=self.clock(),
            started_at=self._started_at,
            units=1,
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

    def reserve_and_record_dispatch(
        self,
        *,
        seat: str,
        allocate_attempt: Callable[[], int],
        record_requested: Callable[[int], int | None],
    ) -> int | None:
        """Atomically allocate attempt, reserve, and durably record requested.

        Attempt allocation and reservation share one lock so parallel same-seat
        callers cannot both claim attempt 1. A durable open pending identity is
        reclaimed once per process (crash/retry) only after the wall-clock
        ceiling still allows authorization; concurrent callers then allocate a
        fresh attempt. The durable ``run.dispatch.requested`` fact is written
        before unlock so external transport cannot start without it.
        """
        if not isinstance(seat, str) or not seat:
            raise BudgetError("seat must be a non-empty string", code="reservation_invalid")
        with self._lock:
            pending = self._open_pending_attempt_unlocked(seat)
            if pending is not None:
                pending_id = dispatch_reservation_request_id(seat, pending)
                if pending_id not in self._launch_claims and self._has_durable_dispatch_requested(seat, pending):
                    # Restart/recovery: reclaim the unfinished durable identity
                    # once, but never authorize launch past a wall-clock ceiling.
                    self._ensure_wall_clock_allows_unlocked(request_id=pending_id)
                    self._launch_claims.add(pending_id)
                    self._reserved_request_ids.add(pending_id)
                    return pending

            attempt = allocate_attempt()
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise BudgetError("attempt must be a positive integer", code="reservation_invalid")
            # Guard against a stale allocator that reuses an already-claimed id.
            while True:
                request_id = dispatch_reservation_request_id(seat, attempt)
                if request_id not in self._launch_claims and not self._has_durable_dispatch_requested(seat, attempt):
                    break
                attempt = max(attempt, self._max_attempt_unlocked(seat)) + 1

            request_id = dispatch_reservation_request_id(seat, attempt)
            decision = self._reserve_unlocked(request_id=request_id, units=1)
            try:
                recorded_attempt = record_requested(attempt)
            except Exception:
                if not decision.replayed:
                    self._unreserve_unlocked(request_id=request_id, units=1)
                raise
            if recorded_attempt is None:
                recorded_attempt = attempt
            if self._pending_dispatch_units > 0:
                self._pending_dispatch_units -= 1
            self._events_cache.append(
                {
                    "event_type": "run.dispatch.requested",
                    "payload": {"seat": seat, "attempt": recorded_attempt, "detail": "requested"},
                }
            )
            self._projection = project_budget_state(self.declaration, self._events_cache)
            recorded_id = dispatch_reservation_request_id(seat, recorded_attempt)
            self._reserved_request_ids.add(recorded_id)
            self._launch_claims.add(recorded_id)
            return recorded_attempt

    def request_cancel(
        self,
        *,
        request_id: str,
        reason_class: str,
        transport_capability: str,
        dimension: str,
        cancel_fn: Callable[[], CancellationReport | tuple[str, int]] | None = None,
    ) -> CancelReceipt:
        """Best-effort cancel of active work with durable cancel receipts.

        ``cancel_fn`` returns observations. The legacy tuple form remains
        readable for callers that only have an aggregate transport result.
        """
        with self._lock:
            for receipt in reversed(self._projection.cancel_receipts):
                if receipt.request_id == request_id:
                    return receipt
            requested = build_cancel_requested_event(
                request_id=request_id,
                reason_class=reason_class,
                transport_capability=transport_capability,
                dimension=dimension,
            )
            self._commit([requested])
            transport_result = "unsupported"
            active_remaining = 0
            outcomes: tuple[CancelOutcome, ...] = ()
            active_seats: tuple[str, ...] = ()
            if cancel_fn is not None:
                observation = cancel_fn()
                if isinstance(observation, CancellationReport):
                    outcomes = observation.outcomes
                    active_seats = observation.active_seats
                    transport_result = observation.transport_result
                    active_remaining = len(active_seats)
                else:
                    transport_result, active_remaining = observation
            cancelled = build_cancelled_event(
                request_id=request_id,
                reason_class=reason_class,
                transport_capability=transport_capability,
                transport_result=transport_result,
                active_remaining=active_remaining,
                dimension=dimension,
                outcomes=outcomes,
                active_seats=active_seats,
            )
            self._commit([cancelled])
            return CancelReceipt(
                request_id=request_id,
                reason_class=reason_class,
                transport_capability=transport_capability,
                transport_result=transport_result,
                active_remaining=active_remaining,
                dimension=dimension,
                outcomes=outcomes,
                active_seats=active_seats,
            )

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
        with self._lock:
            if dimension in ENFORCEABLE_DIMENSIONS and mode_for_dimension(dimension) == "enforceable":
                pass
            else:
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
