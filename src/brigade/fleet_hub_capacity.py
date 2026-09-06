"""Shared Fleet seat occupancy accounting for reservations and legacy model leases."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from .fleet_hub import FleetHubError

_REASON_LEGACY_UNLINKED = "legacy-unlinked"
_REASON_LEGACY_IDENTITY_UNKNOWN = "legacy-identity-unknown"
_REASON_LEGACY_IDENTITY_DRIFT = "legacy-identity-drift"
_REASON_SCOPE_UNCONFIGURED = "scope-unconfigured"
_MAX_REASONS = 16
_EXECUTION_KEYS = ("decision_id", "session_id", "node_id")


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_stamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reservation_occupies(row: Mapping[str, Any], *, now: datetime) -> bool:
    if str(row.get("state") or "") == "released":
        return False
    expires = _parse_stamp(row.get("expires_at"))
    if bool(row.get("accepted")):
        return True
    return expires is not None and expires > now


def _lease_occupies(row: Mapping[str, Any], *, now: datetime) -> bool:
    if row.get("released_at"):
        return False
    expires = row.get("expires_at")
    if isinstance(expires, (int, float)):
        return float(expires) > now.timestamp()
    parsed = _parse_stamp(expires)
    return parsed is not None and parsed > now


def _complete_execution(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise FleetHubError("capacity exclude_execution must be a mapping of decision_id, session_id, and node_id")
    missing = [key for key in _EXECUTION_KEYS if not isinstance(raw.get(key), str) or not str(raw.get(key) or "")]
    extra = sorted(str(key) for key in raw if key not in _EXECUTION_KEYS)
    if missing or extra:
        raise FleetHubError("capacity exclude_execution must be exactly decision_id, session_id, and node_id")
    return {key: str(raw[key]) for key in _EXECUTION_KEYS}


def _execution_key(decision_id: Any, session_id: Any, node_id: Any) -> tuple[str, str, str] | None:
    if not isinstance(decision_id, str) or not decision_id:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(node_id, str) or not node_id:
        return None
    return (decision_id, session_id, node_id)


def _add_reason(reasons: list[str], code: str) -> None:
    if code not in reasons and len(reasons) < _MAX_REASONS:
        reasons.append(code)


def capacity_usage(
    conn: sqlite3.Connection,
    *,
    document: Mapping[str, Any],
    seat: str,
    now: datetime,
    exclude_execution: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Count active reservations and unexpired model leases for one configured seat.

    ``exclude_execution`` drops only a matching owned reservation while a caller
    adds its own model lease. Unlinked leases stay separate occupants.
    """
    if not isinstance(seat, str) or not seat:
        raise FleetHubError("capacity seat is required")
    current = _aware(now)
    seats = document.get("seats") if isinstance(document.get("seats"), Mapping) else {}
    record = seats.get(seat) if isinstance(seats, Mapping) else None
    if not isinstance(record, Mapping):
        raise FleetHubError(f"unknown seat '{seat}'")
    exclusion = _complete_execution(exclude_execution)
    if exclusion is not None:
        owned = conn.execute(
            "SELECT reservation_id FROM fleet_reservations "
            "WHERE decision_id=? AND session_id=? AND node_id=? AND seat=?",
            (exclusion["decision_id"], exclusion["session_id"], exclusion["node_id"], seat),
        ).fetchone()
        if owned is None:
            raise FleetHubError("capacity exclude_execution does not match an owned reservation")

    reasons: list[str] = []
    occupants: list[dict[str, Any]] = []
    seen_executions: set[tuple[str, str, str]] = set()

    reservations = conn.execute(
        "SELECT reservation_id, decision_id, session_id, node_id, seat, state, accepted, expires_at "
        "FROM fleet_reservations WHERE seat=?",
        (seat,),
    ).fetchall()
    for row in reservations:
        item = {
            "kind": "reservation",
            "reservation_id": row[0],
            "decision_id": row[1],
            "session_id": row[2],
            "node_id": row[3],
            "seat": row[4],
            "state": row[5],
            "accepted": bool(row[6]),
            "expires_at": row[7],
        }
        if not _reservation_occupies(item, now=current):
            continue
        execution = _execution_key(item["decision_id"], item["session_id"], item["node_id"])
        if exclusion is not None and execution == (
            exclusion["decision_id"],
            exclusion["session_id"],
            exclusion["node_id"],
        ):
            continue
        if execution is not None:
            seen_executions.add(execution)
        occupants.append(item)

    lease_rows = conn.execute(
        "SELECT lease_id, seat, owner_node, expires_at, released_at, "
        "provider, model, launch_model, account_id, pool, decision_id, session_id, "
        "consumer, context_hash, request_id FROM model_leases WHERE seat=?",
        (seat,),
    ).fetchall()
    for row in lease_rows:
        item = {
            "kind": "lease",
            "lease_id": row[0],
            "seat": row[1],
            "node_id": row[2],
            "expires_at": row[3],
            "released_at": row[4],
            "provider": row[5],
            "model": row[6],
            "launch_model": row[7],
            "account_id": row[8],
            "pool": row[9],
            "decision_id": row[10],
            "session_id": row[11],
            "consumer": row[12],
            "context_hash": row[13],
            "request_id": row[14],
        }
        if not _lease_occupies(item, now=current):
            continue
        execution = _execution_key(item["decision_id"], item["session_id"], item["node_id"])
        linked = execution is not None and all(
            isinstance(item.get(key), str) and item.get(key)
            for key in ("provider", "model", "consumer", "context_hash")
        )
        if execution is not None and execution in seen_executions and linked:
            continue
        if execution is None or not linked:
            _add_reason(reasons, _REASON_LEGACY_UNLINKED)
            if not item.get("provider") or not item.get("model"):
                _add_reason(reasons, _REASON_LEGACY_IDENTITY_UNKNOWN)
        elif execution is not None:
            seen_executions.add(execution)
        occupants.append(item)

    provider = record.get("provider")
    pool_id = record.get("quota_pool")
    routing = document.get("routing") if isinstance(document.get("routing"), Mapping) else {}
    pools = routing.get("quota_pools") if isinstance(routing, Mapping) else {}
    pool_record = pools.get(pool_id) if isinstance(pools, Mapping) and isinstance(pool_id, str) else None
    account_id = pool_record.get("account_id") if isinstance(pool_record, Mapping) else None

    seat_used = len(occupants)
    # Every occupant of this seat counts against the seat's configured scopes.
    # A recorded identity that no longer matches the roster is still occupied
    # capacity; it is flagged, never silently dropped or re-attributed.
    provider_used = len(occupants)
    for item in occupants:
        recorded_provider = item.get("provider")
        if recorded_provider is not None and recorded_provider != provider:
            _add_reason(reasons, _REASON_LEGACY_IDENTITY_DRIFT)

    account_used: int | None
    pool_used: int | None
    if not isinstance(pool_id, str) or not pool_id:
        pool_used = None
        account_used = None
        _add_reason(reasons, _REASON_SCOPE_UNCONFIGURED)
    else:
        pool_used = len(occupants)
        for item in occupants:
            recorded_pool = item.get("pool")
            if recorded_pool is not None and recorded_pool != pool_id:
                _add_reason(reasons, _REASON_LEGACY_IDENTITY_DRIFT)
        if not isinstance(account_id, str) or not account_id:
            account_used = None
        else:
            account_used = len(occupants)
            for item in occupants:
                recorded_account = item.get("account_id")
                if recorded_account is not None and recorded_account != account_id:
                    _add_reason(reasons, _REASON_LEGACY_IDENTITY_DRIFT)

    return {
        "used": {
            "seat": seat_used,
            "provider": provider_used,
            "account": account_used,
            "pool": pool_used,
        },
        "reasons": reasons,
    }
