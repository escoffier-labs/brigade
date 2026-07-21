"""Typed worker transport dispatch for :mod:`brigade.aboyeur`."""

from __future__ import annotations

import inspect
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from . import agents, proc, run_control
from .roster import Agent, Roster, is_cli_allowed, timeout_for

_GROK_CONTINUATION_PROMPT = (
    "Return the final answer now using the required structured answer schema. "
    "Do not narrate progress or repeat the task."
)


@dataclass(frozen=True)
class Assignment:
    worker: str
    task: str
    stage: int = 1
    covers: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerAttempt:
    kind: str
    worker: str
    task: str
    transport: str
    model: str | None
    reasoning: str | None
    started_at: str
    finished_at: str
    exit_code: int | None
    terminal_reason: str
    failure_phase: str | None
    failure_kind: str | None
    session_id: str | None
    selected: bool = False
    stdout: str | None = None
    stderr: str | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None


def _attempt_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_direct_grok_invalid_final(
    agent: Agent,
    result: agents.AgentResult,
    *,
    direct: bool,
    read_only: bool,
) -> bool:
    return (
        direct
        and read_only
        and agent.cli == "grok"
        and agent.transport == "direct"
        and result.failure_phase == "output-validation"
        and result.failure_kind == "malformed-final-output"
    )


def _cloudflare_preflight_failure(agent: Agent, assignment: Assignment) -> WorkerResult | None:
    """Return a preflight failure if the agent's Cloudflare route lacks env.

    Empty string values are treated as missing.
    """

    detail = agents.cloudflare_ai_gateway_preflight_detail(agent.model)
    if detail is None:
        return None
    return WorkerResult(
        worker=assignment.worker,
        task=assignment.task,
        text="",
        ok=False,
        detail=detail,
        failure_phase="preflight",
        failure_kind="provider-config",
    )


