"""Session-state synchronization and timeout-latch helpers for Claude hooks."""

from __future__ import annotations

import contextlib
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .. import localio
from ..work_cmd import inbox_lock
from . import envelope

_SESSION_STATE_PROCESS_LOCKS_GUARD = threading.Lock()
_SESSION_STATE_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_TIMEOUT_JOURNAL_MAX_LINES = 16
_TIMEOUT_JOURNAL_MAX_BYTES = 4096
_TIMEOUT_LOCK_RETRIES = 3
_ANNOUNCE_TOKEN = b"announced"
_ANNOUNCE_CLAIMED_KEY = "hook_announce_claimed"


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


def _journal_path_is_safe_without_nofollow(path: Path) -> bool:
    """Reject links before opening when the platform lacks ``O_NOFOLLOW``."""
    if getattr(os, "O_NOFOLLOW", 0):
        return True
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_reparse_tag", 0)


def _journal_fd_is_safe(fd: int, path: Path) -> bool:
    """Accept only the single-link regular file currently named by ``path``."""
    try:
        path_info = os.lstat(path)
        fd_info = os.fstat(fd)
    except OSError:
        return False
    return (
        stat.S_ISREG(fd_info.st_mode)
        and fd_info.st_nlink == 1
        and not stat.S_ISLNK(path_info.st_mode)
        and not getattr(path_info, "st_reparse_tag", 0)
        and (path_info.st_ino, path_info.st_dev) == (fd_info.st_ino, fd_info.st_dev)
    )


def _parse_timeout_journal(data: bytes) -> tuple[int, bool, str | None]:
    """Return marker count, whether announce was claimed, and the first claim id.

    Timeout-marker lines count toward the latch. Standalone ``announced``
    lines claim the one-shot notice and do not count. Older timestamp-only
    markers still count. A legacy ``{iso} announced {id}`` line both counts
    and claims.
    """
    count = 0
    announced = False
    first_claim: str | None = None
    for raw in data.splitlines():
        if not raw.strip():
            continue
        parts = raw.split()
        if parts[0] == _ANNOUNCE_TOKEN:
            announced = True
            if first_claim is None:
                first_claim = parts[1].decode("ascii", "replace") if len(parts) >= 2 else ""
            continue
        count += 1
        if len(parts) >= 2 and parts[1] == _ANNOUNCE_TOKEN:
            announced = True
            if first_claim is None:
                first_claim = parts[2].decode("ascii", "replace") if len(parts) >= 3 else ""
    return count, announced, first_claim


def _read_timeout_journal(target: Path, session_id: str) -> tuple[int, bool, str | None]:
    """Read the timeout journal, or ``(0, False, None)`` on refusal or failure."""
    path = _journal_path(target, session_id)
    try:
        if not _journal_path_is_safe_without_nofollow(path):
            return 0, False, None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            if not _journal_fd_is_safe(fd, path):
                return 0, False, None
            data = os.read(fd, _TIMEOUT_JOURNAL_MAX_BYTES)
        finally:
            os.close(fd)
    except OSError:
        return 0, False, None
    return _parse_timeout_journal(data)


def _journal_timeout_count(target: Path, session_id: str) -> int:
    """Return the number of non-empty timeout markers, or zero on read failure."""
    return _read_timeout_journal(target, session_id)[0]


def _append_journal_line(target: Path, session_id: str, payload: bytes) -> bool:
    """Append one journal line with the existing path/byte hardening."""
    path = _journal_path(target, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _journal_path_is_safe_without_nofollow(path):
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags, 0o600)
        try:
            if not _journal_fd_is_safe(fd, path):
                return False
            if os.fstat(fd).st_size >= _TIMEOUT_JOURNAL_MAX_BYTES:
                return False
            os.write(fd, payload)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


def _append_timeout_journal(target: Path, session_id: str) -> tuple[int, bool]:
    """Append one timeout occurrence without taking the session-state lock."""
    journal_lines, _already_announced, _first = _read_timeout_journal(target, session_id)
    if journal_lines >= _TIMEOUT_JOURNAL_MAX_LINES:
        return journal_lines, False
    if not _append_journal_line(target, session_id, f"{localio.utc_now_iso()}\n".encode()):
        return journal_lines, False
    return _read_timeout_journal(target, session_id)[0], True


