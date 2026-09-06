"""Fleet Hub authority for the control-plane policy (schema v20).

Additive tables next to the existing roster and preference stores:

- ``fleet_policy_revisions`` - immutable, append-only snapshots carrying
  revision, digest, actor, reason, creation time, and parent revision.
- ``fleet_policy_meta`` - the one-row pointer at the current revision, used
  as the compare-and-swap fence for every write.
- ``fleet_policy_sessions`` - per-session receipts of the policy a consumer
  actually loaded, plus refresh acknowledgements.

What this module deliberately does *not* do yet: it does not migrate the
legacy ``model_policy`` roster, does not project into it, and does not
enforce anything at launch time. Those are later slices. Saving a revision
here changes no admission decision today.

Every mutation validates a bounded JSON document, runs inside one
transaction, and refuses on a version mismatch rather than merging. Rollback
appends a *new* revision holding an older document; revisions never rewind.

Policy revisions carry no telemetry. Capacity and quota observations are a
separate, separately versioned stream, so a heartbeat can never make a
session's policy look stale.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import fleet_hub, fleet_policy
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden
from .fleet_policy import FleetPolicyError, resolve_policy  # noqa: F401  (re-exported API)

MAX_ACTOR = 128
MAX_REASON = 512
MAX_SOURCE = 64
MAX_DETAIL = 512
MAX_SESSION_ID = 128

REFRESH_STATES = ("none", "requested", "applied", "failed")
ACK_STATES = ("applied", "failed")

_REVISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_policy_revisions (
    revision INTEGER PRIMARY KEY,
    schema TEXT NOT NULL,
    digest TEXT NOT NULL,
    document TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    parent_revision INTEGER
);
"""
_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_policy_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    current_revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""
_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_policy_sessions (
    session_id TEXT NOT NULL PRIMARY KEY,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    revision INTEGER,
    digest TEXT,
    source TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    refresh_state TEXT NOT NULL DEFAULT 'none',
    refresh_requested_at TEXT,
    refresh_updated_at TEXT,
    refresh_detail TEXT,
    updated_at TEXT NOT NULL
);
"""

_REVISION_COLUMNS = "revision, schema, digest, document, actor, reason, created_at, parent_revision"
_SESSION_COLUMNS = (
    "session_id, consumer, owner_node, repo_identity, revision, digest, source, loaded_at, "
    "refresh_state, refresh_requested_at, refresh_updated_at, refresh_detail"
)
_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_policy_pending (
    pending_key TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    overrides TEXT,
    override_reason TEXT,
    effective TEXT NOT NULL,
    sources TEXT NOT NULL,
    selected TEXT,
    instructions TEXT,
    context_hash TEXT,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_PENDING_COLUMNS = (
    "pending_key, session_id, consumer, owner_node, repo_identity, origin, revision, digest, "
    "overrides, override_reason, effective, sources, selected, instructions, context_hash, source, "
    "created_at, updated_at"
)

_ACKNOWLEDGED_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_policy_acknowledged (
    receipt_key TEXT NOT NULL PRIMARY KEY,
    external_session_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    source TEXT NOT NULL,
    effective TEXT NOT NULL,
    sources TEXT NOT NULL,
    selected TEXT,
    overrides TEXT,
    override_reason TEXT,
    context_hash TEXT,
    loaded_at TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
"""
_ACKNOWLEDGED_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_policy_acknowledged_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_key TEXT NOT NULL,
    external_session_id TEXT NOT NULL,
    consumer TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    repo_identity TEXT,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    source TEXT NOT NULL,
    effective TEXT NOT NULL,
    sources TEXT NOT NULL,
    selected TEXT,
    overrides TEXT,
    override_reason TEXT,
    context_hash TEXT,
    loaded_at TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
"""
_ACK_COLUMNS = (
    "receipt_key, external_session_id, consumer, owner_node, repo_identity, origin, revision, digest, "
    "source, effective, sources, selected, overrides, override_reason, context_hash, loaded_at, acknowledged_at"
)
MAX_PENDING_JSON = 262144
MAX_ORIGIN = 256
LOADED_AT_FUTURE_SECONDS = 300


def ensure_schema(conn: sqlite3.Connection) -> None:
    """v19 -> v20: policy revisions, the CAS pointer, and session receipts.

    Seeding is lazy and generic: revision 1 is the empty policy, which
    resolves to training-off defaults and names no machine, seat, or
    repository.
    """
    conn.execute(_REVISIONS_SCHEMA)
    conn.execute(_META_SCHEMA)
    conn.execute(_SESSIONS_SCHEMA)
    conn.execute(_PENDING_SCHEMA)
    conn.execute(_ACKNOWLEDGED_SCHEMA)
    conn.execute(_ACKNOWLEDGED_HISTORY_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_policy_sessions_revision ON fleet_policy_sessions (revision)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS fleet_policy_pending_session ON fleet_policy_pending (session_id, consumer)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS fleet_policy_ack_history_key ON fleet_policy_acknowledged_history (receipt_key, revision)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS fleet_policy_ack_ext_session ON fleet_policy_acknowledged (external_session_id, consumer)"
    )
    if conn.execute("SELECT 1 FROM fleet_policy_meta WHERE singleton=1").fetchone() is not None:
        return
    document = fleet_policy.parse_document(fleet_policy.empty_document())
    now = fleet_hub._utc_now()
    conn.execute(
        f"INSERT OR IGNORE INTO fleet_policy_revisions ({_REVISION_COLUMNS}) VALUES (1, ?, ?, ?, ?, ?, ?, NULL)",
        (
            fleet_policy.POLICY_SCHEMA,
            fleet_policy.document_digest(document),
            fleet_policy.canonical_json(document),
            "schema",
            "initial empty policy",
            now,
        ),
    )
    conn.execute(
        "INSERT INTO fleet_policy_meta (singleton, current_revision, updated_at, updated_by) VALUES (1, 1, ?, ?)",
        (now, "schema"),
    )


def _bounded(raw: Any, field: str, limit: int, *, required: bool = True, allow_newlines: bool = False) -> str | None:
    if raw is None:
        if required:
            raise FleetHubError(f"fleet policy field {field!r} is required")
        return None
    if not isinstance(raw, str):
        raise FleetHubError(f"fleet policy field {field!r} must be a string")
    if required and not raw:
        raise FleetHubError(f"fleet policy field {field!r} must not be empty")
    if len(raw) > limit:
        raise FleetHubError(f"fleet policy field {field!r} must be at most {limit} characters")
    if allow_newlines:
        if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in raw):
            raise FleetHubError(f"fleet policy field {field!r} must not contain control characters")
    else:
        fleet_hub._reject_controls(raw, field, kind="fleet policy")
    return raw


def _parse(document: Any) -> dict[str, Any]:
    try:
        return fleet_policy.parse_document(document)
    except FleetPolicyError as exc:
        raise FleetHubError(str(exc)) from exc


def _current_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT current_revision FROM fleet_policy_meta WHERE singleton=1").fetchone()
    if row is None:
        raise FleetHubError("fleet policy revision metadata is missing; run init_db")
    return int(row[0])


def _revision_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "revision": int(row[0]),
        "schema": row[1],
        "digest": row[2],
        "document": json.loads(row[3]),
        "actor": row[4],
        "reason": row[5],
        "created_at": row[6],
        "parent_revision": None if row[7] is None else int(row[7]),
    }


def _fetch_revision(conn: sqlite3.Connection, revision: int) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM fleet_policy_revisions WHERE revision=?", (revision,)
    ).fetchone()
    if row is None:
        raise FleetHubError(f"fleet policy revision {revision} does not exist")
    return _revision_payload(row)


def current_policy(conn: sqlite3.Connection) -> dict[str, Any]:
    """The current revision with its parsed document and digest."""
    return _fetch_revision(conn, _current_revision(conn))


def list_revisions(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """Revision history, newest first. Snapshots are never rewritten."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise FleetHubError("fleet policy history limit must be an integer in 1..500")
    rows = conn.execute(
        f"SELECT {_REVISION_COLUMNS} FROM fleet_policy_revisions ORDER BY revision DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_revision_payload(row) for row in rows]


def _pin_errors(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"code": violation["reason"], "seat": violation["seat"], "changed": violation["changed"]}
        for violation in fleet_policy.pin_violations(current, proposed)
    ]


