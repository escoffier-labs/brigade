"""Retire and revive published cards without sacrificing local undo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_care_journal as journal
from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_hub_sync as sync


def _replay_state(target: Path, decision: journal.Decision) -> dict[str, str]:
    """Reconcile a queued decision with current local files. Never rewrite an event."""
    hub = decision.hub or {}
    path = str(hub.get("path") or "")
    decisions = journal.entries(target)
    if any(
        later.card_id == decision.card_id
        and later.kind == "archive"
        and later.created_at > decision.created_at
        and journal._payload(target, later.decision_id).get("phase") != "abandoned"
        for later in decisions
    ):
        return {"status": "superseded", "reason": "later-archive"}
    try:
        live = journal.current(target, path)
        if hub.get("action") == "put":
            recorded = journal._payload(target, decision.decision_id)
            import base64

            saved = recorded["after_bytes"][decision.paths.index(path)]
            recorded_text = base64.b64decode(saved).decode("utf-8") if saved is not None else ""
            withheld = sync.withheld(recorded_text) or (
                sync.withheld(live.decode("utf-8")) if live is not None else None
            )
            if withheld:
                return {"status": "skipped", "reason": withheld}
            expected = decision.after_sha256.get(path)
            if live is None or journal.kernel.content_digest(live) != expected:
                return {"status": "superseded", "reason": "live-card-changed"}
        else:
            # A recorded undo must complete its retire before its revive, even though
            # the undo has restored the source and removed the archive copy.
            undo_pair = any(
                d.undoes == decision.decision_id and journal._payload(target, d.decision_id).get("phase") != "abandoned"
                for d in decisions
            )
            if not undo_pair:
                if live is not None:
                    return {"status": "superseded", "reason": "source-active"}
                from .memory_archive import archive_rel

                archived = archive_rel(path)
                data = journal.current(target, archived)
                if data is None or journal.kernel.content_digest(data) != decision.after_sha256.get(archived):
                    return {"status": "superseded", "reason": "archive-changed"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "needs_review", "reason": "local-state-unreadable"}
    return {"status": "send"}


def _holders(target: Path, card_id: str, path: str) -> list[dict[str, Any]]:
    """Live, publishable local cards (other than `path`) whose publication id is `card_id`."""
    return [
        card
        for card in sync._local_cards(target)
        if card["card_id"] == card_id and card["rel"] != path and not card["private"]
    ]


_WRITES = ("retire", "put", "handover")


def _retired_here(target: Path, card_id: str, identity: str, scope: str, base: dict[str, Any]) -> bool:
    """This hub and scope's last completed write for the id from this workspace was a retire.

    Only journal results recorded against this hub identity and scope count. A
    result that stored the revision it produced must also match the record's
    current revision, so a later write by anyone else voids the evidence.
    Results from older releases carry no revision and fall back to hub and scope.
    """
    last: dict[str, Any] | None = None
    for d in sorted(journal.entries(target), key=lambda d: d.created_at):
        saved = d.hub or {}
        if d.card_id != card_id or saved.get("status") != "done" or saved.get("action") not in _WRITES:
            continue
        if saved.get("hub") != identity or (saved.get("request") or {}).get("scope") != scope:
            continue
        last = saved
    if last is None or last["action"] != "retire":
        return False
    if base.get("revision") is not None and last.get("revision") is not None:
        return bool(last["revision"] == base["revision"] and last.get("log_epoch") in (None, base.get("log_epoch")))
    return True


def _archived_hash(target: Path, decision: journal.Decision, path: str) -> str | None:
    """Hash of the card text this decision archived, as sync hashes a card. None when it cannot be read."""
    try:
        data = journal.before_bytes(target, decision, path)
        return None if data is None else hubc.content_hash(data.decode("utf-8"))
    except (OSError, ValueError, KeyError, UnicodeError, journal.JournalConflict):
        return None


def _authorized(base: dict[str, Any], content_hash: str | None) -> bool:
    """This workspace wrote the record, or the hub copy at that revision is exactly the archived text."""
    return base.get("written") is True or (content_hash is not None and base.get("content_hash") == content_hash)


def _ownership(
    target: Path,
    action: str,
    card_id: str,
    path: str,
    base: dict[str, Any] | None,
    holders: list[dict[str, Any]],
    *,
    identity: str,
    scope: str,
    content_hash: str | None = None,
) -> str:
    """What the hub copy does with this card: "send", "handover", "collision" or "not-owned".

    The hub copy follows the cards still in use. Archiving the last holder
    retires it. Archiving the owner while one other holder remains writes that
    holder's text to the hub. Either only happens for a record this workspace
    wrote or whose text is the archived text. A revive goes out only when no
    other card holds the id, and either this card owns the record or this
    hub and scope's last write for the id was a retire from here.
    """
    if action == "retire":
        if not path:
            return "collision"
        if holders and not (base is not None and base.get("path") == path and len(holders) == 1):
            return "collision"
        if base is not None and not _authorized(base, content_hash):
            return "not-owned"
        return "handover" if holders else "send"
    if holders:
        return "collision"
    if not path or base is None or base.get("path") == path or _retired_here(target, card_id, identity, scope, base):
        return "send"
    return "collision"


def _send(
    target: Path,
    *,
    action: str,
    card_id: str,
    reason: str = "",
    path: str = "",
    text: str = "",
    request: dict[str, Any] | None = None,
    recorded_hub: str | None = None,
    decision: journal.Decision | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    if action == "put":
        reason_withheld = sync.withheld(text)
        if path:
            live = journal.current(target, path)
            if live is not None:
                reason_withheld = reason_withheld or sync.withheld(live.decode("utf-8"))
        if reason_withheld:
            return {"status": "skipped", "reason": reason_withheld}
    if action == "retire" and content_hash is None and decision is not None:
        content_hash = _archived_hash(target, decision, path)
    pending: dict[str, Any] = {
        "status": "pending",
        "action": action,
        "card_id": card_id,
        "reason": reason,
        "path": path,
    }
    if request:
        pending.update(request=request, hub=recorded_hub)
        if decision is not None and (decision.hub or {}).get("holder"):
            pending["holder"] = (decision.hub or {})["holder"]
    try:
        settings = hubc.load_settings(target)
        if settings.mode != "sync" or not settings.scope:
            return {"status": "skipped"}
        client = hubc.client_for(settings)
        if request and (recorded_hub != client.identity or request.get("scope") != settings.scope):
            return {
                **pending,
                "request": request,
                "hub": recorded_hub,
                "status": "needs_review",
                "reason": "hub-changed" if recorded_hub != client.identity else "scope-changed",
            }
        if decision is not None:
            verdict = _replay_state(target, decision)
            if verdict["status"] != "send":
                return {**pending, **verdict}
        with store.locked(target):
            state = store.published(target)
            base = state.get(client.identity, {}).get(settings.scope, {}).get(card_id)
            # A revive must respect ownership too, or undo could overwrite the card that owns the id.
            holders = _holders(target, card_id, path)
            ownership = _ownership(
                target,
                action,
                card_id,
                path,
                base,
                holders,
                identity=client.identity,
                scope=settings.scope,
                content_hash=content_hash,
            )
            if ownership == "collision":
                return {**pending, "status": "skipped", "reason": "card-id-collision"}
            if ownership == "not-owned":
                return {**pending, "status": "skipped", "reason": "card-id-not-owned"}
            if not base:
                return {"status": "skipped"}
            # The wire action is a put for a handover: the remaining holder's text replaces the archived text.
            handover = ownership == "handover"
            wire = "put" if handover else action
            body_text = holders[0]["text"] if handover else text
            written_path = holders[0]["rel"] if handover else path or base.get("path")
            if handover:
                pending.update(action="handover", holder=written_path)
            else:
                pending.pop("holder", None)
            # Replay a stored request only when it is the same kind of write for the same card.
            stored = request if request and ("text" in request) == (wire == "put") else None
            if stored and handover and (decision is None or (decision.hub or {}).get("holder") != written_path):
                stored = None
            fields = (
                dict(stored)
                if stored
                else {
                    "scope": settings.scope,
                    "card_id": card_id,
                    "base_revision": base["revision"],
                    "base_epoch": base["log_epoch"],
                    "client_event_id": hubc.event_id(),
                }
            )
            if wire == "retire":
                fields["reason"] = reason
            elif not (stored and handover):
                fields.update(text=body_text, origin="interactive")
            pending.update(request=fields, hub=client.identity)
            # Persist the event id before sending, so a lost reply replays the same write.
            if decision is not None:
                journal.update_hub(target, decision.decision_id, pending)
            else:
                for other in journal.entries(target):
                    saved = other.hub or {}
                    if other.card_id == card_id and saved.get("status") == "pending" and saved.get("action") == action:
                        journal.update_hub(target, other.decision_id, pending)
            hubc.refuse_admin(client.call("status"))
            try:
                reply = client.call(wire, **fields)
            except hubc.HubRefusal as exc:
                if exc.status in (401, 429) or exc.status >= 500:
                    return pending
                proposal = store.propose(
                    target,
                    {"hub": client.identity, "action": wire, **fields, "path": written_path},
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
                "path": written_path,
                "written": True,
                "content_hash": hubc.content_hash(fields["text"]) if wire == "put" else base.get("content_hash"),
            }
            store.save_published(target, state)
            return {**pending, "status": "done", "revision": event["revision"], "log_epoch": reply["log_epoch"]}
    except (hubc.HubUnavailable, hubc.HubConfigError, hubc.HubRefusal, OSError):
        return pending


def retire(
    target: Path,
    *,
    card_id: str,
    reason: str,
    path: str = "",
    content_hash: str | None = None,
    decision: journal.Decision | None = None,
) -> dict[str, Any]:
    return _send(
        target,
        action="retire",
        card_id=card_id,
        reason=reason,
        path=path,
        content_hash=content_hash,
        decision=decision,
    )


def revive(target: Path, *, card_id: str, path: str, text: str) -> dict[str, Any]:
    return _send(target, action="put", card_id=card_id, path=path, text=text)


def _skipped_retire(d: journal.Decision) -> bool:
    """An archive still in force whose retire was skipped for a card-id collision."""
    hub = d.hub or {}
    return (
        d.kind == "archive"
        and not d.undone_by
        and hub.get("status") == "skipped"
        and hub.get("reason") == "card-id-collision"
        and hub.get("action") == "retire"
        and bool(hub.get("path"))
    )


@journal.serialized
def retry_pending(target: Path) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "needs_review": 0,
        "review_items": [],
        "done": 0,
        "pending": 0,
        "conflict": 0,
        "skipped": 0,
        "skipped_items": [],
        "superseded": 0,
    }
    decisions = sorted(journal.entries(target), key=lambda d: d.created_at)
    for d in decisions:
        hub = d.hub or {}
        payload = journal._payload(target, d.decision_id)
        if payload.get("phase") == "abandoned" and not (
            payload.get("restorable")
            and any(
                journal.current(target, path) is None and payload["after_sha256"][path] is None
                for path in d.paths
                if path not in journal.index_paths(payload)
            )
        ):
            continue
        if hub.get("status") == "needs_review":
            counts["needs_review"] += 1
            counts["review_items"].append({"decision_id": d.decision_id, "reason": hub.get("reason")})
            continue
        if _skipped_retire(d):
            # Earlier releases skipped these on a path mismatch. Retire when no holder is left.
            result = _send(
                target,
                action="retire",
                card_id=d.card_id or "",
                reason=d.reason,
                path=str(hub.get("path") or ""),
                decision=d,
            )
            if result.get("status") == "skipped" and result.get("reason") in (None, "card-id-collision"):
                continue
            journal.update_hub(target, d.decision_id, result)
            counts[result["status"]] += 1
            if result["status"] == "skipped":
                counts["skipped_items"].append({"decision_id": d.decision_id, "reason": result.get("reason")})
            if result["status"] == "needs_review":
                counts["review_items"].append({"decision_id": d.decision_id, "reason": result.get("reason")})
            continue
        if hub.get("status") != "pending":
            continue
        verdict = _replay_state(target, d)
        if verdict["status"] != "send":
            journal.update_hub(target, d.decision_id, {**hub, **verdict})
            counts[verdict["status"]] += 1
            if verdict["status"] in ("needs_review", "skipped"):
                key = "review_items" if verdict["status"] == "needs_review" else "skipped_items"
                counts[key].append({"decision_id": d.decision_id, "reason": verdict["reason"]})
            continue
        if d.undoes:
            predecessor = journal.load(target, d.undoes)
            previous = predecessor.hub or {}
            if previous.get("status") in ("pending", "conflict", "needs_review"):
                result = {**hub, "status": previous["status"], "reason": previous.get("reason", "retire unresolved")}
                journal.update_hub(target, d.decision_id, result)
                counts[result["status"]] += 1
                if result["status"] == "needs_review":
                    counts["review_items"].append({"decision_id": d.decision_id, "reason": result["reason"]})
                continue
        path = str(hub.get("path") or "")
        text = ""
        if hub.get("action") == "put":
            import base64

            payload = journal._payload(target, d.decision_id)
            # The immutable after-image binds this text to this decision and event id.
            if path not in d.paths:
                continue
            data = payload["after_bytes"][d.paths.index(path)]
            if data is None:
                continue
            text = base64.b64decode(data).decode("utf-8")
        result = _send(
            target,
            # A pending handover replays through the retire path, which decides again who owns the id.
            action="retire" if hub.get("action") == "handover" else str(hub.get("action", "retire")),
            card_id=d.card_id or "",
            reason=d.reason,
            path=path,
            text=text,
            request=hub.get("request"),
            recorded_hub=hub.get("hub"),
            decision=d,
        )
        journal.update_hub(target, d.decision_id, result)
        counts[result["status"]] += 1
        if result["status"] == "skipped":
            counts["skipped_items"].append({"decision_id": d.decision_id, "reason": result.get("reason")})
        if result["status"] == "needs_review":
            counts["review_items"].append({"decision_id": d.decision_id, "reason": result.get("reason")})
    return counts
