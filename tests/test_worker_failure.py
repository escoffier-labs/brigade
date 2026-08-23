import pytest

from brigade.worker_failure import (
    FAILURE_CLASS_SPECS,
    LEGACY_FAILURE_KIND_MAP,
    FailureClass,
    FailurePhase,
    RetryDisposition,
    WorkerFailure,
    normalized_failure,
    safe_detail,
)
from brigade.run_receipts import worker_payload
from brigade.run_transport import WorkerAttempt, WorkerResult


EXPECTED_PUBLIC_CLASSES = {
    "configuration-invalid",
    "executable-unavailable",
    "auth-required",
    "entitlement-denied",
    "version-gate",
    "model-unavailable",
    "capacity-exhausted",
    "network-unavailable",
    "transport-unavailable",
    "transport-hang",
    "interactive-blocked",
    "worker-crash",
    "timeout",
    "isolation-breach",
    "output-contract-violation",
    "provider-rejected",
    "unclassified",
}
EXPECTED_LEGACY_CLASSIFICATIONS = {
    "authentication-error": FailureClass.AUTH_REQUIRED,
    "auth-status-unavailable": FailureClass.UNCLASSIFIED,
    "auth-status-unrecognized": FailureClass.UNCLASSIFIED,
    "browser-auth": FailureClass.AUTH_REQUIRED,
    "command-not-found": FailureClass.EXECUTABLE_UNAVAILABLE,
    "decode-failure": FailureClass.TRANSPORT_UNAVAILABLE,
    "output-limit": FailureClass.TRANSPORT_UNAVAILABLE,
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
    # #703: a seat that calls no tools and ships no deliverable is a model-quality failure.
    "suspected-noop-run": FailureClass.OUTPUT_CONTRACT_VIOLATION,
    "transport-error": FailureClass.TRANSPORT_UNAVAILABLE,
    "unsafe-worktree": FailureClass.CONFIGURATION_INVALID,
    "unsupported-command-shim": FailureClass.EXECUTABLE_UNAVAILABLE,
    "unsupported-sandbox": FailureClass.CONFIGURATION_INVALID,
    "version-mismatch": FailureClass.VERSION_GATE,
    "workspace-trust": FailureClass.INTERACTIVE_BLOCKED,
}

EXPECTED_RETRY_POLICIES = {
    FailureClass.CONFIGURATION_INVALID: ({RetryDisposition.AFTER_REMEDIATION}, RetryDisposition.AFTER_REMEDIATION),
    FailureClass.EXECUTABLE_UNAVAILABLE: ({RetryDisposition.AFTER_REMEDIATION}, RetryDisposition.AFTER_REMEDIATION),
    FailureClass.AUTH_REQUIRED: ({RetryDisposition.AFTER_REMEDIATION}, RetryDisposition.AFTER_REMEDIATION),
    FailureClass.ENTITLEMENT_DENIED: ({RetryDisposition.AFTER_REMEDIATION}, RetryDisposition.AFTER_REMEDIATION),
    FailureClass.VERSION_GATE: ({RetryDisposition.AFTER_REMEDIATION}, RetryDisposition.AFTER_REMEDIATION),
    FailureClass.MODEL_UNAVAILABLE: ({RetryDisposition.FALLBACK}, RetryDisposition.FALLBACK),
    FailureClass.CAPACITY_EXHAUSTED: (
        {RetryDisposition.FALLBACK, RetryDisposition.SAME_SEAT_ONCE},
        RetryDisposition.FALLBACK,
    ),
    FailureClass.NETWORK_UNAVAILABLE: ({RetryDisposition.SAME_SEAT_ONCE}, RetryDisposition.SAME_SEAT_ONCE),
    FailureClass.TRANSPORT_UNAVAILABLE: ({RetryDisposition.SAME_SEAT_ONCE}, RetryDisposition.SAME_SEAT_ONCE),
    FailureClass.TRANSPORT_HANG: ({RetryDisposition.FALLBACK}, RetryDisposition.FALLBACK),
    FailureClass.INTERACTIVE_BLOCKED: ({RetryDisposition.NEVER}, RetryDisposition.NEVER),
    FailureClass.WORKER_CRASH: ({RetryDisposition.FALLBACK}, RetryDisposition.FALLBACK),
    FailureClass.TIMEOUT: ({RetryDisposition.FALLBACK}, RetryDisposition.FALLBACK),
    FailureClass.ISOLATION_BREACH: ({RetryDisposition.NEVER}, RetryDisposition.NEVER),
    FailureClass.OUTPUT_CONTRACT_VIOLATION: (
        {RetryDisposition.NEVER, RetryDisposition.FALLBACK},
        RetryDisposition.NEVER,
    ),
    FailureClass.PROVIDER_REJECTED: ({RetryDisposition.FALLBACK}, RetryDisposition.FALLBACK),
    FailureClass.UNCLASSIFIED: ({RetryDisposition.NEVER}, RetryDisposition.NEVER),
}


