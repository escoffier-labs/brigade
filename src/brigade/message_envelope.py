"""Inter-seat message envelopes and receive gates.

Slice 4 of ``brigade.provenance-envelope.v1``. Every planner, worker, and
synthesis send/receive boundary stamps one envelope over the exact outbound
UTF-8 bytes and verifies it again before the receiving parser or model sees
the body. Message bodies stay in memory unless an existing receipt already
keeps them. ``message-envelopes.jsonl`` stores routing metadata and the
envelope only.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from . import localio, provenance, trust_gate
from .untrusted import wrap_untrusted

SCHEMA = provenance.SCHEMA
CONTENT_SCOPE = "message.text.utf8.v1"
MESSAGE_ENVELOPES_NAME = "message-envelopes.jsonl"
LEGACY_MESSAGE_DISPLAY = "UNKNOWN PROVENANCE - legacy message"
MESSAGE_WRAP_MAX_BYTES = 20_000
SOURCE_SYSTEM = "run"
ASSIGNED_BY = "ingest:message_envelope.emit"
BRIGADE_SEAT = "brigade"

MESSAGE_KINDS = frozenset(
    {
        "plan-request",
        "plan-result",
        "worker-request",
        "worker-result",
        "synthesis-request",
        "synthesis-result",
    }
)
REQUEST_KINDS = frozenset({"plan-request", "worker-request", "synthesis-request"})
RESULT_KINDS = frozenset({"plan-result", "worker-result", "synthesis-result"})
KIND_PHASE = {
    "plan-request": "planning",
    "plan-result": "planning",
    "worker-request": "work",
    "worker-result": "work",
    "synthesis-request": "synthesis",
    "synthesis-result": "synthesis",
}
ALLOWLISTED_PRODUCERS = frozenset(
    {
        "aboyeur.build_plan_prompt",
        "aboyeur.plan",
        "aboyeur._worker_prompt",
        "aboyeur.build_synth_prompt",
        "aboyeur.run",
        "run_transport.dispatch",
        "run_transport._dag_dispatch",
    }
)
BLOCKED_LABELS = frozenset({"unknown", "quarantined"})
BLOCKED_INJECTION = frozenset({"pending", "error", "flagged"})
UPGRADED_LABELS = frozenset({"reviewed", "verified"})
MODEL_TOOL_ORIGINS = frozenset({"agent-session"})
MODEL_TOOL_MODALITIES = frozenset({"model-generated", "tool-output"})

_JSONL_LOCK = threading.Lock()
_RECEIPT_BODY_KEYS = frozenset(
    {
        "text",
        "body",
        "prompt",
        "message",
        "content",
        "stdout",
        "stderr",
        "raw",
        "transcript",
    }
)


@dataclass(frozen=True)
class MessageDelivery:
    message_id: str
    phase: str
    from_seat: str
    to_seat: str
    kind: str
    envelope: dict[str, Any]
    delivered: bool
    reason: str = ""
    display: str = ""


class MessageRejected(ValueError):
    """A gated inter-seat message must not be delivered."""


def _safe_label(value: str | None, fallback: str) -> str:
    if isinstance(value, str) and provenance.is_safe_identity_label(value):
        return value
    return fallback


def _safe_harness(value: str | None) -> str | None:
    if isinstance(value, str) and provenance.is_safe_identity_label(value):
        return value
    return None


def phase_for(kind: str) -> str:
    return KIND_PHASE.get(kind, "work")


def message_envelopes_path(run_dir: Path) -> Path:
    return run_dir / MESSAGE_ENVELOPES_NAME


def synthesize_legacy_message_provenance() -> tuple[dict[str, Any], str]:
    """Metadata-only unknown envelope for legacy run messages. Never hashed."""

    env: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": provenance.SCHEMA_VERSION,
        "source": {"system": "legacy", "kind": "legacy", "producer": "legacy.read_synthesis"},
        "origin": "unknown",
        "repository": None,
        "session": None,
        "collection_id": None,
        "item_id": None,
        "locator": None,
        "attribution": "inferred",
        "modality": "unknown",
        "trust": {
            "label": "unknown",
            "assigned_by": "ingest:legacy.read_synthesis",
            "assigned_at": None,
            "trust_policy": {
                "schema": provenance.TRUST_POLICY_SCHEMA,
                "schema_version": provenance.TRUST_POLICY_VERSION,
            },
            "injection": {"status": "clean", "count": 0, "rules": []},
        },
        "hashes": {
            "content_algorithm": provenance.HASH_ALGORITHM,
            "content_scope": CONTENT_SCOPE,
            "content": None,
            "raw_algorithm": None,
            "raw_scope": None,
            "raw": None,
        },
        "captured_at": None,
        "ingested_at": None,
    }
    return env, LEGACY_MESSAGE_DISPLAY


def envelope_from_message_record(record: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
    """Return the stored envelope or a synthesized legacy-message envelope."""

    if isinstance(record, Mapping):
        env = record.get("provenance")
        if isinstance(env, Mapping):
            return dict(env), ""
        if record.get("schema") == SCHEMA:
            return dict(record), ""
    return synthesize_legacy_message_provenance()


def truncate_utf8(text: str, max_bytes: int = MESSAGE_WRAP_MAX_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def is_model_or_tool_origin(env: Mapping[str, Any] | None) -> bool:
    """True when the envelope claims model-session or tool-output provenance."""

    if not isinstance(env, Mapping):
        return False
    return env.get("origin") in MODEL_TOOL_ORIGINS or env.get("modality") in MODEL_TOOL_MODALITIES


def receive_trust_label(env: Mapping[str, Any] | None) -> str:
    """Effective receive-side trust. Never taken from a stored upgrade.

    Model and tool-origin content stay ``untrusted`` (or the blocked label
    already on the envelope). ``reviewed`` / ``verified`` are not rendered
    from disk; those labels require authority proof at the gate.
    """

    if not isinstance(env, Mapping):
        return "unknown"
    trust = env.get("trust")
    stored = "unknown"
    if isinstance(trust, Mapping) and isinstance(trust.get("label"), str):
        stored = trust["label"]
    if is_model_or_tool_origin(env):
        if stored in BLOCKED_LABELS:
            return stored
        return "untrusted"
    if stored in UPGRADED_LABELS:
        return "untrusted"
    return stored


def wrap_message_body(text: str, env: Mapping[str, Any] | None) -> str:
    """Wrap admitted worker/prior-stage text as untrusted, byte-capped data."""

    label = receive_trust_label(env)
    body = truncate_utf8(text)
    wrapped = wrap_untrusted(body, source_kind="tool-output", max_chars=len(body))
    return f"[envelope trust.label={label}]\n{wrapped}"


def receipt_payload(delivery: MessageDelivery) -> dict[str, Any]:
    """Routing metadata plus envelope. Never includes a message body."""

    payload: dict[str, Any] = {
        "message_id": delivery.message_id,
        "phase": delivery.phase,
        "from_seat": delivery.from_seat,
        "to_seat": delivery.to_seat,
        "envelope": delivery.envelope,
    }
    if not delivery.delivered:
        payload["rejected"] = True
        payload["rejection_reason"] = delivery.reason
    return payload


def receipt_has_message_body(payload: Mapping[str, Any]) -> bool:
    return any(key in payload for key in _RECEIPT_BODY_KEYS)


def append_message_receipt(run_dir: Path, delivery: MessageDelivery) -> Path:
    path = message_envelopes_path(run_dir)
    payload = receipt_payload(delivery)
    if receipt_has_message_body(payload):
        raise ValueError("message receipt must not retain a message body")
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _JSONL_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    return path


def admit_message(
    text: str,
    env: Mapping[str, Any] | None,
    *,
    kind: str | None = None,
    producer: str | None = None,
) -> MessageDelivery:
    """Verify hash, channel, trust, scan state, and schema before delivery."""

    display = ""
    if not isinstance(env, Mapping):
        env, display = synthesize_legacy_message_provenance()
    envelope = dict(env)
    message_id = _safe_label(str(envelope.get("item_id") or ""), f"legacy-{uuid4().hex[:8]}")
    if not isinstance(kind, str) or not kind:
        return MessageDelivery(
            message_id=message_id,
            phase="work",
            from_seat="",
            to_seat="",
            kind="",
            envelope=envelope,
            delivered=False,
            reason="receive gate requires an explicit message kind",
            display=display,
        )
    if producer is None:
        return MessageDelivery(
            message_id=message_id,
            phase=phase_for(kind),
            from_seat="",
            to_seat="",
            kind=kind,
            envelope=envelope,
            delivered=False,
            reason="receive gate requires an explicit producer",
            display=display,
        )
    reason = _rejection_reason(text, envelope, kind=kind, producer=producer)
    return MessageDelivery(
        message_id=message_id,
        phase=phase_for(kind),
        from_seat="",
        to_seat="",
        kind=kind,
        envelope=envelope,
        delivered=reason is None,
        reason=reason or "",
        display=display or (LEGACY_MESSAGE_DISPLAY if reason and provenance.is_legacy_unknown(envelope) else ""),
    )


def _rejection_reason(
    text: str,
    env: Mapping[str, Any],
    *,
    kind: str,
    producer: str,
) -> str | None:
    errors = provenance.validate_envelope(env, inbound_adapter=True)
    if errors:
        return "invalid envelope: " + "; ".join(errors)
    if provenance.is_legacy_unknown(env):
        return "legacy message has unknown provenance and cannot be replayed"
    raw_source = env.get("source")
    source: Mapping[str, Any] = raw_source if isinstance(raw_source, Mapping) else {}
    env_kind = source.get("kind")
    env_producer = source.get("producer")
    if env_kind != kind:
        return f"envelope source.kind {env_kind!r} does not match receive channel {kind!r}"
    if env_producer != producer:
        return f"envelope source.producer {env_producer!r} does not match receive producer {producer!r}"
    raw_hashes = env.get("hashes")
    hashes: Mapping[str, Any] = raw_hashes if isinstance(raw_hashes, Mapping) else {}
    if hashes.get("content_scope") != CONTENT_SCOPE:
        return f"hashes.content_scope must be {CONTENT_SCOPE}"
    if not provenance.verify_content_digest(text, env):
        return "envelope content hash does not match outbound UTF-8 bytes"
    raw_trust = env.get("trust")
    trust: Mapping[str, Any] = raw_trust if isinstance(raw_trust, Mapping) else {}
    label = trust.get("label")
    if label in BLOCKED_LABELS:
        return f"trust label {label} is not deliverable"
    if label in UPGRADED_LABELS:
        if is_model_or_tool_origin(env):
            return f"trust label {label} is not deliverable for model/tool-origin content"
        return f"trust label {label} requires authority proof and is not deliverable on receive"
    status = provenance.injection_status(env)
    if status in BLOCKED_INJECTION:
        return f"injection status {status} is not deliverable"
    if kind not in MESSAGE_KINDS:
        return f"unknown message kind {kind!r}"
    if producer not in ALLOWLISTED_PRODUCERS:
        return f"producer {producer!r} is not an allowlisted inter-seat producer"
    return None


def emit(
    text: str,
    *,
    kind: str,
    producer: str,
    from_seat: str,
    to_seat: str,
    run_dir: Path | None = None,
    run_id: str | None = None,
    session_harness: str | None = None,
    repository_id: str = "workspace",
    repository_revision: str | None = None,
) -> MessageDelivery:
    """Stamp, gate, and optionally persist one inter-seat message envelope."""

    if kind not in MESSAGE_KINDS:
        raise ValueError(f"unknown message kind {kind!r}; expected one of {sorted(MESSAGE_KINDS)}")
    now = localio.utc_now_iso()
    resolved_run_id = _safe_label(run_id or (run_dir.name if run_dir is not None else None), "in-memory")
    message_id = _safe_label(f"{kind}-{uuid4().hex[:12]}", f"message-{uuid4().hex[:12]}")
    from_label = _safe_label(from_seat, BRIGADE_SEAT)
    to_label = _safe_label(to_seat, BRIGADE_SEAT)
    if kind in RESULT_KINDS:
        origin = "agent-session"
        modality = "model-generated"
    else:
        origin = "workspace"
        modality = "mixed"
    status, count, rules = trust_gate.scan_injection(text)
    trust_label = "untrusted"
    if status == "flagged":
        trust_label = "quarantined"
    elif status in {"pending", "error"}:
        trust_label = "untrusted"
    try:
        envelope = provenance.build_envelope(
            source_system=SOURCE_SYSTEM,
            source_kind=kind,
            source_producer=producer,
            origin=origin,
            repository_id=_safe_label(repository_id, "workspace"),
            repository_revision=repository_revision
            if repository_revision is None or provenance.is_safe_repository_revision(repository_revision)
            else None,
            session_id=resolved_run_id,
            session_harness=_safe_harness(session_harness),
            collection_id=resolved_run_id,
            item_id=message_id,
            locator_kind="repo-relative",
            locator_value=f".brigade/runs/{resolved_run_id}/{MESSAGE_ENVELOPES_NAME}",
            attribution="observed",
            modality=modality,
            trust_label=trust_label,
            trust_assigned_by=ASSIGNED_BY,
            trust_assigned_at=now,
            injection_status=status,
            injection_count=count,
            injection_rules=rules,
            text=text,
            raw_bytes=None,
            content_scope=CONTENT_SCOPE,
            captured_at=now,
            ingested_at=now,
        )
    except ValueError as exc:
        env, display = synthesize_legacy_message_provenance()
        delivery = MessageDelivery(
            message_id=message_id,
            phase=phase_for(kind),
            from_seat=from_label,
            to_seat=to_label,
            kind=kind,
            envelope=env,
            delivered=False,
            reason=str(exc),
            display=display,
        )
        if run_dir is not None:
            append_message_receipt(run_dir, delivery)
        return delivery

    reason = _rejection_reason(text, envelope, kind=kind, producer=producer)
    display = LEGACY_MESSAGE_DISPLAY if reason and provenance.is_legacy_unknown(envelope) else ""
    delivery = MessageDelivery(
        message_id=message_id,
        phase=phase_for(kind),
        from_seat=from_label,
        to_seat=to_label,
        kind=kind,
        envelope=envelope,
        delivered=reason is None,
        reason=reason or "",
        display=display,
    )
    if run_dir is not None:
        append_message_receipt(run_dir, delivery)
    return delivery


def require_delivery(delivery: MessageDelivery) -> None:
    if delivery.delivered:
        return
    raise MessageRejected(delivery.reason or "inter-seat message rejected by provenance gate")
