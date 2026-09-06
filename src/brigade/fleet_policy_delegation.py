"""Brigade-to-T3 Fleet policy delegation (identity and reservation adoption).

This module implements the frozen brigade.fleet_policy_delegation.v1 contract.
Delegations bind an origin routing decision to an immutable source git revision
and parent request ID, adopting the decision's reservation without re-routing or
creating a second reservation.

Authority models plan/admit/launch proof workers and T3 clients validate
delegations using ``validate_delegation(conn, delegation_id, caller_node, phase=...)``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import secrets
import sqlite3
from typing import Any, Mapping

from . import fleet_hub_policy
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden

DELEGATION_SCHEMA = "brigade.fleet_policy_delegation.v1"
DELEGATION_PUBLIC_KEYS = (
    "schema",
    "delegation_id",
    "launch_authorized",
    "consumer",
    "adapter",
    "parent_request_id",
    "decision_id",
    "reservation_id",
    "session_id",
    "repo_identity",
    "seat",
    "workload",
    "policy_version",
    "policy_digest",
    "origin_node",
    "target_machine",
    "target_node",
    "source_revision",
    "created_at",
    "expires_at",
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PROTOCOL_PREFIX = re.compile(r"^[a-zA-Z0-9+.-]+://")


def _parse_stamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(stamp: datetime | None = None) -> str:
    dt = stamp or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_source_revision(raw: Any) -> str:
    if not isinstance(raw, str) or not SHA_RE.match(raw):
        raise FleetHubError("source_revision must be a 40-character lowercase hexadecimal sha")
    return raw


def _validate_text(raw: Any, field: str, limit: int = 512) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise FleetHubError(f"delegation field '{field}' is required and must be a non-empty string")
    if len(raw) > limit:
        raise FleetHubError(f"delegation field '{field}' exceeds {limit} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise FleetHubError(f"delegation field '{field}' must not contain control characters")
    return raw.strip()


def _validate_repo_identity(repo: str) -> str:
    if repo.startswith("github:") or _PROTOCOL_PREFIX.match(repo):
        raise FleetHubError("repo_identity must be a canonical hostname/path, not github: prefix")
    if any(ord(char) < 32 or ord(char) == 127 for char in repo):
        raise FleetHubError("repo_identity must not contain control characters")
    return repo


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create delegation storage and indices if missing."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_policy_delegations (
            delegation_id TEXT NOT NULL PRIMARY KEY,
            parent_request_id TEXT NOT NULL,
            origin_node TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            reservation_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            consumer TEXT NOT NULL,
            adapter TEXT NOT NULL,
            repo_identity TEXT NOT NULL,
            seat TEXT NOT NULL,
            workload TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            policy_digest TEXT NOT NULL,
            target_machine TEXT NOT NULL,
            target_node TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            parent_metadata TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            document TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_parent_origin
        ON fleet_policy_delegations (origin_node, parent_request_id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_delegation_decision
        ON fleet_policy_delegations (decision_id)
        """
    )


