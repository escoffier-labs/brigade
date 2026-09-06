"""Fleet Hub routing authority: telemetry, selection, reservations, snapshot.

This module is the control-plane half that root can wire into HTTP later. It
does not serve routes, probe providers, or mark Worklore items running.
Policy preference still comes from ``fleet_policy.resolve_policy`` /
``admissible_seat``; this file only ranks configured machines against live
telemetry, claims, quota, and reservations.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from . import fleet_hub, fleet_hub_policy, fleet_policy, fleet_quota
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden
from .fleet_model_roster import canonical_json


ROUTE_SCHEMA = "brigade.fleet_route.v1"
RESERVATION_SCHEMA = "brigade.fleet_reservation.v1"
CONTROL_PLANE_SCHEMA = "brigade.fleet_control_plane.v1"
WORKLORE_INTEGRATION = "pending-root"
TELEMETRY_STATUSES = ("available", "busy", "draining", "unavailable", "unknown")
CREDENTIAL_STATES = ("ok", "missing", "stale", "error")
INTENT_STATES = ("pending", "accepted", "unknown", "released")
MAX_JSON = 65536
MAX_TEXT = fleet_policy.MAX_TEXT
MAX_FUTURE_SECONDS = 300
WorkItemLookup = Callable[[sqlite3.Connection, str], Mapping[str, Any] | None]

_work_item_lookup: WorkItemLookup | None = None

_TELEMETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_machine_telemetry (
    node_id TEXT NOT NULL PRIMARY KEY,
    machine TEXT,
    observed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    status TEXT NOT NULL,
    load REAL,
    running TEXT NOT NULL,
    active_claims TEXT NOT NULL,
    usable_seats TEXT NOT NULL,
    credential_state TEXT NOT NULL,
    observed_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_route_decisions (
    decision_id TEXT NOT NULL PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    admission_generation INTEGER NOT NULL DEFAULT 0,
    policy_version INTEGER NOT NULL,
    policy_digest TEXT NOT NULL,
    origin TEXT,
    origin_node TEXT NOT NULL,
    session_id TEXT NOT NULL,
    consumer TEXT,
    repo_identity TEXT,
    workload TEXT,
    work_id TEXT,
    selected_machine TEXT,
    selected_seat TEXT,
    reservation_id TEXT,
    reason TEXT,
    override_reason TEXT,
    request_document TEXT,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
_RESERVATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_reservations (
    reservation_id TEXT NOT NULL PRIMARY KEY,
    decision_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    machine TEXT NOT NULL,
    seat TEXT NOT NULL,
    node_id TEXT NOT NULL,
    origin_node TEXT NOT NULL,
    state TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_INTENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_dispatch_intents (
    decision_id TEXT NOT NULL PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    origin_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(stamp: datetime | None = None) -> str:
    return (stamp or _now()).isoformat()


def _parse_stamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """v20 -> v21: routing telemetry, decisions, reservations, and intents."""
    fleet_quota.ensure_schema(conn)
    conn.execute(_TELEMETRY_SCHEMA)
    conn.execute(_DECISIONS_SCHEMA)
    conn.execute(_RESERVATIONS_SCHEMA)
    conn.execute(_INTENTS_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_reservations_machine ON fleet_reservations (machine, state)")
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_reservations_node ON fleet_reservations (node_id, state)")


def set_work_item_lookup(callback: WorkItemLookup | None) -> None:
    """Test/root seam. ``None`` restores the default Worklore ``get_item`` lookup."""
    global _work_item_lookup
    _work_item_lookup = callback


def _default_work_lookup(conn: sqlite3.Connection, work_id: str) -> Mapping[str, Any] | None:
    try:
        from .worklore_store import WorkloreNotFound, _link_summary, _source_policies, get_item
    except Exception:
        return None
    try:
        item = dict(get_item(conn, work_id))
        try:
            item["source_policies"] = _source_policies(_link_summary(conn, work_id))
        except Exception:
            item["source_policies"] = ["stale-source"]
        return item
    except WorkloreNotFound:
        return None
    except Exception:
        return None


def _lookup_work(conn: sqlite3.Connection, work_id: str) -> Mapping[str, Any] | None:
    callback = _work_item_lookup
    if callback is None:
        return _default_work_lookup(conn, work_id)
    return callback(conn, work_id)


def evaluate_work_item(item: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read-only ready/authorized check. Never transitions Worklore state."""
    if item is None:
        return {"ok": False, "reason": "work-missing"}
    required = ("status", "burn_eligible", "execution_mode", "attempt_count")
    missing = [name for name in required if name not in item]
    if missing:
        return {"ok": False, "reason": "work-held", "detail": f"missing-{missing[0]}"}
    try:
        from .worklore_store import exclusion_bucket
    except Exception:
        return {"ok": False, "reason": "work-held", "detail": "worklore-unavailable"}
    source_policies = item.get("source_policies")
    if source_policies is None:
        if item.get("has_source_link"):
            source_policies = ["stale-source"] if not item.get("has_eligible_non_stale_source_link") else []
            blocking = item.get("blocking_policy")
            if blocking:
                source_policies = [str(blocking)]
        else:
            policy = item.get("source_policy")
            source_policies = [] if policy is None else [str(policy)]
    if not isinstance(source_policies, list):
        return {"ok": False, "reason": "work-held", "detail": "source-policy"}
    try:
        bucket = exclusion_bucket(item, source_policies, now=_now())
    except Exception:
        return {"ok": False, "reason": "work-held", "detail": "worklore-unavailable"}
    if bucket is not None:
        return {"ok": False, "reason": "work-held", "detail": bucket}
    spend_by = _parse_stamp(item.get("spend_by") if isinstance(item.get("spend_by"), str) else None)
    if spend_by is not None and spend_by <= _now():
        return {"ok": False, "reason": "work-held", "detail": "spend-by"}
    return {"ok": True, "reason": None}


def _bounded(raw: Any, field: str, limit: int = MAX_TEXT, *, required: bool = True) -> str | None:
    if raw is None:
        if required:
            raise FleetHubError(f"fleet routing field {field!r} is required")
        return None
    if not isinstance(raw, str):
        raise FleetHubError(f"fleet routing field {field!r} must be a string")
    if required and not raw:
        raise FleetHubError(f"fleet routing field {field!r} must not be empty")
    if len(raw) > limit:
        raise FleetHubError(f"fleet routing field {field!r} must be at most {limit} characters")
    fleet_hub._reject_controls(raw, field, kind="fleet routing")
    return raw


def _load_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    return json.loads(raw)


def _dump_json(value: Any) -> str:
    fleet_policy.reject_json_tree(value, "routing payload")
    rendered = canonical_json(value)
    if len(rendered.encode("ascii")) > MAX_JSON:
        raise FleetHubError("fleet routing payload exceeded the size limit")
    return rendered


def _mint(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(16)}"


