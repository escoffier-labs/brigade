from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock
from typing import Any, Callable

from . import agents, localio, proc, run_control, run_receipts, run_transport
from .roster import Agent, Roster


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


def _unsupported_appserver(
    appserver: object,
    agent: Agent,
    worker: str,
    prompt: str,
    *,
    timeout: float,
    cwd: Path | None,
    read_only: bool,
    sandbox: str | None,
    registry: run_control.LiveTurnRegistry | None,
    on_event: object = None,
) -> agents.AgentResult:
    raise RuntimeError("single-seat research uses direct transport")


def _no_events(
    events_dir: Path | None,
    worker: str,
    *,
    verbose: bool = False,
    workspace: Path | None = None,
    correlation_marker: str | None = None,
) -> Callable[[dict[str, Any]], None] | None:
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
        on_dispatch_requested: Callable[[Agent], int | None] | None = None,
        on_dispatch_observed: Callable[[Agent, int], None] | None = None,
        on_dispatch_completed: Callable[[Agent, int], None] | None = None,
        on_dispatch_failed: Callable[[Agent, int], None] | None = None,
        receipt_admission: Event | None = None,
    ) -> None:
        self.roster = roster
        self.cwd = cwd
        self.run_id = run_id
        self.process_registry = process_registry
        self.output_dir = output_dir
        self.events_dir = events_dir
        self._receipt_lock = Lock()
        self._receipt_sequence = 0
        self.on_dispatch_requested = on_dispatch_requested
        self.on_dispatch_observed = on_dispatch_observed
        self.on_dispatch_completed = on_dispatch_completed
        self.on_dispatch_failed = on_dispatch_failed
        self._receipts_open = receipt_admission is None or receipt_admission.is_set()

    def close_receipts(self) -> None:
        """Atomically prevent later worker receipts from being written."""
        with self._receipt_lock:
            self._receipts_open = False

    def invoke(
        self,
        *,
        seat: str,
        phase: str,
        prompt: str,
        timeout: int,
        deadline_ceiling: bool = False,
    ) -> SeatResult:
        agent = self.roster.agents[seat]
        # Normal phase calls keep the roster timeout floor. Discovery/run-deadline
        # callers pass deadline_ceiling=True so remaining budget is a hard cap.
        if deadline_ceiling:
            effective_timeout = float(timeout)
        else:
            effective_timeout = max(float(timeout), agent.timeout_seconds or 0.0)
        effective = replace(agent, timeout_seconds=effective_timeout)
        roster = replace(self.roster, agents={**self.roster.agents, seat: effective})
        assignment = run_transport.Assignment(worker=seat, task=phase, capabilities=(phase,))

        def build_prompt(
            agent: Agent,
            assignment: run_transport.Assignment,
            *,
            prior_results: list[run_transport.WorkerResult] | None = None,
            read_only: bool = False,
            direct: bool = False,
            code_graph: Any | None = None,
            drift_impact: Any | None = None,
            evidence: Any | None = None,
        ) -> str:
            return prompt

        def dispatch() -> list[run_transport.WorkerResult]:
            return run_transport.dispatch(
                [assignment],
                roster,
                build_prompt=build_prompt,
                run_appserver_worker=_unsupported_appserver,
                event_writer=_no_events,
                cwd=self.cwd,
                read_only=True,
                direct=True,
                fail_fast=True,
                process_registry=self.process_registry,
                events_dir=self.events_dir,
                run_id=self.run_id,
                on_dispatch_requested=self.on_dispatch_requested,
                on_dispatch_observed=self.on_dispatch_observed,
                on_dispatch_completed=self.on_dispatch_completed,
                on_dispatch_failed=self.on_dispatch_failed,
            )

        if agent.cli == "oracle":
            gate_started = time.monotonic()
            acquired = _ORACLE_GATE.acquire(timeout=effective_timeout)
            if not acquired:
                worker = run_transport.WorkerResult(
                    worker=seat,
                    task=phase,
                    text="",
                    ok=False,
                    detail="timed out waiting for the Oracle dispatch gate",
                    failure_phase="dispatch",
                    failure_kind="timeout",
                )
            else:
                try:
                    # Gate wait counts against deadline_ceiling; pass only remaining time.
                    if deadline_ceiling:
                        elapsed = time.monotonic() - gate_started
                        remaining = max(0.0, effective_timeout - elapsed)
                        if remaining <= 0.0:
                            worker = run_transport.WorkerResult(
                                worker=seat,
                                task=phase,
                                text="",
                                ok=False,
                                detail="timed out waiting for the Oracle dispatch gate",
                                failure_phase="dispatch",
                                failure_kind="timeout",
                            )
                        else:
                            effective = replace(agent, timeout_seconds=remaining)
                            roster = replace(self.roster, agents={**self.roster.agents, seat: effective})
                            results = dispatch()
                            if not results:
                                worker = run_transport.WorkerResult(
                                    worker=seat,
                                    task=phase,
                                    text="",
                                    ok=False,
                                    detail="dispatch returned no worker results",
                                    failure_phase="dispatch",
                                    failure_kind="unclassified",
                                )
                            else:
                                worker = results[0]
                    else:
                        results = dispatch()
                        if not results:
                            worker = run_transport.WorkerResult(
                                worker=seat,
                                task=phase,
                                text="",
                                ok=False,
                                detail="dispatch returned no worker results",
                                failure_phase="dispatch",
                                failure_kind="unclassified",
                            )
                        else:
                            worker = results[0]
                finally:
                    _ORACLE_GATE.release()
        else:
            results = dispatch()
            if not results:
                worker = run_transport.WorkerResult(
                    worker=seat,
                    task=phase,
                    text="",
                    ok=False,
                    detail="dispatch returned no worker results",
                    failure_phase="dispatch",
                    failure_kind="unclassified",
                )
            else:
                worker = results[0]
        if worker.failure_kind == "browser-auth":
            worker = replace(
                worker,
                stdout="",
                stderr="",
                attempts=tuple(replace(attempt, stdout="", stderr="") for attempt in worker.attempts),
            )
        with self._receipt_lock:
            if not self._receipts_open:
                return SeatResult(
                    seat=seat,
                    attempt_id=f"{self.run_id}:discarded",
                    text=worker.text,
                    ok=worker.ok,
                    failure_phase=worker.failure_phase,
                    failure_kind=worker.failure_kind,
                    detail=worker.detail,
                    requested_model=worker.requested_model or agent.model,
                    observed_model=worker.effective_model or "unverified",
                    attempts=tuple(worker.attempts),
                )
            self._receipt_sequence += 1
            sequence = self._receipt_sequence
            attempt_id = f"{self.run_id}:{sequence:04d}"
            receipt_path = self.output_dir / "workers" / (f"{sequence:04d}-{phase.replace('.', '-')}-{seat}.json")
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