def _session_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT {_SESSION_COLUMNS} FROM fleet_policy_sessions ORDER BY session_id").fetchall()
    return [
        {
            "session_id": row[0],
            "consumer": row[1],
            "owner_node": row[2],
            "repo_identity": row[3],
            "revision": None if row[4] is None else int(row[4]),
            "digest": row[5],
            "source": row[6],
            "loaded_at": row[7],
            "refresh_state": row[8],
            "refresh_requested_at": row[9],
            "refresh_updated_at": row[10],
            "refresh_detail": row[11],
        }
        for row in rows
    ]


def _effective_expanded(document: Mapping[str, Any], consumer: str | None, repo: str | None) -> Any:
    resolved = fleet_policy.resolve_policy(document, consumer, repo, _parsed=document)
    effective = resolved["effective"]
    used_seats = [seat_name for seat_name in effective.get("roles", {}).values() if seat_name]

    seat_graph: dict[str, Any] = {}
    visited_seats: set[str] = set()
    queue = list(used_seats)
    while queue:
        seat_name = queue.pop(0)
        if seat_name in visited_seats:
            continue
        visited_seats.add(seat_name)
        seat_record = document.get("seats", {}).get(seat_name)
        if seat_record is None:
            seat_graph[seat_name] = None
            continue
        effective_bindings = fleet_policy.effective_seat_bindings(document, consumer, seat_name, _parsed=document)
        seat_def = dict(seat_record)
        seat_def["bindings"] = effective_bindings
        seat_graph[seat_name] = seat_def
        for fb in seat_record.get("fallback", []):
            if fb not in visited_seats:
                queue.append(fb)

    repo_record = document.get("repositories", {}).get(repo) if repo else None
    repo_eligible = repo_record.get("eligible_machines") if repo_record else None
    routing = document.get("routing", {})
    workload_requirements = routing.get("workload_requirements", {})
    all_machines = document.get("machines", {})

    applicable_machine_names: set[str] = set()
    explicit_machine = effective.get("execution", {}).get("machine")
    if explicit_machine:
        applicable_machine_names.add(explicit_machine)

    for seat_def in seat_graph.values():
        if not seat_def:
            continue
        seat_machines = seat_def.get("eligible_machines") or []
        if seat_machines:
            for m in seat_machines:
                if repo_eligible is None or not repo_eligible or m in repo_eligible:
                    applicable_machine_names.add(m)
        else:
            for m_name, m_record in all_machines.items():
                if repo_eligible and m_name not in repo_eligible:
                    continue
                m_os = m_record.get("os", "unknown")
                m_caps = set(m_record.get("capabilities", []))
                if workload_requirements:
                    matches_any_workload = False
                    for req in workload_requirements.values():
                        req_os = req.get("os", "unknown")
                        if req_os != "unknown" and m_os != "unknown" and m_os != req_os:
                            continue
                        req_caps = set(req.get("capabilities", []))
                        if req_caps and not req_caps.issubset(m_caps):
                            continue
                        matches_any_workload = True
                        break
                    if not matches_any_workload:
                        continue
                applicable_machine_names.add(m_name)

    machine_graph: dict[str, Any] = {}
    visited_machines: set[str] = set()
    m_queue = list(sorted(applicable_machine_names))
    while m_queue:
        m_name = m_queue.pop(0)
        if m_name in visited_machines:
            continue
        visited_machines.add(m_name)
        m_record = all_machines.get(m_name)
        if m_record is None:
            machine_graph[m_name] = None
            continue
        machine_graph[m_name] = dict(m_record)
        for m_fb in m_record.get("fallback", []):
            if m_fb not in visited_machines:
                m_queue.append(m_fb)

    consumer_record = document.get("consumers", {}).get(consumer) if consumer else None
    consumer_meta = (
        {
            "reload": consumer_record.get("reload"),
            "coverage": consumer_record.get("coverage"),
            "adapter_version": consumer_record.get("adapter_version"),
        }
        if consumer_record
        else None
    )

    return {
        "effective": effective,
        "constraints": resolved.get("constraints"),
        "consumer": consumer_meta,
        "seats": seat_graph,
        "machines": machine_graph,
        "routing": {
            "enabled": routing.get("enabled", False),
            "telemetry_ttl_seconds": routing.get("telemetry_ttl_seconds"),
            "reservation_ttl_seconds": routing.get("reservation_ttl_seconds"),
            "quota_reserve_percent": routing.get("quota_reserve_percent"),
            "workload_requirements": workload_requirements,
            "pools": {
                s_name: document.get("routing", {}).get("quota_pools", {}).get(s_def.get("quota_pool"))
                for s_name, s_def in seat_graph.items()
                if s_def and s_def.get("quota_pool")
            },
        },
    }