def _append_timeout_marker(target: Path, session_id: str) -> int:
    """Append one timeout marker without taking the session-state lock."""
    return _append_timeout_journal(target, session_id)[0]


def _persisted_announce_claimed(state: object) -> bool:
    return isinstance(state, dict) and state.get("hook_latch_announced") is True


def _claim_timeout_announce(target: Path, session_id: str) -> bool:
    """Elect the first announcement claim by appending a non-counting journal line.

    Persisted ``hook_latch_announced`` from older state is already claimed.
    Concurrent callers re-read after append; only the first claim id wins.
    """
    _lines, already_announced, _first = _read_timeout_journal(target, session_id)
    if already_announced or _persisted_announce_claimed(_rt().read_session_state(target, session_id)):
        return False
    claim_id = f"{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}"
    if not _append_journal_line(target, session_id, f"announced {claim_id}\n".encode()):
        return False
    _count, _announced, first_claim = _read_timeout_journal(target, session_id)
    return first_claim == claim_id


def _timeout_state_count(state: dict[str, Any]) -> int:
    count = state.get("hook_timeout_count")
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0


def _timeout_journal_folded(state: dict[str, Any]) -> int:
    folded = state.get("hook_timeout_journal_folded")
    return folded if isinstance(folded, int) and not isinstance(folded, bool) and folded >= 0 else 0


def _effective_timeout_count(state: dict[str, Any], journal_lines: int) -> int:
    """Return state count plus journal markers not yet folded into it."""
    return _timeout_state_count(state) + max(journal_lines - _timeout_journal_folded(state), 0)


@contextlib.contextmanager
def _session_state_lock(target: Path, session_id: str) -> Iterator[None]:
    """Serialize one session state's read-modify-write window.

    ``held_file_lock`` creates the state directory as needed, matching the
    parent creation performed by ``write_session_state`` through ``localio``.
    Both the process-local lock and the file lock share one budget
    (``_SESSION_STATE_LOCK_SECONDS``), which is shorter than the outer
    ``_TIMEOUT_FOLLOWUP_SECONDS`` parent wait. Running out raises
    ``InboxLockTimeout`` and the caller writes nothing.
    """
    state_path = _rt()._state_path(target.expanduser().resolve(), session_id)
    process_lock = _session_state_process_lock(state_path)
    budget = float(_rt()._SESSION_STATE_LOCK_SECONDS)
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
    journal_lines, journal_announced, _claim = _read_timeout_journal(target, session_id)
    state = _rt().read_session_state(target, session_id)
    current_state = state if isinstance(state, dict) else {}
    latched = (
        current_state.get("hook_latched") is True
        or _effective_timeout_count(current_state, journal_lines) >= _rt().HOOK_TIMEOUT_LATCH_AFTER
    )
    if not latched:
        return False, False
    return True, not (_persisted_announce_claimed(state) or journal_announced)


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
            current_folded = current_state.get("hook_timeout_journal_folded")
            next_folded = updated.get("hook_timeout_journal_folded")
            folded_counts = [
                folded
                for folded in (current_folded, next_folded)
                if isinstance(folded, int) and not isinstance(folded, bool)
            ]
            if folded_counts:
                updated["hook_timeout_journal_folded"] = max(folded_counts)
            else:
                updated.pop("hook_timeout_journal_folded", None)
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


def _mark_hook_latch_announced(target: Path, session_id: str) -> bool:
    """Compatibility wrapper: new notices claim announce only in the journal."""
    return _claim_timeout_announce(target, session_id)


def _apply_timeout_latch(state: dict[str, Any], next_count: int) -> None:
    if state.get("hook_latched") is True or next_count >= _rt().HOOK_TIMEOUT_LATCH_AFTER:
        state["hook_latched"] = True


def _with_announce_claim(state: dict[str, Any], claimed: bool) -> dict[str, Any]:
    """Return a caller-local copy; the claim key is never written to disk."""
    returned = dict(state)
    returned[_ANNOUNCE_CLAIMED_KEY] = claimed
    return returned


