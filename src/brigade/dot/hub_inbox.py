"""Durable hub inbox mirror and direct fleet message transport."""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import threading
import time
from typing import Any, Callable, Iterator, Mapping

from brigade.fleet_client import FleetClientError
from brigade.fleet_client_message_credentials import ENV_TOKEN_FILE, message_scope, message_settings
from brigade.hub_messages import (
    LOCAL_CLI_CODES as LOCAL_CLI_CODES,
    HubMessageError,
    MessageOperations,
    call_message,
    inbox_page,
    default_subject as default_subject,
    legacy_message as legacy_message,
    sender_name as sender_name,
    truncate_subject as truncate_subject,
)

from .contracts import MESSAGE_BODY, THREAD, DotError, _text, require
from .runtime_config import HubConfig
from .state import BridgeState, _iso, utc_now


class HubInbox(MessageOperations):
    def __init__(self, state: BridgeState, hub: HubConfig, *, clock: Callable[[], datetime] = utc_now) -> None:
        super().__init__(token_file=str(hub.message_token_file), recipients=hub.recipients, clock=clock)
        self.state, self.config = state, hub
        self._sync_lock = threading.Lock()
        self._reconcile_lock = threading.Lock()

    def scope(self, *, agent: bool = True) -> str:
        if not agent:
            require(ENV_TOKEN_FILE not in os.environ, "hub_credential_invalid")
        try:
            hub, token, _ = message_settings(str(self.config.message_token_file) if agent else None)
            return message_scope(hub, token)
        except FleetClientError:
            raise DotError("hub_credential_invalid") from None

    def require_scope(self, scope: str, *, agent: bool = True) -> None:
        require(self.scope(agent=agent) == scope, "hub_credential_changed")

    @contextmanager
    def _operation_scope(self) -> Iterator[str]:
        scope = self.scope(agent=True)
        try:
            yield scope
        finally:
            # Shared tool projections must finish under the authority captured before preparation.
            self.require_scope(scope, agent=True)

    def _call(self, action: str, *args: Any, agent: bool = True, **kwargs: Any) -> Any:
        scope = kwargs.pop("expected_scope", None) or self.scope(agent=agent)
        self.require_scope(scope, agent=agent)
        token_file = str(self.config.message_token_file) if agent else None
        try:
            try:
                value = call_message(action, *args, token_file=token_file, expected_scope=scope, **kwargs)
                if isinstance(value, dict) and value.get("queued") is True:
                    self.state.hub_set("agent_spool_pending" if agent else "node_spool_pending", "1", scope=scope)
            finally:
                self.require_scope(scope, agent=agent)
        except HubMessageError as error:
            raise DotError(error.code) from None
        return value

    @property
    def recipients(self) -> Mapping[str, str]:
        return self.config.recipients

    _require = staticmethod(require)

    def _pages(
        self,
        *,
        after: int = 0,
        unacked: bool = False,
        agent: bool = True,
        max_pages: int = 1001,
        timeout: float = 45,
        deadline: float | None = None,
        scope: str | None = None,
    ) -> Iterator[tuple[builtins.list[dict[str, Any]], int, datetime]]:
        scope = self.scope(agent=agent) if scope is None else scope
        for _ in range(max_pages):
            self.require_scope(scope, agent=agent)
            remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                return
            fetched_at = self.clock()  # A local ack after the fetch starts wins over this copy.
            value = self._call(
                "list_messages", after=after, unacked=unacked, agent=agent, timeout=remaining, expected_scope=scope
            )
            try:
                rows, cursor = inbox_page(
                    value, after=after, recipient=self.config.principal if agent else self.config.node_id
                )
            except HubMessageError as error:
                raise DotError(error.code) from None
            self.require_scope(scope, agent=agent)
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
        scope: str | None = None,
    ) -> None:
        scope = self.scope() if scope is None else scope
        self.require_scope(scope)
        acquired = self._sync_lock.acquire() if lock_timeout is None else self._sync_lock.acquire(timeout=lock_timeout)
        if not acquired:
            return
        try:
            try:
                after = int(self.state.hub_get("cursor", "0", scope=scope) or "0") if after is None else after
                for rows, cursor, fetched_at in self._pages(
                    after=after, max_pages=max_pages, timeout=timeout, deadline=deadline, scope=scope
                ):
                    self.require_scope(scope)
                    with self.state.transaction() as c:
                        self.state.hub_upsert(
                            rows,
                            node_id=self.config.node_id,
                            scope=scope,
                            clock=self.clock,
                            connection=c,
                            fetched_at=fetched_at,
                        )
                        self.state.hub_set("cursor", cursor, scope=scope, connection=c)
                        self.state.hub_set("last_sync_ok_at", _iso(self.clock()), scope=scope, connection=c)
                        self.state.hub_set("last_sync_error", "", scope=scope, connection=c)
                self.require_scope(scope)
            except DotError as error:
                self.state.hub_set("last_sync_error", str(error), scope=scope)
                raise
        finally:
            self._sync_lock.release()

    def reconcile(self) -> None:
        scope = self.scope()
        # Independent of foreground sync; never hold its lock during network IO.
        if not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            after = int(self.state.hub_get("reconcile_cursor", "0", scope=scope) or "0")
            try:
                for rows, cursor, fetched_at in self._pages(
                    after=after, max_pages=10, timeout=2, deadline=time.monotonic() + 20, scope=scope
                ):
                    self.require_scope(scope)
                    with self.state.transaction() as c:
                        self.state.hub_upsert(
                            rows,
                            node_id=self.config.node_id,
                            scope=scope,
                            clock=self.clock,
                            connection=c,
                            fetched_at=fetched_at,
                        )
                        self.state.hub_set("reconcile_cursor", cursor if rows else 0, scope=scope, connection=c)
            except DotError as error:
                # A broken credential must not read as an outage in status or alerts.
                self.state.hub_set("last_reconcile_error", str(error), scope=scope)
                raise
            self.state.hub_set("last_reconcile_error", "", scope=scope)
            # Absence is never evidence of ack, including an empty/wrong inbox.
            with self.state.transaction() as c:
                c.execute(
                    "DELETE FROM hub_inbox WHERE scope=? AND acked_at < ?",
                    (scope, _iso(self.clock() - timedelta(days=90))),
                )
            self.require_scope(scope)
            self._sweep_node_replies()
            self.require_scope(scope)
        finally:
            self._reconcile_lock.release()

    def _sweep_node_replies(self) -> None:
        scope = self.scope(agent=False)
        swept = 0
        deadline = time.monotonic() + 10
        cutoff = self.clock() - timedelta(hours=24)
        after = int(self.state.hub_get("node_sweep_cursor", "0", scope=scope) or "0")
        for rows, _, _ in self._pages(
            after=after, unacked=True, agent=False, max_pages=2, timeout=2, deadline=deadline, scope=scope
        ):
            if not rows:
                self.state.hub_set("node_sweep_cursor", "0", scope=scope)
            for row in rows:
                if time.monotonic() >= deadline:
                    return
                try:
                    old = datetime.fromisoformat(row["created_at"]).astimezone(timezone.utc) < cutoff
                except (ValueError, TypeError):
                    raise DotError("hub_inconsistent") from None
                if row["sender"] == self.config.principal and row.get("reply_to") and old:
                    self._call(
                        "ack",
                        row["id"],
                        agent=False,
                        expected_scope=scope,
                        timeout=max(0.1, min(2.0, deadline - time.monotonic())),
                    )
                    swept += 1
                self.require_scope(scope, agent=False)
                self.state.hub_set("node_sweep_cursor", row["id"], scope=scope)
                if swept >= 5:
                    return

    def list(self, unacked_only: bool = True, limit: int = 20) -> dict[str, Any]:
        scope = self.scope()
        try:
            self.sync(max_pages=1, timeout=1.5, lock_timeout=0.1, deadline=time.monotonic() + 1.6, scope=scope)
        except DotError as error:
            if error.code in {"hub_credential_invalid", "hub_credential_changed"}:
                raise
        self.require_scope(scope)
        value = self.state.hub_messages(
            unacked_only=unacked_only, limit=limit, node_id=self.config.node_id, scope=scope
        )
        self.require_scope(scope)
        return value

    def ack(self, ids: builtins.list[int]) -> dict[str, Any]:
        scope = self.scope()

        def known_rows() -> dict[int, str | None]:
            with self.state.transaction() as c:
                return {
                    row["id"]: row["acked_at"]
                    for row in c.execute("SELECT id,acked_at FROM hub_inbox WHERE scope=?", (scope,))
                }

        known = known_rows()
        acked, not_pending = [], []
        refreshed = False
        for ident in ids:
            self.require_scope(scope)
            if ident not in known and not refreshed:
                self.sync(scope=scope)
                refreshed = True
                known = known_rows()
            if ident not in known or known[ident] is not None:
                not_pending.append(ident)
                continue
            try:
                self._call("ack", ident, expected_scope=scope)
            except DotError as error:
                if str(error) != "message_not_found":
                    raise
                self.state.hub_mark_acked([ident], scope=scope, clock=self.clock)
                known[ident] = _iso(self.clock())
                not_pending.append(ident)
                continue
            self.state.hub_mark_acked([ident], scope=scope, clock=self.clock)
            known[ident] = _iso(self.clock())
            acked.append(ident)
        listing = self.state.hub_messages(unacked_only=True, limit=1, node_id=self.config.node_id, scope=scope)
        self.require_scope(scope)
        return dict(acked=acked, not_pending=not_pending, unacked_total=listing["unacked_total"])

    def _sent_message(self, value: dict[str, Any], *, agent: bool) -> dict[str, Any] | None:
        if value.get("queued") is True:
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
            try:
                scope = self.scope(agent=agent)
                if self.state.hub_get(key, scope=scope) != "1":
                    continue
                self._call("flush_credential", agent=agent, expected_scope=scope)
            except DotError as error:
                first_error = first_error or error
            else:
                self.state.hub_set(key, "0", scope=scope)
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
        *,
        node_scope: str | None = None,
    ) -> dict[str, Any] | None:
        """Node send for the notify entry point, mirrored only to the proven agent scope."""
        require(isinstance(sender, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", sender) is not None)
        MESSAGE_BODY(body)
        if thread is not None:
            THREAD(thread)
        if ref is not None:
            THREAD(ref)
        subject = _text(max_bytes=200)(subject) if subject is not None else default_subject(body, kind, sender)
        node_scope = self.scope(agent=False) if node_scope is None else node_scope
        self.require_scope(node_scope, agent=False)
        try:
            scope = self.scope()
        except DotError:
            scope = None
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
            expected_scope=node_scope,
        )
        row = self._sent_message(value, agent=False)
        if row is not None:
            require(row.get("recipient") == self.config.principal, "hub_inconsistent")
            if scope is not None:
                self.require_scope(scope)
                self.require_scope(node_scope, agent=False)
                self.state.hub_upsert([row], node_id=self.config.node_id, scope=scope, clock=self.clock)
        if scope is not None:
            self.require_scope(scope)
        self.require_scope(node_scope, agent=False)
        return row

    def wait_replies(
        self, msg_id: int, cursor: int, *, deadline: float | None = None, scope: str | None = None
    ) -> tuple[builtins.list[dict[str, Any]], int]:
        scope = self.scope(agent=False) if scope is None else scope
        self.require_scope(scope, agent=False)
        replies: builtins.list[dict[str, Any]] = []
        for rows, next_cursor, _ in self._pages(after=cursor, agent=False, max_pages=5, deadline=deadline, scope=scope):
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
                self._call("ack", row["id"], agent=False, expected_scope=scope, **bound)
            except DotError as error:
                if error.code in {"hub_credential_invalid", "hub_credential_changed"}:
                    raise
        self.require_scope(scope, agent=False)
        return replies, cursor
