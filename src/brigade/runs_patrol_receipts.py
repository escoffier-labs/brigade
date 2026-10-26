"""Bound receipt retention while archiving compact patrol action fences."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import inbox_lock, localio, run_reap

REPORT_RECEIPT_LIMIT = 144
REPORT_RECEIPT_MAX_AGE = timedelta(days=7)
RECEIPT_BYTE_LIMIT = 4 * 1024 * 1024
FENCE_FILE = ".action-fences.json"
FENCE_DIRECTORY = ".action-fences"
FENCE_STATUSES = frozenset({"recorded", "pending", "failed", "observed", "applied"})
FENCE_FIELDS = ("lane_id", "action", "scope", "idempotency_key", "attempted_at", "status")


@contextmanager
def receipt_lock(target: Path, *, report_only: bool = False) -> Iterator[bool]:
    """Serialize actions and retention. Reports may publish while actions are busy.

    A False result permits only atomic publication of a unique report receipt.
    Retention waits for a later patrol that can acquire the action lock.
    """
    root = target
    for part in (".brigade", "patrol"):
        root = root / part
        if root.is_symlink():
            raise ValueError("receipt path must not be symlinked")
        root.mkdir(mode=0o700, exist_ok=True)
    with ExitStack() as locks:
        try:
            locks.enter_context(inbox_lock.held_file_lock(root / ".apply.lock", deadline_seconds=1))
        except inbox_lock.InboxLockTimeout:
            if not report_only:
                raise
            yield False
        else:
            yield True


_LANE_SUMMARY = ("lane_id", "run_id", "task_id", "claim_id", "state", "reason", "classification_digest")


def bounded_receipt_text(payload: dict[str, Any]) -> str:
    """Serialize a receipt that the strict reader is guaranteed to accept.

    A scan can hold up to 1000 lanes with long evidence lists. Evidence, then
    lane detail, then trailing lanes are dropped until the receipt fits. The
    action history is never trimmed because retention and fences read it.
    """

    def render(value: dict[str, Any]) -> str:
        return json.dumps(value, indent=2, sort_keys=True) + "\n"

    text = render(payload)
    if len(text.encode()) <= RECEIPT_BYTE_LIMIT:
        return text
    lanes = [dict(lane, evidence=list(lane.get("evidence", ())[:3])) for lane in payload.get("lanes", ())]
    bounded = {**payload, "lanes": lanes, "receipt_truncated": True}
    text = render(bounded)
    if len(text.encode()) <= RECEIPT_BYTE_LIMIT:
        return text
    lanes = [{key: lane[key] for key in _LANE_SUMMARY if key in lane} for lane in lanes]
    bounded["lanes"] = lanes
    text = render(bounded)
    while len(text.encode()) > RECEIPT_BYTE_LIMIT and lanes:
        del lanes[len(lanes) // 2 :]
        text = render(bounded)
    if len(text.encode()) > RECEIPT_BYTE_LIMIT:
        # The action history alone is too large. Refuse to publish a receipt
        # the strict reader would reject and abort every later apply patrol.
        raise ValueError("patrol action history exceeds receipt byte limit")
    return text


def write_receipt(path: Path, payload: dict[str, Any]) -> None:
    localio.write_text_atomic(path, bounded_receipt_text(payload))


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
    for record in records:
        status = record.get("status")
        if not isinstance(status, str) or status not in FENCE_STATUSES | {"refused", "skipped"}:
            raise ValueError("invalid patrol action history")
        # Refused/skipped outcomes do not fence later attempts. Legacy receipts
        # may contain only their status; fence-bearing records need both keys.
        if status in FENCE_STATUSES and any(
            not isinstance(record.get(field), str) or not record[field].strip()
            for field in ("scope", "idempotency_key")
        ):
            raise ValueError("invalid patrol action history")
    return payload


def _fence_root(root: Path) -> Path:
    directory = root / FENCE_DIRECTORY
    if directory.is_symlink():
        raise ValueError("patrol fence directory must not be symlinked")
    return directory


def _fence_path(directory: Path, lane_id: str) -> Path:
    return directory / (hashlib.sha256(lane_id.encode()).hexdigest() + ".json")


def read_fences(root: Path, *, lane_ids: set[str] | None = None) -> list[dict[str, Any]]:
    # The legacy archive remains readable until a locked retention pass migrates it.
    payload = _read_payload(root / FENCE_FILE)
    actions = payload["actions"] if payload is not None else []
    directory = _fence_root(root)
    if directory.exists():
        paths = directory.iterdir() if lane_ids is None else (_fence_path(directory, lane) for lane in lane_ids)
        for path in paths:
            payload = _read_payload(path)
            if payload is not None:
                actions.extend(payload["actions"])
    return actions


def _write_fences(path: Path, actions: list[dict[str, Any]]) -> None:
    raw = json.dumps({"schema": "brigade.runs_patrol.v1", "actions": actions}, indent=2, sort_keys=True) + "\n"
    if len(raw.encode()) > RECEIPT_BYTE_LIMIT:
        raise ValueError("patrol fence archive exceeds byte limit")
    localio.write_text_atomic(path, raw)


def _archive_fences(root: Path, actions: list[dict[str, Any]], legacy: list[dict[str, Any]]) -> None:
    """Keep each lane's lifetime fences without a shared archive size ceiling."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        lane_id = action.get("lane_id")
        groups.setdefault(lane_id if isinstance(lane_id, str) else "", []).append(action)
    directory = _fence_root(root)
    for lane_id, records in groups.items():
        if not lane_id:
            continue  # Preserve unidentified legacy records in the original archive.
        directory.mkdir(mode=0o700, exist_ok=True)
        path = _fence_path(directory, lane_id)
        payload = _read_payload(path)
        previous = payload["actions"] if payload is not None else []
        combined = merge_fences([*previous, *records])
        if combined != previous:
            _write_fences(path, combined)
    remaining = groups.get("", [])
    if remaining != legacy:
        # Publish all migrated shards before forgetting the legacy copies.
        _write_fences(root / FENCE_FILE, remaining)


def merge_fences(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate receipt/archive records without weakening a fence."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    priority = {"recorded": 0, "pending": 1, "failed": 2, "observed": 3, "applied": 4}
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
    legacy = _read_payload(root / FENCE_FILE)
    archived = legacy["actions"] if legacy is not None else []
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
    if fences or archived:
        _archive_fences(root, fences, archived)
    for directory in expired:
        (directory / "patrol.json").unlink()
        try:
            directory.rmdir()
        except OSError:
            pass  # Preserve unexpected sibling files.
