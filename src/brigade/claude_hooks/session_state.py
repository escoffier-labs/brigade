"""Session-state synchronization and timeout-latch helpers for Claude hooks."""

from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .. import localio
from ..work_cmd import inbox_lock
from . import envelope

_SESSION_STATE_PROCESS_LOCKS_GUARD = threading.Lock()
_SESSION_STATE_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _rt() -> Any:
    """Resolve runtime lazily so its state primitives remain monkeypatchable."""
    from . import runtime

    return runtime


def _reset_session_state_locks() -> None:
    """Discard inherited thread locks after a POSIX fork."""
    global _SESSION_STATE_PROCESS_LOCKS_GUARD, _SESSION_STATE_PROCESS_LOCKS
    _SESSION_STATE_PROCESS_LOCKS_GUARD = threading.Lock()
    _SESSION_STATE_PROCESS_LOCKS = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_session_state_locks)


def _session_state_process_lock(state_path: Path) -> threading.Lock:
    """Return this process's serializer for one session-state lock path."""
    key = str(state_path)
    with _SESSION_STATE_PROCESS_LOCKS_GUARD:
        lock = _SESSION_STATE_PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SESSION_STATE_PROCESS_LOCKS[key] = lock
        return lock


def _timeouts_path(state_path: Path) -> Path:
    """Return the append-only timeout journal beside one session state file."""
    return Path(f"{state_path}.timeouts")


def _journal_path(target: Path, session_id: str) -> Path:
    state_path = _rt()._state_path(target.expanduser().resolve(), session_id)
    return _timeouts_path(state_path)


def _journal_timeout_count(target: Path, session_id: str) -> int:
    """Return the number of non-empty timeout markers, or zero on read failure."""
    path = _journal_path(target, session_id)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            chunks = []
            while chunk := os.read(fd, 65_536):
                chunks.append(chunk)
        finally:
            os.close(fd)
    except OSError:
        return 0
    return sum(1 for line in b"".join(chunks).splitlines() if line.strip())


