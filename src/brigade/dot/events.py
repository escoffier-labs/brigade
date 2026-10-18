"""Durable MCP event subscriptions and delivery ported from the bridge."""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from functools import partial
import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import ssl
import time
from typing import Any, Awaitable, Callable, Protocol
from uuid import uuid4
from .contracts import DotError
from .event_app import EventError, EventsApp as EventsApp, IDENTITY as IDENTITY
from .state import BridgeState
from .webhooks import callback_url, secret_key, signed_headers, webhook_post

EVENT_NAMES = ("fleet.inbox.message", "fleet.request.completed")
TERMINAL = frozenset({"succeeded", "failed", "interrupted", "cancelled", "completed"})
WebhookPost = Callable[[str, bytes, dict[str, str]], Awaitable[tuple[int, bytes]]]


class HubSync(Protocol):
    def sync(self) -> None: ...
    def reconcile(self) -> None: ...
    def flush_pending(self) -> None: ...
    def send_completion(self, request_id: str, generation: int, state: str) -> Any: ...


def enqueue(
    connection: sqlite3.Connection,
    name: str,
    data: dict[str, Any],
    priority: str,
    source: str,
    now: float | None = None,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO event_queue VALUES (?,?,?,?,?,?)",
        (
            "evt_" + uuid4().hex,
            name,
            json.dumps(data, separators=(",", ":")),
            priority,
            time.time() if now is None else now,
            source,
        ),
    )


