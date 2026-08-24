"""Fleet hub: central run-event collector and claim arbiter on the tailnet
(issue #1123 phase 2, issue #1125 phase 4).

A small stdlib-only HTTP service (``http.server.ThreadingHTTPServer`` +
``sqlite3``) that accepts ``POST /events`` from fleet nodes and answers
``GET /status`` with the latest observed state per (node_id, run_id).

Endpoints:
- ``GET /health`` — no auth; liveness probe.
- ``POST /events`` — bearer auth; body is a single event object or a JSON
  array of events. Dedupe on UNIQUE(node_id, run_id, sequence, digest).
  Responds ``{"accepted": n, "duplicate": m}``.
- ``GET /status`` — bearer auth; latest state per (node_id, run_id) for
  non-terminal runs. ``?all=1`` includes terminal runs.
- ``POST /claims`` — bearer auth; ``{"action": "acquire"|"renew"|"release",
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
  Trust boundary: ``node_id``, ``lock`` and ``supersede`` are
  caller-asserted under one shared bearer token, so ``scope`` is an intent
  marker that keeps *honest* clients from stealing each other's claims (a
  same-basename workspace, a cloned node identity), not an authorization —
  the bearer token already authorizes ``force`` and arbitrary claims.
- ``GET /claims`` — bearer auth; active claims (``?all=1`` includes expired).
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

The token comes from ``BRIGADE_FLEET_TOKEN`` or ``--token-file``; it is never
persisted by Brigade. The database is one SQLite file in WAL mode with a
versioned schema (PRAGMA user_version).
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
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from . import fleet_dashboard

SCHEMA_VERSION = 3
DEFAULT_PORT = 3774
MAX_BODY_BYTES = 8 * 1024 * 1024

# Dashboard cookie (issue #1124): a derived, read-only capability, not the token.
DASHBOARD_COOKIE = "brigade_fleet_view"
DASHBOARD_COOKIE_MAX_AGE = 30 * 86400
_DASHBOARD_COOKIE_PURPOSE = b"brigade-fleet-dashboard-cookie-v1"
_DASHBOARD_PREFIX = "/view/"

CLAIM_ACTIONS = frozenset({"acquire", "renew", "release"})
# Request scopes (issue #1141): "holder" is the fencing-token contract; "node"
# lets a node act on its own dead holder's row; "force" is the operator
# override, release only. Renew is never widened: a token-less renew would
# let any same-node process keep a claim alive.
CLAIM_SCOPES = frozenset({"holder", "node", "force"})
CLAIM_ACTION_SCOPES: dict[str, frozenset[str]] = {
    "acquire": frozenset({"holder", "node"}),
    "renew": frozenset({"holder"}),
    "release": CLAIM_SCOPES,
}
DEFAULT_CLAIM_TTL_SECONDS = 900
CLAIM_TTL_MIN_SECONDS = 1
CLAIM_TTL_MAX_SECONDS = 86400
# Two cloned or identity-less machines must never both be granted a target:
# a claim identity has to be a real, well-formed node id, never "unknown".
CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

EVENT_FIELDS = {
    "node_id": str,
    "run_id": str,
    "state": str,
    "ts": str,
    "sequence": int,
    "digest": str,
}
OPTIONAL_STR_FIELDS = ("repo", "seat", "harness")

# Terminal run_event.v1 lifecycle types (see run_events.EVENT_TYPES).
TERMINAL_STATES = frozenset({"run.completed", "run.failed", "run.interrupted"})

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
    holder_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    lock_token TEXT,
    lock_acquired_at TEXT,
    lock_run_dir TEXT
);
"""
_CLAIMS_LEASE_COLUMNS = ("lock_token", "lock_acquired_at", "lock_run_dir")
# Bounded so a hostile client cannot bloat the row; a lease token is a uuid4
# hex and an ISO-8601 stamp, a run_dir a filesystem path.
LEASE_FIELD_MAX_CHARS = 1024