def _number(raw: Any, field: str, *, optional: bool = True) -> float | None:
    if raw is None:
        if optional:
            return None
        raise FleetHubError(f"fleet routing field {field!r} is required")
    if type(raw) is bool or not isinstance(raw, (int, float)):
        raise FleetHubError(f"fleet routing field {field!r} must be a finite number")
    value = float(raw)
    if not math.isfinite(value):
        raise FleetHubError(f"fleet routing field {field!r} must be a finite number")
    return value


def _string_list(raw: Any, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FleetHubError(f"fleet routing field {field!r} must be a list")
    values: list[str] = []
    for index, item in enumerate(raw):
        text = _bounded(item, f"{field}[{index}]")
        assert text is not None
        values.append(text)
    return values


def _machine_for_node(document: Mapping[str, Any], node_id: str) -> str | None:
    matches = [name for name, record in document.get("machines", {}).items() if record.get("node_id") == node_id]
    if len(matches) != 1:
        return None
    return str(matches[0])


def _node_for_machine(document: Mapping[str, Any], machine: str) -> str | None:
    record = document.get("machines", {}).get(machine) or {}
    node_id = record.get("node_id")
    return str(node_id) if node_id else None


def observe_machine(conn: sqlite3.Connection, body: Any, caller_node: str) -> dict[str, Any]:
    """Record one machine snapshot. Older events cannot overwrite a fresher row."""
    ensure_schema(conn)
    payload = body if isinstance(body, Mapping) else None
    if payload is None:
        raise FleetHubError("fleet routing telemetry must be a JSON object")
    node_id = _bounded(payload.get("node_id"), "node_id")
    caller = _bounded(caller_node, "caller_node")
    assert node_id is not None and caller is not None
    if node_id != caller:
        raise FleetHubForbidden(f"telemetry node_id {node_id!r} does not match the caller ({caller})")
    observed_at = _bounded(payload.get("observed_at"), "observed_at")
    stamp = _parse_stamp(observed_at)
    if stamp is None:
        raise FleetHubError("fleet routing field 'observed_at' must be an ISO-8601 timestamp")
    skew = (stamp - _now()).total_seconds()
    if skew > MAX_FUTURE_SECONDS:
        raise FleetHubError("fleet routing field 'observed_at' is too far in the future")
    ttl = payload.get("ttl_seconds", fleet_policy.DEFAULT_TELEMETRY_TTL_SECONDS)
    if type(ttl) is not int or not 1 <= ttl <= fleet_policy.MAX_TIMEOUT_SECONDS:
        raise FleetHubError("fleet routing field 'ttl_seconds' must be an integer in 1..86400")
    status = _bounded(payload.get("status"), "status")
    if status not in TELEMETRY_STATUSES:
        raise FleetHubError(f"fleet routing field 'status' must be one of: {', '.join(TELEMETRY_STATUSES)}")
    if "credential_state" not in payload:
        raise FleetHubError("fleet routing field 'credential_state' is required")
    credential_state = _bounded(payload.get("credential_state"), "credential_state")
    if credential_state not in CREDENTIAL_STATES:
        raise FleetHubError(f"fleet routing field 'credential_state' must be one of: {', '.join(CREDENTIAL_STATES)}")
    load = _number(payload.get("load"), "load")
    if load is not None and not 0.0 <= load <= 1.0:
        raise FleetHubError("fleet routing field 'load' must be a finite number in 0..1")
    running_raw = payload.get("running") or {}
    if not isinstance(running_raw, Mapping):
        raise FleetHubError("fleet routing field 'running' must be a JSON object")
    running = {
        "session_ids": _string_list(running_raw.get("session_ids"), "running.session_ids"),
        "run_ids": _string_list(running_raw.get("run_ids"), "running.run_ids"),
    }
    claims = _string_list(payload.get("active_claims"), "active_claims")
    if "usable_seats" not in payload:
        raise FleetHubError("fleet routing field 'usable_seats' is required")
    usable = _string_list(payload.get("usable_seats"), "usable_seats")
    current = fleet_hub_policy.current_policy(conn)
    machine = _machine_for_node(current["document"], node_id)
    if machine is None:
        raise FleetHubError(f"telemetry node_id {node_id!r} is not a configured machine")
    observed_id = "obs-" + hashlib.sha256(f"{node_id}\0{observed_at}".encode("utf-8")).hexdigest()[:16]
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        existing = conn.execute(
            "SELECT observed_at, load, status, usable_seats, credential_state FROM fleet_machine_telemetry WHERE node_id=?",
            (node_id,),
        ).fetchone()
        ignored = False
        stored_load = load
        if existing is not None:
            previous = _parse_stamp(existing[0])
            if previous is not None and stamp < previous:
                ignored = True
                stored_load = existing[1]
            elif previous is not None and stamp == previous:
                previous_usable = _load_json(existing[3], [])
                previous_payload = (existing[2], existing[1], previous_usable, existing[4])
                incoming_payload = (status, load, usable, credential_state)
                if previous_payload != incoming_payload:
                    raise FleetHubConflict("telemetry-conflict: same-instant snapshots for this node disagree")
        if not ignored:
            conn.execute(
                "INSERT INTO fleet_machine_telemetry (node_id, machine, observed_at, ttl_seconds, status, load, "
                "running, active_claims, usable_seats, credential_state, observed_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET machine=excluded.machine, observed_at=excluded.observed_at, "
                "ttl_seconds=excluded.ttl_seconds, status=excluded.status, load=excluded.load, "
                "running=excluded.running, active_claims=excluded.active_claims, "
                "usable_seats=excluded.usable_seats, credential_state=excluded.credential_state, "
                "observed_id=excluded.observed_id, updated_at=excluded.updated_at",
                (
                    node_id,
                    machine,
                    observed_at,
                    ttl,
                    status,
                    load,
                    _dump_json(running),
                    _dump_json(claims),
                    _dump_json(usable),
                    credential_state,
                    observed_id,
                    _iso(),
                ),
            )
        if opened:
            conn.commit()
        return {
            "node_id": node_id,
            "machine": machine,
            "observed_at": existing[0] if ignored and existing is not None else observed_at,
            "status": status if not ignored else None,
            "load": stored_load,
            "observed_id": observed_id,
            "ignored": ignored,
        }
    except BaseException:
        if opened:
            conn.rollback()
        raise


def _telemetry(conn: sqlite3.Connection, node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    row = conn.execute(
        "SELECT node_id, machine, observed_at, ttl_seconds, status, load, running, active_claims, "
        "usable_seats, credential_state, observed_id FROM fleet_machine_telemetry WHERE node_id=?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "node_id": row[0],
        "machine": row[1],
        "observed_at": row[2],
        "ttl_seconds": int(row[3]),
        "status": row[4],
        "load": row[5],
        "running": _load_json(row[6], {"session_ids": [], "run_ids": []}),
        "active_claims": _load_json(row[7], []),
        "usable_seats": _load_json(row[8], []),
        "credential_state": row[9],
        "observed_id": row[10],
    }


def _reservation_occupies(row: Mapping[str, Any], *, now: datetime) -> bool:
    if row["state"] == "released":
        return False
    expires = _parse_stamp(row.get("expires_at"))
    accepted = bool(row.get("accepted"))
    if accepted:
        return True
    return expires is not None and expires > now


def _reservations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT reservation_id, decision_id, session_id, machine, seat, node_id, origin_node, state, "
        "accepted, expires_at FROM fleet_reservations"
    ).fetchall()
    return [
        {
            "reservation_id": row[0],
            "decision_id": row[1],
            "session_id": row[2],
            "machine": row[3],
            "seat": row[4],
            "node_id": row[5],
            "origin_node": row[6],
            "state": row[7],
            "accepted": bool(row[8]),
            "expires_at": row[9],
        }
        for row in rows
    ]


def _occupancy(
    conn: sqlite3.Connection,
    *,
    machine: str,
    node_id: str | None,
    seat: str | None,
    now: datetime,
    telemetry: Mapping[str, Any] | None,
    exclude_reservation_id: str | None = None,
) -> tuple[int, set[str]]:
    identities: set[str] = set()
    for row in _reservations(conn):
        if exclude_reservation_id and row["reservation_id"] == exclude_reservation_id:
            continue
        if not _reservation_occupies(row, now=now):
            continue
        if row["machine"] != machine:
            continue
        session = row.get("session_id")
        if session:
            identities.add(f"session:{session}")
        else:
            identities.add(f"res:{row['reservation_id']}")
    if node_id:
        for claim in fleet_hub.list_claims(conn):
            if claim.get("expired"):
                continue
            if claim.get("owner_node") != node_id:
                continue
            session = claim.get("session")
            if session and f"session:{session}" in identities:
                continue
            if session:
                identities.add(f"session:{session}")
            else:
                identities.add(f"claim:{claim['target']}")
    if telemetry:
        for session in telemetry.get("running", {}).get("session_ids") or []:
            if session:
                identities.add(f"session:{session}")
        for run in telemetry.get("running", {}).get("run_ids") or []:
            if run and f"session:{run}" not in identities:
                identities.add(f"run:{run}")
    return len(identities), identities


def _chain(start: str | None, records: Mapping[str, Any], field: str) -> list[str]:
    ordered: list[str] = []
    if not start:
        return ordered
    pending = [start]
    seen: set[str] = set()
    while pending:
        name = pending.pop(0)
        if not name or name in seen or name not in records:
            continue
        seen.add(name)
        ordered.append(name)
        pending.extend(list(records[name].get(field) or []))
    return ordered


def _request_digest(request: Mapping[str, Any]) -> str:
    material = {
        "consumer": request.get("consumer"),
        "repo_identity": request.get("repo_identity"),
        "session_id": request.get("session_id"),
        "origin": request.get("origin"),
        "workload": request.get("workload"),
        "machine": request.get("machine"),
        "seat": request.get("seat"),
        "override_reason": request.get("override_reason"),
        "work_id": request.get("work_id"),
    }
    return "sha256:" + hashlib.sha256(canonical_json(material).encode("ascii")).hexdigest()


def _parse_route_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise FleetHubError("fleet routing request must be a JSON object")
    allowed = {
        "consumer",
        "repo_identity",
        "session_id",
        "origin",
        "workload",
        "machine",
        "seat",
        "override_reason",
        "work_id",
        "decision_id",
    }
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        raise FleetHubError(f"unknown fleet routing field(s): {', '.join(unknown)}")
    return {
        "consumer": _bounded(raw.get("consumer"), "consumer"),
        "repo_identity": _bounded(raw.get("repo_identity"), "repo_identity", required=False),
        "session_id": _bounded(raw.get("session_id"), "session_id"),
        "origin": _bounded(raw.get("origin"), "origin", required=False),
        "workload": _bounded(raw.get("workload"), "workload"),
        "machine": _bounded(raw.get("machine"), "machine", required=False),
        "seat": _bounded(raw.get("seat"), "seat", required=False),
        "override_reason": _bounded(raw.get("override_reason"), "override_reason", required=False, limit=512),
        "work_id": _bounded(raw.get("work_id"), "work_id", required=False),
        "decision_id": _bounded(raw.get("decision_id"), "decision_id", required=False),
    }


def _candidate(
    *,
    machine: str,
    seat: str,
    reasons: list[str],
    observed_id: str | None,
) -> dict[str, Any]:
    unique: list[str] = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)
    return {
        "machine": machine,
        "seat": seat,
        "eligible": not unique,
        "reasons": unique,
        "observed_id": observed_id,
    }


