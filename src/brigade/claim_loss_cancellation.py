"""Stop run-owned subprocess groups independently of main-thread interrupts."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator

from . import fleet_client, proc, runguard
from .roster import Roster


# Allow nested Brigade seats to unwind their own registries before escalation.
CLAIM_LOSS_TERMINATE_GRACE_SECONDS = 3.0
CLAIM_LOSS_KILL_GRACE_SECONDS = 0.5


class ClaimLossCancelled(BaseException):
    """Unwind claim loss without recording an operator or provider failure."""


@contextmanager
def cancellation_boundary(check: Callable[[], None]) -> Iterator[None]:
    """Check loss after a call without replacing a failure that retains the lock."""
    try:
        yield
    except runguard.RetainRunLockError:
        raise
    except BaseException as exc:
        runguard.raise_if_retained_run_lock(exc)
        check()
        raise
    else:
        check()


def guard_run(function: Callable[..., int]) -> Callable[..., int]:
    """Classify fleet interrupts before the generic run lifecycle handles them."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> int:
        try:
            return function(*args, **kwargs)
        except (KeyboardInterrupt, ClaimLossCancelled) as exc:
            runguard.raise_if_retained_run_lock(exc)
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
        self._requested = threading.Event()
        self._stopped = threading.Event()
        self.process_registry = proc.ProcessRegistry(
            terminate_grace=CLAIM_LOSS_TERMINATE_GRACE_SECONDS,
            kill_grace=CLAIM_LOSS_KILL_GRACE_SECONDS,
            check_cancellation=self.raise_if_requested,
        )
        self.outcomes: tuple[proc.ProcessCancelOutcome, ...] = ()
        self._done = threading.Event()
        self._watcher: threading.Thread | None = None
        self._watcher_error: BaseException | None = None
        self._shutdown_deadline: float | None = None

    def __enter__(self) -> ClaimLossCancellation:
        self._watcher = threading.Thread(target=self._watch, name="brigade-claim-cancel", daemon=True)
        self._watcher.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        retained: runguard.RetainRunLockError | None = None
        while True:
            try:
                self._finish_exit()
                break
            except KeyboardInterrupt as exc:
                retained = retained or runguard.retained_run_lock_error(exc)
                if not self._requested.is_set():
                    if retained is None:
                        raise
                    break
            except runguard.RetainRunLockError:
                if retained is None:
                    raise
                break
        if retained is not None:
            raise retained

    def _finish_exit(self) -> None:
        self._stopped.set()
        if self._shutdown_deadline is None:
            self._shutdown_deadline = time.monotonic() + self.process_registry.cancellation_timeout
        deadline = self._shutdown_deadline
        while not self._done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise runguard.RetainRunLockError("claim-loss cancellation timed out; local run lock retained")
            self._done.wait(remaining)
        if self._requested.is_set():
            if self._watcher_error is not None:
                raise runguard.RetainRunLockError(
                    "claim-loss cancellation watcher failed; local run lock retained"
                ) from self._watcher_error
            # Include failures retained by concurrent dispatch cleanup and late registration.
            try:
                self.outcomes = self.process_registry.cancellation_outcomes
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                raise runguard.RetainRunLockError(
                    "claim-loss cancellation outcomes unavailable; local run lock retained"
                ) from exc
            if any(outcome.result != "interrupted" for outcome in self.outcomes):
                raise runguard.RetainRunLockError(
                    "claim-loss cancellation failed or left active processes; local run lock retained"
                )

    def request(self) -> None:
        """Signal cancellation before any fallible heartbeat diagnostics."""
        self._requested.set()

    def raise_if_requested(self) -> None:
        """Never dispatch or report success after the claim-loss transition."""
        if self._requested.is_set():
            raise ClaimLossCancelled

    def _watch(self) -> None:
        try:
            while not self._requested.wait(0.05):
                if self._stopped.is_set():
                    return
            self.outcomes = self.process_registry.cancel()
        except BaseException as exc:
            self._watcher_error = exc
            self.outcomes = (proc.ProcessCancelOutcome(None, "error"),)
        finally:
            self._done.set()


def model_lease_lifecycle(function: Callable[..., int]) -> Callable[..., int]:
    """Freeze one startup policy snapshot without reserving unused seats."""

    def wrapped(task: str, roster: Roster, *args: Any, **kwargs: Any) -> int:
        snapshot = fleet_client.load_model_policy_snapshot()
        kwargs["model_policy_snapshot"] = snapshot
        return function(task, roster, *args, **kwargs)

    return wrapped
