"""Worktree-local coordination and retry state for automatic receipt imports."""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Any

from . import localio
from .inbox_lock import held_file_lock

OUTDATED = "outdated_evidence_engine"
_BACKOFF_SECONDS = (15, 30, 60, 120)

RECEIPT_SOURCES = (
    ("work_verify", ".brigade/work/verify-runs", "*/receipt.json"),
    ("run", ".brigade/runs", "*/run.json"),
)


def _state_path(target: Path) -> Path:
    return target / ".brigade" / "work" / "evidence-import-state.json"


def replay_lock(target: Path, *, deadline_seconds: float = 0) -> AbstractContextManager[None]:
    return held_file_lock(_state_path(target).with_suffix(".lock"), deadline_seconds=deadline_seconds)


@contextmanager
def explicit_retry_locks(targets: Iterable[Path]) -> Iterator[None]:
    """Allow explicit retries even when paused, serializing with active imports."""
    with ExitStack() as stack:
        for target in sorted({target.resolve() for target in targets}):
            stack.enter_context(replay_lock(target, deadline_seconds=125))
        yield


def engine_identity(binary: str | None) -> dict[str, Any] | None:
    if binary is None:
        return None
    try:
        path = Path(binary).resolve(strict=True)
        stat = path.stat()
    except (OSError, RuntimeError):
        return None
    # A managed state's digest describes the installed asset, so it cannot
    # identify the actual executable after a manual replacement.
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "inode": stat.st_ino,
        "device": stat.st_dev,
    }


def read_state(target: Path, engine: dict[str, Any] | None) -> dict[str, Any]:
    if engine is None:
        return {}
    try:
        state = localio.read_json_dict(_state_path(target)) or {}
    except (RecursionError, ValueError):
        return {}
    if state.get("engine") != engine:
        return {}
    if any(
        type(state.get(key)) is not int or state[key] < 0 for key in ("failed_at", "pending_count", "candidate_count")
    ):
        return {}
    if not 0 < state["pending_count"] <= state["candidate_count"]:
        return {}
    if state.get("kind") == OUTDATED:
        return state
    if state.get("kind") == "timeout":
        step = state.get("timeout_step")
        if type(step) is int and 0 <= step < len(_BACKOFF_SECONDS):
            retry_after = state.get("retry_after")
            if type(retry_after) is int and retry_after == state["failed_at"] + _BACKOFF_SECONDS[step]:
                return state
    return {}


def skipped_index(target: Path, state: dict[str, Any]) -> dict[str, Any]:
    kind = state.get("kind")
    if kind == OUTDATED:
        from .component_install import engine_repair_hint

        # Count receipt paths only. The snapshot already excludes known hashes
        # and malformed receipts. Subsequent path changes are an estimate.
        count = sum(1 for _, root, pattern in RECEIPT_SOURCES for _ in (target / root).glob(pattern))
        pending = max(0, state["pending_count"] + count - state["candidate_count"])
        repair = engine_repair_hint()
        return {
            "status": "paused",
            "reason": kind,
            "pending_count": pending,
            "repair": repair,
            "message": f"Evidence indexing paused ({pending} receipt(s) pending). {repair}",
        }
    if kind == "timeout" and int(time.time()) < state["retry_after"]:
        return {
            "status": "backoff",
            "reason": kind,
            "pending_count": state["pending_count"],
            "retry_after": state["retry_after"],
            "message": "Evidence indexing cooling down after an import timeout. Receipts are pending.",
        }
    return {}


def record_failure(
    target: Path,
    state: dict[str, Any],
    engine: dict[str, Any] | None,
    *,
    kind: str,
    pending_count: int,
    candidate_count: int,
) -> None:
    if engine is None:
        return
    failed_at = int(time.time())
    marker: dict[str, Any] = {
        "engine": engine,
        "kind": kind,
        "failed_at": failed_at,
        "pending_count": pending_count,
        "candidate_count": candidate_count,
    }
    if kind == "timeout":
        step = min(state.get("timeout_step", -1) + 1, len(_BACKOFF_SECONDS) - 1)
        marker.update(timeout_step=step, retry_after=failed_at + _BACKOFF_SECONDS[step])
    try:
        localio.write_json(_state_path(target), marker)
    except OSError as exc:
        print(f"warning: could not save evidence import state: {exc}", file=sys.stderr)


def clear_state(target: Path) -> None:
    try:
        _state_path(target).unlink(missing_ok=True)
    except OSError as exc:
        print(f"warning: could not clear evidence import state: {exc}", file=sys.stderr)
