"""The `prod-readonly` profile of the hub contract drive (v2 slice S29).

`control-brigade.py hub-contract --profile prod-readonly` runs an exact list
of operations, each named by method, path and, for `POST /messages`, action.
Each one stays on the list only while the audit below proves it writes
nothing. Any case outside the list stops the run before the first request.

Two modes:

- `--hub-url` probes a running hub on a loopback origin with credentials the
  operator supplies: a node token and the token of an operator-enrolled probe
  principal with no send grants. The drive never enrolls anything. This is the
  only drive run allowed against production (`docs/runbooks/fleet-hub-proxmox.md`).
- Without `--hub-url` it runs the audit. It launches a throwaway hub through
  `hub_audit.py`, which puts a SQLite authorizer and a statement trace on every
  connection and keeps the background sweepers from starting. It builds a
  fixture that holds every kind of overdue row on a controlled clock, takes a
  logical digest of the whole database, runs the profile, takes the digest
  again, and fails on any recorded write, any `BEGIN IMMEDIATE` or `BEGIN
  EXCLUSIVE`, or any digest change. There is no exception for retention. The
  excluded operations then run as positive controls, and each must record a
  write, or the audit is blind. `--hub-cmd` runs the same audit through another
  front, such as the Rust hub in managed proxy mode, whose Python child starts
  through `hub_audit.py` by way of `BRIGADE_HUB_PYTHON`.

  The hooks see only connections the instrumented child opens, so the audit
  also needs evidence that nothing else opened the database. The child opens
  it through a private hard link, and an inotify watch on the `--db` path's
  directory runs for the whole hub lifetime. Any open through the configured
  path fails the run, even one that is closed again before the response, such
  as a front's own connection or a helper process. Where that watch cannot
  run, the front is reported as not certified, never as passing.

Like the contract drive, nothing here imports `brigade.*`.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import shlex
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hub_contract as hc
import storage_parity as sp

PROFILE = "prod-readonly"
LAUNCHER = Path(__file__).resolve().parent / "hub_audit.py"
DEFAULT_HUB_CMD = "{python} " + shlex.quote(str(LAUNCHER)) + " " + hc.DEFAULT_HUB_CMD.removeprefix("{brigade} ")

# The exact list: operation -> the body keys it may carry (None for a bodyless GET).
ALLOWED_OPERATIONS: dict[str, frozenset[str] | None] = {
    "GET /health": None,
    "GET /owners": None,
    "GET /status": None,
    "GET /sessions": None,
    "GET /claims": None,
    # identity without peers: a deferred read transaction.
    "POST /messages identity": frozenset({"action"}),
    "POST /messages count": frozenset({"action"}),
    "POST /messages keys_get": frozenset({"action", "kid"}),
}
# Operations that write on main, kept off the list. The audit runs the ones it
# can reach as positive controls; each must record a write.
EXCLUDED_OPERATIONS = {
    "POST /messages list": "_prune_expired, database-wide",
    "POST /messages sent": "_prune_expired, database-wide",
    "POST /messages sent_lookup": "_prune_expired, database-wide",
    "POST /messages ack": "_prune_expired, database-wide",
    "POST /messages who": "may rebuild a paging snapshot under BEGIN IMMEDIATE",
    "POST /messages claims": "may rebuild a paging snapshot under BEGIN IMMEDIATE",
    "POST /messages question_*": "expires pending questions",
    "GET /policy/inventory": "runs ensure_schema",
    "browser routes": "the deck's machines view calls unread_counts, which prunes",
}
NODE_TOKEN_ENV = "BRIGADE_HUB_PROBE_NODE_TOKEN"
PROBE_TOKEN_ENV = "BRIGADE_HUB_PROBE_PRINCIPAL_TOKEN"
# Handler threads finish closing their connections just after the response is sent.
SETTLE_SECONDS = 0.3

# Fixture clock: cohort one at T0, cohort two 80 days later, the probe at 92 days.
DAY = sp.DAY
COHORT_TWO, PROBE_AT = 80 * DAY, 92 * DAY
RETENTION_DAYS = 90
NODE_A, NODE_B = "audit-a", "audit-b"
OPERATOR = sp.OPERATOR
PROBE_PRINCIPAL = "agent:audit-probe"


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    path: str
    status: int
    shape: Any
    token: str  # "none", "node" or "principal"
    body: dict | None = None


def operation(case: Case) -> str:
    if case.method == "POST" and case.path == "/messages":
        return f"POST /messages {(case.body or {}).get('action')}"
    return f"{case.method} {case.path}"


def profile_cases() -> list[Case]:
    unknown_kid = "k1_" + hc.b64(secrets.token_bytes(16))
    return [
        Case("health", "GET", "/health", 200, {"ok": hc.Eq(True), "service": hc.Eq(hc.SERVICE_NAME)}, "none"),
        Case("owners", "GET", "/owners", 200, hc.OWNERS, "node"),
        Case("status", "GET", "/status", 200, hc.STATUS, "node"),
        Case("sessions", "GET", "/sessions", 200, {"sessions": [hc.SESSION]}, "node"),
        Case("claims", "GET", "/claims", 200, {"claims": [hc.LISTED_CLAIM]}, "node"),
        Case(
            "messages identity",
            "POST",
            "/messages",
            200,
            {"principal": hc.NonEmpty()},
            "principal",
            {"action": "identity"},
        ),
        Case("messages count", "POST", "/messages", 200, {"unread_count": int}, "principal", {"action": "count"}),
        Case(
            "messages keys_get an unknown key",
            "POST",
            "/messages",
            404,
            hc.error_body("key not found"),
            "principal",
            {"action": "keys_get", "kid": unknown_kid},
        ),
    ]


def guard_cases(cases: list[Case]) -> None:
    """Refuse the whole run if any case is not exactly a listed operation. Runs before any request."""
    for case in cases:
        op = operation(case)
        refusal = f"refusing case {case.name!r}: {op} is not on the {PROFILE} list"
        if op not in ALLOWED_OPERATIONS or "?" in case.path:
            raise hc.ContractError(refusal)
        keys = ALLOWED_OPERATIONS[op]
        if (keys is None) != (case.body is None) or (keys is not None and set(case.body or {}) != keys):
            raise hc.ContractError(f"{refusal} with that body")


def select(cases: list[Case], only: list[str] | None) -> list[Case]:
    if not only:
        return cases
    for op in only:
        if op not in ALLOWED_OPERATIONS:
            raise hc.ContractError(f"refusing --only {op!r}: it is not on the {PROFILE} list")
    return [case for case in cases if operation(case) in only]


def run_profile(url: str, *, node_token: str, principal_token: str, cases: list[Case]) -> dict:
    """Send each case once. The caller has already guarded the list; this checks again."""
    guard_cases(cases)
    d = hc.Drive(hc.Hub(url=url, admin_token="", mode="probe"))
    d.secrets.update(token for token in (node_token, principal_token) if token)
    tokens = {"none": None, "node": node_token, "principal": principal_token}
    for case in cases:
        d.call(
            case.name,
            case.method,
            case.path,
            status=case.status,
            shape=case.shape,
            body=case.body,
            token=tokens[case.token],
        )
    secrets_ = {secret for secret in d.secrets if len(secret) >= 8}
    return {
        "cases": d.cases,
        "mismatches": hc.scrub(d.mismatches, secrets_),
        "exchanges": hc.scrub(d.exchanges, secrets_),
        "operations": sorted({operation(case) for case in cases}),
    }


# ----------------------------------------------------------- the digest


def digest(db: Path) -> dict[str, str]:
    """A logical digest of every table, the schema and `user_version`, opened read-only."""
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        parts: dict[str, str] = {}
        schema = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name").fetchall()
        parts["$schema"] = _hash(schema)
        parts["$user_version"] = str(conn.execute("PRAGMA user_version").fetchone()[0])
        for (name,) in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall():
            quoted = '"' + name.replace('"', '""') + '"'
            width = len(conn.execute(f"SELECT * FROM {quoted} LIMIT 0").description or ())
            order = ", ".join(str(index) for index in range(1, width + 1)) or "1"
            parts[name] = _hash(conn.execute(f"SELECT * FROM {quoted} ORDER BY {order}").fetchall())
        return parts
    finally:
        conn.close()


def _hash(rows: list[tuple]) -> str:
    def cell(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"b": value.hex()}
        if isinstance(value, float):
            return {"f": value.hex()}
        return value

    text = json.dumps([[cell(value) for value in row] for row in rows], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def digest_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


# ----------------------------------------------------------- the audit file


class AuditLog:
    """The JSON lines `hub_audit.py` appends; a window is everything after an offset."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def offset(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def since(self, offset: int) -> list[dict]:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(offset)
                raw = handle.read()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def findings(records: list[dict]) -> list[dict]:
    return [record for record in records if record.get("kind") in ("write", "begin")]


