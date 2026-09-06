"""HTTP /policy adapter for the fleet control-plane document.

This module is the public transport for ``brigade.fleet_policy.v1``. It does
not migrate the legacy ``/models`` roster and never reports that authority as
synced. Resolve and prepare persist a pending receipt only; they do not mark
a session loaded.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Sequence

from . import (
    fleet_hub_policy,
    fleet_hub_routing,
    fleet_model_inventory,
    fleet_model_roster,
    fleet_policy,
    fleet_quota,
)
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden

POLICY_SCHEMA = fleet_policy.POLICY_SCHEMA
POLICY_SESSION_SCHEMA = fleet_policy.POLICY_SESSION_SCHEMA
MAX_POLICY_BODY_BYTES = fleet_policy.MAX_DOCUMENT_BYTES + 8192
NODE_ACTIONS = frozenset(
    {
        "resolve",
        "prepare",
        "acknowledge",
        "route",
        "work-next",
        "reservation-renew",
        "reservation-release",
        "telemetry-observe",
        "delegation-create",
        "delegation-show",
    }
)
ADMIN_ACTIONS = frozenset({"preview", "save", "rollback"})
SHARED_ACTIONS = frozenset({"refresh", "quota-ingest", "inventory-ingest"})
ACTIONS = NODE_ACTIONS | ADMIN_ACTIONS | SHARED_ACTIONS
ACK_STATUSES = frozenset({"applied", "failed"})
RESOLVE_PUBLIC_KEYS = ("schema", "version", "digest", "effective", "sources", "ack_required")
PREPARE_PUBLIC_KEYS = RESOLVE_PUBLIC_KEYS + ("selected", "instructions", "repo_identity", "context_hash")
ACK_PUBLIC_KEYS = (
    "schema",
    "session_id",
    "consumer",
    "repo_identity",
    "version",
    "digest",
    "state",
    "loaded_at",
    "applied",
    "context_hash",
)
CONTEXT_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_RE = re.compile(r"(?i)(bearer\s+\S+|token[=:]\s*\S+)")


class FleetPolicyApiError(FleetHubError):
    def __init__(self, code: str, message: str, *, status: int = 400, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra or {}

    def payload(self) -> dict[str, Any]:
        body = {"error": {"code": self.code, "message": scrub_text(str(self))}}
        body.update(self.extra)
        return body


def scrub_text(value: str) -> str:
    text = _SECRET_RE.sub("[redacted]", value)
    if len(text) > 512:
        return text[:509] + "..."
    return text


def _error(code: str, message: str, *, status: int = 400, extra: dict[str, Any] | None = None) -> FleetPolicyApiError:
    return FleetPolicyApiError(code, message, status=status, extra=extra)


def _require_mapping(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _error("invalid-request", "policy request must be a JSON object")
    return dict(raw)


def _text(raw: Any, field: str, *, required: bool = True) -> str | None:
    if raw is None:
        if required:
            raise _error("invalid-request", f"policy field '{field}' is required")
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise _error("invalid-request", f"policy field '{field}' must be a non-empty string")
    if len(raw) > fleet_policy.MAX_TEXT:
        raise _error("invalid-request", f"policy field '{field}' is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise _error("invalid-request", f"policy field '{field}' must not contain control characters")
    return raw.strip()


def _repo_identity(raw: Any) -> str | None:
    from .fleet_session_presence import validate_repo_identity

    value = _text(raw, "repo_identity", required=False)
    if value is None:
        return None
    if value.startswith("github:") or "://" in value:
        raise _error("invalid-request", "repo_identity must be a canonical hostname/path, not github: prefix")
    identity = validate_repo_identity(value)
    if identity is None or fleet_policy.IDENTITY_PATTERN.match(identity) is None:
        raise _error("invalid-request", "repo_identity must be a credential-free canonical id")
    return identity


def _int(raw: Any, field: str, *, required: bool = True) -> int | None:
    if raw is None:
        if required:
            raise _error("invalid-request", f"policy field '{field}' is required")
        return None
    if type(raw) is not int:
        raise _error("invalid-request", f"policy field '{field}' must be an integer")
    return raw


def _reject_spoofed_node(body: Mapping[str, Any], caller_node: str | None) -> None:
    if "node_id" not in body:
        return
    claimed = body.get("node_id")
    if caller_node is None:
        return
    if claimed != caller_node:
        raise _error("auth-failed", "policy field 'node_id' is not allowed on node writes", status=403)


def _origin(document: Mapping[str, Any], *, origin: str, caller_node: str | None, is_admin: bool) -> str:
    if origin == "local":
        return "local"
    if caller_node is not None and origin == caller_node:
        return origin
    machines = document.get("machines") or {}
    record = machines.get(origin)
    if isinstance(record, Mapping):
        mapped = record.get("node_id")
        if caller_node is not None and mapped == caller_node:
            return origin
        if is_admin:
            return origin
    raise _error("invalid-request", "origin must be the authenticated node, local, or a mapped machine alias")


def _coverage(document: Mapping[str, Any], consumer: str) -> str:
    record = document.get("consumers", {}).get(consumer)
    if not isinstance(record, Mapping):
        return "unknown"
    return str(record.get("coverage") or "unverified")


def _require_enrolled(document: Mapping[str, Any], consumer: str) -> dict[str, Any]:
    record = document.get("consumers", {}).get(consumer)
    if not isinstance(record, Mapping):
        raise _error(
            "enrollment-required",
            f"consumer '{consumer}' is not enrolled in the current policy",
            extra={"coverage": "unknown"},
        )
    return dict(record)


def _overrides_present(overrides: Any) -> bool:
    if not overrides:
        return False
    try:
        parsed = fleet_policy.parse_settings(overrides, "session overrides")
    except fleet_policy.FleetPolicyError as exc:
        raise _error("invalid-request", str(exc)) from exc
    return any(parsed[section] for section in parsed)


def _envelope(
    current: Mapping[str, Any],
    resolved: Mapping[str, Any],
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": POLICY_SCHEMA,
        "version": current["revision"],
        "digest": current["digest"],
        "effective": resolved["effective"],
        "sources": resolved["sources"],
        "ack_required": True,
    }
    if extra:
        payload.update(dict(extra))
        return {key: payload[key] for key in PREPARE_PUBLIC_KEYS}
    return {key: payload[key] for key in RESOLVE_PUBLIC_KEYS}


def _instructions(
    current: Mapping[str, Any],
    resolved: Mapping[str, Any],
    *,
    origin: str,
    selected: Mapping[str, Any] | None = None,
) -> str:
    consumer = resolved.get("consumer")
    coverage = _coverage(current["document"], str(consumer) if consumer else "")
    reload = "none"
    record = current["document"].get("consumers", {}).get(consumer) if consumer else None
    if isinstance(record, Mapping):
        reload = str(record.get("reload") or "none")
    lines = [
        f"schema={POLICY_SCHEMA} version={current['revision']} digest={current['digest']}",
        f"origin={origin} (native origin is local; worker routing is separate)",
        f"consumer={consumer} coverage={coverage} reload={reload}",
        "effective:",
    ]
    for path in sorted(resolved.get("sources") or {}):
        source = resolved["sources"][path]
        lines.append(f"  {path}={source.get('value')!r} layer={source.get('layer')}")
    if selected:
        lines.append(
            "selected "
            f"seat={selected.get('seat')} provider={selected.get('provider')} "
            f"model={selected.get('model')} instance_id={selected.get('instance_id')}"
        )
    lines.append("ack_required=true")
    return "\n".join(lines)


def _context_hash(
    *,
    current: Mapping[str, Any],
    consumer: str,
    repo_identity: str | None,
    origin: str,
    selected: Mapping[str, Any] | None,
    effective: Mapping[str, Any],
    sources: Mapping[str, Any],
    overrides: Mapping[str, Any] | None,
    override_reason: str | None,
    instructions: str | None,
    session_id: str,
    owner_node: str,
    decision_id: str | None = None,
    delegation_id: str | None = None,
) -> str:
    material = fleet_policy.canonical_json(
        {
            "schema": POLICY_SCHEMA,
            "version": current["revision"],
            "digest": current["digest"],
            "consumer": consumer,
            "repo_identity": repo_identity,
            "session_id": session_id,
            "owner_node": owner_node,
            "origin": origin,
            "effective": effective,
            "sources": sources,
            "overrides": overrides or {},
            "override_reason": override_reason,
            "instructions": instructions,
            "selected": selected,
            "decision_id": decision_id,
            "delegation_id": delegation_id,
        }
    )
    return "sha256:" + hashlib.sha256(material.encode("ascii")).hexdigest()


def _resolve_document(
    conn: Any,
    body: Mapping[str, Any],
    *,
    caller_node: str | None,
    is_admin: bool,
    source: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    current = fleet_hub_policy.current_policy(conn)
    delegation_id = _text(body.get("delegation_id"), "delegation_id", required=False)
    decision_id = _text(body.get("decision_id"), "decision_id", required=False)
    delegation: dict[str, Any] | None = None
    decision_row: dict[str, Any] | None = None

    if delegation_id:
        if caller_node is None:
            raise _error("auth-failed", "a node token is required to prepare a delegated session", status=403)
        from . import fleet_policy_delegation

        delegation = fleet_policy_delegation.validate_delegation(
            conn, delegation_id, caller_node, phase="prepare" if source == "prepare" else "read"
        )
        decision_id = str(delegation["decision_id"])
        decision_row = fleet_hub_routing._decision_row(conn, decision_id=decision_id)
        if decision_row is None:
            raise _error("invalid-request", f"fleet routing decision {decision_id!r} does not exist")

        body_consumer = body.get("consumer")
        if body_consumer is not None and body_consumer != delegation["consumer"]:
            raise _error("invalid-request", "consumer does not match the delegation")
        consumer = str(delegation["consumer"])

        body_repo = body.get("repo_identity") or body.get("repo")
        if body_repo is not None and body_repo != delegation["repo_identity"]:
            raise _error("invalid-request", "repository does not match the delegation")
        repo = _repo_identity(delegation["repo_identity"])

        body_session = body.get("session_id")
        if body_session is not None and body_session != delegation["session_id"]:
            raise _error("invalid-request", "session does not match the delegation")
        session_id = str(delegation["session_id"])

        body_dec = body.get("decision_id")
        if body_dec is not None and body_dec != delegation["decision_id"]:
            raise _error("invalid-request", "decision does not match the delegation")

        stored_origin = str(decision_row.get("_origin") or decision_row.get("_request_document", {}).get("origin"))
        origin_node = str(delegation["origin_node"])
        body_origin = body.get("origin")
        if body_origin is not None and body_origin not in (stored_origin, origin_node):
            raise _error("invalid-request", "origin does not match the stored decision")
        origin = stored_origin

        overrides = decision_row.get("_request_document", {}).get("overrides") or {}
        reason = decision_row.get("_override_reason")

    elif decision_id:
        if caller_node is None:
            raise _error("auth-failed", "a node token is required to resolve or prepare a stored decision", status=403)
        decision_row = fleet_hub_routing._decision_row(conn, decision_id=decision_id)
        if decision_row is None:
            raise _error("invalid-request", f"fleet routing decision {decision_id!r} does not exist")

        selected_machine = decision_row.get("_selected_machine")
        target_node = (
            fleet_hub_routing._node_for_machine(current["document"], selected_machine) if selected_machine else None
        )
        if target_node != caller_node:
            raise _error("auth-failed", "only the selected target node may recover this decision origin", status=403)

        reservation_id = decision_row.get("_reservation_id")
        now = fleet_hub_routing._now()
        live = fleet_hub_routing._reservation_live(conn, reservation_id, now=now)
        if live is None or live.get("state") == "released":
            raise _error("invalid-request", "fleet routing reservation is not live")
        expires = fleet_hub_routing._parse_stamp(live.get("expires_at"))
        if expires is None or expires <= now:
            raise _error("invalid-request", "fleet routing reservation expired unclaimed and cannot launch")

        if (
            decision_row.get("_policy_version") != current["revision"]
            or decision_row.get("_policy_digest") != current["digest"]
        ):
            raise _error("revision-conflict", "current policy does not match the stored decision", status=409)

        dec_consumer = decision_row.get("_consumer")
        body_consumer = body.get("consumer")
        if body_consumer is not None and body_consumer != dec_consumer:
            raise _error("invalid-request", "consumer does not match the stored decision")
        if dec_consumer:
            consumer = str(dec_consumer)
        else:
            raw_c = _text(body.get("consumer"), "consumer")
            assert raw_c is not None
            consumer = raw_c

        dec_repo = decision_row.get("_repo_identity")
        body_repo = body.get("repo_identity") or body.get("repo")
        if body_repo is not None and body_repo != dec_repo:
            raise _error("invalid-request", "repository does not match the stored decision")
        repo = dec_repo or _repo_identity(body_repo)

        dec_session = decision_row.get("_session_id")
        body_session = body.get("session_id")
        if body_session is not None and body_session != dec_session:
            raise _error("invalid-request", "session does not match the stored decision")
        if dec_session:
            session_id = str(dec_session)
        else:
            raw_s = _text(body.get("session_id"), "session_id")
            assert raw_s is not None
            session_id = raw_s

        stored_origin = str(decision_row.get("_origin") or decision_row.get("_request_document", {}).get("origin"))
        origin_node = str(decision_row.get("_origin_node"))
        body_origin = body.get("origin")
        if body_origin is not None and body_origin not in (stored_origin, origin_node):
            raise _error("invalid-request", "origin does not match the stored decision")
        origin = stored_origin

        overrides = decision_row.get("_request_document", {}).get("overrides") or {}
        reason = decision_row.get("_override_reason")

    else:
        raw_c = _text(body.get("consumer"), "consumer")
        assert raw_c is not None
        consumer = raw_c
        repo = _repo_identity(body.get("repo_identity") or body.get("repo"))
        raw_s = _text(body.get("session_id"), "session_id")
        assert raw_s is not None
        session_id = raw_s
        origin_raw = _text(body.get("origin"), "origin")
        assert origin_raw is not None
        origin = _origin(current["document"], origin=origin_raw, caller_node=caller_node, is_admin=is_admin)
        overrides = body.get("overrides") if body.get("overrides") is not None else {}
        if overrides in ({}, None):
            overrides = {}
        elif not isinstance(overrides, Mapping):
            raise _error("invalid-request", "policy field 'overrides' must be a JSON object")
        reason = _text(body.get("override_reason"), "override_reason", required=False)

    if caller_node is None:
        raise _error("auth-failed", "a node token is required to resolve or prepare policy", status=403)

    _require_enrolled(current["document"], consumer)
    if _overrides_present(overrides) and not reason:
        raise _error("invalid-request", "override_reason is required when overrides are present")

    expected_version = _int(body.get("expected_version"), "expected_version", required=False)
    expected_digest = _text(body.get("expected_digest"), "expected_digest", required=False)
    pending = fleet_hub_policy.find_pending_policy(
        conn, session_id=session_id, consumer=consumer, repo_identity=repo, node_id=caller_node
    )
    if pending is not None and (expected_version is not None or expected_digest is not None):
        if expected_version not in (None, current["revision"]) or expected_digest not in (None, current["digest"]):
            raise _error(
                "revision-conflict",
                f"expected {expected_version or expected_digest}, current {current['revision']}",
                status=409,
            )
    try:
        resolved = fleet_policy.resolve_policy(
            current["document"], consumer, repo, overrides=overrides, override_reason=reason
        )
    except fleet_policy.FleetPolicyError as exc:
        raise _error("invalid-request", str(exc)) from exc
    pending_row = fleet_hub_policy.record_pending_policy(
        conn,
        session_id=session_id,
        consumer=consumer,
        node_id=caller_node,
        repo_identity=repo,
        origin=origin,
        revision=current["revision"],
        digest=current["digest"],
        overrides=overrides,
        override_reason=reason,
        effective=resolved["effective"],
        sources=resolved["sources"],
        source=source,
    )
    return current, resolved, pending_row, delegation, decision_row


def _select_seat(
    conn: Any,
    current: Mapping[str, Any],
    resolved: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    consumer: str,
    delegation: Mapping[str, Any] | None = None,
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    provider = _text(body.get("provider"), "provider")
    model = _text(body.get("model"), "model")
    instance_id = _text(body.get("instance_id"), "instance_id")
    reasoning = _text(body.get("reasoning") or body.get("effort"), "reasoning", required=False)
    assert provider is not None and model is not None and instance_id is not None

    if delegation is not None:
        seat_name = str(delegation["seat"])
        seat = current["document"]["seats"].get(seat_name)
        if not isinstance(seat, Mapping):
            raise _error("seat-unresolved", "no configured seat binding matches the requested identity")
        if not seat["enabled"]:
            raise _error("seat-disabled", f"seat '{seat_name}' is disabled")
        effective = fleet_policy.effective_seat_bindings(current["document"], consumer, seat_name)
        t3_b = effective.get("t3_fleet") or {}
        nat_b = effective.get("native") or {}
        expected_model = nat_b.get("model") or t3_b.get("model") or seat["model"]
        expected_instance = nat_b.get("instance_id") or t3_b.get("instance_id")

        if seat["provider"] != provider:
            raise _error("seat-unresolved", "no configured seat binding matches the requested identity")
        if expected_instance is None or expected_instance != instance_id:
            raise _error("seat-unresolved", "no configured seat binding matches the requested identity")
        if model != expected_model:
            raise _error("seat-unresolved", "no configured seat binding matches the requested identity")
        if reasoning is not None and seat["effort"] != reasoning:
            raise _error("seat-unresolved", "no configured seat binding matches the requested identity")

        admission = fleet_policy.admissible_seat(current["document"], seat_name, resolved)
        if "training-not-permitted" in admission["reasons"]:
            raise _error("training-disallowed", f"seat '{seat_name}' is not permitted for this repository")
        if not admission["admissible"]:
            raise _error("seat-unresolved", f"seat '{seat_name}' is not admissible ({', '.join(admission['reasons'])})")
        retired = fleet_model_roster.retired_reason(provider, model)
        if retired:
            raise _error("retired", f"model '{model}' is retired ({retired})")

        selected = {
            "seat": seat_name,
            "provider": seat["provider"],
            "model": seat["model"],
            "reasoning": reasoning or seat["effort"],
            "instance_id": instance_id,
        }
        if model != seat["model"]:
            selected["launch_model"] = model
        return selected

    matches = fleet_policy.matching_seats(
        current["document"],
        consumer,
        provider=provider,
        model=model,
        instance_id=instance_id,
        reasoning=reasoning,
    )
    if not matches:
        consumer_record = current["document"]["consumers"][consumer]
        if consumer_record.get("legacy_bootstrap"):
            matches = _legacy_bootstrap_seats(conn, provider=provider, model=model, instance_id=instance_id)
        if not matches:
            raise _error("seat-unresolved", "no configured seat binding matches the requested identity")

    if decision is not None:
        decision_seat = decision.get("_selected_seat")
        if decision_seat:
            if decision_seat not in matches:
                raise _error("seat-unresolved", "requested identity does not match the decision's selected seat")
            seat_name = str(decision_seat)
        else:
            if len(matches) > 1:
                raise _error("ambiguous-seat", "multiple configured seat bindings match the requested identity")
            seat_name = matches[0]
    else:
        if len(matches) > 1:
            raise _error("ambiguous-seat", "multiple configured seat bindings match the requested identity")
        seat_name = matches[0]

    seat = current["document"]["seats"].get(seat_name)
    if not isinstance(seat, Mapping):
        raise _error("seat-unresolved", "no configured seat binding matches the requested identity")
    if not seat["enabled"]:
        raise _error("seat-disabled", f"seat '{seat_name}' is disabled")
    admission = fleet_policy.admissible_seat(current["document"], seat_name, resolved)
    if "training-not-permitted" in admission["reasons"]:
        raise _error("training-disallowed", f"seat '{seat_name}' is not permitted for this repository")
    if not admission["admissible"]:
        raise _error("seat-unresolved", f"seat '{seat_name}' is not admissible ({', '.join(admission['reasons'])})")
    retired = fleet_model_roster.retired_reason(provider, model)
    if retired:
        raise _error("retired", f"model '{model}' is retired ({retired})")

    selected = {
        "seat": seat_name,
        "provider": seat["provider"],
        "model": seat["model"],
        "reasoning": reasoning or seat["effort"],
        "instance_id": instance_id,
    }
    if model != seat["model"]:
        selected["launch_model"] = model
    return selected


def _legacy_bootstrap_seats(conn: Any, *, provider: str, model: str, instance_id: str) -> list[str]:
    try:
        from . import fleet_hub_model_roster

        roster = fleet_hub_model_roster.project_roster(conn, audience_node_id=None)
    except Exception:
        return []
    names: list[str] = []
    for seat in roster.get("seats") or []:
        if not isinstance(seat, Mapping):
            continue
        raw_bindings = seat.get("bindings")
        bindings = dict(raw_bindings) if isinstance(raw_bindings, Mapping) else {}
        raw_t3 = bindings.get("t3_fleet")
        t3 = dict(raw_t3) if isinstance(raw_t3, Mapping) else {}
        raw_native = bindings.get("native")
        native = dict(raw_native) if isinstance(raw_native, Mapping) else {}
        if seat.get("provider") != provider or seat.get("model") != model:
            continue
        if instance_id not in {t3.get("instance_id"), native.get("instance_id")}:
            continue
        name = seat.get("seat")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _ack_payload(
    row: Mapping[str, Any],
    *,
    session_id: str,
    applied: bool,
) -> dict[str, Any]:
    state = row.get("state")
    if not isinstance(state, str) or not state:
        current_revision = row.get("current_revision")
        if type(current_revision) is not int:
            current_revision = int(row["revision"]) if row.get("revision") is not None else -1
        reload = row.get("reload") if isinstance(row.get("reload"), str) else None
        state = fleet_hub_policy.session_state(row, current_revision, reload)
    payload = {
        "schema": POLICY_SESSION_SCHEMA,
        "session_id": session_id,
        "consumer": row.get("consumer"),
        "repo_identity": row.get("repo_identity"),
        "version": row.get("revision"),
        "digest": row.get("digest"),
        "state": state,
        "loaded_at": row.get("loaded_at"),
        "applied": applied,
        "context_hash": row.get("context_hash"),
    }
    return {key: payload[key] for key in ACK_PUBLIC_KEYS}


def _work_next(conn: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    """Read-only next-burn-item lookup. Never reserves, persists, or dispatches."""
    unknown = sorted(set(body).difference({"action", "consumer", "workload"}))
    if unknown:
        raise _error("invalid-request", f"unknown work-next field(s): {', '.join(unknown)}")
    consumer = _text(body.get("consumer"), "consumer", required=False) or "brigade-run"
    workload = _text(body.get("workload"), "workload", required=False) or "general"
    return fleet_hub_routing.work_next(conn, consumer=consumer, workload=workload)


def handle_policy(
    conn: Any,
    raw: Any,
    *,
    caller_node: str | None,
    is_admin: bool,
) -> tuple[int, dict[str, Any]]:
    """Dispatch one POST /policy action. Tokens are never included in the body."""
    body = _require_mapping(raw)
    _reject_spoofed_node(body, caller_node)
    action = _text(body.get("action"), "action")
    if action not in ACTIONS:
        raise _error("invalid-request", f"policy action must be one of: {', '.join(sorted(ACTIONS))}")
    if action in ADMIN_ACTIONS and not is_admin:
        raise _error("auth-failed", "the admin token is required for policy document writes", status=403)
    if action in NODE_ACTIONS and caller_node is None:
        raise _error(
            "auth-failed",
            "enroll this node with 'brigade fleet nodes add' and configure its node token",
            status=403,
        )
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        if action == "preview":
            expected_version = _int(body.get("expected_version"), "expected_version")
            reason = _text(body.get("reason"), "reason")
            assert expected_version is not None and reason is not None
            result = fleet_hub_policy.preview_policy(
                conn,
                body.get("document"),
                expected_version=expected_version,
                actor="admin" if is_admin else (caller_node or "node"),
                reason=reason,
            )
            status_payload: tuple[int, dict[str, Any]] = (200, result)
        elif action == "save":
            expected_version = _int(body.get("expected_version"), "expected_version")
            reason = _text(body.get("reason"), "reason")
            assert expected_version is not None and reason is not None
            saved = fleet_hub_policy.save_policy(
                conn,
                body.get("document"),
                expected_version=expected_version,
                actor="admin",
                reason=reason,
            )
            status_payload = (200, saved)
        elif action == "rollback":
            to_revision = _int(body.get("revision") or body.get("to_revision"), "revision")
            expected_version = _int(body.get("expected_version"), "expected_version")
            reason = _text(body.get("reason"), "reason")
            assert to_revision is not None and expected_version is not None and reason is not None
            rolled = fleet_hub_policy.rollback_policy(
                conn,
                to_revision=to_revision,
                expected_version=expected_version,
                actor="admin",
                reason=reason,
            )
            status_payload = (200, rolled)
        elif action in {"resolve", "prepare"}:
            current, resolved, pending, delegation, decision_row = _resolve_document(
                conn, body, caller_node=caller_node, is_admin=is_admin, source=action
            )
            extra: dict[str, Any] | None = None
            if action == "prepare":
                selected = _select_seat(
                    conn,
                    current,
                    resolved,
                    body,
                    consumer=str(resolved["consumer"]),
                    delegation=delegation,
                    decision=decision_row,
                )
                instructions = _instructions(current, resolved, origin=pending["origin"], selected=selected)
                if delegation is not None:
                    decision_id = str(delegation["decision_id"])
                    delegation_id = str(delegation["delegation_id"])
                elif decision_row is not None and decision_row.get("decision_id"):
                    decision_id = str(decision_row["decision_id"])
                    delegation_id = None
                else:
                    decision_id = None
                    delegation_id = None
                context_hash = _context_hash(
                    current=current,
                    consumer=str(resolved["consumer"]),
                    repo_identity=pending["repo_identity"],
                    origin=pending["origin"],
                    selected=selected,
                    effective=pending["effective"],
                    sources=pending["sources"],
                    overrides=pending["overrides"],
                    override_reason=pending["override_reason"],
                    instructions=instructions,
                    session_id=pending["session_id"],
                    owner_node=pending["owner_node"],
                    decision_id=decision_id,
                    delegation_id=delegation_id,
                )
                pending = fleet_hub_policy.record_pending_policy(
                    conn,
                    session_id=pending["session_id"],
                    consumer=pending["consumer"],
                    node_id=pending["owner_node"],
                    repo_identity=pending["repo_identity"],
                    origin=pending["origin"],
                    revision=pending["revision"],
                    digest=pending["digest"],
                    overrides=pending["overrides"],
                    override_reason=pending["override_reason"],
                    effective=pending["effective"],
                    sources=pending["sources"],
                    selected=selected,
                    instructions=instructions,
                    context_hash=context_hash,
                    source="prepare",
                )
                extra = {
                    "selected": selected,
                    "instructions": instructions,
                    "repo_identity": pending["repo_identity"],
                    "context_hash": context_hash,
                }
            status_payload = (200, _envelope(current, resolved, extra=extra))
        elif action == "delegation-create":
            if caller_node is None:
                raise _error("auth-failed", "a node token is required to create a delegation", status=403)
            from . import fleet_policy_delegation

            status_payload = (200, fleet_policy_delegation.create_delegation(conn, body, caller_node))
        elif action == "delegation-show":
            if caller_node is None:
                raise _error("auth-failed", "a node token is required to show a delegation", status=403)
            from . import fleet_policy_delegation

            delegation_id = _text(body.get("delegation_id"), "delegation_id")
            assert delegation_id is not None
            status_payload = (200, fleet_policy_delegation.show_delegation(conn, delegation_id, caller_node))
        elif action == "acknowledge":
            status_payload = (200, _acknowledge(conn, body, caller_node=caller_node))
        elif action == "refresh":
            status_payload = (200, _refresh(conn, body, caller_node=caller_node, is_admin=is_admin))
        elif action == "route":
            if caller_node is None:
                raise _error("auth-failed", "a node token is required to route", status=403)
            routed = {key: value for key, value in body.items() if key != "action"}
            status_payload = (200, fleet_hub_routing.public_route(fleet_hub_routing.route(conn, routed, caller_node)))
        elif action == "work-next":
            if caller_node is None:
                raise _error("auth-failed", "a node token is required to read the next burn item", status=403)
            status_payload = (200, _work_next(conn, body))
        elif action == "reservation-renew":
            if caller_node is None:
                raise _error("auth-failed", "a node token is required to renew a reservation", status=403)
            reserved = {key: value for key, value in body.items() if key != "action"}
            status_payload = (200, fleet_hub_routing.renew_reservation(conn, reserved, caller_node))
        elif action == "reservation-release":
            if caller_node is None:
                raise _error("auth-failed", "a node token is required to release a reservation", status=403)
            reserved = {key: value for key, value in body.items() if key != "action"}
            status_payload = (200, fleet_hub_routing.release_reservation(conn, reserved, caller_node))
        elif action == "telemetry-observe":
            if caller_node is None:
                raise _error("auth-failed", "a node token is required to observe telemetry", status=403)
            observed = {key: value for key, value in body.items() if key != "action"}
            status_payload = (200, fleet_hub_routing.observe_machine(conn, observed, caller_node))
        elif action == "quota-ingest":
            status_payload = (200, _ingest_quota(conn, body, caller_node=caller_node, is_admin=is_admin))
        elif action == "inventory-ingest":
            status_payload = (200, _ingest_inventory(conn, body, caller_node=caller_node, is_admin=is_admin))
        else:
            raise _error("invalid-request", f"unsupported policy action {action!r}")
        if opened:
            conn.commit()
            opened = False
        return status_payload
    except FleetHubConflict as exc:
        raise _error("revision-conflict", str(exc), status=409) from exc
    except FleetHubForbidden as exc:
        raise _error("auth-failed", str(exc), status=403) from exc
    except FleetPolicyApiError:
        raise
    except FleetHubError as exc:
        message = str(exc)
        if "policy_revision_conflict" in message:
            raise _error("revision-conflict", message, status=409) from exc
        raise _error("invalid-request", message) from exc
    finally:
        if opened:
            conn.rollback()


def _session_row_with_state(conn: Any, recorded: Mapping[str, Any]) -> dict[str, Any]:
    states = {row["session_id"]: row for row in fleet_hub_policy.list_session_states(conn)}
    row = {**recorded, **states.get(recorded["session_id"], {})}
    if row.get("state") is None:
        current = fleet_hub_policy.current_policy(conn)
        consumers = current["document"]["consumers"]
        record = consumers.get(row.get("consumer"))
        capability = record["reload"] if isinstance(record, Mapping) else None
        row["state"] = fleet_hub_policy.session_state(row, current["revision"], capability)
        row["current_revision"] = current["revision"]
    return row


def _acknowledge(conn: Any, body: Mapping[str, Any], *, caller_node: str | None) -> dict[str, Any]:
    if caller_node is None:
        raise _error("auth-failed", "a node token is required to acknowledge policy", status=403)
    consumer = _text(body.get("consumer"), "consumer")
    session_id = _text(body.get("session_id"), "session_id")
    repo = _repo_identity(body.get("repo_identity") or body.get("repo"))
    version = _int(body.get("version") or body.get("revision"), "version")
    digest = _text(body.get("digest"), "digest")
    status = _text(body.get("status"), "status", required=False) or "applied"
    reason = _text(body.get("reason") or body.get("detail"), "reason", required=False)
    requested_hash = _text(body.get("context_hash"), "context_hash", required=False)
    assert consumer is not None and session_id is not None and digest is not None and version is not None
    if status not in ACK_STATUSES:
        raise _error("invalid-request", "status must be applied or failed")
    if requested_hash is not None and CONTEXT_HASH_PATTERN.fullmatch(requested_hash) is None:
        raise _error("invalid-request", "policy field 'context_hash' must be a sha256:64hex digest")
    pending = fleet_hub_policy.find_pending_policy(
        conn, session_id=session_id, consumer=consumer, repo_identity=repo, node_id=caller_node
    )
    if pending is None:
        raise _error("auth-failed", "no pending policy receipt for this authenticated node", status=403)
    if pending["owner_node"] != caller_node:
        raise _error("auth-failed", "fleet policy session is owned by another node", status=403)
    if pending["consumer"] != consumer or pending["session_id"] != session_id:
        raise _error("auth-failed", "pending policy identity does not match the acknowledgement", status=403)
    if (pending["repo_identity"] or "") != (repo or ""):
        raise _error("auth-failed", "pending policy identity does not match the acknowledgement", status=403)
    if not isinstance(pending.get("effective"), dict) or not isinstance(pending.get("sources"), dict):
        raise _error("invalid-request", "pending policy snapshot is incomplete")
    prepared = pending.get("source") == "prepare"
    if prepared:
        selected = pending.get("selected")
        if not isinstance(selected, dict) or not selected.get("seat"):
            raise _error("invalid-request", "pending policy snapshot is incomplete")
    current = fleet_hub_policy.current_policy(conn)
    if pending["revision"] != version or pending["digest"] != digest:
        raise _error("revision-conflict", "acknowledgement does not match the prepared snapshot", status=409)
    if current["revision"] != version or current["digest"] != digest:
        raise _error("revision-conflict", "acknowledgement does not match the current policy snapshot", status=409)
    key = pending["pending_key"]
    if status == "failed":
        loaded = fleet_hub_policy.find_loaded_session(
            conn, session_id=session_id, consumer=consumer, repo_identity=repo, node_id=caller_node
        )
        if loaded is None:
            return _ack_payload(
                {
                    "consumer": consumer,
                    "repo_identity": repo,
                    "revision": pending["revision"],
                    "digest": pending["digest"],
                    "state": "unknown",
                    "loaded_at": None,
                    "context_hash": None,
                },
                session_id=session_id,
                applied=False,
            )
        failed = fleet_hub_policy.acknowledge_session_refresh(
            conn, loaded["session_id"], node_id=caller_node, state="failed", detail=reason
        )
        row = _session_row_with_state(conn, failed)
        if row.get("context_hash") is None:
            row["context_hash"] = loaded.get("context_hash")
        return _ack_payload(row, session_id=session_id, applied=False)
    if prepared:
        if requested_hash is None:
            raise _error("invalid-request", "policy field 'context_hash' is required")
        loaded = fleet_hub_policy.record_acknowledged_policy(
            conn, pending, node_id=caller_node, expected_context_hash=requested_hash
        )
        row = _session_row_with_state(conn, loaded)
        return _ack_payload(row, session_id=session_id, applied=True)
    recorded = fleet_hub_policy.record_session_policy(
        conn,
        session_id=key,
        consumer=consumer,
        node_id=caller_node,
        repo_identity=repo,
        revision=version,
        digest=digest,
        source="ack",
    )
    row = _session_row_with_state(conn, recorded)
    return _ack_payload(row, session_id=session_id, applied=True)


def _refresh(conn: Any, body: Mapping[str, Any], *, caller_node: str | None, is_admin: bool) -> dict[str, Any]:
    consumer = _text(body.get("consumer"), "consumer")
    session_id = _text(body.get("session_id"), "session_id")
    repo = _repo_identity(body.get("repo_identity") or body.get("repo"))
    assert consumer is not None and session_id is not None
    loaded = fleet_hub_policy.find_loaded_session(
        conn, session_id=session_id, consumer=consumer, repo_identity=repo, node_id=None if is_admin else caller_node
    )
    if loaded is None:
        raise _error("invalid-request", "no loaded policy receipt matches this session")
    if not is_admin and loaded["owner_node"] != caller_node:
        raise _error("auth-failed", "fleet policy session is owned by another node", status=403)
    refreshed = fleet_hub_policy.request_session_refresh(
        conn, loaded["session_id"], actor="admin" if is_admin else (caller_node or "node")
    )
    row = _session_row_with_state(conn, refreshed)
    payload = _ack_payload(row, session_id=session_id, applied=False)
    payload["refresh_state"] = row.get("refresh_state")
    return payload


def _ingest_quota(
    conn: Any,
    body: Mapping[str, Any],
    *,
    caller_node: str | None,
    is_admin: bool,
) -> dict[str, Any]:
    envelope: Any = {key: value for key, value in body.items() if key != "action"}
    if "schema" not in envelope and isinstance(body.get("document"), Mapping):
        envelope = body.get("document")
    elif "schema" not in envelope and "observations" in envelope:
        envelope = {
            "schema": fleet_quota.QUOTA_SCHEMA,
            "collected_at": envelope.get("collected_at"),
            "observations": envelope.get("observations"),
        }
    try:
        parsed = fleet_quota.parse_observations(envelope)
    except fleet_quota.FleetQuotaError as exc:
        raise _error("invalid-request", str(exc)) from exc
    current = fleet_hub_policy.current_policy(conn)
    pools = (current["document"].get("routing") or {}).get("quota_pools") or {}
    allowed: list[dict[str, Any]] = []
    for observation in parsed["observations"]:
        pool = pools.get(observation["pool_id"])
        if not isinstance(pool, Mapping):
            raise _error("invalid-request", f"quota pool {observation['pool_id']!r} is not configured")
        if observation["account_id"] != pool.get("account_id") or observation["provider"] != pool.get("provider"):
            raise _error("auth-failed", "quota observation does not match the configured account", status=403)
        if observation["source"] == "operator":
            if not is_admin:
                raise _error("auth-failed", "only the admin token may write an operator correction", status=403)
        else:
            collector = pool.get("collector_node")
            if caller_node is None or collector != caller_node:
                raise _error(
                    "auth-failed", "quota probe writes are limited to the configured collector node", status=403
                )
        allowed.append(observation)
    try:
        return fleet_quota.ingest_observations(
            conn, {"schema": fleet_quota.QUOTA_SCHEMA, "collected_at": parsed["collected_at"], "observations": allowed}
        )
    except fleet_quota.FleetQuotaError as exc:
        raise _error("invalid-request", str(exc)) from exc


def _inventory_envelope(body: Mapping[str, Any]) -> dict[str, Any]:
    envelope: dict[str, Any] = {key: value for key, value in body.items() if key != "action"}
    if "schema" not in envelope and isinstance(body.get("document"), Mapping):
        return dict(body["document"])
    return envelope


def _match_inventory_collectors(
    collectors: Mapping[str, Any],
    *,
    provider: str,
    harness: str,
    account_id: str,
    node_id: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for record in collectors.values():
        if not isinstance(record, Mapping):
            continue
        if (
            record.get("provider") != provider
            or str(record.get("harness") or "") != harness
            or str(record.get("account_id") or "") != account_id
        ):
            continue
        if node_id is not None and record.get("node_id") != node_id:
            continue
        if source is not None and record.get("source") != source:
            continue
        matched.append(dict(record))
    return matched


def _trusted_source_from_collectors(records: Sequence[Mapping[str, Any]]) -> str:
    sources = {str(record.get("source") or "") for record in records}
    sources.discard("")
    if len(sources) != 1:
        raise _error("invalid-request", "inventory collector mapping is ambiguous")
    return next(iter(sources))


def _ingest_inventory(
    conn: Any,
    body: Mapping[str, Any],
    *,
    caller_node: str | None,
    is_admin: bool,
) -> dict[str, Any]:
    fleet_model_inventory.ensure_schema(conn)
    envelope = _inventory_envelope(body)
    current = fleet_hub_policy.current_policy(conn)
    collectors = (current["document"].get("routing") or {}).get("inventory_collectors") or {}
    if not isinstance(collectors, Mapping) or not collectors:
        raise _error("auth-failed", "inventory collectors are not configured", status=403)
    provider = _text(envelope.get("provider"), "provider")
    assert provider is not None
    harness_raw = envelope.get("harness")
    if harness_raw is None:
        harness = ""
    elif not isinstance(harness_raw, str):
        raise _error("invalid-request", "policy field 'harness' must be a string")
    else:
        harness = harness_raw.strip()
    account_raw = envelope.get("account_id")
    if account_raw is None:
        account_id = ""
    elif not isinstance(account_raw, str):
        raise _error("invalid-request", "policy field 'account_id' must be a string")
    else:
        account_id = account_raw.strip()
    claimed_source = envelope.get("source")
    manual = claimed_source == "manual:browser" or envelope.get("evidence_type") == "manual_browser"
    payload = dict(envelope)
    payload["provider"] = provider
    payload["harness"] = harness
    payload["account_id"] = account_id
    if manual:
        if not is_admin:
            raise _error("auth-failed", "manual:browser inventory is limited to the admin token", status=403)
        operator = envelope.get("operator")
        if not isinstance(operator, str) or not operator.strip():
            raise _error("invalid-request", "manual:browser inventory requires operator provenance")
        matched = _match_inventory_collectors(
            collectors,
            provider=provider,
            harness=harness,
            account_id=account_id,
            source="manual:browser",
        )
        if not matched:
            raise _error("auth-failed", "inventory observation does not match the configured collector", status=403)
        trusted_source = "manual:browser"
        try:
            validated = fleet_model_inventory.build_manual_browser_payload(
                provider,
                envelope.get("models") or [],
                captured_at=str(envelope.get("captured_at") or ""),
                expires_at=str(envelope.get("expires_at") or ""),
                operator=operator.strip(),
                harness=harness,
                account_id=account_id,
                scope=envelope.get("scope") or "complete",
            )
        except fleet_model_inventory.FleetModelInventoryError as exc:
            raise _error("invalid-request", scrub_text(str(exc))) from exc
        payload.update(validated)
        payload["source"] = trusted_source
    else:
        if is_admin or caller_node is None:
            raise _error(
                "auth-failed",
                "inventory probe writes are limited to the configured collector node",
                status=403,
            )
        matched = _match_inventory_collectors(
            collectors,
            provider=provider,
            harness=harness,
            account_id=account_id,
            node_id=caller_node,
        )
        if not matched:
            raise _error("auth-failed", "inventory observation does not match the configured collector", status=403)
        trusted_source = _trusted_source_from_collectors(matched)
        if trusted_source == "manual:browser":
            raise _error("auth-failed", "manual:browser inventory is limited to the admin token", status=403)
        payload["source"] = trusted_source
    try:
        return fleet_model_inventory.ingest(conn, payload, trusted_source=trusted_source)
    except fleet_model_inventory.InventorySourceRejectedError as exc:
        raise _error("auth-failed", scrub_text(str(exc)), status=403) from exc
    except fleet_model_inventory.FleetModelInventoryError as exc:
        raise _error("invalid-request", scrub_text(str(exc))) from exc


def read_inventory(conn: Any) -> dict[str, Any]:
    """Read-only inventory projection. Never probes a provider."""
    fleet_model_inventory.ensure_schema(conn)
    snap = fleet_model_inventory.snapshot(conn)
    current = fleet_hub_policy.current_policy(conn)
    bindings = fleet_policy.inventory_collector_bindings(current["document"])
    mapped = fleet_model_inventory.to_fleet_policy_inventory(conn, bindings=bindings or None)
    return {
        "schema": fleet_model_inventory.SNAPSHOT_SCHEMA,
        "generated_at": snap.get("generated_at"),
        "coverages": snap.get("coverages") or [],
        "providers": mapped,
    }


def read_status(conn: Any) -> dict[str, Any]:
    return fleet_hub_routing.control_plane_status(conn)


def read_policy(conn: Any, *, history: bool = False, limit: int = 50) -> dict[str, Any]:
    if history:
        revisions = []
        for row in fleet_hub_policy.list_revisions(conn, limit=limit):
            revisions.append(
                {
                    "revision": row["revision"],
                    "schema": row["schema"],
                    "digest": row["digest"],
                    "actor": row["actor"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                    "parent_revision": row["parent_revision"],
                }
            )
        return {"schema": POLICY_SCHEMA, "revisions": revisions}
    current = fleet_hub_policy.current_policy(conn)
    return {
        "schema": current["schema"],
        "version": current["revision"],
        "digest": current["digest"],
        "document": current["document"],
        "actor": current["actor"],
        "reason": current["reason"],
        "created_at": current["created_at"],
        "parent_revision": current["parent_revision"],
    }


def validate_delegation(
    conn: Any,
    delegation_id: str,
    caller_node: str,
    *,
    phase: str = "read",
) -> dict[str, Any]:
    """Validate a delegation and return its frozen envelope."""
    from . import fleet_policy_delegation

    return fleet_policy_delegation.validate_delegation(conn, delegation_id, caller_node, phase=phase)
