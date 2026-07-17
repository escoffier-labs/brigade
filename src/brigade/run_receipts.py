"""Typed worker receipt serialization and log persistence."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from . import agents, localio
from .run_transport import Assignment, WorkerResult


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
        payload.append(entry)
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
        if result.stdout is None and result.stderr is None:
            recorded.append(result)
            continue
        logs_dir.mkdir(parents=True, exist_ok=True)
        worker = re.sub(r"[^A-Za-z0-9_.-]+", "-", result.worker).strip("-") or "worker"
        prefix = f"worker-{index:03d}-{worker}"
        stdout_ref = f"logs/{prefix}.stdout.log"
        stderr_ref = f"logs/{prefix}.stderr.log"
        localio.write_text_atomic(output_dir / stdout_ref, result.stdout or "")
        localio.write_text_atomic(output_dir / stderr_ref, result.stderr or "")
        recorded.append(replace(result, stdout_log=stdout_ref, stderr_log=stderr_ref))
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
