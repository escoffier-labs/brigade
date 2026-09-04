"""brigade.run_event.v1 envelope: canonicalization, digests, and validation.

Implements the typed run-lifecycle event envelope for the append-only per-run
journal kernel (issue #568, slice 1). The envelope is a closed-key JSON object
serialized as canonical UTF-8 (sorted keys, compact separators, no ASCII
escaping) with exact six-digit UTC-Z timestamps, integer-only payload numbers,
explicit null handling, deterministic request/event digests, and deterministic
per-run event IDs.

Digest binding (non-circular): ``event_digest`` is the SHA-256 of the canonical
envelope with both ``event_digest`` and ``event_id`` excluded, and
``event_id = f"{run_id}-{sequence:06d}-{event_digest[:12]}"``. Excluding
``event_id`` from the digest input is required because ``event_id`` embeds
``event_digest[:12]``; including it would create an infeasible SHA-256 fixed
point. ``request_digest`` is the SHA-256 of the canonical request object
``{event_type, payload, idempotency_key}`` and is computed by the append API,
never trusted from the caller.

Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from brigade.receipt_schema import RUN_EVENT_SCHEMA, RUN_EVENT_SCHEMA_VERSION

SCHEMA = RUN_EVENT_SCHEMA
SCHEMA_VERSION = RUN_EVENT_SCHEMA_VERSION
LEGACY_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({LEGACY_SCHEMA_VERSION, SCHEMA_VERSION})

MAX_LINE_BYTES = 16384
MAX_IDEMPOTENCY_KEY_LEN = 128
MAX_PAYLOAD_STR_LEN = 512
MAX_DIAGNOSTIC_LEN = 240
# Canonical integers are bounded to the signed 64-bit range so every reader
# (including non-Python consumers) can represent them exactly.
MAX_CANONICAL_INT = (1 << 63) - 1
MIN_CANONICAL_INT = -(1 << 63)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_RECORDED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

# Closed set of envelope keys. Any key outside this set fails closed.
ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "event_id",
        "run_id",
        "sequence",
        "event_type",
        "recorded_at",
        "idempotency_key",
        "request_digest",
        "previous_digest",
        "event_digest",
        "payload",
    }
)

# Allowlisted event-type registry -> closed per-type payload key set.
# Payloads are flat (no nested objects) so the allowlist is enforceable and
# private-data exclusions are structural: no key can carry raw prompts, model
# output, tool arguments, credentials, provider response bodies, or stack
# traces -- only bounded detail/status/seat-style strings and integer attempts.
EVENT_TYPES: dict[str, frozenset[str]] = {
    "run.created": frozenset({"status"}),
    "run.planning.started": frozenset({"detail"}),
    "run.planning.completed": frozenset({"detail"}),
    "run.planning.failed": frozenset({"detail"}),
    "run.dispatching.started": frozenset({"detail"}),
    "run.dispatch.requested": frozenset({"seat", "attempt", "detail"}),
    "run.dispatch.observed": frozenset({"seat", "attempt", "detail"}),
    "run.dispatch.completed": frozenset({"seat", "attempt", "detail"}),
    "run.dispatch.failed": frozenset({"seat", "attempt", "detail"}),
    "run.result-processing.started": frozenset({"detail"}),
    "run.synthesis.started": frozenset({"detail"}),
    "run.synthesis.completed": frozenset({"detail"}),
    "run.synthesis.failed": frozenset({"detail"}),
    "run.paused": frozenset({"approval_id", "reason"}),
    "run.resumed": frozenset({"approval_id"}),
    "approval.requested": frozenset({"approval_id", "source", "contract_fingerprint"}),
    "approval.granted": frozenset({"approval_id", "decided_at", "decision_state"}),
    "approval.rejected": frozenset({"approval_id", "decided_at", "decision_state"}),
    "approval.held": frozenset({"approval_id", "decided_at", "decision_state"}),
    "approval.consumed": frozenset({"approval_id", "consuming_run_id"}),
    "approval": frozenset(
        {
            "decision",
            "scope",
            "approver_principal",
            "approver_keyid",
            "subject_tree",
            "nonce",
            "expires_at",
            "statement_sha256",
            "attestation_path",
            "producer_keyids",
        }
    ),
    "request.signed": frozenset(
        {
            "requester_principal",
            "requester_keyid",
            "baseline_commit",
            "task_sha256",
            "nonce",
            "statement_sha256",
            "attestation_path",
        }
    ),
    "run.recovery.started": frozenset({"detail"}),
    "run.recovery.completed": frozenset({"detail"}),
    "run.completed": frozenset({"status", "detail"}),
    "run.failed": frozenset({"status", "detail"}),
    "run.interrupted": frozenset({"status", "detail"}),
    "run.ship": frozenset({"stage"}),
    "run.merge": frozenset({"stage"}),
    "run.orphaned": frozenset({"status", "last_observed_status", "uncommitted_change_count"}),
    "run.snapshot.checkpointed": frozenset(
        {
            "path",
            "sha256",
            "media_type",
            "byte_size",
            "privacy_class",
            "paired_event_type",
            "body_kind",
            "pairing_key",
        }
    ),
    "run.artifact_collection.started": frozenset({"detail"}),
    "run.redaction.recorded": frozenset(
        {
            "operation_id",
            "affected_first_sequence",
            "affected_last_sequence",
            "reason_class",
            "record_sha256",
        }
    ),
    # Live-control request/observation pairs (issue #604). Reference-only:
    # digests, op codes, worker names, turn ids, and request ids — never
    # steering text, model output, or provider bodies.
    "control.requested": frozenset({"op", "worker", "text_digest", "turn_id", "request_id"}),
    "control.observed": frozenset({"op", "worker", "turn_id", "request_id", "detail"}),
    "control.failed": frozenset({"op", "worker", "turn_id", "request_id", "error_class", "detail_digest"}),
    # Run budget lifecycle (issue #593). Safe summaries only — never raw
    # diagnostics, prompts, tool args, or provider bodies.
    "run_budget.threshold_reached": frozenset(
        {"dimension", "mode", "declared", "used", "remaining", "threshold_pct", "reason_class"}
    ),
    "run_budget.reservation_denied": frozenset(
        {"dimension", "mode", "declared", "used", "remaining", "request_id", "reason_class"}
    ),
    "run_budget.exhausted": frozenset({"dimension", "mode", "declared", "used", "remaining", "reason_class"}),
    "run_budget.cancel_requested": frozenset({"request_id", "reason_class", "transport_capability", "dimension"}),
    "run_budget.cancelled": frozenset(
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
    "run_budget.usage_reconciled": frozenset(
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
CONTROL_FAILURE_CLASSES = frozenset({"transport_exception", "transport_rejected", "transport_protocol_error"})
APPROVAL_DECISION_STATES = frozenset({"pending", "approved", "rejected", "held", "consumed"})
APPROVAL_DECISION_EVENT_STATES = {
    "approval.granted": "approved",
    "approval.rejected": "rejected",
    "approval.held": "held",
}
_SCHEMA_V2_EVENT_TYPES = frozenset({"approval", "run.ship", "run.merge"})
_REQUIRED_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "approval": frozenset(
        {
            "decision",
            "scope",
            "approver_principal",
            "approver_keyid",
            "subject_tree",
            "nonce",
            "expires_at",
            "statement_sha256",
            "attestation_path",
        }
    ),
    "request.signed": frozenset(
        {
            "requester_principal",
            "requester_keyid",
            "baseline_commit",
            "task_sha256",
            "nonce",
            "statement_sha256",
            "attestation_path",
        }
    ),
    "run.ship": frozenset({"stage"}),
    "run.merge": frozenset({"stage"}),
}

# Private-data exclusion list: these payload key names are never part of any
# allowlist and are rejected explicitly if a caller attempts to set them.
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "prompt",
        "prompt_text",
        "model_output",
        "tool_args",
        "credentials",
        "provider_response",
        "stack_trace",
    }
)

# Recovery-checkpoint bodies under ``events/recovery-checkpoints/`` are
# ``privacy_class: private`` (see ``run_checkpoint.CHECKPOINT_PRIVACY_CLASS``).
# Journal event payloads only carry the closed artifact-reference shape
# (relative path, sha256, media type, byte size, privacy class). Any exporter
# or collector that copies a run directory across a boundary must strip those
# bodies, replace them with that artifact-reference shape, or refuse with a
# bounded error naming the privacy class. Local recovery may still read the
# private bodies in place. Helpers: ``run_checkpoint.strip_checkpoint_bodies_for_export``,
# ``run_checkpoint.refuse_checkpoint_body_export``.


class CanonicalizationError(ValueError):
    """Raised when a value cannot be canonicalized under the strict rules."""


def _bound(msg: str) -> str:
    if len(msg) <= MAX_DIAGNOSTIC_LEN:
        return msg
    return msg[: MAX_DIAGNOSTIC_LEN - 1] + "\u2026"


def _validate_canonical_node(value: Any) -> None:
    """Recursively enforce the strict canonical value rules."""
    if value is None:
        return
    if isinstance(value, bool):
        raise CanonicalizationError("booleans are not allowed in canonical values")
    if isinstance(value, int):
        if value > MAX_CANONICAL_INT or value < MIN_CANONICAL_INT:
            raise CanonicalizationError("integer exceeds the signed 64-bit canonical range")
        return
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for item in value:
            _validate_canonical_node(item)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"non-string key {key!r}")
            _validate_canonical_node(child)
        return
    raise CanonicalizationError(f"unsupported canonical value type {type(value).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    """Canonical UTF-8 JSON: sorted keys, compact separators, no ASCII escaping.

    Rejects floats, booleans, oversized integers, and any non-JSON-native type
    before serializing. Any residual ``json.dumps`` conversion failure (type
    errors, circular references, recursion/overflow) is wrapped in a bounded
    CanonicalizationError rather than escaping as a raw exception.
    """
    try:
        _validate_canonical_node(obj)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except CanonicalizationError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise CanonicalizationError(_bound(f"value cannot be canonicalized: {exc}")) from exc


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def format_recorded_at(dt: datetime) -> str:
    """Format a datetime as an exact six-digit UTC-Z timestamp."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def is_valid_recorded_at(value: object) -> bool:
    return isinstance(value, str) and bool(_RECORDED_AT_RE.match(value))