def create_delegation(conn: sqlite3.Connection, request: Mapping[str, Any], caller_node: str) -> dict[str, Any]:
    """Create a new delegation binding parent_request_id and source_revision to a routed decision."""
    ensure_schema(conn)
    parent_request_id = _validate_text(request.get("parent_request_id"), "parent_request_id")
    source_revision = _validate_source_revision(request.get("source_revision"))
    decision_id = _validate_text(request.get("decision_id"), "decision_id")
    parent_metadata = request.get("parent_metadata")

    row = conn.execute(
        """
        SELECT document, origin_node, reservation_id, selected_machine, selected_seat,
               policy_version, policy_digest, session_id, consumer, repo_identity, workload
        FROM fleet_route_decisions WHERE decision_id=?
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        raise FleetHubError(f"fleet routing decision {decision_id!r} does not exist")

    (
        _dec_doc,
        origin_node,
        reservation_id,
        selected_machine,
        selected_seat,
        policy_version,
        policy_digest,
        session_id,
        consumer,
        repo_identity,
        workload,
    ) = row

    if caller_node != origin_node:
        raise FleetHubForbidden("only the decision origin node may create a delegation")

    if consumer != "brigade-run":
        raise FleetHubError("delegation requires consumer 'brigade-run'")

    if not repo_identity:
        raise FleetHubError("decision has no repository identity")
    _validate_repo_identity(repo_identity)

    current = fleet_hub_policy.current_policy(conn)
    if policy_version != current["revision"] or policy_digest != current["digest"]:
        raise FleetHubConflict("policy-mismatch: current policy does not match the decision")

    if not selected_machine:
        raise FleetHubError("decision has no selected target machine")

    target_record = current["document"].get("machines", {}).get(selected_machine)
    if not isinstance(target_record, Mapping) or not target_record.get("node_id"):
        raise FleetHubError(f"target machine {selected_machine!r} has no enrolled node")
    target_node = str(target_record["node_id"])

    if target_node == caller_node:
        raise FleetHubError("delegation requires a remote target node")

    if not reservation_id:
        raise FleetHubError("decision has no reservation")

    res_row = conn.execute(
        "SELECT state, accepted, expires_at FROM fleet_reservations WHERE reservation_id=?",
        (reservation_id,),
    ).fetchone()
    if res_row is None or res_row[0] == "released":
        raise FleetHubError("decision reservation is not active")
    res_expires = _parse_stamp(res_row[2])
    now = datetime.now(timezone.utc)
    if res_expires is None or res_expires <= now:
        raise FleetHubError("decision reservation has expired")
    expires_at_str = str(res_row[2])

    existing = conn.execute(
        """
        SELECT delegation_id, parent_request_id, origin_node, decision_id,
               source_revision, reservation_id, repo_identity, document
        FROM fleet_policy_delegations
        WHERE (origin_node=? AND parent_request_id=?) OR decision_id=?
        """,
        (caller_node, parent_request_id, decision_id),
    ).fetchone()

    if existing is not None:
        (
            _ex_id,
            ex_parent,
            ex_origin,
            ex_decision,
            ex_sha,
            ex_res,
            ex_repo,
            ex_doc,
        ) = existing
        if (
            ex_parent == parent_request_id
            and ex_origin == caller_node
            and ex_decision == decision_id
            and ex_sha == source_revision
            and ex_res == reservation_id
            and ex_repo == repo_identity
        ):
            return json.loads(ex_doc)
        raise FleetHubConflict("delegation context conflict for parent_request_id or decision_id")

    delegation_id = "del-" + secrets.token_hex(16)
    created_at = _iso()

    envelope = {
        "schema": DELEGATION_SCHEMA,
        "delegation_id": delegation_id,
        "launch_authorized": False,
        "consumer": "brigade-run",
        "adapter": "t3-fleet",
        "parent_request_id": parent_request_id,
        "decision_id": decision_id,
        "reservation_id": reservation_id,
        "session_id": session_id,
        "repo_identity": repo_identity,
        "seat": selected_seat,
        "workload": workload or "",
        "policy_version": current["revision"],
        "policy_digest": current["digest"],
        "origin_node": caller_node,
        "target_machine": selected_machine,
        "target_node": target_node,
        "source_revision": source_revision,
        "created_at": created_at,
        "expires_at": expires_at_str,
    }

    parent_meta_str = json.dumps(parent_metadata) if parent_metadata is not None else None
    doc_json = json.dumps(envelope, sort_keys=True)

    conn.execute(
        """
        INSERT INTO fleet_policy_delegations (
            delegation_id, parent_request_id, origin_node, decision_id, reservation_id,
            session_id, consumer, adapter, repo_identity, seat, workload,
            policy_version, policy_digest, target_machine, target_node,
            source_revision, parent_metadata, created_at, expires_at, document
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            delegation_id,
            parent_request_id,
            caller_node,
            decision_id,
            reservation_id,
            session_id,
            "brigade-run",
            "t3-fleet",
            repo_identity,
            selected_seat,
            workload or "",
            current["revision"],
            current["digest"],
            selected_machine,
            target_node,
            source_revision,
            parent_meta_str,
            created_at,
            expires_at_str,
            doc_json,
        ),
    )

    return {k: envelope[k] for k in DELEGATION_PUBLIC_KEYS}


