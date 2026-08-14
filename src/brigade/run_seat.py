from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Callable

from . import localio, proc, run_receipts, run_transport
from .roster import Roster


_ORACLE_GATE = BoundedSemaphore(1)


@dataclass(frozen=True)
class SeatResult:
    seat: str
    attempt_id: str
    text: str
    ok: bool
    failure_phase: str | None
    failure_kind: str | None
    detail: str
    requested_model: str | None
    observed_model: str
    attempts: tuple[object, ...]


def _unsupported_appserver(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("single-seat research uses direct transport")


def _no_events(*_args: object, **_kwargs: object) -> None:
    return None


class SeatInvoker:
    def __init__(
        self,
        *,
        roster: Roster,
        cwd: Path,
        run_id: str,
        process_registry: proc.ProcessRegistry,
        output_dir: Path,
        events_dir: Path | None = None,
        on_dispatch_requested: Callable | None = None,
        on_dispatch_observed: Callable | None = None,
        on_dispatch_completed: Callable | None = None,
        on_dispatch_failed: Callable | None = None,
    ) -> None:
        self.roster = roster
        self.cwd = cwd
        self.run_id = run_id
        self.process_registry = process_registry
        self.output_dir = output_dir
        self.events_dir = events_dir
        self._receipt_lock = Lock()
        self._receipt_sequence = 0
        self.budget_callbacks = {
            "on_dispatch_requested": on_dispatch_requested,
            "on_dispatch_observed": on_dispatch_observed,
            "on_dispatch_completed": on_dispatch_completed,
            "on_dispatch_failed": on_dispatch_failed,
        }

    def invoke(self, *, seat: str, phase: str, prompt: str, timeout: int) -> SeatResult:
        agent = self.roster.agents[seat]
        effective_timeout = max(float(timeout), agent.timeout_seconds or 0.0)
        effective = replace(agent, timeout_seconds=effective_timeout)
        roster = replace(self.roster, agents={**self.roster.agents, seat: effective})
        assignment = run_transport.Assignment(worker=seat, task=phase, capabilities=(phase,))
        gate = _ORACLE_GATE if agent.cli == "oracle" else nullcontext()
        with gate:
            results = run_transport.dispatch(
                [assignment],
                roster,
                build_prompt=lambda _agent, _assignment, **_context: prompt,
                run_appserver_worker=_unsupported_appserver,
                event_writer=_no_events,
                cwd=self.cwd,
                read_only=True,
                direct=True,
                fail_fast=True,
                process_registry=self.process_registry,
                events_dir=self.events_dir,
                run_id=self.run_id,
                **{key: value for key, value in self.budget_callbacks.items() if value is not None},
            )
        worker = results[0]
        if worker.failure_kind == "browser-auth":
            worker = replace(
                worker,
                stdout="",
                stderr="",
                attempts=tuple(
                    replace(attempt, stdout="", stderr="") for attempt in worker.attempts
                ),
            )
        with self._receipt_lock:
            self._receipt_sequence += 1
            sequence = self._receipt_sequence
            attempt_id = f"{self.run_id}:{sequence:04d}"
            receipt_path = self.output_dir / "workers" / (
                f"{sequence:04d}-{phase.replace('.', '-')}-{seat}.json"
            )
            # write_worker_logs indexes from 1 per call; stamp the worker name so
            # repeated SeatInvoker.invoke calls keep distinct log paths.
            labeled = replace(worker, worker=f"{worker.worker}-{sequence:04d}")
            recorded = run_receipts.write_worker_logs(self.output_dir, [labeled])[0]
            recorded = replace(recorded, worker=worker.worker)
            receipt = run_receipts.worker_payload_one(recorded)
            receipt["attempt_id"] = attempt_id
            localio.write_json(receipt_path, receipt)
        return SeatResult(
            seat=seat,
            attempt_id=attempt_id,
            text=worker.text,
            ok=worker.ok,
            failure_phase=worker.failure_phase,
            failure_kind=worker.failure_kind,
            detail=worker.detail,
            requested_model=recorded.requested_model or agent.model,
            observed_model=recorded.effective_model or "unverified",
            attempts=tuple(recorded.attempts),
        )
