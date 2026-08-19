"""Typed worker transport dispatch for :mod:`brigade.aboyeur`."""

from __future__ import annotations

import inspect
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from . import agents, message_envelope, proc, receipt_schema, run_budget, run_control, runguard
from .roster import Agent, Roster, is_cli_allowed, timeout_for
from .seat_health_policy import SeatQuarantineState, decide_retry, failure_for_worker_result

# Re-export the stable worker env key so transport tests and callers document
# one name: BRIGADE_RUN_ID (see receipt_schema.BRIGADE_RUN_ID_ENV).
BRIGADE_RUN_ID_ENV = receipt_schema.BRIGADE_RUN_ID_ENV

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
    selected_skill_ids: tuple[str, ...] = ()
    domain: str | None = None
    capabilities: tuple[str, ...] = ()
    max_risk_class: str | None = None
    admissible_tool_ids: tuple[str, ...] = ()


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
    detail: str = ""
    ok: bool = True
    timed_out: bool = False


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
        detail=result.detail,
        ok=result.ok,
        timed_out=result.timed_out,
    )


def _apply_same_seat_retry(
    *,
    assignment: Assignment,
    agent: Agent,
    prompt: str,
    first_result: agents.AgentResult,
    first_started: str,
    first_finished: str,
    invoke: Callable[..., agents.AgentResult],
    finish: Callable[..., WorkerResult],
    quarantine_state: SeatQuarantineState,
    reprobe_seat: Callable[[Agent], bool] | None,
    on_failed_attempt_persisted: Callable[[WorkerResult], None] | None,
) -> WorkerResult:
    """Persist a failed attempt, then re-probe and retry the same seat once.

    Callers must already have confirmed the failure disposition is
    ``same-seat-once`` and recorded that retry on ``quarantine_state``.
    """
    first_attempt = _worker_attempt(
        kind="initial",
        worker=agent,
        task=assignment.task,
        result=first_result,
        started_at=first_started,
        finished_at=first_finished,
        selected=False,
    )
    persisted = finish(first_result, agent, [first_attempt])
    if on_failed_attempt_persisted is not None:
        on_failed_attempt_persisted(persisted)
    failure = failure_for_worker_result(persisted)
    failure_class = failure.failure_class.value if failure is not None else "unclassified"
    if reprobe_seat is not None and not reprobe_seat(agent):
        quarantine_state.quarantine(assignment.worker)
        return replace(
            persisted,
            detail=(
                f"{persisted.detail}; same-seat retry skipped because re-probe reported unhealthy [{failure_class}]"
            ).strip("; "),
        )
    retry_started = _attempt_timestamp()
    retry_result = invoke(agent, prompt)
    retry_finished = _attempt_timestamp()
    attempts = [
        first_attempt,
        _worker_attempt(
            kind="same-seat-once",
            worker=agent,
            task=assignment.task,
            result=retry_result,
            started_at=retry_started,
            finished_at=retry_finished,
            selected=retry_result.ok,
        ),
    ]
    finished = finish(retry_result, agent, attempts)
    if not finished.ok:
        quarantine_state.quarantine(assignment.worker)
    return finished


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
    provenance: dict[str, Any] | None = None


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
        self,
        events_dir: Path | None,
        worker: str,
        *,
        verbose: bool = False,
        workspace: Path | None = None,
        correlation_marker: str | None = None,
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
    fail_fast: bool = True,
    scheduler: str = "waves",
    route_dependencies: dict[str, tuple[str, ...]] | None = None,
    route_held: dict[str, list[str]] | None = None,
    on_stage_start: Callable[[int, tuple[str, ...]], None] | None = None,
    on_interrupt: Callable[[], None] | None = None,
    on_scheduler_resolved: Callable[[str, str | None], None] | None = None,
    on_dispatch_requested: Callable[[Agent], int | None] | None = None,
    on_dispatch_observed: Callable[[Agent, int], None] | None = None,
    on_dispatch_completed: Callable[[Agent, int], None] | None = None,
    on_dispatch_failed: Callable[[Agent, int], None] | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    quarantine_state: SeatQuarantineState | None = None,
    reprobe_seat: Callable[[Agent], bool] | None = None,
    on_failed_attempt_persisted: Callable[[WorkerResult], None] | None = None,
    run_id: str | None = None,
    output_dir: Path | None = None,
) -> list[WorkerResult]:
    """Dispatch staged assignments while keeping transport policy in one module.

    ``on_scheduler_resolved`` receives the scheduler that actually ran and the
    fallback reason, so a receipt can distinguish a real DAG dispatch from a
    silent degrade to waves. Without it the fallback is stderr-only and the run
    record cannot tell the two apart.

    ``run_id`` is the orchestrator run identity. When set, direct/ACPX workers
    receive it as ``BRIGADE_RUN_ID`` through the existing env transport path so
    receipt producers can stamp optional ``producer_run_id`` (#499). Codex
    app-server workers receive the same identity via the AppServer process env
    constructed by ``brigade run`` (run-scoped, not per-seat).

    ``quarantine_state`` / ``reprobe_seat`` implement the #474 same-seat-once
    retry bound: persist the failed attempt, re-probe, then allow at most one
    same-seat retry before quarantining the seat for the rest of the run.
    """

    process_registry = process_registry or proc.ProcessRegistry()
    quarantine_state = quarantine_state or SeatQuarantineState()
    orchestrator_run_id = run_id.strip() if isinstance(run_id, str) and run_id.strip() else None
    if orchestrator_run_id is None and output_dir is not None:
        orchestrator_run_id = output_dir.name
    orchestrator_seat = roster.orchestrator
    worker_producer = "run_transport.dispatch"

    def _with_orchestrator_run_id(env: dict[str, str] | None) -> dict[str, str] | None:
        if orchestrator_run_id is None:
            return env
        merged = dict(env) if env is not None else {}
        merged[BRIGADE_RUN_ID_ENV] = orchestrator_run_id
        return merged

    def run_direct_agent(*args: Any, **kwargs: Any) -> agents.AgentResult:
        runner = agents.run_agent
        parameters = inspect.signature(runner).parameters.values()
        accepts_var_keyword = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        accepts_registry = accepts_var_keyword or any(parameter.name == "process_registry" for parameter in parameters)
        accepts_env = accepts_var_keyword or any(parameter.name == "env" for parameter in parameters)
        if not accepts_registry:
            kwargs.pop("process_registry", None)
        if orchestrator_run_id is not None:
            # Real agents.run_agent accepts env=; legacy fixed-signature test
            # doubles do not. Mirror the process_registry gate so BRIGADE_RUN_ID
            # still reaches production workers without crashing the doubles.
            merged_env = _with_orchestrator_run_id(kwargs.get("env"))
            if accepts_env:
                kwargs["env"] = merged_env
            else:
                kwargs.pop("env", None)
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
        if quarantine_state.is_quarantined(assignment.worker):
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text="",
                ok=False,
                detail=f"seat {assignment.worker} is quarantined for this run",
                failure_phase="dispatch",
                failure_kind="unclassified",
            )
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
        request = message_envelope.emit(
            prompt,
            kind="worker-request",
            producer=worker_producer,
            from_seat=message_envelope.BRIGADE_SEAT,
            to_seat=assignment.worker,
            run_dir=output_dir,
            run_id=orchestrator_run_id,
            assignment_id=message_envelope.assignment_id_for(assignment.worker, assignment.task),
            session_harness=agent.cli,
        )
        if not request.delivered:
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text="",
                ok=False,
                detail=request.reason or "worker-request rejected by provenance gate",
                failure_phase="dispatch",
                failure_kind="unclassified",
                provenance=request.envelope,
            )
        started = time.monotonic()
        effective_read_only = read_only if sandbox_read_only is None else sandbox_read_only
        approval_correlation_marker = secrets.token_urlsafe(32)

        def _invoke_external(
            selected_agent: Agent,
            selected_prompt: str,
            *,
            resume_session_id: str | None = None,
        ) -> agents.AgentResult:
            assert selected_agent.cli is not None
            cli_ref = selected_agent.cli
            seat_process_registry = process_registry.for_seat(selected_agent.name)
            if selected_agent.transport == "acpx":
                from . import acpx_adapter

                acpx_kwargs: dict[str, Any] = {
                    "cwd": cwd or Path.cwd(),
                    "timeout": timeout_for(selected_agent, roster),
                    "model": selected_agent.model or "",
                    "version": selected_agent.transport_version or "",
                    "read_only": effective_read_only,
                    "writable_worktree": authorized_writable_worktree,
                    "process_registry": seat_process_registry,
                }
                worker_env = _with_orchestrator_run_id(
                    dict(selected_agent.env) if selected_agent.env is not None else None
                )
                if worker_env is not None:
                    acpx_kwargs["env"] = worker_env
                return acpx_adapter.run_cursor(selected_prompt, **acpx_kwargs)
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
                    process_registry=seat_process_registry,
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
                    process_registry=seat_process_registry,
                    **env_kwargs,
                )
            if selected_agent.cli == "codex" and appserver is not None:
                approval_prompt = (
                    f"{selected_prompt}\n\n"
                    "Approval correlation rule: prefix every Brigade CLI command in this turn with "
                    f"`BRIGADE_APPROVAL_CORRELATION={approval_correlation_marker}`. "
                    "Do not print or return that marker."
                )
                try:
                    on_event = event_writer(
                        events_dir,
                        selected_agent.name,
                        verbose=verbose,
                        workspace=cwd,
                        correlation_marker=approval_correlation_marker,
                    )
                except TypeError as exc:
                    if "workspace" not in str(exc) and "correlation_marker" not in str(exc):
                        raise
                    on_event = event_writer(events_dir, selected_agent.name, verbose=verbose)
                result = run_appserver_worker(
                    appserver,
                    selected_agent,
                    selected_agent.name,
                    approval_prompt,
                    timeout=timeout_for(selected_agent, roster),
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    registry=control_registry,
                    on_event=on_event,
                )
                return replace(
                    result,
                    text=result.text.replace(approval_correlation_marker, "[redacted]"),
                    detail=result.detail.replace(approval_correlation_marker, "[redacted]"),
                    stdout=(
                        result.stdout.replace(approval_correlation_marker, "[redacted]")
                        if result.stdout is not None
                        else None
                    ),
                    stderr=(
                        result.stderr.replace(approval_correlation_marker, "[redacted]")
                        if result.stderr is not None
                        else None
                    ),
                )

            timeout = timeout_for(selected_agent, roster)
            if sandbox is None and selected_agent.model is None and selected_agent.reasoning is None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    process_registry=seat_process_registry,
                )
            if sandbox is not None and selected_agent.model is None and selected_agent.reasoning is None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    sandbox=sandbox,
                    process_registry=seat_process_registry,
                )
            if sandbox is None and selected_agent.model is not None and selected_agent.reasoning is None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    model=selected_agent.model,
                    process_registry=seat_process_registry,
                )
            if sandbox is None and selected_agent.model is None and selected_agent.reasoning is not None:
                return run_direct_agent(
                    cli_ref,
                    selected_prompt,
                    timeout=timeout,
                    cwd=cwd,
                    read_only=effective_read_only,
                    reasoning=selected_agent.reasoning,
                    process_registry=seat_process_registry,
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
                    process_registry=seat_process_registry,
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
                    process_registry=seat_process_registry,
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
                    process_registry=seat_process_registry,
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
                process_registry=seat_process_registry,
            )

        def invoke(
            selected_agent: Agent,
            selected_prompt: str,
            *,
            resume_session_id: str | None = None,
        ) -> agents.AgentResult:
            """Record transport facts around exactly one real external call."""
            attempt = on_dispatch_requested(selected_agent) if on_dispatch_requested is not None else None
            try:
                result = _invoke_external(selected_agent, selected_prompt, resume_session_id=resume_session_id)
            except BaseException:
                if attempt is not None and on_dispatch_failed is not None:
                    on_dispatch_failed(selected_agent, attempt)
                raise
            if attempt is not None:
                if on_dispatch_observed is not None:
                    on_dispatch_observed(selected_agent, attempt)
                if result.ok:
                    if on_dispatch_completed is not None:
                        on_dispatch_completed(selected_agent, attempt)
                elif on_dispatch_failed is not None:
                    on_dispatch_failed(selected_agent, attempt)
            return result

        def finish(
            result: agents.AgentResult,
            terminal_agent: Agent,
            attempts: list[WorkerAttempt] | None = None,
        ) -> WorkerResult:
            captured = message_envelope.emit(
                result.text,
                kind="worker-result",
                producer=worker_producer,
                from_seat=terminal_agent.name,
                to_seat=orchestrator_seat,
                run_dir=output_dir,
                run_id=orchestrator_run_id,
                assignment_id=message_envelope.assignment_id_for(assignment.worker, assignment.task),
                session_harness=terminal_agent.cli,
            )
            delivered_text = result.text if captured.delivered else ""
            delivered_ok = result.ok if captured.delivered else False
            delivered_detail = result.detail if captured.delivered else (captured.reason or result.detail)
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text=delivered_text,
                ok=delivered_ok,
                detail=delivered_detail,
                failure_phase=result.failure_phase if captured.delivered else "dispatch",
                failure_kind=result.failure_kind if captured.delivered else "unclassified",
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
                provenance=captured.envelope,
            )

        initial_started = _attempt_timestamp()
        result = invoke(agent, prompt)
        initial_finished = _attempt_timestamp()
        recovery_candidate = direct and effective_read_only and agent.cli == "grok" and agent.transport == "direct"
        if not recovery_candidate:
            if result.ok:
                return finish(result, agent)
            finished = finish(result, agent)
            failure = failure_for_worker_result(finished)
            decision = decide_retry(failure, seat=assignment.worker, state=quarantine_state)
            if not decision.should_retry_same_seat:
                return finished
            return _apply_same_seat_retry(
                assignment=assignment,
                agent=agent,
                prompt=prompt,
                first_result=result,
                first_started=initial_started,
                first_finished=initial_finished,
                invoke=invoke,
                finish=finish,
                quarantine_state=quarantine_state,
                reprobe_seat=reprobe_seat,
                on_failed_attempt_persisted=on_failed_attempt_persisted,
            )

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

    if scheduler == "dag":
        placement_error = _dag_placement_error(assignments, route_dependencies)
        if placement_error is None:
            if on_scheduler_resolved is not None:
                on_scheduler_resolved("single-node" if len(assignments) == 1 else "dag", None)
            return _dag_dispatch(
                assignments,
                roster,
                run_one=run_one,
                on_stage_start=on_stage_start,
                on_interrupt=on_interrupt,
                cancel_active_work=cancel_active_work,
                route_dependencies=route_dependencies or {},
                route_held=route_held or {},
            )
        if on_scheduler_resolved is not None:
            on_scheduler_resolved("waves", placement_error)
        print(
            f"warning: dag scheduler: {placement_error}; falling back to wave scheduler",
            file=sys.stderr,
        )
    elif on_scheduler_resolved is not None:
        on_scheduler_resolved("waves", None)

    stage_order = sorted({assignment.stage for assignment in assignments})
    abort_after_stage: int | None = None
    all_results: list[WorkerResult] = []
    for stage in stage_order:
        stage_assignments = [assignment for assignment in assignments if assignment.stage == stage]
        if fail_fast and abort_after_stage is not None:
            all_results.extend(
                WorkerResult(
                    worker=assignment.worker,
                    task=assignment.task,
                    text="",
                    ok=False,
                    status="skipped",
                    detail=f"skipped: stage {abort_after_stage} prerequisite failed",
                )
                for assignment in stage_assignments
            )
            continue
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
                except runguard.RetainRunLockError:
                    raise
                except run_budget.BudgetPolicyError:
                    raise
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
        stage_results = [stage_results_by_index[index] for index in range(len(stage_assignments))]
        all_results.extend(stage_results)
        if fail_fast and any(not result.ok for result in stage_results):
            abort_after_stage = stage
    return all_results