def _seat_occupancy(
    conn: sqlite3.Connection,
    *,
    document: Mapping[str, Any],
    seat_name: str,
    now: datetime,
    exclude_reservation_id: str | None,
) -> int:
    """Combined seat occupancy: active reservations plus unexpired legacy model leases.

    Counts through the shared ``fleet_hub_capacity`` helper so routing and the
    legacy lease gate never disagree. A caller revalidating its own reservation
    is excluded only through that reservation's exact execution identity.
    """
    from . import fleet_hub_capacity

    exclude: dict[str, str] | None = None
    if exclude_reservation_id:
        row = conn.execute(
            "SELECT decision_id, session_id, node_id FROM fleet_reservations WHERE reservation_id=?",
            (exclude_reservation_id,),
        ).fetchone()
        if row is not None and all(isinstance(value, str) and value for value in row):
            exclude = {"decision_id": row[0], "session_id": row[1], "node_id": row[2]}
    try:
        usage = fleet_hub_capacity.capacity_usage(
            conn, document=document, seat=seat_name, now=now, exclude_execution=exclude
        )
    except FleetHubError:
        if exclude is None:
            raise
        usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=seat_name, now=now)
    return int(usage["used"]["seat"])


def _evaluate_pair(
    conn: sqlite3.Connection,
    *,
    document: Mapping[str, Any],
    routing: Mapping[str, Any],
    machine_name: str,
    seat_name: str,
    workload: str,
    requirements: Mapping[str, Any],
    resolution: Mapping[str, Any],
    now: datetime,
    exclude_reservation_id: str | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    machine = document["machines"].get(machine_name)
    seat = document["seats"].get(seat_name)
    if machine is None:
        return _candidate(machine=machine_name, seat=seat_name, reasons=["machine-unmapped-node"], observed_id=None)
    if seat is None:
        return _candidate(machine=machine_name, seat=seat_name, reasons=["unknown-seat"], observed_id=None)
    node_id = machine.get("node_id")
    telemetry = _telemetry(conn, node_id)
    observed_id = None if telemetry is None else str(telemetry["observed_id"])
    if not node_id:
        reasons.append("machine-unmapped-node")
    admission = fleet_policy.admissible_seat(document, seat_name, resolution)
    reasons.extend(admission["reasons"])
    if not machine.get("enabled", True):
        reasons.append("machine-disabled")
    if machine.get("draining"):
        reasons.append("machine-draining")
    required_os = requirements.get("os")
    if required_os and required_os != "unknown" and machine.get("os") != required_os:
        reasons.append("machine-wrong-os")
    missing = [
        item for item in requirements.get("capabilities") or [] if item not in (machine.get("capabilities") or [])
    ]
    if missing:
        reasons.append("machine-missing-capability")
    if workload in (machine.get("prohibited_workloads") or []):
        reasons.append("machine-prohibited-workload")
    eligible_machines = seat.get("eligible_machines") or []
    if eligible_machines and machine_name not in eligible_machines:
        reasons.append("machine-not-eligible")
    ttl = int(routing.get("telemetry_ttl_seconds") or fleet_policy.DEFAULT_TELEMETRY_TTL_SECONDS)
    if telemetry is None:
        reasons.append("machine-unknown-telemetry")
    else:
        observed_at = _parse_stamp(telemetry.get("observed_at"))
        report_ttl = int(telemetry.get("ttl_seconds") or ttl)
        effective_ttl = min(ttl, report_ttl)
        stale = observed_at is None or (now - observed_at).total_seconds() >= effective_ttl
        if stale:
            reasons.append("machine-stale-telemetry")
        if telemetry.get("status") == "unknown":
            reasons.append("machine-unknown-telemetry")
        elif telemetry.get("status") in {"unavailable", "draining"}:
            reasons.append(f"machine-{telemetry['status']}")
        elif telemetry.get("status") == "busy":
            reasons.append("machine-busy")
        credential_state = telemetry.get("credential_state")
        if credential_state is None:
            reasons.append("machine-unknown-auth")
        elif credential_state != "ok":
            reasons.append("machine-stale-auth")
        if "usable_seats" not in telemetry or telemetry.get("usable_seats") is None:
            reasons.append("machine-unknown-inventory")
        else:
            usable = list(telemetry.get("usable_seats") or [])
            if seat_name not in usable:
                reasons.append("machine-seat-unusable")
    occupancy, tokens = _occupancy(
        conn,
        machine=machine_name,
        node_id=node_id,
        seat=seat_name,
        now=now,
        telemetry=telemetry,
        exclude_reservation_id=exclude_reservation_id,
    )
    capacity = int(machine.get("concurrency") or 0)
    seat_capacity = int(seat.get("concurrency") or 0)
    seat_used = _seat_occupancy(
        conn,
        document=document,
        seat_name=seat_name,
        now=now,
        exclude_reservation_id=exclude_reservation_id,
    )
    if capacity <= 0:
        reasons.append("machine-disabled")
        reasons.append("machine-capacity")
    elif occupancy >= capacity:
        if any(token.startswith("claim:") for token in tokens):
            reasons.append("machine-claim-collision")
        reasons.append("machine-capacity")
    if seat_capacity <= 0:
        reasons.append("machine-capacity")
    elif seat_used >= seat_capacity:
        reasons.append("machine-capacity")
    pool_id = seat.get("quota_pool")
    if pool_id:
        quota = fleet_quota.admit_pool(conn, pool_id, routing=routing, now=now)
        if not quota["admitted"]:
            if "quota-exhausted" in quota["reasons"]:
                reasons.append("seat-quota-exhausted")
            else:
                reasons.append("seat-quota-unknown")
    return _candidate(machine=machine_name, seat=seat_name, reasons=reasons, observed_id=observed_id)


def _rank(document: Mapping[str, Any], machine_name: str, workload: str) -> tuple[int, int, int]:
    machine = document["machines"][machine_name]
    preferred = 0 if workload in (machine.get("preferred_workloads") or []) else 1
    discouraged = 1 if workload in (machine.get("discouraged_workloads") or []) else 0
    priority = -int(machine.get("priority") or 0)
    return (preferred, discouraged, priority)


def _request_key(
    *,
    caller_node: str,
    consumer: str | None,
    repo_identity: str | None,
    session_id: str,
    generation: int,
) -> str:
    return "\0".join((caller_node, consumer or "", repo_identity or "", session_id, str(generation)))


def _store_decision(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, Any],
    caller_node: str,
    current: Mapping[str, Any],
    selected: Mapping[str, Any] | None,
    reason: str,
    candidates: list[dict[str, Any]],
    override_reason: str | None,
    reservation_id: str | None,
    expires_at: str | None,
    decision_id: str,
    generation: int = 0,
) -> dict[str, Any]:
    payload = {
        "schema": ROUTE_SCHEMA,
        "decision_id": decision_id,
        "policy_version": current["revision"],
        "policy_digest": current["digest"],
        "selected": selected,
        "reason": reason,
        "candidates": candidates,
        "reservation_id": reservation_id,
        "expires_at": expires_at,
        "override_reason": override_reason,
        "created_at": _iso(),
        "work_id": request.get("work_id"),
        "admission_generation": generation,
    }
    conn.execute(
        "INSERT INTO fleet_route_decisions (decision_id, request_key, request_digest, admission_generation, "
        "policy_version, policy_digest, origin, origin_node, session_id, consumer, repo_identity, workload, "
        "work_id, selected_machine, selected_seat, reservation_id, reason, override_reason, request_document, "
        "document, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            _request_key(
                caller_node=caller_node,
                consumer=request.get("consumer"),
                repo_identity=request.get("repo_identity"),
                session_id=request["session_id"],
                generation=generation,
            ),
            _request_digest(request),
            generation,
            current["revision"],
            current["digest"],
            request.get("origin"),
            caller_node,
            request["session_id"],
            request.get("consumer"),
            request.get("repo_identity"),
            request.get("workload"),
            request.get("work_id"),
            None if selected is None else selected["machine"],
            None if selected is None else selected["seat"],
            reservation_id,
            reason,
            override_reason,
            _dump_json(dict(request)),
            _dump_json(payload),
            payload["created_at"],
        ),
    )
    return payload


