"""Fleet hub: central run-event collector on the tailnet (issue #1123, phase 2).

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

The token comes from ``BRIGADE_FLEET_TOKEN`` or ``--token-file``; it is never
persisted by Brigade. The database is one SQLite file in WAL mode with a
versioned schema (PRAGMA user_version).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

SCHEMA_VERSION = 1
DEFAULT_PORT = 3774
MAX_BODY_BYTES = 8 * 1024 * 1024

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
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            expected = "a non-empty string" if kind is str else "an integer"
            raise FleetHubError(f"event field {field!r} must be {expected}")
        event[field] = value
    for field in OPTIONAL_STR_FIELDS:
        value = raw.get(field)
        if value is None:
            event[field] = None
        elif isinstance(value, str) and value.strip():
            event[field] = value
        else:
            event[field] = None
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
    """Latest event per (node_id, run_id); non-terminal runs unless include_all."""
    rows = conn.execute(
        "SELECT e.node_id, e.run_id, e.repo, e.seat, e.harness, e.state, e.ts, e.sequence, e.digest "
        "FROM events e JOIN ("
        "  SELECT node_id, run_id, MAX(sequence) AS max_seq FROM events GROUP BY node_id, run_id"
        ") m ON e.node_id = m.node_id AND e.run_id = m.run_id AND e.sequence = m.max_seq "
        "ORDER BY e.node_id, e.run_id"
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

        def log_message(self, fmt: str, *log_args: Any) -> None:  # quiet by default
            pass

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            return auth == f"Bearer {token}"

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
            if path == "/status":
                if not self._authorized():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                include_all = parse_qs(query).get("all", [""])[0].lower() in ("1", "true", "yes")
                try:
                    conn = init_db(Path(db_path))
                except FleetHubError as exc:
                    self._send_json(500, {"error": str(exc)})
                    return
                try:
                    self._send_json(200, {"runs": latest_status(conn, include_all=include_all)})
                finally:
                    conn.close()
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            path = self.path.partition("?")[0]
            if path != "/events":
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
            except FleetHubError as exc:
                self._send_json(500, {"error": str(exc)})
                return
            try:
                counts = store_events(conn, parsed)
            except FleetHubError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            finally:
                conn.close()
            self._send_json(200, counts)

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
    bound_host, bound_port = server.server_address[:2]
    print(f"brigade fleet hub listening on {bound_host}:{bound_port} (db {Path(db_path).expanduser()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
