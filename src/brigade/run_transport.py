"""Typed worker transport dispatch for :mod:`brigade.aboyeur`."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from . import agents, run_control
from .roster import Agent, Roster, is_cli_allowed, timeout_for


@dataclass(frozen=True)
class Assignment:
    worker: str
    task: str
    stage: int = 1
    covers: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerResult:
    worker: str
    task: str
    text: str
    ok: bool
    detail: str = ""
    thread_id: str | None = None
    status: str = ""
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stdout_log: str | None = None
    stderr_log: str | None = None
    duration_seconds: float | None = None
    transport: str = "cli"
    requested_model: str | None = None
    effective_model: str | None = None
    reasoning: str | None = None
    stop_reason: str | None = None
    protocol_version: int | None = None
    session_id: str | None = None
    request_id: str | None = None
    acpx_version: str | None = None
    safe_events: tuple[dict[str, object], ...] = ()


class PromptBuilder(Protocol):
    def __call__(
        self,
        agent: Agent,
        assignment: Assignment,
        *,
        prior_results: list[WorkerResult] | None = None,
        read_only: bool = False,
        direct: bool = False,
        code_graph: Any | None = None,
        drift_impact: Any | None = None,
        evidence: Any | None = None,
    ) -> str: ...


class AppserverRunner(Protocol):
    def __call__(
        self,
        appserver: Any,
        agent: Agent,
        worker: str,
        prompt: str,
        *,
        timeout: float,
        cwd: Path | None,
        read_only: bool,
        sandbox: str | None,
        registry: run_control.LiveTurnRegistry | None,
        on_event: Any = None,
    ) -> agents.AgentResult: ...


class EventWriter(Protocol):
    def __call__(
        self, events_dir: Path | None, worker: str, *, verbose: bool = False
    ) -> Callable[[dict[str, Any]], None] | None: ...


def dispatch(
    assignments: list[Assignment],
    roster: Roster,
    *,
    build_prompt: PromptBuilder,
    run_appserver_worker: AppserverRunner,
    event_writer: EventWriter,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    direct: bool = False,
    code_graph: object | None = None,
    drift_impact: object | None = None,
    evidence: object | None = None,
    appserver: object | None = None,
    control_registry: run_control.LiveTurnRegistry | None = None,
    events_dir: Path | None = None,
    verbose: bool = False,
    authorized_writable_worktree: bool = False,
) -> list[WorkerResult]:
    """Dispatch staged assignments while keeping transport policy in one module."""

    def run_one(assignment: Assignment, prior_results: list[WorkerResult]) -> WorkerResult:
        agent = roster.agents[assignment.worker]
        if agent.cli is None or not is_cli_allowed(agent.cli, roster):
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text="",
                ok=False,
                detail=(
                    "worker has no CLI adapter"
                    if agent.cli is None
                    else f"{agent.cli} is not allowed by limits.allow_models"
                ),
            )
        cli_ref = agent.cli
        prompt = build_prompt(
            agent,
            assignment,
            prior_results=prior_results,
            read_only=read_only,
            direct=direct,
            code_graph=code_graph,
            drift_impact=drift_impact,
            evidence=evidence,
        )
        started = time.monotonic()
        effective_read_only = read_only if sandbox_read_only is None else sandbox_read_only
        if agent.transport == "acpx":
            from . import acpx_adapter

            result = acpx_adapter.run_cursor(
                prompt,
                cwd=cwd or Path.cwd(),
                timeout=timeout_for(agent, roster),
                model=agent.model or "",
                version=agent.transport_version or "",
                read_only=effective_read_only,
                writable_worktree=authorized_writable_worktree,
            )
        elif agent.cli == "codex" and appserver is not None:
            on_event = event_writer(events_dir, assignment.worker, verbose=verbose)
            result = run_appserver_worker(
                appserver,
                agent,
                assignment.worker,
                prompt,
                timeout=timeout_for(agent, roster),
                cwd=cwd,
                read_only=effective_read_only,
                sandbox=sandbox,
                registry=control_registry,
                on_event=on_event,
            )
        else:
            timeout = timeout_for(agent, roster)
            if sandbox is None and agent.model is None and agent.reasoning is None:
                result = agents.run_agent(cli_ref, prompt, timeout=timeout, cwd=cwd, read_only=effective_read_only)
            elif sandbox is not None and agent.model is None and agent.reasoning is None:
                result = agents.run_agent(
                    cli_ref,
                    prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                )
            elif sandbox is None and agent.model is not None and agent.reasoning is None:
                result = agents.run_agent(
                    cli_ref,
                    prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    model=agent.model,
                )
            elif sandbox is None and agent.model is None and agent.reasoning is not None:
                result = agents.run_agent(
                    cli_ref,
                    prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    reasoning=agent.reasoning,
                )
            elif sandbox is not None and agent.model is not None and agent.reasoning is None:
                result = agents.run_agent(
                    cli_ref,
                    prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    model=agent.model,
                )
            elif sandbox is not None and agent.model is None and agent.reasoning is not None:
                result = agents.run_agent(
                    cli_ref,
                    prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    reasoning=agent.reasoning,
                )
            elif sandbox is None and agent.model is not None and agent.reasoning is not None:
                result = agents.run_agent(
                    cli_ref,
                    prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    model=agent.model,
                    reasoning=agent.reasoning,
                )
            else:
                assert sandbox is not None
                assert agent.model is not None
                assert agent.reasoning is not None
                result = agents.run_agent(
                    cli_ref,
                    prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    model=agent.model,
                    reasoning=agent.reasoning,
                )
        return WorkerResult(
            worker=assignment.worker,
            task=assignment.task,
            text=result.text,
            ok=result.ok,
            detail=result.detail,
            thread_id=result.thread_id,
            status=result.status,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_seconds=max(0.0, round(time.monotonic() - started, 3)),
            transport=result.transport,
            requested_model=result.requested_model,
            effective_model=result.effective_model,
            reasoning=result.reasoning,
            stop_reason=result.stop_reason,
            protocol_version=result.protocol_version,
            session_id=result.session_id,
            request_id=result.request_id,
            acpx_version=result.acpx_version,
            safe_events=result.safe_events,
        )

    if not assignments:
        return []

    all_results: list[WorkerResult] = []
    for stage in sorted({assignment.stage for assignment in assignments}):
        stage_assignments = [assignment for assignment in assignments if assignment.stage == stage]
        stage_results_by_index: dict[int, WorkerResult] = {}
        prior_results = list(all_results)
        with ThreadPoolExecutor(max_workers=min(roster.max_workers, len(stage_assignments))) as executor:
            future_to_index = {
                executor.submit(run_one, assignment, prior_results): index
                for index, assignment in enumerate(stage_assignments)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    stage_results_by_index[index] = future.result()
                except Exception as exc:  # pragma: no cover - defensive boundary
                    assignment = stage_assignments[index]
                    stage_results_by_index[index] = WorkerResult(
                        worker=assignment.worker,
                        task=assignment.task,
                        text="",
                        ok=False,
                        detail=str(exc)[:200],
                    )
        all_results.extend(stage_results_by_index[index] for index in range(len(stage_assignments)))
    return all_results