_DECISION_SELECT = (
    "document, origin_node, request_digest, reservation_id, selected_machine, selected_seat, "
    "policy_version, policy_digest, admission_generation, session_id, consumer, repo_identity, "
    "origin, request_document, work_id, override_reason"
)


def _hydrate_decision(row: tuple[Any, ...]) -> dict[str, Any]:
    payload = json.loads(row[0])
    payload["_origin_node"] = row[1]
    payload["_request_digest"] = row[2]
    payload["_reservation_id"] = row[3]
    payload["_selected_machine"] = row[4]
    payload["_selected_seat"] = row[5]
    payload["_policy_version"] = row[6]
    payload["_policy_digest"] = row[7]
    payload["_admission_generation"] = int(row[8] or 0)
    payload["_session_id"] = row[9]
    payload["_consumer"] = row[10]
    payload["_repo_identity"] = row[11]
    payload["_origin"] = row[12]
    payload["_request_document"] = _load_json(row[13], {})
    payload["_work_id"] = row[14]
    payload["_override_reason"] = row[15]
    return payload


def _decision_row(
    conn: sqlite3.Connection, *, decision_id: str | None = None, request_key: str | None = None
) -> dict[str, Any] | None:
    if decision_id:
        row = conn.execute(
            f"SELECT {_DECISION_SELECT} FROM fleet_route_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {_DECISION_SELECT} FROM fleet_route_decisions WHERE request_key=?",
            (request_key,),
        ).fetchone()
    if row is None:
        return None
    return _hydrate_decision(row)


