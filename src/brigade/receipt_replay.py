"""Worktree-local coordination and retry state for automatic receipt imports."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Any

from . import localio
from .inbox_lock import held_file_lock

OUTDATED = "outdated_evidence_engine"
AUTO_BATCH_SIZE = 32
AUTO_IMPORT_TIMEOUT = 5
_MANUAL_RETRY = "To retry now, run `brigade receipts export evidence --new-only --import`."
_BACKOFF_SECONDS = (15, 30, 60, 120)
_TIMEOUT_STREAK_SECONDS = 600
_CHECK_ENGINE_DIGEST = sys.platform == "win32"
_DIGEST_INTERVAL_SECONDS = 180

RECEIPT_SOURCES = (
    ("work_verify", ".brigade/work/verify-runs", "*/receipt.json"),
    ("run", ".brigade/runs", "*/run.json"),
)


def _state_path(target: Path) -> Path:
    return target / ".brigade" / "work" / "evidence-import-state.json"


def replay_lock(target: Path, *, deadline_seconds: float = 0) -> AbstractContextManager[None]:
    return held_file_lock(_state_path(target).with_suffix(".lock"), deadline_seconds=deadline_seconds)


@contextmanager
def explicit_retry_locks(targets: Iterable[Path], *, enabled: bool) -> Iterator[bool]:
    """Acquire all worktree locks before the receipt and cursor snapshots."""
    with ExitStack() as stack:
        try:
            if enabled:
                for target in sorted({target.resolve() for target in targets}):
                    stack.enter_context(replay_lock(target, deadline_seconds=125))
        except OSError as exc:
            print(f"error: could not lock evidence import: {exc}", file=sys.stderr)
            yield False
        else:
            yield True


def engine_identity(binary: str | None) -> dict[str, Any] | None:
    if binary is None:
        return None
    try:
        path = Path(binary).resolve(strict=True)
        stat = path.stat()
    except (OSError, RuntimeError):
        return None
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "inode": stat.st_ino,
        "device": stat.st_dev,
    }


def read_state(target: Path) -> dict[str, Any]:
    try:
        state = localio.read_json_dict(_state_path(target)) or {}
    except (RecursionError, ValueError):
        return {}
    engine = state.get("engine")
    if not isinstance(engine, dict) or not isinstance(engine.get("path"), str):
        return {}
    if any(type(engine.get(key)) is not int for key in ("size", "mtime_ns", "ctime_ns", "inode", "device")):
        return {}
    if any(
        type(state.get(key)) is not int or state[key] < 0 for key in ("failed_at", "pending_count", "candidate_count")
    ):
        return {}
    if not 0 < state["pending_count"] <= state["candidate_count"]:
        return {}
    size = state.get("batch_size", AUTO_BATCH_SIZE)
    if type(size) is not int or not 1 <= size <= AUTO_BATCH_SIZE:
        return {}
    if state.get("kind") in (OUTDATED, "drain"):
        return state
    if state.get("kind") == "timeout":
        step = state.get("timeout_step")
        if type(step) is int and 0 <= step < len(_BACKOFF_SECONDS):
            retry_after = state.get("retry_after")
            if type(retry_after) is int and retry_after == state["failed_at"] + _BACKOFF_SECONDS[step]:
                return state
    return {}


def state_for_engine(target: Path, engine: dict[str, Any] | None) -> dict[str, Any]:
    state = read_state(target)
    if not state or engine is None:
        return state
    if state["engine"] != engine or _engine_digest_changed(target, state):
        clear_state(target)
        return {}
    return state


def _engine_digest_changed(target: Path, state: dict[str, Any]) -> bool:
    if not _CHECK_ENGINE_DIGEST or state["kind"] != OUTDATED:
        return False
    now = int(time.time())
    checked_at = state.get("digest_checked_at")
    if type(checked_at) is int and 0 <= now - checked_at < _DIGEST_INTERVAL_SECONDS:
        return False
    previous = state.get("engine_sha256")
    _refresh_engine_digest(state, now)
    digest = state.get("engine_sha256")
    if previous is not None and digest != previous:
        return True
    _write_state(target, state)
    return False


def _refresh_engine_digest(state: dict[str, Any], now: int) -> None:
    state["digest_checked_at"] = now
    try:
        state["engine_sha256"] = localio.file_sha256(Path(state["engine"]["path"]))
    except OSError:
        # Keep a previously observed digest across unreadable-file errors.
        pass


def skipped_index(target: Path, state: dict[str, Any]) -> dict[str, Any]:
    kind = state.get("kind")
    cooling = (
        kind == "timeout" and 0 < state["retry_after"] - int(time.time()) <= _BACKOFF_SECONDS[state["timeout_step"]]
    )
    if kind != OUTDATED and not cooling:
        return {}

    from .component_install import engine_repair_hint

    repair = engine_repair_hint()
    if kind == OUTDATED:
        # The original snapshot excludes known and malformed receipts.
        count = sum(1 for _, root, pattern in RECEIPT_SOURCES for _ in (target / root).glob(pattern))
        pending = max(0, state["pending_count"] + count - state["candidate_count"])
        return {
            "status": "paused",
            "reason": kind,
            "pending_count": pending,
            "repair": repair,
            "message": f"Evidence indexing paused ({pending} receipt(s) pending). {repair} {_MANUAL_RETRY}",
        }
    return {
        "status": "backoff",
        "reason": kind,
        "pending_count": state["pending_count"],
        "retry_after": state["retry_after"],
        "repair": repair,
        "message": f"Evidence indexing cooling down after an import timeout. {repair} {_MANUAL_RETRY}",
    }


def oldest_pending(
    receipts: list[dict[str, Any]],
    cursor_hashes: set[str],
    receipt_hash: Callable[[dict[str, Any], Path], tuple[str, str]],
    batch_size: int,
) -> tuple[list[dict[str, Any]], int]:
    """Select a bounded batch before building adapter envelopes."""
    batch: list[dict[str, Any]] = []
    pending_count = 0
    for receipt in reversed(receipts):
        digest, _ = receipt_hash(receipt["payload"], receipt["path"])
        if digest not in cursor_hashes:
            pending_count += 1
            if len(batch) < batch_size:
                batch.append(receipt)
    return batch, pending_count


def record_replay(
    target: Path,
    state: dict[str, Any],
    engine: dict[str, Any] | None,
    result: dict[str, Any],
    *,
    kind: str,
) -> None:
    if engine is None:
        return
    now = int(time.time())
    batch_size = result["exported_count"]
    marker: dict[str, Any] = {
        "engine": engine,
        "kind": kind,
        "failed_at": now,
        "pending_count": result["pending_count"],
        "candidate_count": result["candidate_count"],
        "batch_size": batch_size,
    }
    if kind == "timeout":
        recent = state.get("kind") == "timeout" and 0 <= now - state["failed_at"] <= _TIMEOUT_STREAK_SECONDS
        step = min(state["timeout_step"] + 1, len(_BACKOFF_SECONDS) - 1) if recent else 0
        marker.update(timeout_step=step, retry_after=now + _BACKOFF_SECONDS[step], batch_size=max(1, batch_size // 2))
    elif kind == OUTDATED and _CHECK_ENGINE_DIGEST:
        _refresh_engine_digest(marker, now)
    _write_state(target, marker)


def finish_batch(target: Path, state: dict[str, Any], engine: dict[str, Any] | None, result: dict[str, Any]) -> int:
    remaining = result["pending_count"] - result["exported_count"]
    if remaining and state.get("batch_size", AUTO_BATCH_SIZE) < AUTO_BATCH_SIZE:
        record_replay(target, state, engine, result | {"pending_count": remaining}, kind="drain")
    else:
        clear_state(target)
    return remaining


def _write_state(target: Path, marker: dict[str, Any]) -> None:
    try:
        localio.write_json(_state_path(target), marker)
    except OSError as exc:
        print(f"warning: could not save evidence import state: {exc}", file=sys.stderr)


def clear_state(target: Path) -> bool:
    try:
        _state_path(target).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"warning: could not clear evidence import state: {exc}", file=sys.stderr)
        return False
    return True
