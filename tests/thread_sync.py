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
    """Join ``thread``, failing on ``hard_timeout`` instead of a short guess."""
    deadline = time.monotonic() + hard_timeout
    while thread.is_alive():
        thread.join(timeout=poll_interval)
        if not thread.is_alive():
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for thread: {description}")


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
