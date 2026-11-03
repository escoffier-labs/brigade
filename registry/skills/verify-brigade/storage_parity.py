"""Fleet Hub storage parity: one scripted sequence, every table dumped, compared exactly.

`control-brigade.py storage-parity` imports this module. The wire contract
(`hub_contract.py`) checks what a hub answers; this drive checks what it
stores. It runs one scripted request sequence against a hub on a stepped
clock, stops the hub, reads every table of the SQLite file (the trigger-
maintained ones included), normalizes the rows, and compares them with the
golden the in-repo Python hub produced. Rows one hub writes must read
correctly in the other, so the comparison is exact:

- Values that depend on neither the clock nor randomness compare as stored,
  storage class included (integer 5 and real 5.0 differ).
- Timestamps compare by their exact text format and by their exact offset
  from the run's start, because the hub runs on a clock the drive sets.
- Random values (token digests, snapshot ids, event ids) map to first-seen
  order per id space, and the mapping is applied inside every other column,
  so a wrong relationship shows up as a difference.
- Relations are checked on the rows alone, independent of the golden: token
  digests against the issued tokens, payload digests recomputed from the
  stored bytes, expiries against creation plus the lease, sealed tombstones
  against the envelope that was sent, reply links, and JSON encodings.

The hub runs on a clock read from a file the drive writes (`{clock_file}` in
the launch template; `hub_clock.py` is the Python hub's adapter). The
sequence has three phases, and the hub restarts between them, so a mixed run
can alternate engines on one file: phase 2 starts two days in, phase 3 starts
91 days in, past the 7-day sealed-content lifetime after the phase-2
acknowledgement and past the 90-day message retention for phase-1 mail.

Like the contract drive, nothing here imports `brigade.*`. What the drive
covers is written down in `features/storage-parity.md`.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import hub_contract as hc

PARITY_VERSION = 1
SKILL_DIR = Path(__file__).resolve().parent
RULES_PATH = SKILL_DIR / "storage-parity-rules.json"
GOLDEN_PATH = SKILL_DIR / "storage-parity-golden.json"
FIXTURE_DIR = SKILL_DIR / "storage-parity-fixtures"
LAUNCHER = SKILL_DIR / "hub_clock.py"

# The run's start, 2026-03-02T00:00:00Z. Every timestamp is stored as an offset from it.
T0 = 1772409600
DAY = 86_400
# Phase start offsets: phase 2 is two days in, phase 3 is 91 days in.
PHASE_STARTS = (0, 2 * DAY, 91 * DAY)
# The clock moves before every request, alternating whole and quarter seconds,
# so stored timestamps exercise both of Python's isoformat shapes (with and
# without a fraction) and every value stays exact in binary floating point.
TICKS = (Decimal(1), Decimal("0.25"))
# Epoch-valued REAL cells within this window of T0 are read as timestamps.
EPOCH_WINDOW = 400 * DAY
# Text longer than this is kept in the golden as its digest and length.
LONG_TEXT = 200
# Random values shorter than this are never substituted inside other text.
MIN_RANDOM = 8
MAX_DIFFERENCES = 60
OUTBOX_DEADLINE_SECONDS = 15.0
RETENTION_DAYS = 90
SEALED_LIFETIME = 7 * DAY

DEFAULT_HUB_CMD = (
    "{python} {launcher} --clock-file {clock_file} -- "
    "fleet serve --host {host} --port {port} --db {db} --token-file {token_file}"
)
OPERATOR = "agent:parity-operator"
AGENT = "agent:parity-agent"
NODE_A, NODE_B, NODE_C = "parity-a", "parity-b", "parity-c"
SUBSCRIPTION_SECRET_LABEL = "subscription-secret"

# Run profiles: the hub environment and the database the sequence starts on.
# `defaults` is the hub with its default flags, as the contract doc requires;
# `outbox` names callback hosts, so the hub writes the MCP Events outbox.
PROFILES: dict[str, dict[str, Any]] = {
    "defaults": {"env": {}, "fixture": None, "outbox": False},
    "outbox": {"env": {}, "fixture": None, "outbox": True},
    "migrated-v34": {"env": {}, "fixture": "user-version-34.sql", "outbox": False},
    "migrated-v22": {"env": {}, "fixture": "user-version-22.sql", "outbox": False},
}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$")


class ParityError(Exception):
    """The drive cannot run: a hub did not start, or an input is unusable."""


# ----------------------------------------------------------- the clock


class Clock:
    """The hub's wall clock, written to a file the hub reads on every clock call."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = Decimal(0)
        self._ticks = 0

    def set(self, offset: int | Decimal) -> None:
        self.offset = Decimal(offset)
        scratch = self.path.with_name(self.path.name + ".next")
        scratch.write_text(f"{Decimal(T0) + self.offset}\n", encoding="ascii")
        os.replace(scratch, self.path)

    def tick(self) -> None:
        self.set(self.offset + TICKS[self._ticks % len(TICKS)])
        self._ticks += 1

    def moment(self) -> datetime:
        return datetime.fromtimestamp(T0, tz=timezone.utc) + timedelta(seconds=float(self.offset))

    def iso(self) -> str:
        """Python's `isoformat()` on aware UTC, the form most hub columns use."""
        return self.moment().isoformat()

    def mesh(self) -> str:
        """The mesh wire form: microseconds and a `Z`."""
        return self.moment().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----------------------------------------------------------- deterministic fixtures


def det(label: str, size: int) -> bytes:
    """Repeatable bytes for a label, so key records and envelopes are the same on every run."""
    return hashlib.shake_256(b"brigade-storage-parity/v1\0" + label.encode("utf-8")).digest(size)


HUB_ID = "h_" + hc.b64(det("hub", 16))


def key_record(principal: str, created_at: str) -> dict:
    sig, enc = det(f"{principal}/sig", 32), det(f"{principal}/enc", 1216)
    kid = "k1_" + hc.b64(hashlib.sha256(b"brigade-mesh/v1/kid\0" + sig + enc).digest()[:16])
    return {
        "v": 1,
        "principal": principal,
        "hub": HUB_ID,
        "kid": kid,
        "sig_pub": hc.b64(sig),
        "enc_pub": hc.b64(enc),
        "suites": ["bm1"],
        "created_at": created_at,
        "self_sig": hc.b64(det(f"{principal}/self", 64)),
    }


