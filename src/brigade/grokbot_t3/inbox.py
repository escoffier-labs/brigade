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


class HubInbox(MessageOperations):
    def notification_scope(self) -> str:
        # Rotation must not reuse a different principal's notification cursor.
        try:
            return hashlib.sha256(read_message_token_file(self.token_file).encode("ascii")).hexdigest()
        except FleetMessageHubError as error:
            raise HubMessageError("hub_credential_invalid", error.code) from None

    def pages(
        self,
        *,
        after: int = 0,
        unacked: bool = True,
        max_pages: int = POLL_MAX_PAGES,
        timeout: float = POLL_TIMEOUT_SECONDS,
        budget: float = POLL_BUDGET_SECONDS,
    ) -> Iterator[tuple[builtins.list[dict[str, Any]], int]]:
        deadline = time.monotonic() + budget
        for _ in range(max_pages):
            remaining = min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                return
            value = self._call("list_messages", after=after, unacked=unacked, timeout=remaining)
            rows, cursor = inbox_page(value, after=after)
            yield rows, cursor
            if not rows:
                return
            after = cursor

    def list(self, unacked_only: bool = True, limit: int = 20) -> dict[str, Any]:
        rows = []
        complete = False
        try:
            for page, _ in self.pages(unacked=unacked_only):
                rows.extend(page)
                complete = not page
        except HubMessageError:
            if not rows:
                raise
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
        acked, not_pending = [], []
        for ident in ids:
            try:
                self._call("ack", ident)
            except HubMessageError as error:
                if error.code != "message_not_found":
                    raise
                not_pending.append(ident)
            else:
                acked.append(ident)
        try:
            listing = self.list(limit=1)
        except HubMessageError:
            # The mutation succeeded even when its best-effort recount is unavailable.
            listing = {"unacked_total": None, "has_more": True}
        return dict(
            acked=acked, not_pending=not_pending, unacked_total=listing["unacked_total"], has_more=listing["has_more"]
        )
