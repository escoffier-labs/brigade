"""Direct principal inbox client for the t3-dispatch connector."""

from __future__ import annotations

import builtins
import json
import time
from typing import Any, Iterator

from ..fleet_client_message_credentials import MessageAuthority
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
        # Compatibility snapshot accessor. Operations pass the captured authority instead.
        return self.capture().scope

    def _call(self, action: str, *args: Any, authority: MessageAuthority, **kwargs: Any) -> Any:
        self.require_authority(authority)
        try:
            return super()._call(action, *args, authority=authority, **kwargs)
        finally:
            self.require_authority(authority)

    def pages(
        self,
        *,
        after: int = 0,
        unacked: bool = True,
        max_pages: int = POLL_MAX_PAGES,
        timeout: float = POLL_TIMEOUT_SECONDS,
        budget: float = POLL_BUDGET_SECONDS,
        authority: MessageAuthority | None = None,
        deadline: float | None = None,
    ) -> Iterator[tuple[builtins.list[dict[str, Any]], int]]:
        authority = self.capture() if authority is None else authority
        poll_deadline = time.monotonic() + budget
        deadline = poll_deadline if deadline is None else min(poll_deadline, deadline)
        return self._pages(
            after=after, unacked=unacked, max_pages=max_pages, timeout=timeout, deadline=deadline, authority=authority
        )

    def _pages(
        self, *, after: int, unacked: bool, max_pages: int, timeout: float, deadline: float, authority: MessageAuthority
    ) -> Iterator[tuple[builtins.list[dict[str, Any]], int]]:
        for _ in range(max_pages):
            self.require_authority(authority)
            remaining = min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                return
            try:
                value = self._call(
                    "list_messages", after=after, unacked=unacked, timeout=remaining, authority=authority
                )
            finally:
                # Check failed fetches too: a changed credential invalidates earlier rows.
                self.require_authority(authority)
            rows, cursor = inbox_page(value, after=after)
            yield rows, cursor
            if not rows:
                return
            after = cursor

    def list(
        self,
        unacked_only: bool = True,
        limit: int = 20,
        *,
        deadline: float | None = None,
        authority: MessageAuthority | None = None,
    ) -> dict[str, Any]:
        authority = self.capture() if authority is None else authority
        rows = []
        complete = False
        try:
            for page, _ in self.pages(unacked=unacked_only, authority=authority, deadline=deadline):
                rows.extend(page)
                complete = not page
        except HubMessageError as error:
            if not rows or error.code in {"hub_credential_changed", "hub_credential_invalid"}:
                raise
        self.require_authority(authority)
        ordered = rows if unacked_only else list(reversed(rows))
        result: dict[str, Any] = dict(
            messages=[],
            unacked_total=sum(row.get("acked_at") is None for row in rows) if complete else None,
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
        self.require_authority(authority)
        return result

    def ack(self, ids: builtins.list[int]) -> dict[str, Any]:
        deadline = time.monotonic() + ACK_BUDGET_SECONDS
        authority = self.capture()
        acked: builtins.list[int] = []
        not_pending: builtins.list[int] = []
        remaining_ids = []
        for index, ident in enumerate(ids):
            self.require_authority(authority)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                remaining_ids = ids[index:]
                break
            try:
                try:
                    self._call("ack", ident, timeout=min(ACK_TIMEOUT_SECONDS, remaining), authority=authority)
                finally:
                    # Check failures too, before preserving any partial batch result.
                    self.require_authority(authority)
            except HubMessageError as error:
                if error.code in {"hub_credential_changed", "hub_credential_invalid"}:
                    raise
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
                listing = self.list(limit=1, deadline=deadline, authority=authority)
            except HubMessageError as error:
                if error.code in {"hub_credential_changed", "hub_credential_invalid"}:
                    raise
                # Confirmed mutations survive an unavailable best-effort recount.
                pass
        self.require_authority(authority)
        result = dict(
            acked=acked, not_pending=not_pending, unacked_total=listing["unacked_total"], has_more=listing["has_more"]
        )
        if remaining_ids:
            result.update(incomplete=True, remaining_ids=remaining_ids)
        return result
