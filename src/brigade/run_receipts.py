"""Typed worker receipt serialization and log persistence."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from . import agents, localio, message_envelope, proc
from .run_transport import Assignment, WorkerAttempt, WorkerResult
from .worker_failure import normalized_failure


def _bound_capture_text(value: str | None) -> str | None:
    if value is None:
        return None
    return proc.bound_text(value, proc.MAX_CAPTURE_BYTES)


def _bound_message_text(value: str) -> str:
    return message_envelope.truncate_utf8(value, message_envelope.MESSAGE_WRAP_MAX_BYTES)


def _bound_detail(value: str) -> str:
    return proc.bound_text(value, proc.MAX_CAPTURE_BYTES)


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
        if assignment.selected_skill_ids:
            entry["selected_skill_ids"] = list(assignment.selected_skill_ids)
        if assignment.domain:
            entry["domain"] = assignment.domain
        if assignment.capabilities:
            entry["capabilities"] = list(assignment.capabilities)
        if assignment.max_risk_class:
            entry["max_risk_class"] = assignment.max_risk_class
        if assignment.admissible_tool_ids:
            entry["admissible_tool_ids"] = list(assignment.admissible_tool_ids)
        payload.append(entry)
    return payload


def worker_payload_one(result: WorkerResult) -> dict[str, object]:
    return worker_payload([result])[0]


def worker_payload(results: list[WorkerResult]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for result in results:
        entry: dict[str, object] = {
            "worker": result.worker,
            "task": result.task,
            "ok": result.ok,
            "detail": _bound_detail(result.detail),
            "text": _bound_message_text(result.text),
        }
        if result.failure_phase is not None:
            entry["failure_phase"] = result.failure_phase
        if result.failure_kind is not None:
            entry["failure_kind"] = result.failure_kind
        if not result.ok:
            failure = normalized_failure(
                failure_phase=result.failure_phase,
                failure_kind=result.failure_kind,
                detail=result.detail,
                timed_out=result.timed_out,
                status=result.status,
            )
            if failure is not None:
                entry["failure"] = failure.payload()
        if result.transport_warning is not None:
            entry["transport_warning"] = dict(result.transport_warning)
        if result.cloud_environment is not None:
            entry["cloud_environment"] = dict(result.cloud_environment)
        if result.output_truncated:
            entry["output_truncated"] = True
        if result.thread_id is not None:
            entry["thread_id"] = result.thread_id
            entry["status"] = result.status
        if result.exit_code is not None:
            entry["exit_code"] = result.exit_code
        if result.exit_code is not None or result.timed_out:
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
            entry["attempts"] = [
                _attempt_payload(attempt, attempt_number=index)
                for index, attempt in enumerate(result.attempts, start=1)
            ]
        if result.provenance is not None:
            entry["provenance"] = dict(result.provenance)
        payload.append(entry)
    return payload


def _attempt_payload(attempt: WorkerAttempt, *, attempt_number: int) -> dict[str, object]:
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
    if not attempt.ok or attempt.failure_phase is not None or attempt.failure_kind is not None:
        failure = normalized_failure(
            failure_phase=attempt.failure_phase,
            failure_kind=attempt.failure_kind,
            detail=attempt.detail,
            timed_out=attempt.timed_out,
            status="",
            attempt=attempt_number,
        )
        if failure is not None:
            payload["failure"] = failure.payload()
    return payload


def agent_result_payload(result: agents.AgentResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": result.ok,
        "detail": _bound_detail(result.detail),
        "text": _bound_message_text(result.text),
    }
    if result.failure_phase is not None:
        payload["failure_phase"] = result.failure_phase
    if result.failure_kind is not None:
        payload["failure_kind"] = result.failure_kind
    if result.transport_warning is not None:
        payload["transport_warning"] = dict(result.transport_warning)
    if result.cloud_environment is not None:
        payload["cloud_environment"] = dict(result.cloud_environment)
    if result.exit_code is not None:
        payload["exit_code"] = result.exit_code
    if result.exit_code is not None or result.timed_out:
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
        cloud_environment=result.cloud_environment,
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
            bounded_stdout = _bound_capture_text(attempt.stdout) or ""
            bounded_stderr = _bound_capture_text(attempt.stderr) or ""
            localio.write_text_atomic(output_dir / stdout_ref, bounded_stdout)
            localio.write_text_atomic(output_dir / stderr_ref, bounded_stderr)
            recorded_attempts.append(
                replace(
                    attempt,
                    stdout=bounded_stdout,
                    stderr=bounded_stderr,
                    stdout_log=stdout_ref,
                    stderr_log=stderr_ref,
                )
            )

        recorded_result = replace(
            result,
            text=_bound_message_text(result.text),
            detail=_bound_detail(result.detail),
            stdout=_bound_capture_text(result.stdout),
            stderr=_bound_capture_text(result.stderr),
            attempts=tuple(recorded_attempts),
        )
        if result.stdout is not None or result.stderr is not None:
            logs_dir.mkdir(parents=True, exist_ok=True)
            prefix = f"worker-{index:03d}-{worker}"
            stdout_ref = f"logs/{prefix}.stdout.log"
            stderr_ref = f"logs/{prefix}.stderr.log"
            localio.write_text_atomic(output_dir / stdout_ref, recorded_result.stdout or "")
            localio.write_text_atomic(output_dir / stderr_ref, recorded_result.stderr or "")
            recorded_result = replace(recorded_result, stdout_log=stdout_ref, stderr_log=stderr_ref)
        recorded.append(recorded_result)
    return recorded


def write_agent_logs(output_dir: Path, label: str, result: agents.AgentResult) -> agents.AgentResult:
    bounded = replace(
        result,
        text=_bound_message_text(result.text),
        detail=_bound_detail(result.detail),
        stdout=_bound_capture_text(result.stdout),
        stderr=_bound_capture_text(result.stderr),
    )
    if bounded.stdout is None and bounded.stderr is None:
        return bounded
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_ref = f"logs/{label}.stdout.log"
    stderr_ref = f"logs/{label}.stderr.log"
    localio.write_text_atomic(output_dir / stdout_ref, bounded.stdout or "")
    localio.write_text_atomic(output_dir / stderr_ref, bounded.stderr or "")
    return replace(bounded, stdout_log=stdout_ref, stderr_log=stderr_ref)
