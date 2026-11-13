"""Retirement requires both demonstrated non-use and an ended subject."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import memory_archive, memory_card_reads, memory_cmd, memory_hub_sync

if TYPE_CHECKING:
    from .memory_care_tend import TendConfig

Judge = Callable[[str, str], str]


@dataclass(frozen=True)
class Candidate:
    card_id: str
    file: str
    last_read: str | None
    signal: str


def plan(
    target: Path, config: memory_cmd.MemoryCareConfig, tend: TendConfig, *, today: date, judge: Judge | None = None
) -> dict[str, Any]:
    ledger = memory_card_reads.load(target)
    hub_reads = memory_card_reads.hub_last_reads(target)
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
        card_id = memory_cmd.card_identity_from_text(text, rel)
        read = max(
            filter(
                None,
                [
                    memory_card_reads.last_read(ledger, (card_id, rel)),
                    hub_reads.get(memory_hub_sync.publication_identity(text, rel)[0]),
                ],
            ),
            default=None,
        )
        if read is not None and (today - read).days < tend.unused_after_days:
            skipped["read_recently"] += 1
            continue
        expires = memory_cmd._parse_date(fm.get("expires_at") or fm.get("expires"))
        signal = "subject-expired" if expires and expires < today else ""
        if not signal and judge is not None:
            reviewed = memory_cmd._parse_date(fm.get("last_reviewed"))
            if reviewed and (today - reviewed).days > config.stale_after_days and judge(card_id, text) == "ended":
                signal = "subject-ended"
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
    judge: Judge | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = plan(target, config, tend, today=today, judge=judge)
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
