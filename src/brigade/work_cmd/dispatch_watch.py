"""Lease-owned supervision of immutable dispatch ticks and graceful draining."""

from __future__ import annotations

import signal
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from .. import __version__, localio
from . import dispatch_state as store
from .dispatch_apply import Apply


def installed_version() -> str:
    """Read distribution metadata afresh, rather than the imported module cache."""
    try:
        return metadata.version("brigade-cli")
    except metadata.PackageNotFoundError:
        return __version__


class Watch:
    def __init__(self, engine: Apply, directory: Path, session: dict[str, Any], *, interval: float, drain_wait: bool):
        self.engine = engine
        self.directory = directory
        self.session = session
        self.interval = interval
        self.drain_wait = drain_wait
        self.reason: str | None = None
        self.version = installed_version()
        self.started = time.monotonic()
        self.initial_runs = {row["intent_id"] for _, row in store.intents(engine.target) if row.get("run_dir")}
        self.seen = self.snapshot()
        self.tick_no = 0
        self.denied = False

    def snapshot(self) -> dict[str, tuple[bool, str]]:
        return {row["intent_id"]: (bool(row.get("pid")), row["state"]) for _, row in store.intents(self.engine.target)}

    def launched(self) -> int:
        return sum(
            bool(row.get("run_dir")) and row["intent_id"] not in self.initial_runs
            for _, row in store.intents(self.engine.target)
        )

    def exclusions(self) -> dict[str, str]:
        """Keep exhausted infrastructure retries bounded across watch ticks."""
        own_sessions = {
            directory
            for directory in store.sessions(self.engine.target)
            if store.read(directory / "session.json").get("parent_watch") == self.session["dispatch_id"]
        }
        return {
            row["task_id"]: "infra-retries-exhausted"
            for path, row in store.intents(self.engine.target)
            if path.parent.parent in own_sessions and row.get("retry_exhausted")
        }

    def can_launch(self) -> bool:
        if self.reason is None and installed_version() != self.version:
            self.reason = "version-changed"
        return self.reason is None

    def budget_allows(self) -> bool:
        if not self.can_launch():
            return False
        elapsed = time.monotonic() - self.started
        deadline, ceiling = self.session.get("deadline"), self.session.get("max_total")
        dimension = (
            "wall_clock_seconds"
            if deadline is not None and elapsed >= deadline
            else "worker_dispatch_count"
            if ceiling is not None and self.launched() >= ceiling
            else None
        )
        if dimension is None:
            return True
        self.reason = "deadline" if dimension == "wall_clock_seconds" else "max-total"
        if not self.denied:
            store.event(
                self.directory,
                "run_budget.reservation_denied",
                {
                    "dimension": dimension,
                    "mode": "enforceable",
                    "declared": int((deadline or 0) if dimension == "wall_clock_seconds" else (ceiling or 0)),
                    "used": int(elapsed) if dimension == "wall_clock_seconds" else self.launched(),
                    "remaining": 0,
                    "request_id": self.session["dispatch_id"],
                    "reason_class": "reservation_denied",
                },
            )
            self.denied = True
        return False

    def pause(self, seconds: float) -> None:
        until = time.monotonic() + seconds
        while self.can_launch():
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
            if not self.budget_allows():
                break

    def record_tick(self) -> None:
        current = self.snapshot()
        launched = [key for key, (has_run, _) in current.items() if has_run and not self.seen.get(key, (False, ""))[0]]
        finalized = [
            key
            for key, (_, state) in current.items()
            if state in store.TERMINAL and self.seen.get(key, (False, ""))[1] != state
        ]
        if launched or finalized:
            store.event(
                self.directory,
                "work.dispatch.tick",
                {
                    "tick": self.tick_no,
                    "launched": launched,
                    "finalized": finalized,
                },
            )
        self.seen = current
        self.tick_no += 1

    def run(self, tick: Callable[[], int]) -> int:
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

        def stop(signum: int, frame: Any) -> None:
            if self.reason is None:
                self.reason = signal.Signals(signum).name

        result = 0
        try:
            for sig in previous:
                signal.signal(sig, stop)
            self.engine.can_launch = self.can_launch
            self.engine.reservation_allowed = self.budget_allows
            self.engine.pause = self.pause
            store.event(self.directory, "work.dispatch.opened", {"version": self.version})
            while self.budget_allows():
                result = max(result, tick())
                self.record_tick()
                if self.budget_allows():
                    self.pause(self.interval)
            # Reconciliation in drain mode observes and finalizes, without new
            # intents, retries, or resuming a pre-launch intent.
            while True:
                result = max(result, self.engine.reconcile(no_wait=True, launch=False))
                self.record_tick()
                if not self.drain_wait or not store.live_children(self.engine.target):
                    break
                time.sleep(min(0.1, self.interval))
        except BaseException:
            self.reason = self.reason or "error"
            raise
        finally:
            try:
                self.record_tick()
                self.session.update(finished_at=localio.utc_now_iso(), close_reason=self.reason or "stopped")
                store.event(
                    self.directory,
                    "work.dispatch.closed",
                    {
                        "reason": self.session["close_reason"],
                        "drain_wait": self.drain_wait,
                        "live_children": store.live_children(self.engine.target),
                    },
                )
                localio.write_json(self.directory / "session.json", self.session)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                self.engine.can_launch = lambda: True
                self.engine.reservation_allowed = lambda: True
                self.engine.pause = time.sleep
        return result
