"""Journaled append publication and exact-block undo for inbox decisions."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from . import instruction_overflow
from . import memory_care_journal as journal

# Writers whose bytes land in a workspace document through the no-follow appender.
DOCUMENT_WRITERS = frozenset({"document", "card-append"})


def plan_append(path: str, data: bytes, writer: str, **fields: Any) -> dict[str, Any]:
    return dict(
        path=path,
        length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=base64.b64encode(data).decode("ascii"),
        writer=writer,
        **fields,
    )


def publish(target: Path, decision_id: str) -> None:
    """Recover a finished decision's appends, recognizing already published bytes."""
    from . import ingest, memory_evidence_log

    payload = journal._payload(target, decision_id)
    if (
        payload.get("appends_complete")
        or payload.get("phase") not in ("committed", "undone")
        or payload.get("undone_by")
    ):
        return
    from .memory_cmd import _config_or_default

    read_only = _config_or_default(target).read_only_paths
    blocked = False
    for item in payload.get("appends", []):
        data = base64.b64decode(item["data"])
        path = journal.safe_path(target, item["path"])
        # The stored timestamp or decision-specific ledger row identifies this append.
        if _contains(target, item["path"], data):
            continue
        if item["writer"] in DOCUMENT_WRITERS and journal.read_only_pattern(item["path"], read_only):
            # Protected since the decision finished: leave the append pending until it is lifted.
            blocked = True
            continue
        if item["writer"] in ("evidence", "evidence-retraction"):
            memory_evidence_log.append_log_row(target, data)
        elif item["writer"] == "ledger":
            from .memory_inbox_io import append

            append(target, item["path"], data)
        else:
            ingest._append_document_nofollow(target, path, data.decode("utf-8"))
        if not _contains(target, item["path"], data):
            raise journal.JournalConflict("append bytes differ from recorded block")
    if payload.get("appends") and not blocked:
        journal.annotate(target, decision_id, appends_complete=True)


def undo_changes(target: Path, payload: dict[str, Any]) -> list[journal.Change]:
    """Match only our bytes; preserve every other block, including later appends."""
    changes = []
    for item in payload.get("appends", []):
        if item["writer"] not in DOCUMENT_WRITERS:
            continue
        data = base64.b64decode(item["data"])
        live = journal.current(target, item["path"])
        if live is None or live.count(data) != 1 or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise journal.JournalConflict("appended block missing or changed; file left unchanged")
        header = int(item.get("header_bytes") or 0)
        if item["writer"] == "card-append" and item.get("created", True) and header:
            # The creating block carries the frontmatter. It must still start the card.
            if not live.startswith(data):
                raise journal.JournalConflict("appended block missing or changed; file left unchanged")
            # Later appends keep their frontmatter: remove only the body part of this block.
            after = data[:header] + live[len(data) :]
            changes.append(journal.Change(item["path"], live, after if len(live) > len(data) else None))
            continue
        after = live.replace(data, b"", 1)
        # Remove the file only when this decision created it; an existing empty document stays.
        changes.append(journal.Change(item["path"], live, after if after or not item.get("created", True) else None))
    return changes


def retractions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    from .memory_log_retractions import withdrawn

    return [
        plan_append(
            item["path"],
            withdrawn(base64.b64decode(item["data"])),
            "evidence-retraction" if item["writer"] == "evidence" else "ledger",
        )
        for item in payload.get("appends", [])
        if item["writer"] in ("evidence", "ledger")
    ]


def _contains(target: Path, rel: str, data: bytes) -> bool:
    from .memory_inbox_io import rows, contains

    if rel.endswith(".jsonl"):
        return any(line == data for line in rows(target, rel))
    return contains(target, rel, data)


@journal.serialized
def commit_card_append(
    owner: Path,
    card_rel: str,
    block: bytes,
    *,
    header_bytes: int,
    reason: str,
    card_limit: int | None = None,
    limit_requested: int | None = None,
) -> journal.Decision:
    """Journal one append to an overflow card and publish it with exact-block undo.

    Callers hold ``ingest._document_lock(owner)`` first, the order retry and revert use.
    `limit_requested` is the configured card limit when it was clamped to `card_limit`.
    """
    before = journal.current(owner, card_rel)
    if before is not None and (problem := instruction_overflow.untrusted_bytes(before)):
        # The bytes the append guard pins: the last look at the card before anything is written to it.
        raise journal.JournalConflict(f"overflow card is not one this feature created: {problem}")
    if (before is None) != (header_bytes > 0):
        # The block was built for a card that was missing (or present) and the card is not as it was then.
        raise journal.JournalConflict("overflow card changed since the block was built")
    metadata: dict[str, Any] = {
        "appends": [plan_append(card_rel, block, "card-append", created=before is None, header_bytes=header_bytes)],
        "append_guards": {card_rel: journal.kernel.content_digest(before)},
    }
    if limit_requested is not None:
        metadata["card_limit"], metadata["card_limit_requested"] = card_limit, limit_requested
    decision = journal.commit_phased(
        owner,
        kind="inbox",
        reason=reason,
        card_id=None,
        changes=[],
        prepared_paths=[],
        hub={},
        metadata=metadata,
    )
    if journal._payload(owner, decision.decision_id).get("phase") != "committed":
        raise journal.JournalConflict("card append abandoned")
    return decision
