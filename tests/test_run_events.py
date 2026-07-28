"""Tests for brigade.run_events canonicalization, digests, and envelope validation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from brigade import run_events
from brigade.receipt_schema import RUN_EVENT_SCHEMA, RUN_EVENT_SCHEMA_VERSION

RUN_ID = "20260727-153045-a1b2c3d4"
RECORDED_AT = "2026-07-27T15:30:45.123456Z"
IDEMPOTENCY_KEY = "create-1"

# UTF-8 bytes for "café" (é = 0xC3 0xA9). The slice-1 contract requires
# canonical UTF-8 JSON with no ASCII escaping; a Latin-1 byte literal would
# contradict that contract and produce a different digest.
_CANONICAL_CAFE = b'{"a":1,"b":"caf\xc3\xa9"}'
_CANONICAL_CAFE_DIGEST = "f7a52e662c92fc838cc3e18c060954d6b27190e0051d76e139b8bab26a2923bd"

_REQUEST = {
    "event_type": "run.created",
    "idempotency_key": IDEMPOTENCY_KEY,
    "payload": {"status": "started"},
}
_REQUEST_DIGEST = "3c8ed91a3781b4f7033bc1940ee95eaa50a88f33d453e2b9fb9142150b3f89cb"
# event_digest is the SHA-256 of the canonical envelope with BOTH event_digest
# and event_id excluded. event_id embeds event_digest[:12], so including
# event_id in the digest input would create an infeasible SHA-256 fixed point.
# Excluding event_id is the only computable reading of the slice-1 binding.
_EVENT_DIGEST = "6d43c3e25e4d4c8627166ab5b565622f98b0999218889b65ac8c84dcfac9301b"
_EVENT_ID = f"{RUN_ID}-000001-{_EVENT_DIGEST[:12]}"

_FORBIDDEN_PAYLOAD_KEYS = (
    "prompt",
    "prompt_text",
    "model_output",
    "tool_args",
    "credentials",
    "provider_response",
    "stack_trace",
)


def _sample_envelope(*, event_digest: str = _EVENT_DIGEST, event_id: str = _EVENT_ID) -> dict:
    return {
        "schema": RUN_EVENT_SCHEMA,
        "schema_version": RUN_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "run_id": RUN_ID,
        "sequence": 1,
        "event_type": "run.created",
        "recorded_at": RECORDED_AT,
        "idempotency_key": IDEMPOTENCY_KEY,
        "request_digest": _REQUEST_DIGEST,
        "previous_digest": None,
        "event_digest": event_digest,
        "payload": {"status": "started"},
    }


def test_schema_constants_are_registered_in_receipt_schema():
    assert RUN_EVENT_SCHEMA == "brigade.run_event.v1"
    assert RUN_EVENT_SCHEMA_VERSION == 1
    assert run_events.SCHEMA == RUN_EVENT_SCHEMA
    assert run_events.SCHEMA_VERSION == RUN_EVENT_SCHEMA_VERSION


def test_canonical_bytes_matches_exact_utf8_compact_sorted_form():
    assert run_events.canonical_bytes({"a": 1, "b": "café"}) == _CANONICAL_CAFE
    assert hashlib.sha256(_CANONICAL_CAFE).hexdigest() == _CANONICAL_CAFE_DIGEST


def test_canonical_bytes_rejects_floats_and_bools():
    with pytest.raises(run_events.CanonicalizationError):
        run_events.canonical_bytes({"value": 1.5})
    with pytest.raises(run_events.CanonicalizationError):
        run_events.canonical_bytes({"value": True})
    with pytest.raises(run_events.CanonicalizationError):
        run_events.canonical_bytes({"value": False})


def test_canonical_bytes_rejects_oversized_integers():
    with pytest.raises(run_events.CanonicalizationError):
        run_events.canonical_bytes({"value": 2**63})
    with pytest.raises(run_events.CanonicalizationError):
        run_events.canonical_bytes({"value": -(2**63) - 1})
    # The signed 64-bit boundaries themselves remain canonical.
    assert run_events.canonical_bytes({"value": 2**63 - 1}) == b'{"value":9223372036854775807}'
    assert run_events.canonical_bytes({"value": -(2**63)}) == b'{"value":-9223372036854775808}'


def test_canonical_bytes_wraps_conversion_errors_in_bounded_typed_error():
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(run_events.CanonicalizationError) as excinfo:
        run_events.canonical_bytes(cyclic)
    assert len(str(excinfo.value)) <= 240


def test_validate_event_reports_oversized_integer_as_bounded_error():
    env = _sample_envelope()
    env["sequence"] = 2**63
    errors = run_events.validate_event(env)
    assert errors
    assert all(len(err) <= 240 for err in errors)


def test_canonical_bytes_preserves_explicit_null():
    assert run_events.canonical_bytes({"optional": None}) == b'{"optional":null}'


def test_format_recorded_at_emits_exact_six_digit_utc_z():
    dt = datetime(2026, 7, 27, 15, 30, 45, 123456, tzinfo=timezone.utc)
    assert run_events.format_recorded_at(dt) == RECORDED_AT
    assert run_events.is_valid_recorded_at(RECORDED_AT) is True


@pytest.mark.parametrize(
    "bad_ts",
    [
        "2026-07-27T15:30:45Z",
        "2026-07-27T15:30:45.12345Z",
        "2026-07-27T15:30:45.1234567Z",
        "2026-07-27T15:30:45.123456+00:00",
        "not-a-timestamp",
    ],
)
def test_is_valid_recorded_at_rejects_nonconforming_timestamps(bad_ts):
    assert run_events.is_valid_recorded_at(bad_ts) is False


def test_request_digest_is_deterministic_for_known_request():
    digest = run_events.request_digest(
        event_type=_REQUEST["event_type"],
        payload=_REQUEST["payload"],
        idempotency_key=_REQUEST["idempotency_key"],
    )
    assert digest == _REQUEST_DIGEST
    assert len(digest) == 64
    assert digest == digest.lower()


def test_compute_event_digest_excludes_event_digest_and_event_id_for_noncircular_binding():
    # event_id embeds event_digest[:12], so event_digest must exclude event_id
    # (and event_digest itself) to keep the binding computable.
    envelope = _sample_envelope()
    assert run_events.compute_event_digest(envelope) == _EVENT_DIGEST


def test_make_event_id_is_deterministic():
    assert (
        run_events.make_event_id(
            run_id=RUN_ID,
            sequence=1,
            event_digest=_EVENT_DIGEST,
        )
        == _EVENT_ID
    )


def test_build_event_produces_byte_exact_known_envelope():
    event = run_events.build_event(
        run_id=RUN_ID,
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key=IDEMPOTENCY_KEY,
        recorded_at=RECORDED_AT,
        previous_digest=None,
    )
    assert event["request_digest"] == _REQUEST_DIGEST
    assert event["event_digest"] == _EVENT_DIGEST
    assert event["event_id"] == _EVENT_ID
    line = run_events.canonical_bytes(event) + b"\n"
    expected = run_events.canonical_bytes(_sample_envelope()) + b"\n"
    assert line == expected


def test_validate_event_accepts_known_good_envelope():
    assert run_events.validate_event(_sample_envelope()) == []


def test_validate_event_rejects_unknown_envelope_key_with_bounded_diagnostic():
    env = _sample_envelope()
    env["surprise"] = "nope"
    errors = run_events.validate_event(env)
    assert errors
    assert len(errors[0]) <= 240
    assert "surprise" in errors[0]


def test_validate_event_rejects_unknown_payload_key_with_bounded_diagnostic():
    env = _sample_envelope()
    env["payload"] = {"status": "started", "extra": "nope"}
    errors = run_events.validate_event(env)
    assert errors
    assert len(errors[0]) <= 240
    assert "extra" in errors[0]


def test_validate_event_rejects_unknown_schema_version():
    env = _sample_envelope()
    env["schema_version"] = 99
    errors = run_events.validate_event(env)
    assert errors
    assert len(errors[0]) <= 240
    assert "schema_version" in errors[0]


def test_validate_event_rejects_unknown_schema_string():
    env = _sample_envelope()
    env["schema"] = "brigade.run_event.v99"
    errors = run_events.validate_event(env)
    assert errors
    assert len(errors[0]) <= 240
    assert "schema" in errors[0]


def test_validate_event_rejects_unknown_event_type():
    env = _sample_envelope()
    env["event_type"] = "run.not-a-real-event"
    errors = run_events.validate_event(env)
    assert errors
    assert len(errors[0]) <= 240
    assert "event_type" in errors[0]


@pytest.mark.parametrize("forbidden_key", _FORBIDDEN_PAYLOAD_KEYS)
def test_validate_event_rejects_private_data_payload_keys(forbidden_key):
    env = _sample_envelope()
    env["payload"] = {forbidden_key: "secret"}
    errors = run_events.validate_event(env)
    assert errors
    assert len(errors[0]) <= 240


def test_event_type_registry_includes_run_created_with_status_only():
    assert "run.created" in run_events.EVENT_TYPES
    assert run_events.EVENT_TYPES["run.created"] == frozenset({"status"})


def test_build_event_rejects_payload_with_non_integer_numbers():
    with pytest.raises(run_events.CanonicalizationError):
        run_events.build_event(
            run_id=RUN_ID,
            sequence=1,
            event_type="run.dispatch.requested",
            payload={"seat": "coder", "attempt": 1.0},
            idempotency_key="dispatch-1",
            recorded_at=RECORDED_AT,
            previous_digest=None,
        )


def test_validate_event_requires_explicit_null_previous_digest_at_sequence_one():
    env = _sample_envelope()
    del env["previous_digest"]
    errors = run_events.validate_event(env)
    assert errors
    assert any("previous_digest" in err for err in errors)


def test_validate_event_recomputes_event_digest_mismatch():
    env = _sample_envelope(event_digest="0" * 64)
    errors = run_events.validate_event(env)
    assert errors
    assert any("event_digest" in err for err in errors)


def test_validate_event_limits_unknown_key_diagnostics():
    env = _sample_envelope()
    for idx in range(12):
        env[f"unknown_{idx}"] = "x"
    errors = run_events.validate_event(env)
    assert errors
    assert len(errors[0]) <= 240
