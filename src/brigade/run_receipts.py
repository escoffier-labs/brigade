"""Typed worker receipt serialization and log persistence."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from . import agents, localio
from .run_transport import Assignment, WorkerAttempt, WorkerResult


def assignment_payload(assignments: list[Assignment]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for assignment in assignments:
        entry: dict[str, object] = {
            "stage": assignment.stage,
            "worker": assignment.worker,
            "task": assignment.task,
        }
        if assignment.covers:
            entry["covers"] = list(assignment.covers)
        payload.append(entry)
    return payload


def worker_payload(results: list[WorkerResult]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for result in results:
        entry: dict[str, object] = {
            "worker": result.worker,
            "task": result.task,
            "ok": result.ok,
            "detail": result.detail,
            "text": result.text,
        }
        if result.failure_phase is not None:
            entry["failure_phase"] = result.failure_phase
        if result.failure_kind is not None:
            entry["failure_kind"] = result.failure_kind
        if result.transport_warning is not None:
            entry["transport_warning"] = dict(result.transport_warning)
        if result.thread_id is not None:
            entry["thread_id"] = result.thread_id
            entry["status"] = result.status
        if result.exit_code is not None:
            entry["exit_code"] = result.exit_code
            entry["timed_out"] = result.timed_out
        if result.stdout_log is not None:
            entry["stdout_log"] = result.stdout_log
        if result.stderr_log is not None:
            entry["stderr_log"] = result.stderr_log
        if result.duration_seconds is not None:
            entry["duration_seconds"] = result.duration_seconds
        entry["transport"] = result.transport
        for key, value in (
            ("requested_model", result.requested_model),
            ("effective_model", result.effective_model),
            ("reasoning", result.reasoning),
            ("stop_reason", result.stop_reason),
            ("protocol_version", result.protocol_version),
            ("session_id", result.session_id),
            ("request_id", result.request_id),
            ("acpx_version", result.acpx_version),
        ):
            if value is not None:
                entry[key] = value
        if result.safe_events:
            entry["events"] = list(result.safe_events)
        if result.env_overrides:
            entry["env_overrides"] = list(result.env_overrides)
        if result.endpoint_host is not None:
            entry["endpoint_host"] = result.endpoint_host
        if result.attempts:
            entry["attempts"] = [_attempt_payload(attempt) for attempt in result.attempts]
        payload.append(entry)
    return payload


def _attempt_payload(attempt: WorkerAttempt) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": attempt.kind,
        "worker": attempt.worker,
        "task": attempt.task,
        "transport": attempt.transport,
        "model": attempt.model,
        "reasoning": attempt.reasoning,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "exit_code": attempt.exit_code,
        "terminal_reason": attempt.terminal_reason,
        "failure_phase": attempt.failure_phase,
        "failure_kind": attempt.failure_kind,
        "session_id": attempt.session_id,
        "selected": attempt.selected,
    }
    if attempt.stdout_log is not None:
        payload["stdout_log"] = attempt.stdout_log
    if attempt.stderr_log is not None:
        payload["stderr_log"] = attempt.stderr_log
    return payload


def agent_result_payload(result: agents.AgentResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": result.ok,
        "detail": result.detail,
        "text": result.text,
    }
    if result.failure_phase is not None:
        payload["failure_phase"] = result.failure_phase
    if result.failure_kind is not None:
        payload["failure_kind"] = result.failure_kind
    if result.transport_warning is not None:
        payload["transport_warning"] = dict(result.transport_warning)
    if result.exit_code is not None:
        payload["exit_code"] = result.exit_code
        payload["timed_out"] = result.timed_out
    if result.stdout_log is not None:
        payload["stdout_log"] = result.stdout_log
    if result.stderr_log is not None:
        payload["stderr_log"] = result.stderr_log
    if result.duration_seconds is not None:
        payload["duration_seconds"] = result.duration_seconds
    payload["transport"] = result.transport
    for key, value in (
        ("requested_model", result.requested_model),
        ("effective_model", result.effective_model),
        ("reasoning", result.reasoning),
        ("stop_reason", result.stop_reason),
        ("protocol_version", result.protocol_version),
        ("session_id", result.session_id),
        ("request_id", result.request_id),
        ("acpx_version", result.acpx_version),
    ):
        if value is not None:
            payload[key] = value
    if result.safe_events:
        payload["events"] = list(result.safe_events)
    return payload


def agent_result_from_worker(result: WorkerResult) -> agents.AgentResult:
    return agents.AgentResult(
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
        stdout_log=result.stdout_log,
        stderr_log=result.stderr_log,
        duration_seconds=result.duration_seconds,
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


def write_worker_logs(output_dir: Path, results: list[WorkerResult]) -> list[WorkerResult]:
    logs_dir = output_dir / "logs"
    recorded: list[WorkerResult] = []
    for index, result in enumerate(results, start=1):
        worker = re.sub(r"[^A-Za-z0-9_.-]+", "-", result.worker).strip("-") or "worker"
        recorded_attempts: list[WorkerAttempt] = []
        for attempt_index, attempt in enumerate(result.attempts, start=1):
            if attempt.stdout is None and attempt.stderr is None:
                recorded_attempts.append(attempt)
                continue
            logs_dir.mkdir(parents=True, exist_ok=True)
            kind = re.sub(r"[^A-Za-z0-9_.-]+", "-", attempt.kind).strip("-") or "attempt"
            prefix = f"worker-{index:03d}-{worker}-attempt-{attempt_index:03d}-{kind}"
            stdout_ref = f"logs/{prefix}.stdout.log"
            stderr_ref = f"logs/{prefix}.stderr.log"
            localio.write_text_atomic(output_dir / stdout_ref, attempt.stdout or "")
            localio.write_text_atomic(output_dir / stderr_ref, attempt.stderr or "")
            recorded_attempts.append(replace(attempt, stdout_log=stdout_ref, stderr_log=stderr_ref))

        recorded_result = replace(result, attempts=tuple(recorded_attempts))
        if result.stdout is not None or result.stderr is not None:
            logs_dir.mkdir(parents=True, exist_ok=True)
            prefix = f"worker-{index:03d}-{worker}"
            stdout_ref = f"logs/{prefix}.stdout.log"
            stderr_ref = f"logs/{prefix}.stderr.log"
            localio.write_text_atomic(output_dir / stdout_ref, result.stdout or "")
            localio.write_text_atomic(output_dir / stderr_ref, result.stderr or "")
            recorded_result = replace(recorded_result, stdout_log=stdout_ref, stderr_log=stderr_ref)
        recorded.append(recorded_result)
    return recorded


def write_agent_logs(output_dir: Path, label: str, result: agents.AgentResult) -> agents.AgentResult:
    if result.stdout is None and result.stderr is None:
        return result
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_ref = f"logs/{label}.stdout.log"
    stderr_ref = f"logs/{label}.stderr.log"
    localio.write_text_atomic(output_dir / stdout_ref, result.stdout or "")
    localio.write_text_atomic(output_dir / stderr_ref, result.stderr or "")
    return replace(result, stdout_log=stdout_ref, stderr_log=stderr_ref)