def _dag_placement_error(
    assignments: list[Assignment],
    route_dependencies: dict[str, tuple[str, ...]] | None,
) -> str | None:
    """Reason the DAG scheduler cannot place the plan, or None when it can."""
    if not route_dependencies:
        return "no route dependencies available"
    known = set(route_dependencies)
    for assignment in assignments:
        # Empty covers are an independent DAG root; only nonempty covers must
        # name known route stages.
        if assignment.covers and not set(assignment.covers) <= known:
            return "plan not fully covered"
    return None


def dag_cycle_members(
    assignments: list[Assignment],
    route_dependencies: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """Return workers in mutual-wait components of an assignment plan.

    This uses the same covers-set semantics as the ready queue: a dependency
    covered by the assignment itself is satisfied locally, while a stage with
    multiple coverers needs any one of them to finish.  Nodes that can become
    ready are peeled first so an optional cyclic coverer does not make an
    otherwise runnable plan look cyclic.
    """
    coverers: dict[str, tuple[int, ...]] = {}
    for stage_name in route_dependencies:
        coverers[stage_name] = tuple(i for i, assignment in enumerate(assignments) if stage_name in assignment.covers)

    groups: list[tuple[tuple[int, ...], ...]] = []
    for i, assignment in enumerate(assignments):
        dependencies: dict[str, tuple[int, ...]] = {}
        for stage_name in assignment.covers:
            for dependency in route_dependencies.get(stage_name, ()):
                stage_coverers = coverers.get(dependency, ())
                if i in stage_coverers:
                    continue
                others = tuple(index for index in stage_coverers if index != i)
                if others:
                    dependencies[dependency] = others
        groups.append(tuple(dependencies.values()))

    runnable: set[int] = set()
    changed = True
    while changed:
        changed = False
        for i, prerequisites in enumerate(groups):
            if i not in runnable and all(set(group) & runnable for group in prerequisites):
                runnable.add(i)
                changed = True

    blocked = set(range(len(assignments))) - runnable
    edges = {i: {member for group in groups[i] for member in group if member in blocked} for i in blocked}
    cycle_nodes: set[int] = set()

    # Small plans make a direct reachability test clearer than a full SCC
    # implementation: an edge is cyclic exactly when its target reaches back.
    def reaches(start: int, target: int, seen: set[int]) -> bool:
        if start == target:
            return True
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(next_node, target, seen) for next_node in edges.get(start, ()))

    for source, targets in edges.items():
        for target in targets:
            if reaches(target, source, set()):
                cycle_nodes.update((source, target))
    return tuple(assignments[i].worker for i in sorted(cycle_nodes))


