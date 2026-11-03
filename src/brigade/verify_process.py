"""Process-lifetime signal ownership for the ``brigade`` console entry point.

``brigade.cli.process_main`` enters this on every invocation, so it must stay
cheap to import: standard library only, no ``brigade.work_cmd`` package
import. ``brigade.work_cmd.verify_runtime`` re-exports these names and shares
the same ``_ACTIVE_VERIFY_PROCESS`` context variable.
"""

from __future__ import annotations

import signal
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


class _VerifyProcessSignals:
    """Retain caught cancellation only for an explicitly owned CLI lifetime."""

    def __init__(self) -> None:
        self.handlers: dict[int, tuple[Any, Any]] = {}
        self.ignored: set[int] = set()
        self.finished = False

    def finish(self) -> None:
        # CPython resets Python handlers to SIG_DFL during finalization, but
        # preserves SIG_IGN. Do this only after main and its spawn work finish.
        # Final shutdown callbacks must not spawn children after this boundary.
        for signum, (_, handler) in self.handlers.items():
            if signal.getsignal(signum) is handler:
                signal.signal(signum, signal.SIG_IGN)
                self.ignored.add(signum)
        self.finished = True

    def restore(self) -> None:
        if not self.finished:
            for signum, (previous, handler) in self.handlers.items():
                current = signal.getsignal(signum)
                if current is handler or (signum in self.ignored and current == signal.SIG_IGN):
                    signal.signal(signum, previous)


_ACTIVE_VERIFY_PROCESS: ContextVar[_VerifyProcessSignals | None] = ContextVar("verify_process", default=None)


@contextmanager
def _verify_process_signals():
    owner = _VerifyProcessSignals()
    token = _ACTIVE_VERIFY_PROCESS.set(owner)
    try:
        yield owner
    finally:
        try:
            owner.restore()
        finally:
            _ACTIVE_VERIFY_PROCESS.reset(token)