def _summary(records: list[dict]) -> list[dict]:
    """Collapse repeated records (an UPDATE fires once per column) into counted entries."""
    counted: dict[str, dict] = {}
    for record in records:
        entry = {key: record.get(key) for key in ("kind", "action", "target", "mode", "db")}
        key = json.dumps(entry, sort_keys=True)
        counted.setdefault(key, {**entry, "count": 0})["count"] += 1
    return sorted(counted.values(), key=lambda item: json.dumps(item, sort_keys=True))


def uninstrumented_holders(pgid: int, instrumented: set[int], *dbs: Path) -> list[dict]:
    """Processes in the hub's group that hold a database path open without the audit hooks (Linux only)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    names = {f"{db.resolve()}{suffix}" for db in dbs for suffix in ("", "-wal", "-shm", "-journal")}
    holders = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) in instrumented:
            continue
        try:
            fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid:
                continue
            open_files = set()
            for fd in (entry / "fd").iterdir():
                try:
                    open_files.add(os.readlink(fd))
                except OSError:  # closed while listing, such as the listing's own descriptor
                    continue
        except (OSError, IndexError, ValueError):
            continue
        if open_files & names:
            holders.append({"pid": int(entry.name), "files": sorted(open_files & names)})
    return holders


# ----------------------------------------------------------- the open watch

# inotify(7) masks.
IN_ATTRIB, IN_OPEN, IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x4, 0x20, 0x40, 0x80, 0x100, 0x200
IN_DELETE_SELF, IN_MOVE_SELF, IN_Q_OVERFLOW, IN_IGNORED = 0x400, 0x800, 0x4000, 0x8000
WATCH_MASK = IN_OPEN | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF
EVENT_NAMES = {
    IN_OPEN: "open",
    IN_MOVED_FROM: "moved_from",
    IN_MOVED_TO: "moved_to",
    IN_CREATE: "create",
    IN_DELETE: "delete",
}
_EVENT = struct.Struct("iIII")


class OpenWatch:
    """Watch the configured database's directory, so an open through that path is seen even after it closes.

    The instrumented child opens the database through a private hard link
    instead, and the drive reads it through the same link, so nothing the
    audit trusts uses the configured path. Any open of the database file (or
    its `-wal`, `-shm` or `-journal`) through it, or any create, delete or
    rename of one, came from outside the hooks. One event is enough: inotify
    coalesces identical unread events, which would break a count but cannot
    hide that an event happened. The watch is the evidence, so when it cannot
    run (not Linux, no inotify, the queue overflowed, the directory went
    away) the audit says the front is not certified.

    `inode`, when given, is also watched for IN_ATTRIB, which fires when its
    link count changes through any path. A hard link made and removed while
    requests run, which the final link-count check cannot see, then loses the
    evidence. Attribute changes during launch are allowed, since the hub may
    set the file mode then; the final check still sees a link made then.
    """

    def __init__(self, directory: Path, name: str, inode: Path | None = None) -> None:
        self.names = {name, *(f"{name}{suffix}" for suffix in ("-wal", "-shm", "-journal"))}
        self.events: list[dict] = []
        self.lost: str | None = None
        self.fd = -1
        self.inode_wd = -1
        self.phase = "launch"
        if not sys.platform.startswith("linux"):
            self.lost = f"inotify is Linux-only and this is {sys.platform}"
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init, add = libc.inotify_init1, libc.inotify_add_watch
        except (OSError, AttributeError) as exc:
            self.lost = f"inotify is unavailable: {exc}"
            return
        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            self.lost = f"inotify_init1 failed: {os.strerror(ctypes.get_errno())}"
            return
        self.fd = fd
        if add(fd, os.fsencode(str(directory)), WATCH_MASK) < 0:
            self.lost = f"inotify_add_watch failed: {os.strerror(ctypes.get_errno())}"
            self.close()
            return
        if inode is not None:
            self.inode_wd = add(fd, os.fsencode(str(inode)), IN_ATTRIB)
            if self.inode_wd < 0:
                self.lost = f"inotify_add_watch on the database inode failed: {os.strerror(ctypes.get_errno())}"
                self.close()

    def drain(self, next_phase: str | None = None) -> None:
        while self.fd >= 0:
            try:
                raw = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                break
            offset = 0
            while offset + _EVENT.size <= len(raw):
                wd, mask, _cookie, length = _EVENT.unpack_from(raw, offset)
                name = os.fsdecode(raw[offset + _EVENT.size : offset + _EVENT.size + length].rstrip(b"\0"))
                offset += _EVENT.size + length
                if mask & IN_Q_OVERFLOW:
                    self.lost = self.lost or "the inotify queue overflowed"
                elif wd == self.inode_wd and self.inode_wd >= 0:
                    if mask & (IN_IGNORED | IN_DELETE_SELF):
                        self.lost = self.lost or "the database inode was removed during the run"
                    elif mask & IN_ATTRIB and self.phase != "launch":
                        self.lost = self.lost or "the database inode's links or attributes changed during the run"
                elif mask & (IN_IGNORED | IN_DELETE_SELF | IN_MOVE_SELF):
                    self.lost = self.lost or "the database directory was removed or moved during the run"
                elif name in self.names:
                    kinds = [label for bit, label in EVENT_NAMES.items() if mask & bit]
                    self.events.append({"phase": self.phase, "name": name, "events": kinds})
        if next_phase:
            self.phase = next_phase

    def close(self) -> None:
        if self.fd >= 0:
            self.drain()
            os.close(self.fd)
            self.fd = -1


def prepare_route(state_dir: Path) -> tuple[Path, Path | None, str | None]:
    """The configured database path, its private route, and why there is no route (if there is none)."""
    db_dir = state_dir / "db"
    db_dir.mkdir(mode=0o700)
    db = db_dir / "hub.db"
    # Created here, empty and private, so the hub finds nothing to create or fix through this path.
    os.close(os.open(db, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
    route_dir = state_dir / f"db-route-{secrets.token_hex(8)}"
    route_dir.mkdir(mode=0o700)
    route = route_dir / "hub.db"
    try:
        os.link(db, route)
    except OSError as exc:
        return db, None, f"could not link the database route: {exc}"
    return db, route, None


def route_intact(db: Path, route: Path, identity: tuple[int, int]) -> bool:
    try:
        a, b = db.lstat(), route.lstat()
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) == identity and a.st_nlink == 2


# ----------------------------------------------------------- the fixture


@dataclass
class Fixture:
    node_token: str
    principal_token: str
    tokens: dict[str, str]
    question_id: str
    secrets: set[str]


class FixtureBuilder:
    def __init__(self, hub: hc.Hub, clock: sp.Clock) -> None:
        self.hub, self.clock = hub, clock
        self.failures: list[dict] = []

    def call(
        self, step: str, method: str, path: str, *, status: int, body: Any = None, token: str | None = None
    ) -> dict:
        self.clock.tick()
        resp = hc.request(self.hub.url, method, path, body=body, token=token)
        if resp.status != status:
            self.failures.append({"step": step, "expected": status, "observed": resp.status or resp.error})
        return resp.json if isinstance(resp.json, dict) else {}


def build_fixture(hub: hc.Hub, clock: sp.Clock) -> tuple[Fixture, list[dict]]:
    """Every kind of overdue row the design names, made through the API on the controlled clock."""
    f = FixtureBuilder(hub, clock)
    admin = hub.admin_token
    tokens: dict[str, str] = {}
    for node in (NODE_A, NODE_B):
        added = f.call(
            f"nodes add {node}", "POST", "/nodes", status=200, body={"action": "add", "node_id": node}, token=admin
        )
        tokens[node] = str(added.get("token") or "")
    for name, grants in ((OPERATOR, ["node:*"]), (PROBE_PRINCIPAL, None)):
        short = name.removeprefix("agent:")
        added = f.call(
            f"principal add {short}",
            "POST",
            "/message-principals",
            status=201,
            body={"action": "add", "name": short},
            token=admin,
        )
        tokens[name] = str(hc._obj(added.get("principal")).get("token") or "")
        if grants:
            f.call(
                f"principal grants {short}",
                "POST",
                "/message-principals",
                status=200,
                body={"action": "grants", "name": short, "may_send_to": grants},
                token=admin,
            )
    a, b = tokens[NODE_A], tokens[NODE_B]
    f.call(
        "events",
        "POST",
        "/events",
        status=200,
        body=[sp._event(NODE_A, "audit-run", 1, "run.started", clock.iso())],
        token=a,
    )
    f.call(
        "sessions upsert", "POST", "/sessions", status=200, body={**sp.SESSION, "session_id": "audit-session"}, token=a
    )
    f.call(
        "claims acquire",
        "POST",
        "/claims",
        status=200,
        body=sp._claim("acquire", sp.TARGET_1, NODE_A, "audit-holder", ttl_seconds=600),
        token=a,
    )
    # Cohort one: past retention at probe time.
    f.call(
        "messages send old",
        "POST",
        "/messages",
        status=201,
        body=sp._send(NODE_B, "audit-old", "old", clock.iso()),
        token=a,
    )

    # Cohort two: inside retention, past the sealed lifetime, the send window, the question deadline and the snapshot TTL.
    clock.set(COHORT_TWO)
    kids = {}
    for node, token in ((NODE_A, a), (NODE_B, b)):
        record = sp.key_record(node, clock.mesh())
        kids[node] = record["kid"]
        f.call(
            f"keys publish {node}",
            "POST",
            "/messages",
            status=201,
            body={"action": "keys_publish", "record": record},
            token=token,
        )
    sealed = hc.canonical(
        sp.envelope(
            sender=NODE_A,
            sender_kid=kids[NODE_A],
            recipient=NODE_B,
            recipient_kid=kids[NODE_B],
            client_id="audit-s1",
            created_at=clock.mesh(),
        )
    )
    sent = f.call("sealed send", "POST", "/messages", status=201, body={"action": "send", "envelope": sealed}, token=a)
    sealed_id = hc._obj(sent.get("message")).get("id")
    f.call("sealed ack", "POST", "/messages", status=200, body={"action": "ack", "id": sealed_id}, token=b)
    f.call(
        "messages send recent",
        "POST",
        "/messages",
        status=201,
        body=sp._send(NODE_B, "audit-recent", "recent", clock.iso()),
        token=a,
    )
    asked = f.call(
        "question create",
        "POST",
        "/messages",
        status=201,
        body={"action": "question_create", "question": "Still there?", "timeout_secs": 120, "request_id": "audit-q1"},
        token=a,
    )
    f.call("station who page", "POST", "/messages", status=200, body={"action": "who", "limit": 1}, token=b)

    clock.set(PROBE_AT)
    fixture = Fixture(
        node_token=a,
        principal_token=tokens[PROBE_PRINCIPAL],
        tokens=tokens,
        question_id=str(asked.get("question_id") or ""),
        secrets={token for token in tokens.values() if token} | {admin},
    )
    return fixture, f.failures


def overdue_rows(db: Path, now: float) -> dict[str, int]:
    """Count each kind of overdue row, so a fixture that lost one cannot pass vacuously."""

    def iso(seconds: float) -> str:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

    queries = {
        "messages past retention": (
            "SELECT COUNT(*) FROM fleet_messages WHERE created_at<?",
            iso(now - RETENTION_DAYS * DAY),
        ),
        "sealed acknowledged over 7 days ago": (
            "SELECT COUNT(*) FROM fleet_messages WHERE content_state='sealed' AND acked_at<=?",
            iso(now - 7 * DAY),
        ),
        "send-ledger rows over a day old": (
            "SELECT COUNT(*) FROM fleet_message_sends WHERE received_at<?",
            iso(now - DAY),
        ),
        "questions past their deadline": (
            "SELECT COUNT(*) FROM fleet_questions WHERE status='pending' AND deadline<?",
            iso(now),
        ),
        "paging snapshots past their TTL": ("SELECT COUNT(*) FROM fleet_read_snapshots WHERE expires_at<=?", now),
    }
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        counts = {}
        for label, (sql, value) in queries.items():
            try:
                counts[label] = int(conn.execute(sql, (value,)).fetchone()[0])
            except sqlite3.Error:
                counts[label] = 0
        return counts
    finally:
        conn.close()


def controls(fixture: Fixture) -> list[tuple[str, str, str, dict | None, str]]:
    """Excluded operations the audit runs after the probe: (name, method, path, body, token)."""
    a, b = fixture.tokens[NODE_A], fixture.tokens[NODE_B]
    return [
        ("question_status", "POST", "/messages", {"action": "question_status", "question_id": fixture.question_id}, a),
        ("who", "POST", "/messages", {"action": "who", "limit": 1}, b),
        ("list", "POST", "/messages", {"action": "list", "after": 0, "unacked": False}, b),
        ("policy inventory", "GET", "/policy/inventory", None, a),
    ]


# ----------------------------------------------------------- the audit run


def _wrapper(state_dir: Path, python: str) -> Path:
    """`BRIGADE_HUB_PYTHON` for a front in managed mode: start its Python hub child through the audit launcher.

    The Rust front also runs small helper scripts with `-c` through the same
    variable; those go to the plain interpreter.
    """
    wrapper = state_dir / "audited-python"
    py, launcher = shlex.quote(python), shlex.quote(str(LAUNCHER))
    wrapper.write_text(
        f'#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "brigade" ]; then exec {py} {launcher} "$@"; fi\nexec {py} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return wrapper


def audit(
    *,
    brigade: list[str],
    state_dir: Path,
    hub_cmd: str | None,
    startup_timeout: float,
    mutation: str | None,
    only: list[str] | None,
) -> tuple[hc.Hub, dict]:
    cases = select(profile_cases(), only)
    guard_cases(cases)
    python = sp.python_for(brigade)
    admin_token = "audit-admin-" + secrets.token_urlsafe(18)
    token_file = state_dir / "admin-token"
    token_file.write_text(admin_token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    clock = sp.Clock(state_dir / "clock")
    clock.set(0)
    log = AuditLog(state_dir / "audit.jsonl")
    db, route, route_error = prepare_route(state_dir)
    info = db.lstat()
    identity = (info.st_dev, info.st_ino)
    # The drive reads through the route too: the WAL lives beside the path the hub's connections open.
    read_db = route or db
    env = {
        "HUB_AUDIT_FILE": str(log.path),
        "HUB_AUDIT_CLOCK": str(clock.path),
        "BRIGADE_HUB_PYTHON": str(_wrapper(state_dir, python)),
        "BRIGADE_FLEET_MESSAGES_RETENTION_DAYS": str(RETENTION_DAYS),
    }
    if route:
        env.update({"HUB_AUDIT_DB": str(db), "HUB_AUDIT_DB_ROUTE": str(route)})
    if mutation:
        env["HUB_AUDIT_MUTATE"] = mutation
    template = (
        (hub_cmd or DEFAULT_HUB_CMD).replace("{python}", shlex.quote(python)).replace("{db}", shlex.quote(str(db)))
    )
    watch = OpenWatch(db.parent, db.name, inode=db)
    try:
        try:
            hub = sp.launch(
                sp.Engine("audited", template),
                brigade=brigade,
                state_dir=state_dir,
                admin_token=admin_token,
                env_extra=env,
                phase=1,
                startup_timeout=startup_timeout,
            )
        except sp.ParityError as exc:
            raise hc.ContractError(str(exc)) from exc
        try:
            watch.drain("fixture")
            fixture, fixture_failures = build_fixture(hub, clock)
            setup = log.since(0)
            overdue = overdue_rows(read_db, sp.T0 + PROBE_AT)
            time.sleep(SETTLE_SECONDS)
            watch.drain("probe")
            start = log.offset()
            before = digest(read_db)
            probe = run_profile(
                hub.url, node_token=fixture.node_token, principal_token=fixture.principal_token, cases=cases
            )
            time.sleep(SETTLE_SECONDS)
            after = digest(read_db)
            window = log.since(start)
            watch.drain("controls")
            control_results = []
            for name, method, path, body, token in controls(fixture):
                mark = log.offset()
                resp = hc.request(hub.url, method, path, body=body, token=token)
                time.sleep(SETTLE_SECONDS)
                wrote = findings(log.since(mark))
                control_results.append(
                    {"operation": name, "status": resp.status, "writes": len(wrote), "ok": bool(wrote)}
                )
            instrumented = {int(record["pid"]) for record in log.since(0) if record.get("kind") == "ready"}
            dbs = [db, *([route] if route else [])]
            holders = (
                uninstrumented_holders(hub.process.pid, instrumented, *dbs)
                if hub.process and os.name == "posix"
                else []
            )
            watch.drain("shutdown")
        finally:
            hc.stop_hub(hub)
    finally:
        watch.close()
    routed = any(record.get("kind") == "route" for record in log.since(0))
    lost = route_error or watch.lost
    if not lost and not routed:
        lost = "the instrumented hub did not report its database route"
    if not lost and route and not route_intact(db, route, identity):
        lost = "the database file was replaced or linked elsewhere during the run"
    open_evidence = {"complete": lost is None, "reason": lost, "outside_opens": watch.events}
    hooks_live = bool(instrumented) and any(record.get("kind") == "write" for record in setup)
    sweepers_paused = any(record.get("kind") == "sweepers" for record in setup)
    report = {
        "instrumented_pids": sorted(instrumented),
        "hooks_live": hooks_live,
        "sweepers_paused": sweepers_paused,
        "uninstrumented_holders": holders,
        "open_evidence": open_evidence,
        "fixture_failures": fixture_failures,
        "overdue_rows": overdue,
        "window_findings": _summary(findings(window)),
        "digest_changes": digest_changes(before, after),
        "controls": control_results,
        "mutation": mutation,
    }
    problems = []
    if not hooks_live:
        problems.append("the audit hooks recorded nothing while the fixture was built: the hub is not instrumented")
    if not sweepers_paused:
        problems.append("the audited hub did not report its sweepers paused")
    if holders:
        problems.append("a process holds the database open without the audit hooks")
    if watch.events:
        problems.append("a process opened the database outside the audit hooks")
    if lost:
        problems.append(f"no evidence that every database open went through the audit hooks ({lost}): not certified")
    if fixture_failures:
        problems.append("the fixture did not build")
    if not all(overdue.values()):
        problems.append("the fixture is missing a kind of overdue row")
    if report["window_findings"]:
        problems.append("a listed operation wrote, or took the write lock")
    if report["digest_changes"]:
        problems.append("the database changed during the probe")
    if not all(item["ok"] for item in control_results):
        problems.append("an excluded operation recorded no write: the audit cannot see writes")
    report["problems"] = problems
    report["certified"] = not problems
    secrets_ = {secret for secret in fixture.secrets if len(secret) >= 8}
    return hub, {**probe, "audit": hc.scrub(report, secrets_)}


# ----------------------------------------------------------- control-brigade glue


def add_arguments(p: Any) -> None:
    p.add_argument(
        "--profile",
        choices=("contract", "findings", "memory", PROFILE),
        default="contract",
        help="contract (default), findings, memory (the S13a POST /memory cases), or prod-readonly.",
    )
    p.add_argument(
        "--node-token-env", default=NODE_TOKEN_ENV, help="prod-readonly with --hub-url: env var with a node token."
    )
    p.add_argument(
        "--probe-token-env",
        default=PROBE_TOKEN_ENV,
        help="prod-readonly with --hub-url: env var with the probe principal's token.",
    )
    p.add_argument(
        "--only", action="append", default=None, help="prod-readonly: run only this listed operation (repeatable)."
    )
    p.add_argument(
        "--audit-mutation", default=None, help="prod-readonly audit self-test: add a write, as OPERATION:KIND."
    )


def command(args: Any, helper: Any) -> int:
    """`control-brigade.py hub-contract --profile prod-readonly`."""
    root = helper.state_root(args)
    try:
        return _command(args, helper, root)
    except hc.ContractError as exc:
        raise helper.HelperError(str(exc)) from exc


def _command(args: Any, helper: Any, root: Path) -> int:
    if args.hub_url and args.hub_cmd:
        raise hc.ContractError("pass --hub-cmd or --hub-url, not both")
    external = args.hub_url is not None
    url = hc.guard_hub_url(args.hub_url) if external else None
    cases = select(profile_cases(), args.only)
    guard_cases(cases)
    if external and args.audit_mutation:
        raise hc.ContractError("--audit-mutation applies to the launched audit, never to a running hub")
    base = {"action": "hub-contract", "profile": PROFILE, "mode": "probe" if external else "audit", "hub_url": url}
    if args.dry_run:
        return helper.emit(
            {
                **base,
                "ok": True,
                "dry_run": True,
                "operations": sorted({operation(case) for case in cases}),
                "excluded": EXCLUDED_OPERATIONS,
            }
        )
    if external:
        node_token = os.environ.get(args.node_token_env, "").strip()
        principal_token = os.environ.get(args.probe_token_env, "").strip()
        if not node_token or not principal_token:
            raise hc.ContractError(
                f"--hub-url needs a node token in {args.node_token_env} and the probe principal's token in {args.probe_token_env}"
            )
        result = run_profile(url or "", node_token=node_token, principal_token=principal_token, cases=cases)
        hub_info: dict[str, Any] = {"mode": "probe", "url": url}
        ok = not result["mismatches"]
    else:
        startup_timeout = float(args.startup_timeout)
        if not (0 < startup_timeout < float("inf")):
            raise hc.ContractError(
                f"--startup-timeout must be a positive, finite number of seconds, not {args.startup_timeout}"
            )
        work = helper.load_sibling("hub_contract_cli").workdir(args, helper, root, dry_run=False)
        hub, result = audit(
            brigade=helper.brigade_args(args),
            state_dir=work,
            hub_cmd=args.hub_cmd,
            startup_timeout=startup_timeout,
            mutation=args.audit_mutation,
            only=args.only,
        )
        hub_info = {"mode": "audit", "url": hub.url, "argv": hub.argv, "state_dir": str(work), "log": str(hub.log)}
        ok = not result["mismatches"] and not result["audit"]["problems"]
    capture = helper.record_capture(
        root,
        "hub-contract-prod-readonly",
        {"name": "hub-contract-prod-readonly", "exchanges": result["exchanges"], "mismatches": result["mismatches"]},
    )
    return helper.emit(
        {
            **base,
            "ok": ok,
            "dry_run": False,
            "hub": hub_info,
            "operations": result["operations"],
            "cases_total": len(result["cases"]),
            "cases_failed": sum(1 for case in result["cases"] if not case["ok"]),
            "mismatches": result["mismatches"],
            "cases": result["cases"],
            "audit": result.get("audit"),
            "capture": str(capture),
        },
        0 if ok else 3,
    )