def test_public_taxonomy_has_exactly_the_17_proposed_classes():
    assert {failure_class.value for failure_class in FailureClass} == EXPECTED_PUBLIC_CLASSES


@pytest.mark.parametrize("failure_class, spec", FAILURE_CLASS_SPECS.items())
def test_every_public_failure_class_has_a_valid_default_phase_and_retry(failure_class, spec):
    allowed_retries, default_retry = EXPECTED_RETRY_POLICIES[failure_class]
    failure = WorkerFailure(
        failure_class=failure_class,
        phase=spec.default_phase,
        retry=spec.default_retry,
        detected_by="fake-adapter",
        detail="fake fixture detail",
    )

    assert spec.allowed_retries == frozenset(allowed_retries)
    assert spec.default_retry is default_retry
    assert failure.payload()["schema"] == "brigade.worker_failure.v1"
    assert failure.payload()["class"] == failure_class.value
    assert failure.payload()["domain"] == "infrastructure"


def test_capacity_exhausted_allows_same_seat_once_when_a_provider_supplies_a_retry_delay():
    failure = WorkerFailure(
        failure_class=FailureClass.CAPACITY_EXHAUSTED,
        phase=FailurePhase.DISPATCH,
        retry=RetryDisposition.SAME_SEAT_ONCE,
        detected_by="fake-provider-delay",
        detail="retry after 30 seconds",
    )

    assert failure.retry is RetryDisposition.SAME_SEAT_ONCE


def test_output_contract_violation_allows_reviewed_invalid_final_fallback():
    failure = WorkerFailure(
        failure_class=FailureClass.OUTPUT_CONTRACT_VIOLATION,
        phase=FailurePhase.VALIDATION,
        retry=RetryDisposition.FALLBACK,
        detected_by="fake-reviewed-invalid-final-fallback",
        detail="final output was invalid",
    )

    assert failure.retry is RetryDisposition.FALLBACK


@pytest.mark.parametrize(
    ("failure_kind", "expected_class", "expected_retry"),
    [
        ("rate-limit-error", FailureClass.CAPACITY_EXHAUSTED, RetryDisposition.FALLBACK),
        ("empty-output", FailureClass.OUTPUT_CONTRACT_VIOLATION, RetryDisposition.NEVER),
    ],
)
def test_normalized_contextual_failures_use_their_conservative_defaults(failure_kind, expected_class, expected_retry):
    failure = normalized_failure(
        failure_phase="dispatch",
        failure_kind=failure_kind,
        detail="fake adapter failure",
    )

    assert failure is not None
    assert failure.failure_class is expected_class
    assert failure.retry is expected_retry


@pytest.mark.parametrize("failure_class, spec", FAILURE_CLASS_SPECS.items())
def test_worker_failure_rejects_a_retry_outside_the_class_allowed_set(failure_class, spec):
    invalid_retry = next(retry for retry in RetryDisposition if retry not in spec.allowed_retries)

    with pytest.raises(ValueError, match="must use one of"):
        WorkerFailure(
            failure_class=failure_class,
            phase=spec.default_phase,
            retry=invalid_retry,
            detected_by="fake-adapter",
            detail="fake fixture detail",
        )