def _latest_decision(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    consumer: str | None,
    repo_identity: str | None,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_DECISION_SELECT} FROM fleet_route_decisions WHERE session_id=? AND IFNULL(consumer,'')=? "
        "AND IFNULL(repo_identity,'')=? ORDER BY admission_generation DESC, created_at DESC LIMIT 1",
        (session_id, consumer or "", repo_identity or ""),
    ).fetchone()
    if row is None:
        return None
    return _hydrate_decision(row)


def _public(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if not str(key).startswith("_")}


def public_route(payload: Mapping[str, Any]) -> dict[str, Any]:
    """T3 public route envelope. Rich reasons stay on the stored/status document."""
    candidates = []
    for row in payload.get("candidates") or []:
        reasons = [str(item) for item in (row.get("reasons") or []) if item]
        reason = None if not reasons else ", ".join(reasons)
        if "reason" in row and not isinstance(row.get("reasons"), list):
            reason = row.get("reason")
        candidates.append(
            {
                "machine": row.get("machine"),
                "seat": row.get("seat"),
                "eligible": bool(row.get("eligible")),
                "reason": reason,
            }
        )
    return {
        "schema": ROUTE_SCHEMA,
        "decision_id": payload.get("decision_id"),
        "policy_version": payload.get("policy_version"),
        "policy_digest": payload.get("policy_digest"),
        "selected": payload.get("selected"),
        "reason": payload.get("reason"),
        "candidates": candidates,
        "reservation_id": payload.get("reservation_id"),
        "expires_at": payload.get("expires_at"),
    }


def decision_origin_context(conn: sqlite3.Connection, decision_id: str, caller_node: str) -> dict[str, Any]:
    """Return stored origin and exact overrides after the selected target authenticates."""
    ensure_schema(conn)
    stored = _decision_row(conn, decision_id=decision_id)
    if stored is None:
        raise FleetHubError(f"fleet routing decision {decision_id!r} does not exist")
    current = fleet_hub_policy.current_policy(conn)
    selected_machine = stored.get("_selected_machine")
    node_id = _node_for_machine(current["document"], selected_machine) if selected_machine else None
    if node_id != caller_node:
        raise FleetHubForbidden("only the selected target node may recover this decision origin")
    request = dict(stored.get("_request_document") or {})
    return {
        "origin": stored.get("_origin") or request.get("origin"),
        "consumer": stored.get("_consumer") or request.get("consumer"),
        "repo_identity": stored.get("_repo_identity") or request.get("repo_identity"),
        "session_id": stored.get("_session_id") or request.get("session_id"),
        "overrides": request.get("overrides") or {},
        "override_reason": stored.get("_override_reason") or request.get("override_reason"),
        "machine": request.get("machine") or stored.get("_selected_machine"),
        "seat": request.get("seat") or stored.get("_selected_seat"),
        "workload": request.get("workload"),
        "work_id": stored.get("_work_id") or request.get("work_id"),
        "decision_id": stored.get("decision_id"),
    }


def _authenticate_origin(document: Mapping[str, Any], origin: str | None, caller_node: str) -> str:
    if not origin:
        raise FleetHubError("fleet routing field 'origin' is required")
    if origin == caller_node:
        return origin
    record = document.get("machines", {}).get(origin)
    if isinstance(record, Mapping) and record.get("node_id") == caller_node:
        return origin
    raise FleetHubForbidden("origin must authenticate as the mapped caller node")


def _reservation_live(conn: sqlite3.Connection, reservation_id: str | None, *, now: datetime) -> dict[str, Any] | None:
    if not reservation_id:
        return None
    row = conn.execute(
        "SELECT state, accepted, expires_at FROM fleet_reservations WHERE reservation_id=?",
        (reservation_id,),
    ).fetchone()
    if row is None:
        return None
    return {"state": row[0], "accepted": bool(row[1]), "expires_at": row[2]}


def _revalidate(conn: sqlite3.Connection, request: Mapping[str, Any], caller_node: str) -> dict[str, Any]:
    stored = _decision_row(conn, decision_id=request["decision_id"])
    if stored is None:
        raise FleetHubError(f"fleet routing decision {request['decision_id']!r} does not exist")
    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    selected_machine = stored.get("_selected_machine")
    node_id = _node_for_machine(document, selected_machine) if selected_machine else None
    if node_id != caller_node:
        raise FleetHubForbidden("only the selected target node may revalidate this decision")
    if stored.get("_session_id") != request.get("session_id"):
        raise FleetHubForbidden("session does not match the stored decision")
    if stored.get("_consumer") != request.get("consumer"):
        raise FleetHubForbidden("consumer does not match the stored decision")
    if (stored.get("_repo_identity") or "") != (request.get("repo_identity") or ""):
        raise FleetHubForbidden("repository does not match the stored decision")
    if stored["_policy_version"] != current["revision"] or stored["_policy_digest"] != current["digest"]:
        raise FleetHubConflict("policy-mismatch: current policy does not match the stored decision")
    now = _now()
    reservation_id = stored.get("_reservation_id")
    live = _reservation_live(conn, reservation_id, now=now)
    if live is None or live["state"] == "released":
        raise FleetHubError("fleet routing reservation is not live")
    expires = _parse_stamp(live.get("expires_at"))
    if not live["accepted"] and (expires is None or expires <= now):
        raise FleetHubError("fleet routing reservation expired unclaimed and cannot launch")
    if live["accepted"] and expires is not None and expires <= now:
        raise FleetHubConflict("reservation-unknown: accepted reservation lost heartbeat")
    if stored.get("_selected_machine") and stored.get("_selected_seat"):
        requirements = ((document.get("routing") or {}).get("workload_requirements") or {}).get(
            request.get("workload")
        ) or {}
        pair = _evaluate_pair(
            conn,
            document=document,
            routing=document.get("routing") or fleet_policy.empty_routing(),
            machine_name=str(stored["_selected_machine"]),
            seat_name=str(stored["_selected_seat"]),
            workload=str(request.get("workload") or ""),
            requirements=requirements,
            resolution=fleet_policy.resolve_policy(
                document, str(request.get("consumer")), request.get("repo_identity")
            ),
            now=now,
            exclude_reservation_id=reservation_id,
        )
        if not pair["eligible"]:
            raise FleetHubConflict(f"revalidate-ineligible: {', '.join(pair['reasons'])}")
    return _public(stored)


