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
SAVE_EVERY_PAGES = 50
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


class _EpochMoved(Exception):
    """The hub's log_epoch changed while this pull ran."""


def read_status(client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> dict[str, Any]:
    """The hub's memory status with the principal and which configured read scopes it can read now."""
    body = client.call("status", scopes=list(settings.read_scopes))
    principal = body.get("principal")
    readable = body.get("readable_scopes")
    if not isinstance(principal, str) or not principal or not isinstance(readable, list):
        raise hubc.HubConfigError("this hub's memory status names no principal or readable scopes; upgrade the hub")
    return body


def forget_other_principal(target: Path, hub: str, principal: object) -> bool:
    """Discard the replica when the hub reports another principal for this credential."""
    live = store.replica(target)
    if live.exists and live.meta.get("hub") == hub and not live.belongs_to(hub, principal):
        store.discard_replica(target)
        return True
    return False


def _card_text(
    client: hubc.MemoryHubClient, scope: str, card_id: str, revision: int, epoch: str
) -> dict[str, Any] | None:
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
        if str(body.get("log_epoch") or "") != epoch:
            # A restore between the log page and this read: revision numbers are
            # reissued, so this text may be another card version entirely.
            raise _EpochMoved
        card = hubc.obj(body.get("card"))
        parts.append(str(card.get("text", "")))
        following = card.get("next_offset")
        offset = following if isinstance(following, int) and following > offset else None
    return {**card, "text": "".join(parts)}


def pull(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> dict[str, Any]:
    """Bring the replica up to the hub's log. An epoch or principal change rebuilds it from empty."""
    with store.locked(target):
        return _pull(target, client, settings)


def _pull(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> dict[str, Any]:
    status = read_status(client, settings)
    epoch = str(status.get("log_epoch") or "")
    principal = str(status["principal"])
    readable = set(status["readable_scopes"]) & set(settings.read_scopes)
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
    elif forget_other_principal(target, hub, principal):
        # The replica was pulled with another credential's grants.
        report["rebuilt"] = True
        report["warnings"].append(
            "the hub names another principal for this credential; the old replica was discarded and is being rebuilt"
        )
    swap = not store.replica(target).exists
    replica = (
        store.start_building(target, hub=hub, log_epoch=epoch, principal=principal) if swap else store.replica(target)
    )
    # A revoked grant hides the scope's events, so no retirement would ever
    # arrive for its heads: drop them now.
    dropped = replica.retain_scopes(readable)
    covered = sorted(set(replica.meta.get("read_scopes") or []) & readable)
    if dropped or covered != sorted(replica.meta.get("read_scopes") or []):
        # Saved now, so fallback search stops serving those scopes even if
        # the rest of this pull fails.
        replica.meta["read_scopes"] = covered
        replica.save()
    if dropped:
        report["dropped"] = dropped
        report["warnings"].append(f"dropped {dropped} replicated card(s) in scopes this credential can no longer read")
    try:
        _follow(client, replica, settings, epoch=epoch, readable=readable, report=report)
        if str(client.call("status").get("log_epoch") or "") != epoch:
            raise _EpochMoved
    except _EpochMoved:
        # Heads pulled so far belong to the old epoch: never serve them.
        store.discard_replica(target)
        store.discard_building(target)
        report["warnings"].append("the hub's log_epoch changed during the pull; run pull again to rebuild")
        report.update(seq=None, cards=0, bytes=0, rebuilt=True)
        return report
    replica.meta["read_scopes"] = sorted(readable)
    replica.save()
    if swap:
        replica = store.finish_building(target)
    if replica.missing:
        report["warnings"].append(
            f"the replica reached its {settings.replica_max_bytes // (1024 * 1024)} MB cap; "
            f"{len(replica.missing)} card(s) are not replicated yet"
        )
    report.update(seq=replica.meta.get("seq"), cards=len(replica.heads), bytes=replica.bytes())
    return report


def _follow(
    client: hubc.MemoryHubClient,
    replica: store.Replica,
    settings: hubc.HubSettings,
    *,
    epoch: str,
    readable: set[str],
    report: dict[str, Any],
) -> None:
    """Apply the log after the watermark. Scopes readable for the first time are read from the start."""
    watermark = int(replica.meta.get("seq") or 0)
    new_scopes = readable - set(replica.meta.get("read_scopes") or [])
    after = 0 if new_scopes else watermark
    used = replica.bytes()
    pages = 0
    while True:
        page = client.call("log", after_seq=after, limit=LOG_PAGE)
        if str(page.get("log_epoch") or "") != epoch:
            raise _EpochMoved
        latest: dict[str, dict[str, Any]] = {}
        for event in page.get("events") or []:
            if not (
                isinstance(event, dict)
                and event.get("kind") in HEAD_KINDS
                and event.get("scope") in readable
                and isinstance(event.get("card_id"), str)
                and isinstance(event.get("revision"), int)
            ):
                continue
            seq = event.get("seq")
            if event["scope"] in new_scopes or not isinstance(seq, int) or seq > watermark:
                latest[store.Replica.key(event["scope"], event["card_id"])] = event
        for key, event in latest.items():
            report["applied"] += 1
            used = _apply(client, replica, settings, key, event, epoch=epoch, used=used)
        next_seq = page.get("next_seq")
        if not isinstance(next_seq, int) or next_seq <= after:
            break
        after = next_seq
        replica.meta["seq"] = max(after, watermark)
        pages += 1
        if pages % SAVE_EVERY_PAGES == 0:
            # Bounded progress for a long rebuild; the full write happens once at the end.
            replica.save()
    _refill(client, replica, settings, epoch=epoch, used=used)


def _apply(
    client: hubc.MemoryHubClient,
    replica: store.Replica,
    settings: hubc.HubSettings,
    key: str,
    event: dict[str, Any],
    *,
    epoch: str,
    used: int,
) -> int:
    replica.missing.pop(key, None)
    previous = replica.heads.pop(key, None)
    if previous:
        used -= store.head_size(key, previous)
    if event["kind"] == "card.retire":
        return used
    card = _card_text(client, event["scope"], event["card_id"], event["revision"], epoch)
    if card is None or card.get("state") == "retired":
        return used
    head = {
        "scope": event["scope"],
        "card_id": event["card_id"],
        "revision": card.get("revision", event["revision"]),
        "content_hash": card.get("content_hash", ""),
        "trust": card.get("trust", ""),
        "text": card["text"],
    }
    size = store.head_size(key, head)
    if used + size > settings.replica_max_bytes:
        # Kept on the list, so a later pull fetches it once space frees.
        replica.missing[key] = {
            "scope": head["scope"],
            "card_id": head["card_id"],
            "revision": head["revision"],
            "size": size,
        }
        return used
    replica.heads[key] = head
    return used + size


def _refill(
    client: hubc.MemoryHubClient, replica: store.Replica, settings: hubc.HubSettings, *, epoch: str, used: int
) -> None:
    """Fetch cards left out for the cap whose recorded size now fits."""
    for key, item in sorted(replica.missing.items()):
        size = item.get("size")
        if isinstance(size, int) and used + size > settings.replica_max_bytes:
            continue
        event = {"kind": "card.put", **{k: item.get(k) for k in ("scope", "card_id", "revision")}}
        if not (isinstance(event["scope"], str) and isinstance(event["card_id"], str)):
            replica.missing.pop(key, None)
            continue
        used = _apply(client, replica, settings, key, event, epoch=epoch, used=used)


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


def _adoptable(target: Path, settings: hubc.HubSettings, hub: str, principal: object) -> dict[str, dict[str, Any]]:
    """Replica heads in the read scopes, when the replica was pulled from `hub` as `principal`."""
    replica = store.replica(target)
    if not replica.belongs_to(hub, principal):
        return {}
    return replica.readable_heads(settings.read_scopes)


def _publishable(
    target: Path, settings: hubc.HubSettings, hub: str, principal: object
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Local cards whose bytes differ from what this workspace last published here.

    Also returns the proposals this run resolves: the local card now holds
    exactly the hub head's bytes, so the conflict is over and that head is the base.
    """
    scope = str(settings.scope)
    state = store.published(target).get(hub, {}).get(scope, {})
    spooled = {(str(e.get("scope")), str(e.get("card_id"))) for e in store.spool_entries(target, hub)}
    conflicts = store.proposal_cards(target, hub)
    heads = _adoptable(target, settings, hub, principal)
    log_epoch = store.replica(target).meta.get("log_epoch")
    changes: list[dict] = []
    skipped: list[dict] = []
    adopted: list[dict] = []
    resolved: list[str] = []
    for card in _local_cards(target):
        if card["private"]:
            skipped.append({"path": card["rel"], "reason": "visibility-private"})
            continue
        if not hubc.valid_card_id(card["card_id"]):
            skipped.append({"path": card["rel"], "reason": "card-id-invalid"})
            continue
        key = (scope, card["card_id"])
        head = heads.get(store.Replica.key(scope, card["card_id"])) or {}
        matches_head = bool(head) and head.get("content_hash") == card["content_hash"]
        if key in conflicts and key not in spooled and matches_head:
            resolved.extend(conflicts.pop(key))
            adopted.append({"card": card, "revision": head.get("revision"), "log_epoch": log_epoch})
            continue
        if key in spooled or key in conflicts:
            skipped.append({"path": card["rel"], "reason": "pending"})
            continue
        last = state.get(card["card_id"])
        if isinstance(last, dict) and last.get("content_hash") == card["content_hash"]:
            continue
        if last is None and matches_head:
            # The hub already holds these exact bytes: adopt its head as the base.
            adopted.append({"card": card, "revision": head.get("revision"), "log_epoch": log_epoch})
            continue
        changes.append({**card, "base": last if isinstance(last, dict) else None})
    return changes, skipped, adopted, resolved


def _record_published(target: Path, hub: str, entry: dict[str, Any], event: dict[str, Any], epoch: str) -> None:
    """Read-modify-write of published.json. Callers hold `store.locked`."""
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
    def __init__(self, target: Path, client: hubc.MemoryHubClient, epoch: str | None, *, reachable: bool) -> None:
        # `epoch` is None when the hub could not say (unreachable, or a
        # write-only credential that cannot read status); the hub's own
        # base_epoch check still refuses a stale base.
        self.target, self.client, self.epoch = target, client, epoch
        self.reachable = reachable
        self.unavailable: str | None = None
        self.report: dict[str, Any] = {"published": [], "spooled": [], "proposals": [], "refused": []}

    def _propose(self, entry: dict[str, Any], reason: str, reply: dict[str, Any] | None = None) -> None:
        proposal_id = store.propose(self.target, entry, reason=reason, hub_reply=reply)
        store.spool_remove(self.target, entry["hub"], entry["client_event_id"])
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
        if self.epoch is not None and entry.get("base_revision") is not None and entry.get("base_epoch") != self.epoch:
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
        epoch = str(reply.get("log_epoch") or self.epoch or "")
        if self.epoch is None and epoch:
            self.epoch = epoch
        _record_published(self.target, self.client.identity, entry, event, epoch)
        store.spool_remove(self.target, entry["hub"], entry["client_event_id"])
        self.report["published"].append({"card_id": entry["card_id"], "revision": event.get("revision")})


class _Preflight:
    """What `status` said before a push: whether to send, the epoch and the principal."""

    def __init__(self, client: hubc.MemoryHubClient, warnings: list[str]) -> None:
        self.reachable, self.epoch, self.principal, self.no_memory = True, None, None, False
        try:
            body = client.call("status")
        except hubc.HubUnavailable as exc:
            self.reachable, self.no_memory = False, exc.reason == "no-hub-memory"
            warnings.append(f"hub unavailable ({exc.reason}); writes are spooled")
            return
        except hubc.HubRefusal as exc:
            if exc.status == 401:
                # A rotated or not-yet-enrolled token: keep the writes for when it works.
                self.reachable = False
                warnings.append("the hub refused this credential (401 unauthorized); writes are spooled")
                return
            if exc.status == 403 and exc.code == "memory_grant_missing":
                # Reading status needs a read grant. A write-only credential still
                # pushes: the hub checks each write's grant and base_epoch itself.
                warnings.append("this credential cannot read hub memory status; the hub checks each write's base epoch")
                return
            raise
        self.epoch = str(body.get("log_epoch") or "")
        self.principal = body.get("principal")


def push(
    target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings, *, yes: bool = False
) -> dict[str, Any]:
    if settings.mode != "sync":
        raise hubc.HubConfigError('push needs [hub] mode = "sync"')
    with store.locked(target):
        return _push(target, client, settings, yes=yes)


def _push(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings, *, yes: bool) -> dict[str, Any]:
    hub = client.identity
    scope = str(settings.scope)
    warnings: list[str] = []
    preflight = _Preflight(client, warnings)
    if preflight.no_memory:
        if not store.has_watermark(target, hub):
            return {"ok": False, "scope": scope, "hub_memory": False, "error": "the hub has no memory (404)"}
        warnings.append("the hub answered 404 but this workspace holds a replica watermark for it")
    principal = preflight.principal
    if principal is not None and forget_other_principal(target, hub, principal):
        warnings.append("the replica was pulled as another principal and was discarded; run pull to rebuild it")
    changes, skipped, adopted, resolved = _publishable(target, settings, hub, principal)
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
    for proposal_id in resolved:
        store.drop_proposal(target, proposal_id)
    run = _Push(target, client, preflight.epoch, reachable=preflight.reachable)
    for entry in store.spool_entries(target, hub):
        run.send(entry, spooled=True)
    for card in changes:
        run.send(_entry(scope, card, hub), spooled=False)
    if run.unavailable and preflight.reachable:
        warnings.append(f"hub became unavailable ({run.unavailable}); the rest is spooled")
    if run.report.get("kept_local"):
        warnings.append(f"the spool is full ({store.SPOOL_CAP} entries); these writes stay local")
    if run.report["proposals"]:
        warnings.append("some writes became local proposals for review (memory hub proposals list shows them)")
    run.report["adopted"] = [item["card"]["card_id"] for item in adopted]
    run.report["resolved"] = resolved
    return {"ok": True, "scope": scope, **run.report, "skipped": skipped, "warnings": warnings}


def drop_proposal(target: Path, proposal_id: str) -> dict[str, Any] | None:
    """Drop a reviewed proposal so its card pushes again.

    When this host's replica holds the card's hub head, that head becomes the
    card's base, so the next push publishes the local text on top of it
    instead of conflicting again. Without one, run `pull` first.
    """
    with store.locked(target):
        record = store.drop_proposal(target, proposal_id)
        if record is None:
            return None
        entry = record.get("entry") or {}
        hub, scope, card_id = entry.get("hub"), entry.get("scope"), entry.get("card_id")
        replica = store.replica(target)
        head = None
        if isinstance(hub, str) and replica.watermark(hub) is not None and isinstance(scope, str):
            head = replica.readable_heads([scope]).get(store.Replica.key(scope, str(card_id)))
        rebased = None
        if head and isinstance(hub, str):
            state = store.published(target)
            state.setdefault(hub, {}).setdefault(scope, {})[str(card_id)] = {
                "revision": head.get("revision"),
                "content_hash": head.get("content_hash"),
                "log_epoch": replica.meta.get("log_epoch"),
            }
            store.save_published(target, state)
            rebased = head.get("revision")
        return {"proposal_id": proposal_id, "card_id": card_id, "scope": scope, "rebased_on": rebased}


# --- import --------------------------------------------------------------------------


def import_cards(target: Path, client: hubc.MemoryHubClient, *, scope: str, paths: list[Path]) -> dict[str, Any]:
    """One `import` call per card. Refusals carry the hub's code. Nothing is spooled."""
    from . import card_identity, memory_cmd

    live = store.replica(target)
    heads = live.readable_heads([scope])
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
        head = heads.get(store.Replica.key(scope, card_id))
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
    report: dict[str, Any] = {
        "mode": settings.mode,
        "scope": settings.scope,
        "read_scopes": list(settings.read_scopes),
        "warnings": [],
    }
    live = store.replica(target)
    rebuilding = store.building(target)
    keys = ("log_epoch", "seq", "cards", "bytes", "full", "principal", "read_scopes")
    report["replica"] = {key: live.meta.get(key) for key in keys} if live.exists else None
    if live.exists:
        report["replica"]["missing"] = len(live.missing)
    report["replica_rebuilding"] = rebuilding.exists
    hub = client.identity if client is not None else None
    report["spool"] = store.spool_count(target, hub) if hub else store.spool_count(target)
    others = store.spool_count(target) - report["spool"]
    if others:
        report["spool_other_hubs"] = others
    report["proposals"] = proposal_rows(target, hub)
    if client is None:
        report["hub"] = {"available": False, "reason": "no-hub-configured"}
        return report
    report["hub_url"] = client.hub_url
    try:
        body = read_status(client, settings)
    except hubc.HubUnavailable as exc:
        report["hub"] = {"available": False, "reason": exc.reason}
        if exc.reason == "no-hub-memory" and store.has_watermark(target, client.identity):
            report["warnings"].append("the hub answered 404 but this workspace holds a replica watermark for it")
        return report
    except hubc.HubRefusal as exc:
        report["hub"] = {"available": False, "reason": exc.code or f"http-{exc.status}"}
        return report
    hub_keys = (
        "cards",
        "revisions",
        "seq",
        "indexed_seq",
        "log_epoch",
        "hub_time",
        "scan",
        "embedders",
        "default_mode",
    )
    report["hub"] = {
        "available": True,
        **{key: body.get(key) for key in hub_keys},
        "principal": body.get("principal"),
        "readable_scopes": body.get("readable_scopes"),
    }
    skew = _skew_seconds(body.get("hub_time"))
    if skew is not None and abs(skew) > CLOCK_SKEW_WARN_SECONDS:
        report["warnings"].append(f"this clock differs from the hub's by {abs(skew):.0f}s")
    if live.exists and live.meta.get("log_epoch") != body.get("log_epoch"):
        report["warnings"].append("the replica was pulled under another log_epoch; the next pull rebuilds it")
    elif live.exists and live.meta.get("principal") != body.get("principal"):
        report["warnings"].append("the replica was pulled as another principal; the next pull rebuilds it")
    unreadable = sorted(set(settings.read_scopes) - set(body.get("readable_scopes") or []))
    if unreadable:
        report["warnings"].append(f"this credential cannot read: {', '.join(unreadable)}")
    return report


def proposal_rows(target: Path, hub: str | None = None) -> list[dict[str, Any]]:
    return [
        {
            "proposal_id": p.get("proposal_id"),
            "reason": p.get("reason"),
            "card_id": (p.get("entry") or {}).get("card_id"),
            "scope": (p.get("entry") or {}).get("scope"),
            "hub": (p.get("entry") or {}).get("hub"),
        }
        for p in store.proposals(target, hub)
    ]
