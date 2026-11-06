"""`memory hub status`, `pull`, `push` and `import` (S14).

`pull` follows the hub log into the per-host replica. `push` publishes local
card changes in `sync` mode as `put` events, flushing the spool first, and
spools what the hub cannot take right now. A write the hub can no longer take
as it is becomes a local proposal for review and is never resent on its own.

Every command here that contacts the hub asks the hub's `status` first and
refuses an admin credential (`memory_hub_client.refuse_admin`).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
# Card metadata a head keeps beside its text. Replicas pulled before S18 lack all of it.
SCAN_FIELDS = ("state", "scan_state", "writer", "updated_at")
HEAD_KINDS = {"card.put", "card.import", "card.retire", "card.trust", "proposal.apply", "proposal.rollback"}


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- pull -------------------------------------------------------------------------


class _EpochMoved(Exception):
    """The hub's log_epoch changed while this pull ran."""


class _Superseded(Exception):
    """Another run discarded the live replica while this pull followed the log."""


class _ScopeRefused(Exception):
    """The hub refused a `card` read in this scope during the pull (memory_grant_missing)."""

    def __init__(self, scope: str) -> None:
        super().__init__(scope)
        self.scope = scope


def read_status(client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> dict[str, Any]:
    """The hub's memory status with the principal and which configured read scopes it can read now.

    Refuses the admin credential (HubConfigError).
    """
    body = hubc.refuse_admin(client.call("status", scopes=list(settings.read_scopes)))
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


def forget_hub_replica(target: Path, hub: str) -> None:
    """Discard the live replica and any rebuild pulled from `hub`. Takes the state lock."""
    with store.locked(target):
        if store.replica(target).meta.get("hub") == hub:
            store.discard_replica(target)
        if store.building(target).meta.get("hub") == hub:
            store.discard_building(target)


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
            if exc.code == "memory_card_not_found":
                return None
            if exc.code == "memory_grant_missing":
                raise _ScopeRefused(scope) from exc
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
    """Bring the replica up to the hub's log. An epoch or principal change rebuilds it from empty.

    The pull lock covers the whole run, so two pulls never interleave. The
    state lock is taken only around changes to the replica (the discard, each
    save and the swap), so a push waits at most for one of those, never for
    the network calls of a long pull.
    """
    with store.pulling(target):
        try:
            return _pull(target, client, settings)
        except hubc.HubRefusal as exc:
            if exc.status == 403 and exc.code == "memory_grant_missing":
                # A `status` or `log` refusal at any point of the pull means this
                # credential can read no scope on this hub now: nothing pulled
                # from it may be served by fallback search later. (A `card`
                # refusal is scope-wide only and is handled inside the pull.)
                forget_hub_replica(target, client.identity)
            raise


def _pull(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> dict[str, Any]:
    status = read_status(client, settings)
    epoch = str(status.get("log_epoch") or "")
    principal = str(status["principal"])
    readable = set(status["readable_scopes"]) & set(settings.read_scopes)
    hub = client.identity
    report: dict[str, Any] = {
        "hub": hub,
        "log_epoch": epoch,
        "principal": principal,
        "rebuilt": False,
        "applied": 0,
        "warnings": [],
    }
    with store.locked(target):
        swap, replica = _prepare(target, hub, epoch, principal, readable, report)

    def save_held() -> None:
        # Hold `locked`. A rebuild writes only replica.building, which only
        # pulls touch; an incremental pull writes the live replica, which a
        # push may have discarded meanwhile (another principal).
        live = store.replica(target)
        if not swap and not (live.belongs_to(hub, principal) and live.meta.get("log_epoch") == epoch):
            raise _Superseded
        replica.save()

    def save() -> None:
        with store.locked(target):
            save_held()

    try:
        _follow(client, replica, settings, epoch=epoch, readable=readable, report=report, save=save)
        if str(client.call("status").get("log_epoch") or "") != epoch:
            raise _EpochMoved
        replica.meta["read_scopes"] = sorted(readable)
        with store.locked(target):
            save_held()
            if swap:
                replica = store.finish_building(target)
    except _EpochMoved:
        # Heads pulled so far belong to the old epoch: never serve them.
        with store.locked(target):
            store.discard_replica(target)
            store.discard_building(target)
        report["warnings"].append("the hub's log_epoch changed during the pull; run pull again to rebuild")
        report.update(seq=None, cards=0, bytes=0, rebuilt=True)
        return report
    except _Superseded:
        report["warnings"].append("another run discarded the replica during this pull; run pull again to rebuild")
        report.update(seq=None, cards=0, bytes=0, rebuilt=True)
        return report
    if replica.missing:
        report["warnings"].append(
            f"the replica reached its {settings.replica_max_bytes // (1024 * 1024)} MB cap; "
            f"{len(replica.missing)} card(s) are not replicated yet"
        )
    report.update(seq=replica.meta.get("seq"), cards=len(replica.heads), bytes=replica.bytes())
    return report


def refresh_scan_metadata(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> int:
    """Fill in scan metadata on readable heads that lack `scan_state`, in place. Returns how many it refreshed.

    The live replica keeps serving the whole time: nothing is discarded or
    rebuilt. Each stale head costs one `card` read of its own revision, made
    under the pull lock, and the results are written back under the state lock
    only if the replica is still the same hub, principal and log_epoch.
    Raises HubConfigError when the hub answers without `scan_state` (it predates
    scan metadata) or the replica or log moved meanwhile. A HubUnavailable
    propagates, with the replica untouched.
    """
    hub = client.identity
    with store.pulling(target):
        with store.locked(target):
            live = store.replica(target)
            epoch, principal = str(live.meta.get("log_epoch") or ""), live.meta.get("principal")
            stale = {
                key: head for key, head in live.readable_heads(settings.read_scopes).items() if "scan_state" not in head
            }
        fetched: dict[str, dict[str, Any]] = {}
        for key, head in stale.items():
            try:
                body = client.call(
                    "card", scope=head["scope"], card_id=head["card_id"], revision=head["revision"], offset=0
                )
            except hubc.HubRefusal as exc:
                if exc.status == 403 and exc.code == "memory_grant_missing":
                    _drop_revoked_scope(target, hub, principal, epoch, head["scope"])
                if exc.code in ("memory_card_not_found", "memory_grant_missing"):
                    raise hubc.HubConfigError(
                        f"the hub no longer serves {head['scope']}/{head['card_id']} at revision {head['revision']}"
                        ". Run memory hub pull"
                    ) from exc
                raise
            if str(body.get("log_epoch") or "") != epoch:
                raise hubc.HubConfigError(
                    "the hub's log_epoch changed during the metadata refresh. Run memory hub pull"
                )
            card = hubc.obj(body.get("card"))
            if "scan_state" not in card:
                raise hubc.HubConfigError("the hub's card read has no scan_state: the hub predates scan metadata")
            if head.get("content_hash") and card.get("content_hash") not in (None, head["content_hash"]):
                raise hubc.HubConfigError(
                    f"the hub's {head['scope']}/{head['card_id']} differs from the replica. Run memory hub pull"
                )
            fetched[key] = {name: card[name] for name in SCAN_FIELDS if name in card}
        with store.locked(target):
            live = store.replica(target)
            if not live.belongs_to(hub, principal) or str(live.meta.get("log_epoch") or "") != epoch:
                raise hubc.HubConfigError("the replica changed during the metadata refresh. Run memory hub pull")
            updated = {
                key: {**live.heads[key], **fields}
                for key, fields in fetched.items()
                if key in live.heads and live.heads[key].get("revision") == stale[key].get("revision")
            }
            # The added fields can grow a replica that sits near the cap. A replica over it would be
            # accepted as complete, because nothing is marked missing, so refuse and save nothing.
            grown = live.bytes() + sum(
                store.head_size(key, head) - store.head_size(key, live.heads[key]) for key, head in updated.items()
            )
            if grown > settings.replica_max_bytes:
                raise hubc.HubConfigError(
                    "refreshing scan metadata would push the replica past its "
                    f"{settings.replica_max_bytes // (1024 * 1024)} MB cap, replica unchanged. "
                    "Increase [hub] replica_max_mb and run memory hub pull"
                )
            live.heads.update(updated)
            if updated:
                live.save()
    return len(fetched)


def _drop_revoked_scope(target: Path, hub: str, principal: object, epoch: str, scope: str) -> None:
    """Take a scope the hub just refused out of the live replica and save it, as `_apply` does. Hold the pull lock.

    Only when the replica is still this hub, principal and log_epoch. A refusal
    that is not about a grant never reaches here, so an ordinary outage keeps the replica.
    """
    with store.locked(target):
        live = store.replica(target)
        if not live.belongs_to(hub, principal) or str(live.meta.get("log_epoch") or "") != epoch:
            return
        kept = {name for name in live.meta.get("read_scopes") or [] if name != scope}
        live.retain_scopes(kept)
        live.meta["read_scopes"] = sorted(kept)
        live.save()


def _prepare(
    target: Path, hub: str, epoch: str, principal: str, readable: set[str], report: dict[str, Any]
) -> tuple[bool, store.Replica]:
    """Discard a replica from another epoch or principal and pick what this pull writes into. Hold `locked`."""
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
    return swap, replica


def _follow(
    client: hubc.MemoryHubClient,
    replica: store.Replica,
    settings: hubc.HubSettings,
    *,
    epoch: str,
    readable: set[str],
    report: dict[str, Any],
    save: Callable[[], None],
) -> None:
    """Apply the log after the watermark. Scopes readable for the first time are read from the start.

    `save` writes the replica under the state lock; it runs every
    SAVE_EVERY_PAGES pages so an interrupted rebuild resumes.
    """
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
            if event["scope"] not in readable:
                continue  # refused earlier in this page
            report["applied"] += 1
            used = _apply(
                client,
                replica,
                settings,
                key,
                event,
                epoch=epoch,
                used=used,
                readable=readable,
                report=report,
                save=save,
            )
        next_seq = page.get("next_seq")
        if not isinstance(next_seq, int) or next_seq <= after:
            break
        after = next_seq
        replica.meta["seq"] = max(after, watermark)
        pages += 1
        if pages % SAVE_EVERY_PAGES == 0:
            # Bounded progress for a long rebuild; the full write happens once at the end.
            save()
    _refill(client, replica, settings, epoch=epoch, used=used, readable=readable, report=report, save=save)


def _apply(
    client: hubc.MemoryHubClient,
    replica: store.Replica,
    settings: hubc.HubSettings,
    key: str,
    event: dict[str, Any],
    *,
    epoch: str,
    used: int,
    readable: set[str],
    report: dict[str, Any],
    save: Callable[[], None],
) -> int:
    """Replace the head at `key` with the card `event` names. Returns the replica's bytes in use.

    A `card` read the hub refuses for the scope's grant takes the scope out
    of `readable`, which `_pull` saves as the replica's read scopes, and drops
    every head and cap entry the replica holds in it, so nothing pulled from
    that scope before the refusal is served or marked readable. The narrowed
    replica is saved at once, so fallback search stops serving the scope even
    if a later request in this pull fails.
    """
    replica.missing.pop(key, None)
    previous = replica.heads.pop(key, None)
    if previous:
        used -= store.head_size(key, previous)
    if event["kind"] == "card.retire":
        return used
    try:
        card = _card_text(client, event["scope"], event["card_id"], event["revision"], epoch)
    except _ScopeRefused as exc:
        readable.discard(exc.scope)
        replica.retain_scopes(readable)
        replica.meta["read_scopes"] = sorted(set(replica.meta.get("read_scopes") or []) & readable)
        save()
        report["warnings"].append(
            f"the hub refused {exc.scope} during the pull; its replicated cards were dropped and it is not readable"
        )
        return replica.bytes()
    if card is None or card.get("state") == "retired":
        return used
    head: dict[str, Any] = {
        "scope": event["scope"],
        "card_id": event["card_id"],
        "revision": card.get("revision", event["revision"]),
        "content_hash": card.get("content_hash", ""),
        "trust": card.get("trust", ""),
        "text": card["text"],
        **{name: card[name] for name in SCAN_FIELDS if name in card},
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
    client: hubc.MemoryHubClient,
    replica: store.Replica,
    settings: hubc.HubSettings,
    *,
    epoch: str,
    used: int,
    readable: set[str],
    report: dict[str, Any],
    save: Callable[[], None],
) -> None:
    """Fetch cards left out for the cap whose recorded size now fits."""
    for key, item in sorted(replica.missing.items()):
        if key not in replica.missing:
            continue  # dropped with a scope the hub refused earlier in this loop
        size = item.get("size")
        if isinstance(size, int) and used + size > settings.replica_max_bytes:
            continue
        event = {"kind": "card.put", **{k: item.get(k) for k in ("scope", "card_id", "revision")}}
        if not (isinstance(event["scope"], str) and isinstance(event["card_id"], str)):
            replica.missing.pop(key, None)
            continue
        used = _apply(
            client, replica, settings, key, event, epoch=epoch, used=used, readable=readable, report=report, save=save
        )


# --- push ----------------------------------------------------------------------------


VISIBILITY_PRIVATE = "visibility-private"
VISIBILITY_UNRECOGNIZED = "visibility-unrecognized"
_VISIBILITY_LINE = re.compile(r"""^\s*(['"]?)visibility\1\s*:(.*)$""", re.IGNORECASE)


def withheld(text: str) -> str | None:
    """Why a card's text must never reach the hub, or None when it is public. Fails closed.

    A leading U+FEFF is ignored. Every `visibility:` line in the frontmatter
    counts, whatever its case and whether or not the key is quoted, and the first word of its value decides: no
    such line or `public` publishes, `private` is `visibility-private`, and
    anything else, an empty value included, is `visibility-unrecognized`. A
    `visibility:` line in a leading block that does not parse as frontmatter
    (no closing fence, a malformed fence, no fence at all) is
    `visibility-unrecognized` too. Push, spool replay and import all ask this.
    """
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and lines[start] == "---":
        block = lines[start + 1 :]
        end = next((index for index, line in enumerate(block) if line.strip() == "---"), None)
        parsed = start == 0 and end is not None
        block = block[:end] if end is not None else block
    elif start < len(lines) and lines[start].strip().startswith("---"):
        block, parsed = lines[start + 1 :], False
        end = next((index for index, line in enumerate(block) if line.strip() == "---"), None)
        block = block[:end] if end is not None else block
    else:
        end = next((index for index, line in enumerate(lines[start:]) if not line.strip()), None)
        block, parsed = lines[start : start + end if end is not None else None], False
    values = [match.group(2) for match in map(_VISIBILITY_LINE.match, block) if match]
    if not values:
        return None
    if not parsed:
        return VISIBILITY_UNRECOGNIZED
    words = [value.split()[0].strip("'\"").lower() if value.split() else "" for value in values]
    if "private" in words:
        return VISIBILITY_PRIVATE
    if any(word != "public" for word in words):
        return VISIBILITY_UNRECOGNIZED
    return None


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
        reason = withheld(text)
        cards.append(
            {
                "rel": fields["rel"],
                "card_id": identity.card_id,
                # Not publishable: `withheld` says why.
                "private": reason is not None,
                "withheld": reason,
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


def _resolves(records: list[dict[str, Any]], card: dict[str, Any], head: dict[str, Any]) -> bool:
    """The local card ends these conflicts: it holds the head each one reported, and the replica agrees.

    The replica alone is not enough: one pulled before the other writer's
    edit holds an older head, and the local card matching it says nothing
    about the hub. A proposal that reported no hub head never resolves here.
    """
    if not head or head.get("content_hash") != card["content_hash"]:
        return False
    for record in records:
        reported = record.get("hub") or {}
        if reported.get("current_hash") != card["content_hash"]:
            return False
        if reported.get("current_revision") != head.get("revision"):
            return False
    return True


def _publishable(
    target: Path,
    settings: hubc.HubSettings,
    hub: str,
    principal: object,
    epoch: str | None,
    confirm: Callable[[str, dict[str, Any]], bool],
) -> tuple[list[dict], list[dict], list[dict], list[str], list[dict]]:
    """Local cards whose bytes differ from what this workspace last published here.

    Also returns the proposals this run resolves: the local card now holds
    exactly the hub head the conflict reported, the replica holds that head
    too, and `confirm(card_id, head)` says the hub's head is that one now,
    so the conflict is over and that head is the base. A card never
    published here whose bytes the replica's head holds adopts that head
    only when the replica was pulled under `epoch`, the log_epoch the push
    preflight reported; under another epoch it is skipped as `replica-stale`
    until a pull. The last item is every readable local card, so a spooled
    write replays only onto the card it came from (`_replay_refusal`).
    """
    scope = str(settings.scope)
    state = store.published(target).get(hub, {}).get(scope, {})
    spooled = {(str(e.get("scope")), str(e.get("card_id"))) for e in store.spool_entries(target, hub)}
    conflicts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in store.proposals(target, hub):
        entry = record.get("entry") or {}
        conflicts.setdefault((str(entry.get("scope")), str(entry.get("card_id"))), []).append(record)
    heads = _adoptable(target, settings, hub, principal)
    log_epoch = store.replica(target).meta.get("log_epoch")
    changes: list[dict] = []
    skipped: list[dict] = []
    adopted: list[dict] = []
    resolved: list[str] = []
    local = _local_cards(target)
    for card in local:
        if card["private"]:
            skipped.append({"path": card["rel"], "reason": card["withheld"]})
            continue
        if not hubc.valid_card_id(card["card_id"]):
            skipped.append({"path": card["rel"], "reason": "card-id-invalid"})
            continue
        key = (scope, card["card_id"])
        head = heads.get(store.Replica.key(scope, card["card_id"])) or {}
        matches_head = bool(head) and head.get("content_hash") == card["content_hash"]
        if (
            key in conflicts
            and key not in spooled
            and _resolves(conflicts[key], card, head)
            and confirm(card["card_id"], head)
        ):
            resolved.extend(str(record["proposal_id"]) for record in conflicts.pop(key))
            adopted.append({"card": card, "revision": head.get("revision"), "log_epoch": log_epoch})
            continue
        if key in spooled or key in conflicts:
            skipped.append({"path": card["rel"], "reason": "pending"})
            continue
        last = state.get(card["card_id"])
        if isinstance(last, dict) and epoch and last.get("log_epoch") and last.get("log_epoch") != epoch:
            # Published under another log_epoch: the restored hub may not hold
            # this card, so the old base proves nothing. Adopt a current head
            # or send the card again; a base the hub rejects becomes a proposal.
            last = None
        if isinstance(last, dict) and last.get("content_hash") == card["content_hash"]:
            continue
        if last is None and matches_head:
            if epoch is None or log_epoch != epoch:
                # The head was pulled under another log_epoch: its revision may
                # name another card version now. Pull, then push adopts it.
                skipped.append({"path": card["rel"], "reason": "replica-stale"})
                continue
            # The hub already holds these exact bytes: adopt its head as the base.
            adopted.append({"card": card, "revision": head.get("revision"), "log_epoch": log_epoch})
            continue
        changes.append({**card, "base": last if isinstance(last, dict) else None})
    return changes, skipped, adopted, resolved, local


def _replay_refusal(target: Path, entry: dict[str, Any], local: list[dict[str, Any]], scope: str | None) -> str | None:
    """Why a spooled write must stay local instead of replaying, or None to replay it.

    It replays only into the current `[hub] scope` (`scope`), and only when
    exactly one current, readable, public local card holds its card id, at
    the path it was spooled from. Anything else (the scope changed, or the
    card turned private, was renamed, moved, re-identified, duplicated,
    deleted or made unreadable) fails closed: a moved public card waits for
    review too, so a write never goes out under a stale identity or into a
    scope the config left.
    """
    if entry.get("scope") != scope:
        return "scope-changed"
    card_id, rel = entry.get("card_id"), entry.get("path")
    holders = [card for card in local if card["card_id"] == card_id]
    at_path = [card for card in local if card["rel"] == rel]
    withheld_by = [card["withheld"] for card in holders + at_path if card["private"]]
    if VISIBILITY_PRIVATE in withheld_by:
        return "card-now-private"
    if withheld_by:
        return "card-visibility-unrecognized"
    if len(holders) > 1:
        return "card-id-duplicated"
    if holders and holders[0]["rel"] == rel:
        return None
    if holders or at_path:
        return "card-identity-changed"
    if isinstance(rel, str) and (target / rel).exists():
        return "card-unreadable"
    return "card-gone"


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
        hubc.refuse_admin(body)
        self.epoch = str(body.get("log_epoch") or "")
        self.principal = body.get("principal")


def _hub_holds(
    target: Path,
    client: hubc.MemoryHubClient,
    preflight: _Preflight,
    scope: str,
    card_id: str,
    head: dict[str, Any],
) -> bool:
    """The hub's head for the card is the replica's `head` now, under the epoch the replica was pulled in.

    Both the conflict reply and the replica are snapshots: another write
    after the pull, or a restore since, leaves them agreeing on a head the
    hub no longer holds. Any mismatch, refusal or unavailable answer says no.
    """
    epoch = store.replica(target).meta.get("log_epoch")
    if not preflight.reachable or preflight.epoch is None or preflight.epoch != epoch:
        return False
    try:
        _require_current_head(client, scope, card_id, head, epoch)
    except (hubc.HubConfigError, hubc.HubRefusal, hubc.HubUnavailable):
        return False
    return True


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

    def confirm(card_id: str, head: dict[str, Any]) -> bool:
        return _hub_holds(target, client, preflight, scope, card_id, head)

    changes, skipped, adopted, resolved, local = _publishable(
        target, settings, hub, principal, preflight.epoch, confirm
    )
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
    kept: dict[str, str] = {}
    for proposal_id in resolved:
        closed = store.drop_proposal(target, proposal_id, outcome="resolved-local-matches-hub")
        if closed is not None:
            kept[proposal_id] = closed["dropped_to"]
    run = _Push(target, client, preflight.epoch, reachable=preflight.reachable)
    for entry in store.spool_entries(target, hub):
        refusal = _replay_refusal(target, entry, local, settings.scope)
        if refusal is not None:
            # The card or the scope is not the one this write was spooled
            # for any more: keep its text as a local proposal and never send it.
            run._propose(entry, refusal)
            continue
        (holder,) = [card for card in local if card["card_id"] == entry.get("card_id")]
        if holder["content_hash"] != hubc.content_hash(str(entry.get("text", ""))):
            # The card changed since this write was spooled, maybe to take a
            # line back out: send the card as it is now, on the same base,
            # under a new client_event_id, and never the spooled text. If an
            # earlier try of the old write committed and its answer was lost,
            # the hub refuses this base and the current text becomes a proposal.
            store.spool_remove(target, hub, entry["client_event_id"])
            run.send({**entry, "client_event_id": hubc.event_id(), "text": holder["text"]}, spooled=False)
            continue
        run.send(entry, spooled=True)
    for card in changes:
        run.send(_entry(scope, card, hub), spooled=False)
    # A card skipped as pending whose spooled write went out in this run is
    # no longer pending.
    still = store.pending_cards(target, hub)
    keys = {card["rel"]: (scope, card["card_id"]) for card in local}
    skipped = [item for item in skipped if item["reason"] != "pending" or keys.get(item["path"]) in still]
    if run.unavailable and preflight.reachable:
        warnings.append(f"hub became unavailable ({run.unavailable}); the rest is spooled")
    if run.report.get("kept_local"):
        warnings.append(f"the spool is full ({store.SPOOL_CAP} entries); these writes stay local")
    if run.report["proposals"]:
        warnings.append("some writes became local proposals for review (memory hub proposals list shows them)")
    run.report["adopted"] = [item["card"]["card_id"] for item in adopted]
    run.report["resolved"] = resolved
    if kept:
        run.report["resolved_kept"] = kept
        warnings.append(
            f"{len(kept)} proposal(s) resolved because the local card now holds the hub's text; "
            f"their own text is kept under {store.dropped_dir(target)}"
        )
    return {"ok": True, "scope": scope, **run.report, "skipped": skipped, "warnings": warnings}


DROP_KEEP_LOCAL = "keep-local"
DROP_KEEP_HUB = "keep-hub"


class DropRefused(hubc.HubConfigError):
    """`proposals drop` cannot act safely now; nothing changed."""


def drop_proposal(
    target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings, proposal_id: str, *, keep: str
) -> dict[str, Any] | None:
    """Close a reviewed proposal, keeping either the local text or the hub's head.

    - `keep-local`: the replica's hub head becomes the card's base, so the next
      push writes the local card on top of it and replaces the other writer's
      text on the hub. When another writer retired the card, the retired
      head's revision becomes the base, so the next push revives the card
      with the local text. When the card is gone, private or already holds
      the hub head, the next push sends nothing, and `local_card` says why.
    - `keep-hub`: the local card file is rewritten with the replica's hub head,
      which becomes the published base, so the next push sends nothing for it.
      It refuses a private local card, which the hub's public text would replace.

    Both need a replica pulled from this hub, as the principal and under the
    log_epoch its `status` reports now, whose head for the card is the one the
    hub's `card` answers now; otherwise they refuse and say to pull first. A
    replica pulled before the conflicting write has the right epoch and an old
    head. `keep-hub` finds the local card by its id, so a moved card is
    rewritten where it is. Both refuse when more than one file holds the id,
    or when the proposal's scope is not `[hub] scope`, the only scope push
    compares cards against. The closed record, and for `keep-hub` the card
    file's text it replaced, move to `proposals/dropped/`. None when there is
    no such proposal.
    """
    if keep not in {DROP_KEEP_LOCAL, DROP_KEEP_HUB}:
        raise ValueError(keep)
    status = read_status(client, settings)
    hub = client.identity
    with store.locked(target):
        record = store.read_proposal(target, proposal_id)
        if record is None:
            return None
        entry = record.get("entry") or {}
        scope, card_id = entry.get("scope"), entry.get("card_id")
        if entry.get("hub") != hub:
            raise DropRefused(f"{proposal_id} is for another hub ({entry.get('hub')}); configure that hub to drop it")
        if not (isinstance(scope, str) and hubc.valid_card_id(card_id)):
            raise DropRefused(f"{proposal_id} names no valid scope and card id")
        assert isinstance(card_id, str)
        if scope != settings.scope:
            raise DropRefused(
                f"{proposal_id} is for {scope}, but [hub] scope is now {settings.scope or 'unset'}; "
                f'set [hub] scope = "{scope}" to drop it'
            )
        replica = store.replica(target)
        if not (
            replica.belongs_to(hub, status["principal"]) and replica.meta.get("log_epoch") == status.get("log_epoch")
        ):
            raise DropRefused(
                "the replica was not pulled from this hub as this principal under its current log_epoch; "
                "run `brigade memory hub pull` first"
            )
        key = store.Replica.key(scope, card_id)
        if key in replica.missing:
            raise DropRefused(f"{scope}/{card_id} is left out of the replica for its size cap; raise replica_max_mb")
        if scope not in settings.read_scopes:
            raise DropRefused(
                f"{scope} is not one of the [hub] read scopes, so the replica holds no head for {scope}/{card_id}; "
                "add it to read_scopes and pull"
            )
        head = replica.readable_heads(settings.read_scopes).get(key)
        epoch = replica.meta.get("log_epoch")
        retired = _require_current_head(client, scope, card_id, head, epoch)
        holder = _single_holder(target, card_id)
        extra: dict[str, Any] = {}
        local_card = "present"
        if keep == DROP_KEEP_HUB:
            if holder is not None and holder["private"]:
                raise DropRefused(
                    f"{holder['rel']} is private, and --keep-hub would replace it with the hub's public text; "
                    "drop with --keep-local, or edit the card yourself"
                )
            if head is None:
                raise DropRefused(
                    f"the replica holds no hub head for {scope}/{card_id}; it is retired or absent, "
                    "so there is no hub text to keep"
                )
            path, previous = _keep_hub_card(target, card_id, holder, entry.get("path"))
            extra["card_text_replaced"] = previous
            closed = store.drop_proposal(target, proposal_id, outcome=DROP_KEEP_HUB, **extra)
            assert closed is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(str(head.get("text", "")))
        else:
            if holder is None:
                local_card = "gone"
            elif holder["private"]:
                local_card = "private"
            elif head is not None and holder["content_hash"] == head.get("content_hash"):
                local_card = "matches-hub"
            closed = store.drop_proposal(target, proposal_id, outcome=DROP_KEEP_LOCAL)
            assert closed is not None
        state = store.published(target)
        cards = state.setdefault(hub, {}).setdefault(scope, {})
        base = head if head is not None else retired
        if head is not None:
            cards[card_id] = {
                "revision": head.get("revision"),
                "content_hash": head.get("content_hash"),
                "log_epoch": epoch,
            }
        elif retired is not None:
            # Another writer retired the card. The hub keeps the retired head
            # and wants its revision as the base, so the next push revives the
            # card with the local text. No content hash, so that push always sends.
            cards[card_id] = {"revision": retired.get("revision"), "content_hash": None, "log_epoch": epoch}
        else:
            # No hub head under this epoch: the next push sends no base.
            cards.pop(card_id, None)
        store.save_published(target, state)
        return {
            "proposal_id": proposal_id,
            "card_id": card_id,
            "scope": scope,
            "keep": keep,
            "local_card": local_card,
            "rebased_on": base.get("revision") if base is not None else None,
            "dropped_to": closed["dropped_to"],
        }


def _require_current_head(
    client: hubc.MemoryHubClient, scope: str, card_id: str, head: dict[str, Any] | None, epoch: object
) -> dict[str, Any] | None:
    """Refuse unless the replica's head for the card is the hub's head now.

    A retired head counts as no head, as in the replica, and is returned, so
    a caller can name its revision as a base: the hub needs it to revive the
    card. None otherwise.
    """
    try:
        body = client.call("card", card_id=card_id, scope=scope, offset=0)
    except hubc.HubRefusal as exc:
        if exc.code != "memory_card_not_found":
            raise
        live: dict[str, Any] | None = None
        retired: dict[str, Any] | None = None
    else:
        if str(body.get("log_epoch") or "") != str(epoch or ""):
            raise DropRefused("the hub's log_epoch changed; run `brigade memory hub pull` first")
        card = hubc.obj(body.get("card"))
        live = None if card.get("state") == "retired" else card
        retired = card if live is None else None
    hub_head = (live.get("revision"), live.get("content_hash")) if live is not None else None
    replica_head = (head.get("revision"), head.get("content_hash")) if head is not None else None
    if hub_head != replica_head:
        held = f"revision {replica_head[0]}" if replica_head else "no head"
        current = f"revision {hub_head[0]}" if hub_head else "no head"
        raise DropRefused(
            f"the replica is behind the hub for {scope}/{card_id} (replica: {held}, hub: {current}); "
            "run `brigade memory hub pull` first"
        )
    return retired


def _single_holder(target: Path, card_id: str) -> dict[str, Any] | None:
    """The one readable local card holding `card_id`, or None. More than one refuses."""
    holders = [card for card in _local_cards(target) if card["card_id"] == card_id]
    if len(holders) > 1:
        names = ", ".join(sorted(card["rel"] for card in holders))
        raise DropRefused(f"more than one local card holds id {card_id} ({names}); keep one and drop again")
    return holders[0] if holders else None


def _keep_hub_card(target: Path, card_id: str, holder: dict[str, Any] | None, rel: object) -> tuple[Path, str | None]:
    """The local card file `keep-hub` rewrites, and its text now (None when it is gone).

    The card is found by id (`holder`), so a card that moved since the
    proposal is rewritten where it is now. A file at the recorded path that
    holds another id or cannot be read refuses.
    """
    path = _card_path(target, holder["rel"] if holder else rel)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DropRefused(f"{path} is not a regular card file")
    if not path.exists():
        return path, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DropRefused(f"{path} cannot be read as UTF-8 text ({type(exc).__name__}); fix or move it") from exc
    if holder is None:
        raise DropRefused(f"{path} no longer holds card id {card_id}; move the card back or drop with --keep-local")
    return path, text


def _card_path(target: Path, rel: object) -> Path:
    """The local card a proposal came from, refused unless it is a `.md` file under the workspace's memory/."""
    if not isinstance(rel, str) or not rel.endswith(".md"):
        raise DropRefused("the proposal names no local card path")
    memory = (target / "memory").resolve()
    path = (target / rel).resolve()
    if memory not in path.parents:
        raise DropRefused(f"the proposal's card path {rel} is outside {memory}")
    return target / rel


# --- import --------------------------------------------------------------------------


def import_cards(target: Path, client: hubc.MemoryHubClient, *, scope: str, paths: list[Path]) -> dict[str, Any]:
    """One `import` call per card. Refusals carry the hub's code. Nothing is spooled.

    The hub's `status` is read first so an admin credential is refused. A
    credential with no read grant cannot read status; it still imports, and
    the hub checks each card's import grant.
    """
    from . import card_identity, memory_cmd

    hub_status: dict[str, Any] = {}
    try:
        hub_status = hubc.refuse_admin(client.call("status"))
    except hubc.HubRefusal as exc:
        if not (exc.status == 403 and exc.code == "memory_grant_missing"):
            raise
    live = store.replica(target)
    # A replica head is a base only when the replica was pulled as the
    # principal and under the log_epoch the hub reports now. Otherwise the
    # import names no base and the hub answers whether one is needed.
    current = live.belongs_to(client.identity, hub_status.get("principal")) and live.meta.get(
        "log_epoch"
    ) == hub_status.get("log_epoch")
    heads = live.readable_heads([scope]) if current else {}
    results: list[dict[str, Any]] = []
    for path in paths:
        rel = path.as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            results.append({"path": rel, "status": "unreadable", "error": type(exc).__name__})
            continue
        reason = withheld(text)
        if reason is not None:
            results.append({"path": rel, "status": "skipped", "reason": reason})
            continue
        meta, _ = memory_cmd._parse_frontmatter(text)
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