def _record_hook_timeout(target: Path, session_id: str, *, announce: bool = False) -> dict[str, Any]:
    state = _rt().read_session_state(target, session_id)
    fallback: dict[str, Any] = dict(state) if isinstance(state, dict) else {"session_id": session_id}
    last_timeout: inbox_lock.InboxLockTimeout | None = None
    for _attempt in range(_TIMEOUT_LOCK_RETRIES + 1):
        try:
            with _rt()._session_state_lock(target, session_id):
                journal_lines = _journal_timeout_count(target, session_id)
                state = _rt().read_session_state(target, session_id)
                updated: dict[str, Any] = dict(state) if isinstance(state, dict) else {"session_id": session_id}
                next_count = _effective_timeout_count(updated, journal_lines) + 1
                updated["hook_timeout_count"] = next_count
                updated["hook_timeout_journal_folded"] = journal_lines
                already_announced = _persisted_announce_claimed(updated)
                _apply_timeout_latch(updated, next_count)
                _rt().write_session_state(target, session_id, updated)
            claimed = False
            if announce and updated.get("hook_latched") is True and not already_announced:
                claimed = _claim_timeout_announce(target, session_id)
            return _with_announce_claim(updated, claimed)
        except inbox_lock.InboxLockTimeout as exc:
            last_timeout = exc
            _, persisted = _append_timeout_journal(target, session_id)
            if not persisted:
                continue
            fresh = _rt().read_session_state(target, session_id)
            current: dict[str, Any] = dict(fresh) if isinstance(fresh, dict) else dict(fallback)
            journal_lines, journal_announced, _claim = _read_timeout_journal(target, session_id)
            next_count = _effective_timeout_count(current, journal_lines)
            current["hook_timeout_count"] = next_count
            _apply_timeout_latch(current, next_count)
            already = journal_announced or _persisted_announce_claimed(current)
            claimed = False
            if announce and current.get("hook_latched") is True and not already:
                claimed = _claim_timeout_announce(target, session_id)
            return _with_announce_claim(current, claimed)
    if last_timeout is None:
        raise RuntimeError("timeout retry loop exited without InboxLockTimeout")
    raise last_timeout


def _clear_hook_timeouts(target: Path, session_id: str) -> None:
    try:
        with _rt()._session_state_lock(target, session_id):
            journal_lines = _journal_timeout_count(target, session_id)
            state = _rt().read_session_state(target, session_id)
            current_state = state if isinstance(state, dict) else {}
            if (
                current_state.get("hook_latched") is True
                or _effective_timeout_count(current_state, journal_lines) >= _rt().HOOK_TIMEOUT_LATCH_AFTER
            ):
                return
            updated = dict(current_state) if current_state else {"session_id": session_id}
            updated["hook_timeout_count"] = 0
            updated["hook_timeout_journal_folded"] = journal_lines
            _rt().write_session_state(target, session_id, updated)
    except inbox_lock.InboxLockTimeout:
        return


def _apply_timeout_followup_inprocess(
    event: str,
    target: Path,
    session_id: str,
    detail: str,
    *,
    publish: Callable[[str], None] | None = None,
) -> str:
    try:
        state = _rt()._record_hook_timeout(target, session_id, announce=True)
    except OSError:
        return "degraded"
    if state.get(_ANNOUNCE_CLAIMED_KEY) is True:
        outcome = "latched"
    elif state.get("hook_latched") is True:
        outcome = "latched_silent"
    else:
        outcome = "degraded"
    if publish is not None:
        publish(outcome)
    try:
        envelope.append_log(target, f"{event}: degraded: {detail}")
        if outcome == "latched":
            envelope.append_log(target, f"{event}: latched after repeated timeouts")
    except OSError:
        pass
    return outcome


def _bounded_timeout_followup(event: str, log_target: Path, session_id: str, exc: BaseException) -> str:
    """Record timeout latch/state without blocking past a short budget.

    The path was already resolved inside the timed worker. Latch-state
    writes (and that path's target-tree hook.log) still touch that tree;
    they run best-effort under a hard cap so a later stall cannot hang
    the parent. The latch outcome is published as soon as state is known
    so a later log stall cannot hide it. Non-timeout doctor-pointer logs
    use ``_append_degraded_diagnostic`` instead.
    """
    detail = str(exc)
    published = ["degraded"]

    def publish(outcome: str) -> None:
        published[0] = outcome

    _rt()._run_best_effort_bounded(
        lambda: _rt()._apply_timeout_followup_inprocess(event, log_target, session_id, detail, publish=publish),
        timeout=_rt()._TIMEOUT_FOLLOWUP_SECONDS,
        default="degraded",
    )
    return published[0]
