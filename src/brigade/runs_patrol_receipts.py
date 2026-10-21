"""Bound receipt retention while archiving compact patrol action fences."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import inbox_lock, localio, run_reap

REPORT_RECEIPT_LIMIT = 144
REPORT_RECEIPT_MAX_AGE = timedelta(days=7)
RECEIPT_BYTE_LIMIT = 4 * 1024 * 1024
FENCE_FILE = ".action-fences.json"
FENCE_STATUSES = frozenset({"recorded", "pending", "failed", "applied"})
FENCE_FIELDS = ("lane_id", "action", "scope", "idempotency_key", "attempted_at", "status")


@contextmanager
def receipt_lock(target: Path) -> Iterator[None]:
    """Serialize publication, actions and retention before creating a receipt."""
    root = target
    for part in (".brigade", "patrol"):
        root = root / part
        if root.is_symlink():
            raise ValueError("receipt path must not be symlinked")
        root.mkdir(mode=0o700, exist_ok=True)
    with inbox_lock.held_file_lock(root / ".apply.lock", deadline_seconds=1):
        yield


def read_receipt(directory: Path) -> dict[str, Any] | None:
    """Ignore unpublished directories, but fail closed on corrupt receipts."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid patrol receipt directory")
    return _read_payload(directory / "patrol.json")


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > RECEIPT_BYTE_LIMIT:
            raise ValueError("invalid patrol receipt file")
        payload = json.loads(stream.read(RECEIPT_BYTE_LIMIT + 1))
    if not isinstance(payload, dict) or payload.get("schema") != "brigade.runs_patrol.v1":
        raise ValueError("invalid patrol receipt schema")
    records = payload.get("actions")
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise ValueError("invalid patrol action history")
    return payload


def read_fences(root: Path) -> list[dict[str, Any]]:
    payload = _read_payload(root / FENCE_FILE)
    return payload["actions"] if payload is not None else []


def merge_fences(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate receipt/archive records without weakening a fence."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    priority = {"recorded": 0, "pending": 1, "failed": 2, "applied": 3}
    for action in actions:
        status = action.get("status")
        if status not in FENCE_STATUSES:
            continue
        compact = {field: action[field] for field in FENCE_FIELDS if field in action}
        key = (str(compact.get("idempotency_key")), str(compact.get("scope")))
        previous = merged.get(key)
        if previous is None or priority[status] > priority[previous["status"]]:
            merged[key] = compact
    return list(merged.values())


def prune_reports(root: Path, now: datetime, *, keep: Path, strict: bool = False) -> None:
    """Keep recent receipts, archiving action fences before deleting old ones.

    Unknown or corrupt files stay in place for inspection. Never recursively
    delete a receipt directory, which might contain evidence we do not own.
    The first sweep also archives receipts from before retention was enforced.
    """
    reports = 0
    expired: list[Path] = []
    archived = read_fences(root)
    actions = list(archived)
    for directory in sorted(root.iterdir(), key=lambda path: (path == keep, path.name), reverse=True):
        if directory.name.startswith("."):
            continue
        try:
            payload = read_receipt(directory)
        except (OSError, ValueError):
            if strict:
                raise
            continue
        if payload is None:
            continue
        observed = run_reap._parse_timestamp(payload.get("observed_at"))
        if observed is None:
            continue
        reports += 1
        if directory == keep or (reports <= REPORT_RECEIPT_LIMIT and now - observed <= REPORT_RECEIPT_MAX_AGE):
            continue
        actions.extend(payload["actions"])
        expired.append(directory)
    fences = merge_fences(actions)
    if fences != archived:
        payload = {"schema": "brigade.runs_patrol.v1", "actions": fences}
        raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if len(raw.encode()) > RECEIPT_BYTE_LIMIT:
            raise ValueError("patrol fence archive exceeds byte limit")
        localio.write_text_atomic(root / FENCE_FILE, raw)
    for directory in expired:
        (directory / "patrol.json").unlink()
        try:
            directory.rmdir()
        except OSError:
            pass  # Preserve unexpected sibling files.