def route(conn: sqlite3.Connection, request: Any, caller_node: str) -> dict[str, Any]:
    """Select a machine/seat under the current policy, or record a structured denial."""
    ensure_schema(conn)
    parsed = _parse_route_request(request)
    caller = _bounded(caller_node, "caller_node")
    assert caller is not None
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        current = fleet_hub_policy.current_policy(conn)
        document = current["document"]
        routing = document.get("routing") or fleet_policy.empty_routing()
        now = _now()
        if not parsed.get("decision_id"):
            _authenticate_origin(document, parsed.get("origin"), caller)
        if parsed.get("repo_identity"):
            from .fleet_session_presence import validate_repo_identity

            if validate_repo_identity(parsed["repo_identity"]) is None:
                raise FleetHubError("repo_identity must be a credential-free canonical id")
        if parsed["decision_id"]:
            payload = _revalidate(conn, parsed, caller)
            if opened:
                conn.commit()
            return payload
        existing = _latest_decision(
            conn,
            session_id=parsed["session_id"],
            consumer=parsed.get("consumer"),
            repo_identity=parsed.get("repo_identity"),
        )
        digest = _request_digest(parsed)
        generation = 0
        if existing is not None:
            if existing["_origin_node"] != caller:
                raise FleetHubForbidden("session key is owned by another node")
            if existing["_request_digest"] != digest:
                raise FleetHubConflict("request-conflict: session already has a different routing request")
            live = _reservation_live(conn, existing.get("_reservation_id"), now=now)
            if existing.get("selected") and live is not None and live["state"] != "released":
                expires = _parse_stamp(live.get("expires_at"))
                if live["accepted"] and expires is not None and expires <= now:
                    raise FleetHubConflict("reservation-unknown: accepted reservation lost heartbeat")
                if live["accepted"] or (expires is not None and expires > now):
                    if opened:
                        conn.commit()
                    return _public(existing)
            generation = int(existing.get("_admission_generation") or 0) + 1
        if parsed["consumer"] not in (document.get("consumers") or {}):
            payload = _store_decision(
                conn,
                request=parsed,
                caller_node=caller,
                current=current,
                selected=None,
                reason="enrollment-required",
                candidates=[],
                override_reason=parsed["override_reason"],
                reservation_id=None,
                expires_at=None,
                decision_id=_mint("dec-"),
                generation=generation,
            )
            if opened:
                conn.commit()
            return payload
        if parsed["work_id"]:
            verdict = evaluate_work_item(_lookup_work(conn, parsed["work_id"]))
            if not verdict["ok"]:
                payload = _store_decision(
                    conn,
                    request=parsed,
                    caller_node=caller,
                    current=current,
                    selected=None,
                    reason=str(verdict["reason"]),
                    candidates=[],
                    override_reason=parsed["override_reason"],
                    reservation_id=None,
                    expires_at=None,
                    decision_id=_mint("dec-"),
                    generation=generation,
                )
                if opened:
                    conn.commit()
                return payload
        if parsed["machine"] or parsed["seat"]:
            if not parsed["override_reason"]:
                payload = _store_decision(
                    conn,
                    request=parsed,
                    caller_node=caller,
                    current=current,
                    selected=None,
                    reason="override-reason-required",
                    candidates=[],
                    override_reason=None,
                    reservation_id=None,
                    expires_at=None,
                    decision_id=_mint("dec-"),
                    generation=generation,
                )
                if opened:
                    conn.commit()
                return payload
        if not routing.get("enabled"):
            payload = _store_decision(
                conn,
                request=parsed,
                caller_node=caller,
                current=current,
                selected=None,
                reason="routing-disabled",
                candidates=[],
                override_reason=parsed["override_reason"],
                reservation_id=None,
                expires_at=None,
                decision_id=_mint("dec-"),
                generation=generation,
            )
            if opened:
                conn.commit()
            return payload
        requirements = (routing.get("workload_requirements") or {}).get(parsed["workload"])
        if requirements is None:
            payload = _store_decision(
                conn,
                request=parsed,
                caller_node=caller,
                current=current,
                selected=None,
                reason="unknown-workload",
                candidates=[],
                override_reason=parsed["override_reason"],
                reservation_id=None,
                expires_at=None,
                decision_id=_mint("dec-"),
                generation=generation,
            )
            if opened:
                conn.commit()
            return payload
        resolution = fleet_policy.resolve_policy(
            document,
            parsed["consumer"],
            parsed["repo_identity"],
            override_reason=parsed["override_reason"],
        )
        start_seat = parsed["seat"] or (resolution.get("effective", {}).get("roles", {}) or {}).get("impl")
        seats = _chain(start_seat, document["seats"], "fallback")
        if not seats:
            payload = _store_decision(
                conn,
                request=parsed,
                caller_node=caller,
                current=current,
                selected=None,
                reason="unknown-role",
                candidates=[],
                override_reason=parsed["override_reason"],
                reservation_id=None,
                expires_at=None,
                decision_id=_mint("dec-"),
                generation=generation,
            )
            if opened:
                conn.commit()
            return payload
        candidates: list[dict[str, Any]] = []
        eligible: list[dict[str, Any]] = []
        for seat_name in seats:
            seat = document["seats"][seat_name]
            machine_names = list(seat.get("eligible_machines") or []) or list(document["machines"])
            if parsed["machine"] and parsed["machine"] not in machine_names:
                machine_names = [parsed["machine"], *machine_names]
            if parsed["machine"] and parsed["machine"] in machine_names:
                ordered = [parsed["machine"]] + [name for name in machine_names if name != parsed["machine"]]
            else:
                preferred = None
                for name in machine_names:
                    record = document["machines"].get(name) or {}
                    if parsed["workload"] in (record.get("preferred_workloads") or []):
                        preferred = name
                        break
                start_machine = preferred or (machine_names[0] if machine_names else None)
                chained = _chain(
                    start_machine,
                    {name: document["machines"][name] for name in machine_names if name in document["machines"]},
                    "fallback",
                )
                extra = [name for name in machine_names if name not in chained]
                ordered = chained + extra
            ordered = sorted(
                ordered,
                key=lambda name: (
                    _rank(document, name, parsed["workload"]) if name in document["machines"] else (9, 9, 0)
                ),
            )
            for machine_name in ordered:
                row = _evaluate_pair(
                    conn,
                    document=document,
                    routing=routing,
                    machine_name=machine_name,
                    seat_name=seat_name,
                    workload=parsed["workload"],
                    requirements=requirements,
                    resolution=resolution,
                    now=now,
                )
                candidates.append(row)
                if row["eligible"]:
                    eligible.append(row)
        selected_row = None
        if parsed["machine"] or parsed["seat"]:
            matching = [
                row
                for row in eligible
                if (not parsed["machine"] or row["machine"] == parsed["machine"])
                and (not parsed["seat"] or row["seat"] == parsed["seat"])
            ]
            if not matching:
                payload = _store_decision(
                    conn,
                    request=parsed,
                    caller_node=caller,
                    current=current,
                    selected=None,
                    reason="machine-override-ineligible",
                    candidates=candidates,
                    override_reason=parsed["override_reason"],
                    reservation_id=None,
                    expires_at=None,
                    decision_id=_mint("dec-"),
                    generation=generation,
                )
                if opened:
                    conn.commit()
                return payload
            selected_row = matching[0]
        elif eligible:
            selected_row = eligible[0]
        if selected_row is None:
            payload = _store_decision(
                conn,
                request=parsed,
                caller_node=caller,
                current=current,
                selected=None,
                reason="no-eligible-candidate",
                candidates=candidates,
                override_reason=parsed["override_reason"],
                reservation_id=None,
                expires_at=None,
                decision_id=_mint("dec-"),
                generation=generation,
            )
            if opened:
                conn.commit()
            return payload
        decision_id = _mint("dec-")
        reservation_id = _mint("rsv-")
        expires_at = _iso(now + timedelta(seconds=int(routing["reservation_ttl_seconds"])))
        node_id = _node_for_machine(document, selected_row["machine"])
        assert node_id is not None
        conn.execute(
            "INSERT INTO fleet_reservations (reservation_id, decision_id, session_id, machine, seat, node_id, "
            "origin_node, state, accepted, expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)",
            (
                reservation_id,
                decision_id,
                parsed["session_id"],
                selected_row["machine"],
                selected_row["seat"],
                node_id,
                caller,
                expires_at,
                _iso(now),
                _iso(now),
            ),
        )
        conn.execute(
            "INSERT INTO fleet_dispatch_intents (decision_id, reservation_id, session_id, origin_node, "
            "target_node, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                decision_id,
                reservation_id,
                parsed["session_id"],
                caller,
                node_id,
                _iso(now),
                _iso(now),
            ),
        )
        payload = _store_decision(
            conn,
            request=parsed,
            caller_node=caller,
            current=current,
            selected={"machine": selected_row["machine"], "seat": selected_row["seat"]},
            reason="selected",
            candidates=candidates,
            override_reason=parsed["override_reason"],
            reservation_id=reservation_id,
            expires_at=expires_at,
            decision_id=decision_id,
            generation=generation,
        )
        if opened:
            conn.commit()
        return payload
    except BaseException:
        if opened:
            conn.rollback()
        raise


