"""Fleet hub: central run-event collector and claim arbiter on the tailnet
(issue #1123 phase 2, issue #1125 phase 4).

A small stdlib-only HTTP service (``http.server.ThreadingHTTPServer`` +
``sqlite3``) that accepts ``POST /events`` from fleet nodes and answers
``GET /status`` with the latest observed state per (node_id, run_id).

Credentials (issue #1150). Two kinds of bearer token, both compared in
constant time, neither ever logged:

- The **admin token** (``BRIGADE_FLEET_TOKEN`` / ``--token-file``, the one
  shared secret from before per-node credentials) is the control plane: it
  manages the ``nodes`` table (``/nodes``), reads ``/status`` and
  ``/claims``, and enrols the dashboard cookie. It may ``POST /events`` and
  ``POST /claims`` under *any* ``node_id`` only when the hub runs with
  ``--allow-admin-writes`` (off by default, so a fleet still on the shared
  token migrates explicitly); otherwise those posts answer 403.
- A **node token** (``brigade fleet nodes add <node_id>``; only its SHA-256
  is stored) *is* the node's identity: ``POST /events`` and ``POST /claims``
  derive the caller's ``node_id`` from it and answer 403 when a body
  ``node_id`` differs, so one fleet member can no longer post events or
  claim operations as another. A revoked token answers 401. Node tokens
  may also read ``/status`` and ``/claims``.

Endpoints:
- ``GET /health`` — no auth; liveness probe.
- ``POST /events`` — node token (or admin with ``--allow-admin-writes``);
  body is a single event object or a JSON array of events, every one
  carrying the caller's ``node_id``. Dedupe on UNIQUE(node_id, run_id,
  sequence, digest). Responds ``{"accepted": n, "duplicate": m}``.
- ``GET /status`` — admin or node token; latest state per (node_id, run_id)
  for non-terminal runs. ``?all=1`` includes terminal runs.
- ``GET /nodes`` / ``POST /nodes`` — admin token only; list enrolled nodes,
  ``{"action": "add", "node_id", "label"?}`` mints a token (returned once,
  in that response only; an enrolled, unrevoked node answers 409),
  ``{"action": "revoke", "node_id"}`` marks it revoked (re-add to rotate).
- ``POST /claims`` — node token (or admin with ``--allow-admin-writes``);
  ``{"action": "acquire"|"renew"|"release",
  "target": ..., "node_id": ..., "holder": ..., "conductor"?: ...,
  "ttl_seconds"?: ...}``. One unique row per target grants the claim to
  exactly one holder; a held target answers 409 with the current owner. The
  ``holder`` value is a per-acquisition fencing token: renew and release
  require it to match the stored row, so a sibling process sharing the same
  node identity can never extend or delete another holder's live claim (the
  token is never echoed back to other callers). A claim past its TTL is
  reclaimable by anyone; an unexpired one is never silently stolen. Expired
  rows are pruned on every acquire. ``node_id`` must be a real identity
  (``[A-Za-z0-9._-]``, max 128 chars, never the literal ``unknown``).
  An optional ``"scope"`` widens who a request may act on (issue #1141):
  ``"holder"`` (default) is the fencing-token contract above;
  ``"node"`` on ``acquire`` also supersedes an unexpired claim held by the
  *same* ``node_id`` — but only the exact row whose recorded local lease
  (``lock``: the acquiring run's ``run.lock`` owner token, its
  ``acquired_at``, ``run_dir``, stored at acquire) matches the
  ``supersede`` lease the caller presents, i.e. the dead lock its local
  reconcile just released, and whose lease is not newer than that one; on
  ``release`` it deletes the row owned by the caller's ``node_id`` without
  its token (which died with the run); ``"force"`` on ``release`` deletes
  whatever holds the target. ``holder`` is optional for a ``node``/``force``
  release; ``renew`` is always ``holder``-scoped. A row acquired without a
  ``lock`` lease can never be superseded, only released or expired.
  ``"inspect"`` (no holder needed) answers the current row for a target;
  when the caller's ``node_id`` owns it the answer also carries
  ``lock_run_dir`` so that node's CLI can find the run and check its lock
  is dead before a token-less release (never to other callers).
  Each live row carries a monotonic ``generation``. Release writes a
  per-(target, holder, node) tombstone for that generation and raises a
  durable per-target generation floor (schema v5) so a late acquire or
  renew whose generation, holder, or lease stamp is stale is refused with
  ``reason: "stale-generation"`` instead of resurrecting the row (issue
  #1189). The floor outlives claim TTL. Holder tombstones and live claims
  share a durable fence budget; a new no-row fence that would exceed the
  per-(target, node) or global cap is refused instead of evicting an
  existing fence a delayed initial acquire could still match. Schema v4
  databases migrate forward to v5.
  Trust boundary: ``node_id`` is bound to the node token that presents it,
  so a member can only act as itself; ``lock``, ``supersede`` and ``scope``
  remain caller-asserted intent that keeps *honest* clients from stealing
  each other's claims (a same-basename workspace, a cloned node identity),
  not an authorization — any enrolled node may still ``force``-release,
  attributed to its own ``node_id``.
- ``GET /claims`` — admin or node token; active claims (``?all=1`` includes
  expired).
- ``GET /`` and ``GET /view/{machines,repos}`` — the server-rendered Fleet
  dashboard (issue #1124 phase 3; see ``fleet_dashboard``). Same bearer auth,
  or the ``brigade_fleet_view`` cookie: opening the page once with
  ``?token=<fleet token>`` from a phone sets an HttpOnly, SameSite=Strict
  cookie and 303-redirects to the same URL without the token. The cookie
  value is an HMAC of the token, never the token: it grants read-only
  dashboard access only (never ``/status``, ``/claims``, or ``/events``),
  and rotating the hub token invalidates every cookie. Tradeoff: the token
  transits once in a URL (browser history on that device; the hub logs
  nothing) and the cookie is a 30-day read-only capability on that device,
  which is why it is scoped to the HTML routes only.

The admin token comes from ``BRIGADE_FLEET_TOKEN`` or ``--token-file``; it
is never persisted by Brigade, and node tokens are persisted only as SHA-256
digests. The database is one SQLite file in WAL mode with a versioned schema
(PRAGMA user_version).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from . import fleet_dashboard

SCHEMA_VERSION = 5
DEFAULT_PORT = 3774
MAX_BODY_BYTES = 8 * 1024 * 1024
# No-row release fences kept per (target, node). Overflow fails closed
# rather than evicting a fence a delayed generation-less acquire can match.
# Real releases may exceed this per-node cap; the global durable budget
# still applies. The generation floor is stored separately.
CLAIM_TOMBSTONE_LIMIT_PER_TARGET_NODE = 16
CLAIM_DURABLE_FENCE_LIMIT = 1024
CLAIM_TARGET_MAX_CHARS = 256

# Dashboard cookie (issue #1124): a derived, read-only capability, not the token.
DASHBOARD_COOKIE = "brigade_fleet_view"
DASHBOARD_COOKIE_MAX_AGE = 30 * 86400
_DASHBOARD_COOKIE_PURPOSE = b"brigade-fleet-dashboard-cookie-v1"
_DASHBOARD_PREFIX = "/view/"

CLAIM_ACTIONS = frozenset({"acquire", "renew", "release", "inspect"})
# Request scopes (issue #1141): "holder" is the fencing-token contract; "node"
# lets a node act on its own dead holder's row; "force" is the operator
# override, release only. Renew is never widened: a token-less renew would
# let any same-node process keep a claim alive.
CLAIM_SCOPES = frozenset({"holder", "node", "force"})
CLAIM_ACTION_SCOPES: dict[str, frozenset[str]] = {
    "acquire": frozenset({"holder", "node"}),
    "renew": frozenset({"holder"}),
    "release": CLAIM_SCOPES,
    "inspect": frozenset({"holder"}),
}
DEFAULT_CLAIM_TTL_SECONDS = 900
CLAIM_TTL_MIN_SECONDS = 1
CLAIM_TTL_MAX_SECONDS = 86400
# Two cloned or identity-less machines must never both be granted a target:
# a claim identity has to be a real, well-formed node id, never "unknown".
CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Per-node credentials (issue #1150): a fresh token is 32 random bytes,
# url-safe; the hub keeps only its SHA-256 hex digest.
NODE_TOKEN_BYTES = 32
NODE_LABEL_MAX_CHARS = 128
NODE_ACTIONS = frozenset({"add", "revoke"})

EVENT_FIELDS = {
    "node_id": str,
    "run_id": str,
    "state": str,
    "ts": str,
    "sequence": int,
    "digest": str,
}
OPTIONAL_STR_FIELDS = ("repo", "seat", "harness")
OPTIONAL_EVENT_STR_FIELDS = ("capability_fingerprint",)
OPTIONAL_EVENT_INT_FIELDS = ("exit_status",)

# Terminal run_event.v1 lifecycle types (see run_events.EVENT_TYPES), plus
# verify/external completions so finished sessions leave the live status view.
TERMINAL_STATES = frozenset(
    {
        "run.completed",
        "run.failed",
        "run.interrupted",
        "verify.completed",
        "external.completed",
        "external.failed",
        "external.canceled",
    }
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    node_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    digest TEXT NOT NULL,
    repo TEXT,
    seat TEXT,
    harness TEXT,
    state TEXT NOT NULL,
    ts TEXT NOT NULL,
    exit_status INTEGER,
    capability_fingerprint TEXT,
    received_at TEXT NOT NULL,
    PRIMARY KEY (node_id, run_id, sequence, digest)
);
"""

