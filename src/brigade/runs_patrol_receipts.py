"""Bound report retention without discarding patrol action fences."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import inbox_lock, run_reap

REPORT_RECEIPT_LIMIT = 144
REPORT_RECEIPT_MAX_AGE = timedelta(days=7)
RECEIPT_BYTE_LIMIT = 4 * 1024 * 1024


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
    path = directory / "patrol.json"
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


def prune_reports(root: Path, now: datetime) -> None:
    """Under the receipt lock, keep only recent reports and every action fence.

    Unknown or corrupt files stay in place for inspection. Never recursively
    delete a receipt directory, which might contain evidence we do not own.
    """
    reports = 0
    for directory in sorted(root.iterdir(), reverse=True):
        if directory.name.startswith("."):
            continue
        try:
            payload = read_receipt(directory)
        except (OSError, ValueError):
            continue
        if payload is None or payload["actions"]:
            continue
        observed = run_reap._parse_timestamp(payload.get("observed_at"))
        if observed is None:
            continue
        reports += 1
        if reports <= REPORT_RECEIPT_LIMIT and now - observed <= REPORT_RECEIPT_MAX_AGE:
            continue
        (directory / "patrol.json").unlink()
        try:
            directory.rmdir()
        except OSError:
            pass  # Preserve unexpected sibling files.