def request_digest(*, event_type: str, payload: Mapping[str, Any], idempotency_key: str) -> str:
    """SHA-256 of the canonical request object ``{event_type, payload, idempotency_key}``.

    The request is canonicalized under the same strict rules; non-integer
    numbers, booleans, and unknown types raise CanonicalizationError.
    """
    request = {
        "event_type": event_type,
        "payload": dict(payload),
        "idempotency_key": idempotency_key,
    }
    return _sha256_hex(canonical_bytes(request))


def validate_event_request(
    *,
    event_type: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> None:
    """Validate a caller request before any journal-side mutation."""
    if event_type not in EVENT_TYPES:
        raise CanonicalizationError(_bound(f"unknown event_type {event_type!r}"))
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise CanonicalizationError("idempotency_key must be a non-empty string")
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LEN:
        raise CanonicalizationError(_bound(f"idempotency_key exceeds {MAX_IDEMPOTENCY_KEY_LEN} chars"))
    _validate_payload(event_type, payload)
    request_digest(event_type=event_type, payload=payload, idempotency_key=idempotency_key)


def compute_event_digest(envelope: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical envelope excluding ``event_digest`` and ``event_id``.

    ``event_id`` is excluded because it embeds ``event_digest[:12]``; including
    it would create an infeasible SHA-256 fixed point. This is the single
    deviation from the "exclude only event_digest" phrasing in the slice-1 plan
    and is required for the binding to be computable.
    """
    subset = {k: v for k, v in envelope.items() if k not in ("event_digest", "event_id")}
    return _sha256_hex(canonical_bytes(subset))


def make_event_id(*, run_id: str, sequence: int, event_digest: str) -> str:
    return f"{run_id}-{sequence:06d}-{event_digest[:12]}"


def build_event(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
    recorded_at: str,
    previous_digest: str | None,
    request_digest_value: str | None = None,
) -> dict[str, Any]:
    """Build a fully validated, self-consistent run_event.v1 envelope.

    ``request_digest_value`` may be supplied by the journal layer (which has
    already computed it for idempotency); if omitted it is derived here.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise CanonicalizationError(_bound(f"invalid run_id {run_id!r}"))
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise CanonicalizationError(_bound(f"invalid sequence {sequence!r}"))
    validate_event_request(
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if not is_valid_recorded_at(recorded_at):
        raise CanonicalizationError(_bound(f"invalid recorded_at {recorded_at!r}"))
    if previous_digest is not None and not (isinstance(previous_digest, str) and bool(_HEX64.match(previous_digest))):
        raise CanonicalizationError(_bound("invalid previous_digest"))
    if sequence == 1 and previous_digest is not None:
        raise CanonicalizationError("previous_digest must be null at sequence 1")
    if sequence > 1 and previous_digest is None:
        raise CanonicalizationError("previous_digest must not be null after sequence 1")

    rd = request_digest_value or request_digest(event_type=event_type, payload=payload, idempotency_key=idempotency_key)
    if not (isinstance(rd, str) and bool(_HEX64.match(rd))):
        raise CanonicalizationError(_bound("invalid request_digest"))

    base = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "sequence": sequence,
        "event_type": event_type,
        "recorded_at": recorded_at,
        "idempotency_key": idempotency_key,
        "request_digest": rd,
        "previous_digest": previous_digest,
        "payload": dict(payload),
    }
    event_digest = compute_event_digest(base)
    event_id = make_event_id(run_id=run_id, sequence=sequence, event_digest=event_digest)
    envelope = {**base, "event_id": event_id, "event_digest": event_digest}
    errors = validate_event(envelope)
    if errors:
        raise CanonicalizationError(_bound("invalid built envelope: " + "; ".join(errors)))
    return envelope


def _validate_payload(event_type: str, payload: Any) -> None:
    if not isinstance(payload, Mapping):
        raise CanonicalizationError("payload must be an object")
    allowed = EVENT_TYPES[event_type]
    extra = set(payload.keys()) - allowed
    if extra:
        forbidden = extra & FORBIDDEN_PAYLOAD_KEYS
        if forbidden:
            raise CanonicalizationError(_bound(f"forbidden private-data payload keys: {sorted(forbidden)}"))
        raise CanonicalizationError(_bound(f"unknown payload keys for {event_type}: {sorted(extra)}"))
    missing = _REQUIRED_PAYLOAD_FIELDS.get(event_type, frozenset()) - set(payload.keys())
    if missing:
        raise CanonicalizationError(_bound(f"missing payload keys for {event_type}: {sorted(missing)}"))
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, bool):
            raise CanonicalizationError(_bound(f"payload {key!r} must not be boolean"))
        if isinstance(value, float):
            raise CanonicalizationError(_bound(f"payload {key!r} must be integer, not float"))
        if isinstance(value, int):
            continue
        if isinstance(value, str):
            if len(value) > MAX_PAYLOAD_STR_LEN:
                raise CanonicalizationError(_bound(f"payload {key!r} exceeds {MAX_PAYLOAD_STR_LEN} chars"))
            continue
        if key == "producer_keyids" and isinstance(value, list):
            if any(not isinstance(item, str) or not item or len(item) > MAX_PAYLOAD_STR_LEN for item in value):
                raise CanonicalizationError("approval producer_keyids must be a list of bounded strings")
            continue
        raise CanonicalizationError(_bound(f"payload {key!r} has unsupported type {type(value).__name__}"))
    if event_type == "approval":
        if payload.get("decision") not in {"allow", "deny", "hold"}:
            raise CanonicalizationError("approval decision is invalid")
        if payload.get("scope") not in {"run", "merge"}:
            raise CanonicalizationError("approval scope is invalid")
        for key in ("approver_principal", "approver_keyid", "subject_tree", "expires_at", "attestation_path"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise CanonicalizationError(f"approval {key} is required")
        if not isinstance(payload.get("nonce"), str) or not _HEX32.fullmatch(payload["nonce"]):
            raise CanonicalizationError("approval nonce must be 32 lowercase hex characters")
        if not isinstance(payload.get("statement_sha256"), str) or not _HEX64.fullmatch(payload["statement_sha256"]):
            raise CanonicalizationError("approval statement_sha256 must be 64 lowercase hex characters")
    if event_type == "request.signed":
        if not isinstance(payload.get("nonce"), str) or not _HEX32.fullmatch(payload["nonce"]):
            raise CanonicalizationError("request.signed nonce must be 32 lowercase hex characters")
        for key in ("task_sha256", "statement_sha256"):
            if not isinstance(payload.get(key), str) or not _HEX64.fullmatch(payload[key]):
                raise CanonicalizationError(f"request.signed {key} must be 64 lowercase hex characters")
    if event_type in {"run.ship", "run.merge"} and (not isinstance(payload.get("stage"), str) or not payload["stage"]):
        raise CanonicalizationError(f"{event_type} stage is required")
    expected_decision = APPROVAL_DECISION_EVENT_STATES.get(event_type)
    if expected_decision is not None and payload.get("decision_state") != expected_decision:
        raise CanonicalizationError(_bound(f"{event_type} requires decision_state {expected_decision!r}"))
    if event_type == "control.failed":
        if payload.get("error_class") not in CONTROL_FAILURE_CLASSES:
            raise CanonicalizationError("control.failed error_class is invalid")
        detail_digest = payload.get("detail_digest")
        if not isinstance(detail_digest, str) or not _HEX64.fullmatch(detail_digest):
            raise CanonicalizationError("control.failed detail_digest is invalid")
    if event_type.startswith("run_budget."):
        from brigade import run_budget

        run_budget.validate_run_budget_payload(event_type, payload)


def validate_event(env: Any) -> list[str]:
    """Return a list of bounded diagnostic strings for an envelope. Empty = valid."""
    errors: list[str] = []
    if not isinstance(env, Mapping):
        return ["envelope must be a JSON object"]

    unknown = sorted(set(env.keys()) - ENVELOPE_KEYS)
    if unknown:
        shown = ", ".join(unknown[:8])
        errors.append(_bound(f"unknown envelope keys: {shown}"))

    if env.get("schema") != SCHEMA:
        errors.append(_bound(f"schema must be {SCHEMA!r}"))
    schema_version = env.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(_bound(f"schema_version must be one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}"))

    run_id = env.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        errors.append(_bound(f"invalid run_id {run_id!r}"))

    sequence = env.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        errors.append(_bound(f"invalid sequence {sequence!r}"))

    event_type = env.get("event_type")
    if event_type not in EVENT_TYPES:
        errors.append(_bound(f"unknown event_type {event_type!r}"))
    elif schema_version == LEGACY_SCHEMA_VERSION and event_type in _SCHEMA_V2_EVENT_TYPES:
        errors.append(_bound(f"unknown event_type {event_type!r}"))
    else:
        try:
            _validate_payload(event_type, env.get("payload"))
        except CanonicalizationError as exc:
            errors.append(_bound(str(exc)))

    recorded_at = env.get("recorded_at")
    if not is_valid_recorded_at(recorded_at):
        errors.append(_bound(f"invalid recorded_at {recorded_at!r}"))

    idempotency_key = env.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        errors.append("idempotency_key must be a non-empty string")
    elif len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LEN:
        errors.append(_bound(f"idempotency_key exceeds {MAX_IDEMPOTENCY_KEY_LEN} chars"))

    request_digest_value = env.get("request_digest")
    if not (isinstance(request_digest_value, str) and bool(_HEX64.match(request_digest_value))):
        errors.append("request_digest must be a 64-char lowercase hex string")

    previous_digest = env.get("previous_digest")
    if "previous_digest" not in env:
        errors.append("previous_digest must be present (use null at sequence 1)")
    elif previous_digest is None:
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence != 1:
            errors.append("previous_digest must not be null after sequence 1")
    elif not (isinstance(previous_digest, str) and bool(_HEX64.match(previous_digest))):
        errors.append("previous_digest must be 64-char hex or null")

    event_digest = env.get("event_digest")
    if not (isinstance(event_digest, str) and bool(_HEX64.match(event_digest))):
        errors.append("event_digest must be a 64-char lowercase hex string")
        event_digest = None

    event_id = env.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        errors.append("event_id must be a non-empty string")

    if errors:
        return errors

    # Recompute digests and event_id for content binding (only when structurally valid).
    try:
        recomputed_digest = compute_event_digest(env)
    except CanonicalizationError as exc:
        errors.append(_bound(f"envelope is not canonicalizable: {exc}"))
        return errors
    if event_digest != recomputed_digest:
        errors.append(_bound("event_digest does not match recomputed digest"))
    assert isinstance(run_id, str)
    assert isinstance(sequence, int) and not isinstance(sequence, bool)
    expected_id = make_event_id(run_id=run_id, sequence=sequence, event_digest=recomputed_digest)
    if event_id != expected_id:
        errors.append(_bound("event_id does not match run_id/sequence/event_digest"))

    return errors