def _effective(document: Mapping[str, Any], consumer: str | None, repo: str | None) -> Any:
    return _effective_expanded(document, consumer, repo)


def _affected(
    conn: sqlite3.Connection,
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    digest_changed: bool,
) -> dict[str, list[str]]:
    consumers = sorted(set(current["consumers"]) | set(proposed["consumers"]))
    repositories = sorted(set(current["repositories"]) | set(proposed["repositories"]))

    memo: dict[tuple[int, str | None, str | None], Any] = {}

    def get_expanded(doc: Mapping[str, Any], c: str | None, r: str | None) -> Any:
        key = (id(doc), c, r)
        if key not in memo:
            memo[key] = _effective_expanded(doc, c, r)
        return memo[key]

    changed_consumers = [
        consumer
        for consumer in consumers
        if get_expanded(current, consumer, None) != get_expanded(proposed, consumer, None)
    ]
    changed_repositories = []
    for repo in repositories:
        if get_expanded(current, None, repo) != get_expanded(proposed, None, repo):
            changed_repositories.append(repo)
        else:
            for consumer in changed_consumers:
                exp_curr = dict(get_expanded(current, consumer, repo))
                exp_prop = dict(get_expanded(proposed, consumer, repo))
                exp_curr["consumer"] = None
                exp_prop["consumer"] = None
                if exp_curr != exp_prop:
                    changed_repositories.append(repo)
                    break

    sessions = [
        row["session_id"]
        for row in _session_rows(conn)
        if get_expanded(current, row["consumer"], row["repo_identity"])
        != get_expanded(proposed, row["consumer"], row["repo_identity"])
    ]
    return {"consumers": changed_consumers, "repositories": changed_repositories, "sessions": sessions}