def envelope(
    *, sender: str, sender_kid: str, recipient: str, recipient_kid: str, client_id: str, created_at: str, **extra: Any
) -> dict:
    """A structurally valid bm1 envelope with repeatable random fields."""
    rcpt = sorted({sender_kid, recipient_kid})
    pad = 1024
    value: dict[str, Any] = {
        "v": 1,
        "suite": "bm1",
        "hub": HUB_ID,
        "from": sender,
        "from_kid": sender_kid,
        "to": recipient,
        "kind": "message",
        "priority": "normal",
        "client_id": client_id,
        "created_at": created_at,
        "pad": pad,
        "rcpt": rcpt,
        "ct_sha256": hc.b64(det(f"{client_id}/ct-sha", 32)),
        "wraps_sha256": hc.b64(det(f"{client_id}/wraps-sha", 32)),
        "rc": hc.b64(det(f"{client_id}/rc", 32)),
        "sig": hc.b64(det(f"{client_id}/sig", 64)),
        "ct": hc.b64(det(f"{client_id}/ct", pad + 16)),
        "recipients": [{"kid": kid, "wrap": hc.b64(det(f"{client_id}/wrap/{kid}", 1168))} for kid in rcpt],
        **extra,
    }
    return value


def subscription_secret() -> str:
    return "whsec_" + base64.b64encode(det(SUBSCRIPTION_SECRET_LABEL, 32)).decode("ascii")


# ----------------------------------------------------------- hub launch


@dataclass
class Engine:
    name: str
    template: str


def python_for(brigade: list[str]) -> str:
    """The interpreter that can import the Brigade the helper resolved."""
    if len(brigade) >= 3 and brigade[1:3] == ["-m", "brigade"]:
        return brigade[0]
    try:
        first = Path(brigade[0]).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return sys.executable
    if first.startswith("#!") and "python" in first:
        return shlex.split(first[2:])[0]
    return sys.executable


def launch(
    engine: Engine,
    *,
    brigade: list[str],
    state_dir: Path,
    admin_token: str,
    env_extra: dict[str, str],
    phase: int,
    startup_timeout: float,
) -> hc.Hub:
    port = hc.free_port()
    values = {
        "host": hc.LOOPBACK,
        "port": str(port),
        "state_dir": str(state_dir),
        "db": str(state_dir / "hub.db"),
        "token_file": str(state_dir / "admin-token"),
        "clock_file": str(state_dir / "clock"),
        "python": python_for(brigade),
        "launcher": str(LAUNCHER),
    }
    try:
        argv = hc.expand_command(engine.template, brigade=brigade, values=values)
    except hc.ContractError as exc:
        raise ParityError(str(exc)) from exc
    log = state_dir / f"hub-{phase}-{engine.name}.log"
    handle = open(log, "wb")  # closed by hc.stop_hub
    os.chmod(log, 0o600)
    env = hc.hub_env(state_dir, admin_token, OPERATOR)
    env.update(env_extra)
    popen: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {}
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(state_dir),
            **popen,
        )
    except OSError as exc:
        handle.close()
        raise ParityError(f"could not start hub engine {engine.name}: {exc}") from exc
    hub = hc.Hub(
        url=f"http://{hc.LOOPBACK}:{port}",
        admin_token=admin_token,
        mode="launched",
        operator=OPERATOR,
        argv=argv,
        state_dir=state_dir,
        log=log,
        process=process,
        log_handle=handle,
    )
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            hc.stop_hub(hub)
            raise ParityError(f"the {engine.name} hub exited with {process.returncode}: {hc.log_tail(log)}")
        if hc.request(hub.url, "GET", "/health", timeout=1.0).status is not None:
            return hub
        time.sleep(0.1)
    hc.stop_hub(hub)
    raise ParityError(f"the {engine.name} hub did not answer /health within {startup_timeout:g}s: {hc.log_tail(log)}")


# ----------------------------------------------------------- the outbox sink


