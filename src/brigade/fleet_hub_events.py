"""Fleet Hub MCP Events: subscriptions, event outbox and the JSON handlers behind the fleet MCP station.

A hosted agent subscribes (``events/subscribe`` on the fleet MCP station, which
forwards to the hub with the caller's own principal credential) and the hub
delivers signed webhooks. Everything is off until the operator names at least
one callback host (``fleet hub serve --events-callback-host``); with no hosts
configured every handler answers ``events_disabled`` and the record_* hooks
return before touching the database.

Webhook payloads are thin on purpose: ids, principal names, hub-validated enums
and timestamps. They never carry message subjects, bodies, threads or labels,
and free text that a node chose (claim targets, run ids) is included only when
it is a short token without whitespace. The hosted agent reads the full record
through its own authenticated tools, so delivery widens nothing it could not
already read.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import ssl
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .mcp_events import (
    CallbackPolicy,
    canonical_arguments,
    parse_callback,
    post_webhook,
    secret_key,
    signed_headers,
    subscription_id,
)

MESSAGE_CREATED = "message.created"
CLAIM_EXPIRED = "claim.expired"
RUN_FAILED = "run.failed"
EVENT_NAMES = (MESSAGE_CREATED, CLAIM_EXPIRED, RUN_FAILED)

DEFAULT_TTL_MS = 7 * 86_400_000
MIN_TTL_MS = 60_000
MAX_SUBSCRIPTIONS_PER_PRINCIPAL = 20
MAX_SUBSCRIPTIONS = 200
ROTATION_WINDOW_SECONDS = 300
_VERIFY_SLOTS = threading.BoundedSemaphore(4)
_ID_RE = re.compile(r"^(agent:)?[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A free-text field from a node is shared only when it cannot carry a sentence.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:#@-]{0,199}$")
_TIMESTAMP_RE = re.compile(r"^[0-9][0-9T:.+Z-]{9,39}$")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS fleet_event_subscriptions (
        id TEXT NOT NULL PRIMARY KEY, principal_id TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
        filter_value TEXT, url TEXT NOT NULL, secret TEXT NOT NULL, previous_secret TEXT, rotate_until REAL,
        created_at REAL NOT NULL, expires_at REAL, cursor_seq INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS fleet_event_subscriptions_principal ON fleet_event_subscriptions (principal_id)",
    "CREATE INDEX IF NOT EXISTS fleet_event_subscriptions_name ON fleet_event_subscriptions (name)",
    """CREATE TABLE IF NOT EXISTS fleet_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        subject TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS fleet_event_deliveries (
        seq INTEGER NOT NULL, subscription_id TEXT NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, attempted_at REAL,
        PRIMARY KEY (seq, subscription_id))""",
    "CREATE INDEX IF NOT EXISTS fleet_event_deliveries_due ON fleet_event_deliveries (state, next_attempt)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)


@dataclass(frozen=True)
class EventsConfig:
    """Operator choices. No callback host means the feature is off."""

    callback_hosts: frozenset[str] = frozenset()
    allow_loopback_http: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.callback_hosts)

    def policy(self) -> CallbackPolicy:
        return CallbackPolicy(self.callback_hosts, self.allow_loopback_http)


_CONFIG = EventsConfig()
HOSTS_ENV = "BRIGADE_FLEET_EVENTS_CALLBACK_HOSTS"
LOOPBACK_ENV = "BRIGADE_FLEET_EVENTS_ALLOW_LOOPBACK_HTTP"
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)*$")


def config_from_environment(environ: Mapping[str, str]) -> EventsConfig:
    """Read the operator opt-in. Hosts are exact names (comma separated); wildcards and URLs are refused."""
    hosts = frozenset(part.strip().lower() for part in environ.get(HOSTS_ENV, "").split(",") if part.strip())
    for host in hosts:
        if not _HOST_RE.fullmatch(host) and host not in ("::1",):
            raise ValueError(f"{HOSTS_ENV} entries must be exact host names; refused {host!r}")
    loopback = environ.get(LOOPBACK_ENV, "").strip() == "1"
    if loopback and not hosts:
        raise ValueError(f"{LOOPBACK_ENV} has no effect without {HOSTS_ENV}")
    return EventsConfig(hosts, loopback)


def configure(config: EventsConfig) -> None:
    global _CONFIG
    _CONFIG = config


def current() -> EventsConfig:
    return _CONFIG


class EventsError(Exception):
    def __init__(self, status: int, code: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": 128, "description": description}


def definitions() -> list[dict[str, Any]]:
    """Event catalog for ``events/list``: arguments in ``inputSchema``, ``data`` shape in ``payloadSchema``."""
    node = _text("Hub node id.")
    return [
        {
            "name": MESSAGE_CREATED,
            "description": (
                "A message was delivered to this principal's inbox. The payload carries ids only; "
                "read the message with the inbox tool."
            ),
            "delivery": ["webhook"],
            "inputSchema": {
                "type": "object",
                "properties": {"recipient": _text("Inbox to watch. Defaults to, and must equal, the caller.")},
                "additionalProperties": False,
            },
            "payloadSchema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "recipient": {"type": "string"},
                    "sender": {"type": "string"},
                    "priority": {"enum": ["low", "normal", "high", "urgent"]},
                    "kind": {"enum": ["message", "question", "completion"]},
                    "created_at": {"type": "string"},
                },
                "required": ["message_id", "recipient", "sender", "priority", "created_at"],
                "additionalProperties": False,
            },
        },
        {
            "name": CLAIM_EXPIRED,
            "description": "A repository claim ran past its lease without a renewal or release.",
            "delivery": ["webhook"],
            "inputSchema": {
                "type": "object",
                "properties": {"owner_node": node},
                "additionalProperties": False,
            },
            "payloadSchema": {
                "type": "object",
                "properties": {
                    "owner_node": {"type": "string"},
                    "target": {"type": "string"},
                    "target_digest": {"type": "string"},
                    "expired_at": {"type": "string"},
                },
                "required": ["owner_node", "target_digest", "expired_at"],
                "additionalProperties": False,
            },
        },
        {
            "name": RUN_FAILED,
            "description": "A run on a fleet node reported run.failed.",
            "delivery": ["webhook"],
            "inputSchema": {
                "type": "object",
                "properties": {"node_id": node},
                "additionalProperties": False,
            },
            "payloadSchema": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "run_id": {"type": "string"},
                    "exit_status": {"type": "integer"},
                    "failed_at": {"type": "string"},
                },
                "required": ["node_id"],
                "additionalProperties": False,
            },
        },
    ]


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def permits(conn: sqlite3.Connection, principal: str, subject: str) -> bool:
    """Same visibility the claims and sessions station reads apply: agents see only what their grants name."""
    if not principal.startswith("agent:"):
        return True
    from . import fleet_hub_message_grants as grants

    return grants.permits(grants.grants_for(conn, principal), principal, subject)


# --------------------------------------------------------------------------------------
# Recording (called inside the transaction that made the fact true)
# --------------------------------------------------------------------------------------


def has_subscribers(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM fleet_event_subscriptions WHERE name=? AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
            (name, time.time()),
        ).fetchone()
        is not None
    )


def _append(
    conn: sqlite3.Connection, name: str, subject: str, data: dict[str, Any], *, event_id: str | None = None
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO fleet_events(event_id,name,subject,data,created_at) VALUES(?,?,?,?,?)",
        (event_id or "evt_" + uuid4().hex, name, subject, json.dumps(data, separators=(",", ":")), time.time()),
    )


def record_message_created(conn: sqlite3.Connection, message: dict[str, Any]) -> None:
    if not _CONFIG.enabled or not has_subscribers(conn, MESSAGE_CREATED):
        return
    data: dict[str, Any] = {
        "message_id": str(message["id"]),
        "recipient": message["recipient"],
        "sender": message["sender"],
        "priority": message["priority"],
        "created_at": message["created_at"],
    }
    if message.get("kind"):
        data["kind"] = message["kind"]
    _append(conn, MESSAGE_CREATED, message["recipient"], data)


def record_run_failed(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    if not _CONFIG.enabled or not has_subscribers(conn, RUN_FAILED):
        return
    data: dict[str, Any] = {"node_id": event["node_id"]}
    if _SAFE_TOKEN_RE.fullmatch(str(event.get("run_id", ""))):
        data["run_id"] = event["run_id"]
    if isinstance(event.get("exit_status"), int):
        data["exit_status"] = event["exit_status"]
    if _TIMESTAMP_RE.fullmatch(str(event.get("ts", ""))):
        data["failed_at"] = event["ts"]
    _append(conn, RUN_FAILED, event["node_id"], data)


def claim_expired_event_id(target: str, holder_token: str, expires_at: float) -> str:
    """One id per lease expiry, so the sweep and the prune path cannot emit it twice."""
    digest = hashlib.sha256(json.dumps([target, holder_token, expires_at]).encode()).hexdigest()
    return "evt_claim_" + digest[:32]


def record_claim_expired(
    conn: sqlite3.Connection, target: str, owner_node: str, holder_token: str, expires_at: float
) -> None:
    if not _CONFIG.enabled or not has_subscribers(conn, CLAIM_EXPIRED):
        return
    data: dict[str, Any] = {
        "owner_node": owner_node,
        "target_digest": hashlib.sha256(target.encode()).hexdigest()[:16],
        "expired_at": iso(expires_at),
    }
    if _SAFE_TOKEN_RE.fullmatch(target):
        data["target"] = target
    _append(conn, CLAIM_EXPIRED, owner_node, data, event_id=claim_expired_event_id(target, holder_token, expires_at))


# --------------------------------------------------------------------------------------
# events/list, events/subscribe, events/unsubscribe
# --------------------------------------------------------------------------------------


def _normalized_arguments(name: str, arguments: Any, caller: str) -> tuple[dict[str, str], str | None]:
    """Validate filters and apply the authorization rule for each event. Returns (arguments, filter value)."""
    allowed = {MESSAGE_CREATED: {"recipient"}, CLAIM_EXPIRED: {"owner_node"}, RUN_FAILED: {"node_id"}}[name]
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise EventsError(400, "events_invalid_arguments", "invalid_arguments")
    for value in arguments.values():
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise EventsError(400, "events_invalid_arguments", "invalid_arguments")
    if name == MESSAGE_CREATED:
        recipient = arguments.get("recipient", caller)
        if recipient != caller:
            # An inbox is private to its owner; there is no grant that widens this.
            raise EventsError(403, "events_forbidden", "forbidden")
        return {"recipient": caller}, caller
    key = next(iter(allowed))
    value = arguments.get(key)
    return ({key: value}, value) if value is not None else ({}, None)


def _parse_delivery(delivery: Any, *, with_secret: bool) -> dict[str, str]:
    fields = {"mode", "url", "secret"} if with_secret else {"mode", "url"}
    if not isinstance(delivery, dict) or set(delivery) != fields or delivery["mode"] != "webhook":
        raise EventsError(400, "events_invalid_arguments", "invalid_arguments")
    return {key: delivery[key] for key in fields}


def _ttl_seconds(params: dict[str, Any]) -> float:
    ttl = params.get("ttlMs", DEFAULT_TTL_MS)
    if ttl is None:
        # No expiration is never granted; the longest lifetime is.
        ttl = DEFAULT_TTL_MS
    if type(ttl) is not int:
        raise EventsError(400, "events_invalid_ttl", "invalid_ttl")
    return max(MIN_TTL_MS, min(ttl, DEFAULT_TTL_MS)) / 1000


def _verify_callback(url: str, secret: str, sid: str) -> None:
    """Send a signed single-use challenge and require the same value back; no data is sent before this passes."""
    challenge = secrets.token_urlsafe(32)
    body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
    headers = signed_headers(secret, "msg_verification_" + uuid4().hex, body, sid)
    if not _VERIFY_SLOTS.acquire(blocking=False):
        raise EventsError(503, "events_busy", "busy")
    try:
        status, result = post_webhook(url, body, headers, _CONFIG.policy())
    except TimeoutError:
        raise EventsError(400, "events_callback_timeout", "timeout") from None
    except ssl.SSLError:
        raise EventsError(400, "events_callback_tls_error", "tls_error") from None
    except ConnectionRefusedError:
        raise EventsError(400, "events_callback_connection_refused", "connection_refused") from None
    except (OSError, ValueError):
        raise EventsError(400, "events_callback_challenge_failed", "challenge_failed") from None
    finally:
        _VERIFY_SLOTS.release()
    if not 200 <= status < 300:
        reason = "http_4xx" if 400 <= status < 500 else "http_5xx" if status >= 500 else "http_status"
        raise EventsError(400, "events_callback_" + reason, reason)
    try:
        echoed = json.loads(result).get("challenge")
    except (ValueError, AttributeError):
        raise EventsError(400, "events_callback_response_not_json", "response_not_json") from None
    if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
        raise EventsError(400, "events_callback_challenge_mismatch", "challenge_mismatch")


def _subscribe(conn: sqlite3.Connection, params: dict[str, Any], caller: str) -> dict[str, Any]:
    from . import fleet_hub_messages as messages

    if set(params) - {"name", "arguments", "delivery", "cursor", "ttlMs"} or params.get("cursor") is not None:
        raise EventsError(400, "events_invalid_arguments", "invalid_arguments")
    name = params.get("name")
    if name not in EVENT_NAMES:
        raise EventsError(400, "events_invalid_arguments", "invalid_arguments")
    arguments, filter_value = _normalized_arguments(name, params.get("arguments", {}), caller)
    delivery = _parse_delivery(params.get("delivery"), with_secret=True)
    ttl = _ttl_seconds(params)
    try:
        parse_callback(delivery["url"], _CONFIG.policy())
        secret_key(delivery["secret"])
    except (ValueError, TypeError):
        raise EventsError(400, "events_invalid_delivery", "invalid_delivery") from None
    canonical = canonical_arguments(arguments)
    sid = subscription_id(caller, delivery["url"], name, canonical)

    def known() -> sqlite3.Row | tuple[Any, ...] | None:
        return conn.execute(
            "SELECT secret, expires_at FROM fleet_event_subscriptions WHERE id=? AND principal_id=?", (sid, caller)
        ).fetchone()

    existing = messages._as_active_principal(conn, caller, known, read_only=True)
    now = time.time()
    # A refresh with the same identity and secret was verified when it was created.
    if not (existing and existing[0] == delivery["secret"] and (existing[1] is None or existing[1] > now)):
        _verify_callback(delivery["url"], delivery["secret"], sid)

    def store() -> float:
        moment = time.time()
        expires = moment + ttl
        old = conn.execute(
            "SELECT secret, previous_secret, rotate_until, created_at, cursor_seq FROM fleet_event_subscriptions "
            "WHERE id=?",
            (sid,),
        ).fetchone()
        if old is None:
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM fleet_event_subscriptions WHERE principal_id=?", (caller,)
                ).fetchone()[0]
                >= MAX_SUBSCRIPTIONS_PER_PRINCIPAL
                or conn.execute("SELECT COUNT(*) FROM fleet_event_subscriptions").fetchone()[0] >= MAX_SUBSCRIPTIONS
            ):
                raise EventsError(429, "events_subscription_limit", "subscription_limit")
        previous, rotate_until = None, None
        if old is not None and old[0] != delivery["secret"]:
            previous, rotate_until = old[0], moment + ROTATION_WINDOW_SECONDS
        elif old is not None and old[2] and old[2] > moment:
            previous, rotate_until = old[1], old[2]
        cursor_seq = (
            old[4] if old is not None else conn.execute("SELECT COALESCE(MAX(seq),0) FROM fleet_events").fetchone()[0]
        )
        conn.execute(
            "INSERT OR REPLACE INTO fleet_event_subscriptions(id,principal_id,name,arguments,filter_value,url,secret,"
            "previous_secret,rotate_until,created_at,expires_at,cursor_seq,failures) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                sid,
                caller,
                name,
                canonical,
                filter_value,
                delivery["url"],
                delivery["secret"],
                previous,
                rotate_until,
                old[3] if old is not None else moment,
                expires,
                cursor_seq,
            ),
        )
        return expires

    expires = messages._as_active_principal(conn, caller, store)
    return {"id": sid, "refreshBefore": iso(expires), "cursor": None, "truncated": False}


def _unsubscribe(conn: sqlite3.Connection, params: dict[str, Any], caller: str) -> dict[str, Any]:
    from . import fleet_hub_messages as messages

    if set(params) == {"id"} and isinstance(params["id"], str):
        sid = params["id"]
    else:
        name = params.get("name")
        if set(params) - {"name", "arguments", "delivery"} or name not in EVENT_NAMES:
            raise EventsError(400, "events_invalid_arguments", "invalid_arguments")
        arguments, _ = _normalized_arguments(name, params.get("arguments", {}), caller)
        delivery = _parse_delivery(params.get("delivery"), with_secret=False)
        sid = subscription_id(caller, delivery["url"], name, canonical_arguments(arguments))

    def remove() -> None:
        # The owner column is the authorization: another principal's id deletes nothing.
        conn.execute("DELETE FROM fleet_event_subscriptions WHERE id=? AND principal_id=?", (sid, caller))
        conn.execute(
            "DELETE FROM fleet_event_deliveries WHERE subscription_id=? "
            "AND subscription_id NOT IN (SELECT id FROM fleet_event_subscriptions)",
            (sid,),
        )

    messages._as_active_principal(conn, caller, remove)
    return {}


def handle(conn: sqlite3.Connection, raw: dict[str, Any], *, caller: str) -> tuple[int, dict[str, Any]]:
    """Hub-side handler for the events_* message actions; ``caller`` is the authenticated principal."""
    if not _CONFIG.enabled:
        return 404, {"error": "events are disabled on this hub", "code": "events_disabled"}
    params = {key: value for key, value in raw.items() if key != "action"}
    try:
        action = raw.get("action")
        if action == "events_list":
            if params:
                raise EventsError(400, "events_invalid_arguments", "invalid_arguments")
            return 200, {"events": definitions()}
        if action == "events_subscribe":
            return 200, _subscribe(conn, params, caller)
        if action == "events_unsubscribe":
            return 200, _unsubscribe(conn, params, caller)
    except EventsError as error:
        return error.status, {"error": error.reason, "code": error.code}
    return 400, {"error": "unknown events action", "code": "events_invalid_arguments"}
