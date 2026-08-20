"""Shared trust-policy enforcement and auditable trust transitions.

Slice 6 of ``brigade.provenance-envelope.v1``. Consumers derive entitlements
from ``brigade.trust-policy.v1`` and fail closed on unknown, quarantined,
pending, flagged, or error injection state. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from . import localio, provenance
from .untrusted import scan_handoff_injection_heuristics

EVENT_SCHEMA = "brigade.provenance-event.v1"
EVENT_SCHEMA_VERSION = 1
WORK_EVENTS_REL = Path(".brigade") / "work" / "provenance-events.jsonl"
RESEARCH_EVENTS_NAME = "provenance-events.jsonl"
OPERATOR_REVIEW = "operator:brigade evidence trust review"
OPERATOR_VERIFY = "brigade receipts verify"

ITEM_REF_MISELEDGER = "miseledger:item:"
ITEM_REF_WORK_IMPORT = "work-import:"
ITEM_REF_RESEARCH = "research-finding:"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_POLICY_PATH = Path(__file__).resolve().parent / "fixtures" / "trust-policy.v1.json"
_POLICY_CACHE: dict[str, Any] | None = None

BodyMode = Literal["full", "wrapped", "metadata", "omit"]


@dataclass(frozen=True)
class ConsumerAdmission:
    label: str
    allowed: bool
    body_mode: BodyMode
    injection_status: str
    envelope: dict[str, Any]
    reason: str = ""


def load_trust_policy() -> dict[str, Any]:
    """Load the shared ``brigade.trust-policy.v1`` fixture."""

    global _POLICY_CACHE
    if _POLICY_CACHE is not None:
        return _POLICY_CACHE
    payload = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("trust policy fixture must be a JSON object")
    _POLICY_CACHE = payload
    return payload


def entitlements_for(label: str) -> frozenset[str]:
    policy = load_trust_policy()
    entitlements = policy.get("entitlements")
    if not isinstance(entitlements, dict):
        return frozenset()
    raw = entitlements.get(label)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(item for item in raw if isinstance(item, str))


def allows(label: str, entitlement: str) -> bool:
    return entitlement in entitlements_for(label)


def untrusted_caps() -> tuple[int, float]:
    policy = load_trust_policy()
    caps = policy.get("untrusted_caps")
    if not isinstance(caps, dict):
        return 2, 0.5
    max_items = caps.get("max_items", 2)
    max_fraction = caps.get("max_fraction", 0.5)
    items = max_items if isinstance(max_items, int) and not isinstance(max_items, bool) and max_items >= 0 else 2
    fraction = max_fraction if isinstance(max_fraction, (int, float)) and not isinstance(max_fraction, bool) else 0.5
    return items, float(fraction)


def _label_from_env(env: Mapping[str, Any] | None) -> str | None:
    if not isinstance(env, Mapping):
        return None
    trust = env.get("trust")
    if isinstance(trust, Mapping):
        label = trust.get("label")
        if label in provenance.TRUST_LABELS:
            return str(label)
    return None


def _is_envelope(mapping: Mapping[str, Any]) -> bool:
    """True when *mapping* is already a provenance envelope, not a host record."""

    if mapping.get("schema") == provenance.SCHEMA:
        return True
    return _label_from_env(mapping) is not None and isinstance(mapping.get("hashes"), Mapping)


def envelope_from_record(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the embedded envelope or a synthesized legacy unknown envelope."""

    if isinstance(record, Mapping):
        if _is_envelope(record):
            return dict(record)
        env = record.get("provenance")
        if isinstance(env, Mapping):
            return dict(env)
        metadata = record.get("metadata")
        if isinstance(metadata, Mapping):
            env = metadata.get("provenance")
            if isinstance(env, Mapping):
                return dict(env)
    synthesized, _display = provenance.synthesize_legacy_provenance()
    return synthesized


def trust_label_of(record_or_env: Mapping[str, Any] | None) -> str:
    if isinstance(record_or_env, Mapping) and _is_envelope(record_or_env):
        return _label_from_env(record_or_env) or "unknown"
    env = envelope_from_record(record_or_env)
    found = _label_from_env(env)
    if found:
        return found
    if isinstance(record_or_env, Mapping):
        for key in ("trust_label", "trust"):
            value = record_or_env.get(key)
            if value in provenance.TRUST_LABELS:
                return str(value)
    return "unknown"


