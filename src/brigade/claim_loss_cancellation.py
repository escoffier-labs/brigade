"""Stop run-owned subprocess groups independently of main-thread interrupts."""

from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable

from . import proc


class ClaimLossCancelled(BaseException):
    """Unwind claim loss without recording an operator or provider failure."""


def guard_run(function: Callable[..., int]) -> Callable[..., int]:
    """Classify fleet interrupts before the generic run lifecycle handles them."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> int:
        try:
            return function(*args, **kwargs)
        except KeyboardInterrupt:
            check = kwargs.get("check_cancellation")
            if check is not None:
                check()
            raise

    return wrapped


class ClaimLossCancellation:
    """A cheap callback channel with bounded-poll process cancellation.

    The heartbeat only sets an event. A separate watcher terminates the shared
    process registry even while dispatch is blocked in a syscall. The registry
    also terminates late registrations, so a racing launch cannot escape loss.
    Receipt classification and writing remain on the CLI thread.
    """

    def __init__(self) -> None:
        self.process_registry = proc.ProcessRegistry()
        self._requested = threading.Event()
        self._stopped = threading.Event()
        self._watcher = threading.Thread(target=self._watch, name="brigade-claim-cancel", daemon=True)

    def __enter__(self) -> ClaimLossCancellation:
        self._watcher.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stopped.set()
        self._watcher.join(timeout=2)

    def request(self) -> None:
        """Signal cancellation before any fallible heartbeat diagnostics."""
        self._requested.set()

    def raise_if_requested(self) -> None:
        """Never dispatch or report success after the claim-loss transition."""
        if self._requested.is_set():
            raise ClaimLossCancelled

    def _watch(self) -> None:
        while not self._requested.wait(0.05):
            if self._stopped.is_set():
                return
        self.process_registry.cancel()
