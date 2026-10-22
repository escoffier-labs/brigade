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
from .ledger import locking


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
        self.stop_waiting = False
        self.version = installed_version()
        self.started = time.monotonic()
        self.next_version_check = self.started + 1
        self.seen = self.snapshot()
        self.initial_runs = {key for key, (has_run, _) in self.seen.items() if has_run}
        self.launch_count = 0
        self.tick_no = 0
        self.denied = False
        self.exhausted: dict[str, str] = {}
        self.settled_ticks: set[str] = set()

    def snapshot(self) -> dict[str, tuple[bool, str]]:
        result = {}
        for _, row in store.intents(self.engine.target):
            pid = row.get("pid")
            if (type(pid) is not int or pid <= 0) and row.get("run_dir"):
                # The bootstrap can publish its PID before the dispatcher
                # persists it. Exit does not erase that historical launch.
                receipt = localio.read_json_dict(Path(row["run_dir"]) / "dispatch-launch.json")
                pid = receipt.get("pid") if receipt is not None else None
            result[row["intent_id"]] = (type(pid) is int and pid > 0, row["state"])
        return result

    def launched(self) -> int:
        return self.launch_count

    def reserve(self) -> bool:
        # Reservations can happen several times within a tick, including retry
        # and resumed launches. Refresh here, never on every pause wake.
        self.count_launches(self.snapshot())
        return self.budget_allows()

    def count_launches(self, current: dict[str, tuple[bool, str]]) -> None:
        self.launch_count = sum(has_run and key not in self.initial_runs for key, (has_run, _) in current.items())

    def exclusions(self) -> dict[str, str]:
        """Keep exhausted infrastructure retries bounded across watch ticks.

        Only this watch's own tick sessions are read, and a finished tick is
        folded in once, so per-tick work does not grow with history.
        """
        for tick_id in self.session.get("tick_sessions", []):
            if tick_id in self.settled_ticks:
                continue
            directory = store.root(self.engine.target) / tick_id
            for path in (directory / "intents").glob("*.json"):
                row = store.read(path)
                if row.get("retry_exhausted"):
                    self.exhausted[row["task_id"]] = "infra-retries-exhausted"
            if store.read(directory / "session.json").get("finished_at"):
                self.settled_ticks.add(tick_id)
        return dict(self.exhausted)

    def can_launch(self, *, check_version: bool = False) -> bool:
        now = time.monotonic()
        if self.reason is None and (check_version or now >= self.next_version_check):
            self.next_version_check = now + 1
            if installed_version() != self.version:
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
        self.count_launches(current)
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

    def claimed_prelaunch_intents(self) -> int:
        claims = {
            task["id"]: (task.get("claim") or {}).get("claim_id")
            for task in locking._read_task_ledger(self.engine.target)["tasks"]
        }
        return sum(
            row["state"] in {"planned", "claimed", "workspace_ready", "launching"}
            and claims.get(row["task_id"]) == row["claim_id"]
            and not store.child_alive(row)
            for _, row in store.intents(self.engine.target)
        )

    def capacity_allows(self) -> bool:
        # One tick can write planned, error, tick, and budget denial. Keep
        # two further envelopes for the final drain snapshot and close.
        if not store.event_room(self.directory, reserve_events=6):
            self.reason = self.reason or "event-capacity"
            return False
        return True

    def own_capacity(self, exc: store.EventCapacityError) -> bool:
        # Only this watch's own log is a graceful capacity close; a child
        # session's full log raised through a tick is a real failure.
        return exc.directory == self.directory.resolve()

    def run(self, tick: Callable[[], int]) -> int:
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

        def stop(signum: int, frame: Any) -> None:
            if self.reason is None:
                self.reason = signal.Signals(signum).name
            else:
                self.stop_waiting = True

        result = 0
        failure: BaseException | None = None
        try:
            for sig in previous:
                signal.signal(sig, stop)
            self.engine.can_launch = self.can_launch
            self.engine.reservation_allowed = self.reserve
            self.engine.pause = self.pause
            self.engine.continue_on_error = True
            store.event(self.directory, "work.dispatch.opened", {"version": self.version})
            while self.can_launch(check_version=True) and self.capacity_allows() and self.budget_allows():
                try:
                    result = max(result, tick())
                except Exception as exc:
                    if isinstance(exc, store.EventCapacityError) and self.own_capacity(exc):
                        self.reason = self.reason or "event-capacity"
                        break
                    try:
                        store.event(
                            self.directory,
                            "work.dispatch.tick_error",
                            {"tick": self.tick_no, "error_type": type(exc).__name__, "error": str(exc)},
                        )
                    except BaseException:
                        raise exc from None
                    result = max(result, 1)
                self.record_tick()
                if self.budget_allows():
                    self.pause(self.interval)
            # Reconciliation in drain mode observes and finalizes, without new
            # intents, retries, or resuming a pre-launch intent.
            while not self.stop_waiting:
                result = max(result, self.engine.reconcile(no_wait=True, launch=False))
                # Near the bound, aggregate drain observations into the final
                # tick instead of consuming its reserved envelope each poll.
                if store.event_room(self.directory, reserve_events=3):
                    self.record_tick()
                if not self.drain_wait:
                    break
                if not store.live_children(self.engine.target):
                    # A child can exit after the reconcile above observed it
                    # alive. This watch owns the dispatch lock and launches
                    # nothing while draining, so no live child can appear:
                    # one more pass finalizes whatever just exited.
                    if not self.stop_waiting:
                        result = max(result, self.engine.reconcile(no_wait=True, launch=False))
                    break
                time.sleep(min(0.1, self.interval))
        except store.EventCapacityError as exc:
            if not self.own_capacity(exc):
                failure = exc
                self.reason = self.reason or "error"
                raise
            self.reason = self.reason or "event-capacity"
        except BaseException as exc:
            failure = exc
            self.reason = self.reason or "error"
            raise
        finally:
            try:
                try:
                    self.record_tick()
                    store.event(
                        self.directory,
                        "work.dispatch.closed",
                        {
                            "reason": self.reason or "stopped",
                            "drain_wait": self.drain_wait,
                            "live_children": store.live_children(self.engine.target),
                            "claimed_prelaunch_intents": self.claimed_prelaunch_intents(),
                        },
                    )
                except store.EventCapacityError as exc:
                    if not self.own_capacity(exc):
                        self.reason = self.reason or "error"
                        raise
                    self.reason = self.reason or "event-capacity"
                except BaseException:
                    self.reason = self.reason or "error"
                    raise
                finally:
                    # The session receipt is independent of event-log space,
                    # including a pre-existing full log or closing I/O error.
                    self.session.update(
                        finished_at=localio.utc_now_iso(),
                        close_reason=self.reason or "stopped",
                        launched=self.launched(),
                    )
                    localio.write_json(self.directory / "session.json", self.session)
            except BaseException:
                if failure is None:
                    raise
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                self.engine.can_launch = lambda: True
                self.engine.reservation_allowed = lambda: True
                self.engine.pause = time.sleep
                self.engine.continue_on_error = False
        return result
