"""Deterministic candidate selection for the memory verification pass (D2).

Every function here is pure: it reads replica heads, per-card read counts and
the verify state, and returns card ids, scores and reason codes. Nothing
here calls a model, the hub or the filesystem, and no result carries card
text, titles or evidence.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import memory_fairness as fairness
from . import memory_proposal_v2_ops as delta
from . import memory_reflect_near as near

DAY_MS = 86_400_000
NEVER_VERIFIED_DAYS = 180
SUPPORTED_INTERVAL_DAYS = 30
MAX_CARD_TEXT_BYTES = 16 * 1024
PENDING_CEILING = 8
READS_7D_CAP = 7_000
READS_30D_CAP = 30_000
STALENESS_SHARE = 4  # One quarter of the slots, rounded up.
STALENESS_LANE = "staleness"
SCORE_LANE = "score"


@dataclass(frozen=True)
class Reads:
    reads_7d: int = 0
    reads_30d: int = 0
    total_reads: int = 0


@dataclass(frozen=True)
class Evidence:
    """What evidence assembly learns about a card's cited items: their digest and newest observation day."""

    digest: str
    newest_day: int


@dataclass(frozen=True)
class Card:
    scope: str
    card_id: str
    key: str
    score: float
    verified_day: int | None
    order: tuple[int, int, str]


@dataclass(frozen=True)
class Selection:
    selected: list[tuple[Card, str]]
    eligible: int
    skipped: dict[str, int]


def access(reads: Reads) -> float:
    return math.log2(1 + 4 * min(reads.reads_7d, READS_7D_CAP) + min(reads.reads_30d, READS_30D_CAP))


def volatility(revision_age_days: int | None, evidence_newer: bool) -> int:
    value = 0
    if revision_age_days is not None and revision_age_days >= 0:
        if revision_age_days <= 14:
            value += 2
        elif revision_age_days <= 60:
            value += 1
    return value + (2 if evidence_newer else 0)


def staleness(days_since_verified: int | None) -> float:
    days = NEVER_VERIFIED_DAYS if days_since_verified is None else max(0, days_since_verified)
    return min(days, NEVER_VERIFIED_DAYS) / 30


def score(
    reads: Reads, *, revision_age_days: int | None, evidence_newer: bool, days_since_verified: int | None
) -> float:
    """access + volatility + staleness, exactly as D2 defines them."""
    return access(reads) + volatility(revision_age_days, evidence_newer) + staleness(days_since_verified)


def updated_day(head: Mapping[str, Any]) -> int | None:
    """The UTC day of the head's last change, from the hub's `updated_at`. None when absent or unreadable."""
    value = head.get("updated_at")
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000) // DAY_MS


def interval_days(entry: Mapping[str, Any], head: Mapping[str, Any], evidence: Evidence | None, minimum: int) -> int:
    """30 days after a supported verdict on an unchanged revision and unchanged evidence, else the minimum.

    Unknown evidence never counts as unchanged.
    """
    unchanged = (
        entry.get("verdict") == "supported"
        and entry.get("revision") == head.get("revision")
        and evidence is not None
        and entry.get("evidence") == evidence.digest
    )
    return max(minimum, SUPPORTED_INTERVAL_DAYS) if unchanged else minimum


def skip_reason(
    head: Mapping[str, Any], entry: Mapping[str, Any], *, today: int, min_interval_days: int, evidence: Evidence | None
) -> str | None:
    """Why a card is not eligible this run, or None when it is."""
    text = head.get("text")
    if not isinstance(text, str):
        return "invalid_head"
    if head.get("state") == "retired":
        return "retired"
    if near._superseded(text):
        return "superseded"
    if head.get("scan_state") != "clean":
        return "non_clean"
    if head.get("trust") not in near.KNOWN_TRUST:
        return "flagged_trust"
    if len(text.encode()) > MAX_CARD_TEXT_BYTES:
        return "card_too_large"
    try:
        if not any(block.block_id for block in delta.blocks(text)):
            return "no_anchor"
    except ValueError:
        return "no_anchor"
    if entry.get("pending"):
        return "pending_proposal"
    verified = entry.get("verified")
    if verified is not None and today - verified < interval_days(entry, head, evidence, min_interval_days):
        return "recently_verified"
    return None


