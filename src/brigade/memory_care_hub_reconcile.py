"""Find the result of a pending hub write without replaying its content."""

from __future__ import annotations

from typing import Any

from . import memory_hub_client as hubc


def accepted_revision(client: Any, request: dict[str, Any]) -> tuple[int, str] | None:
    """Read the accepted event's revision, without resending withdrawn content.

    The hub has no event-status read. Its log exposes accepted event ids, scoped
    to the caller's readable events. A textless put cannot probe idempotency:
    the hub requires text and binds replay to the original request digest.
    """
    event_id = request.get("client_event_id")
    base = request.get("base_revision")
    scope = request.get("scope")
    card_id = request.get("card_id")
    if not isinstance(event_id, str) or type(base) is not int or base <= 0:
        return None
    # Head revisions are event sequence numbers. No older event can be this write.
    after = base
    try:
        status = hubc.refuse_admin(client.call("status"))
        principal = status.get("principal")
        if not isinstance(principal, str) or status.get("log_epoch") != request.get("base_epoch"):
            return None
        through = status.get("seq")
        while True:
            reply = client.call("log", after_seq=after, limit=500)
            epoch = reply.get("log_epoch")
            events = reply.get("events")
            if not isinstance(epoch, str) or epoch != request.get("base_epoch") or not isinstance(events, list):
                return None
            for raw in events:
                event = hubc.obj(raw)
                if (
                    event.get("client_event_id") == event_id
                    and event.get("principal") == principal
                    and event.get("kind") == ("card.put" if "text" in request else "card.retire")
                    and event.get("scope") == scope
                    and event.get("card_id") == card_id
                    and type(event.get("revision")) is int
                    and event["revision"] > base
                ):
                    return event["revision"], epoch
            next_seq = reply.get("next_seq")
            # Empty pages still advance over events hidden by scope grants.
            if type(next_seq) is not int or next_seq <= after:
                return None
            # Bound the scan to the head observed before reading, even on a busy hub.
            if type(through) is int and next_seq >= through:
                return None
            after = next_seq
    except (hubc.HubUnavailable, hubc.HubRefusal, OSError):
        return None
