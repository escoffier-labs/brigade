"""Durable hub inbox mirror and direct fleet message transport."""

from __future__ import annotations

import builtins
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Callable, Iterator

from brigade import fleet_client_messages
from brigade.fleet_client import FleetClientError
from brigade.fleet_client_message_credentials import ENV_TOKEN_FILE, FleetMessageHubError

from .contracts import MAX_MESSAGE_BYTES, MESSAGE_BODY, THREAD, DotError, _text, require
from .runtime_config import HubConfig
from .state import BridgeState, _iso, utc_now

LOCAL_CLI_CODES = {
    "token_file_invalid": "hub_credential_invalid",
    "admin_token_refused": "hub_credential_invalid",
    "spool_conflict": "client_id_conflict",
    "client_id_invalid": "invalid_request",
    "invalid_argument": "invalid_request",
}


def sender_name(row: dict[str, Any], node_id: str) -> str:
    label = row.get("sender_label")
    sender = row["sender"]
    return label if label and sender == node_id else f"{label}@{sender}" if label else sender


def legacy_message(row: dict[str, Any], node_id: str) -> dict[str, Any]:
    body = row["body"]
    raw = body.encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        suffix = "\n[truncated; hub message " + str(row["id"]) + "]"
        body = raw[: MAX_MESSAGE_BYTES - len(suffix.encode())].decode("utf-8", errors="ignore") + suffix
    return dict(
        id=row["id"],
        created_at=row["created_at"],
        sender=sender_name(row, node_id),
        thread=row.get("thread"),
        body=body,
        acked_at=row.get("acked_at"),
        priority="urgent" if row.get("priority") in ("high", "urgent") else "normal",
        subject=row.get("subject"),
        ref=row.get("ref"),
        kind=row.get("kind") or "message",
    )


def truncate_subject(subject: str) -> str:
    return subject.encode("utf-8")[:200].decode("utf-8", errors="ignore")


def default_subject(body: str, kind: str, sender: str) -> str:
    return truncate_subject(next((line.strip() for line in body.splitlines() if line.strip()), f"{kind} from {sender}"))


