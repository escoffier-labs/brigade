"""Direct principal inbox client for the t3-dispatch connector."""

from __future__ import annotations

import builtins
import hashlib
import json
import time
from typing import Any, Iterator

from ..fleet_client_message_credentials import FleetMessageHubError, read_message_token_file
from ..hub_messages import HubMessageError, MessageOperations, inbox_page, legacy_message
from .contracts import PUBLIC_RESULT_LIMIT

POLL_MAX_PAGES = 3
POLL_TIMEOUT_SECONDS = 1.0
POLL_BUDGET_SECONDS = 3.0
# Bound the complete acknowledgement batch, including its best-effort recount.
ACK_BUDGET_SECONDS = 10.0
ACK_TIMEOUT_SECONDS = 5.0


class HubInbox(MessageOperations):
    def notification_scope(self) -> str:
        # Rotation must not reuse a different principal's notification cursor.
        try:
            return hashlib.sha256(read_message_token_file(self.token_file).encode("ascii")).hexdigest()
        except FleetMessageHubError as error:
            raise HubMessageError("hub_credential_invalid", error.code) from None

    def require_notification_scope(self, scope: str) -> None:
        if self.notification_scope() != scope:
            raise HubMessageError("hub_credential_changed")

    def pages(
        self,
        *,
        after: int = 0,
        unacked: bool = True,
        max_pages: int = POLL_MAX_PAGES,
        timeout: float = POLL_TIMEOUT_SECONDS,
        budget: float = POLL_BUDGET_SECONDS,
        scope: str | None = None,
        deadline: float | None = None,
    ) -> Iterator[tuple[builtins.list[dict[str, Any]], int]]:
        scope = self.notification_scope() if scope is None else scope
        poll_deadline = time.monotonic() + budget
        deadline = poll_deadline if deadline is None else min(poll_deadline, deadline)
        for _ in range(max_pages):
            self.require_notification_scope(scope)
            remaining = min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                return
            try:
                value = self._call("list_messages", after=after, unacked=unacked, timeout=remaining)
            finally:
                # Check failed fetches too: a changed credential invalidates earlier rows.
                self.require_notification_scope(scope)
            rows, cursor = inbox_page(value, after=after)
            yield rows, cursor
            if not rows:
                return
            after = cursor

    def list(self, unacked_only: bool = True, limit: int = 20, *, deadline: float | None = None) -> dict[str, Any]:
        scope = self.notification_scope()
        rows = []
        complete = False
        try:
            for page, _ in self.pages(unacked=unacked_only, scope=scope, deadline=deadline):
                rows.extend(page)
                complete = not page
        except HubMessageError as error:
            if not rows or error.code in {"hub_credential_changed", "hub_credential_invalid"}:
                raise
        self.require_notification_scope(scope)
        ordered = rows if unacked_only else list(reversed(rows))
        result: dict[str, Any] = dict(
            messages=[],
            unacked_total=sum(row.get("acked_at") is None for row in rows),
            has_more=False,
            order=(
                "oldest first"
                if unacked_only
                else "newest first"
                if complete
                else "newest first within oldest fetched window"
            ),
            content_trust="untrusted",
        )
        # Match the public boundary's JSON encoding, including ASCII escaping and all metadata.
        for row in ordered[:limit]:
            result["messages"].append(legacy_message(row, ""))
            if len(json.dumps(result, separators=(",", ":")).encode("utf-8")) > PUBLIC_RESULT_LIMIT:
                result["messages"].pop()
                if not result["messages"]:
                    raise HubMessageError("hub_unavailable")
                break
        result["has_more"] = not complete or len(result["messages"]) < len(rows)
        return result

    def ack(self, ids: builtins.list[int]) -> dict[str, Any]:
        deadline = time.monotonic() + ACK_BUDGET_SECONDS
        acked: builtins.list[int] = []
        not_pending: builtins.list[int] = []
        remaining_ids = []
        for index, ident in enumerate(ids):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                remaining_ids = ids[index:]
                break
            try:
                self._call("ack", ident, timeout=min(ACK_TIMEOUT_SECONDS, remaining))
            except HubMessageError as error:
                if error.code != "message_not_found":
                    if not acked and not not_pending:
                        raise
                    # Include the uncertain id so an idempotent retry can confirm it.
                    remaining_ids = ids[index:]
                    break
                not_pending.append(ident)
            else:
                acked.append(ident)
        listing = {"unacked_total": None, "has_more": True}
        remaining = deadline - time.monotonic()
        if not remaining_ids and remaining > 0:
            try:
                listing = self.list(limit=1, deadline=deadline)
            except HubMessageError:
                # Confirmed mutations survive an unavailable best-effort recount.
                pass
        result = dict(
            acked=acked, not_pending=not_pending, unacked_total=listing["unacked_total"], has_more=listing["has_more"]
        )
        if remaining_ids:
            result.update(incomplete=True, remaining_ids=remaining_ids)
        return result