class EventService:
    def __init__(
        self,
        state: BridgeState,
        *,
        fleet_status: Callable[[str], dict[str, Any]],
        hub: HubSync | None,
        hub_poll_seconds: int,
        callback_hosts: frozenset[str],
        post: WebhookPost = webhook_post,
    ) -> None:
        self._hub_polls = 0
        self.state, self.fleet_status, self.hub = (state, fleet_status, hub)
        self.hub_poll_seconds, self.callback_hosts = (hub_poll_seconds, callback_hosts)
        self.post: WebhookPost = partial(webhook_post, allowed_hosts=callback_hosts) if post is webhook_post else post

    async def rpc(self, owner: str | None, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "events/list":
            if set(params) - {"cursor"} or params.get("cursor") not in (None, ""):
                raise EventError(-32602, "invalid_arguments")
            return {
                "events": [
                    dict(
                        name=name,
                        description="Fleet wake. Always read inbox_list on wake; payload may be absent in dots.",
                        delivery=["webhook"],
                        inputSchema={
                            "type": "object",
                            "properties": {"min_priority": {"enum": ["normal", "urgent"]}}
                            if name == EVENT_NAMES[0]
                            else {},
                            "additionalProperties": False,
                        },
                        payloadSchema={
                            "type": "object",
                            "properties": {
                                "msg_id": {"type": "integer"},
                                "request_id": {"type": "string"},
                                "priority": {"enum": ["normal", "urgent"]},
                                "state": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    )
                    for name in EVENT_NAMES
                ]
            }
        if method == "events/unsubscribe":
            if set(params) == {"id"} and isinstance(params["id"], str):
                sid = params["id"]
            else:
                name, arguments, delivery = (params.get("name"), params.get("arguments", {}), params.get("delivery"))
                if (
                    set(params) - {"name", "arguments", "delivery"}
                    or not isinstance(name, str)
                    or (not isinstance(arguments, dict))
                    or (not isinstance(delivery, dict))
                    or set(delivery) - {"mode", "url"}
                    or (delivery.get("mode", "webhook") != "webhook")
                    or (not isinstance(delivery.get("url"), str))
                ):
                    raise EventError(-32602, "invalid_arguments")
                canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
                sid = (
                    "sub_" + hashlib.sha256(json.dumps([owner, delivery["url"], name, canonical]).encode()).hexdigest()
                )
            with self.state.transaction() as c:
                c.execute("DELETE FROM subscriptions WHERE id=? AND owner=?", (sid, owner))
            return {}
        if method != "events/subscribe":
            raise EventError(-32601, "method_not_found")
        if set(params) - {"name", "arguments", "delivery", "cursor", "ttlMs"}:
            raise EventError(-32602, "invalid_arguments")
        name, arguments, delivery = (params.get("name"), params.get("arguments", {}), params.get("delivery"))
        if name not in EVENT_NAMES or not isinstance(arguments, dict) or (not isinstance(delivery, dict)):
            raise EventError(-32602, "invalid_arguments")
        if (
            set(arguments) - ({"min_priority"} if name == EVENT_NAMES[0] else set())
            or arguments.get("min_priority", "normal") not in ("normal", "urgent")
            or set(delivery) != {"mode", "url", "secret"}
            or (delivery["mode"] != "webhook")
            or (params.get("cursor") is not None)
        ):
            raise EventError(-32602, "invalid_arguments")
        ttl = params.get("ttlMs", 7 * 86400000)
        if ttl is not None and (type(ttl) is not int or not 60000 <= ttl <= 7 * 86400000):
            raise EventError(-32602, "invalid_ttl")
        try:
            callback_url(delivery["url"], self.callback_hosts)
            secret_key(delivery["secret"])
        except (ValueError, TypeError):
            raise EventError(-32602, "invalid_delivery") from None
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        sid = "sub_" + hashlib.sha256(json.dumps([owner, delivery["url"], name, canonical]).encode()).hexdigest()
        challenge = secrets.token_urlsafe(32)
        body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
        status: int | None = None
        reason = "challenge_failed"
        try:
            status, result = await self.post(
                delivery["url"], body, signed_headers(delivery["secret"], "msg_verification_" + uuid4().hex, body, sid)
            )
            if not 200 <= status < 300:
                reason = "http_4xx" if 400 <= status < 500 else "http_5xx" if status >= 500 else "http_status"
                raise ValueError(reason)
            try:
                echo = json.loads(result).get("challenge")
            except (ValueError, AttributeError):
                reason = "response_not_json"
                raise ValueError(reason) from None
            if not isinstance(echo, str) or not hmac.compare_digest(echo, challenge):
                reason = "challenge_mismatch"
                raise ValueError(reason)
        except Exception as error:
            if isinstance(error, TimeoutError):
                reason = "timeout"
            elif isinstance(error, ssl.SSLError):
                reason = "tls_error"
            elif isinstance(error, OSError):
                reason = "connection_refused"
            detail = (
                str(error)
                if isinstance(error, ValueError)
                and re.fullmatch("[a-z][a-z0-9_]{2,40}|callback_response_unexpected_[A-Za-z]{1,30}", str(error))
                else ""
            )
            logging.getLogger("dot-events").warning(
                "event_callback_verification_failed reason=%s status=%s error=%s detail=%s",
                reason,
                status,
                type(error).__name__,
                detail,
            )
            raise EventError(-32015, reason) from None
        now = time.time()
        expires = now + ttl / 1000 if ttl is not None else None
        with self.state.transaction() as c:
            old = c.execute(
                "SELECT secret,previous_secret,rotate_until,created FROM subscriptions WHERE id=?", (sid,)
            ).fetchone()
            previous = (
                old["secret"]
                if old and old["secret"] != delivery["secret"]
                else old["previous_secret"]
                if old and old["rotate_until"] and (old["rotate_until"] > now)
                else None
            )
            rotate_until = (
                now + 300 if old and old["secret"] != delivery["secret"] else old["rotate_until"] if previous else None
            )
            if not old and c.execute("SELECT count(*) FROM subscriptions").fetchone()[0] >= 100:
                raise EventError(-32000, "subscription_limit")
            c.execute(
                "INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    owner,
                    name,
                    canonical,
                    delivery["url"],
                    delivery["secret"],
                    previous,
                    rotate_until,
                    expires,
                    old["created"] if old else now,
                ),
            )
        logging.getLogger("dot-events").info("event_subscription_verified")
        return dict(
            id=sid,
            refreshBefore=datetime.fromtimestamp(expires - min(3600, ttl / 10000), timezone.utc).isoformat()
            if expires and ttl is not None
            else None,
            cursor=None,
            truncated=False,
        )

    async def poll_requests(self) -> None:
        with self.state.transaction() as c:
            requests = [dict(row) for row in c.execute("SELECT * FROM tracked_requests WHERE completed=0 LIMIT 50")]
        for row in requests:
            result = await asyncio.to_thread(self.fleet_status, row["request_id"])
            status = result.get("result", {}).get("state") if result.get("ok") else None
            if status not in TERMINAL:
                continue
            hub = self.hub
            if hub is not None:
                try:
                    sent = await asyncio.to_thread(hub.send_completion, row["request_id"], row["generation"], status)
                except Exception:
                    continue
                if not sent:
                    continue
            with self.state.transaction() as c:
                current = c.execute(
                    "SELECT * FROM tracked_requests WHERE request_id=?", (row["request_id"],)
                ).fetchone()
                if not current or current["generation"] != row["generation"] or current["completed"]:
                    continue
                data = {"request_id": row["request_id"], "state": status, "priority": "normal"}
                if hub is None:
                    pending = c.execute("SELECT count(*) FROM inbox WHERE acked_at IS NULL").fetchone()[0]
                    if pending >= 1000:
                        continue
                    c.execute(
                        """INSERT INTO inbox(created_at,sender,thread,body,priority,subject,ref,kind)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            datetime.now(timezone.utc).isoformat(),
                            "dot-bridge",
                            row["request_id"],
                            "Fleet request reached terminal state: " + status,
                            "normal",
                            "Fleet request completed",
                            row["request_id"],
                            "completion",
                        ),
                    )
                enqueue(
                    c, EVENT_NAMES[1], data, "normal", "request:" + row["request_id"] + ":" + str(row["generation"])
                )
                c.execute("UPDATE tracked_requests SET completed=1 WHERE request_id=?", (row["request_id"],))

    async def deliver(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.state.transaction() as c:
            c.execute("DELETE FROM subscriptions WHERE expires IS NOT NULL AND expires <= ?", (now,))
            c.execute("DELETE FROM wake_attempts WHERE timestamp < ?", (now - 3600,))
            c.execute(
                """INSERT OR IGNORE INTO event_deliveries(event_id,subscription_id,state) SELECT q.id,s.id,'pending'
                FROM event_queue q JOIN subscriptions s ON q.name=s.name WHERE q.created>=s.created AND
                (q.priority='urgent' OR json_extract(s.arguments,'$.min_priority') IS NOT 'urgent')"""
            )
            rows = [
                dict(r)
                for r in c.execute(
                    """SELECT d.*,q.name,q.data,q.priority,q.created,s.url,s.secret,s.previous_secret,s.rotate_until
                    FROM event_deliveries d JOIN event_queue q ON q.id=d.event_id JOIN subscriptions s ON
                    s.id=d.subscription_id WHERE d.state='pending' AND d.next_attempt<=? ORDER BY
                    (q.priority='urgent') DESC,q.created LIMIT 100""",
                    (now,),
                )
            ]
        for row in rows:
            with self.state.transaction() as c:
                current = c.execute(
                    "SELECT state FROM event_deliveries WHERE event_id=? AND subscription_id=?",
                    (row["event_id"], row["subscription_id"]),
                ).fetchone()
                if not current or current["state"] != "pending":
                    continue
                count = c.execute("SELECT count(*) FROM wake_attempts").fetchone()[0]
                last_normal = c.execute("SELECT max(timestamp) FROM wake_attempts WHERE priority='normal'").fetchone()[
                    0
                ]
                retry_at = None
                if count >= 30:
                    oldest = c.execute("SELECT min(timestamp) FROM wake_attempts").fetchone()[0]
                    retry_at = oldest + 3600
                if row["priority"] == "normal" and last_normal is not None and (now - last_normal < 300):
                    retry_at = max(retry_at or 0, last_normal + 300)
                if retry_at is not None:
                    # Defer to the window end so the row is not reselected and rewritten every second.
                    c.execute(
                        "UPDATE event_deliveries SET next_attempt=? WHERE event_id=? AND subscription_id=?",
                        (retry_at, row["event_id"], row["subscription_id"]),
                    )
                    continue
                # Reserve failures too, so retries cannot exceed the hourly wake ceiling.
                c.execute("INSERT INTO wake_attempts VALUES (?,?)", (now, row["priority"]))
            body = json.dumps(
                dict(
                    eventId=row["event_id"],
                    name=row["name"],
                    timestamp=datetime.fromtimestamp(row["created"], timezone.utc).isoformat(),
                    data=json.loads(row["data"]),
                    cursor=None,
                ),
                separators=(",", ":"),
            ).encode()
            previous = row["previous_secret"] if row["rotate_until"] and row["rotate_until"] > now else None
            try:
                status, _ = await self.post(
                    row["url"],
                    body,
                    signed_headers(
                        row["secret"], row["event_id"], body, row["subscription_id"], now=now, previous=previous
                    ),
                )
            except Exception:
                status = 503
            done = 200 <= status < 300
            permanent = status in (410, 413) or (400 <= status < 500 and status not in (408, 429))
            attempts = row["attempts"] + 1
            with self.state.transaction() as c:
                c.execute(
                    """UPDATE event_deliveries SET state=?,attempts=?,next_attempt=? WHERE event_id=? AND
                    subscription_id=?""",
                    (
                        "delivered" if done else "failed" if permanent else "pending",
                        attempts,
                        now + min(3600, 30 * 2 ** min(attempts, 7)),
                        row["event_id"],
                        row["subscription_id"],
                    ),
                )
                # A thin wake covers the subscription's entire pending inbox.
                if done and row["priority"] == "normal":
                    c.execute(
                        """UPDATE event_deliveries SET state='coalesced' WHERE state='pending' AND subscription_id=?
                        AND event_id IN (SELECT id FROM event_queue WHERE priority='normal' AND created<=?)""",
                        (row["subscription_id"], now),
                    )
                if status == 410:
                    c.execute("DELETE FROM subscriptions WHERE id=?", (row["subscription_id"],))
        with self.state.transaction() as c:
            c.execute(
                """DELETE FROM event_queue WHERE created<? AND id NOT IN (SELECT event_id FROM event_deliveries WHERE
                state='pending')""",
                (now - 30 * 86400,),
            )
            c.execute(
                """DELETE FROM event_deliveries WHERE event_id NOT IN (SELECT id FROM event_queue) OR subscription_id
                NOT IN (SELECT id FROM subscriptions)"""
            )

    async def periodic(self, action: Callable[[], Awaitable[None]], interval: int) -> None:
        while True:
            try:
                await action()
            except Exception:
                logging.getLogger("dot-events").warning("event_worker_retry")
            await asyncio.sleep(interval)

    async def poll_hub(self) -> None:
        hub = self.hub
        if hub is None:
            return
        self._hub_polls += 1
        actions = [hub.sync]
        if self._hub_polls % 20 == 0:
            actions.append(hub.reconcile)
        actions.append(hub.flush_pending)
        for action in actions:
            try:
                await asyncio.to_thread(action)
            except DotError as error:
                if error.code == "hub_credential_invalid":
                    logging.getLogger("dot-events").warning("hub_credential_invalid")

    async def run(self) -> None:
        import anyio

        async with anyio.create_task_group() as group:
            group.start_soon(self.periodic, self.poll_requests, 30)
            group.start_soon(self.periodic, self.deliver, 1)
            hub = self.hub
            if hub is not None:
                group.start_soon(self.periodic, self.poll_hub, self.hub_poll_seconds)
