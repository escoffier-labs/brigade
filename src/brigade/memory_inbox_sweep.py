"""Collect redundant abandoned before-images without losing unique bytes."""

from __future__ import annotations

import hashlib
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
            copies = {path: read(target, path) for path in decision.paths}
            removals: list[dict[str, Any]] = []
            for image in sorted(journal.safe_path(target, before.relative_to(target).as_posix()).iterdir()):
                rel = image.relative_to(target).as_posix()
                data = read(target, rel)
                if data is None or data not in copies.values():
                    raise ValueError("unique or unreadable before-image")
                live_path = next(path for path, content in copies.items() if content == data)
                removals.append(
                    {"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "live_path": live_path}
                )
            if not removals:
                continue
            if not dry_run:
                # Record proof, never another copy of the redundant bytes.
                journal.annotate(target, directory.name, swept_images=removals)
                for item in removals:
                    data = read(target, item["path"])
                    live = read(target, item["live_path"])
                    if data != live or data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
                        raise ValueError("unique or unreadable before-image")
                    journal.safe_path(target, item["path"]).unlink()
                result["decisions"].append(directory.name)
                try:
                    before.rmdir()
                except OSError:
                    pass
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