class FleetHubError(RuntimeError):
    """Hub configuration or request failure with an operator-readable message."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open the hub database, creating the schema on first use.

    WAL mode for concurrent readers; ``PRAGMA user_version`` records the schema
    version. A database written by a newer hub is refused rather than
    silently reinterpreted.
    """
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        conn.close()
        raise FleetHubError(
            f"fleet hub database {db_path} has schema version {current}; this brigade supports {SCHEMA_VERSION}"
        )
    conn.execute(_SCHEMA)
    # Claims are ephemeral TTL state: a pre-holder-token claims table (early
    # v2 builds) is dropped and recreated rather than migrated.
    claims_columns = {row[1] for row in conn.execute("PRAGMA table_info(claims)").fetchall()}
    if claims_columns and "holder_token" not in claims_columns:
        conn.execute("DROP TABLE claims")
        claims_columns = set()
    conn.execute(_CLAIMS_SCHEMA)
    # v2 -> v3: the lease columns are nullable, so live claims survive the
    # upgrade (they simply can never be superseded, only released/expired).
    if claims_columns:
        for column in _CLAIMS_LEASE_COLUMNS:
            if column not in claims_columns:
                conn.execute(f"ALTER TABLE claims ADD COLUMN {column} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS events_run_seq ON events (node_id, run_id, sequence)")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
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
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise FleetHubError(f"event field {field!r} must be a non-negative integer")
        event[field] = value
    for field in OPTIONAL_STR_FIELDS:
        value = raw.get(field)
        event[field] = value if isinstance(value, str) and value.strip() else None
    return event