def preview_policy(
    conn: sqlite3.Connection,
    document: Any,
    *,
    expected_version: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Dry-run a save: diff, affected scope, and refusal reasons. Writes nothing.

    Errors are returned rather than raised so a UI can render every problem
    with the proposed document at once. ``save_policy`` re-checks all of them.
    """
    _bounded(actor, "actor", MAX_ACTOR)
    _bounded(reason, "reason", MAX_REASON)
    current = current_policy(conn)
    errors: list[dict[str, Any]] = []
    if type(expected_version) is not int:
        raise FleetHubError("fleet policy field 'expected_version' must be an integer")
    if expected_version != current["revision"]:
        errors.append(
            {
                "code": "policy_revision_conflict",
                "expected_version": expected_version,
                "current_revision": current["revision"],
            }
        )
    try:
        proposed = fleet_policy.parse_document(document)
    except FleetPolicyError as exc:
        errors.append({"code": "invalid_document", "detail": str(exc)})
        return {
            "ok": False,
            "current_revision": current["revision"],
            "next_revision": current["revision"] + 1,
            "digest": None,
            "diff": {"added": [], "removed": [], "changed": []},
            "affected": {"consumers": [], "repositories": [], "sessions": []},
            "errors": errors,
        }
    errors.extend(_pin_errors(current["document"], proposed))
    digest = fleet_policy.document_digest(proposed)
    return {
        "ok": not errors,
        "current_revision": current["revision"],
        "next_revision": current["revision"] + 1,
        "digest": digest,
        "diff": fleet_policy.diff_documents(current["document"], proposed),
        "affected": _affected(conn, current["document"], proposed, digest_changed=digest != current["digest"]),
        "errors": errors,
    }


def _write_revision(
    conn: sqlite3.Connection,
    *,
    expected_version: int,
    actor: str,
    reason: str,
    build: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """CAS the meta pointer and append one immutable revision.

    ``build`` receives the current revision payload and returns the parsed
    document to store plus any extra payload fields. It runs inside the
    transaction so validation always sees the fenced current state.
    """
    if type(expected_version) is not int:
        raise FleetHubError("fleet policy field 'expected_version' must be an integer")
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        current = current_policy(conn)
        if current["revision"] != expected_version:
            raise FleetHubConflict(
                f"policy_revision_conflict: expected {expected_version}, current {current['revision']}"
            )
        document, extra = build(current)
        revision = current["revision"] + 1
        digest = fleet_policy.document_digest(document)
        now = fleet_hub._utc_now()
        conn.execute(
            f"INSERT INTO fleet_policy_revisions ({_REVISION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision,
                fleet_policy.POLICY_SCHEMA,
                digest,
                fleet_policy.canonical_json(document),
                actor,
                reason,
                now,
                current["revision"],
            ),
        )
        conn.execute(
            "UPDATE fleet_policy_meta SET current_revision=?, updated_at=?, updated_by=? WHERE singleton=1",
            (revision, now, actor),
        )
        if opened:
            conn.commit()
        return {
            "revision": revision,
            "schema": fleet_policy.POLICY_SCHEMA,
            "digest": digest,
            "document": document,
            "actor": actor,
            "reason": reason,
            "created_at": now,
            "parent_revision": current["revision"],
            **extra,
        }
    except BaseException:
        if opened:
            conn.rollback()
        raise


def _refuse_pin_violations(current: Mapping[str, Any], proposed: Mapping[str, Any]) -> None:
    violations = _pin_errors(current, proposed)
    if not violations:
        return
    detail = ", ".join(f"{item['seat']}: {item['code']}" for item in violations)
    raise FleetHubError(f"fleet policy refuses a pinned seat change without an explicit unpin ({detail})")


def save_policy(
    conn: sqlite3.Connection,
    document: Any,
    *,
    expected_version: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Append a new policy revision under a compare-and-swap on ``expected_version``."""
    actor_value = _bounded(actor, "actor", MAX_ACTOR)
    reason_value = _bounded(reason, "reason", MAX_REASON)
    assert actor_value is not None and reason_value is not None
    proposed = _parse(document)

    def build(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        _refuse_pin_violations(current["document"], proposed)
        return proposed, {}

    return _write_revision(conn, expected_version=expected_version, actor=actor_value, reason=reason_value, build=build)


def rollback_policy(
    conn: sqlite3.Connection,
    *,
    to_revision: int,
    expected_version: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Restore an older document as a *new* revision, revalidated against today.

    The revision counter only moves forward. A rollback that would move a
    pinned seat, or that no longer validates, is refused instead of applied.
    """
    actor_value = _bounded(actor, "actor", MAX_ACTOR)
    reason_value = _bounded(reason, "reason", MAX_REASON)
    assert actor_value is not None and reason_value is not None
    if type(to_revision) is not int:
        raise FleetHubError("fleet policy field 'to_revision' must be an integer")

    def build(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        restored = _parse(_fetch_revision(conn, to_revision)["document"])
        _refuse_pin_violations(current["document"], restored)
        return restored, {"rolled_back_from": current["revision"], "restored_revision": to_revision}

    return _write_revision(conn, expected_version=expected_version, actor=actor_value, reason=reason_value, build=build)


def _session_payload(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    for row in _session_rows(conn):
        if row["session_id"] == session_id:
            return row
    raise FleetHubError(f"fleet policy session {session_id!r} has no receipt")


def _require_owner(row: Mapping[str, Any], node_id: str) -> None:
    if row["owner_node"] != node_id:
        raise FleetHubForbidden(
            f"fleet policy session {row['session_id']!r} is owned by another node; refusing the write"
        )


def _existing_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    for row in _session_rows(conn):
        if row["session_id"] == session_id:
            return row
    return None


def record_session_policy(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    consumer: str,
    node_id: str,
    repo_identity: str | None,
    revision: int | None,
    digest: str | None,
    source: str,
    loaded_at: str | None = None,
) -> dict[str, Any]:
    """Record what a session actually loaded. Only the owning node may write it.

    ``revision``/``digest`` may both be ``None`` for a legacy or backfilled
    session whose loaded policy is genuinely unknown; a stated revision must
    exist and its digest must match, so no receipt can claim a version the
    Hub never issued.
    """
    session = _bounded(session_id, "session_id", MAX_SESSION_ID)
    consumer_value = _bounded(consumer, "consumer", fleet_policy.MAX_TEXT)
    owner = _bounded(node_id, "node_id", fleet_policy.MAX_TEXT)
    source_value = _bounded(source, "source", MAX_SOURCE)
    repo = _bounded(repo_identity, "repo_identity", fleet_policy.MAX_TEXT, required=False)
    loaded = _aware_timestamp(loaded_at, "loaded_at")
    assert session is not None and consumer_value is not None and owner is not None and source_value is not None
    if (revision is None) != (digest is None):
        raise FleetHubError("fleet policy session receipt needs both 'revision' and 'digest', or neither")
    if revision is not None:
        if type(revision) is not int:
            raise FleetHubError("fleet policy field 'revision' must be an integer")
        stored = _fetch_revision(conn, revision)
        if stored["digest"] != digest:
            raise FleetHubError(f"fleet policy digest does not match revision {revision}")

    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        existing = _existing_session(conn, session)
        if existing is not None:
            _require_owner(existing, owner)
            if existing["consumer"] != consumer_value or (existing["repo_identity"] or "") != (repo or ""):
                raise FleetHubError("fleet policy session is already owned by a different consumer or repository")
        now = fleet_hub._utc_now()
        conn.execute(
            "INSERT INTO fleet_policy_sessions (session_id, consumer, owner_node, repo_identity, revision, digest, "
            "source, loaded_at, refresh_state, refresh_requested_at, refresh_updated_at, refresh_detail, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'none', NULL, NULL, NULL, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET consumer=excluded.consumer, repo_identity=excluded.repo_identity, "
            "revision=excluded.revision, digest=excluded.digest, source=excluded.source, "
            "loaded_at=excluded.loaded_at, refresh_state='none', refresh_requested_at=NULL, "
            "refresh_updated_at=NULL, refresh_detail=NULL, updated_at=excluded.updated_at",
            (session, consumer_value, owner, repo, revision, digest, source_value, loaded, now),
        )
        payload = _session_payload(conn, session)
        if opened:
            conn.commit()
        return payload
    except BaseException:
        if opened:
            conn.rollback()
        raise


def request_session_refresh(conn: sqlite3.Connection, session_id: str, *, actor: str) -> dict[str, Any]:
    """Ask a session to reload. This is a request, never an acknowledgement."""
    session = _bounded(session_id, "session_id", MAX_SESSION_ID)
    _bounded(actor, "actor", MAX_ACTOR)
    assert session is not None
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        _session_payload(conn, session)
        now = fleet_hub._utc_now()
        conn.execute(
            "UPDATE fleet_policy_sessions SET refresh_state='requested', refresh_requested_at=?, "
            "refresh_updated_at=NULL, refresh_detail=NULL, updated_at=? WHERE session_id=?",
            (now, now, session),
        )
        payload = _session_payload(conn, session)
        if opened:
            conn.commit()
        return payload
    except BaseException:
        if opened:
            conn.rollback()
        raise


def acknowledge_session_refresh(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    node_id: str,
    state: str,
    revision: int | None = None,
    digest: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Record an acknowledgement a consumer actually sent.

    ``applied`` must name the revision and digest the session now holds, and
    that pair must exist; ``failed`` leaves the loaded revision untouched so a
    failed reload never looks like a successful one.
    """
    session = _bounded(session_id, "session_id", MAX_SESSION_ID)
    owner = _bounded(node_id, "node_id", fleet_policy.MAX_TEXT)
    detail_value = _bounded(detail, "detail", MAX_DETAIL, required=False)
    assert session is not None and owner is not None
    if state not in ACK_STATES:
        raise FleetHubError(f"fleet policy refresh acknowledgement must be one of: {', '.join(ACK_STATES)}")
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        row = _session_payload(conn, session)
        _require_owner(row, owner)
        now = fleet_hub._utc_now()
        if state == "failed":
            conn.execute(
                "UPDATE fleet_policy_sessions SET refresh_state='failed', refresh_updated_at=?, refresh_detail=?, "
                "updated_at=? WHERE session_id=?",
                (now, detail_value, now, session),
            )
            payload = _session_payload(conn, session)
            if opened:
                conn.commit()
            return payload
        if type(revision) is not int or digest is None:
            raise FleetHubError("an 'applied' acknowledgement must carry the loaded 'revision' and 'digest'")
        stored = _fetch_revision(conn, revision)
        if stored["digest"] != digest:
            raise FleetHubError(f"fleet policy digest does not match revision {revision}")
        conn.execute(
            "UPDATE fleet_policy_sessions SET refresh_state='applied', refresh_updated_at=?, refresh_detail=?, "
            "revision=?, digest=?, loaded_at=?, updated_at=? WHERE session_id=?",
            (now, detail_value, revision, digest, now, now, session),
        )
        payload = _session_payload(conn, session)
        if opened:
            conn.commit()
        return payload
    except BaseException:
        if opened:
            conn.rollback()
        raise


def session_state(
    row: Mapping[str, Any],
    current_revision: int,
    reload_capability: str | None,
    *,
    pending_apply_required: bool | None = None,
) -> str:
    """Derive current/stale/refreshable/restart-required/unknown from a stored row.

    A requested or failed refresh is never ``current``, even when the loaded
    revision still matches. ``failed`` leaves the loaded revision untouched
    but the acknowledgement is not a successful apply.
    """
    if row.get("revision") is None or row.get("digest") is None:
        return "unknown"
    if pending_apply_required is None:
        pending = row.get("refresh_state") in {"requested", "failed"}
        if row["revision"] == current_revision and not pending:
            return "current"
    elif not pending_apply_required:
        return "current"

    if reload_capability == "restart-required":
        return "restart-required"
    if reload_capability == "refreshable":
        return "refreshable"
    return "stale"


def _session_state(
    row: Mapping[str, Any],
    current_revision: int,
    reload_capability: str | None,
    *,
    pending_apply_required: bool | None = None,
) -> str:
    return session_state(row, current_revision, reload_capability, pending_apply_required=pending_apply_required)


def list_session_states(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every session receipt with its state against the current revision.

    States: ``current``, ``stale``, ``refreshable``, ``restart-required``, and
    ``unknown`` for legacy sessions that never acknowledged a load. A pending
    refresh is never reported as ``current``.
    """
    ensure_schema(conn)
    current = current_policy(conn)
    consumers = current["document"]["consumers"]
    rows: list[dict[str, Any]] = []
    for row in _session_rows(conn):
        record = consumers.get(row["consumer"])
        capability = record["reload"] if record else None

        ack_row = find_acknowledged_policy(conn, receipt_key=row["session_id"])
        if ack_row is None:
            ack_row = find_acknowledged_policy(
                conn,
                session_id=row["session_id"],
                node_id=row["owner_node"],
                consumer=row["consumer"],
                repo_identity=row["repo_identity"],
            )

        if ack_row is not None and ack_row["revision"] == row["revision"] and ack_row["digest"] == row["digest"]:
            loaded_snapshot: dict[str, Any] = ack_row
            receipt_key = ack_row["receipt_key"]
            external_session_id = ack_row["external_session_id"]
        else:
            receipt_key = row["session_id"]
            external_session_id = row["session_id"]
            loaded_snapshot = {
                "status": "unknown",
                "session_id": receipt_key,
                "external_session_id": external_session_id,
                "receipt_key": receipt_key,
                "consumer": row["consumer"],
                "owner_node": row["owner_node"],
                "repo_identity": row["repo_identity"],
                "revision": row["revision"],
                "digest": row["digest"],
                "source": row["source"],
                "loaded_at": row["loaded_at"],
                "effective": None,
                "sources": None,
                "selected": None,
                "overrides": None,
                "override_reason": None,
                "context_hash": None,
            }

        pending_snapshot = None
        try:
            pending_snapshot = get_pending_policy(conn, pending_key=receipt_key)
        except FleetHubError:
            try:
                pending_snapshot = get_pending_policy(
                    conn,
                    node_id=row["owner_node"],
                    consumer=row["consumer"],
                    repo_identity=row["repo_identity"],
                    session_id=external_session_id,
                )
            except FleetHubError:
                pass

        pending_apply_required = False
        if row.get("revision") is None or row.get("digest") is None:
            pending_apply_required = False
        elif row["revision"] != current["revision"] or row["digest"] != current["digest"]:
            pending_apply_required = True
        elif row.get("refresh_state") in {"requested", "failed"}:
            pending_apply_required = True
        elif pending_snapshot is not None:
            if loaded_snapshot.get("status") == "unknown":
                pending_apply_required = True
            elif (
                pending_snapshot.get("context_hash") != loaded_snapshot.get("context_hash")
                or pending_snapshot.get("revision") != loaded_snapshot.get("revision")
                or pending_snapshot.get("digest") != loaded_snapshot.get("digest")
            ):
                pending_apply_required = True

        state = _session_state(row, current["revision"], capability, pending_apply_required=pending_apply_required)

        if loaded_snapshot.get("status") == "unknown" or loaded_snapshot.get("context_hash") is None:
            context_state = "unknown"
        elif pending_apply_required:
            context_state = "stale"
        else:
            context_state = "current"

        rows.append(
            {
                **row,
                "receipt_key": receipt_key,
                "external_session_id": external_session_id,
                "loaded_snapshot": loaded_snapshot,
                "pending_snapshot": pending_snapshot,
                "current_revision": current["revision"],
                "reload": capability,
                "context_state": context_state,
                "pending_apply_required": pending_apply_required,
                "state": state,
                "context_hash": loaded_snapshot.get("context_hash"),
                "effective": loaded_snapshot.get("effective"),
                "sources": loaded_snapshot.get("sources"),
                "selected": loaded_snapshot.get("selected"),
                "origin": loaded_snapshot.get("origin"),
            }
        )
    return rows


def sessions_for_revision(conn: sqlite3.Connection, revision: int) -> list[str]:
    """Session ids that reported loading ``revision``. Used by preview scope."""
    return [row["session_id"] for row in _session_rows(conn) if row["revision"] == revision]


def registered_consumers(conn: sqlite3.Connection) -> Iterable[str]:
    """Consumers named by the current policy document."""
    return sorted(current_policy(conn)["document"]["consumers"])


def session_key(node_id: str, consumer: str, repo_identity: str | None, session_id: str) -> str:
    """Deterministic internal id so consumer/repo/node collisions cannot share a row."""
    material = "\x1f".join([node_id, consumer, repo_identity or "", session_id])
    return "p:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _aware_timestamp(raw: Any, field: str) -> str:
    if raw is None:
        return fleet_hub._utc_now()
    value = _bounded(raw, field, MAX_SOURCE)
    assert value is not None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FleetHubError(f"fleet policy field {field!r} must be an ISO-8601 timestamp") from exc
    if stamp.tzinfo is None:
        raise FleetHubError(f"fleet policy field {field!r} must be timezone-aware")
    now = datetime.now(timezone.utc)
    if stamp.astimezone(timezone.utc) - now > timedelta(seconds=LOADED_AT_FUTURE_SECONDS):
        raise FleetHubError(f"fleet policy field {field!r} is too far in the future")
    return stamp.astimezone(timezone.utc).isoformat()


def _load_pending_json(raw: Any, field: str, *, default: Any) -> Any:
    if not raw:
        return default
    try:
        return fleet_policy.parse_json_object(raw, limit=MAX_PENDING_JSON)
    except FleetPolicyError as exc:
        raise FleetHubError(f"fleet policy field {field!r} is not valid JSON") from exc


def _pending_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "pending_key": row[0],
        "session_id": row[1],
        "consumer": row[2],
        "owner_node": row[3],
        "repo_identity": row[4],
        "origin": row[5],
        "revision": int(row[6]),
        "digest": row[7],
        "overrides": _load_pending_json(row[8], "overrides", default={}),
        "override_reason": row[9],
        "effective": _load_pending_json(row[10], "effective", default={}),
        "sources": _load_pending_json(row[11], "sources", default={}),
        "selected": _load_pending_json(row[12], "selected", default=None) if row[12] else None,
        "instructions": row[13],
        "context_hash": row[14],
        "source": row[15],
        "created_at": row[16],
        "updated_at": row[17],
    }


def _dump_pending_json(value: Any, field: str) -> str | None:
    if value is None:
        return None
    try:
        fleet_policy.reject_json_tree(value, field)
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, FleetPolicyError) as exc:
        raise FleetHubError(f"fleet policy field {field!r} is not valid JSON") from exc
    if len(rendered.encode("utf-8")) > MAX_PENDING_JSON:
        raise FleetHubError(f"fleet policy field {field!r} exceeded the size limit")
    return rendered


def record_pending_policy(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    consumer: str,
    node_id: str,
    repo_identity: str | None,
    origin: str,
    revision: int,
    digest: str,
    overrides: Mapping[str, Any] | None,
    override_reason: str | None,
    effective: Mapping[str, Any],
    sources: Mapping[str, Any],
    selected: Mapping[str, Any] | None = None,
    instructions: str | None = None,
    context_hash: str | None = None,
    source: str = "resolve",
) -> dict[str, Any]:
    """Persist a prepared/resolved snapshot. This is not a loaded-session receipt."""
    session = _bounded(session_id, "session_id", MAX_SESSION_ID)
    consumer_value = _bounded(consumer, "consumer", fleet_policy.MAX_TEXT)
    owner = _bounded(node_id, "node_id", fleet_policy.MAX_TEXT)
    origin_value = _bounded(origin, "origin", MAX_ORIGIN)
    source_value = _bounded(source, "source", MAX_SOURCE)
    repo = _bounded(repo_identity, "repo_identity", fleet_policy.MAX_TEXT, required=False)
    reason = _bounded(override_reason, "override_reason", MAX_REASON, required=False)
    instructions_value = _bounded(instructions, "instructions", MAX_PENDING_JSON, required=False, allow_newlines=True)
    context = _bounded(context_hash, "context_hash", MAX_DETAIL, required=False)
    assert session is not None and consumer_value is not None and owner is not None
    assert origin_value is not None and source_value is not None
    if type(revision) is not int:
        raise FleetHubError("fleet policy field 'revision' must be an integer")
    stored = _fetch_revision(conn, revision)
    if stored["digest"] != digest:
        raise FleetHubError(f"fleet policy digest does not match revision {revision}")
    key = session_key(owner, consumer_value, repo, session)
    now = fleet_hub._utc_now()
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        conn.execute(
            f"INSERT INTO fleet_policy_pending ({_PENDING_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pending_key) DO UPDATE SET origin=excluded.origin, revision=excluded.revision, "
            "digest=excluded.digest, overrides=excluded.overrides, override_reason=excluded.override_reason, "
            "effective=excluded.effective, sources=excluded.sources, selected=excluded.selected, "
            "instructions=excluded.instructions, context_hash=excluded.context_hash, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (
                key,
                session,
                consumer_value,
                owner,
                repo,
                origin_value,
                revision,
                digest,
                _dump_pending_json(overrides or {}, "overrides"),
                reason,
                _dump_pending_json(effective, "effective"),
                _dump_pending_json(sources, "sources"),
                _dump_pending_json(selected, "selected") if selected is not None else None,
                instructions_value,
                context,
                source_value,
                now,
                now,
            ),
        )
        payload = get_pending_policy(conn, pending_key=key)
        if opened:
            conn.commit()
        return payload
    except BaseException:
        if opened:
            conn.rollback()
        raise


def get_pending_policy(
    conn: sqlite3.Connection,
    *,
    pending_key: str | None = None,
    node_id: str | None = None,
    consumer: str | None = None,
    repo_identity: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    if pending_key is None:
        if node_id is None or consumer is None or session_id is None:
            raise FleetHubError("fleet policy pending lookup requires a session identity")
        pending_key = session_key(node_id, consumer, repo_identity, session_id)
    row = conn.execute(
        f"SELECT {_PENDING_COLUMNS} FROM fleet_policy_pending WHERE pending_key=?", (pending_key,)
    ).fetchone()
    if row is None:
        raise FleetHubError(f"fleet policy session {session_id or pending_key!r} has no pending receipt")
    return _pending_row(row)


def find_pending_policy(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    consumer: str,
    repo_identity: str | None,
    node_id: str | None = None,
) -> dict[str, Any] | None:
    if node_id is not None:
        try:
            return get_pending_policy(
                conn, node_id=node_id, consumer=consumer, repo_identity=repo_identity, session_id=session_id
            )
        except FleetHubError:
            return None
    rows = conn.execute(
        f"SELECT {_PENDING_COLUMNS} FROM fleet_policy_pending WHERE session_id=? AND consumer=? AND "
        "IFNULL(repo_identity,'')=IFNULL(?, '')",
        (session_id, consumer, repo_identity),
    ).fetchall()
    if len(rows) != 1:
        return None
    return _pending_row(rows[0])


def _ack_row(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "receipt_key": row[0],
        "session_id": row[0],
        "external_session_id": row[1],
        "consumer": row[2],
        "owner_node": row[3],
        "repo_identity": row[4],
        "origin": row[5],
        "revision": row[6],
        "digest": row[7],
        "source": row[8],
        "effective": _load_pending_json(row[9], "effective", default={}),
        "sources": _load_pending_json(row[10], "sources", default={}),
        "selected": _load_pending_json(row[11], "selected", default=None) if row[11] else None,
        "overrides": _load_pending_json(row[12], "overrides", default={}),
        "override_reason": row[13],
        "context_hash": row[14],
        "loaded_at": row[15],
        "acknowledged_at": row[16],
    }


def get_acknowledged_policy(
    conn: sqlite3.Connection,
    *,
    receipt_key: str | None = None,
    node_id: str | None = None,
    consumer: str | None = None,
    repo_identity: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    if receipt_key is None:
        if node_id is None or consumer is None or session_id is None:
            raise FleetHubError("fleet policy acknowledged lookup requires a session identity")
        receipt_key = session_key(node_id, consumer, repo_identity, session_id)
    row = conn.execute(
        f"SELECT {_ACK_COLUMNS} FROM fleet_policy_acknowledged WHERE receipt_key=?", (receipt_key,)
    ).fetchone()
    if row is None:
        raise FleetHubError(f"fleet policy session {session_id or receipt_key!r} has no acknowledged receipt")
    return _ack_row(row)


def find_acknowledged_policy(
    conn: sqlite3.Connection,
    *,
    receipt_key: str | None = None,
    session_id: str | None = None,
    consumer: str | None = None,
    repo_identity: str | None = None,
    node_id: str | None = None,
) -> dict[str, Any] | None:
    try:
        if receipt_key is not None or (node_id is not None and consumer is not None and session_id is not None):
            return get_acknowledged_policy(
                conn,
                receipt_key=receipt_key,
                node_id=node_id,
                consumer=consumer,
                repo_identity=repo_identity,
                session_id=session_id,
            )
    except FleetHubError:
        return None
    if receipt_key is not None:
        rows = conn.execute(
            f"SELECT {_ACK_COLUMNS} FROM fleet_policy_acknowledged WHERE receipt_key=?",
            (receipt_key,),
        ).fetchall()
        if len(rows) == 1:
            return _ack_row(rows[0])
    if session_id is not None:
        rows = conn.execute(
            f"SELECT {_ACK_COLUMNS} FROM fleet_policy_acknowledged WHERE external_session_id=?",
            (session_id,),
        ).fetchall()
        if len(rows) == 1:
            return _ack_row(rows[0])
    return None


def acknowledged_policy_history(
    conn: sqlite3.Connection,
    *,
    receipt_key: str | None = None,
    session_id: str | None = None,
    node_id: str | None = None,
    consumer: str | None = None,
    repo_identity: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    if receipt_key is None and node_id is not None and consumer is not None and session_id is not None:
        receipt_key = session_key(node_id, consumer, repo_identity, session_id)
    if receipt_key is not None:
        rows = conn.execute(
            f"SELECT {_ACK_COLUMNS} FROM fleet_policy_acknowledged_history WHERE receipt_key=? ORDER BY history_id ASC",
            (receipt_key,),
        ).fetchall()
    elif session_id is not None:
        rows = conn.execute(
            f"SELECT {_ACK_COLUMNS} FROM fleet_policy_acknowledged_history WHERE external_session_id=? ORDER BY history_id ASC",
            (session_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_ACK_COLUMNS} FROM fleet_policy_acknowledged_history ORDER BY history_id ASC"
        ).fetchall()
    return [_ack_row(row) for row in rows]


def record_acknowledged_policy(
    conn: sqlite3.Connection,
    pending: Mapping[str, Any] | str,
    *,
    node_id: str,
    expected_context_hash: str,
    loaded_at: str | None = None,
) -> dict[str, Any]:
    """Persist an immutable acknowledged snapshot and append history after validation.

    Mandatory contract:
        record_acknowledged_policy(conn, pending, *, node_id: str, expected_context_hash: str, loaded_at=None)

    Acquires BEGIN IMMEDIATE before all lookup and validation. Validates current
    policy revision and digest, node ownership, pending identity (session_id,
    consumer, repo_identity), and expected_context_hash (sha256:64hex). If caller
    provides a snapshot mapping, compares its sealed fields against stored pending
    instead of silently upgrading to latest. Refuses if stored pending context_hash
    is absent.
    """
    ensure_schema(conn)
    if not isinstance(node_id, str) or not node_id:
        raise FleetHubError("fleet policy field 'node_id' is required")
    owner = _bounded(node_id, "node_id", fleet_policy.MAX_TEXT)
    assert owner is not None
    # fullmatch (not match): `$` still accepts a trailing newline.
    if not isinstance(expected_context_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_context_hash):
        raise FleetHubError("fleet policy field 'expected_context_hash' must be a sha256:64hex digest")

    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        if isinstance(pending, str):
            stored_pending = get_pending_policy(conn, pending_key=pending)
        elif isinstance(pending, Mapping):
            pending_key = pending.get("pending_key")
            session_id = pending.get("session_id")
            consumer = pending.get("consumer")
            owner_node = pending.get("owner_node") or pending.get("node_id")
            repo = pending.get("repo_identity")
            if pending_key is None:
                if session_id is None or consumer is None or owner_node is None:
                    raise FleetHubError("fleet policy pending lookup requires a session identity")
                pending_key = session_key(owner_node, consumer, repo, session_id)
            stored_pending = get_pending_policy(conn, pending_key=pending_key)

            sealed_fields = (
                "session_id",
                "consumer",
                "owner_node",
                "repo_identity",
                "origin",
                "revision",
                "digest",
                "context_hash",
                "effective",
                "sources",
                "selected",
                "instructions",
                "overrides",
                "override_reason",
            )
            for field in sealed_fields:
                if field in pending:
                    val = pending[field]
                    stored_val = stored_pending.get(field)
                    if field in ("repo_identity", "override_reason", "instructions"):
                        if (val or None) != (stored_val or None):
                            raise FleetHubError(
                                f"fleet policy pending field {field!r} does not match stored pending receipt"
                            )
                    elif field == "owner_node":
                        if val != stored_val:
                            raise FleetHubForbidden(
                                f"fleet policy session {stored_pending['session_id']!r} is owned by another node; refusing the write"
                            )
                    else:
                        if val != stored_val:
                            raise FleetHubError(
                                f"fleet policy pending field {field!r} does not match stored pending receipt"
                            )
        else:
            raise FleetHubError("fleet policy pending argument must be a pending key or mapping")

        if owner != stored_pending["owner_node"]:
            raise FleetHubForbidden(
                f"fleet policy session {stored_pending['session_id']!r} is owned by another node; refusing the write"
            )

        stored_hash = stored_pending.get("context_hash")
        if not stored_hash:
            raise FleetHubError("fleet policy pending receipt has no context_hash; refusing acknowledgement")
        if stored_hash != expected_context_hash:
            raise FleetHubError(
                f"fleet policy pending context hash {stored_hash!r} does not match expected {expected_context_hash!r}"
            )

        current = current_policy(conn)
        if stored_pending["revision"] != current["revision"] or stored_pending["digest"] != current["digest"]:
            raise FleetHubError(
                f"fleet policy refuses stale revision {stored_pending['revision']} "
                f"(current revision is {current['revision']})"
            )

        loaded = _aware_timestamp(loaded_at, "loaded_at")
        now = fleet_hub._utc_now()

        receipt_key = stored_pending["pending_key"]
        external_session_id = stored_pending["session_id"]
        consumer_val = stored_pending["consumer"]
        repo_val = stored_pending["repo_identity"]
        origin_val = stored_pending["origin"]
        revision_val = stored_pending["revision"]
        digest_val = stored_pending["digest"]
        source_val = stored_pending["source"]
        effective_val = stored_pending["effective"]
        sources_val = stored_pending["sources"]
        selected_val = stored_pending["selected"]
        overrides_val = stored_pending["overrides"] or {}
        override_reason_val = stored_pending["override_reason"]

        last_row = conn.execute(
            f"SELECT {_ACK_COLUMNS} FROM fleet_policy_acknowledged WHERE receipt_key=?",
            (receipt_key,),
        ).fetchone()

        is_identical = False
        if last_row is not None:
            last = _ack_row(last_row)
            if (
                last["revision"] == revision_val
                and last["digest"] == digest_val
                and last["effective"] == effective_val
                and last["sources"] == sources_val
                and last["selected"] == selected_val
                and last["overrides"] == overrides_val
                and last["override_reason"] == override_reason_val
                and last["context_hash"] == stored_hash
                and last["source"] == source_val
                and last["origin"] == origin_val
            ):
                is_identical = True

        if not is_identical:
            conn.execute(
                "INSERT INTO fleet_policy_acknowledged_history "
                "(receipt_key, external_session_id, consumer, owner_node, repo_identity, origin, revision, digest, "
                "source, effective, sources, selected, overrides, override_reason, context_hash, loaded_at, acknowledged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt_key,
                    external_session_id,
                    consumer_val,
                    owner,
                    repo_val,
                    origin_val,
                    revision_val,
                    digest_val,
                    source_val,
                    _dump_pending_json(effective_val, "effective"),
                    _dump_pending_json(sources_val, "sources"),
                    _dump_pending_json(selected_val, "selected") if selected_val is not None else None,
                    _dump_pending_json(overrides_val, "overrides"),
                    override_reason_val,
                    stored_hash,
                    loaded,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO fleet_policy_acknowledged "
                "(receipt_key, external_session_id, consumer, owner_node, repo_identity, origin, revision, digest, "
                "source, effective, sources, selected, overrides, override_reason, context_hash, loaded_at, acknowledged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(receipt_key) DO UPDATE SET "
                "external_session_id=excluded.external_session_id, consumer=excluded.consumer, "
                "owner_node=excluded.owner_node, repo_identity=excluded.repo_identity, origin=excluded.origin, "
                "revision=excluded.revision, digest=excluded.digest, source=excluded.source, "
                "effective=excluded.effective, sources=excluded.sources, selected=excluded.selected, "
                "overrides=excluded.overrides, override_reason=excluded.override_reason, "
                "context_hash=excluded.context_hash, loaded_at=excluded.loaded_at, "
                "acknowledged_at=excluded.acknowledged_at",
                (
                    receipt_key,
                    external_session_id,
                    consumer_val,
                    owner,
                    repo_val,
                    origin_val,
                    revision_val,
                    digest_val,
                    source_val,
                    _dump_pending_json(effective_val, "effective"),
                    _dump_pending_json(sources_val, "sources"),
                    _dump_pending_json(selected_val, "selected") if selected_val is not None else None,
                    _dump_pending_json(overrides_val, "overrides"),
                    override_reason_val,
                    stored_hash,
                    loaded,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO fleet_policy_sessions (session_id, consumer, owner_node, repo_identity, revision, digest, "
                "source, loaded_at, refresh_state, refresh_requested_at, refresh_updated_at, refresh_detail, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'none', NULL, NULL, NULL, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET consumer=excluded.consumer, repo_identity=excluded.repo_identity, "
                "revision=excluded.revision, digest=excluded.digest, source=excluded.source, "
                "loaded_at=excluded.loaded_at, refresh_state='none', refresh_requested_at=NULL, "
                "refresh_updated_at=NULL, refresh_detail=NULL, updated_at=excluded.updated_at",
                (
                    receipt_key,
                    consumer_val,
                    owner,
                    repo_val,
                    revision_val,
                    digest_val,
                    source_val,
                    loaded,
                    now,
                ),
            )
        # Identical ack is idempotent: do not rewrite sessions or reset
        # observed requested/failed runtime refresh state. A fresh generation
        # or restart is an explicit new apply, not a duplicate acknowledgement.

        if external_session_id != receipt_key:
            conn.execute(
                "DELETE FROM fleet_policy_sessions WHERE session_id=? AND consumer=? AND owner_node=? "
                "AND IFNULL(repo_identity, '') = IFNULL(?, '')",
                (external_session_id, consumer_val, owner, repo_val),
            )

        payload = get_acknowledged_policy(conn, receipt_key=receipt_key)
        if opened:
            conn.commit()
        return payload
    except BaseException:
        if opened:
            conn.rollback()
        raise


def find_loaded_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    consumer: str,
    repo_identity: str | None,
    node_id: str | None = None,
) -> dict[str, Any] | None:
    norm_repo = repo_identity or ""
    all_rows = list_session_states(conn)
    if node_id is not None:
        key = session_key(node_id, consumer, repo_identity, session_id)
        candidates = [
            row
            for row in all_rows
            if row["consumer"] == consumer
            and (row.get("repo_identity") or "") == norm_repo
            and row.get("owner_node") == node_id
        ]
        matches = [
            row
            for row in candidates
            if row.get("receipt_key") in {key, session_id}
            or row.get("session_id") in {key, session_id}
            or row.get("external_session_id") == session_id
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    candidates = [
        row for row in all_rows if row["consumer"] == consumer and (row.get("repo_identity") or "") == norm_repo
    ]
    matches = [
        row
        for row in candidates
        if (
            row.get("receipt_key") == session_id
            or row.get("external_session_id") == session_id
            or row.get("session_id") == session_id
            or (
                row.get("owner_node")
                and row.get("receipt_key") == session_key(row["owner_node"], consumer, repo_identity, session_id)
            )
            or (
                row.get("owner_node")
                and row.get("session_id") == session_key(row["owner_node"], consumer, repo_identity, session_id)
            )
        )
    ]
    if len(matches) != 1:
        return None
    return matches[0]
