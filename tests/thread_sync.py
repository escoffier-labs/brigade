"""Deterministic threading helpers for tests.

Fixed short ``Event.wait`` / ``Thread.join`` timeouts are load-sensitive and
flake under CI. Prefer explicit events, predicates, and hard-timeout guards.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

DEFAULT_HARD_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 0.001


def current_thread_cancelled() -> bool:
    """Return whether ``join_thread`` requested cancellation for this worker."""
    cancel = getattr(threading.current_thread(), "_thread_sync_cancel", None)
    return cancel is not None and cancel.is_set()


def cancel_thread(thread: threading.Thread) -> None:
    """Request cooperative cancellation for a thread started by ``start_thread``."""
    cancel = getattr(thread, "_thread_sync_cancel", None)
    if cancel is not None:
        cancel.set()


def note_cleanup_failure(primary: BaseException, cleanup: BaseException) -> None:
    """Record a cleanup failure on ``primary`` without requiring Python 3.11 ``add_note``."""
    detail = f"cleanup failed: {cleanup!r}"
    add_note = getattr(primary, "add_note", None)
    if add_note is not None:
        add_note(detail)
        return
    if primary.args:
        primary.args = (f"{primary.args[0]} ({detail})", *primary.args[1:])
    else:
        primary.args = (detail,)


def start_thread(target: Callable[[], None]) -> threading.Thread:
    """Start a daemon ``target`` and preserve any target exception for ``join_thread``."""
    failures: list[BaseException] = []
    cancel = threading.Event()

    def capture_failure() -> None:
        try:
            target()
        except BaseException as error:
            if not cancel.is_set():
                failures.append(error)

    thread = threading.Thread(target=capture_failure, daemon=True)
    thread._thread_sync_failures = failures
    thread._thread_sync_cancel = cancel
    thread.start()
    return thread


def wait_for_event(
    event: threading.Event,
    *,
    description: str,
    hard_timeout: float = DEFAULT_HARD_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Block until ``event`` is set, failing on ``hard_timeout``."""
    deadline = time.monotonic() + hard_timeout
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for event: {description}")
        event.wait(timeout=min(poll_interval, remaining))


def wait_for_predicate(
    predicate: Callable[[], bool],
    *,
    description: str,
    hard_timeout: float = DEFAULT_HARD_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Poll ``predicate`` until it returns true, failing on ``hard_timeout``."""
    deadline = time.monotonic() + hard_timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for condition: {description}")
        time.sleep(poll_interval)


def join_thread(
    thread: threading.Thread,
    *,
    description: str,
    hard_timeout: float = DEFAULT_HARD_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Join a thread from ``start_thread``, raising its target exception if any."""
    deadline = time.monotonic() + hard_timeout
    while thread.is_alive():
        thread.join(timeout=poll_interval)
        if thread.is_alive() and time.monotonic() >= deadline:
            cancel_thread(thread)
            raise AssertionError(f"timed out waiting for thread: {description}")
    failures = getattr(thread, "_thread_sync_failures", [])
    if failures:
        raise failures[0]


class ThreadGate:
    """Two-party open/close gate for cross-thread handoffs."""

    def __init__(self) -> None:
        self._opened = threading.Event()

    def open(self) -> None:
        self._opened.set()

    def close(self) -> None:
        self._opened.clear()

    def wait_open(self, *, description: str) -> None:
        wait_for_event(self._opened, description=description)
