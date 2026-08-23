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
- ``GET /claims`` — bearer auth; active claims (``?all=1`` includes expired).

The token comes from ``BRIGADE_FLEET_TOKEN`` or ``--token-file``; it is never
persisted by Brigade. The database is one SQLite file in WAL mode with a
versioned schema (PRAGMA user_version).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

SCHEMA_VERSION = 2
DEFAULT_PORT = 3774
MAX_BODY_BYTES = 8 * 1024 * 1024

CLAIM_ACTIONS = frozenset({"acquire", "renew", "release"})
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

# Server-arbitrated repo claims (schema v2): one row per target grants the
# target to exactly one holder. holder_token is the per-acquisition fencing
# token (renew/release must present it; it is never sent to other callers).
# expires_at is Unix epoch seconds so expiry comparisons are numeric, not
# string-format bound.
_CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    target TEXT NOT NULL PRIMARY KEY,
    owner_node TEXT NOT NULL,
    owner_conductor TEXT,
    holder_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
"""


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
    conn.execute(_CLAIMS_SCHEMA)
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
    for field in ("target", "node_id", "holder"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise FleetHubError(f"claim field {field!r} must be a non-empty string")
        request[field] = value.strip()
    for field in ("node_id", "holder"):
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
    return request


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
    """Row as (_CLAIM_COLUMNS..., holder_token); the token is index 7."""
    return conn.execute(f"SELECT {_CLAIM_COLUMNS}, holder_token FROM claims WHERE target = ?", (target,)).fetchone()


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
    """
    request = _validate_claim_request(raw)
    target = request["target"]
    node = request["node_id"]
    conductor = request["conductor"]
    holder = request["holder"]
    ttl = request["ttl_seconds"]
    now = _now_epoch()
    now_iso = _epoch_to_iso(now)
    if request["action"] == "acquire":
        conn.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))
        cursor = conn.execute(
            "INSERT INTO claims "
            "(target, owner_node, owner_conductor, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target) DO UPDATE SET "
            "owner_node = excluded.owner_node, "
            "owner_conductor = excluded.owner_conductor, "
            "holder_token = excluded.holder_token, "
            "acquired_at = CASE WHEN claims.holder_token = excluded.holder_token "
            "AND claims.owner_node = excluded.owner_node "
            "AND claims.expires_at > ? THEN claims.acquired_at ELSE excluded.acquired_at END, "
            "renewed_at = excluded.renewed_at, "
            "ttl_seconds = excluded.ttl_seconds, "
            "expires_at = excluded.expires_at "
            "WHERE claims.expires_at <= ? "
            "OR (claims.holder_token = excluded.holder_token AND claims.owner_node = excluded.owner_node)",
            (target, node, conductor, holder, now_iso, now_iso, ttl, now + ttl, now, now),
        )
        conn.commit()
        if cursor.rowcount == 1:
            row = _fetch_claim(conn, target)
            written = (target, node, conductor, now_iso, now_iso, ttl, now + ttl)
            return 200, {"granted": True, "claim": _claim_payload(row if row is not None else written)}
        row = _fetch_claim(conn, target)
        owner = _claim_payload(row) if row is not None else None
        held_by = owner["owner_node"] if owner is not None else "unknown"
        return 409, {"granted": False, "error": f"target {target!r} is held by {held_by}", "owner": owner}
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
    cursor = conn.execute(
        "DELETE FROM claims WHERE target = ? AND holder_token = ? AND owner_node = ?",
        (target, holder, node),
    )
    conn.commit()
    if cursor.rowcount == 1:
        return 200, {"released": True}
    row = _fetch_claim(conn, target)
    if row is not None:
        return 409, {"released": False, "error": f"target {target!r} is held by {row[1]}", "owner": _claim_payload(row)}
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

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path, _, query = self.path.partition("?")
            if path == "/health":
                self._send_json(200, {"ok": True, "service": "brigade-fleet-hub"})
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