def _append_timeout_marker(target: Path, session_id: str) -> int:
    """Append one timeout marker without taking the session-state lock."""
    path = _journal_path(target, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, f"{localio.utc_now_iso()}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        return 0
    return _journal_timeout_count(target, session_id)


def _truncate_timeout_journal(target: Path, session_id: str) -> None:
    """Discard recorded markers after their count has been folded into state."""
    path = _journal_path(target, session_id)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
        os.close(fd)
    except OSError:
        return


@contextlib.contextmanager
def _session_state_lock(target: Path, session_id: str) -> Iterator[None]:
    """Serialize one session state's read-modify-write window.

    ``held_file_lock`` creates the state directory as needed, matching the
    parent creation performed by ``write_session_state`` through ``localio``.
    Both the process-local lock and the file lock share one budget
    (``_TIMEOUT_FOLLOWUP_SECONDS``); running out raises ``InboxLockTimeout``
    and the caller writes nothing.
    """
    state_path = _rt()._state_path(target.expanduser().resolve(), session_id)
    process_lock = _session_state_process_lock(state_path)
    budget = float(_rt()._TIMEOUT_FOLLOWUP_SECONDS)
    deadline = time.monotonic() + budget
    # One monotonic budget covers both stages so a stalled follow-up holding
    # the process lock cannot park later follow-ups past the file-lock deadline.
    acquired = False
    try:
        acquired = process_lock.acquire(timeout=max(budget, 0.0))
        if not acquired:
            raise inbox_lock.InboxLockTimeout(f"session state lock busy past {budget:.3f}s: {state_path}")
        remaining = max(deadline - time.monotonic(), 0.0)
        with inbox_lock.held_file_lock(Path(f"{state_path}.lock"), deadline_seconds=remaining):
            yield
    finally:
        if acquired:
            process_lock.release()


def _read_hook_latch(target: Path, session_id: str) -> tuple[bool, bool]:
    state = _rt().read_session_state(target, session_id)
    state_latched = isinstance(state, dict) and state.get("hook_latched") is True
    journal_latched = _journal_timeout_count(target, session_id) >= _rt().HOOK_TIMEOUT_LATCH_AFTER
    if not state_latched and not journal_latched:
        return False, False
    return True, not isinstance(state, dict) or state.get("hook_latch_announced") is not True


def _write_session_state_preserving_latch(
    target: Path,
    session_id: str,
    state: dict[str, Any],
    *,
    log_target: Path | None = None,
) -> bool:
    """Write state without allowing an older payload to clear the timeout latch."""
    try:
        with _rt()._session_state_lock(target, session_id):
            current = _rt().read_session_state(target, session_id)
            current_state = current if isinstance(current, dict) else {}
            updated = dict(state)
            current_count = current_state.get("hook_timeout_count")
            next_count = updated.get("hook_timeout_count")
            counts = [
                count for count in (current_count, next_count) if isinstance(count, int) and not isinstance(count, bool)
            ]
            if counts:
                updated["hook_timeout_count"] = max(counts)
            else:
                updated.pop("hook_timeout_count", None)
            updated["hook_latched"] = current_state.get("hook_latched") is True or updated.get("hook_latched") is True
            updated["hook_latch_announced"] = (
                current_state.get("hook_latch_announced") is True or updated.get("hook_latch_announced") is True
            )
            _rt().write_session_state(target, session_id, updated)
            return True
    except inbox_lock.InboxLockTimeout:
        if log_target is not None:
            envelope.append_log(log_target, f"session state write skipped after lock timeout: {session_id}")
        return False


def _mark_hook_latch_announced(target: Path, session_id: str) -> None:
    try:
        with _rt()._session_state_lock(target, session_id):
            state = _rt().read_session_state(target, session_id)
            if not isinstance(state, dict) or state.get("hook_latch_announced") is True:
                return
            updated = dict(state)
            updated["hook_latched"] = True
            updated["hook_latch_announced"] = True
            _rt().write_session_state(target, session_id, updated)
    except inbox_lock.InboxLockTimeout:
        return


def _record_hook_timeout(target: Path, session_id: str) -> dict[str, Any]:
    state = _rt().read_session_state(target, session_id)
    fallback: dict[str, Any] = dict(state) if isinstance(state, dict) else {"session_id": session_id}
    try:
        with _rt()._session_state_lock(target, session_id):
            state = _rt().read_session_state(target, session_id)
            updated: dict[str, Any] = dict(state) if isinstance(state, dict) else {"session_id": session_id}
            count = updated.get("hook_timeout_count")
            current_count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
            next_count = current_count + 1 + _journal_timeout_count(target, session_id)
            updated["hook_timeout_count"] = next_count
            if updated.get("hook_latched") is True or next_count >= _rt().HOOK_TIMEOUT_LATCH_AFTER:
                updated["hook_latched"] = True
            _rt().write_session_state(target, session_id, updated)
            _truncate_timeout_journal(target, session_id)
            return updated
    except inbox_lock.InboxLockTimeout:
        journal_count = _append_timeout_marker(target, session_id)
        count = fallback.get("hook_timeout_count")
        fallback_count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
        next_count = max(fallback_count, journal_count)
        fallback["hook_timeout_count"] = next_count
        if fallback.get("hook_latched") is True or next_count >= _rt().HOOK_TIMEOUT_LATCH_AFTER:
            fallback["hook_latched"] = True
        return fallback


def _clear_hook_timeouts(target: Path, session_id: str) -> None:
    try:
        with _rt()._session_state_lock(target, session_id):
            state = _rt().read_session_state(target, session_id)
            if (
                isinstance(state, dict)
                and state.get("hook_latched") is True
                or _journal_timeout_count(target, session_id) >= _rt().HOOK_TIMEOUT_LATCH_AFTER
            ):
                return
            if isinstance(state, dict) and state.get("hook_timeout_count"):
                updated = dict(state)
                updated["hook_timeout_count"] = 0
                _rt().write_session_state(target, session_id, updated)
            _truncate_timeout_journal(target, session_id)
    except inbox_lock.InboxLockTimeout:
        return
