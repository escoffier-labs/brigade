"""Which overflow cards are holding review drafts, for the report to read.

One card that is not one this feature created holds every later handoff for its file and type
(`overflow-card-untrusted`) until someone repairs or removes it. `refresh` recomputes, from the
review inbox as it is (every draft the drain retries as a document append, whatever code it was first
held with), each such card and how many drafts wait on it, and rewrites
`.brigade/memory-care/inbox/blocked-cards.json`. Ingest and the drain both call it, so the file is
level-triggered: a repaired card disappears from it at the next run, and nothing here needs a person.

The file holds workspace-relative card paths, a fixed problem phrase, and counts. It never holds
handoff content.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from . import instruction_overflow, memory_care_journal as journal
from .memory_hub_store import write_json

BLOCKED_REL = ".brigade/memory-care/inbox/blocked-cards.json"
CODE = "overflow-card-untrusted"


def refresh(target: Path, *, routing: instruction_overflow.RouteContext | None = None) -> list[dict[str, Any]]:
    """The blocked cards now, as `[{"path", "problem", "held_drafts"}]`, written to `BLOCKED_REL`.

    The file is only touched when there is something to record or an old record to clear.
    """
    from . import ingest, memory_inbox_drain as drain
    from .memory_inbox_io import read

    target = target.resolve()
    routing = routing or instruction_overflow.RouteContext(target)
    counts: Counter[str] = Counter()
    problems: dict[str, str] = {}
    try:
        drafts = drain.pending(target)
    except (ValueError, OSError):
        drafts = []
    for draft in drafts:
        try:
            data = read(target, draft.relative_to(target).as_posix())
            if data is None:
                continue
            code, routed = drain.parse_header(data.decode("utf-8"))
            # A draft keeps the code it was first held with (`document-destination-unwritable`, an old budget
            # hold), so every draft the drain would retry as a document append is judged as it is now.
            if code not in drain.RETRY_CODES or routed is None:
                continue
            flags = drain.retry_flags(code, data.decode("utf-8"))
            if flags is None or not flags[1]:
                continue
            outcome = ingest.decide(
                ingest.parse(draft, data),
                target=target,
                promote_cards=flags[0],
                route_documents=flags[1],
                routing=routing,
            )
        except (ValueError, OSError):
            continue
        if outcome.code != CODE or outcome.overflow is None or not outcome.overflow.card_untrusted:
            continue
        counts[outcome.overflow.card_rel] += 1
        problems[outcome.overflow.card_rel] = outcome.overflow.card_untrusted
    cards = [{"path": rel, "problem": problems[rel], "held_drafts": counts[rel]} for rel in sorted(counts)]
    path = journal.safe_path(target, BLOCKED_REL)
    if cards or path.exists():
        try:
            write_json(path, {"version": 1, "cards": cards})
        except OSError:
            pass  # informational: the cards are still returned to the caller
    return cards


def load(target: Path) -> list[dict[str, Any]]:
    """The recorded blocked cards, for the report. Empty when there is no record."""
    from .memory_hub_store import read_json

    value = read_json(journal.safe_path(target, BLOCKED_REL))
    cards = value.get("cards") if isinstance(value, dict) else None
    return [row for row in cards if isinstance(row, dict)] if isinstance(cards, list) else []
