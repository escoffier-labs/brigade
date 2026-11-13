"""Journaled append publication and exact-block undo for inbox decisions."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from . import memory_care_journal as journal


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
    for item in payload.get("appends", []):
        data = base64.b64decode(item["data"])
        path = journal.safe_path(target, item["path"])
        # The stored timestamp or decision-specific ledger row identifies this append.
        if _contains(target, item["path"], data):
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
    if payload.get("appends"):
        journal.annotate(target, decision_id, appends_complete=True)


def undo_changes(target: Path, payload: dict[str, Any]) -> list[journal.Change]:
    """Match only our bytes; preserve every other block, including later appends."""
    changes = []
    for item in payload.get("appends", []):
        if item["writer"] != "document":
            continue
        data = base64.b64decode(item["data"])
        live = journal.current(target, item["path"])
        if live is None or live.count(data) != 1 or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise journal.JournalConflict("appended block missing or changed; file left unchanged")
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