def store_events(conn: sqlite3.Connection, raw_events: Any) -> dict[str, int]:
    """Insert events with dedupe; returns {"accepted": n, "duplicate": m}."""
    if isinstance(raw_events, dict):
        raw_list = [raw_events]
    elif isinstance(raw_events, list):
        raw_list = raw_events
    else:
        raise FleetHubError("body must be an event object or array of events")
    if len(raw_list) > 1000:
        raise FleetHubError("too many events in one POST (max 1000)")
    events = [_validate_event(raw) for raw in raw_list]
    accepted = 0
    duplicate = 0
    received_at = _utc_now()
    for event in events:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO events "
            "(node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        "SELECT node_id, run_id, repo, seat, harness, state, ts, sequence, digest FROM ("
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
        raise FleetHubError("claim field 'action' must be one of: acquire, renew, release")
    request["action"] = action
    scope = raw.get("scope", "holder")
    if not isinstance(scope, str) or scope not in CLAIM_ACTION_SCOPES[action]:
        allowed = ", ".join(sorted(CLAIM_ACTION_SCOPES[action]))
        raise FleetHubError(f"claim field 'scope' for {action} must be one of: {allowed}")
    request["scope"] = scope
    # A node/force release has no holder token to present: the token died
    # with the crashed run it is cleaning up after.
    holder_optional = action == "release" and scope != "holder"
    for field in ("target", "node_id", "holder"):
        value = raw.get(field)
        if field == "holder" and holder_optional and value is None:
            request[field] = None
            continue
        if not isinstance(value, str) or not value.strip():
            raise FleetHubError(f"claim field {field!r} must be a non-empty string")
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
    request["conductor"] = conductor.strip() if isinstance(conductor, str) and conductor.strip() else None
    ttl = raw.get("ttl_seconds", DEFAULT_CLAIM_TTL_SECONDS)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not CLAIM_TTL_MIN_SECONDS <= ttl <= CLAIM_TTL_MAX_SECONDS:
        raise FleetHubError(
            f"claim field 'ttl_seconds' must be an integer in [{CLAIM_TTL_MIN_SECONDS}, {CLAIM_TTL_MAX_SECONDS}]"
        )
    request["ttl_seconds"] = ttl
    lock = raw.get("lock")
    if lock is not None and action != "acquire":
        raise FleetHubError("claim field 'lock' is only valid for acquire")
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


def _claim_payload(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "target": row[0],
        "owner_node": row[1],
        "owner_conductor": row[2],
        "acquired_at": row[3],
        "renewed_at": row[4],
        "ttl_seconds": row[5],
        "expires_at": _epoch_to_iso(row[6]),
    }


# Payload columns only: holder_token is a fencing capability and is never
# serialized to callers (it would let anyone forge a renew/release).
_CLAIM_COLUMNS = "target, owner_node, owner_conductor, acquired_at, renewed_at, ttl_seconds, expires_at"


def _fetch_claim(conn: sqlite3.Connection, target: str) -> tuple[Any, ...] | None:
    """Row as (_CLAIM_COLUMNS..., holder_token, lock_token, lock_acquired_at);
    the fencing token is index 7, the lease token index 8."""
    return conn.execute(
        f"SELECT {_CLAIM_COLUMNS}, holder_token, lock_token, lock_acquired_at FROM claims WHERE target = ?",
        (target,),
    ).fetchone()


def handle_claim(conn: sqlite3.Connection, raw: Any) -> tuple[int, dict[str, Any]]:
    """Arbitrate one claim request; returns (http_status, response_payload).

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
    release reports the deleted row as ``claim``.
    """
    request = _validate_claim_request(raw)
    target = request["target"]
    node = request["node_id"]
    conductor = request["conductor"]
    holder = request["holder"]
    ttl = request["ttl_seconds"]
    scope = request["scope"]
    now = _now_epoch()
    now_iso = _epoch_to_iso(now)
    if request["action"] == "acquire":
        conn.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))
        # Only a same-node supersede needs the prior row (to report what it
        # replaced); expired rows are already gone, so this is a live claim.
        prior = _fetch_claim(conn, target) if scope == "node" else None
        lock = request["lock"] or {"token": None, "acquired_at": None, "run_dir": None}
        supersede = request["supersede"] or {"token": None, "acquired_at": None}
        cursor = conn.execute(
            "INSERT INTO claims "
            "(target, owner_node, owner_conductor, holder_token, lock_token, lock_acquired_at, lock_run_dir, "
            "acquired_at, renewed_at, ttl_seconds, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target) DO UPDATE SET "
            "owner_node = excluded.owner_node, "
            "owner_conductor = excluded.owner_conductor, "
            "holder_token = excluded.holder_token, "
            "lock_token = excluded.lock_token, "
            "lock_acquired_at = excluded.lock_acquired_at, "
            "lock_run_dir = excluded.lock_run_dir, "
            "acquired_at = CASE WHEN claims.holder_token = excluded.holder_token "
            "AND claims.owner_node = excluded.owner_node "
            "AND claims.expires_at > ? THEN claims.acquired_at ELSE excluded.acquired_at END, "
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
                holder,
                lock["token"],
                lock["acquired_at"],
                lock["run_dir"],
                now_iso,
                now_iso,
                ttl,
                now + ttl,
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
            written = (target, node, conductor, now_iso, now_iso, ttl, now + ttl)
            granted: dict[str, Any] = {"granted": True, "claim": _claim_payload(row if row is not None else written)}
            if prior is not None and prior[7] != holder:
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
        cursor = conn.execute(
            "UPDATE claims SET renewed_at = ?, ttl_seconds = ?, expires_at = ? "
            "WHERE target = ? AND holder_token = ? AND owner_node = ? AND expires_at > ?",
            (now_iso, ttl, now + ttl, target, holder, node, now),
        )
        conn.commit()
        if cursor.rowcount == 1:
            row = _fetch_claim(conn, target)
            written = (target, node, conductor, now_iso, now_iso, ttl, now + ttl)
            return 200, {"renewed": True, "claim": _claim_payload(row if row is not None else written)}
        row = _fetch_claim(conn, target)
        if row is not None and row[6] > now and (row[7] != holder or row[1] != node):
            return 409, {
                "renewed": False,
                "error": f"target {target!r} is held by {row[1]}",
                "owner": _claim_payload(row),
            }
        return 409, {"renewed": False, "error": f"claim on {target!r} is expired or missing", "owner": None}
    if scope == "holder":
        cursor = conn.execute(
            "DELETE FROM claims WHERE target = ? AND holder_token = ? AND owner_node = ?",
            (target, holder, node),
        )
        conn.commit()
        if cursor.rowcount == 1:
            return 200, {"released": True}
        row = _fetch_claim(conn, target)
        if row is not None:
            return 409, {
                "released": False,
                "error": f"target {target!r} is held by {row[1]}",
                "owner": _claim_payload(row),
            }
        return 200, {"released": False}
    # Token-less release (issue #1141): the caller's own node's row, or with
    # "force" whoever holds the target. One statement, so a concurrent
    # acquire cannot slip a new owner's row under a node-scoped delete.
    prior = _fetch_claim(conn, target)
    cursor = conn.execute(
        "DELETE FROM claims WHERE target = ? AND (? = 1 OR owner_node = ?)",
        (target, int(scope == "force"), node),
    )
    conn.commit()
    if cursor.rowcount == 1:
        released: dict[str, Any] = {"released": True}
        if prior is not None:
            released["claim"] = _claim_payload(prior)
        return 200, released
    row = _fetch_claim(conn, target)
    if row is not None:
        return 409, {
            "released": False,
            "error": f"target {target!r} is held by {row[1]}, not {node}",
            "owner": _claim_payload(row),
        }
    return 200, {"released": False}


def list_claims(conn: sqlite3.Connection, *, include_all: bool = False) -> list[dict[str, Any]]:
    """All active claims (expired ones only with ``include_all``)."""
    now = _now_epoch()
    rows = conn.execute(f"SELECT {_CLAIM_COLUMNS} FROM claims ORDER BY target").fetchall()
    result = []
    for row in rows:
        expired = row[6] <= now
        if expired and not include_all:
            continue
        payload = _claim_payload(row)
        payload["expired"] = expired
        result.append(payload)
    return result


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


def make_handler(token: str, db_path: Path) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "brigade-fleet-hub/1"
        # Idle-socket guard: a peer that connects and never sends a request
        # line cannot pin a handler thread forever (pre-auth).
        timeout = 30

        def log_message(self, fmt: str, *log_args: Any) -> None:  # quiet by default
            pass

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return hmac.compare_digest(auth.encode("utf-8"), f"Bearer {token}".encode("utf-8"))

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
                conn = init_db(Path(db_path))
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
            if path in ("/status", "/claims"):
                if not self._authorized():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                include_all = parse_qs(query).get("all", [""])[0].lower() in ("1", "true", "yes")
                try:
                    conn = init_db(Path(db_path))
                except (FleetHubError, sqlite3.Error) as exc:
                    self._send_json(500, {"error": f"hub database error: {exc}"})
                    return
                payload: dict[str, Any]
                try:
                    if path == "/status":
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
            if path not in ("/events", "/claims"):
                self._send_json(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
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
            try:
                conn = init_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_json(500, {"error": f"hub database error: {exc}"})
                return
            body_payload: dict[str, Any]
            try:
                if path == "/events":
                    status, body_payload = 200, dict(store_events(conn, parsed))
                else:
                    status, body_payload = handle_claim(conn, parsed)
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


def make_server(host: str, port: int, db_path: Path, token: str) -> ThreadingHTTPServer:
    """Build (but do not serve) the hub HTTPServer; used by tests."""
    return ThreadingHTTPServer((host, port), make_handler(token, Path(db_path)))


def run(*, host: str | None, port: int, db_path: Path, token_file: Path | None) -> int:
    if not host:
        print("error: --host is required (the hub never binds all interfaces by default)", file=sys.stderr)
        return 2
    try:
        token = _load_token(argparse.Namespace(token_file=token_file))
    except FleetHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    server = make_server(host, port, Path(db_path).expanduser(), token)
    bound_host, bound_port = str(server.server_address[0]), int(server.server_address[1])
    print(f"brigade fleet hub listening on {bound_host}:{bound_port} (db {Path(db_path).expanduser()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