def show_delegation(conn: sqlite3.Connection, delegation_id: str, caller_node: str) -> dict[str, Any]:
    """Show an existing delegation for the owning origin or selected target."""
    return validate_delegation(conn, delegation_id, caller_node, phase="read")


def validate_delegation(
    conn: sqlite3.Connection,
    delegation_id: str,
    caller_node: str,
    *,
    phase: str = "read",
) -> dict[str, Any]:
    """Validate a delegation and return its frozen envelope.

    Helper for authority models plan/admit/launch proof workers and T3 clients.

    Parameters:
        conn: SQLite connection with fleet schema.
        delegation_id: Opaque delegation ID (e.g. 'del-...').
        caller_node: Authenticated node ID of the caller.
        phase: One of 'read', 'prepare', or 'launch'.
            - 'read': Allowed for origin_node or target_node.
            - 'prepare': Allowed only for target_node.
            - 'launch': Allowed only for target_node; requires reservation to be accepted.

    Returns:
        The verified 20-key dictionary matching brigade.fleet_policy_delegation.v1.

    Raises:
        FleetHubError: If delegation is missing, expired, released, or invalid.
        FleetHubForbidden: If caller_node is not authorized for the requested phase.
        FleetHubConflict: If current policy does not match delegation policy_version/digest.
    """
    if phase not in {"read", "prepare", "launch"}:
        raise FleetHubError(f"invalid delegation validation phase: {phase!r}")

    ensure_schema(conn)

    row = conn.execute(
        """
        SELECT delegation_id, parent_request_id, origin_node, decision_id,
               reservation_id, session_id, consumer, adapter, repo_identity,
               seat, workload, policy_version, policy_digest, target_machine,
               target_node, source_revision, created_at, expires_at, document
        FROM fleet_policy_delegations WHERE delegation_id=?
        """,
        (delegation_id,),
    ).fetchone()
    if row is None:
        raise FleetHubError(f"delegation {delegation_id!r} does not exist")

    (
        _del_id,
        _parent_request_id,
        origin_node,
        _decision_id,
        reservation_id,
        _session_id,
        _consumer,
        _adapter,
        _repo_identity,
        _seat,
        _workload,
        policy_version,
        policy_digest,
        target_machine,
        target_node,
        _source_revision,
        _created_at,
        _expires_at,
        doc_json,
    ) = row

    if phase == "read":
        if caller_node not in (origin_node, target_node):
            raise FleetHubForbidden("only the owning origin or selected target node may view this delegation")
    else:  # prepare or launch
        if caller_node != target_node:
            raise FleetHubForbidden("only the selected target node may prepare or launch this delegation")

    current = fleet_hub_policy.current_policy(conn)
    if policy_version != current["revision"] or policy_digest != current["digest"]:
        raise FleetHubConflict("policy-mismatch: current policy does not match delegation")

    res_row = conn.execute(
        "SELECT state, accepted, expires_at FROM fleet_reservations WHERE reservation_id=?",
        (reservation_id,),
    ).fetchone()
    if res_row is None or res_row[0] == "released":
        raise FleetHubError("delegation reservation is not active")

    res_expires = _parse_stamp(res_row[2])
    now = datetime.now(timezone.utc)
    if res_expires is None or res_expires <= now:
        raise FleetHubError("delegation reservation has expired")

    if phase == "launch":
        if not bool(res_row[1]):
            raise FleetHubError("delegation reservation has not been accepted for execution")

    target_record = current["document"].get("machines", {}).get(target_machine)
    if (
        not isinstance(target_record, Mapping)
        or str(target_record.get("node_id")) != target_node
        or target_record.get("enabled") is False
    ):
        raise FleetHubError(f"target machine {target_machine!r} is unavailable")

    envelope = json.loads(doc_json)
    return {k: envelope[k] for k in DELEGATION_PUBLIC_KEYS}
