"""`memory hub status`, `pull`, `push` and `import` (S14).

`pull` follows the hub log into the per-host replica. `push` publishes local
card changes in `sync` mode as `put` events, flushing the spool first, and
spools what the hub cannot take right now. A write the hub can no longer take
as it is becomes a local proposal for review and is never resent on its own.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_hub_store as store

SPOOL_MAX_AGE_MS = 28 * 24 * 3600 * 1000
CLOCK_SKEW_WARN_SECONDS = 60
LOG_PAGE = 500
# Refusals that mean "this exact write can never apply": review it as a proposal.
CONFLICT_CODES = {
    "memory_base_mismatch",
    "memory_epoch_changed",
    "memory_client_event_expired",
    "memory_client_event_conflict",
}
HEAD_KINDS = {"card.put", "card.import", "card.retire", "card.trust", "proposal.apply", "proposal.rollback"}


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- pull -------------------------------------------------------------------------


def _card_text(client: hubc.MemoryHubClient, scope: str, card_id: str, revision: int) -> dict[str, Any] | None:
    offset: int | None = 0
    parts: list[str] = []
    card: dict[str, Any] = {}
    while offset is not None:
        try:
            body = client.call("card", scope=scope, card_id=card_id, revision=revision, offset=offset)
        except hubc.HubRefusal as exc:
            if exc.code in {"memory_card_not_found", "memory_grant_missing"}:
                return None
            raise
        card = hubc.obj(body.get("card"))
        parts.append(str(card.get("text", "")))
        following = card.get("next_offset")
        offset = following if isinstance(following, int) and following > offset else None
    return {**card, "text": "".join(parts)}


def pull(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> dict[str, Any]:
    """Bring the replica up to the hub's log. An epoch change rebuilds it from empty."""
    status = client.call("status")
    epoch = str(status.get("log_epoch") or "")
    hub = client.identity
    report: dict[str, Any] = {"hub": hub, "log_epoch": epoch, "rebuilt": False, "applied": 0, "warnings": []}
    live = store.replica(target)
    if live.exists and (live.meta.get("hub") != hub or live.meta.get("log_epoch") != epoch):
        # The restored log has neither the creation nor a retirement of a card
        # made after the hub's copy, so replaying into the old heads would keep
        # a card the hub no longer has. Drop them before anything else.
        store.discard_replica(target)
        report["rebuilt"] = True
        report["warnings"].append("the hub's log_epoch changed; the old replica was discarded and is being rebuilt")
    swap = not store.replica(target).exists
    replica = store.start_building(target, hub=hub, log_epoch=epoch) if swap else store.replica(target)
    after = int(replica.meta.get("seq") or 0)
    used = replica.bytes()
    while True:
        page = client.call("log", after_seq=after, limit=LOG_PAGE)
        if str(page.get("log_epoch") or "") != epoch:
            # Heads pulled so far belong to the old epoch: never serve them.
            if not swap:
                store.discard_replica(target)
            report["warnings"].append("the hub's log_epoch changed during the pull; run pull again to rebuild")
            report.update(seq=None, cards=0, bytes=0, rebuilt=True)
            return report
        latest: dict[str, dict[str, Any]] = {}
        for event in page.get("events") or []:
            if (
                isinstance(event, dict)
                and event.get("kind") in HEAD_KINDS
                and isinstance(event.get("scope"), str)
                and isinstance(event.get("card_id"), str)
                and isinstance(event.get("revision"), int)
            ):
                latest[store.Replica.key(event["scope"], event["card_id"])] = event
        for key, event in latest.items():
            report["applied"] += 1
            previous = replica.heads.get(key)
            if previous:
                used -= len(str(previous["text"]).encode("utf-8"))
            if event["kind"] == "card.retire":
                replica.heads.pop(key, None)
                continue
            card = _card_text(client, event["scope"], event["card_id"], event["revision"])
            if card is None or card.get("state") == "retired":
                replica.heads.pop(key, None)
                continue
            head = {
                "scope": event["scope"],
                "card_id": event["card_id"],
                "revision": card.get("revision", event["revision"]),
                "content_hash": card.get("content_hash", ""),
                "trust": card.get("trust", ""),
                "text": card["text"],
            }
            size = len(head["text"].encode("utf-8"))
            if used + size > settings.replica_max_bytes:
                replica.heads.pop(key, None)
                replica.meta["full"] = True
                continue
            replica.heads[key] = head
            used += size
        next_seq = page.get("next_seq")
        if not isinstance(next_seq, int) or next_seq <= after:
            break
        after = next_seq
        replica.meta["seq"] = after
        replica.save()
    replica.save()
    if swap:
        replica = store.finish_building(target)
    if replica.meta.get("full"):
        report["warnings"].append(
            f"the replica reached its {settings.replica_max_bytes // (1024 * 1024)} MB cap; some cards are not replicated"
        )
    report.update(seq=replica.meta.get("seq"), cards=len(replica.heads), bytes=replica.bytes())
    return report


