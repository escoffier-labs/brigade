"""Collect redundant abandoned before-images without losing unique bytes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_care_journal as journal
from .memory_inbox_io import read


@journal.serialized
def sweep(target: Path, *, dry_run: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"swept": 0, "retained": 0, "decisions": [], "retentions": []}
    target = target.resolve()
    root = journal.safe_path(target, journal.JOURNAL_REL)
    if not root.exists():
        return result
    for directory in sorted(root.iterdir()):
        before = directory / "before"
        if not before.exists():
            continue
        try:
            payload = journal._payload(target, directory.name)
            if payload.get("phase") != "abandoned":
                continue
            if payload.get("restorable") or payload.get("repair_indexes"):
                result["retained"] += 1
                result["retentions"].append(
                    {"decision_id": directory.name, "reason": "restorable or pending index repair"}
                )
                continue
            decision = journal.load(target, directory.name)
            copies = [read(target, path) for path in decision.paths]
            changes = []
            for image in sorted(journal.safe_path(target, before.relative_to(target).as_posix()).iterdir()):
                rel = image.relative_to(target).as_posix()
                data = read(target, rel)
                if data is None or data not in copies:
                    raise ValueError("unique or unreadable before-image")
                changes.append(journal.Change(rel, data, None))
            if not changes:
                continue
            if not dry_run:
                saved = journal.commit_phased(
                    target,
                    kind="inbox",
                    reason=f"redundant-before-images:{directory.name}",
                    card_id=None,
                    changes=changes,
                    prepared_paths=[],
                    hub={},
                )
                result["decisions"].append(saved.decision_id)
                before.rmdir()
            result["swept"] += 1
        except (ValueError, OSError, KeyError, TypeError, journal.kernel.ProjectionError) as exc:
            result["retained"] += 1
            reason = (
                "unique or unreadable before-image"
                if str(exc) == "unique or unreadable before-image"
                else "unreadable or unsafe decision"
            )
            result["retentions"].append({"decision_id": directory.name, "reason": reason})
    return result
