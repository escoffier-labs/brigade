"""Run the in-repo Python hub with a write audit on every SQLite connection.

`python -I hub_audit.py [-m brigade] fleet serve ...` starts the Python hub the
way `hub_clock.py` does, and adds what the S29 read-only audit needs:

- Every `sqlite3.connect` gets an authorizer before its first statement. The
  authorizer fires when a statement is prepared, and Python's hub opens a
  connection per request, so every statement a request runs is seen. Inserts,
  updates, deletes, schema changes, attach and detach, and pragmas that set a
  value (other than the connection-local ones listed below) are recorded.
- Every connection also gets a statement trace, which records `BEGIN
  IMMEDIATE` and `BEGIN EXCLUSIVE`: in production either one takes the write
  lock even when nothing changes.
- The hub's background sweepers (Grok Bot expiry, stats retention, the MCP
  Events dispatcher) never start, so no row changes under the audit for a
  reason the probe did not cause.
- With `HUB_AUDIT_DB` (the hub's `--db` path) and `HUB_AUDIT_DB_ROUTE` (a
  hard link to the same file in a private directory) set, every instrumented
  connection to the database opens the route instead. The drive watches the
  `--db` directory with inotify, so any open through the configured path came
  from outside these hooks: a front's own connection, or a helper process.
- `HUB_AUDIT_MUTATE=<operation>:<kind>` adds a write to one read path, for
  the mutation test that proves the audit sees it. Kinds: `write` changes a
  row, `noop` prepares an UPDATE that matches nothing, `begin` takes and
  releases the write lock, and `helper` runs a plain interpreter in the hub's
  process group that takes and releases the write lock and closes before the
  response.

Configuration comes from the environment, so the Rust front's managed mode can
start this launcher through `BRIGADE_HUB_PYTHON`. Its wrapper removes the
leading `-I` before matching `-m brigade` and starts this launcher with `-I`.

The launcher reads `HUB_AUDIT_FILE` (required, JSON lines), `HUB_AUDIT_CLOCK`
(optional clock file, as in `hub_clock.py`), `HUB_AUDIT_DB` and `HUB_AUDIT_DB_ROUTE`
(optional, together) and `HUB_AUDIT_MUTATE` (optional).

This is the Python hub's test adapter, like `hub_clock.py`. The drive in
`hub_readonly.py` never imports Brigade; only this hub-side process does.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any

# Pragmas that change only the connection that ran them, never the file.
LOCAL_PRAGMAS = frozenset(
    {"busy_timeout", "query_only", "foreign_keys", "cache_size", "temp_store", "trusted_schema", "wal_autocheckpoint"}
)
# Pragmas that can write to the database file or take its locks with no argument at all, so the
# "has a value" test below would miss them: a checkpoint copies WAL pages back, `optimize` may run
# ANALYZE, and `incremental_vacuum` frees pages.
WRITING_PRAGMAS = frozenset({"wal_checkpoint", "optimize", "incremental_vacuum"})
# Read paths a mutation can be added to, by the operation name the drive uses.
MUTATION_POINTS = {
    "GET /owners": ("brigade.fleet_owners", "load_view"),
    "GET /status": ("brigade.fleet_hub", "latest_status"),
    "POST /messages count": ("brigade.fleet_hub_messages", "unread_count"),
}
MUTATION_KINDS = ("write", "noop", "begin", "begin-semicolon", "begin-comment", "pragma-checkpoint", "helper")
# The SQL each `begin*` mutation runs: plain, with a trailing semicolon, and behind a comment.
BEGIN_FORMS = {
    "begin": "BEGIN IMMEDIATE",
    "begin-semicolon": "BEGIN IMMEDIATE;",
    "begin-comment": "/* read */ BEGIN EXCLUSIVE",
}
# The `helper` mutation: a plain interpreter in the hub's process group takes and releases the write lock.
HELPER_SCRIPT = (
    "import sqlite3, sys\n"
    "conn = sqlite3.connect(sys.argv[1], timeout=10, isolation_level=None)\n"
    "conn.execute('BEGIN IMMEDIATE')\n"
    "conn.execute('ROLLBACK')\n"
    "conn.close()\n"
)
SWEEPERS = (
    ("brigade.fleet_hub_grokbot", "start_expiry_sweeper"),
    ("brigade.fleet_admin_stats", "start_retention_sweeper"),
    ("brigade.fleet_hub_events_dispatch", "start_dispatcher"),
)


def _codes(*names: str) -> dict[int, str]:
    return {getattr(sqlite3, name): name.removeprefix("SQLITE_").lower() for name in names if hasattr(sqlite3, name)}


WRITE_CODES = _codes(
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DELETE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_VTABLE",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_DROP_VTABLE",
    "SQLITE_ALTER_TABLE",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
)
PRAGMA_CODE = getattr(sqlite3, "SQLITE_PRAGMA", 19)


class Recorder:
    """Appends one JSON line per finding. The drive reads the lines written inside its window."""

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - lives as long as the hub

    def emit(self, kind: str, **fields: Any) -> None:
        line = json.dumps({"kind": kind, "pid": os.getpid(), "thread": threading.current_thread().name, **fields})
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()


def _authorizer(recorder: Recorder):
    def check(action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None) -> int:
        name = WRITE_CODES.get(action)
        pragma = (arg1 or "").lower()
        if (
            name is None
            and action == PRAGMA_CODE
            and pragma not in LOCAL_PRAGMAS
            and (arg2 is not None or pragma in WRITING_PRAGMAS)
        ):
            name = "pragma"
        if name is not None:
            recorder.emit("write", action=name, target=arg1, detail=arg2, db=db, trigger=trigger)
        # Record only: refusing would change what the hub answers.
        return sqlite3.SQLITE_OK

    return check


_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?(?:\*/|\Z)", re.DOTALL)
_SQL_WORD = re.compile(r"[A-Za-z_]+")


def begin_mode(statement: str) -> str | None:
    """`immediate` or `exclusive` when `statement` opens a write-locking transaction, else None.

    Comments become whitespace and punctuation separates words, so `BEGIN IMMEDIATE;`,
    `/* read */ BEGIN EXCLUSIVE` and `BEGIN/**/IMMEDIATE` are all recognized.
    """
    words = _SQL_WORD.findall(_SQL_COMMENT.sub(" ", statement).upper())
    if len(words) >= 2 and words[0] == "BEGIN" and words[1] in ("IMMEDIATE", "EXCLUSIVE"):
        return words[1].lower()
    return None


def _trace(recorder: Recorder):
    def trace(statement: str) -> None:
        mode = begin_mode(statement)
        if mode is not None:
            recorder.emit("begin", mode=mode)

    return trace


def _reroute(database: Any, uri: bool, configured: str, route: str) -> Any:
    """`database` with the configured path replaced by the route; anything else is returned unchanged."""
    if not isinstance(database, (str, bytes, os.PathLike)):
        return database
    text = os.fsdecode(database)
    if uri and text.startswith("file:"):
        parts = urllib.parse.urlsplit(text)
        if parts.netloc or os.path.realpath(urllib.parse.unquote(parts.path)) != configured:
            return database
        return Path(route).as_uri() + (f"?{parts.query}" if parts.query else "")
    if text and text != ":memory:" and os.path.realpath(text) == configured:
        return route
    return database


def instrument(recorder: Recorder, db: str = "", route: str = "") -> None:
    real_connect = sqlite3.connect
    configured = os.path.realpath(db) if db else ""

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if configured and route:
            uri = bool(kwargs.get("uri", False))
            if args:
                args = (_reroute(args[0], uri, configured, route), *args[1:])
            elif "database" in kwargs:
                kwargs["database"] = _reroute(kwargs["database"], uri, configured, route)
        conn = real_connect(*args, **kwargs)
        conn.set_authorizer(_authorizer(recorder))
        conn.set_trace_callback(_trace(recorder))
        return conn

    sqlite3.connect = connect  # type: ignore[assignment]


def pause_sweepers(recorder: Recorder) -> None:
    def idle(*_args: Any, **_kwargs: Any) -> threading.Thread:
        thread = threading.Thread(target=lambda: None, name="hub-audit-paused-sweeper", daemon=True)
        thread.start()
        return thread

    for module_name, attr in SWEEPERS:
        setattr(importlib.import_module(module_name), attr, idle)
    recorder.emit("sweepers", paused=[f"{module}.{attr}" for module, attr in SWEEPERS])


def _db_path(argv: list[str]) -> str:
    for index, word in enumerate(argv):
        if word == "--db" and index + 1 < len(argv):
            return argv[index + 1]
        if word.startswith("--db="):
            return word.split("=", 1)[1]
    raise SystemExit("hub_audit: a mutation needs the hub's --db argument")


def mutate(spec: str, db_path: str, recorder: Recorder) -> None:
    operation, _, kind = spec.rpartition(":")
    if operation not in MUTATION_POINTS or kind not in MUTATION_KINDS:
        raise SystemExit(f"hub_audit: unknown mutation {spec!r}")
    module_name, attr = MUTATION_POINTS[operation]
    module = importlib.import_module(module_name)
    real = getattr(module, attr)

    def with_write(*args: Any, **kwargs: Any) -> Any:
        if kind == "helper":
            # Not through the patched connect: the helper opens the configured path, locks, and is gone before any scan.
            subprocess.run([sys.executable, "-c", HELPER_SCRIPT, db_path], check=True, timeout=30)
            return real(*args, **kwargs)
        # A fresh connection through the patched connect, so the audit hooks see it.
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            if kind == "write":
                conn.execute(
                    "UPDATE nodes SET label=COALESCE(label,'')||'~' WHERE rowid=(SELECT MIN(rowid) FROM nodes)"
                )
                conn.commit()
            elif kind == "noop":
                conn.execute("UPDATE nodes SET label=label WHERE 0")
                conn.commit()
            elif kind == "pragma-checkpoint":
                conn.execute("PRAGMA wal_checkpoint")
            else:
                conn.isolation_level = None
                conn.execute(BEGIN_FORMS[kind])
                conn.execute("ROLLBACK")
        finally:
            conn.close()
        return real(*args, **kwargs)

    setattr(module, attr, with_write)
    recorder.emit("mutation", operation=operation, mutation=kind)


def main(argv: list[str]) -> int:
    if argv[:2] == ["-m", "brigade"]:
        argv = argv[2:]
    audit_file = os.environ.get("HUB_AUDIT_FILE", "")
    if not audit_file:
        print("hub_audit: HUB_AUDIT_FILE is not set", file=sys.stderr)
        return 2
    clock_file = os.environ.get("HUB_AUDIT_CLOCK", "")
    if clock_file:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import hub_clock

        if not os.path.isfile(clock_file):
            print(f"hub_audit: clock file {clock_file} does not exist", file=sys.stderr)
            return 2
        hub_clock._install(os.path.abspath(clock_file))
    recorder = Recorder(audit_file)
    db, route = os.environ.get("HUB_AUDIT_DB", ""), os.environ.get("HUB_AUDIT_DB_ROUTE", "")
    if bool(db) != bool(route):
        print("hub_audit: HUB_AUDIT_DB and HUB_AUDIT_DB_ROUTE go together", file=sys.stderr)
        return 2
    instrument(recorder, db, route)
    if route:
        recorder.emit("route", db=db, route=route)
    pause_sweepers(recorder)
    spec = os.environ.get("HUB_AUDIT_MUTATE", "")
    if spec:
        mutate(spec, _db_path(argv), recorder)
    recorder.emit("ready", argv=argv[:2], isolated=sys.flags.isolated)
    from brigade.cli import process_main

    sys.argv = ["brigade", *argv]
    process_main()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