# --- push ----------------------------------------------------------------------------


def _local_cards(target: Path) -> list[dict[str, Any]]:
    from . import card_identity, memory_cmd

    cards = []
    for fields in memory_cmd.search_card_fields(target):
        path = target / fields["rel"]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = memory_cmd._parse_frontmatter(text)
        identity = card_identity.card_identity(meta, fields["rel"])
        cards.append(
            {
                "rel": fields["rel"],
                "card_id": identity.card_id,
                "private": str(meta.get("visibility", "")).strip().lower() == "private",
                "text": text,
                "content_hash": hubc.content_hash(text),
            }
        )
    return cards


def _publishable(target: Path, settings: hubc.HubSettings, hub: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Local cards whose bytes differ from what this workspace last published here."""
    scope = str(settings.scope)
    state = store.published(target).get(hub, {}).get(scope, {})
    pending = store.pending_cards(target)
    replica = store.replica(target)
    if replica.watermark(hub) is None:
        replica = store.Replica(store.root(target) / ".absent")
    changes: list[dict] = []
    skipped: list[dict] = []
    adopted: list[dict] = []
    for card in _local_cards(target):
        if card["private"]:
            skipped.append({"path": card["rel"], "reason": "visibility-private"})
            continue
        if not hubc.valid_card_id(card["card_id"]):
            skipped.append({"path": card["rel"], "reason": "card-id-invalid"})
            continue
        if (scope, card["card_id"]) in pending:
            skipped.append({"path": card["rel"], "reason": "pending"})
            continue
        last = state.get(card["card_id"])
        if isinstance(last, dict) and last.get("content_hash") == card["content_hash"]:
            continue
        head = replica.heads.get(store.Replica.key(scope, card["card_id"])) if replica.exists else None
        if last is None and head and head.get("content_hash") == card["content_hash"]:
            # The hub already holds these exact bytes: adopt its head as the base.
            adopted.append({"card": card, "revision": head.get("revision"), "log_epoch": replica.meta.get("log_epoch")})
            continue
        changes.append({**card, "base": last if isinstance(last, dict) else None})
    return changes, skipped, adopted


def _record_published(target: Path, hub: str, entry: dict[str, Any], event: dict[str, Any], epoch: str) -> None:
    state = store.published(target)
    cards = state.setdefault(hub, {}).setdefault(entry["scope"], {})
    cards[entry["card_id"]] = {
        "revision": event.get("revision"),
        "content_hash": event.get("content_hash") or hubc.content_hash(entry["text"]),
        "log_epoch": epoch,
    }
    store.save_published(target, state)


def _entry(scope: str, card: dict[str, Any], hub: str) -> dict[str, Any]:
    base = card.get("base")
    entry: dict[str, Any] = {
        "client_event_id": hubc.event_id(),
        "hub": hub,
        "action": "put",
        "scope": scope,
        "card_id": card["card_id"],
        "base_revision": base.get("revision") if base else None,
        "text": card["text"],
        "origin": "interactive",
        "path": card["rel"],
    }
    if base:
        entry["base_epoch"] = base.get("log_epoch")
    return entry


def _request(entry: dict[str, Any]) -> dict[str, Any]:
    fields = ("scope", "card_id", "base_revision", "text", "origin", "client_event_id")
    body = {key: entry[key] for key in fields}
    if entry.get("base_revision") is not None:
        body["base_epoch"] = entry.get("base_epoch")
    return body


class _Push:
    def __init__(self, target: Path, client: hubc.MemoryHubClient, epoch: str | None) -> None:
        self.target, self.client, self.epoch = target, client, epoch
        self.reachable = epoch is not None
        self.unavailable: str | None = None
        self.report: dict[str, Any] = {"published": [], "spooled": [], "proposals": [], "refused": []}

    def _propose(self, entry: dict[str, Any], reason: str, reply: dict[str, Any] | None = None) -> None:
        proposal_id = store.propose(self.target, entry, reason=reason, hub_reply=reply)
        store.spool_remove(self.target, entry["client_event_id"])
        self.report["proposals"].append({"card_id": entry["card_id"], "proposal_id": proposal_id, "reason": reason})

    def _spool(self, entry: dict[str, Any], *, spooled: bool) -> None:
        if spooled or store.spool_append(self.target, entry):
            self.report["spooled"].append({"card_id": entry["card_id"], "client_event_id": entry["client_event_id"]})
        else:
            self.report.setdefault("kept_local", []).append(entry["card_id"])

    def send(self, entry: dict[str, Any], *, spooled: bool) -> None:
        stamp = hubc.event_id_ms(entry.get("client_event_id", ""))
        if stamp is None or _now_ms() - stamp > SPOOL_MAX_AGE_MS:
            self._propose(entry, "spool-entry-expired")
            return
        if not self.reachable:
            self._spool(entry, spooled=spooled)
            return
        if entry.get("base_revision") is not None and entry.get("base_epoch") != self.epoch:
            self._propose(entry, "memory_epoch_changed", {"log_epoch": self.epoch})
            return
        try:
            reply = self.client.call("put", **_request(entry))
        except hubc.HubUnavailable as exc:
            self.reachable, self.unavailable = False, exc.reason
            self._spool(entry, spooled=spooled)
            return
        except hubc.HubRefusal as exc:
            if exc.status == 401:
                self.reachable, self.unavailable = False, "unauthorized"
                self._spool(entry, spooled=spooled)
                return
            reason = exc.code or f"http-{exc.status}"
            if exc.code not in CONFLICT_CODES:
                self.report["refused"].append({"card_id": entry["card_id"], "status": exc.status, "code": reason})
            self._propose(entry, reason, exc.body)
            return
        event = hubc.obj(reply.get("event"))
        _record_published(self.target, self.client.identity, entry, event, str(reply.get("log_epoch") or self.epoch))
        store.spool_remove(self.target, entry["client_event_id"])
        self.report["published"].append({"card_id": entry["card_id"], "revision": event.get("revision")})


def push(
    target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings, *, yes: bool = False
) -> dict[str, Any]:
    if settings.mode != "sync":
        raise hubc.HubConfigError('push needs [hub] mode = "sync"')
    hub = client.identity
    scope = str(settings.scope)
    warnings: list[str] = []
    epoch: str | None
    try:
        epoch = str(client.call("status").get("log_epoch") or "")
    except hubc.HubUnavailable as exc:
        if exc.reason == "no-hub-memory" and not store.has_watermark(target, hub):
            return {"ok": False, "scope": scope, "hub_memory": False, "error": "the hub has no memory (404)"}
        epoch = None
        warnings.append(f"hub unavailable ({exc.reason}); writes are spooled")
        if exc.reason == "no-hub-memory":
            warnings.append("the hub answered 404 but this workspace holds a replica watermark for it")
    changes, skipped, adopted = _publishable(target, settings, hub)
    first = scope not in store.published(target).get(hub, {})
    if first and not yes:
        return {
            "ok": False,
            "needs_confirmation": True,
            "scope": scope,
            "card_count": len(changes),
            "skipped": skipped,
        }
    state = store.published(target)
    cards = state.setdefault(hub, {}).setdefault(scope, {})
    for item in adopted:
        cards[item["card"]["card_id"]] = {
            "revision": item["revision"],
            "content_hash": item["card"]["content_hash"],
            "log_epoch": item["log_epoch"],
        }
    store.save_published(target, state)
    run = _Push(target, client, epoch)
    for entry in store.spool_entries(target):
        if entry.get("hub") == hub:
            run.send(entry, spooled=True)
    for card in changes:
        run.send(_entry(scope, card, hub), spooled=False)
    if run.unavailable and epoch is not None:
        warnings.append(f"hub became unavailable ({run.unavailable}); the rest is spooled")
    if run.report.get("kept_local"):
        warnings.append(f"the spool is full ({store.SPOOL_CAP} entries); these writes stay local")
    if run.report["proposals"]:
        warnings.append("some writes became local proposals for review (memory hub status lists them)")
    run.report["adopted"] = [item["card"]["card_id"] for item in adopted]
    return {"ok": True, "scope": scope, **run.report, "skipped": skipped, "warnings": warnings}


# --- import --------------------------------------------------------------------------


def import_cards(target: Path, client: hubc.MemoryHubClient, *, scope: str, paths: list[Path]) -> dict[str, Any]:
    """One `import` call per card. Refusals carry the hub's code. Nothing is spooled."""
    from . import card_identity, memory_cmd

    live = store.replica(target)
    results: list[dict[str, Any]] = []
    for path in paths:
        rel = path.as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            results.append({"path": rel, "status": "unreadable", "error": type(exc).__name__})
            continue
        meta, _ = memory_cmd._parse_frontmatter(text)
        if str(meta.get("visibility", "")).strip().lower() == "private":
            results.append({"path": rel, "status": "skipped", "reason": "visibility-private"})
            continue
        card_id = card_identity.card_identity(meta, path.name).card_id
        if not hubc.valid_card_id(card_id):
            results.append({"path": rel, "status": "skipped", "reason": "card-id-invalid"})
            continue
        head = live.heads.get(store.Replica.key(scope, card_id)) if live.exists else None
        body: dict[str, Any] = {
            "scope": scope,
            "card_id": card_id,
            "base_revision": head.get("revision") if head else None,
            "text": text,
            "client_event_id": hubc.event_id(),
        }
        if head:
            body["base_epoch"] = live.meta.get("log_epoch")
        try:
            reply = client.call("import", **body)
        except hubc.HubRefusal as exc:
            results.append({"path": rel, "card_id": card_id, "status": "refused", "code": exc.code, "http": exc.status})
            continue
        event = hubc.obj(reply.get("event"))
        results.append({"path": rel, "card_id": card_id, "status": "imported", "revision": event.get("revision")})
    return {"scope": scope, "results": results, "ok": all(r["status"] in {"imported", "skipped"} for r in results)}


# --- status ----------------------------------------------------------------------------


def _skew_seconds(hub_time: object) -> float | None:
    if not isinstance(hub_time, str):
        return None
    try:
        moment = datetime.fromisoformat(hub_time)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def status(target: Path, settings: hubc.HubSettings, client: hubc.MemoryHubClient | None) -> dict[str, Any]:
    report: dict[str, Any] = {"mode": settings.mode, "scope": settings.scope, "warnings": []}
    live = store.replica(target)
    rebuilding = store.building(target)
    report["replica"] = (
        {key: live.meta.get(key) for key in ("log_epoch", "seq", "cards", "bytes", "full")} if live.exists else None
    )
    report["replica_rebuilding"] = rebuilding.exists
    report["spool"] = store.spool_count(target)
    report["proposals"] = [
        {
            "proposal_id": p.get("proposal_id"),
            "reason": p.get("reason"),
            "card_id": (p.get("entry") or {}).get("card_id"),
        }
        for p in store.proposals(target)
    ]
    if client is None:
        report["hub"] = {"available": False, "reason": "no-hub-configured"}
        return report
    report["hub_url"] = client.hub_url
    try:
        body = client.call("status")
    except hubc.HubUnavailable as exc:
        report["hub"] = {"available": False, "reason": exc.reason}
        if exc.reason == "no-hub-memory" and store.has_watermark(target, client.identity):
            report["warnings"].append("the hub answered 404 but this workspace holds a replica watermark for it")
        return report
    except hubc.HubRefusal as exc:
        report["hub"] = {"available": False, "reason": exc.code or f"http-{exc.status}"}
        return report
    keys = ("cards", "revisions", "seq", "indexed_seq", "log_epoch", "hub_time", "scan", "embedders", "default_mode")
    report["hub"] = {"available": True, **{key: body.get(key) for key in keys}}
    skew = _skew_seconds(body.get("hub_time"))
    if skew is not None and abs(skew) > CLOCK_SKEW_WARN_SECONDS:
        report["warnings"].append(f"this clock differs from the hub's by {abs(skew):.0f}s")
    if live.exists and live.meta.get("log_epoch") != body.get("log_epoch"):
        report["warnings"].append("the replica was pulled under another log_epoch; the next pull rebuilds it")
    return report