def select(
    heads: Iterable[Mapping[str, Any]],
    *,
    entries: Mapping[str, Mapping[str, Any]],
    reads: Mapping[str, Reads],
    slots: int,
    now_ms: int,
    min_interval_days: int,
    evidence: Mapping[str, Evidence] | None = None,
) -> Selection:
    """Pick up to `slots` cards: a staleness lane of a quarter, rounded up, then the highest scores.

    `entries`, `reads` and `evidence` are keyed by the fairness key digest of
    the card's scope and id. A card the state has never seen is first seen now.
    Ties follow the shared fairness order.
    """
    evidence = evidence or {}
    today = now_ms // DAY_MS
    skipped: dict[str, int] = {}
    if sum(1 for entry in entries.values() if entry.get("pending")) >= PENDING_CEILING:
        return Selection(selected=[], eligible=0, skipped={"pending_ceiling": 1})
    candidates: list[Card] = []
    for head in heads:
        key = fairness.key_digest(str(head.get("scope")), [str(head.get("card_id"))])
        entry = entries.get(key, {})
        found = evidence.get(key)
        reason = skip_reason(head, entry, today=today, min_interval_days=min_interval_days, evidence=found)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        verified = entry.get("verified")
        since = None if verified is None else today - verified
        changed = updated_day(head)
        newer = found is not None and (verified is None or found.newest_day > verified)
        value = score(
            reads.get(key, Reads()),
            revision_age_days=None if changed is None else today - changed,
            evidence_newer=newer,
            days_since_verified=since,
        )
        fair = [int(entry.get("seen", now_ms)), int(entry.get("proposed", 0))]
        candidates.append(
            Card(
                scope=str(head["scope"]),
                card_id=str(head["card_id"]),
                key=key,
                score=value,
                verified_day=verified,
                order=fairness.priority(key, fair),
            )
        )
    if slots <= 0:
        return Selection(selected=[], eligible=len(candidates), skipped=skipped)
    lane_slots = -(-slots // STALENESS_SHARE)
    # Oldest verification first, never verified before everything, regardless of reads.
    stale = sorted(candidates, key=lambda card: (-1 if card.verified_day is None else card.verified_day, card.order))
    chosen = [(card, STALENESS_LANE) for card in stale[:lane_slots]]
    taken = {card.key for card, _ in chosen}
    rest = sorted((card for card in candidates if card.key not in taken), key=lambda card: (-card.score, card.order))
    chosen.extend((card, SCORE_LANE) for card in rest[: slots - len(chosen)])
    deferred = len(candidates) - len(chosen)
    if deferred:
        skipped["verify_deferred_slots"] = skipped.get("verify_deferred_slots", 0) + deferred
    return Selection(selected=chosen, eligible=len(candidates), skipped=skipped)


def never_read(
    heads: Iterable[Mapping[str, Any]],
    *,
    entries: Mapping[str, Mapping[str, Any]],
    reads: Mapping[str, Reads],
    since_day: int | None,
    now_ms: int,
    never_read_days: int,
) -> list[str] | None:
    """Card ids first seen at least `never_read_days` ago with no read recorded.

    None until read counting itself has covered the whole window, so a card is
    never called never-read before the hub could have counted a read of it.
    """
    today = now_ms // DAY_MS
    if since_day is None or today - since_day < never_read_days:
        return None
    found = []
    for head in heads:
        key = fairness.key_digest(str(head.get("scope")), [str(head.get("card_id"))])
        seen = int(entries.get(key, {}).get("seen", now_ms)) // DAY_MS
        if today - seen >= never_read_days and reads.get(key, Reads()).total_reads == 0:
            found.append(str(head["card_id"]))
    return sorted(found)
