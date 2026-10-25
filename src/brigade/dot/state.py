"""Bridge database schema and journals ported from the bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Iterable

from .contracts import require
from .safe_state import PrivateDatabase, redact_credentials

MAX_OPERATIONS = 500
MAX_ARG_SUMMARY = 200
CALL_LOG_RETENTION = timedelta(days=30)
Clock = Callable[[], datetime]

SUMMARY_KEYS = (
    "to",
    "request_id",
    "job_id",
    "work_id",
    "msg_id",
    "ids",
    "input_id",
    "approval_id",
    "host",
    "repository",
    "seat",
    "workload",
    "consumer",
    "source",
    "status",
    "name",
    "resource",
    "decision",
    "limit",
    "offset",
    "timeout_seconds",
    "since_hash",
    "include_output",
    "include_terminal",
    "include_ended",
    "include_released",
    "unacked_only",
    "read_only",
    "allow_queue",
)
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
SUMMARY_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/#-]{0,95}")
LABEL_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./:-]{0,63}")
CLIENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.@/:()+-]{0,63}")


def call_label(name: Any) -> str:
    """A tool or JSON-RPC method name safe to store; anything else is 'unknown'."""
    return name if isinstance(name, str) and LABEL_RE.fullmatch(name) else "unknown"


def client_label(name: Any) -> str | None:
    """A printable MCP clientInfo.name, bounded; anything else is not stored."""
    if not isinstance(name, str):
        return None
    return name[:64] if CLIENT_RE.fullmatch(name[:64]) else None


def summarize_arguments(arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return None
    parts = []
    for key in SUMMARY_KEYS:
        value = arguments.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, int):
            text = str(value)
        elif isinstance(value, list) and all(type(item) is int for item in value):
            text = ",".join(str(item) for item in value[:10]) + (",..." if len(value) > 10 else "")
        elif isinstance(value, str) and SUMMARY_VALUE_RE.fullmatch(value):
            text = value[:12] if key == "since_hash" else value
        else:
            text = "?"
        parts.append(key + "=" + text)
    return redact_credentials(" ".join(parts))[:MAX_ARG_SUMMARY] or None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    require(isinstance(moment, datetime) and moment.utcoffset() is not None, "clock_unavailable")
    return moment.astimezone(timezone.utc).isoformat()


def _initialize_event_tables(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
        url TEXT NOT NULL, secret TEXT NOT NULL, previous_secret TEXT, rotate_until REAL,
        expires REAL, created REAL NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS event_queue (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL, priority TEXT NOT NULL,
        created REAL NOT NULL, source TEXT UNIQUE NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS event_deliveries (
        event_id TEXT NOT NULL, subscription_id TEXT NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(event_id, subscription_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS wake_attempts (
        timestamp REAL NOT NULL, priority TEXT NOT NULL, source TEXT NOT NULL DEFAULT '')""")
    if "source" not in {row[1] for row in connection.execute("PRAGMA table_info(wake_attempts)")}:
        # Unknown wake origins still consume the global budget, but confer no diagnostic scope.
        connection.execute("ALTER TABLE wake_attempts ADD COLUMN source TEXT NOT NULL DEFAULT ''")
    connection.execute("""CREATE TABLE IF NOT EXISTS tracked_requests (
        request_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
        completed INTEGER NOT NULL DEFAULT 0)""")