def _worker_attempt(
    *,
    kind: str,
    worker: Agent,
    task: str,
    result: agents.AgentResult,
    started_at: str,
    finished_at: str,
    selected: bool = False,
) -> WorkerAttempt:
    terminal_reason = result.stop_reason or (
        "completed" if result.ok else result.failure_kind or result.detail or "failed"
    )
    return WorkerAttempt(
        kind=kind,
        worker=worker.name,
        task=task,
        transport=worker.transport,
        model=result.requested_model or worker.model,
        reasoning=result.reasoning or worker.reasoning,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=result.exit_code,
        terminal_reason=terminal_reason,
        failure_phase=result.failure_phase,
        failure_kind=result.failure_kind,
        session_id=result.session_id,
        selected=selected,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _env_override_names(env: dict[str, str] | None) -> tuple[str, ...]:
    """Resolved override names for provenance: key names only, never values."""

    if not env:
        return ()
    return tuple(sorted(key[: -len("_REF")] if key.endswith("_REF") else key for key in env))


def _env_endpoint_host(env: dict[str, str] | None) -> str | None:
    """Every distinct endpoint host the overrides point at, comma-joined.

    A seat normally declares one base URL; recording all of them keeps the
    provenance honest when a table carries more than one instead of letting
    key order pick a winner.
    """

    if not env:
        return None
    hosts: list[str] = []
    for key in sorted(env):
        base_url: str | None = None
        if key.endswith("_BASE_URL"):
            base_url = env[key]
        elif key.endswith("_BASE_URL_REF"):
            base_url = os.environ.get(env[key])
        if not base_url:
            continue
        host = urlparse(base_url).hostname or base_url
        if host not in hosts:
            hosts.append(host)
    return ",".join(hosts) if hosts else None


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
    failure_phase: str | None = None
    failure_kind: str | None = None
    transport_warning: dict[str, object] | None = None
    env_overrides: tuple[str, ...] = ()
    endpoint_host: str | None = None
    attempts: tuple[WorkerAttempt, ...] = ()


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
    on_stage_start: Callable[[int, tuple[str, ...]], None] | None = None,
    on_interrupt: Callable[[], None] | None = None,
    process_registry: proc.ProcessRegistry | None = None,
) -> list[WorkerResult]:
    """Dispatch staged assignments while keeping transport policy in one module."""

    process_registry = process_registry or proc.ProcessRegistry()

    def run_direct_agent(*args: Any, **kwargs: Any) -> agents.AgentResult:
        runner = agents.run_agent
        parameters = inspect.signature(runner).parameters.values()
        accepts_registry = any(
            parameter.name == "process_registry" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if not accepts_registry:
            kwargs.pop("process_registry", None)
        return runner(*args, **kwargs)

    def cancel_active_work(futures: dict[Any, int]) -> None:
        for future in futures:
            future.cancel()
        process_registry.cancel()
        if control_registry is not None:
            try:
                control_registry.interrupt()
            except Exception:
                pass
        if appserver is not None:
            close = getattr(appserver, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

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
        preflight = _cloudflare_preflight_failure(agent, assignment)
        if preflight is not None:
            return preflight
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

        def invoke(
            selected_agent: Agent,
            selected_prompt: str,
            *,
            resume_session_id: str | None = None,
        ) -> agents.AgentResult:
            assert selected_agent.cli is not None
            cli_ref = selected_agent.cli
            if selected_agent.transport == "acpx":
                from . import acpx_adapter

                return acpx_adapter.run_cursor(
                    selected_prompt,
                    cwd=cwd or Path.cwd(),
                    timeout=timeout_for(selected_agent, roster),
                    model=selected_agent.model or "",
                    version=selected_agent.transport_version or "",
                    read_only=effective_read_only,
                    writable_worktree=authorized_writable_worktree,
                    process_registry=process_registry,
                )
            if resume_session_id is not None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout_for(selected_agent, roster),
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    model=selected_agent.model,
                    reasoning=selected_agent.reasoning,
                    env=dict(selected_agent.env) if selected_agent.env is not None else None,
                    resume_session_id=resume_session_id,
                    process_registry=process_registry,
                )
            if selected_agent.env is not None:
                # Env seats always dispatch through the direct CLI path. The
                # app-server session cannot apply per-seat env safely.
                env_kwargs: dict[str, Any] = {}
                if sandbox is not None:
                    env_kwargs["sandbox"] = sandbox
                if selected_agent.model is not None:
                    env_kwargs["model"] = selected_agent.model
                if selected_agent.reasoning is not None:
                    env_kwargs["reasoning"] = selected_agent.reasoning
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout_for(selected_agent, roster),
                    cwd=cwd,
                    read_only=effective_read_only,
                    env=dict(selected_agent.env),
                    process_registry=process_registry,
                    **env_kwargs,
                )
            if selected_agent.cli == "codex" and appserver is not None:
                on_event = event_writer(events_dir, selected_agent.name, verbose=verbose)
                return run_appserver_worker(
                    appserver,
                    selected_agent,
                    selected_agent.name,
                    selected_prompt,
                    timeout=timeout_for(selected_agent, roster),
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    registry=control_registry,
                    on_event=on_event,
                )

            timeout = timeout_for(selected_agent, roster)
            if sandbox is None and selected_agent.model is None and selected_agent.reasoning is None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    process_registry=process_registry,
                )
            if sandbox is not None and selected_agent.model is None and selected_agent.reasoning is None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    process_registry=process_registry,
                )
            if sandbox is None and selected_agent.model is not None and selected_agent.reasoning is None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    model=selected_agent.model,
                    process_registry=process_registry,
                )
            if sandbox is None and selected_agent.model is None and selected_agent.reasoning is not None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    reasoning=selected_agent.reasoning,
                    process_registry=process_registry,
                )
            if sandbox is not None and selected_agent.model is not None and selected_agent.reasoning is None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    model=selected_agent.model,
                    process_registry=process_registry,
                )
            if sandbox is not None and selected_agent.model is None and selected_agent.reasoning is not None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    reasoning=selected_agent.reasoning,
                    process_registry=process_registry,
                )
            if sandbox is None and selected_agent.model is not None and selected_agent.reasoning is not None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    model=selected_agent.model,
                    reasoning=selected_agent.reasoning,
                    process_registry=process_registry,
                )
            assert sandbox is not None
            assert selected_agent.model is not None
            assert selected_agent.reasoning is not None
            return run_direct_agent(
                cli_ref,
                selected_prompt,
                timeout=timeout,
                cwd=cwd,
                read_only=effective_read_only,
                sandbox=sandbox,
                model=selected_agent.model,
                reasoning=selected_agent.reasoning,
                process_registry=process_registry,
            )

        def finish(
            result: agents.AgentResult,
            terminal_agent: Agent,
            attempts: list[WorkerAttempt] | None = None,
        ) -> WorkerResult:
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text=result.text,
                ok=result.ok,
                detail=result.detail,
                failure_phase=result.failure_phase,
                failure_kind=result.failure_kind,
                transport_warning=result.transport_warning,
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
                env_overrides=(
                    _env_override_names(terminal_agent.env) if result.failure_kind != "env-ref-missing" else ()
                ),
                endpoint_host=(
                    _env_endpoint_host(terminal_agent.env) if result.failure_kind != "env-ref-missing" else None
                ),
                attempts=tuple(attempts or ()),
            )

        initial_started = _attempt_timestamp()
        result = invoke(agent, prompt)
        initial_finished = _attempt_timestamp()
        recovery_candidate = direct and effective_read_only and agent.cli == "grok" and agent.transport == "direct"
        if not recovery_candidate:
            return finish(result, agent)

        attempts = [
            _worker_attempt(
                kind="initial",
                worker=agent,
                task=assignment.task,
                result=result,
                started_at=initial_started,
                finished_at=initial_finished,
                selected=result.ok,
            )
        ]
        if result.ok or not _is_direct_grok_invalid_final(agent, result, direct=direct, read_only=effective_read_only):
            return finish(result, agent, attempts)
        if not result.session_id:
            missing_session = replace(
                result,
                detail="grok invalid-final result did not include the session id required for exact continuation",
                failure_kind="grok-session-missing",
            )
            return finish(missing_session, agent, attempts)

        continuation_started = _attempt_timestamp()
        continuation = invoke(agent, _GROK_CONTINUATION_PROMPT, resume_session_id=result.session_id)
        continuation_finished = _attempt_timestamp()
        attempts.append(
            _worker_attempt(
                kind="continuation",
                worker=agent,
                task=assignment.task,
                result=continuation,
                started_at=continuation_started,
                finished_at=continuation_finished,
                selected=continuation.ok,
            )
        )
        if continuation.ok or not _is_direct_grok_invalid_final(
            agent, continuation, direct=direct, read_only=effective_read_only
        ):
            return finish(continuation, agent, attempts)

        fallback_name = agent.invalid_final_fallback
        if fallback_name is None:
            missing_fallback = replace(
                continuation,
                detail="grok continuation also lacked a structured final; invalid_final_fallback is not configured",
                failure_phase="dispatch",
                failure_kind="grok-fallback-missing",
            )
            return finish(missing_fallback, agent, attempts)

        fallback_agent = roster.agents[fallback_name]
        fallback_cloudflare_detail = agents.cloudflare_ai_gateway_preflight_detail(fallback_agent.model)
        if fallback_cloudflare_detail is not None:
            # Route through finish() so the accumulated grok attempt history and
            # elapsed duration are preserved in the persisted WorkerResult.
            return finish(
                agents.AgentResult(
                    text="",
                    ok=False,
                    detail=fallback_cloudflare_detail,
                    failure_phase="preflight",
                    failure_kind="provider-config",
                ),
                fallback_agent,
                attempts,
            )
        fallback_prompt = build_prompt(
            fallback_agent,
            assignment,
            prior_results=prior_results,
            read_only=read_only,
            direct=direct,
            code_graph=code_graph,
            drift_impact=drift_impact,
            evidence=evidence,
        )
        fallback_started = _attempt_timestamp()
        fallback_result = invoke(fallback_agent, fallback_prompt)
        fallback_finished = _attempt_timestamp()
        attempts.append(
            _worker_attempt(
                kind="fallback",
                worker=fallback_agent,
                task=assignment.task,
                result=fallback_result,
                started_at=fallback_started,
                finished_at=fallback_finished,
                selected=fallback_result.ok,
            )
        )
        return finish(fallback_result, fallback_agent, attempts)

    if not assignments:
        return []

    all_results: list[WorkerResult] = []
    for stage in sorted({assignment.stage for assignment in assignments}):
        stage_assignments = [assignment for assignment in assignments if assignment.stage == stage]
        if on_stage_start is not None:
            on_stage_start(stage, tuple(assignment.worker for assignment in stage_assignments))
        stage_results_by_index: dict[int, WorkerResult] = {}
        prior_results = list(all_results)
        executor = ThreadPoolExecutor(max_workers=min(roster.max_workers, len(stage_assignments)))
        future_to_index = {}
        try:
            for index, assignment in enumerate(stage_assignments):
                future_to_index[executor.submit(run_one, assignment, prior_results)] = index
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
        except KeyboardInterrupt:
            try:
                if on_interrupt is not None:
                    on_interrupt()
            finally:
                cancel_active_work(future_to_index)
                executor.shutdown(wait=True, cancel_futures=True)
            raise
        except BaseException:
            cancel_active_work(future_to_index)
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        all_results.extend(stage_results_by_index[index] for index in range(len(stage_assignments)))
    return all_results
