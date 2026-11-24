"""Bounded clone-local push-gate telemetry and summary statistics."""

from __future__ import annotations

import json
import math
import stat
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from ..inbox_lock import held_file_lock
from ..localio import write_text_atomic

SCHEMA = "brigade.push_gate.v1"
RECORD_LIMIT = 5000
_LINE_LIMIT = 32768


def log_path(common: Path) -> Path:
    return common / "brigade" / "push-gate.jsonl"


def _lines(path: Path) -> deque[bytes]:
    try:
        if path.parent.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise OSError("push-gate log requires a regular file in the common Git directory")
        with path.open("rb") as stream:
            # Bound even a manually edited log. The first line might be partial.
            stream.seek(max(0, stream.seek(0, 2) - RECORD_LIMIT * _LINE_LIMIT))
            if stream.tell():
                stream.readline(_LINE_LIMIT)
            return deque((line for line in stream if len(line) <= _LINE_LIMIT), maxlen=RECORD_LIMIT)
    except FileNotFoundError:
        return deque(maxlen=RECORD_LIMIT)


def append(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError("push-gate log directory is a symlink")
    with held_file_lock(path.with_suffix(".lock"), deadline_seconds=0.05):
        lines = _lines(path)
        lines.extend((json.dumps(row, separators=(",", ":")) + "\n").encode("ascii") for row in rows)
        write_text_atomic(path, b"".join(lines).decode("ascii", errors="replace"))


def read(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in _lines(path):
        try:
            row = json.loads(line)
            if (
                isinstance(row, dict)
                and row.get("schema") == SCHEMA
                and isinstance(row.get("result"), str)
                and isinstance(row.get("would_block"), bool)
                and type(row.get("duration_ms")) is int
                and row["duration_ms"] >= 0
                and datetime.fromisoformat(row["time"]).utcoffset() is not None
            ):
                rows.append(row)
        except (ValueError, KeyError, TypeError, RecursionError):
            continue
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row["result"] for row in rows)
    durations = sorted(row["duration_ms"] for row in rows)
    blocks = sum(row["would_block"] for row in rows)
    return {
        "total": len(rows),
        "results": dict(counts),
        "would_block": blocks,
        "would_block_share": blocks / len(rows) if rows else 0.0,
        "gate_errors": counts["gate_error"],
        "median_ms": median(durations) if durations else None,
        "p95_ms": durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
    }


def report(rows: list[dict[str, Any]], since: str | None) -> dict[str, Any]:
    days: dict[str, list[dict[str, Any]]] = defaultdict(list)
    filtered = []
    for row in rows:
        day = row["time"][:10]
        if since is None or day >= since:
            filtered.append(row)
            days[day].append(row)
    return {**_summary(filtered), "days": {day: _summary(items) for day, items in sorted(days.items())}}