def _reservation_request(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise FleetHubError("fleet reservation request must be a JSON object")
    reservation_id = _bounded(raw.get("reservation_id"), "reservation_id")
    decision_id = _bounded(raw.get("decision_id"), "decision_id")
    session_id = _bounded(raw.get("session_id"), "session_id")
    assert reservation_id is not None and decision_id is not None and session_id is not None
    return {"reservation_id": reservation_id, "decision_id": decision_id, "session_id": session_id}


def _load_reservation(conn: sqlite3.Connection, request: Mapping[str, str]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT reservation_id, decision_id, session_id, machine, seat, node_id, origin_node, state, "
        "accepted, expires_at FROM fleet_reservations WHERE reservation_id=?",
        (request["reservation_id"],),
    ).fetchone()
    if row is None:
        raise FleetHubError(f"fleet reservation {request['reservation_id']!r} does not exist")
    payload = {
        "reservation_id": row[0],
        "decision_id": row[1],
        "session_id": row[2],
        "machine": row[3],
        "seat": row[4],
        "node_id": row[5],
        "origin_node": row[6],
        "state": row[7],
        "accepted": bool(row[8]),
        "expires_at": row[9],
    }
    if payload["decision_id"] != request["decision_id"] or payload["session_id"] != request["session_id"]:
        raise FleetHubError("fleet reservation does not match decision_id/session_id")
    return payload


def _reservation_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": RESERVATION_SCHEMA,
        "reservation_id": row["reservation_id"],
        "decision_id": row["decision_id"],
        "state": row["state"],
        "expires_at": row["expires_at"],
    }


def claim_dispatch_intent(conn: sqlite3.Connection, request: Any, caller_node: str) -> dict[str, Any]:
    """Target node accepts a pending intent. Idempotent for the selected node."""
    ensure_schema(conn)
    parsed = _reservation_request(request)
    caller = _bounded(caller_node, "caller_node")
    assert caller is not None
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        row = _load_reservation(conn, parsed)
        if row["node_id"] != caller:
            raise FleetHubForbidden("only the selected target node may claim this reservation")
        if row["state"] == "released":
            raise FleetHubError("fleet reservation is already released")
        now = _iso()
        conn.execute(
            "UPDATE fleet_reservations SET accepted=1, updated_at=? WHERE reservation_id=?",
            (now, row["reservation_id"]),
        )
        conn.execute(
            "UPDATE fleet_dispatch_intents SET state='accepted', updated_at=? WHERE decision_id=?",
            (now, row["decision_id"]),
        )
        row["accepted"] = True
        if opened:
            conn.commit()
        return {
            "schema": RESERVATION_SCHEMA,
            "reservation_id": row["reservation_id"],
            "decision_id": row["decision_id"],
            "state": "accepted",
        }
    except BaseException:
        if opened:
            conn.rollback()
        raise


def cancel_unclaimed_intent(conn: sqlite3.Connection, request: Any, caller_node: str) -> dict[str, Any]:
    """Originating controller may cancel only a still-unclaimed intent."""
    ensure_schema(conn)
    parsed = _reservation_request(request)
    caller = _bounded(caller_node, "caller_node")
    assert caller is not None
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        row = _load_reservation(conn, parsed)
        if row["origin_node"] != caller:
            raise FleetHubForbidden("only the originating controller may cancel an unclaimed intent")
        if row["accepted"]:
            raise FleetHubForbidden("an accepted reservation cannot be cancelled by the origin")
        now = _iso()
        conn.execute(
            "UPDATE fleet_reservations SET state='released', updated_at=? WHERE reservation_id=?",
            (now, row["reservation_id"]),
        )
        conn.execute(
            "UPDATE fleet_dispatch_intents SET state='released', updated_at=? WHERE decision_id=?",
            (now, row["decision_id"]),
        )
        row["state"] = "released"
        if opened:
            conn.commit()
        return _reservation_payload(row)
    except BaseException:
        if opened:
            conn.rollback()
        raise


def renew_reservation(conn: sqlite3.Connection, request: Any, caller_node: str) -> dict[str, Any]:
    """Target node heartbeat. Expired reservations cannot be resurrected."""
    ensure_schema(conn)
    parsed = _reservation_request(request)
    caller = _bounded(caller_node, "caller_node")
    assert caller is not None
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        row = _load_reservation(conn, parsed)
        if row["node_id"] != caller:
            raise FleetHubForbidden("only the selected target node may renew this reservation")
        if not row["accepted"]:
            raise FleetHubForbidden("only an accepted target reservation may be renewed")
        if row["state"] == "released":
            raise FleetHubError("fleet reservation is already released")
        expires = _parse_stamp(row["expires_at"])
        if expires is None or expires <= _now():
            raise FleetHubError("fleet reservation has expired and cannot be resurrected")
        current = fleet_hub_policy.current_policy(conn)
        ttl = int(current["document"]["routing"]["reservation_ttl_seconds"])
        new_expiry = _iso(_now() + timedelta(seconds=ttl))
        conn.execute(
            "UPDATE fleet_reservations SET expires_at=?, updated_at=? WHERE reservation_id=?",
            (new_expiry, _iso(), row["reservation_id"]),
        )
        row["expires_at"] = new_expiry
        if opened:
            conn.commit()
        return _reservation_payload(row)
    except BaseException:
        if opened:
            conn.rollback()
        raise