@pytest.mark.parametrize("legacy_kind, expected_class", EXPECTED_LEGACY_CLASSIFICATIONS.items())
def test_every_known_legacy_failure_kind_maps_to_a_closed_class(legacy_kind, expected_class):
    failure = normalized_failure(
        failure_phase="dispatch",
        failure_kind=legacy_kind,
        detail="fake adapter failure",
    )

    assert failure is not None
    assert failure.failure_class is expected_class
    assert failure.cause_code == legacy_kind


def test_legacy_compatibility_table_covers_only_currently_emitted_adapter_kinds():
    assert LEGACY_FAILURE_KIND_MAP == EXPECTED_LEGACY_CLASSIFICATIONS


def test_unknown_legacy_failure_is_unclassified_with_actual_normalized_phase():
    failure = normalized_failure(
        failure_phase="output-validation",
        failure_kind="future-adapter-code",
        detail="fake future error",
    )

    assert failure is not None
    assert failure.failure_class is FailureClass.UNCLASSIFIED
    assert failure.phase is FailurePhase.VALIDATION
    assert failure.retry.value == "never"
    assert failure.cause_code == "future-adapter-code"


@pytest.mark.parametrize(
    ("status", "failure_kind"),
    [
        ("skipped", None),
        ("held", None),
        ("canceled", "interrupted"),
        ("failed", "keyboard-interrupt"),
        ("failed", "branch-head-drift"),
        ("failed", "isolation-breach"),
    ],
)
def test_run_and_operator_failures_stay_outside_worker_taxonomy(status, failure_kind):
    assert (
        normalized_failure(
            failure_phase="dispatch",
            failure_kind=failure_kind,
            detail="fake non-worker failure",
            status=status,
        )
        is None
    )


def test_normalized_failure_detail_is_bounded_and_redacted():
    raw = "token=fake-token-value https://fake-user:fake-password@fake.example.test/v1?api_key=fake-key"

    detail = safe_detail(raw)

    assert "fake-token-value" not in detail
    assert "fake-password" not in detail
    assert "fake-key" not in detail
    assert "[redacted]" in detail
    assert len(safe_detail("x" * 400)) == 200


def test_failed_result_and_attempt_serialize_additive_normalized_failures():
    attempt = WorkerAttempt(
        kind="initial",
        worker="fake-worker",
        task="fake task",
        transport="fake-transport",
        model=None,
        reasoning=None,
        started_at="2026-07-26T00:00:00Z",
        finished_at="2026-07-26T00:00:01Z",
        exit_code=7,
        terminal_reason="network-error",
        failure_phase="dispatch",
        failure_kind="network-error",
        session_id=None,
        detail="token=fake-token-value",
    )
    result = WorkerResult(
        worker="fake-worker",
        task="fake task",
        text="",
        ok=False,
        detail="token=fake-token-value",
        failure_phase="dispatch",
        failure_kind="network-error",
        attempts=(attempt,),
    )

    payload = worker_payload([result])[0]

    assert payload["failure"]["class"] == "network-unavailable"
    assert payload["failure"]["detail"] == "token=[redacted]"
    assert payload["attempts"][0]["failure"] == {
        **payload["failure"],
        "attempt": 1,
    }


def test_successful_result_does_not_receive_a_failure_object():
    payload = worker_payload([WorkerResult(worker="fake-worker", task="fake task", text="done", ok=True)])[0]

    assert "failure" not in payload


def test_failed_attempt_without_a_legacy_kind_is_unclassified():
    attempt = WorkerAttempt(
        kind="initial",
        worker="fake-worker",
        task="fake task",
        transport="fake-transport",
        model=None,
        reasoning=None,
        started_at="2026-07-26T00:00:00Z",
        finished_at="2026-07-26T00:00:01Z",
        exit_code=7,
        terminal_reason="failed",
        failure_phase=None,
        failure_kind=None,
        session_id=None,
        ok=False,
    )
    result = WorkerResult(worker="fake-worker", task="fake task", text="", ok=False, attempts=(attempt,))

    payload = worker_payload([result])[0]

    assert payload["failure"]["class"] == "unclassified"
    assert payload["attempts"][0]["failure"]["class"] == "unclassified"