class HubInbox:
    def __init__(self, state: BridgeState, hub: HubConfig, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.state, self.config, self.clock = state, hub, clock
        self._sync_lock = threading.Lock()
        self._reconcile_lock = threading.Lock()

    def _call(self, action: str, *args: Any, agent: bool = True, **kwargs: Any) -> Any:
        if not agent:
            require(ENV_TOKEN_FILE not in os.environ, "hub_credential_invalid")
        token_file = str(self.config.message_token_file) if agent else None
        try:
            value = getattr(fleet_client_messages, action)(*args, token_file=token_file, **kwargs)
        except FleetMessageHubError as error:
            code = {
                401: "hub_unauthorized",
                403: "hub_forbidden",
                404: "message_not_found",
                429: "hub_quota",
                400: "invalid_request",
                409: "invalid_request",
            }.get(error.status or 0, "hub_unavailable")
            code = {
                "message_recipient_not_permitted": "recipient_not_permitted",
                "message_sender_recipient_quota": "recipient_quota",
            }.get(error.code or "", code)
            if error.status is None:
                code = LOCAL_CLI_CODES.get(error.code or "", code)
            raise DotError(code) from None
        except (FleetClientError, OSError, ValueError):
            raise DotError("hub_unavailable") from None
        if action != "flush_credential":
            require(isinstance(value, dict), "hub_unavailable")
        return value

    def _pages(
        self,
        *,
        after: int = 0,
        unacked: bool = False,
        agent: bool = True,
        max_pages: int = 1001,
        timeout: float = 45,
        deadline: float | None = None,
    ) -> Iterator[tuple[builtins.list[dict[str, Any]], int, datetime]]:
        for _ in range(max_pages):
            remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                return
            fetched_at = self.clock()  # A local ack after the fetch starts wins over this copy.
            value = self._call("list_messages", after=after, unacked=unacked, agent=agent, timeout=remaining)
            rows, cursor = value.get("messages"), value.get("next_cursor")
            require(isinstance(rows, list) and type(cursor) is int, "hub_unavailable")
            previous = after
            for row in rows:
                require(
                    isinstance(row, dict) and type(row.get("id")) is int and row["id"] > previous, "hub_inconsistent"
                )
                previous = row["id"]
                require(
                    row.get("recipient") == (self.config.principal if agent else self.config.node_id),
                    "hub_inconsistent",
                )
                require(all(isinstance(row.get(k), str) for k in ("sender", "body", "created_at")), "hub_inconsistent")
            require(cursor == previous, "hub_inconsistent")
            yield rows, cursor, fetched_at
            if not rows:
                return
            after = cursor
        if max_pages == 1001:
            raise DotError("hub_inconsistent")

    def sync(
        self,
        *,
        max_pages: int = 10,
        timeout: float = 45,
        lock_timeout: float | None = None,
        deadline: float | None = None,
        after: int | None = None,
    ) -> None:
        acquired = self._sync_lock.acquire() if lock_timeout is None else self._sync_lock.acquire(timeout=lock_timeout)
        if not acquired:
            return
        try:
            try:
                after = int(self.state.hub_get("cursor", "0") or "0") if after is None else after
                for rows, cursor, fetched_at in self._pages(
                    after=after, max_pages=max_pages, timeout=timeout, deadline=deadline
                ):
                    with self.state.transaction() as c:
                        self.state.hub_upsert(
                            rows, node_id=self.config.node_id, clock=self.clock, connection=c, fetched_at=fetched_at
                        )
                        self.state.hub_set("cursor", cursor, connection=c)
                        self.state.hub_set("last_sync_ok_at", _iso(self.clock()), connection=c)
                        self.state.hub_set("last_sync_error", "", connection=c)
            except DotError as error:
                self.state.hub_set("last_sync_error", str(error))
                raise
        finally:
            self._sync_lock.release()

    def reconcile(self) -> None:
        # Independent of foreground sync; never hold its lock during network IO.
        if not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            after = int(self.state.hub_get("reconcile_cursor", "0") or "0")
            try:
                for rows, cursor, fetched_at in self._pages(
                    after=after, max_pages=10, timeout=2, deadline=time.monotonic() + 20
                ):
                    with self.state.transaction() as c:
                        self.state.hub_upsert(
                            rows, node_id=self.config.node_id, clock=self.clock, connection=c, fetched_at=fetched_at
                        )
                        self.state.hub_set("reconcile_cursor", cursor if rows else 0, connection=c)
            except DotError as error:
                # A broken credential must not read as an outage in status or alerts.
                self.state.hub_set("last_reconcile_error", str(error))
                raise
            self.state.hub_set("last_reconcile_error", "")
            # Absence is never evidence of ack, including an empty/wrong inbox.
            with self.state.transaction() as c:
                c.execute("DELETE FROM hub_inbox WHERE acked_at < ?", (_iso(self.clock() - timedelta(days=90)),))
            self._sweep_node_replies()
        finally:
            self._reconcile_lock.release()

    def _sweep_node_replies(self) -> None:
        swept = 0
        deadline = time.monotonic() + 10
        cutoff = self.clock() - timedelta(hours=24)
        after = int(self.state.hub_get("node_sweep_cursor", "0") or "0")
        for rows, _, _ in self._pages(
            after=after, unacked=True, agent=False, max_pages=2, timeout=2, deadline=deadline
        ):
            if not rows:
                self.state.hub_set("node_sweep_cursor", "0")
            for row in rows:
                if time.monotonic() >= deadline:
                    return
                try:
                    old = datetime.fromisoformat(row["created_at"]).astimezone(timezone.utc) < cutoff
                except (ValueError, TypeError):
                    raise DotError("hub_inconsistent") from None
                if row["sender"] == self.config.principal and row.get("reply_to") and old:
                    self._call("ack", row["id"], agent=False, timeout=max(0.1, min(2.0, deadline - time.monotonic())))
                    swept += 1
                self.state.hub_set("node_sweep_cursor", row["id"])
                if swept >= 5:
                    return

    def list(self, unacked_only: bool = True, limit: int = 20) -> dict[str, Any]:
        try:
            # One page and at most 0.1 s for a competing sync.
            # The deadline bounds pagination and each request's transport timeout.
            self.sync(max_pages=1, timeout=1.5, lock_timeout=0.1, deadline=time.monotonic() + 1.6)
        except DotError:
            pass
        return self.state.hub_messages(unacked_only=unacked_only, limit=limit, node_id=self.config.node_id)

    def ack(self, ids: builtins.list[int]) -> dict[str, Any]:
        with self.state.transaction() as c:
            known = {row["id"]: row["acked_at"] for row in c.execute("SELECT id,acked_at FROM hub_inbox")}
        acked, not_pending = [], []
        refreshed = False
        for ident in ids:
            if ident not in known and not refreshed:
                self.sync()
                refreshed = True
                with self.state.transaction() as c:
                    known = {row["id"]: row["acked_at"] for row in c.execute("SELECT id,acked_at FROM hub_inbox")}
            if ident not in known or known[ident] is not None:
                not_pending.append(ident)
                continue
            try:
                self._call("ack", ident)
            except DotError as error:
                if str(error) != "message_not_found":
                    raise
                self.state.hub_mark_acked([ident], clock=self.clock)
                known[ident] = _iso(self.clock())
                not_pending.append(ident)
                continue
            self.state.hub_mark_acked([ident], clock=self.clock)
            known[ident] = _iso(self.clock())
            acked.append(ident)
        listing = self.state.hub_messages(unacked_only=True, limit=1, node_id=self.config.node_id)
        return dict(acked=acked, not_pending=not_pending, unacked_total=listing["unacked_total"])

    def _sent_message(self, value: dict[str, Any], *, agent: bool) -> dict[str, Any] | None:
        if value.get("queued") is True:
            self.state.hub_set("agent_spool_pending" if agent else "node_spool_pending", "1")
            return None
        row = value.get("message")
        require(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and row["id"] > 0
            and isinstance(row.get("created_at"), str),
            "hub_unavailable",
        )
        return row

    def reply(self, msg_id: int, body: str) -> dict[str, Any]:
        cid = "mr-" + hashlib.sha256(f"{msg_id}\n{body}".encode()).hexdigest()[:40]
        value = self._call(
            "reply",
            msg_id,
            body=body,
            subject=None,
            reference=None,
            priority="normal",
            thread=None,
            kind="message",
            sender_label=None,
            client_id=cid,
        )
        row = self._sent_message(value, agent=True)
        result: dict[str, Any] = dict(
            id=row["id"] if row else None,
            msg_id=msg_id,
            created_at=row["created_at"] if row else _iso(self.clock()),
            body=body,
        )
        if row is None:
            result["queued"] = True
        return result

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        ref: str | None = None,
        priority: str = "normal",
        thread: str | None = None,
        kind: str = "message",
        client_id: str | None = None,
    ) -> dict[str, Any]:
        # Node names from hub_recipients resolve to node ids; grants name node ids.
        recipient = dict(self.config.recipients).get(to, to)
        cid = (
            client_id
            or "ms-"
            + hashlib.sha256(
                json.dumps(
                    [recipient, subject, body, ref, priority, thread, kind, int(self.clock().timestamp()) // 600]
                ).encode()
            ).hexdigest()[:40]
        )
        row = self._sent_message(
            self._call(
                "send",
                recipient=recipient,
                subject=subject,
                body=body,
                reference=ref,
                priority=priority,
                thread=thread,
                kind=kind,
                sender_label=None,
                client_id=cid,
            ),
            agent=True,
        )
        return dict(
            id=row["id"] if row else None,
            to=to,
            recipient=recipient,
            created_at=row["created_at"] if row else _iso(self.clock()),
            client_id=cid,
            queued=row is None,
        )

    def status(self, ids: builtins.list[int]) -> dict[str, Any]:
        value = self._call("sent", ids=ids)
        require(isinstance(value.get("messages"), list), "hub_unavailable")
        keys = ("id", "subject", "created_at", "acked_at", "acked_by", "reply_to")
        names = {node: name for name, node in self.config.recipients.items()}
        return dict(
            messages=[
                dict(to=names.get(row.get("recipient"), row.get("recipient")), **{k: row.get(k) for k in keys})
                for row in value["messages"]
            ]
        )

    def send_completion(self, request_id: str, generation: int, state: str) -> bool:
        cid = "dbc-" + hashlib.sha256(f"{request_id}:{generation}".encode()).hexdigest()[:40]
        value = self._call(
            "send",
            recipient=self.config.principal,
            sender_label="dot-bridge",
            kind="completion",
            thread=request_id,
            reference=request_id,
            subject="Fleet request completed",
            body="Fleet request reached terminal state: " + state,
            priority="normal",
            client_id=cid,
            agent=False,
        )
        self._sent_message(value, agent=False)
        return True

    def flush_pending(self) -> None:
        first_error: DotError | None = None
        for key, agent in (("agent_spool_pending", True), ("node_spool_pending", False)):
            if self.state.hub_get(key) == "1":
                try:
                    self._call("flush_credential", agent=agent)
                except DotError as error:
                    first_error = first_error or error
                else:
                    self.state.hub_set(key, "0")
        if first_error is not None:
            raise first_error

    def notify(
        self,
        sender: str,
        body: str,
        thread: str | None,
        priority: str,
        subject: str | None,
        ref: str | None,
        kind: str,
        client_id: str,
    ) -> dict[str, Any] | None:
        """Node send for the notify entry point; never uses the agent file."""
        require(isinstance(sender, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", sender) is not None)
        MESSAGE_BODY(body)
        if thread is not None:
            THREAD(thread)
        if ref is not None:
            THREAD(ref)
        subject = _text(max_bytes=200)(subject) if subject is not None else default_subject(body, kind, sender)
        value = self._call(
            "send",
            recipient=self.config.principal,
            sender_label=sender,
            subject=subject,
            body=body,
            reference=ref,
            priority=priority,
            thread=thread,
            kind=kind,
            client_id=client_id,
            agent=False,
        )
        row = self._sent_message(value, agent=False)
        if row is not None:
            require(row.get("recipient") == self.config.principal, "hub_inconsistent")
            self.state.hub_upsert([row], node_id=self.config.node_id, clock=self.clock)
        return row

    def wait_replies(
        self, msg_id: int, cursor: int, *, deadline: float | None = None
    ) -> tuple[builtins.list[dict[str, Any]], int]:
        replies: builtins.list[dict[str, Any]] = []
        for rows, next_cursor, _ in self._pages(after=cursor, agent=False, max_pages=5, deadline=deadline):
            cursor = next_cursor
            replies.extend(
                dict(id=row["id"], msg_id=msg_id, created_at=row["created_at"], body=row["body"])
                for row in rows
                if row.get("reply_to") == msg_id and row["sender"] == self.config.principal
            )
        for row in replies:
            if deadline is not None and time.monotonic() >= deadline:
                break  # Unacked replies are still returned; the next poll acks them.
            bound = {} if deadline is None else {"timeout": max(0.1, min(45.0, deadline - time.monotonic()))}
            try:
                self._call("ack", row["id"], agent=False, **bound)
            except DotError:
                pass
        return replies, cursor
