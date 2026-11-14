"""Rebuildable recovery index. Decision records remain the source of truth."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import memory_hub_store as store

INDEX_REL = ".brigade/memory-care/pending.json"


def _row(path: Path) -> dict[str, Any]:
    from . import memory_care_journal as journal

    p = journal._payload(path.parents[4], path.parent.name)
    phase = p.get("phase")
    return {
        "prepared": phase == "prepared",
        "document": phase == "prepared" and bool(p.get("document_undo")),
        "repair": phase == "abandoned" and bool(p.get("repair_indexes")),
        "append": phase in ("committed", "undone")
        and bool(p.get("appends"))
        and not p.get("appends_complete")
        and not p.get("append_refused")  # refused on purpose: recovery does not retry it
        and not p.get("undone_by"),
    }


def _paths(target: Path) -> list[Path]:
    from .memory_care_journal import JOURNAL_REL, safe_path

    root = safe_path(target, JOURNAL_REL)
    return sorted(root.glob("*/decision.json")) if root.exists() else []


def scan(target: Path) -> dict[str, dict[str, Any]]:
    return {p.parent.name: row for p in _paths(target) if any((row := _row(p)).values())}


def load(target: Path) -> dict[str, dict[str, Any]]:
    from .memory_care_journal import safe_path

    path = safe_path(target, INDEX_REL)
    cached: dict[str, Any] = {}
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            value = json.load(handle)
        records = value["records"]
        digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode("utf-8")).hexdigest()
        if (
            digest == value["sha256"]
            and isinstance(records, dict)
            and all(
                isinstance(entry, dict)
                and set(entry) == {"stamp", "row"}
                and isinstance(entry["stamp"], list)
                and len(entry["stamp"]) == 4
                and all(type(part) is int for part in entry["stamp"])
                and isinstance(entry["row"], dict)
                and set(entry["row"]) == {"prepared", "document", "repair", "append"}
                and all(type(flag) is bool for flag in entry["row"].values())
                for entry in records.values()
            )
        ):
            cached = records
    except (OSError, ValueError, KeyError, TypeError):
        pass
    records = {}
    for p in _paths(target):
        stat = p.stat()
        stamp = [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino]
        old = cached.get(p.parent.name)
        row = old["row"] if isinstance(old, dict) and old.get("stamp") == stamp else _row(p)
        records[p.parent.name] = {"stamp": stamp, "row": row}
    if records != cached or not path.exists():
        try:
            store.write_json(
                path,
                {
                    "records": records,
                    "sha256": hashlib.sha256(json.dumps(records, sort_keys=True).encode("utf-8")).hexdigest(),
                },
            )
        except OSError:
            pass  # Rebuilt rows remain usable when the optional cache cannot be replaced.
    return {key: value["row"] for key, value in records.items() if any(value["row"].values())}