class Sink:
    """A loopback webhook receiver: echoes verification challenges and acknowledges deliveries."""

    def __init__(self) -> None:
        received: list[dict] = []
        self.received = received

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                if isinstance(payload, dict) and payload.get("type") == "verification":
                    reply = json.dumps({"challenge": payload.get("challenge")}).encode()
                else:
                    received.append(payload if isinstance(payload, dict) else {})
                    reply = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer((hc.LOOPBACK, 0), Handler)
        self.origin = f"http://{hc.LOOPBACK}:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def outbox_pending(db: Path) -> int | None:
    """Deliveries the hub has not recorded an outcome for, or None when the file cannot be read yet."""
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        return int(conn.execute("SELECT COUNT(*) FROM fleet_event_deliveries WHERE state='pending'").fetchone()[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ----------------------------------------------------------- the sequence


@dataclass
class Ctx:
    """What the sequence learned, for the relation checks and later phases."""

    admin: str
    tokens: dict[str, str] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    ids: dict[str, Any] = field(default_factory=dict)
    sent_envelopes: dict[str, str] = field(default_factory=dict)
    reply_parents: dict[str, int] = field(default_factory=dict)
    kids: dict[str, str] = field(default_factory=dict)
    sink: Sink | None = None
    delivered: int = 0


class Seq:
    """One scripted request at a time, each one clock tick after the last."""

    def __init__(self, clock: Clock, ctx: Ctx, db: Path) -> None:
        self.clock, self.ctx, self.db = clock, ctx, db
        self.hub: hc.Hub | None = None
        self.steps: list[dict] = []
        self.failures: list[dict] = []

    def call(self, step: str, method: str, path: str, *, status: int, body: Any = None, token: str = "") -> dict:
        assert self.hub is not None
        self.clock.tick()
        resp = hc.request(self.hub.url, method, path, body=body, token=token or None)
        self.steps.append({"step": step, "status": resp.status})
        if resp.status != status:
            detail = resp.json if resp.json is not None else (resp.error or resp.body[:200].decode("utf-8", "replace"))
            self.failures.append({"step": step, "expected": status, "observed": resp.status, "detail": detail})
        return resp.json if isinstance(resp.json, dict) else {}

    def tok(self, name: str) -> str:
        return self.ctx.tokens.get(name, "parity-missing-token")

    def expect_deliveries(self, count: int) -> None:
        """In the outbox run, wait until the sink has `count` more webhooks and the hub recorded every outcome.

        The clock must not move while a delivery is in flight, or its attempt time would depend on scheduling.
        """
        sink = self.ctx.sink
        if sink is None:
            return
        self.ctx.delivered += count
        deadline = time.monotonic() + OUTBOX_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            if len(sink.received) >= self.ctx.delivered and outbox_pending(self.db) == 0:
                break
            time.sleep(0.05)
        else:
            self.failures.append(
                {
                    "step": "outbox delivery",
                    "expected": f"{self.ctx.delivered} webhooks",
                    "observed": len(sink.received),
                }
            )


def subscribe(s: Seq) -> None:
    """The agent principal subscribes to its own inbox; the hub verifies the callback first."""
    sink = s.ctx.sink
    if sink is None:
        return
    s.call(
        "events subscribe message.created",
        "POST",
        "/messages",
        status=200,
        body={
            "action": "events_subscribe",
            "name": "message.created",
            "arguments": {},
            "delivery": {"mode": "webhook", "url": sink.origin + "/hook", "secret": subscription_secret()},
        },
        token=s.tok(AGENT),
    )


def _send(to: str, client_id: str, body: str, enqueued_at: str, **extra: Any) -> dict:
    return {
        "action": "send",
        "to": to,
        "subject": "parity",
        "body": body,
        "ref": None,
        "priority": "normal",
        "client_id": client_id,
        "enqueued_at": enqueued_at,
        **extra,
    }


def _event(node: str, run_id: str, sequence: int, state: str, ts: str) -> dict:
    return {
        "node_id": node,
        "run_id": run_id,
        "state": state,
        "ts": ts,
        "sequence": sequence,
        "digest": f"{run_id}-{sequence}",
        "repo": "example-repo",
        "seat": "coder",
        "harness": "claude",
    }


def _claim(action: str, target: str, node: str, holder: str | None = None, **extra: Any) -> dict:
    body: dict[str, Any] = {"action": action, "target": target, "node_id": node, **extra}
    if holder is not None:
        body["holder"] = holder
    return body


SESSION = {
    "action": "upsert",
    "harness": "claude",
    "session_id": "parity-session-1",
    "repo_identity": "github.com/example-org/example-repo",
    "identity_scope": "fleet",
    "repo_label": "example-repo",
    "checkout_path": "/srv/example/checkout",
    "branch": "main",
    "dirty_paths": ["src/example.py", "docs/café.md"],
    "dirty_truncated": False,
    "ttl_seconds": 900,
}
TARGET_1, TARGET_2 = "repo:example-org/parity-one", "repo:example-org/parity-two"


def ask(s: Seq, request_id: str, question: str) -> tuple[Any, int]:
    """Create a question as node a; returns its id and the id of the message the operator received."""
    created = s.call(
        f"questions create {request_id}",
        "POST",
        "/messages",
        status=201,
        body={"action": "question_create", "question": question, "timeout_secs": 120, "request_id": request_id},
        token=s.tok(NODE_A),
    )
    asked = created.get("message_id")
    return created.get("question_id"), int(asked) if isinstance(asked, str) and asked.isdigit() else 0


def operator_reply(s: Seq, step: str, message_id: int, body: str, client_id: str) -> None:
    s.call(
        step,
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": message_id, "body": body, "client_id": client_id},
        token=s.tok(OPERATOR),
    )


def phase_one(s: Seq) -> None:
    """Registration, heartbeat, claims with fencing, old mail, a question, and paging snapshots."""
    x, admin = s.ctx, s.ctx.admin
    for node, label in ((NODE_A, "parity node a"), (NODE_B, None), (NODE_C, None)):
        body = {"action": "add", "node_id": node, **({"label": label} if label else {})}
        added = s.call(f"nodes add {node}", "POST", "/nodes", status=200, body=body, token=admin)
        x.tokens[node], x.kinds[node] = str(added.get("token") or ""), "node"
    for name, grants in ((AGENT, ["node:*"]), (OPERATOR, ["node:*"])):
        short = name.removeprefix("agent:")
        added = s.call(
            f"principal add {short}",
            "POST",
            "/message-principals",
            status=201,
            body={"action": "add", "name": short},
            token=admin,
        )
        x.tokens[name], x.kinds[name] = str(hc._obj(added.get("principal")).get("token") or ""), "principal"
        s.call(
            f"principal grants {short}",
            "POST",
            "/message-principals",
            status=200,
            body={"action": "grants", "name": short, "may_send_to": grants},
            token=admin,
        )
    a, b = s.tok(NODE_A), s.tok(NODE_B)
    subscribe(s)
    s.call(
        "events accept",
        "POST",
        "/events",
        status=200,
        body=[
            _event(NODE_A, "parity-run-live", 1, "run.started", s.clock.iso()),
            _event(NODE_A, "parity-run-done", 1, "run.started", s.clock.iso()),
        ],
        token=a,
    )
    s.call(
        "events terminal",
        "POST",
        "/events",
        status=200,
        body=[_event(NODE_A, "parity-run-done", 2, "run.completed", s.clock.iso())],
        token=a,
    )
    s.call("sessions upsert", "POST", "/sessions", status=200, body=SESSION, token=a)
    lease = {"token": "parity-lease-1", "acquired_at": s.clock.iso(), "run_dir": "/srv/example/run-1"}
    s.call(
        "claims acquire one",
        "POST",
        "/claims",
        status=200,
        body=_claim("acquire", TARGET_1, NODE_A, "holder-a", ttl_seconds=600, lease=lease),
        token=a,
    )
    s.call(
        "claims acquire one held",
        "POST",
        "/claims",
        status=409,
        body=_claim("acquire", TARGET_1, NODE_B, "holder-b", ttl_seconds=600),
        token=b,
    )
    s.call(
        "claims renew one",
        "POST",
        "/claims",
        status=200,
        body=_claim("renew", TARGET_1, NODE_A, "holder-a", ttl_seconds=900),
        token=a,
    )
    s.call(
        "claims release one",
        "POST",
        "/claims",
        status=200,
        body=_claim("release", TARGET_1, NODE_A, "holder-a"),
        token=a,
    )
    s.call(
        "claims acquire one again",
        "POST",
        "/claims",
        status=200,
        body=_claim("acquire", TARGET_1, NODE_B, "holder-b", ttl_seconds=600),
        token=b,
    )
    s.call(
        "claims acquire two",
        "POST",
        "/claims",
        status=200,
        body=_claim("acquire", TARGET_2, NODE_A, "holder-a2", ttl_seconds=300),
        token=a,
    )
    old = s.call(
        "messages send old",
        "POST",
        "/messages",
        status=201,
        body=_send(NODE_B, "p-old-1", "first", s.clock.iso()),
        token=a,
    )
    x.ids["old"] = hc._obj(old.get("message")).get("id")
    reply = s.call(
        "messages reply old",
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": x.ids["old"], "body": "got it", "client_id": "p-old-2"},
        token=b,
    )
    x.ids["old_reply"] = hc._obj(reply.get("message")).get("id")
    s.call(
        "messages send to the agent",
        "POST",
        "/messages",
        status=201,
        body=_send(AGENT, "p-agent-1", "for the agent é", s.clock.iso()),
        token=a,
    )
    s.expect_deliveries(1)
    question, asked = ask(s, "q1", "Ship the parity drive?")
    operator_reply(s, "questions operator reply q1", asked, "yes", "q1-answer")
    s.call(
        "questions status q1",
        "POST",
        "/messages",
        status=200,
        body={"action": "question_status", "question_id": question},
        token=s.tok(NODE_A),
    )
    s.call("station who page", "POST", "/messages", status=200, body={"action": "who", "limit": 1}, token=b)
    s.call("station claims page", "POST", "/messages", status=200, body={"action": "claims", "limit": 1}, token=b)


def phase_two(s: Seq) -> None:
    """Keys, sealed mail and its acknowledgement, a revocation the snapshot triggers see, and recent mail."""
    x = s.ctx
    a, b = s.tok(NODE_A), s.tok(NODE_B)
    for node, token in ((NODE_A, a), (NODE_B, b)):
        record = key_record(node, s.clock.mesh())
        x.kids[node] = record["kid"]
        s.call(
            f"keys publish {node}",
            "POST",
            "/messages",
            status=201,
            body={"action": "keys_publish", "record": record},
            token=token,
        )
    first = hc.canonical(
        envelope(
            sender=NODE_A,
            sender_kid=x.kids[NODE_A],
            recipient=NODE_B,
            recipient_kid=x.kids[NODE_B],
            client_id="s1",
            created_at=s.clock.mesh(),
        )
    )
    x.sent_envelopes["s1"] = first
    sent = s.call("sealed send", "POST", "/messages", status=201, body={"action": "send", "envelope": first}, token=a)
    sealed_id = hc._obj(sent.get("message")).get("id")
    x.ids["sealed"] = sealed_id
    s.call("sealed send retry", "POST", "/messages", status=201, body={"action": "send", "envelope": first}, token=a)
    s.call(
        "sealed list", "POST", "/messages", status=200, body={"action": "list", "after": 0, "unacked": False}, token=b
    )
    s.call("sealed ack", "POST", "/messages", status=200, body={"action": "ack", "id": sealed_id}, token=b)
    answer = hc.canonical(
        envelope(
            sender=NODE_B,
            sender_kid=x.kids[NODE_B],
            recipient=NODE_A,
            recipient_kid=x.kids[NODE_A],
            client_id="s2",
            created_at=s.clock.mesh(),
            in_reply_to={"hub": HUB_ID, "from": NODE_A, "client_id": "s1"},
        )
    )
    x.sent_envelopes["s2"] = answer
    s.call(
        "sealed reply",
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": sealed_id, "envelope": answer},
        token=b,
    )
    recent = s.call(
        "messages reply to old mail",
        "POST",
        "/messages",
        status=201,
        body={"action": "reply", "id": x.ids["old_reply"], "body": "late reply", "client_id": "p-recent-1"},
        token=a,
    )
    x.reply_parents["p-recent-1"] = x.ids["old_reply"]
    x.ids["recent"] = hc._obj(recent.get("message")).get("id")
    s.call(
        "messages send recent",
        "POST",
        "/messages",
        status=201,
        body=_send(NODE_B, "p-recent-2", "recent", s.clock.iso(), thread="parity-thread"),
        token=a,
    )
    s.call(
        "messages send to the agent again",
        "POST",
        "/messages",
        status=201,
        body=_send(AGENT, "p-agent-2", "second", s.clock.iso()),
        token=a,
    )
    s.expect_deliveries(1)
    s.call(
        "messages ack recent", "POST", "/messages", status=200, body={"action": "ack", "id": x.ids["recent"]}, token=b
    )
    s.call(
        "sessions heartbeat",
        "POST",
        "/sessions",
        status=200,
        body={**SESSION, "branch": "parity-branch"},
        token=s.tok(NODE_A),
    )
    s.call(
        "claims acquire one after its lease ran out",
        "POST",
        "/claims",
        status=200,
        body=_claim("acquire", TARGET_1, NODE_B, "holder-b-2", ttl_seconds=600),
        token=b,
    )
    s.call(
        "claims renew one again",
        "POST",
        "/claims",
        status=200,
        body=_claim("renew", TARGET_1, NODE_B, "holder-b-2", ttl_seconds=1200),
        token=b,
    )
    _, asked = ask(s, "q2", "Answered in time?")
    operator_reply(s, "questions operator reply q2", asked, "in time", "q2-answer")
    x.ids["late_question"], x.ids["late_message"] = ask(s, "q3", "Answered late?")
    s.call("nodes revoke c", "POST", "/nodes", status=200, body={"action": "revoke", "node_id": NODE_C}, token=x.admin)
    s.call("messages sent", "POST", "/messages", status=200, body={"action": "sent"}, token=a)


def phase_three(s: Seq) -> None:
    """Past the sealed lifetime and the retention window: the reads that purge, and an expired lease taken over."""
    a, b = s.tok(NODE_A), s.tok(NODE_B)
    # The first subscription lapsed after its 7-day lifetime; the outbox run takes a new one.
    subscribe(s)
    s.call(
        "messages list after the lifetime",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": 0, "unacked": False},
        token=b,
    )
    s.call(
        "messages list for a",
        "POST",
        "/messages",
        status=200,
        body={"action": "list", "after": 0, "unacked": False},
        token=a,
    )
    s.call("messages count", "POST", "/messages", status=200, body={"action": "count"}, token=b)
    s.call("claims list", "GET", "/claims", status=200, token=b)
    s.call(
        "claims acquire two after expiry",
        "POST",
        "/claims",
        status=200,
        body=_claim("acquire", TARGET_2, NODE_B, "holder-b2", ttl_seconds=300),
        token=b,
    )
    s.call(
        "messages send after the gap",
        "POST",
        "/messages",
        status=201,
        body=_send(AGENT, "p-agent-3", "third", s.clock.iso()),
        token=b,
    )
    s.expect_deliveries(1)
    s.call(
        "questions status q3 after its deadline",
        "POST",
        "/messages",
        status=200,
        body={"action": "question_status", "question_id": s.ctx.ids["late_question"]},
        token=a,
    )
    operator_reply(s, "questions operator reply q3 late", s.ctx.ids["late_message"], "late", "q3-answer")
    s.call("status", "GET", "/status?all=1", status=200, token=a)


PHASES: tuple[Callable[[Seq], None], ...] = (phase_one, phase_two, phase_three)


# ----------------------------------------------------------- dump and normalize


def checkpoint(db: Path) -> None:
    """Fold the WAL into the main file, so the file alone holds every committed row."""
    conn = sqlite3.connect(db, timeout=10)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def dump(db: Path) -> dict[str, dict]:
    """Every table: columns, whether it has a rowid, and each row as a column-to-value map."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        tables: dict[str, dict] = {}
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            info = conn.execute(f"PRAGMA table_xinfo({quoted})").fetchall()
            columns = [row[1] for row in info if row[6] == 0]
            keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            try:
                conn.execute(f"SELECT rowid FROM {quoted} LIMIT 0")
                order, rowid = "rowid", True
            except sqlite3.OperationalError:
                order, rowid = ", ".join('"' + key + '"' for key in keys), False
            select = ", ".join('"' + column + '"' for column in columns)
            rows = conn.execute(f"SELECT {select} FROM {quoted} ORDER BY {order}").fetchall()
            tables[name] = {
                "columns": columns,
                "rowid": rowid,
                "rows": [dict(zip(columns, row, strict=True)) for row in rows],
            }
        return tables
    finally:
        conn.close()


def load_rules(path: Path = RULES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "+" + text if value >= 0 else text


def timestamp_form(text: str) -> str | None:
    """`<T+offset|format>` for an ISO timestamp, keeping the exact text shape, else None."""
    found = _ISO.match(text)
    if not found:
        return None
    year, month, day, hour, minute, second, fraction, zone = found.groups()
    shape = "YYYY-MM-DDTHH:MM:SS" + ("." + "f" * len(fraction) if fraction else "") + (zone or "")
    try:
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=timezone.utc)
    except ValueError:
        return None
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        moment -= sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    whole = int((moment - datetime.fromtimestamp(T0, tz=timezone.utc)).total_seconds())
    offset = Decimal(whole) + (Decimal("0." + fraction) if fraction else Decimal(0))
    return f"<T{_decimal_text(offset)}|{shape}>"


def epoch_form(value: float) -> str | None:
    if abs(value - T0) > EPOCH_WINDOW:
        return None
    return f"<E{_decimal_text(Decimal(repr(value)) - T0)}>"


class Normalizer:
    """Maps random values to first-seen placeholders and renders every cell as typed text."""

    def __init__(self, rules: dict, volatile: dict[str, str] | None = None) -> None:
        self.columns: dict[str, str] = {}
        self.owners: list[str] = list(rules.get("owners", []))
        self.patterns: dict[str, re.Pattern[str]] = {}
        for table, spec in rules.get("random", {}).items():
            for column, rule in spec.items():
                key = f"{table}.{column}"
                if isinstance(rule, dict):
                    self.columns[key] = rule["space"]
                    self.patterns[key] = re.compile(rule["match"])
                else:
                    self.columns[key] = rule
        self.volatile = dict(volatile or {})
        self.seen: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def collect(self, tables: dict[str, dict]) -> None:
        for name in _traversal(tables, self.owners):
            for row in tables[name]["rows"]:
                for column, value in row.items():
                    key = f"{name}.{column}"
                    space = self.columns.get(key)
                    pattern = self.patterns.get(key)
                    if pattern is not None and isinstance(value, str) and not pattern.fullmatch(value):
                        continue
                    if space and isinstance(value, str) and value and value not in self.seen:
                        self.counts[space] = self.counts.get(space, 0) + 1
                        self.seen[value] = f"<{space}#{self.counts[space]}>"
        self._order = sorted(
            [(raw, mark) for raw, mark in self.seen.items() if len(raw) >= MIN_RANDOM] + list(self.volatile.items()),
            key=lambda item: -len(item[0]),
        )

    def text(self, value: str) -> str:
        if value in self.seen:
            return self.seen[value]
        stamp = timestamp_form(value)
        if stamp is not None:
            return stamp
        for raw, mark in self._order:
            if raw in value:
                value = value.replace(raw, mark)
        return value

    def cell(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or isinstance(value, int):
            return f"i:{int(value)}"
        if isinstance(value, float):
            return "r:" + (epoch_form(value) or repr(value))
        if isinstance(value, bytes):
            return f"b:{hashlib.sha256(value).hexdigest()}:{len(value)}"
        text = self.text(str(value))
        if len(text) > LONG_TEXT:
            return f"t~{hashlib.sha256(text.encode('utf-8')).hexdigest()}:{len(text)}"
        return "t:" + text


def _traversal(tables: dict[str, dict], owners: list[str]) -> list[str]:
    """The tables that define each id space, then rowid tables by name, then the rest.

    Owners first, so a placeholder is numbered where its value is defined and a
    wrong reference shows up on the referencing column. Rowid tables before
    WITHOUT ROWID ones, so first-seen order never depends on a random primary key.
    """
    rank = {name: index for index, name in enumerate(owners)}
    return sorted(tables, key=lambda name: (rank.get(name, len(rank)), not tables[name]["rowid"], name))


def normalize(tables: dict[str, dict], rules: dict, volatile: dict[str, str] | None = None) -> dict[str, dict]:
    norm = Normalizer(rules, volatile)
    norm.collect(tables)
    out: dict[str, dict] = {}
    for name in sorted(tables):
        table = tables[name]
        columns = sorted(table["columns"])
        rows = [[norm.cell(row.get(column)) for column in columns] for row in table["rows"]]
        if not table["rowid"]:
            rows.sort(key=lambda cells: json.dumps(cells))
        out[name] = {"columns": columns, "rows": rows}
    return out


def dump_digest(normalized: dict[str, dict]) -> str:
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ----------------------------------------------------------- relations


def _epoch(text: Any) -> Decimal | None:
    if not isinstance(text, str):
        return None
    form = timestamp_form(text)
    if form is None:
        return None
    return Decimal(T0) + Decimal(form[2:].split("|", 1)[0])


def _payload_digest(row: dict, reply_to: Any) -> str:
    fields: list[Any] = [row["recipient"], row["subject"], row["body"], row["reference"], row["priority"], reply_to]
    extras = {key: row[key] for key in ("sender_label", "thread", "kind") if row.get(key) is not None}
    if extras:
        fields.append(extras)
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode("utf-8")).hexdigest()


def _encodings(value: Any) -> dict[str, str]:
    return {
        "python-default": json.dumps(value),
        "python-default-unicode": json.dumps(value, ensure_ascii=False),
        "compact": json.dumps(value, separators=(",", ":")),
        "canonical": hc.canonical(value),
    }


def relations(tables: dict[str, dict], rules: dict, ctx: Ctx | None, final_offset: Decimal | None) -> list[dict]:
    """Checks on the rows alone, independent of any golden."""
    failures: list[dict] = []

    def fail(check: str, table: str, detail: Any) -> None:
        failures.append({"check": check, "table": table, "detail": detail})

    def rows(name: str) -> list[dict]:
        return tables.get(name, {}).get("rows", [])

    if ctx is not None:
        stored = {row["node_id"]: row["token_sha256"] for row in rows("nodes")}
        stored.update({row["principal_id"]: row["token_hash"] for row in rows("fleet_message_principals")})
        for who, token in ctx.tokens.items():
            if not token:
                continue
            if stored.get(who) != hashlib.sha256(token.encode("utf-8")).hexdigest():
                fail("token-digest", "nodes" if ctx.kinds.get(who) == "node" else "fleet_message_principals", who)
    messages = {row["id"]: row for row in rows("fleet_messages")}
    for row in messages.values():
        text = row.get("envelope")
        label = {"id": row["id"], "client_id": row["client_id"]}
        if text is not None:
            if row.get("envelope_bytes") != len(text.encode("utf-8")):
                fail("envelope-bytes", "fleet_messages", label)
            original = ctx.sent_envelopes.get(row["client_id"]) if ctx else None
            if row.get("content_state") == "sealed":
                if row["payload_hash"] != hashlib.sha256(text.encode("ascii", "replace")).hexdigest():
                    fail("sealed-digest", "fleet_messages", label)
            elif row.get("content_state") == "content_expired":
                try:
                    value = json.loads(text)
                except ValueError:
                    value = None
                if not isinstance(value, dict) or "ct" in value or "recipients" in value or hc.canonical(value) != text:
                    fail("tombstone", "fleet_messages", label)
                if original is not None:
                    expected = {k: v for k, v in json.loads(original).items() if k not in ("ct", "recipients")}
                    if hc.canonical(expected) != text:
                        fail("tombstone", "fleet_messages", {**label, "against": "the sent envelope"})
                    if row["payload_hash"] != hashlib.sha256(original.encode("ascii")).hexdigest():
                        fail("sealed-digest", "fleet_messages", label)
            if row.get("sealed_reply_to") is not None:
                parent = messages.get(row["sealed_reply_to"])
                if parent is None or (parent["sender"], parent["recipient"]) != (row["recipient"], row["sender"]):
                    fail("reply-link", "fleet_messages", {**label, "sealed_reply_to": row["sealed_reply_to"]})
        elif row.get("kind") is None:
            # Retention clears `reply_to` when the parent expires, but the digest still binds the parent the
            # reply was sent to. Only the sequence knows it; without the sequence such a row cannot be checked.
            parent_id = row.get("reply_to")
            detached = parent_id is None and _payload_digest(row, None) != row["payload_hash"]
            if detached and ctx is not None:
                parent_id = ctx.reply_parents.get(row["client_id"])
            if (ctx is not None or not detached) and row["payload_hash"] != _payload_digest(row, parent_id):
                fail("payload-digest", "fleet_messages", label)
        if row.get("reply_to") is not None:
            parent = messages.get(row["reply_to"])
            if parent is None or (parent["sender"], parent["recipient"]) != (row["recipient"], row["sender"]):
                fail("reply-link", "fleet_messages", {**label, "reply_to": row["reply_to"]})
        created, received = _epoch(row.get("created_at")), _epoch(row.get("received_at"))
        if created is None or received is None or received < created:
            fail("message-times", "fleet_messages", label)
        elif final_offset is not None and created < Decimal(T0) + final_offset - RETENTION_DAYS * DAY:
            fail("retention", "fleet_messages", label)
    for row in rows("claims"):
        acquired, renewed = _epoch(row.get("acquired_at")), _epoch(row.get("renewed_at"))
        if acquired is None or renewed is None or renewed < acquired:
            fail("claim-times", "claims", row["target"])
        elif Decimal(repr(row["expires_at"])) != renewed + row["ttl_seconds"]:
            fail("claim-expiry", "claims", row["target"])
    for row in rows("interactive_sessions"):
        started, beat = _epoch(row.get("started_at")), _epoch(row.get("heartbeat_at"))
        if started is None or beat is None or beat < started:
            fail("session-times", "interactive_sessions", row["session_id"])
        elif Decimal(repr(row["expires_at"])) != beat + row["ttl_seconds"]:
            fail("session-expiry", "interactive_sessions", row["session_id"])
    fences = {row["target"]: row["generation"] for row in rows("claim_fences")}
    for row in rows("claim_retired_holders"):
        if fences.get(row["target"], 0) < row["generation"]:
            fail("fence-generation", "claim_retired_holders", row["target"])
    for name, spec in rules.get("json", {}).items():
        for column, style in spec.items():
            for index, row in enumerate(rows(name)):
                text = row.get(column)
                if text is None:
                    continue
                try:
                    value = json.loads(text)
                except ValueError:
                    fail("json-encoding", name, {"column": column, "row": index, "expected": style})
                    continue
                if _encodings(value)[style] != text:
                    fail("json-encoding", name, {"column": column, "row": index, "expected": style})
    return failures


# ----------------------------------------------------------- compare


def compare(expected: dict[str, dict], observed: dict[str, dict], limit: int = MAX_DIFFERENCES) -> list[dict]:
    """Cell-by-cell differences between two normalized dumps, capped at `limit`."""
    found: list[dict] = []
    for name in sorted(set(expected) | set(observed)):
        if len(found) >= limit:
            break
        want, got = expected.get(name), observed.get(name)
        if want is None or got is None:
            found.append({"table": name, "check": "table", "expected": want is not None, "observed": got is not None})
            continue
        if want["columns"] != got["columns"]:
            found.append({"table": name, "check": "columns", "expected": want["columns"], "observed": got["columns"]})
            continue
        if len(want["rows"]) != len(got["rows"]):
            found.append(
                {"table": name, "check": "row-count", "expected": len(want["rows"]), "observed": len(got["rows"])}
            )
        for index, (left, right) in enumerate(zip(want["rows"], got["rows"], strict=False)):
            for column, a, b in zip(want["columns"], left, right, strict=True):
                if a != b and len(found) < limit:
                    found.append(
                        {"table": name, "check": "value", "row": index, "column": column, "expected": a, "observed": b}
                    )
    return found


# ----------------------------------------------------------- golden


def load_golden(path: Path = GOLDEN_PATH) -> dict:
    if not path.is_file():
        return {"parity_version": PARITY_VERSION, "t0": T0, "profiles": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def write_golden(path: Path, profile: str, normalized: dict[str, dict]) -> None:
    golden = load_golden(path)
    golden["parity_version"], golden["t0"] = PARITY_VERSION, T0
    golden.setdefault("profiles", {})[profile] = {"digest": dump_digest(normalized), "tables": normalized}
    golden["profiles"] = dict(sorted(golden["profiles"].items()))
    path.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def fixture_sql(db: Path) -> str:
    """A checked-in fixture: the schema and rows of a hub database, with its user_version."""
    conn = sqlite3.connect(db)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        lines = [f"PRAGMA user_version={version};", *conn.iterdump()]
    finally:
        conn.close()
    return "\n".join(lines) + "\n"


def restore_fixture(name: str, db: Path) -> None:
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise ParityError(f"no storage parity fixture {name}")
    conn = sqlite3.connect(db)
    try:
        conn.executescript(path.read_text(encoding="utf-8"))
    finally:
        conn.close()


# ----------------------------------------------------------- the run


@dataclass
class Result:
    normalized: dict[str, dict]
    raw: dict[str, dict]
    ctx: Ctx
    steps: list[dict]
    step_failures: list[dict]
    relation_failures: list[dict]
    hubs: list[dict]
    db: Path
    final_offset: Decimal


def run_sequence(
    *,
    profile: str,
    engines: list[Engine],
    brigade: list[str],
    state_dir: Path,
    startup_timeout: float = 30.0,
) -> Result:
    """Drive the three phases, one engine per phase, then dump the stopped hub's file."""
    if profile not in PROFILES:
        raise ParityError(f"unknown storage parity profile {profile!r}; choose from {', '.join(PROFILES)}")
    if len(engines) != len(PHASES):
        raise ParityError(f"a run needs {len(PHASES)} engines, one per phase")
    spec = PROFILES[profile]
    db = state_dir / "hub.db"
    if spec["fixture"]:
        restore_fixture(spec["fixture"], db)
        os.chmod(db, 0o600)
    admin = "parity-admin-" + secrets.token_urlsafe(18)
    token_file = state_dir / "admin-token"
    token_file.write_text(admin + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    clock = Clock(state_dir / "clock")
    ctx = Ctx(admin=admin)
    env = dict(spec["env"])
    if spec["outbox"]:
        ctx.sink = Sink()
        env["BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS"] = hc.LOOPBACK
        env["BRIGADE_FLEET_EVENTS_ALLOW_LOOPBACK_HTTP"] = "1"
    seq = Seq(clock, ctx, db)
    hubs: list[dict] = []
    try:
        for index, (phase, engine, start) in enumerate(zip(PHASES, engines, PHASE_STARTS, strict=True), start=1):
            clock.set(start)
            hub = launch(
                engine,
                brigade=brigade,
                state_dir=state_dir,
                admin_token=admin,
                env_extra=env,
                phase=index,
                startup_timeout=startup_timeout,
            )
            seq.hub = hub
            hubs.append({"phase": index, "engine": engine.name, "argv": hub.argv, "log": str(hub.log)})
            try:
                phase(seq)
            finally:
                hc.stop_hub(hub)
    finally:
        if ctx.sink is not None:
            ctx.sink.close()
    checkpoint(db)
    raw = dump(db)
    volatile = {ctx.sink.origin: "<sink>"} if ctx.sink is not None else {}
    rules = load_rules()
    return Result(
        normalized=normalize(raw, rules, volatile),
        raw=raw,
        ctx=ctx,
        steps=seq.steps,
        step_failures=seq.failures,
        relation_failures=relations(raw, rules, ctx, clock.offset),
        hubs=hubs,
        db=db,
        final_offset=clock.offset,
    )


def rule_problems(raw: dict[str, dict], rules: dict) -> list[str]:
    """Rules that name a table or column the dump does not have, so the rules file cannot go stale."""
    problems = [f"owners: no table {table}" for table in rules.get("owners", []) if table not in raw]
    for section in ("random", "json"):
        for table, spec in rules.get(section, {}).items():
            if table not in raw:
                problems.append(f"{section}: no table {table}")
                continue
            problems += [
                f"{section}: no column {table}.{column}" for column in spec if column not in raw[table]["columns"]
            ]
    return problems


# ----------------------------------------------------------- control-brigade glue


def add_parser(sub: Any, helper: Any, add_dry_run: Callable[[Any, str], None]) -> Any:
    p = sub.add_parser("storage-parity", help="Run the scripted hub sequence and compare every stored row.")
    p.add_argument("--target", default=None, help="Temp target to hold the hub state (default: a new one).")
    p.add_argument("--profile", choices=sorted(PROFILES), default="defaults", help="Hub environment and start file.")
    p.add_argument("--hub-cmd", default=None, help="Engine 'a' launch template; needs {port} and {clock_file}.")
    p.add_argument("--other-hub-cmd", default=None, help="Engine 'b' launch template for mixed runs.")
    p.add_argument("--engines", default="a,a,a", help="One engine per phase, such as a,b,a (default: a,a,a).")
    p.add_argument("--golden", default=None, help="Golden file (default: the checked-in one).")
    p.add_argument("--write-golden", action="store_true", help="Record this run as the profile's golden.")
    p.add_argument("--startup-timeout", type=float, default=30.0, help="Seconds to wait for /health.")
    add_dry_run(p, "Report the engines and profile without starting anything.")
    p.set_defaults(func=lambda args: command(args, helper))
    return p


def _engines(args: Any) -> list[Engine]:
    templates = {"a": args.hub_cmd or DEFAULT_HUB_CMD, "b": args.other_hub_cmd}
    names = [part.strip() for part in str(args.engines).split(",")]
    engines = []
    for name in names:
        if name not in templates:
            raise ParityError(f"--engines names engines a and b only, not {name!r}")
        template = templates[name]
        if template is None:
            raise ParityError("engine 'b' needs --other-hub-cmd")
        if "{clock_file}" not in template:
            raise ParityError(
                f"engine {name!r} template must contain {{clock_file}} so the hub runs on the drive's clock"
            )
        engines.append(Engine(name, template))
    if len(engines) != len(PHASES):
        raise ParityError(f"--engines needs {len(PHASES)} entries, one per phase")
    return engines


def _workdir(args: Any, helper: Any, root: Path) -> Path:
    if args.target:
        target = helper.guard_target(Path(args.target), root)
        if not target.is_dir():
            raise ParityError(f"target {target} is not an existing directory; create one with new-target first")
        parent = target / "storage-parity"
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ParityError(f"refusing to write through {parent}: it is a symlink or not a directory")
        parent.mkdir(mode=0o700, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=f"{helper.now_stamp()}-", dir=parent))
    work = Path(tempfile.mkdtemp(prefix=f"{helper.now_stamp()}-storage-parity-", dir=root / "targets"))
    helper.record_target(root, work)
    return work


def command(args: Any, helper: Any) -> int:
    """`control-brigade.py storage-parity`: run, compare with the golden, and report."""
    try:
        engines = _engines(args)
        if args.write_golden and (args.hub_cmd or args.other_hub_cmd):
            raise ParityError("--write-golden records the in-repo Python hub alone; drop --hub-cmd and --other-hub-cmd")
        golden_path = Path(args.golden) if args.golden else GOLDEN_PATH
        if args.dry_run:
            return helper.emit(
                {
                    "action": "storage-parity",
                    "ok": True,
                    "dry_run": True,
                    "parity_version": PARITY_VERSION,
                    "profile": args.profile,
                    "engines": [{"name": e.name, "template": e.template} for e in engines],
                    "golden": str(golden_path),
                }
            )
        startup = float(args.startup_timeout)
        if not 0 < startup < float("inf"):
            raise ParityError(
                f"--startup-timeout must be a positive, finite number of seconds, not {args.startup_timeout}"
            )
        root = helper.state_root(args)
        work = _workdir(args, helper, root)
        result = run_sequence(
            profile=args.profile,
            engines=engines,
            brigade=helper.brigade_args(args),
            state_dir=work,
            startup_timeout=startup,
        )
    except (ParityError, hc.ContractError) as exc:
        raise helper.HelperError(str(exc)) from exc
    rules_problems = rule_problems(result.raw, load_rules())
    digest = dump_digest(result.normalized)
    if args.write_golden and not result.step_failures and not result.relation_failures and not rules_problems:
        write_golden(golden_path, args.profile, result.normalized)
    golden = load_golden(golden_path).get("profiles", {}).get(args.profile)
    differences = (
        compare(golden["tables"], result.normalized)
        if golden
        else [{"check": "golden", "expected": f"a golden for {args.profile}", "observed": "none recorded"}]
    )
    ok = not (differences or result.step_failures or result.relation_failures or rules_problems)
    return helper.emit(
        {
            "action": "storage-parity",
            "ok": ok,
            "dry_run": False,
            "parity_version": PARITY_VERSION,
            "profile": args.profile,
            "engines": [hub["engine"] for hub in result.hubs],
            "hubs": result.hubs,
            "state_dir": str(work),
            "db": str(result.db),
            "dump_digest": digest,
            "golden_digest": golden.get("digest") if golden else None,
            "golden_written": bool(args.write_golden and golden and golden.get("digest") == digest),
            "tables": len(result.normalized),
            "rows": sum(len(table["rows"]) for table in result.normalized.values()),
            "steps": len(result.steps),
            "step_failures": hc.scrub(result.step_failures, {t for t in result.ctx.tokens.values() if t}),
            "relation_failures": result.relation_failures,
            "rule_problems": rules_problems,
            "differences": differences,
        },
        0 if ok else 3,
    )


if __name__ == "__main__":  # pragma: no cover - the entry point is control-brigade.py storage-parity
    sys.exit("run this through control-brigade.py storage-parity")