# Server-arbitrated repo claims (schema v2, lease columns v3): one row per
# target grants the target to exactly one holder. holder_token is the
# per-acquisition fencing token (renew/release must present it; it is never
# sent to other callers). lock_token / lock_acquired_at / lock_run_dir record
# the acquiring run's local run.lock lease (issue #1141): a same-node
# supersede must present that exact lease, so a dead lock in one directory
# can never replace a claim taken by a run in another. They are never
# serialized either. expires_at is Unix epoch seconds so expiry comparisons
# are numeric, not string-format bound.
_CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    target TEXT NOT NULL PRIMARY KEY,
    owner_node TEXT NOT NULL,
    owner_conductor TEXT,
    harness TEXT,
    role TEXT,
    job TEXT,
    session TEXT,
    holder_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    lock_token TEXT,
    lock_acquired_at TEXT,
    lock_run_dir TEXT,
    generation INTEGER NOT NULL DEFAULT 1
);
"""
# Release tombstones (issue #1189 / schema v5): survive the claim DELETE so
# a late acquire/renew cannot recreate the row. One row per released holder
# on a node. Not expired with claim TTL; new rows fail closed at the cap.
_TOMBSTONES_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_tombstones (
    target TEXT NOT NULL,
    holder_token TEXT NOT NULL,
    owner_node TEXT NOT NULL,
    generation INTEGER NOT NULL,
    lease_stamp TEXT,
    released_at TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (target, holder_token, owner_node)
);
"""
# Durable generation floor per target (schema v5). Survives claim TTL and
# tombstone eviction so generation 1 cannot be reused after a release.
_GENERATION_FLOORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_generation_floors (
    target TEXT NOT NULL PRIMARY KEY,
    generation INTEGER NOT NULL
);
"""
_CLAIMS_ADDITIVE_COLUMNS = (
    "harness",
    "role",
    "job",
    "session",
    "lock_token",
    "lock_acquired_at",
    "lock_run_dir",
)
_EVENTS_ADDITIVE_COLUMNS = ("exit_status", "capability_fingerprint")

# Per-node credentials (schema v4, issue #1150): node_id -> SHA-256 of the
# node's bearer token. The plaintext token is returned once by the add
# request and never stored; a revoked row keeps its digest so the old token
# answers "revoked" (401) rather than silently becoming unknown.
_NODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT NOT NULL PRIMARY KEY,
    token_sha256 TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
"""
# Bounded so a hostile client cannot bloat the row; a lease token is a uuid4
# hex and an ISO-8601 stamp, a run_dir a filesystem path.
LEASE_FIELD_MAX_CHARS = 1024
OPAQUE_LABEL_MAX_CHARS = 128
OPAQUE_LABEL_FIELDS = ("harness", "role", "job", "session")


class FleetHubError(RuntimeError):
    """Hub configuration or request failure with an operator-readable message."""


class FleetHubForbidden(FleetHubError):
    """An authenticated caller asked to act as a node_id its token does not own (HTTP 403)."""


class FleetHubConflict(FleetHubError):
    """The request conflicts with current hub state (HTTP 409)."""


class FleetHubUnprocessable(FleetHubError):
    """A field failed semantic validation (HTTP 422), e.g. control characters."""


def _contains_controls(value: str) -> bool:
    """True when ``value`` contains a C0 or C1 control character (including DEL)."""
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value)


def _reject_controls(value: str, field: str, *, kind: str) -> None:
    if _contains_controls(value):
        raise FleetHubUnprocessable(f"{kind} field {field!r} must not contain control characters")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claims_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()}


def _claims_table_needs_migration(claims_columns: set[str]) -> bool:
    """True when the claims table is absent, pre-holder-token, or missing a
    lease column — i.e. any state where DDL beyond IF NOT EXISTS may run."""
    if not claims_columns:
        return True
    if "holder_token" not in claims_columns:
        return True
    if "generation" not in claims_columns:
        return True
    return any(column not in claims_columns for column in _CLAIMS_ADDITIVE_COLUMNS)


def _events_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}


# Intra-process serialization for the claims migration, in addition to the
# SQL-level BEGIN IMMEDIATE. Schema work happens once at server startup
# (``make_server`` → ``init_db``), so within one process this is not
# per-request contention anymore, but ``init_db`` stays lock-guarded so a
# first-touch storm of concurrent callers (other processes, or tests) is
# same-process thread contention resolved deterministically instead of via
# busy-timeout races.
_MIGRATION_LOCKS: dict[str, threading.Lock] = {}
_MIGRATION_LOCKS_GUARD = threading.Lock()


def _migration_lock(db_path: Path) -> threading.Lock:
    with _MIGRATION_LOCKS_GUARD:
        return _MIGRATION_LOCKS.setdefault(str(db_path), threading.Lock())


# Backoff for the migration write lock. Each retry first re-checks whether
# another connection already finished the job.
_MIGRATION_LOCK_DELAYS: tuple[float | None, ...] = (0.05, 0.1, 0.2, 0.4, 0.8, None)