def envelope_content_hash(env: Mapping[str, Any] | None) -> str | None:
    if not isinstance(env, Mapping):
        return None
    hashes = env.get("hashes")
    if not isinstance(hashes, Mapping):
        return None
    digest = hashes.get("content")
    if isinstance(digest, str) and _HEX64.match(digest):
        return digest
    return None


def item_body_text(record: Mapping[str, Any] | None) -> str:
    if not isinstance(record, Mapping):
        return ""
    for key in ("text", "snippet"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def injection_blocks_content(status: str) -> bool:
    """True when status is not the explicit known-safe ``clean`` value."""

    normalized = status.strip().lower() if isinstance(status, str) else ""
    return normalized != "clean"


def scan_injection(text: str) -> tuple[str, int, list[str]]:
    """Run the shared injection scan. Unavailable or incomplete → error."""

    try:
        hits = scan_handoff_injection_heuristics(text)
        warnings = [hit for hit in hits if hit.severity == "warning"]
    except Exception:
        return "error", 0, []
    if warnings:
        rules = sorted({hit.rule for hit in warnings if isinstance(hit.rule, str) and hit.rule})
        return "flagged", len(warnings), rules
    return "clean", 0, []


def resolve_pending_injection(text: str, env: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Resolve ``pending`` synchronously. Returns (envelope, status).

    A clean scan is a consumer-local release to ``untrusted`` and does not
    persist, but only when the item is not already ``quarantined``. A
    quarantined label stays quarantined regardless of injection resolution.
    Hit, error, or unavailability leaves the body ineligible.
    """

    updated = json.loads(json.dumps(env)) if isinstance(env, Mapping) else envelope_from_record(None)
    trust = updated.get("trust")
    if not isinstance(trust, dict):
        trust = {}
        updated["trust"] = trust
    injection = trust.get("injection")
    if not isinstance(injection, dict):
        injection = {"status": "pending", "count": 0, "rules": []}
        trust["injection"] = injection
    raw_status = injection.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    if status != "pending":
        return updated, status or "error"
    new_status, count, rules = scan_injection(text)
    injection["status"] = new_status
    injection["count"] = count
    injection["rules"] = rules
    if new_status == "flagged":
        trust["label"] = "quarantined"
    return updated, new_status


def admission_integrity_blocker(
    record: Mapping[str, Any] | None,
    *,
    require_complete_envelope: bool = True,
    env: Mapping[str, Any] | None = None,
    text: str | None = None,
) -> str | None:
    """Return a reason when envelope, digest, or redaction integrity fails.

    Promotion requires a complete envelope, a present exact content-digest
    match against current body bytes, and a redaction record that still
    describes those bytes. Other admissions still require the digest match
    so a same-UID rewrite of stored text cannot emit the replacement body.
    """

    resolved_env = dict(env) if isinstance(env, Mapping) else envelope_from_record(record)
    body = text if isinstance(text, str) else item_body_text(record)
    if require_complete_envelope:
        if provenance.validate_envelope(resolved_env):
            return "envelope is missing or malformed"
        redaction_errors = provenance.verify_redaction_against_bytes(body, resolved_env)
        if redaction_errors:
            if any("does not match current item text" in error for error in redaction_errors):
                return "redaction record does not match current item text"
            return "redaction record is missing or malformed"
    if not provenance.verify_content_digest(body, resolved_env, require_present=True):
        return "envelope content hash does not match current item text"
    return None


def admit_consumer(record: Mapping[str, Any] | None, *, entitlement: str) -> ConsumerAdmission:
    """Decide whether a record may enter a content-emitting consumer."""

    env = envelope_from_record(record)
    text = item_body_text(record)
    status = provenance.injection_status(env)
    if status == "pending":
        env, status = resolve_pending_injection(text, env)
        status = status.strip().lower() if isinstance(status, str) else "error"
    label = _label_from_env(env) or trust_label_of(record)
    if injection_blocks_content(status):
        return ConsumerAdmission(
            label=label,
            allowed=entitlement in {"brief", "brief_wrapped", "show", "search", "context"},
            body_mode="metadata",
            injection_status=status,
            envelope=env,
            reason=f"injection {status} emits metadata only",
        )
    if label in {"unknown", "quarantined"}:
        return ConsumerAdmission(
            label=label,
            allowed=False,
            body_mode="omit",
            injection_status=status,
            envelope=env,
            reason=f"trust label {label} is excluded from {entitlement}",
        )
    integrity = admission_integrity_blocker(
        record,
        require_complete_envelope=(entitlement == "promote"),
        env=env,
        text=text,
    )
    if integrity:
        metadata_only = entitlement in {"brief", "brief_wrapped", "show", "search"}
        return ConsumerAdmission(
            label=label,
            allowed=metadata_only,
            body_mode="metadata" if metadata_only else "omit",
            injection_status=status,
            envelope=env,
            reason=integrity,
        )
    if entitlement == "brief" and allows(label, "brief") and status == "clean":
        return ConsumerAdmission(label=label, allowed=True, body_mode="full", injection_status=status, envelope=env)
    if entitlement in {"brief", "brief_wrapped"} and allows(label, "brief_wrapped") and status == "clean":
        return ConsumerAdmission(label=label, allowed=True, body_mode="wrapped", injection_status=status, envelope=env)
    if allows(label, entitlement):
        return ConsumerAdmission(label=label, allowed=True, body_mode="full", injection_status=status, envelope=env)
    if (
        entitlement in {"cite", "promote", "context"}
        and label in {"untrusted", "reviewed", "verified"}
        and status == "clean"
    ):
        # Default work-import and research paths stay usable after a clean scan.
        # Higher labels inherit those surfaces so trust upgrades never revoke them.
        # Unknown and quarantined remain excluded above.
        return ConsumerAdmission(label=label, allowed=True, body_mode="full", injection_status=status, envelope=env)
    return ConsumerAdmission(
        label=label,
        allowed=False,
        body_mode="omit",
        injection_status=status,
        envelope=env,
        reason=f"trust label {label} lacks {entitlement}",
    )


def promotion_blocker(record: Mapping[str, Any] | None) -> str | None:
    integrity = admission_integrity_blocker(record, require_complete_envelope=True)
    if integrity:
        return integrity
    admission = admit_consumer(record, entitlement="promote")
    if admission.label in {"unknown", "quarantined"}:
        return f"trust label {admission.label} cannot be promoted"
    if injection_blocks_content(admission.injection_status):
        return f"injection {admission.injection_status} cannot be promoted"
    if not admission.allowed:
        return admission.reason or "trust policy forbids promotion"
    return None


def citation_allowed(record: Mapping[str, Any] | None) -> bool:
    admission = admit_consumer(record, entitlement="cite")
    if admission.label in {"unknown", "quarantined"}:
        return False
    if injection_blocks_content(admission.injection_status):
        return False
    return admission.allowed


def context_allowed(record: Mapping[str, Any] | None) -> bool:
    admission = admit_consumer(record, entitlement="context")
    if admission.label in {"unknown", "quarantined"}:
        return False
    if injection_blocks_content(admission.injection_status):
        return False
    return admission.allowed


def parse_item_ref(item_ref: str) -> tuple[str, str] | None:
    if not isinstance(item_ref, str) or not item_ref:
        return None
    if item_ref.startswith(ITEM_REF_MISELEDGER):
        ident = item_ref[len(ITEM_REF_MISELEDGER) :]
        return ("miseledger", ident) if ident else None
    if item_ref.startswith(ITEM_REF_WORK_IMPORT):
        ident = item_ref[len(ITEM_REF_WORK_IMPORT) :]
        return ("work-import", ident) if ident else None
    if item_ref.startswith(ITEM_REF_RESEARCH):
        ident = item_ref[len(ITEM_REF_RESEARCH) :]
        return ("research-finding", ident) if ident else None
    return None


def build_provenance_event(
    *,
    item_ref: str,
    from_label: str,
    to_label: str,
    envelope_content_hash: str,
    content_scope: str = "item.text.utf8.v1",
    operator_command: str,
    evidence: Mapping[str, Any] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": EVENT_SCHEMA,
        "schema_version": EVENT_SCHEMA_VERSION,
        "at": at or localio.utc_now_iso(),
        "item_ref": item_ref,
        "from_label": from_label,
        "to_label": to_label,
        "envelope_content_hash": envelope_content_hash,
        "content_scope": content_scope,
        "operator_command": operator_command,
        "evidence": dict(evidence) if isinstance(evidence, Mapping) else {},
    }


def apply_trust_label(
    env: Mapping[str, Any],
    *,
    to_label: str,
    assigned_by: str,
    assigned_at: str | None = None,
) -> dict[str, Any]:
    updated = json.loads(json.dumps(env))
    trust = updated.get("trust")
    if not isinstance(trust, dict):
        trust = {}
        updated["trust"] = trust
    trust["label"] = to_label
    trust["assigned_by"] = assigned_by
    trust["assigned_at"] = assigned_at or localio.utc_now_iso()
    return updated


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def work_events_path(target: Path) -> Path:
    return target.expanduser().resolve() / WORK_EVENTS_REL


def research_events_path(target: Path, run_id: str) -> Path:
    from .research import registry

    return registry.run_dir(target, run_id) / RESEARCH_EVENTS_NAME


def append_work_event(target: Path, event: Mapping[str, Any]) -> Path:
    path = work_events_path(target)
    _append_jsonl(path, event)
    return path


def append_research_event(target: Path, run_id: str, event: Mapping[str, Any]) -> Path:
    path = research_events_path(target, run_id)
    _append_jsonl(path, event)
    return path


def read_events(path: Path) -> list[dict[str, Any]]:
    return localio.read_jsonl_dicts(path)


def matching_transition(
    events: Sequence[Mapping[str, Any]],
    *,
    item_ref: str,
    to_label: str,
    envelope_content_hash: str,
) -> dict[str, Any] | None:
    for event in events:
        if (
            event.get("item_ref") == item_ref
            and event.get("to_label") == to_label
            and event.get("envelope_content_hash") == envelope_content_hash
        ):
            return dict(event)
    return None


def has_matching_transition(
    target: Path,
    *,
    item_ref: str,
    to_label: str,
    envelope_content_hash: str,
) -> bool:
    return (
        matching_transition(
            read_events(work_events_path(target)),
            item_ref=item_ref,
            to_label=to_label,
            envelope_content_hash=envelope_content_hash,
        )
        is not None
    )


class TrustReviewError(ValueError):
    """Operator review rejected because the envelope is not eligible."""


def _require_digest(value: str) -> str:
    digest = value.strip().lower() if isinstance(value, str) else ""
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if not _HEX64.match(digest):
        raise TrustReviewError("content hash must be a bare lowercase 64-char hex digest")
    return digest


def current_envelope_digest(env: Mapping[str, Any], text: str) -> str:
    digest = envelope_content_hash(env)
    recomputed = provenance.content_sha256(text)
    if digest is None:
        raise TrustReviewError("envelope is missing hashes.content")
    if digest != recomputed:
        raise TrustReviewError("envelope content hash does not match current item text")
    return digest


def review_work_import(
    target: Path,
    import_id: str,
    expected_hash: str,
    *,
    operator_command: str = OPERATOR_REVIEW,
) -> dict[str, Any]:
    from .work_cmd import ledger as ledger_mod

    expected = _require_digest(expected_hash)
    item, imports = ledger_mod._find_import(target, import_id)
    if item is None:
        raise TrustReviewError(f"import not found: {import_id}")
    env = envelope_from_record(item)
    text = str(item.get("text") or "")
    current = current_envelope_digest(env, text)
    if current != expected:
        raise TrustReviewError("content hash does not match the current envelope digest")
    item_ref = f"{ITEM_REF_WORK_IMPORT}{item.get('id')}"
    label = trust_label_of(env)
    if label == "reviewed" and has_matching_transition(
        target, item_ref=item_ref, to_label="reviewed", envelope_content_hash=current
    ):
        return {"status": "noop", "item_ref": item_ref, "to_label": "reviewed", "envelope_content_hash": current}
    if label in {"unknown", "quarantined"}:
        raise TrustReviewError(f"cannot review {label} content until classification or scan is resolved")
    if label != "untrusted":
        raise TrustReviewError(f"cannot review trust label {label}")
    now = localio.utc_now_iso()
    updated_env = apply_trust_label(env, to_label="reviewed", assigned_by=operator_command, assigned_at=now)
    raw_metadata = item.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    metadata["provenance"] = updated_env
    item["metadata"] = metadata
    item["updated_at"] = now
    ledger_mod._write_imports(target, imports)
    event = build_provenance_event(
        item_ref=item_ref,
        from_label=label,
        to_label="reviewed",
        envelope_content_hash=current,
        operator_command=operator_command,
        evidence={"kind": "operator-review"},
        at=now,
    )
    append_work_event(target, event)
    return {"status": "reviewed", "item_ref": item_ref, "event": event, "envelope": updated_env}


def review_research_finding(
    target: Path,
    run_id: str,
    index: int,
    expected_hash: str,
    *,
    operator_command: str = OPERATOR_REVIEW,
) -> dict[str, Any]:
    from .research import registry

    expected = _require_digest(expected_hash)
    path = registry.run_dir(target, run_id) / registry.FINDINGS_ARTIFACT
    payload = localio.read_json_dict(path)
    if payload is None:
        raise TrustReviewError(f"research findings not found: {run_id}")
    findings = payload.get("findings")
    if not isinstance(findings, list) or index < 0 or index >= len(findings):
        raise TrustReviewError(f"research finding index out of range: {index}")
    raw = findings[index]
    if not isinstance(raw, dict):
        raise TrustReviewError("research finding is not an object")
    env = envelope_from_record(raw)
    text = str(raw.get("text") or "")
    current = current_envelope_digest(env, text)
    if current != expected:
        raise TrustReviewError("content hash does not match the current envelope digest")
    item_ref = f"{ITEM_REF_RESEARCH}{run_id}:{index}"
    label = trust_label_of(env)
    existing = matching_transition(
        read_events(research_events_path(target, run_id)),
        item_ref=item_ref,
        to_label="reviewed",
        envelope_content_hash=current,
    )
    if label == "reviewed" and existing is not None:
        return {"status": "noop", "item_ref": item_ref, "to_label": "reviewed", "envelope_content_hash": current}
    if label in {"unknown", "quarantined"}:
        raise TrustReviewError(f"cannot review {label} content until classification or scan is resolved")
    if label != "untrusted":
        raise TrustReviewError(f"cannot review trust label {label}")
    now = localio.utc_now_iso()
    updated_env = apply_trust_label(env, to_label="reviewed", assigned_by=operator_command, assigned_at=now)
    raw["provenance"] = updated_env
    findings[index] = raw
    payload["findings"] = findings
    localio.write_json(path, payload)
    event = build_provenance_event(
        item_ref=item_ref,
        from_label=label,
        to_label="reviewed",
        envelope_content_hash=current,
        operator_command=operator_command,
        evidence={"kind": "operator-review"},
        at=now,
    )
    append_research_event(target, run_id, event)
    return {"status": "reviewed", "item_ref": item_ref, "event": event, "envelope": updated_env}


def review_item_ref(
    target: Path,
    item_ref: str,
    expected_hash: str,
    *,
    operator_command: str = OPERATOR_REVIEW,
) -> dict[str, Any]:
    parsed = parse_item_ref(item_ref)
    if parsed is None:
        raise TrustReviewError(
            "item ref must be miseledger:item:<id>, work-import:<import_id>, or research-finding:<run_id>:<index>"
        )
    kind, ident = parsed
    if kind == "work-import":
        return review_work_import(target, ident, expected_hash, operator_command=operator_command)
    if kind == "research-finding":
        run_id, sep, index_text = ident.partition(":")
        if not sep or not index_text.isdigit():
            raise TrustReviewError("research finding ref must be research-finding:<run_id>:<index>")
        return review_research_finding(
            target, run_id, int(index_text), expected_hash, operator_command=operator_command
        )
    return review_miseledger_item(target, ident, expected_hash, operator_command=operator_command)


def notify_miseledger_trust(
    target: Path,
    item_id: str,
    expected_hash: str,
    *,
    to_label: str,
    operator_command: str,
) -> None:
    from . import component_bins

    binary = component_bins.resolve("miseledger")
    if binary is None:
        raise TrustReviewError("miseledger binary is not available")
    command = [
        binary,
        "trust",
        "review",
        "--item",
        item_id,
        "--content-hash",
        expected_hash,
        "--to-label",
        to_label,
        "--operator-command",
        operator_command,
        "--json",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=target,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrustReviewError(f"miseledger trust review failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        raise TrustReviewError(detail)


def review_miseledger_item(
    target: Path,
    item_id: str,
    expected_hash: str,
    *,
    to_label: str = "reviewed",
    operator_command: str = OPERATOR_REVIEW,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected = _require_digest(expected_hash)
    item_ref = f"{ITEM_REF_MISELEDGER}{item_id}"
    if has_matching_transition(target, item_ref=item_ref, to_label=to_label, envelope_content_hash=expected):
        return {"status": "noop", "item_ref": item_ref, "to_label": to_label, "envelope_content_hash": expected}
    notify_miseledger_trust(
        target,
        item_id,
        expected,
        to_label=to_label,
        operator_command=operator_command,
    )
    event = build_provenance_event(
        item_ref=item_ref,
        from_label="untrusted",
        to_label=to_label,
        envelope_content_hash=expected,
        operator_command=operator_command,
        evidence=evidence or {"kind": "operator-review"},
    )
    append_work_event(target, event)
    return {"status": to_label, "item_ref": item_ref, "event": event}