def release_reservation(conn: sqlite3.Connection, request: Any, caller_node: str) -> dict[str, Any]:
    """Explicit terminal acknowledgement from the target. Idempotent."""
    ensure_schema(conn)
    parsed = _reservation_request(request)
    caller = _bounded(caller_node, "caller_node")
    assert caller is not None
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        row = _load_reservation(conn, parsed)
        if row["node_id"] != caller:
            raise FleetHubForbidden("only the selected target node may release this reservation")
        if not row["accepted"] and row["state"] != "released":
            raise FleetHubForbidden("only an accepted target reservation may be released")
        now = _iso()
        conn.execute(
            "UPDATE fleet_reservations SET state='released', updated_at=? WHERE reservation_id=?",
            (now, row["reservation_id"]),
        )
        conn.execute(
            "UPDATE fleet_dispatch_intents SET state='released', updated_at=? WHERE decision_id=?",
            (now, row["decision_id"]),
        )
        row["state"] = "released"
        if opened:
            conn.commit()
        return _reservation_payload(row)
    except BaseException:
        if opened:
            conn.rollback()
        raise


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Safe UI contract. Missing counts stay unknown, never invented as zero."""
    ensure_schema(conn)
    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    now = _now()
    machines: list[dict[str, Any]] = []
    for name, record in document.get("machines", {}).items():
        node_id = record.get("node_id")
        telemetry = _telemetry(conn, node_id)
        occupancy, _tokens = _occupancy(conn, machine=name, node_id=node_id, seat=None, now=now, telemetry=telemetry)
        capacity = int(record.get("concurrency") or 0)
        state = "unknown"
        last_observed_at = None
        load: float | None = None
        unused_reason = None
        available_slots: int | None = None
        if telemetry is None:
            unused_reason = "unknown-telemetry"
        else:
            last_observed_at = telemetry.get("observed_at")
            load = telemetry.get("load")
            ttl = int(document.get("routing", {}).get("telemetry_ttl_seconds") or 300)
            observed_at = _parse_stamp(last_observed_at)
            stale = observed_at is None or (now - observed_at).total_seconds() > ttl
            if stale:
                state = "unknown"
                unused_reason = "stale-telemetry"
            else:
                state = str(telemetry.get("status") or "unknown")
                if state == "unknown":
                    unused_reason = "unknown-telemetry"
                    available_slots = None
                else:
                    available_slots = max(capacity - occupancy, 0)
                    if available_slots > 0 and state == "available":
                        unused_reason = "idle-capacity"
        active_claims = occupancy
        machines.append(
            {
                "id": name,
                "os": record.get("os"),
                "state": state,
                "last_observed_at": last_observed_at,
                "load": load,
                "capacity": capacity,
                "active_claims": active_claims,
                "available_slots": available_slots,
                "eligible_queued_work": None,
                "unused_reason": unused_reason,
            }
        )
    routes = []
    for row in conn.execute(
        "SELECT document, created_at, policy_version, origin, override_reason, reservation_id, work_id FROM "
        "fleet_route_decisions ORDER BY created_at LIMIT 256"
    ).fetchall():
        payload = json.loads(row[0])
        reservation_state = None
        if row[5]:
            stored = conn.execute(
                "SELECT state, accepted, expires_at FROM fleet_reservations WHERE reservation_id=?", (row[5],)
            ).fetchone()
            if stored is not None:
                reservation_state = stored[0]
                if stored[1] and stored[0] != "released":
                    expires = _parse_stamp(stored[2])
                    if expires is not None and expires <= now:
                        reservation_state = "unknown"
        routes.append(
            {
                "decision_id": payload["decision_id"],
                "policy_version": row[2],
                "origin": row[3],
                "selected": payload.get("selected"),
                "reason": payload.get("reason"),
                "candidates": payload.get("candidates") or [],
                "created_at": row[1],
                "override_reason": row[4],
                "state": reservation_state,
                "work_id": row[6] or payload.get("work_id"),
            }
        )
    blocked = [
        {"decision_id": item["decision_id"], "reason": item["reason"]} for item in routes if item["selected"] is None
    ]
    queue = _queue_projection(conn)
    queue["route_denials"] = blocked
    try:
        sessions = fleet_hub_policy.list_session_states(conn)
    except Exception:
        sessions = []
    quota_effective = []
    routing = document.get("routing") or {}
    for pool_id in routing.get("quota_pools") or {}:
        quota_effective.append(fleet_quota.admit_pool(conn, pool_id, routing=routing, now=now))
    return {
        "policy": {
            "version": current["revision"],
            "digest": current["digest"],
            "updated_at": current.get("created_at"),
        },
        "sessions": sessions,
        "machines": machines,
        "quota": fleet_quota.list_observations(conn),
        "quota_effective": quota_effective,
        "routes": routes,
        "queue": queue,
        "worklore_integration": WORKLORE_INTEGRATION,
    }


def _queue_projection(conn: sqlite3.Connection) -> dict[str, Any]:
    """Honest Worklore-backed queue counts. Incomplete scans stay unknown."""
    try:
        from .worklore_store import burn_queue
    except Exception:
        return {
            "eligible_count": None,
            "queued_count": None,
            "blocked": [],
            "complete": False,
            "pending_autotick": [],
        }
    try:
        page = burn_queue(conn, limit=50)
    except Exception:
        return {
            "eligible_count": None,
            "queued_count": None,
            "blocked": [],
            "complete": False,
            "pending_autotick": [],
        }
    eligible = page.get("items") or []
    exclusions = page.get("exclusions") or {}
    try:
        known_items = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()
        has_work = bool(known_items and int(known_items[0]) > 0)
    except Exception:
        has_work = False
    complete = bool(has_work) and page.get("next_cursor") is None
    pending = [
        {"decision_id": row[0], "generation": int(row[1] or 0), "reason": row[2]}
        for row in conn.execute(
            "SELECT decision_id, admission_generation, reason FROM fleet_route_decisions "
            "WHERE selected_machine IS NULL ORDER BY created_at DESC LIMIT 32"
        ).fetchall()
    ]
    return {
        "eligible_count": len(eligible) if complete else None,
        "queued_count": None if not complete else len(eligible),
        "blocked": [{"reason": name, "count": count} for name, count in exclusions.items() if count],
        "complete": complete,
        "pending_autotick": pending,
    }


def control_plane_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Authenticated GET /policy/status projection."""
    from . import fleet_policy_migration

    snap = snapshot(conn)
    migration = fleet_policy_migration.migration_status(conn)
    activated = bool(migration.get("activated"))
    staged = bool((migration.get("schema") or {}).get("present"))
    return {
        "schema": CONTROL_PLANE_SCHEMA,
        "authority": {
            "active": activated,
            "status": "active" if activated else ("staged" if staged else "inactive"),
        },
        "policy": snap["policy"],
        "sessions": snap["sessions"],
        "machines": snap["machines"],
        "quota": snap["quota"],
        "quota_effective": snap.get("quota_effective") or [],
        "routes": snap["routes"],
        "queue": snap["queue"],
    }
