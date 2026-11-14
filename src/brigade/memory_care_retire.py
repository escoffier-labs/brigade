"""Unread cards are never archived for non-use alone.

Automatic retirement needs a positive ended-subject signal, currently a past
expires_at or expires date. A read within unused_after_days blocks retirement.
Missing read history protects cards too.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import memory_archive, memory_card_reads, memory_cmd, memory_hub_sync

if TYPE_CHECKING:
    from .memory_care_tend import TendConfig


@dataclass(frozen=True)
class Candidate:
    card_id: str
    file: str
    last_read: str | None
    signal: str


def plan(target: Path, config: memory_cmd.MemoryCareConfig, tend: TendConfig, *, today: date) -> dict[str, Any]:
    ledger = memory_card_reads.load(target)
    hub_reads = memory_card_reads.hub_last_reads(target, unused_after_days=tend.unused_after_days)
    candidates = []
    skipped = {"ledger_too_young": 0, "exempt": 0, "read_recently": 0}
    for path in memory_cmd._iter_cards(target, config):
        rel = path.relative_to(target).as_posix()
        text = path.read_text(encoding="utf-8")
        fm, _ = memory_cmd._parse_frontmatter(text)
        if memory_cmd._is_read_only(config, rel) or memory_archive.frontmatter_refusal(text):
            skipped["exempt"] += 1
            continue
        if (today - ledger.tracking_since).days < tend.unused_after_days:
            skipped["ledger_too_young"] += 1
            continue
        if hub_reads is None:
            # A configured hub that cannot report reads proves nothing about use.
            skipped["hub_reads_unavailable"] = skipped.get("hub_reads_unavailable", 0) + 1
            continue
        card_id = memory_cmd.card_identity_from_text(text, rel)
        # Replica reads are recorded under the publication id, which is derived for a card without one.
        published = memory_hub_sync.publication_identity(text, rel)[0]
        read = max(
            filter(
                None,
                [
                    memory_card_reads.last_read(ledger, (card_id, rel, published)),
                    hub_reads.get(published),
                ],
            ),
            default=None,
        )
        if read is not None and (today - read).days < tend.unused_after_days:
            skipped["read_recently"] += 1
            continue
        expires = memory_cmd._parse_date(fm.get("expires_at") or fm.get("expires"))
        signal = "subject-expired" if expires and expires < today else ""
        if signal:
            candidates.append(asdict(Candidate(card_id, rel, read.isoformat() if read else None, signal)))
    candidates.sort(key=lambda c: (c["last_read"] or "", c["file"]))
    return {
        "schema_version": 2,
        "approval_required": False,
        "would_archive": tend.enabled,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "skipped": skipped,
    }


def apply(
    target: Path,
    config: memory_cmd.MemoryCareConfig,
    tend: TendConfig,
    *,
    today: date,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = plan(target, config, tend, today=today)
    decisions: list[str] = []
    refused = []
    if tend.enabled and not dry_run:
        for candidate in payload["candidates"]:
            if len(decisions) >= tend.max_archives_per_run:
                break
            try:
                d = memory_archive.archive_card(target, candidate["file"], reason=candidate["signal"], config=config)
                decisions.append(d.decision_id)
            except memory_archive.ArchiveRefused as exc:
                refused.append(str(exc))
    return {**payload, "archived": len(decisions), "decision_ids": decisions, "refused": refused}


def status_view(target: Path, config: memory_cmd.MemoryCareConfig) -> dict[str, Any]:
    from . import memory_care_tend
    from .memory_hub_store import read_json

    try:
        p = read_json(target / memory_care_tend.STATE_REL) or {}
        retirement = p.get("stages", {}).get("retire", {})
    except (OSError, ValueError):
        retirement = {}
    return {
        **retirement,
        "schema_version": 2,
        "approval_required": False,
        "would_archive": memory_care_tend.load_config(target).enabled,
        "candidate_count": retirement.get("candidate_count", 0),
        "candidates": retirement.get("candidates", []),
    }