def _dag_dispatch(
    assignments: list[Assignment],
    roster: Roster,
    *,
    run_one: Callable[[Assignment, list[WorkerResult]], WorkerResult],
    on_stage_start: Callable[[int, tuple[str, ...]], None] | None,
    on_interrupt: Callable[[], None] | None,
    cancel_active_work: Callable[[dict[Any, int]], None],
    route_dependencies: dict[str, tuple[str, ...]],
    route_held: dict[str, list[str]],
) -> list[WorkerResult]:
    """Ready-queue scheduler keyed on Assignment.covers over the route DAG.

    Independent branches keep running when a sibling branch fails; transitive
    dependents of a failed/timed-out/held prerequisite become ``skipped``.
    Result order matches the original ``assignments`` order.
    """
    coverers: dict[str, list[int]] = {}
    for i, a in enumerate(assignments):
        for stage_name in a.covers:
            coverers.setdefault(stage_name, []).append(i)
    # One group per depended-on stage: the stage is satisfied when ANY of its
    # coverers succeeds, and dead only when ALL of them have failed. A single
    # flat prerequisite set would let one failed redundant coverer doom
    # dependents whose stage another coverer already satisfied.
    prereq_groups: list[list[tuple[int, ...]]] = []
    for i, a in enumerate(assignments):
        groups: dict[str, tuple[int, ...]] = {}
        for stage_name in a.covers:
            for dep_stage in route_dependencies.get(stage_name, ()):
                dep_coverers = coverers.get(dep_stage, ())
                if i in dep_coverers:
                    # This assignment covers the dependency stage itself, so the edge
                    # is satisfied within its own execution. Without this, every
                    # parallel assignment sharing a composite covers set waits on its
                    # peers and the whole plan deadlocks on the first sweep.
                    continue
                others = tuple(idx for idx in dep_coverers if idx != i)
                if not others:
                    # Nobody else covers it (or only this assignment does):
                    # vacuously satisfied, same leniency as wave mode.
                    continue
                groups[dep_stage] = others
        prereq_groups.append(list(groups.values()))
    held_indices = {i for i, a in enumerate(assignments) if set(a.covers) & set(route_held)}
    results: list[WorkerResult | None] = [None] * len(assignments)
    completed_ok: list[WorkerResult] = []
    submitted: set[int] = set()
    for i in held_indices:
        a = assignments[i]
        stage_name = sorted(set(a.covers) & set(route_held))[0]
        results[i] = WorkerResult(
            worker=a.worker,
            task=a.task,
            text="",
            ok=False,
            status="held",
            detail=f"held: stage {stage_name} awaits {', '.join(route_held[stage_name])}",
        )

    def terminal(i: int) -> bool:
        return results[i] is not None

    def _group_satisfied(group: tuple[int, ...]) -> bool:
        return any((rp := results[p]) is not None and rp.ok for p in group)

    def ready(i: int) -> bool:
        if terminal(i) or i in submitted:
            return False
        return all(_group_satisfied(group) for group in prereq_groups[i])

    def doomed(i: int) -> bool:
        if terminal(i) or i in submitted:
            return False
        for group in prereq_groups[i]:
            if all(results[p] is not None for p in group) and not _group_satisfied(group):
                return True
        return False

    executor = ThreadPoolExecutor(max_workers=roster.max_workers)
    future_to_index: dict[Any, int] = {}
    try:
        while any(r is None for r in results):
            for i, a in enumerate(assignments):
                if doomed(i):
                    results[i] = WorkerResult(
                        worker=a.worker,
                        task=a.task,
                        text="",
                        ok=False,
                        status="skipped",
                        detail="skipped: prerequisite failed",
                    )
            progress = False
            for i, a in enumerate(assignments):
                if ready(i):
                    if on_stage_start is not None:
                        on_stage_start(a.stage, (a.worker,))
                    # prior_results is a submission-time snapshot in completion order, matching wave-mode semantics; later completions are intentionally not visible to already-submitted workers.
                    future_to_index[executor.submit(run_one, a, list(completed_ok))] = i
                    submitted.add(i)
                    progress = True
            pending = {f for f, i in future_to_index.items() if results[i] is None}
            if not pending:
                if not progress and any(r is None for r in results):
                    for i, a in enumerate(assignments):
                        if results[i] is None:
                            results[i] = WorkerResult(
                                worker=a.worker,
                                task=a.task,
                                text="",
                                ok=False,
                                status="skipped",
                                detail="skipped: unresolvable dependency cycle in plan",
                            )
                continue
            done = next(as_completed(pending))
            i = future_to_index[done]
            try:
                finished = done.result()
                results[i] = finished
            except runguard.RetainRunLockError:
                raise
            except run_budget.BudgetPolicyError:
                raise
            except Exception as exc:
                finished = WorkerResult(
                    worker=assignments[i].worker,
                    task=assignments[i].task,
                    text="",
                    ok=False,
                    detail=str(exc)[:200],
                )
                results[i] = finished
            if finished.ok:
                completed_ok.append(finished)
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
    return [r for r in results if r is not None]
