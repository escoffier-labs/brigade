"""Dot inbox tools backed exclusively by Fleet Hub messaging."""

from __future__ import annotations

from typing import Any

from .hub_inbox import (
    LOCAL_CLI_CODES as LOCAL_CLI_CODES,
    HubInbox as HubInbox,
    default_subject as default_subject,
    legacy_message as legacy_message,
    sender_name as sender_name,
    truncate_subject as truncate_subject,
)
from .runtime_config import Runtime
from .state import BridgeState


class InboxTools:
    def __init__(self, runtime: Runtime, state: BridgeState, hub_inbox: HubInbox) -> None:
        self.runtime, self.state, self.hub = runtime, state, hub_inbox

    def inbox_list(self, unacked_only: bool = True, limit: int = 20) -> dict[str, Any]:
        result = self.hub.list(unacked_only, limit)
        result["order"] = "oldest first" if unacked_only else "newest first"
        result["content_trust"] = "untrusted"
        return result

    def inbox_ack(self, ids: list[int]) -> dict[str, Any]:
        return self.hub.ack(ids)

    def inbox_reply(self, msg_id: int, body: str) -> dict[str, Any]:
        return self.hub.reply(msg_id, body)

    def message_send(
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
        return self.hub.send(to, subject, body, ref, priority, thread, kind, client_id)

    def message_status(self, ids: list[int]) -> dict[str, Any]:
        return self.hub.status(ids)
