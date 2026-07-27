"""Closed worker-failure taxonomy and legacy receipt compatibility."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


SCHEMA = "brigade.worker_failure.v1"
DETAIL_LIMIT = 200


class FailureClass(str, Enum):
    CONFIGURATION_INVALID = "configuration-invalid"
    EXECUTABLE_UNAVAILABLE = "executable-unavailable"
    AUTH_REQUIRED = "auth-required"
    ENTITLEMENT_DENIED = "entitlement-denied"
    VERSION_GATE = "version-gate"
    MODEL_UNAVAILABLE = "model-unavailable"
    CAPACITY_EXHAUSTED = "capacity-exhausted"
    NETWORK_UNAVAILABLE = "network-unavailable"
    TRANSPORT_UNAVAILABLE = "transport-unavailable"
    TRANSPORT_HANG = "transport-hang"
    INTERACTIVE_BLOCKED = "interactive-blocked"
    WORKER_CRASH = "worker-crash"
    TIMEOUT = "timeout"
    ISOLATION_BREACH = "isolation-breach"
    OUTPUT_CONTRACT_VIOLATION = "output-contract-violation"
    PROVIDER_REJECTED = "provider-rejected"
    UNCLASSIFIED = "unclassified"


class FailurePhase(str, Enum):
    PREFLIGHT = "preflight"
    DISPATCH = "dispatch"
    POSTFLIGHT = "postflight"
    VALIDATION = "validation"


class RetryDisposition(str, Enum):
    NEVER = "never"
    AFTER_REMEDIATION = "after-remediation"
    FALLBACK = "fallback"
    SAME_SEAT_ONCE = "same-seat-once"


class FailureDomain(str, Enum):
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class FailureClassSpec:
    allowed_retries: frozenset[RetryDisposition]
    default_retry: RetryDisposition
    phases: frozenset[FailurePhase]
    default_phase: FailurePhase

    def __post_init__(self) -> None:
        if not self.allowed_retries:
            raise ValueError("allowed_retries must not be empty")
        if self.default_retry not in self.allowed_retries:
            raise ValueError("default_retry must be included in allowed_retries")


FAILURE_CLASS_SPECS: dict[FailureClass, FailureClassSpec] = {
    FailureClass.CONFIGURATION_INVALID: FailureClassSpec(
        frozenset({RetryDisposition.AFTER_REMEDIATION}),
        RetryDisposition.AFTER_REMEDIATION,
        frozenset({FailurePhase.PREFLIGHT}),
        FailurePhase.PREFLIGHT,
    ),
    FailureClass.EXECUTABLE_UNAVAILABLE: FailureClassSpec(
        frozenset({RetryDisposition.AFTER_REMEDIATION}),
        RetryDisposition.AFTER_REMEDIATION,
        frozenset({FailurePhase.PREFLIGHT}),
        FailurePhase.PREFLIGHT,
    ),
    FailureClass.AUTH_REQUIRED: FailureClassSpec(
        frozenset({RetryDisposition.AFTER_REMEDIATION}),
        RetryDisposition.AFTER_REMEDIATION,
        frozenset({FailurePhase.PREFLIGHT, FailurePhase.DISPATCH}),
        FailurePhase.PREFLIGHT,
    ),
    FailureClass.ENTITLEMENT_DENIED: FailureClassSpec(
        frozenset({RetryDisposition.AFTER_REMEDIATION}),
        RetryDisposition.AFTER_REMEDIATION,
        frozenset({FailurePhase.PREFLIGHT, FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.VERSION_GATE: FailureClassSpec(
        frozenset({RetryDisposition.AFTER_REMEDIATION}),
        RetryDisposition.AFTER_REMEDIATION,
        frozenset({FailurePhase.PREFLIGHT}),
        FailurePhase.PREFLIGHT,
    ),
    FailureClass.MODEL_UNAVAILABLE: FailureClassSpec(
        frozenset({RetryDisposition.FALLBACK}),
        RetryDisposition.FALLBACK,
        frozenset({FailurePhase.PREFLIGHT}),
        FailurePhase.PREFLIGHT,
    ),
    FailureClass.CAPACITY_EXHAUSTED: FailureClassSpec(
        frozenset({RetryDisposition.FALLBACK, RetryDisposition.SAME_SEAT_ONCE}),
        RetryDisposition.FALLBACK,
        frozenset({FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.NETWORK_UNAVAILABLE: FailureClassSpec(
        frozenset({RetryDisposition.SAME_SEAT_ONCE}),
        RetryDisposition.SAME_SEAT_ONCE,
        frozenset({FailurePhase.PREFLIGHT, FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.TRANSPORT_UNAVAILABLE: FailureClassSpec(
        frozenset({RetryDisposition.SAME_SEAT_ONCE}),
        RetryDisposition.SAME_SEAT_ONCE,
        frozenset({FailurePhase.PREFLIGHT, FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.TRANSPORT_HANG: FailureClassSpec(
        frozenset({RetryDisposition.FALLBACK}),
        RetryDisposition.FALLBACK,
        frozenset({FailurePhase.PREFLIGHT, FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.INTERACTIVE_BLOCKED: FailureClassSpec(
        frozenset({RetryDisposition.NEVER}),
        RetryDisposition.NEVER,
        frozenset({FailurePhase.PREFLIGHT, FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.WORKER_CRASH: FailureClassSpec(
        frozenset({RetryDisposition.FALLBACK}),
        RetryDisposition.FALLBACK,
        frozenset({FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.TIMEOUT: FailureClassSpec(
        frozenset({RetryDisposition.FALLBACK}),
        RetryDisposition.FALLBACK,
        frozenset({FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.ISOLATION_BREACH: FailureClassSpec(
        frozenset({RetryDisposition.NEVER}),
        RetryDisposition.NEVER,
        frozenset({FailurePhase.POSTFLIGHT}),
        FailurePhase.POSTFLIGHT,
    ),
    FailureClass.OUTPUT_CONTRACT_VIOLATION: FailureClassSpec(
        frozenset({RetryDisposition.NEVER, RetryDisposition.FALLBACK}),
        RetryDisposition.NEVER,
        frozenset({FailurePhase.VALIDATION}),
        FailurePhase.VALIDATION,
    ),
    FailureClass.PROVIDER_REJECTED: FailureClassSpec(
        frozenset({RetryDisposition.FALLBACK}),
        RetryDisposition.FALLBACK,
        frozenset({FailurePhase.DISPATCH}),
        FailurePhase.DISPATCH,
    ),
    FailureClass.UNCLASSIFIED: FailureClassSpec(
        frozenset({RetryDisposition.NEVER}),
        RetryDisposition.NEVER,
        frozenset(FailurePhase),
        FailurePhase.DISPATCH,
    ),
}


# Every value emitted by a worker adapter is mapped here. New adapter kinds must
# be added deliberately; unrecognized values map to ``unclassified`` below.
LEGACY_FAILURE_KIND_MAP: dict[str, FailureClass] = {
    "authentication-error": FailureClass.AUTH_REQUIRED,
    "auth-status-unavailable": FailureClass.UNCLASSIFIED,
    "auth-status-unrecognized": FailureClass.UNCLASSIFIED,
    "browser-auth": FailureClass.AUTH_REQUIRED,
    "command-not-found": FailureClass.EXECUTABLE_UNAVAILABLE,
    "decode-failure": FailureClass.TRANSPORT_UNAVAILABLE,
    "empty-output": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "env-ref-missing": FailureClass.CONFIGURATION_INVALID,
    "grok-fallback-missing": FailureClass.CONFIGURATION_INVALID,
    "grok-session-missing": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "invalid-dispatch-args": FailureClass.CONFIGURATION_INVALID,
    "invalid-session-continuation": FailureClass.CONFIGURATION_INVALID,
    "malformed-final-output": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "malformed-transport": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "missing-executable": FailureClass.EXECUTABLE_UNAVAILABLE,
    "network-error": FailureClass.NETWORK_UNAVAILABLE,
    "non-final-output": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "non-final-stop": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "permission-denied": FailureClass.INTERACTIVE_BLOCKED,
    "permission-error": FailureClass.INTERACTIVE_BLOCKED,
    "provider-auth": FailureClass.AUTH_REQUIRED,
    "provider-config": FailureClass.CONFIGURATION_INVALID,
    "provider-error": FailureClass.PROVIDER_REJECTED,
    "provider-setting-error": FailureClass.MODEL_UNAVAILABLE,
    "provider-startup": FailureClass.TRANSPORT_UNAVAILABLE,
    "rate-limit-error": FailureClass.CAPACITY_EXHAUSTED,
    "timeout": FailureClass.TIMEOUT,
    "tool-only-output": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "transport-error": FailureClass.TRANSPORT_UNAVAILABLE,
    "unsafe-worktree": FailureClass.CONFIGURATION_INVALID,
    "unsupported-command-shim": FailureClass.EXECUTABLE_UNAVAILABLE,
    "unsupported-sandbox": FailureClass.CONFIGURATION_INVALID,
    "version-mismatch": FailureClass.VERSION_GATE,
    "workspace-trust": FailureClass.INTERACTIVE_BLOCKED,
}

_LEGACY_PHASE_MAP = {
    "preflight": FailurePhase.PREFLIGHT,
    "provider-preflight": FailurePhase.PREFLIGHT,
    "dispatch": FailurePhase.DISPATCH,
    "harness": FailurePhase.DISPATCH,
    "inference": FailurePhase.DISPATCH,
    "postflight": FailurePhase.POSTFLIGHT,
    "output-validation": FailurePhase.VALIDATION,
    "validation": FailurePhase.VALIDATION,
}
_NON_WORKER_STATUSES = frozenset({"skipped", "held", "canceled", "abandoned"})
_RUN_OWNED_FAILURE_KINDS = frozenset(
    {
        "agent-error",
        "branch-head-drift",
        "collection-error",
        "dirty-worktree",
        "early-exit",
        "handoff-write-error",
        "interrupted",
        "invalid-patch",
        "invalid-plan",
        "keyboard-interrupt",
        "orchestrator-error",
        "planner-failure",
        "receipt-update-error",
        "receipt-write-failure",
        "signal",
        "spawn-error",
        "unexpected-error",
        "verification-failure",
        "worker-failure",
    }
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
_URL_QUERY = re.compile(r"(?P<url>https?://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?P<name>api[_-]?key|token|password|secret|authorization)\s*(?P<separator>=|:)\s*[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)


def safe_detail(detail: str) -> str:
    """Return bounded receipt detail without common credential forms."""

    redacted = _URL_CREDENTIALS.sub(r"\g<scheme>[redacted]@", detail)
    redacted = _URL_QUERY.sub(r"\g<url>", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(r"\g<name>\g<separator>[redacted]", redacted)
    redacted = _BEARER_TOKEN.sub("Bearer [redacted]", redacted)
    return redacted[:DETAIL_LIMIT]


@dataclass(frozen=True)
class WorkerFailure:
    """A normalized, public worker failure suitable for an additive receipt."""

    failure_class: FailureClass
    phase: FailurePhase
    retry: RetryDisposition
    detected_by: str
    detail: str
    cause_code: str | None = None
    attempt: int | None = None
    domain: FailureDomain = FailureDomain.INFRASTRUCTURE

    def __post_init__(self) -> None:
        spec = FAILURE_CLASS_SPECS[self.failure_class]
        if self.phase not in spec.phases:
            raise ValueError(f"{self.failure_class.value} is not valid during {self.phase.value}")
        if self.retry not in spec.allowed_retries:
            allowed = ", ".join(sorted(retry.value for retry in spec.allowed_retries))
            raise ValueError(f"{self.failure_class.value} must use one of: {allowed}")

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": SCHEMA,
            "class": self.failure_class.value,
            "domain": self.domain.value,
            "phase": self.phase.value,
            "retry": self.retry.value,
            "detected_by": self.detected_by,
            "detail": safe_detail(self.detail),
        }
        if self.cause_code is not None:
            payload["cause_code"] = self.cause_code
        if self.attempt is not None:
            payload["attempt"] = self.attempt
        return payload


def normalized_failure(
    *,
    failure_phase: str | None,
    failure_kind: str | None,
    detail: str,
    timed_out: bool = False,
    status: str = "",
    attempt: int | None = None,
) -> WorkerFailure | None:
    """Map a legacy worker receipt value without changing its original fields."""

    normalized_status = status.strip().lower()
    normalized_kind = failure_kind.strip().lower() if failure_kind else None
    if normalized_status in _NON_WORKER_STATUSES or normalized_kind in _RUN_OWNED_FAILURE_KINDS:
        return None

    if normalized_kind is None and timed_out:
        failure_class = FailureClass.TIMEOUT
    else:
        failure_class = LEGACY_FAILURE_KIND_MAP.get(normalized_kind or "", FailureClass.UNCLASSIFIED)
    spec = FAILURE_CLASS_SPECS[failure_class]
    legacy_phase = _LEGACY_PHASE_MAP.get((failure_phase or "").strip().lower(), FailurePhase.DISPATCH)
    phase = legacy_phase if legacy_phase in spec.phases else spec.default_phase
    return WorkerFailure(
        failure_class=failure_class,
        phase=phase,
        retry=spec.default_retry,
        detected_by="legacy-failure-kind" if normalized_kind else "receipt-fallback",
        detail=detail,
        cause_code=normalized_kind,
        attempt=attempt,
    )