def _migrate_claims_table(conn: sqlite3.Connection) -> None:
    """Create/upgrade the claims table under one write lock.

    Serialized twice: same-process callers queue on ``_migration_lock`` (a
    first-touch storm on a threading server is thread contention), and the
    DDL itself runs under BEGIN IMMEDIATE with a bounded locked-database
    backoff so a separate process cannot race it either. A caller that
    loses either race treats "someone else migrated" as success. The
    steady state (nothing to migrate) never calls this, so ordinary
    requests take no write lock.
    """
    for delay in _MIGRATION_LOCK_DELAYS:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if not _claims_table_needs_migration(_claims_columns(conn)):
                return
            if delay is None:
                raise
            time.sleep(delay)
            continue
        break
    try:
        claims_columns = _claims_columns(conn)
        # Claims are ephemeral TTL state: a pre-holder-token claims table
        # (early v2 builds) is dropped and recreated rather than migrated.
        if claims_columns and "holder_token" not in claims_columns:
            conn.execute("DROP TABLE claims")
            claims_columns = set()
        conn.execute(_CLAIMS_SCHEMA)
        # v2 -> v3: the lease columns are nullable, so live claims survive
        # the upgrade (they can never be superseded, only released/expired).
        if claims_columns:
            for column in _CLAIMS_ADDITIVE_COLUMNS:
                if column in claims_columns:
                    continue
                try:
                    conn.execute(f"ALTER TABLE claims ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError as exc:
                    # A column another connection added is the outcome we
                    # wanted, not an error.
                    if "duplicate column" not in str(exc).lower():
                        raise
            if "generation" not in claims_columns:
                try:
                    conn.execute("ALTER TABLE claims ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        conn.execute(_TOMBSTONES_SCHEMA)
        conn.execute(_GENERATION_FLOORS_SCHEMA)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _refuse_newer_schema(conn: sqlite3.Connection, db_path: Path) -> None:
    """Raise ``FleetHubError`` when ``conn``'s database is from a newer hub."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if not isinstance(current, int) or current > SCHEMA_VERSION:
        raise FleetHubError(
            f"fleet hub database {db_path} has schema version {current}; this brigade supports {SCHEMA_VERSION}"
        )


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open a read/write connection to an **existing** hub database without
    touching its schema or persistence mode.

    This is what request handlers use (#1161): schema creation and migration
    happen exactly once at server startup (``init_db``), so a request can
    never stall on DDL behind another caller's write lock, but every request
    still opens a fresh connection, so ``lookup_node_token`` sees enrollments
    and revocations the moment they commit. The open itself is side-effect
    free: URI ``mode=rw`` never creates a missing database file, no
    ``PRAGMA journal_mode`` is issued (WAL is set up once by startup's
    ``init_db``, not per connection), and any setup or version error,
    including a database written by a newer hub, closes the connection
    before raising rather than leaking it.
    """
    db_path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=rw", timeout=10, uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        _refuse_newer_schema(conn, db_path)
    except BaseException:
        conn.close()
        raise
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open the hub database and create/migrate its schema.

    Called once at server startup (``make_server``, before the socket serves
    any request) and by tools and tests that want a migrated connection;
    request handlers use the non-migrating ``open_db`` instead. This is also
    the only place WAL mode is set (#1161): it persists in the database file,
    so request connections inherit it without re-issuing the pragma.
    """
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = db_path.resolve()
    conn = sqlite3.connect(str(resolved), timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _refuse_newer_schema(conn, resolved)
        conn.execute(_SCHEMA)
        for column in _EVENTS_ADDITIVE_COLUMNS:
            if column not in _events_columns(conn):
                conn.execute(
                    f"ALTER TABLE events ADD COLUMN {column} {'INTEGER' if column == 'exit_status' else 'TEXT'}"
                )
        if _claims_table_needs_migration(_claims_columns(conn)):
            with _migration_lock(db_path):
                # Re-check under the lock: the thread that held it first has
                # usually done the work already.
                if _claims_table_needs_migration(_claims_columns(conn)):
                    _migrate_claims_table(conn)
        conn.execute(_TOMBSTONES_SCHEMA)
        conn.execute(_GENERATION_FLOORS_SCHEMA)
        _backfill_generation_floors(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS events_run_seq ON events (node_id, run_id, sequence)")
        # v3 -> v4 is one additive table; IF NOT EXISTS is the whole migration.
        # v4 -> v5 adds generation floors (and tombstones if a v4-on-this-branch
        # hub already created them) without dropping live claims.
        conn.execute(_NODES_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def _validate_event(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("event must be a JSON object")
    event: dict[str, Any] = {}
    for field, kind in EVENT_FIELDS.items():
        value = raw.get(field)
        if kind is str:
            if not isinstance(value, str) or not value.strip():
                raise FleetHubError(f"event field {field!r} must be a non-empty string")
            _reject_controls(value, field, kind="event")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise FleetHubError(f"event field {field!r} must be a non-negative integer")
        event[field] = value
    for field in OPTIONAL_STR_FIELDS:
        value = raw.get(field)
        if isinstance(value, str):
            _reject_controls(value, field, kind="event")
        event[field] = value if isinstance(value, str) and value.strip() else None
    for field in OPTIONAL_EVENT_STR_FIELDS:
        value = raw.get(field)
        if isinstance(value, str):
            _reject_controls(value, field, kind="event")
        event[field] = value if isinstance(value, str) and value.strip() else None
    for field in OPTIONAL_EVENT_INT_FIELDS:
        value = raw.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise FleetHubError(f"event field {field!r} must be a non-negative integer")
        event[field] = value
    return event


def store_events(conn: sqlite3.Connection, raw_events: Any, *, caller_node: str | None = None) -> dict[str, int]:
    """Insert events with dedupe; returns {"accepted": n, "duplicate": m}.

    With ``caller_node`` (the node_id bound to the caller's node token) every
    event must carry that node_id; one that does not rejects the whole batch
    with ``FleetHubForbidden`` (nothing is stored). ``None`` is the admin
    token with ``--allow-admin-writes``: any node_id.
    """
    if isinstance(raw_events, dict):
        raw_list = [raw_events]
    elif isinstance(raw_events, list):
        raw_list = raw_events
    else:
        raise FleetHubError("body must be an event object or array of events")
    if len(raw_list) > 1000:
        raise FleetHubError("too many events in one POST (max 1000)")
    events = [_validate_event(raw) for raw in raw_list]
    if caller_node is not None:
        for event in events:
            if event["node_id"] != caller_node:
                raise FleetHubForbidden(
                    f"event node_id {event['node_id']!r} does not match the caller's node token ({caller_node})"
                )
    accepted = 0
    duplicate = 0
    received_at = _utc_now()
    for event in events:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO events "
            "(node_id, run_id, sequence, digest, repo, seat, harness, state, ts, exit_status, "
            "capability_fingerprint, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["node_id"],
                event["run_id"],
                event["sequence"],
                event["digest"],
                event["repo"],
                event["seat"],
                event["harness"],
                event["state"],
                event["ts"],
                event["exit_status"],
                event["capability_fingerprint"],
                received_at,
            ),
        )
        if cursor.rowcount == 0:
            duplicate += 1
        else:
            accepted += 1
    conn.commit()
    return {"accepted": accepted, "duplicate": duplicate}


def latest_status(conn: sqlite3.Connection, *, include_all: bool = False) -> list[dict[str, Any]]:
    """Latest event per (node_id, run_id); non-terminal runs unless include_all.

    Exactly one row per (node_id, run_id): ties on sequence (same sequence
    seen with two digests) resolve to the most recently received, then the
    larger digest, so the view never shows a run twice.
    """
    rows = conn.execute(
        "SELECT node_id, run_id, repo, seat, harness, state, ts, sequence, digest, exit_status, capability_fingerprint FROM ("
        "  SELECT e.*, ROW_NUMBER() OVER ("
        "    PARTITION BY node_id, run_id ORDER BY sequence DESC, received_at DESC, digest DESC"
        "  ) AS rn FROM events e"
        ") WHERE rn = 1 ORDER BY node_id, run_id"
    ).fetchall()
    result = []
    for row in rows:
        if not include_all and row[5] in TERMINAL_STATES:
            continue
        result.append(
            {
                "node_id": row[0],
                "run_id": row[1],
                "repo": row[2],
                "seat": row[3],
                "harness": row[4],
                "state": row[5],
                "ts": row[6],
                "sequence": row[7],
                "digest": row[8],
                "exit_status": row[9],
                "capability_fingerprint": row[10],
            }
        )
    return result


def run_started_at(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Timestamp of the first observed event per (node_id, run_id), for elapsed."""
    rows = conn.execute(
        "SELECT node_id, run_id, ts FROM ("
        "  SELECT node_id, run_id, ts, ROW_NUMBER() OVER ("
        "    PARTITION BY node_id, run_id ORDER BY sequence ASC, received_at ASC, digest ASC"
        "  ) AS rn FROM events"
        ") WHERE rn = 1"
    ).fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def node_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every observed node with when the hub last heard from it and its event count."""
    rows = conn.execute(
        "SELECT node_id, MAX(received_at), COUNT(*) FROM events GROUP BY node_id ORDER BY node_id"
    ).fetchall()
    return [{"node_id": row[0], "last_received_at": row[1], "events": row[2]} for row in rows]


def dashboard_cookie_value(token: str) -> str:
    """Cookie value derived from the hub token; never the token itself."""
    return hmac.new(token.encode("utf-8"), _DASHBOARD_COOKIE_PURPOSE, hashlib.sha256).hexdigest()


# --- claims (issue #1125, phase 4) ------------------------------------------


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _validate_claim_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("claim request must be a JSON object")
    request: dict[str, Any] = {}
    action = raw.get("action")
    if not isinstance(action, str) or action not in CLAIM_ACTIONS:
        raise FleetHubError("claim field 'action' must be one of: acquire, renew, release, inspect")
    request["action"] = action
    scope = raw.get("scope", "holder")
    if not isinstance(scope, str) or scope not in CLAIM_ACTION_SCOPES[action]:
        allowed = ", ".join(sorted(CLAIM_ACTION_SCOPES[action]))
        raise FleetHubError(f"claim field 'scope' for {action} must be one of: {allowed}")
    request["scope"] = scope
    # A node/force release has no holder token to present: the token died
    # with the crashed run it is cleaning up after.
    holder_optional = action == "inspect" or (action == "release" and scope != "holder")
    for field in ("target", "node_id", "holder"):
        value = raw.get(field)
        if field == "holder" and holder_optional and value is None:
            request[field] = None
            continue
        if not isinstance(value, str) or not value.strip():
            raise FleetHubError(f"claim field {field!r} must be a non-empty string")
        if field == "target":
            _reject_controls(value, field, kind="claim")
            if len(value.strip()) > CLAIM_TARGET_MAX_CHARS:
                raise FleetHubError(
                    f"claim field 'target' must be at most {CLAIM_TARGET_MAX_CHARS} characters"
                )
        request[field] = value.strip()
    for field in ("node_id", "holder"):
        if request[field] is None:
            continue
        if request[field] == "unknown" or not CLAIM_ID_PATTERN.match(request[field]):
            raise FleetHubError(
                f"claim field {field!r} must be a real identity "
                "([A-Za-z0-9._-], max 128 chars, never the literal 'unknown')"
            )
    conductor = raw.get("conductor")
    if isinstance(conductor, str):
        _reject_controls(conductor, "conductor", kind="claim")
    request["conductor"] = conductor.strip() if isinstance(conductor, str) and conductor.strip() else None
    labels: dict[str, str | None] = {}
    for field in OPAQUE_LABEL_FIELDS:
        value = raw.get(field)
        if value is not None and action != "acquire":
            raise FleetHubError(f"claim field {field!r} is only valid for acquire")
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > OPAQUE_LABEL_MAX_CHARS
        ):
            raise FleetHubError(
                f"claim field {field!r} must be a non-empty string at most {OPAQUE_LABEL_MAX_CHARS} characters"
            )
        if isinstance(value, str):
            _reject_controls(value, field, kind="claim")
        labels[field] = value.strip() if isinstance(value, str) and value.strip() else None
    request["labels"] = labels
    ttl = raw.get("ttl_seconds", DEFAULT_CLAIM_TTL_SECONDS)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not CLAIM_TTL_MIN_SECONDS <= ttl <= CLAIM_TTL_MAX_SECONDS:
        raise FleetHubError(
            f"claim field 'ttl_seconds' must be an integer in [{CLAIM_TTL_MIN_SECONDS}, {CLAIM_TTL_MAX_SECONDS}]"
        )
    request["ttl_seconds"] = ttl
    acquired_at = raw.get("acquired_at")
    if acquired_at is not None:
        if action != "release":
            raise FleetHubError("claim field 'acquired_at' is only valid for release")
        if not isinstance(acquired_at, str) or not acquired_at.strip() or len(acquired_at) > LEASE_FIELD_MAX_CHARS:
            raise FleetHubError("claim field 'acquired_at' must be a non-empty string")
    request["acquired_at"] = acquired_at.strip() if isinstance(acquired_at, str) else None
    if action == "release" and scope == "node" and request["acquired_at"] is None:
        # A token-less node-scoped delete without its fencing value would
        # match whatever row currently holds the target — including one a
        # fresh run acquired after the caller looked. Only "force" may
        # delete unfenced.
        raise FleetHubError("claim field 'acquired_at' is required for a token-less node-scoped release")
    generation = raw.get("generation")
    if generation is not None:
        if action not in ("acquire", "renew"):
            raise FleetHubError("claim field 'generation' is only valid for acquire or renew")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise FleetHubError("claim field 'generation' must be a positive integer")
    request["generation"] = generation
    lock = raw.get("lock")
    if lock is not None and action not in ("acquire", "renew"):
        raise FleetHubError("claim field 'lock' is only valid for acquire or renew")
    request["lock"] = _validate_lease(lock, "lock") if lock is not None else None
    supersede = raw.get("supersede")
    if action == "acquire" and scope == "node":
        if supersede is None:
            raise FleetHubError(
                "claim field 'supersede' (the reconciled dead lease) is required for a node-scoped acquire"
            )
        request["supersede"] = _validate_lease(supersede, "supersede")
    elif supersede is not None:
        raise FleetHubError("claim field 'supersede' is only valid for a node-scoped acquire")
    else:
        request["supersede"] = None
    return request


def _validate_lease(raw: Any, field: str) -> dict[str, str | None]:
    """A local run.lock lease as sent by clients: ``{"token", "acquired_at"?, "run_dir"?}``."""
    if not isinstance(raw, dict):
        raise FleetHubError(f"claim field {field!r} must be a JSON object")
    token = raw.get("token")
    if not isinstance(token, str) or not token.strip() or len(token) > LEASE_FIELD_MAX_CHARS:
        raise FleetHubError(f"claim field {field!r} must carry a non-empty 'token'")
    lease: dict[str, str | None] = {"token": token.strip(), "acquired_at": None, "run_dir": None}
    for key in ("acquired_at", "run_dir"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > LEASE_FIELD_MAX_CHARS:
            raise FleetHubError(f"claim field {field!r}.{key} must be a string")
        lease[key] = value.strip() or None
    return lease


def _backfill_generation_floors(conn: sqlite3.Connection) -> None:
    """Raise each target's durable floor to the highest *released* generation.

    Live claims are not a floor. A lost-row heartbeat must be able to
    re-acquire the same generation; only a release (tombstone) retires it.
    """
    conn.execute(
        "INSERT INTO claim_generation_floors (target, generation) "
        "SELECT target, MAX(generation) FROM claim_tombstones GROUP BY target "
        "ON CONFLICT(target) DO UPDATE SET "
        "generation = MAX(claim_generation_floors.generation, excluded.generation)"
    )


def _generation_floor(conn: sqlite3.Connection, target: str) -> int:
    row = conn.execute("SELECT generation FROM claim_generation_floors WHERE target = ?", (target,)).fetchone()
    return _as_generation(row[0]) if row is not None else 0


def _raise_generation_floor(conn: sqlite3.Connection, target: str, generation: int) -> None:
    conn.execute(
        "INSERT INTO claim_generation_floors (target, generation) VALUES (?, ?) "
        "ON CONFLICT(target) DO UPDATE SET "
        "generation = MAX(claim_generation_floors.generation, excluded.generation)",
        (target, generation),
    )


def _durable_fence_count(conn: sqlite3.Connection) -> int:
    tombstones = conn.execute("SELECT COUNT(*) FROM claim_tombstones").fetchone()[0]
    claims = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    return int(tombstones) + int(claims)


def _tombstone_row_exists(conn: sqlite3.Connection, target: str, holder: str, node: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM claim_tombstones WHERE target = ? AND holder_token = ? AND owner_node = ?",
            (target, holder, node),
        ).fetchone()
        is not None
    )


def _target_node_tombstone_count(conn: sqlite3.Connection, target: str, node: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM claim_tombstones WHERE target = ? AND owner_node = ?",
            (target, node),
        ).fetchone()[0]
    )


def _as_generation(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return 1


def _claim_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "target": row[0],
        "owner_node": row[1],
        "owner_conductor": row[2],
        "harness": row[3],
        "role": row[4],
        "job": row[5],
        "session": row[6],
        "acquired_at": row[7],
        "renewed_at": row[8],
        "ttl_seconds": row[9],
        "expires_at": _epoch_to_iso(row[10]),
        "generation": _as_generation(row[11] if len(row) > 11 else None),
    }


# Payload columns only: holder_token is a fencing capability and is never
# serialized to callers (it would let anyone forge a renew/release).
_CLAIM_COLUMNS = (
    "target, owner_node, owner_conductor, harness, role, job, session, "
    "acquired_at, renewed_at, ttl_seconds, expires_at, generation"
)


def _fetch_claim(conn: sqlite3.Connection, target: str) -> tuple[Any, ...] | None:
    """Row as (_CLAIM_COLUMNS..., holder_token, lock_token, lock_acquired_at,
    lock_run_dir); generation is index 11, the fencing token is index 12,
    the lease token index 13."""
    return conn.execute(
        f"SELECT {_CLAIM_COLUMNS}, holder_token, lock_token, lock_acquired_at, lock_run_dir "
        "FROM claims WHERE target = ?",
        (target,),
    ).fetchone()


def _fetch_tombstones(conn: sqlite3.Connection, target: str) -> list[tuple[Any, ...]]:
    """Rows as (target, generation, holder_token, owner_node, lease_stamp,
    released_at, expires_at)."""
    return list(
        conn.execute(
            "SELECT target, generation, holder_token, owner_node, lease_stamp, released_at, expires_at "
            "FROM claim_tombstones WHERE target = ?",
            (target,),
        ).fetchall()
    )


def _stale_generation_payload(action: str, target: str) -> dict[str, Any]:
    key = "granted" if action == "acquire" else "renewed"
    return {
        key: False,
        "error": f"claim generation on {target!r} is stale",
        "reason": "stale-generation",
        "owner": None,
    }


def _storage_full_payload(action: str, target: str) -> dict[str, Any]:
    key = "granted" if action == "acquire" else "released"
    return {
        key: False,
        "error": f"fleet hub durable claim fence limit reached for {target!r}",
        "reason": "storage-full",
        "owner": None,
    }


def _request_lease_stamp(request: dict[str, Any]) -> str | None:
    lock = request.get("lock")
    if not isinstance(lock, dict):
        return None
    stamp = lock.get("acquired_at")
    return stamp.strip() if isinstance(stamp, str) and stamp.strip() else None


def _is_stale_against_tombstones(
    *,
    node: str,
    holder: str | None,
    generation: int | None,
    lease_stamp: str | None,
    prior: tuple[Any, ...] | None,
    tombstones: list[tuple[Any, ...]],
    floor: int,
    now: float,
) -> bool:
    live_same_holder = (
        prior is not None and prior[10] > now and holder is not None and prior[12] == holder and prior[1] == node
    )
    if live_same_holder:
        return generation is not None and generation < _as_generation(prior[11])
    if generation is not None and generation <= floor:
        return True
    if holder is not None and any(row[2] == holder and row[3] == node for row in tombstones):
        return True
    if lease_stamp and any(row[3] == node and row[4] == lease_stamp for row in tombstones):
        return True
    return False


def _next_claim_generation(
    prior: tuple[Any, ...] | None, tombstones: list[tuple[Any, ...]], floor: int, now: float
) -> int:
    next_floor = floor
    if tombstones:
        next_floor = max(next_floor, max(_as_generation(row[1]) for row in tombstones))
    if prior is not None and prior[10] > now:
        next_floor = max(next_floor, _as_generation(prior[11]))
    return next_floor + 1


def _row_lease_stamp(row: tuple[Any, ...]) -> str | None:
    lock_acquired_at = row[14] if len(row) > 14 else None
    if isinstance(lock_acquired_at, str) and lock_acquired_at.strip():
        return lock_acquired_at.strip()
    return None


def _write_claim_tombstone(
    conn: sqlite3.Connection,
    *,
    target: str,
    generation: int,
    holder: str,
    node: str,
    lease_stamp: str | None,
    ttl: int,
    now: float,
    now_iso: str,
    replacing_claim: bool,
) -> bool:
    """Write or refresh a fence. ``False`` when a *new* row would exceed a
    cap: the caller must not evict an existing fence to make room."""
    if not _tombstone_row_exists(conn, target, holder, node):
        if _durable_fence_count(conn) >= CLAIM_DURABLE_FENCE_LIMIT:
            return False
        per_node = _target_node_tombstone_count(conn, target, node)
        if not replacing_claim and per_node >= CLAIM_TOMBSTONE_LIMIT_PER_TARGET_NODE:
            return False
    conn.execute(
        "INSERT INTO claim_tombstones "
        "(target, holder_token, owner_node, generation, lease_stamp, released_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(target, holder_token, owner_node) DO UPDATE SET "
        "generation = MAX(claim_tombstones.generation, excluded.generation), "
        "lease_stamp = excluded.lease_stamp, "
        "released_at = excluded.released_at, "
        "expires_at = excluded.expires_at",
        (target, holder, node, generation, lease_stamp, now_iso, now + ttl),
    )
    _raise_generation_floor(conn, target, generation)
    return True


def handle_claim(conn: sqlite3.Connection, raw: Any, *, caller_node: str | None = None) -> tuple[int, dict[str, Any]]:
    """Arbitrate one claim request; returns (http_status, response_payload).

    ``caller_node`` is the node_id bound to the caller's node token: a
    request whose ``node_id`` differs raises ``FleetHubForbidden`` before
    any read or write (``None`` is the admin token with
    ``--allow-admin-writes``, which may act as any node).

    ``acquire`` prunes expired rows, grants a free or expired target, is
    idempotent for the current holder (retrying a lost response extends the
    TTL and preserves ``acquired_at`` while live), and answers 409 with the
    owner for a held target. ``renew`` extends only an unexpired claim whose
    stored fencing token matches the caller's ``holder``; ``release`` deletes
    only that row — a sibling process on the same node without the token can
    neither extend nor delete a live claim. Each mutation is a single SQL
    statement, so two concurrent callers cannot both win.

    ``scope: "node"`` (issue #1141) widens acquire to also replace an
    unexpired row owned by the caller's own ``node_id`` under another token,
    but only the exact row whose stored local lease (``lock``, recorded at
    acquire) has the token the caller presents in ``supersede`` — the dead
    run.lock its lease reconcile just released — and whose lease
    ``acquired_at`` is not newer than the presented one (both are the
    node's own ISO-8601 stamps, so the comparison never crosses clocks).
    The response then carries the replaced row as ``superseded``. A row
    without a recorded lease, a row owned by another node, or a row taken
    under a different lease on the same node is refused with 409. Release
    widens to delete the caller's node's row without a token; ``scope:
    "force"`` (release only) deletes the row whoever owns it. A token-less
    release reports the deleted row as ``claim``. A node-scoped release
    must carry the ``acquired_at`` of the row the caller inspected (400
    without it): the delete is fenced to that exact row, so a claim
    re-acquired between the caller's inspect and its release is never
    deleted (409 with the current row). Only ``force`` deletes unfenced.

    Release also writes a tombstone for the released (target, holder, node)
    and raises the durable per-target generation floor (issue #1189). A
    later acquire or renew whose generation is not newer than that floor,
    whose holder matches a tombstone, or whose lock ``acquired_at`` matches
    the released lease is refused with ``reason: "stale-generation"``. The
    floor outlives claim TTL. A holder-scoped release that finds no row
    still plants a tombstone so an in-flight acquire for that holder cannot
    land after the release. A new fence that would exceed the per-node or
    global durable cap answers ``reason: "storage-full"`` instead of
    evicting an existing fence.
    """
    request = _validate_claim_request(raw)
    if caller_node is not None and request["node_id"] != caller_node:
        raise FleetHubForbidden(
            f"claim node_id {request['node_id']!r} does not match the caller's node token ({caller_node})"
        )
    target = request["target"]
    node = request["node_id"]
    conductor = request["conductor"]
    holder = request["holder"]
    ttl = request["ttl_seconds"]
    scope = request["scope"]
    labels = request["labels"]
    now = _now_epoch()
    now_iso = _epoch_to_iso(now)
    if request["action"] == "inspect":
        row = _fetch_claim(conn, target)
        if row is None or row[10] <= now:
            return 200, {"inspected": True, "claim": None, "owned": False}
        inspected: dict[str, Any] = {"inspected": True, "claim": _claim_payload(row), "owned": row[1] == node}
        if row[1] == node:
            inspected["lock_run_dir"] = row[15]
        return 200, inspected
    if request["action"] == "acquire":
        # One write transaction: the prune, the tombstone check, the
        # prior-row read that the ``superseded`` receipt describes, and the
        # upsert see one state.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))
        prior = _fetch_claim(conn, target)
        tombstones = _fetch_tombstones(conn, target)
        floor = _generation_floor(conn, target)
        if _is_stale_against_tombstones(
            node=node,
            holder=holder,
            generation=request["generation"],
            lease_stamp=_request_lease_stamp(request),
            prior=prior,
            tombstones=tombstones,
            floor=floor,
            now=now,
        ):
            conn.commit()
            return 409, _stale_generation_payload("acquire", target)
        if prior is None and _durable_fence_count(conn) >= CLAIM_DURABLE_FENCE_LIMIT:
            conn.commit()
            return 409, _storage_full_payload("acquire", target)
        lock = request["lock"] or {"token": None, "acquired_at": None, "run_dir": None}
        supersede = request["supersede"] or {"token": None, "acquired_at": None}
        next_generation = _next_claim_generation(prior, tombstones, floor, now)
        cursor = conn.execute(
            "INSERT INTO claims "
            "(target, owner_node, owner_conductor, harness, role, job, session, holder_token, "
            "lock_token, lock_acquired_at, lock_run_dir, "
            "acquired_at, renewed_at, ttl_seconds, expires_at, generation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target) DO UPDATE SET "
            "owner_node = excluded.owner_node, "
            "owner_conductor = excluded.owner_conductor, "
            "harness = excluded.harness, "
            "role = excluded.role, "
            "job = excluded.job, "
            "session = excluded.session, "
            "holder_token = excluded.holder_token, "
            "lock_token = excluded.lock_token, "
            "lock_acquired_at = excluded.lock_acquired_at, "
            "lock_run_dir = excluded.lock_run_dir, "
            "acquired_at = CASE WHEN claims.holder_token = excluded.holder_token "
            "AND claims.owner_node = excluded.owner_node "
            "AND claims.expires_at > ? THEN claims.acquired_at ELSE excluded.acquired_at END, "
            "generation = CASE WHEN claims.holder_token = excluded.holder_token "
            "AND claims.owner_node = excluded.owner_node "
            "AND claims.expires_at > ? THEN claims.generation ELSE excluded.generation END, "
            "renewed_at = excluded.renewed_at, "
            "ttl_seconds = excluded.ttl_seconds, "
            "expires_at = excluded.expires_at "
            "WHERE claims.expires_at <= ? "
            "OR (claims.holder_token = excluded.holder_token AND claims.owner_node = excluded.owner_node) "
            # Same-node supersede: the exact row taken under the dead lease
            # the caller reconciled, and never a row under a newer lease.
            "OR (? IS NOT NULL AND claims.owner_node = excluded.owner_node AND claims.lock_token = ? "
            "AND (? IS NULL OR claims.lock_acquired_at IS NULL OR claims.lock_acquired_at <= ?))",
            (
                target,
                node,
                conductor,
                labels["harness"],
                labels["role"],
                labels["job"],
                labels["session"],
                holder,
                lock["token"],
                lock["acquired_at"],
                lock["run_dir"],
                now_iso,
                now_iso,
                ttl,
                now + ttl,
                next_generation,
                now,
                now,
                now,
                supersede["token"],
                supersede["token"],
                supersede["acquired_at"],
                supersede["acquired_at"],
            ),
        )
        conn.commit()
        if cursor.rowcount == 1:
            row = _fetch_claim(conn, target)
            written = (
                target,
                node,
                conductor,
                labels["harness"],
                labels["role"],
                labels["job"],
                labels["session"],
                now_iso,
                now_iso,
                ttl,
                now + ttl,
                next_generation,
            )
            granted: dict[str, Any] = {"granted": True, "claim": _claim_payload(row if row is not None else written)}
            if scope == "node" and prior is not None and prior[12] != holder:
                granted["superseded"] = _claim_payload(prior)
            return 200, granted
        row = _fetch_claim(conn, target)
        owner = _claim_payload(row) if row is not None else None
        held_by = owner["owner_node"] if owner is not None else "unknown"
        error = f"target {target!r} is held by {held_by}"
        if scope == "node" and row is not None and row[1] == node:
            error += " under another lease than the reconciled dead run's (not superseded)"
        return 409, {"granted": False, "error": error, "owner": owner}
    if request["action"] == "renew":
        prior = _fetch_claim(conn, target)
        if _is_stale_against_tombstones(
            node=node,
            holder=holder,
            generation=request["generation"],
            lease_stamp=_request_lease_stamp(request),
            prior=prior,
            tombstones=_fetch_tombstones(conn, target),
            floor=_generation_floor(conn, target),
            now=now,
        ):
            return 409, _stale_generation_payload("renew", target)
        cursor = conn.execute(
            "UPDATE claims SET renewed_at = ?, ttl_seconds = ?, expires_at = ? "
            "WHERE target = ? AND holder_token = ? AND owner_node = ? AND expires_at > ?",
            (now_iso, ttl, now + ttl, target, holder, node, now),
        )
        conn.commit()
        if cursor.rowcount == 1:
            row = _fetch_claim(conn, target)
            written = (
                target,
                node,
                conductor,
                None,
                None,
                None,
                None,
                now_iso,
                now_iso,
                ttl,
                now + ttl,
                request["generation"] or 1,
            )
            return 200, {"renewed": True, "claim": _claim_payload(row if row is not None else written)}
        row = _fetch_claim(conn, target)
        if row is not None and row[10] > now and (row[12] != holder or row[1] != node):
            return 409, {
                "renewed": False,
                "error": f"target {target!r} is held by {row[1]}",
                "owner": _claim_payload(row),
            }
        return 409, {"renewed": False, "error": f"claim on {target!r} is expired or missing", "owner": None}
    if scope == "holder":
        conn.execute("BEGIN IMMEDIATE")
        prior = _fetch_claim(conn, target)
        cursor = conn.execute(
            "DELETE FROM claims WHERE target = ? AND holder_token = ? AND owner_node = ?",
            (target, holder, node),
        )
        if cursor.rowcount == 1:
            if not _write_claim_tombstone(
                conn,
                target=target,
                generation=_as_generation(prior[11]) if prior is not None else 1,
                holder=str(holder),
                node=node,
                lease_stamp=_row_lease_stamp(prior) if prior is not None else None,
                ttl=int(prior[9]) if prior is not None else ttl,
                now=now,
                now_iso=now_iso,
                replacing_claim=True,
            ):
                conn.rollback()
                return 409, _storage_full_payload("release", target)
            conn.commit()
            return 200, {"released": True}
        if prior is not None:
            conn.commit()
            return 409, {
                "released": False,
                "error": f"target {target!r} is held by {prior[1]}",
                "owner": _claim_payload(prior),
            }
        if not _write_claim_tombstone(
            conn,
            target=target,
            generation=1,
            holder=str(holder),
            node=node,
            lease_stamp=_request_lease_stamp(request),
            ttl=ttl,
            now=now,
            now_iso=now_iso,
            replacing_claim=False,
        ):
            conn.rollback()
            return 409, _storage_full_payload("release", target)
        conn.commit()
        return 200, {"released": False}
    # Token-less release (issue #1141): the caller's own node's row, or with
    # "force" whoever holds the target. The read and the delete share one
    # write transaction, so the receipt's ``claim`` is the row the delete
    # removed — a concurrent acquire cannot slip a new owner in between.
    # ``acquired_at`` (from the caller's inspect) fences the delete to that
    # exact row: a claim re-acquired in between is a different row.
    expected_acquired_at = request["acquired_at"]
    conn.execute("BEGIN IMMEDIATE")
    prior = _fetch_claim(conn, target)
    cursor = conn.execute(
        "DELETE FROM claims WHERE target = ? AND (? = 1 OR owner_node = ?) AND (? IS NULL OR acquired_at = ?)",
        (target, int(scope == "force"), node, expected_acquired_at, expected_acquired_at),
    )
    if cursor.rowcount == 1 and prior is not None:
        if not _write_claim_tombstone(
            conn,
            target=target,
            generation=_as_generation(prior[11]),
            holder=str(prior[12]),
            node=str(prior[1]),
            lease_stamp=_row_lease_stamp(prior),
            ttl=int(prior[9]),
            now=now,
            now_iso=now_iso,
            replacing_claim=True,
        ):
            conn.rollback()
            return 409, _storage_full_payload("release", target)
    conn.commit()
    if cursor.rowcount == 1:
        released: dict[str, Any] = {"released": True}
        if prior is not None:
            released["claim"] = _claim_payload(prior)
        return 200, released
    row = _fetch_claim(conn, target)
    if row is not None:
        if scope != "force" and row[1] != node:
            error = f"target {target!r} is held by {row[1]}, not {node}"
        else:
            error = f"target {target!r} was re-acquired since it was inspected (now acquired {row[7]})"
        return 409, {"released": False, "error": error, "owner": _claim_payload(row)}
    return 200, {"released": False}


def list_claims(conn: sqlite3.Connection, *, include_all: bool = False) -> list[dict[str, Any]]:
    """All active claims (expired ones only with ``include_all``)."""
    now = _now_epoch()
    rows = conn.execute(f"SELECT {_CLAIM_COLUMNS} FROM claims ORDER BY target").fetchall()
    result = []
    for row in rows:
        expired = row[10] <= now
        if expired and not include_all:
            continue
        payload = _claim_payload(row)
        payload["expired"] = expired
        result.append(payload)
    return result


# --- per-node credentials (issue #1150) -------------------------------------


def hash_node_token(token: str) -> str:
    """The only form of a node token the hub ever stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _node_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"node_id": row[0], "label": row[1], "created_at": row[2], "revoked_at": row[3]}


_NODE_COLUMNS = "node_id, label, created_at, revoked_at"


def _validate_node_id(node_id: Any) -> str:
    if not isinstance(node_id, str) or node_id.strip() == "unknown" or not CLAIM_ID_PATTERN.match(node_id.strip()):
        raise FleetHubError(
            "field 'node_id' must be a real identity ([A-Za-z0-9._-], max 128 chars, never the literal 'unknown')"
        )
    return node_id.strip()


def add_node(conn: sqlite3.Connection, node_id: str, label: str | None = None) -> tuple[dict[str, Any], str]:
    """Enrol ``node_id`` and mint its token; returns (node payload, plaintext token).

    The plaintext exists only in the returned value. An enrolled, unrevoked
    node raises ``FleetHubConflict`` (revoke first to rotate); a revoked one
    is re-enrolled under a fresh token in the same single statement, so two
    concurrent adds cannot both win.
    """
    node_id = _validate_node_id(node_id)
    if label is not None and (not isinstance(label, str) or len(label) > NODE_LABEL_MAX_CHARS):
        raise FleetHubError(f"field 'label' must be a string of at most {NODE_LABEL_MAX_CHARS} chars")
    label = label.strip() or None if isinstance(label, str) else None
    token = secrets.token_urlsafe(NODE_TOKEN_BYTES)
    cursor = conn.execute(
        "INSERT INTO nodes (node_id, token_sha256, label, created_at, revoked_at) VALUES (?, ?, ?, ?, NULL) "
        "ON CONFLICT(node_id) DO UPDATE SET token_sha256 = excluded.token_sha256, label = excluded.label, "
        "created_at = excluded.created_at, revoked_at = NULL WHERE nodes.revoked_at IS NOT NULL",
        (node_id, hash_node_token(token), label, _utc_now()),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise FleetHubConflict(f"node {node_id!r} is already enrolled; revoke it first to issue a new token")
    row = conn.execute(f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    return _node_payload(row), token


def revoke_node(conn: sqlite3.Connection, node_id: str) -> dict[str, Any] | None:
    """Revoke ``node_id``'s token (idempotent); ``None`` when it was never enrolled."""
    node_id = _validate_node_id(node_id)
    conn.execute("UPDATE nodes SET revoked_at = ? WHERE node_id = ? AND revoked_at IS NULL", (_utc_now(), node_id))
    conn.commit()
    row = conn.execute(f"SELECT {_NODE_COLUMNS} FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
    return _node_payload(row) if row is not None else None


def list_nodes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every enrolled node (revoked ones included, flagged by ``revoked_at``); never a digest."""
    rows = conn.execute(f"SELECT {_NODE_COLUMNS} FROM nodes ORDER BY node_id").fetchall()
    return [_node_payload(row) for row in rows]


def lookup_node_token(conn: sqlite3.Connection, presented: str) -> tuple[str | None, bool]:
    """``(node_id, revoked)`` for a presented node token; ``(None, False)`` when unknown.

    The lookup is by SHA-256 digest (the indexed column), so the timing of
    the row fetch depends on the digest, not the secret; the stored digest
    is then compared in constant time.
    """
    digest = hash_node_token(presented)
    row = conn.execute(
        "SELECT node_id, token_sha256, revoked_at FROM nodes WHERE token_sha256 = ?", (digest,)
    ).fetchone()
    if row is None or not hmac.compare_digest(str(row[1]).encode("utf-8"), digest.encode("utf-8")):
        return None, False
    return str(row[0]), row[2] is not None


def handle_node_request(conn: sqlite3.Connection, raw: Any) -> tuple[int, dict[str, Any]]:
    """``POST /nodes`` (admin only): add or revoke a node; returns (status, payload).

    The ``add`` payload carries the plaintext token exactly once; nothing
    else ever returns it.
    """
    if not isinstance(raw, dict):
        raise FleetHubError("node request must be a JSON object")
    action = raw.get("action")
    if not isinstance(action, str) or action not in NODE_ACTIONS:
        raise FleetHubError("node field 'action' must be one of: add, revoke")
    node_id = _validate_node_id(raw.get("node_id"))
    if action == "add":
        try:
            node, token = add_node(conn, node_id, raw.get("label"))
        except FleetHubConflict as exc:
            return 409, {"added": False, "error": str(exc)}
        return 200, {"added": True, "node": node, "token": token}
    revoked = revoke_node(conn, node_id)
    if revoked is None:
        return 404, {"revoked": False, "error": f"node {node_id!r} is not enrolled"}
    return 200, {"revoked": True, "node": revoked}


def _load_token(args: argparse.Namespace) -> str:
    token = os.environ.get("BRIGADE_FLEET_TOKEN", "").strip()
    if token:
        return token
    token_file = getattr(args, "token_file", None)
    if token_file is not None:
        try:
            token = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise FleetHubError(f"could not read token file: {exc}") from exc
        if token:
            return token
    raise FleetHubError("no fleet hub token: set BRIGADE_FLEET_TOKEN or pass --token-file")


def make_handler(token: str, db_path: Path, *, allow_admin_writes: bool = False) -> type[BaseHTTPRequestHandler]:
    """Request handler bound to the admin ``token`` and the hub database.

    ``allow_admin_writes`` lets the admin token ``POST /events`` and
    ``POST /claims`` under any ``node_id`` (the pre-#1150 shared-token
    behaviour); off by default so a migration is an explicit choice.
    """

    class _Handler(BaseHTTPRequestHandler):
        server_version = "brigade-fleet-hub/1"
        sys_version = ""
        # Idle-socket guard: a peer that connects and never sends a request
        # line cannot pin a handler thread forever (pre-auth).
        timeout = 30

        def log_message(self, fmt: str, *log_args: Any) -> None:  # quiet by default
            pass

        def _bearer(self) -> str | None:
            """The presented bearer credential, or ``None`` without one."""
            scheme, _, presented = self.headers.get("Authorization", "").partition(" ")
            if scheme != "Bearer" or not presented:
                return None
            return presented

        def _authorized(self) -> bool:
            """True for the admin token (constant-time)."""
            auth = self.headers.get("Authorization", "")
            return hmac.compare_digest(auth.encode("utf-8"), f"Bearer {token}".encode("utf-8"))

        def _caller(self, conn: sqlite3.Connection) -> tuple[bool, str | None] | None:
            """``(is_admin, node_id)`` for the request's bearer, having sent a
            401 and returned ``None`` when it is missing, unknown, or revoked.
            The admin token is checked first, in constant time; anything
            else is looked up as a node token."""
            presented = self._bearer()
            if presented is None:
                self._send_json(401, {"error": "unauthorized"})
                return None
            if self._authorized():
                return True, None
            node_id, revoked = lookup_node_token(conn, presented)
            if node_id is None:
                self._send_json(401, {"error": "unauthorized"})
                return None
            if revoked:
                self._send_json(401, {"error": "unauthorized: node token revoked"})
                return None
            return False, node_id

        def _cookie_authorized(self) -> bool:
            header = self.headers.get("Cookie", "")
            if not header:
                return False
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(header)
            except CookieError:
                return False
            morsel = jar.get(DASHBOARD_COOKIE)
            if morsel is None:
                return False
            return hmac.compare_digest(morsel.value.encode("utf-8"), dashboard_cookie_value(token).encode("utf-8"))

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(
            self,
            status: int,
            body: str,
            *,
            content_type: str = "text/html; charset=utf-8",
            nonce: str | None = None,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            """Dashboard response with the same security headers as ``center serve``."""
            nonce = nonce or secrets.token_urlsafe(16)
            csp = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; script-src-attr 'none'; "
                f"style-src 'nonce-{nonce}'; img-src 'none'; connect-src 'none'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            )
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Cookie")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_dashboard(self, path: str, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            view = fleet_dashboard.DEFAULT_VIEW if path == "/" else path[len(_DASHBOARD_PREFIX) :]
            if view not in fleet_dashboard.VIEWS:
                self._send_html(404, "Not found.\n", content_type=plain)
                return
            params = parse_qs(query, keep_blank_values=False)
            presented = params.pop("token", [""])[0]
            if presented:
                if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
                    self._send_html(401, "Unauthorized.\n", content_type=plain)
                    return
                # Redirect to the same page without the token so it does not
                # linger in the address bar; the view is validated above, so
                # the Location is always one of our own relative routes.
                cookie = (
                    f"{DASHBOARD_COOKIE}={dashboard_cookie_value(token)}; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age={DASHBOARD_COOKIE_MAX_AGE}"
                )
                rest = urlencode(params, doseq=True)
                location = path + (f"?{rest}" if rest else "")
                self._send_html(303, "", content_type=plain, extra_headers={"Location": location, "Set-Cookie": cookie})
                return
            if not (self._authorized() or self._cookie_authorized()):
                self._send_html(
                    401,
                    "Unauthorized: send the fleet bearer token, or open this page once with "
                    "?token=<fleet token> to set the read-only dashboard cookie.\n",
                    content_type=plain,
                )
                return
            try:
                # Non-migrating connection (#1161): the schema exists because
                # server startup created it; this open is read/write only.
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                runs = latest_status(conn, include_all=True)
                claims = list_claims(conn)
                started_at = run_started_at(conn)
                nodes = node_summary(conn)
            except sqlite3.Error as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            nonce = secrets.token_urlsafe(16)
            page = fleet_dashboard.render_page(
                view=view,
                query_string=urlencode(params, doseq=True),
                runs=runs,
                claims=claims,
                nodes=nodes,
                started_at=started_at,
                nonce=nonce,
            )
            self._send_html(200, page, nonce=nonce)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path, _, query = self.path.partition("?")
            if path == "/health":
                self._send_json(200, {"ok": True, "service": "brigade-fleet-hub"})
                return
            if path == "/" or path.startswith(_DASHBOARD_PREFIX):
                self._serve_dashboard(path, query)
                return
            if path in ("/status", "/claims", "/nodes"):
                if self._bearer() is None:
                    self._send_json(401, {"error": "unauthorized"})
                    return
                include_all = parse_qs(query).get("all", [""])[0].lower() in ("1", "true", "yes")
                try:
                    conn = open_db(Path(db_path))
                except (FleetHubError, sqlite3.Error) as exc:
                    self._send_json(500, {"error": f"hub database error: {exc}"})
                    return
                payload: dict[str, Any]
                try:
                    caller = self._caller(conn)
                    if caller is None:
                        return
                    is_admin, _node = caller
                    if path == "/nodes":
                        if not is_admin:
                            self._send_json(403, {"error": "the admin token is required to manage nodes"})
                            return
                        payload = {"nodes": list_nodes(conn)}
                    elif path == "/status":
                        payload = {"runs": latest_status(conn, include_all=include_all)}
                    else:
                        payload = {"claims": list_claims(conn, include_all=include_all)}
                except sqlite3.Error as exc:
                    self._send_json(500, {"error": f"hub database error: {exc}"})
                    return
                finally:
                    conn.close()
                self._send_json(200, payload)
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            path = self.path.partition("?")[0]
            if path not in ("/events", "/claims", "/nodes"):
                self._send_json(404, {"error": "not found"})
                return
            if self._bearer() is None:
                self._send_json(401, {"error": "unauthorized"})
                return
            # The database is opened before the body is read: a node token
            # is resolved against it, and an unauthenticated peer must not
            # get the hub to read its (up to 8 MiB) body first. This is a
            # non-migrating connection (#1161): the schema was created once
            # at server startup.
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_json(500, {"error": f"hub database error: {exc}"})
                return
            body_payload: dict[str, Any]
            try:
                caller = self._caller(conn)
                if caller is None:
                    return
                is_admin, caller_node = caller
                if path == "/nodes":
                    if not is_admin:
                        self._send_json(403, {"error": "the admin token is required to manage nodes"})
                        return
                elif is_admin and not allow_admin_writes:
                    self._send_json(
                        403,
                        {
                            "error": "the admin token may not post events or claims: enroll this node with "
                            "'brigade fleet nodes add' and configure its node token, or start the hub with "
                            "--allow-admin-writes"
                        },
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "bad Content-Length"})
                    return
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._send_json(400, {"error": "missing or oversized body"})
                    return
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(400, {"error": "body is not valid JSON"})
                    return
                if path == "/events":
                    status, body_payload = 200, dict(store_events(conn, parsed, caller_node=caller_node))
                elif path == "/claims":
                    status, body_payload = handle_claim(conn, parsed, caller_node=caller_node)
                else:
                    status, body_payload = handle_node_request(conn, parsed)
            except FleetHubForbidden as exc:
                self._send_json(403, {"error": str(exc)})
                return
            except FleetHubUnprocessable as exc:
                self._send_json(422, {"error": str(exc)})
                return
            except FleetHubError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(500, {"error": f"hub database error: {exc}"})
                return
            finally:
                conn.close()
            self._send_json(status, body_payload)

    return _Handler


def make_server(
    host: str, port: int, db_path: Path, token: str, *, allow_admin_writes: bool = False
) -> ThreadingHTTPServer:
    """Build (but do not serve) the hub HTTPServer; used by tests.

    The database is created/migrated exactly once here, before the socket
    exists, so no request can ever arrive first (#1161). Request handlers then
    use non-migrating connections (``open_db``), keeping token lookup live per
    request. Raises ``FleetHubError``/``sqlite3.Error`` on an unusable or
    too-new database.
    """
    init_db(Path(db_path)).close()
    return ThreadingHTTPServer((host, port), make_handler(token, Path(db_path), allow_admin_writes=allow_admin_writes))


def run(
    *, host: str | None, port: int, db_path: Path, token_file: Path | None, allow_admin_writes: bool = False
) -> int:
    if not host:
        print("error: --host is required (the hub never binds all interfaces by default)", file=sys.stderr)
        return 2
    try:
        token = _load_token(argparse.Namespace(token_file=token_file))
    except FleetHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        server = make_server(host, port, Path(db_path).expanduser(), token, allow_admin_writes=allow_admin_writes)
    except (FleetHubError, sqlite3.Error) as exc:
        # Startup migration is the one place the hub touches the schema: if
        # it cannot be done, refuse to serve rather than 500-ing every request.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    bound_host, bound_port = str(server.server_address[0]), int(server.server_address[1])
    mode = "admin writes allowed" if allow_admin_writes else "node tokens required for writes"
    print(f"brigade fleet hub listening on {bound_host}:{bound_port} (db {Path(db_path).expanduser()}; {mode})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
