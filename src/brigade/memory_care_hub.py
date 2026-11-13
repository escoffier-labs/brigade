"""Retire and revive published cards without sacrificing local undo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_care_journal as journal
from . import memory_hub_client as hubc
from . import memory_hub_store as store


def _send(
    target: Path,
    *,
    action: str,
    card_id: str,
    reason: str = "",
    path: str = "",
    text: str = "",
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pending: dict[str, Any] = {
        "status": "pending",
        "action": action,
        "card_id": card_id,
        "reason": reason,
        "path": path,
    }
    try:
        settings = hubc.load_settings(target)
        if settings.mode != "sync" or not settings.scope:
            return {"status": "skipped"}
        client = hubc.client_for(settings)
        with store.locked(target):
            state = store.published(target)
            base = state.get(client.identity, {}).get(settings.scope, {}).get(card_id)
            if not base:
                return {"status": "skipped"}
            fields = request or {
                "scope": settings.scope,
                "card_id": card_id,
                "base_revision": base["revision"],
                "base_epoch": base["log_epoch"],
                "client_event_id": hubc.event_id(),
            }
            if action == "retire":
                fields["reason"] = reason
            else:
                fields.update(text=text, origin="interactive")
            pending.update(request=fields, hub=client.identity)
            # Persist the event id before sending, so a lost reply replays the same write.
            for decision in journal.entries(target):
                saved = decision.hub or {}
                if decision.card_id == card_id and saved.get("status") == "pending" and saved.get("action") == action:
                    journal.update_hub(target, decision.decision_id, pending)
            hubc.refuse_admin(client.call("status"))
            try:
                reply = client.call(action, **fields)
            except hubc.HubRefusal as exc:
                if exc.status in (401, 429) or exc.status >= 500:
                    return pending
                proposal = store.propose(
                    target,
                    {"hub": client.identity, "action": action, **fields, "path": path},
                    reason=exc.code,
                    hub_reply=exc.body,
                )
                return {**pending, "status": "conflict", "proposal_id": proposal}
            event = hubc.obj(reply.get("event"))
            if not isinstance(event.get("revision"), int) or not reply.get("log_epoch"):
                return pending
            state[client.identity][settings.scope][card_id] = {
                **base,
                "revision": event["revision"],
                "log_epoch": reply["log_epoch"],
                "path": path or base.get("path"),
                "written": True,
                "content_hash": hubc.content_hash(text) if action == "put" else base.get("content_hash"),
            }
            store.save_published(target, state)
            return {**pending, "status": "done"}
    except (hubc.HubUnavailable, hubc.HubConfigError, hubc.HubRefusal, OSError):
        return pending


def retire(target: Path, *, card_id: str, reason: str) -> dict[str, Any]:
    return _send(target, action="retire", card_id=card_id, reason=reason)


def revive(target: Path, *, card_id: str, path: str, text: str) -> dict[str, Any]:
    return _send(target, action="put", card_id=card_id, path=path, text=text)


def retry_pending(target: Path) -> dict[str, int]:
    counts = {"done": 0, "pending": 0, "conflict": 0, "skipped": 0}
    for d in journal.entries(target):
        hub = d.hub or {}
        if hub.get("status") != "pending":
            continue
        if d.undoes:
            predecessor = journal.load(target, d.undoes)
            previous = predecessor.hub or {}
            if previous.get("status") in ("pending", "conflict"):
                result = {**hub, "status": previous["status"]}
                journal.update_hub(target, d.decision_id, result)
                counts[result["status"]] += 1
                continue
        path = str(hub.get("path") or "")
        text = ""
        if hub.get("action") == "put":
            data = journal.current(target, path)
            if data is None:
                continue
            text = data.decode("utf-8")
        result = _send(
            target,
            action=str(hub.get("action", "retire")),
            card_id=d.card_id or "",
            reason=d.reason,
            path=path,
            text=text,
            request=hub.get("request"),
        )
        journal.update_hub(target, d.decision_id, result)
        counts[result["status"]] += 1
    return counts