class BridgeState(PrivateDatabase):
    """bridge.sqlite3 beside the review ledger, under the same private guards.

    dot-notify (any local agent) inserts inbox rows; Dot lists and acks them.
    The operations journal keeps every mutating fleet call's handle so a lost
    HTTP response never loses a launched request id. Prompts are never stored.
    """

    DATABASE = "bridge.sqlite3"
    LOCK = "bridge.lock"

    def __init__(self, state_dir: Path) -> None:
        super().__init__(state_dir)

    def initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            sender TEXT NOT NULL, thread TEXT, body TEXT NOT NULL, acked_at TEXT)""")
        connection.execute("CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(acked_at, id)")
        connection.execute("""CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, tool TEXT NOT NULL, request_id TEXT,
            outcome TEXT NOT NULL, summary TEXT NOT NULL)""")

        columns = {row[1] for row in connection.execute("PRAGMA table_info(inbox)")}
        for name, definition in (
            ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
            ("subject", "TEXT"),
            ("ref", "TEXT"),
            ("kind", "TEXT NOT NULL DEFAULT 'message'"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE inbox ADD COLUMN {name} {definition}")
        connection.execute("""CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(msg_id, body))""")
        _initialize_event_tables(connection)
        # Bridge v5: metadata-only log of every authenticated MCP call. Additive;
        # never touches existing tables or rows.
        connection.execute("""CREATE TABLE IF NOT EXISTS call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tool TEXT NOT NULL,
            ok INTEGER NOT NULL, duration_ms INTEGER NOT NULL, request_id TEXT,
            arg_summary TEXT, error_code TEXT)""")
        connection.execute("CREATE INDEX IF NOT EXISTS call_log_ts ON call_log(ts)")
        if "client_name" not in {row[1] for row in connection.execute("PRAGMA table_info(call_log)")}:
            # Additive: the MCP clientInfo.name sent with initialize, so hosted clients can be told apart.
            connection.execute("ALTER TABLE call_log ADD COLUMN client_name TEXT")
        connection.execute("CREATE TABLE IF NOT EXISTS send_budget (ts REAL NOT NULL)")
        inbox_columns = connection.execute("PRAGMA table_info(hub_inbox)").fetchall()
        if inbox_columns and "scope" not in {row[1] for row in inbox_columns}:
            # Old IDs have no proven endpoint. Archive them without adopting authority.
            connection.execute("ALTER TABLE hub_inbox RENAME TO hub_inbox_legacy")
            connection.execute("DROP INDEX IF EXISTS hub_inbox_pending")
        connection.execute("""CREATE TABLE IF NOT EXISTS hub_inbox (
            scope TEXT NOT NULL DEFAULT '', id INTEGER NOT NULL, sender TEXT NOT NULL, sender_label TEXT,
            subject TEXT, body TEXT NOT NULL, ref TEXT, thread TEXT, kind TEXT,
            priority TEXT, reply_to INTEGER, created_at TEXT NOT NULL,
            acked_at TEXT, synced_at TEXT NOT NULL, PRIMARY KEY(scope,id))""")
        connection.execute("CREATE INDEX IF NOT EXISTS hub_inbox_pending ON hub_inbox(scope,acked_at,id)")
        connection.execute("CREATE TABLE IF NOT EXISTS hub_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Recover submitted handles from the existing journal without touching inbox rows.
        connection.execute("""INSERT OR IGNORE INTO tracked_requests(request_id)
            SELECT DISTINCT request_id FROM operations WHERE tool='fleet_submit'
            AND request_id IS NOT NULL AND outcome LIKE 'ok%'""")

    def hub_get(
        self, key: str, default: Any = None, *, scope: str = "", connection: sqlite3.Connection | None = None
    ) -> Any:
        if connection is None:
            with self.transaction() as c:
                return self.hub_get(key, default, scope=scope, connection=c)
        row = connection.execute(
            "SELECT value FROM hub_state WHERE key=?", ((scope + ":" if scope else "") + key,)
        ).fetchone()
        return row[0] if row else default

    def hub_set(self, key: str, value: Any, *, scope: str = "", connection: sqlite3.Connection | None = None) -> None:
        if connection is None:
            with self.transaction() as c:
                return self.hub_set(key, value, scope=scope, connection=c)
        connection.execute(
            "INSERT OR REPLACE INTO hub_state VALUES (?,?)", ((scope + ":" if scope else "") + key, str(value))
        )

    def hub_upsert(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        node_id: str,
        scope: str = "",
        clock: Clock = utc_now,
        connection: sqlite3.Connection | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        """Write through and wake atomically. Never advance the sync cursor here.

        fetched_at is when the rows were requested from the hub. A local ack stamped
        after that moment is newer than the fetched copy, so it is not reopened.
        """
        if connection is None:
            with self.transaction() as c:
                return self.hub_upsert(
                    rows, node_id=node_id, scope=scope, clock=clock, connection=c, fetched_at=fetched_at
                )
        from .events import enqueue

        fields = (
            "id",
            "sender",
            "sender_label",
            "subject",
            "body",
            "ref",
            "thread",
            "kind",
            "priority",
            "reply_to",
            "created_at",
            "acked_at",
        )
        now = _iso(clock())
        for row in rows:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO hub_inbox (scope,"
                + ",".join(fields)
                + ",synced_at) VALUES ("
                + ",".join("?" for _ in range(len(fields) + 2))
                + ")",
                (scope,) + tuple(row.get(key) for key in fields) + (now,),
            )
            if fetched_at is not None and row.get("acked_at") is None:
                local = connection.execute(
                    "SELECT acked_at FROM hub_inbox WHERE scope=? AND id=?", (scope, row["id"])
                ).fetchone()
                if local is not None and local[0] is not None and datetime.fromisoformat(local[0]) > fetched_at:
                    continue
            connection.execute(
                "UPDATE hub_inbox SET acked_at=CASE WHEN ? IS NULL THEN NULL "
                "ELSE COALESCE(acked_at, ?) END, synced_at=? WHERE scope=? AND id=?",
                (row.get("acked_at"), row.get("acked_at"), now, scope, row["id"]),
            )
            if (
                cursor.rowcount
                and row.get("acked_at") is None
                and not (
                    row["sender"] == node_id
                    and row.get("sender_label") == "dot-bridge"
                    and row.get("kind") == "completion"
                    and (row.get("client_id") or "").startswith("dbc-")
                )
            ):
                priority = "urgent" if row.get("priority") in ("high", "urgent") else "normal"
                enqueue(
                    connection,
                    "fleet.inbox.message",
                    {"msg_id": row["id"], "priority": priority},
                    priority,
                    "hub-inbox:" + scope + ":" + str(row["id"]),
                )

    def hub_messages(self, *, unacked_only: bool, limit: int, node_id: str, scope: str = "") -> dict[str, Any]:
        from .inbox_tools import legacy_message

        with self.transaction() as c:
            where = "AND acked_at IS NULL" if unacked_only else ""
            order = "ASC" if unacked_only else "DESC"
            rows = c.execute(
                f"SELECT * FROM hub_inbox WHERE scope=? {where} ORDER BY id {order} LIMIT ?", (scope, limit)
            ).fetchall()
            pending = c.execute(
                "SELECT COUNT(*) FROM hub_inbox WHERE scope=? AND acked_at IS NULL", (scope,)
            ).fetchone()[0]
        return {"unacked_total": pending, "messages": [legacy_message(dict(row), node_id) for row in rows]}

    def hub_mark_acked(
        self,
        ids: Iterable[int],
        *,
        scope: str = "",
        clock: Clock = utc_now,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self.transaction() as c:
                return self.hub_mark_acked(ids, scope=scope, clock=clock, connection=c)
        connection.executemany(
            "UPDATE hub_inbox SET acked_at=? WHERE scope=? AND id=? AND acked_at IS NULL",
            [(_iso(clock()), scope, ident) for ident in ids],
        )

    def begin_operation(self, tool: str, summary: dict[str, Any], *, clock: Clock = utc_now) -> int:
        text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        require(len(text.encode("utf-8")) <= 4096)
        now = _iso(clock())
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO operations
                (created_at, updated_at, tool, request_id, outcome, summary)
                VALUES (?,?,?,?, 'started', ?)""",
                (now, now, tool, summary.get("request_id"), text),
            )
            connection.execute(
                """DELETE FROM operations WHERE id <= (
                SELECT id FROM operations ORDER BY id DESC LIMIT 1 OFFSET ?)""",
                (MAX_OPERATIONS,),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def finish_operation(
        self,
        operation_id: int,
        request_id: str | None,
        outcome: str,
        *,
        error_message: str | None = None,
        clock: Clock = utc_now,
    ) -> None:
        with self.transaction() as connection:
            operation = connection.execute("SELECT tool FROM operations WHERE id=?", (operation_id,)).fetchone()
            if (
                request_id
                and outcome.startswith("ok")
                and operation
                and operation["tool"] in ("fleet_submit", "fleet_followup")
            ):
                if operation["tool"] == "fleet_submit":
                    connection.execute("INSERT OR IGNORE INTO tracked_requests(request_id) VALUES (?)", (request_id,))
                else:
                    connection.execute(
                        "UPDATE tracked_requests SET generation=generation+1,completed=0 WHERE request_id=?",
                        (request_id,),
                    )
            if error_message:
                row = connection.execute("SELECT summary FROM operations WHERE id=?", (operation_id,)).fetchone()
                if row is not None:
                    summary = {**json.loads(row["summary"]), "error_message": error_message}
                    connection.execute(
                        "UPDATE operations SET summary=? WHERE id=?",
                        (json.dumps(summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True), operation_id),
                    )
            connection.execute(
                """UPDATE operations SET request_id=COALESCE(?, request_id),
                outcome=?, updated_at=? WHERE id=?""",
                (request_id, outcome, _iso(clock()), operation_id),
            )

    def operations(self, limit: int) -> list[dict[str, Any]]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            dict(
                id=row["id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                tool=row["tool"],
                request_id=row["request_id"],
                outcome=row["outcome"],
                summary=json.loads(row["summary"]),
            )
            for row in rows
        ]

    # -- bridge v5: call log --------------------------------------------------------

    def log_call(
        self,
        tool: str,
        ok: bool,
        duration_ms: float,
        *,
        request_id: str | None = None,
        arg_summary: str | None = None,
        error_code: str | None = None,
        client_name: str | None = None,
        clock: Clock = utc_now,
    ) -> int:
        """Metadata only: tool or method name, outcome, timing, client name and a short id/limit summary."""
        row = (
            _iso(clock()),
            call_label(tool),
            1 if ok else 0,
            max(0, int(duration_ms)),
            request_id if isinstance(request_id, str) and REQUEST_ID_RE.fullmatch(request_id) else None,
            arg_summary[:MAX_ARG_SUMMARY] if isinstance(arg_summary, str) and arg_summary else None,
            str(error_code)[:64] if error_code else None,
            client_label(client_name),
        )
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO call_log
                (ts, tool, ok, duration_ms, request_id, arg_summary, error_code, client_name)
                VALUES (?,?,?,?,?,?,?,?)""",
                row,
            )
            assert cursor.lastrowid is not None
            if cursor.lastrowid % 500 == 0:
                connection.execute("DELETE FROM call_log WHERE ts < ?", (_iso(clock() - CALL_LOG_RETENTION),))
            return cursor.lastrowid

    def reserve_send(self, limit: int, window_seconds: float, *, now: float | None = None) -> bool:
        """Atomically take one send from a rolling window. A refused reservation records nothing."""
        now = time.time() if now is None else now
        with self.transaction() as connection:
            connection.execute("DELETE FROM send_budget WHERE ts <= ?", (now - window_seconds,))
            used = connection.execute("SELECT COUNT(*) FROM send_budget").fetchone()[0]
            if used >= limit:
                return False
            connection.execute("INSERT INTO send_budget(ts) VALUES (?)", (now,))
            return True

    def prune_call_log(self, *, clock: Clock = utc_now) -> int:
        with self.transaction() as connection:
            return connection.execute(
                "DELETE FROM call_log WHERE ts < ?", (_iso(clock() - CALL_LOG_RETENTION),)
            ).rowcount

    def subscription_count(self, owner: str, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.transaction() as connection:
            return connection.execute(
                """SELECT COUNT(*) FROM subscriptions WHERE owner=?
                AND (expires IS NULL OR expires > ?)""",
                (owner, now),
            ).fetchone()[0]
