"""Typed worker receipt serialization and log persistence."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import agents, localio, message_envelope, proc
from .acpx_adapter import is_safe_model_id, sanitize_diagnostic, sanitize_events
from .run_transport import Assignment, WorkerAttempt, WorkerResult
from .worklore_validate import contains_controls, contains_credential, contains_private_path
from .worker_failure import normalized_failure


def _bound_capture_text(value: str | None) -> str | None:
    if value is None:
        return None
    return proc.bound_text(value, proc.MAX_CAPTURE_BYTES)


def _bound_message_text(value: str) -> str:
    return message_envelope.truncate_utf8(value, message_envelope.MESSAGE_WRAP_MAX_BYTES)


def _bound_detail(value: str) -> str:
    return proc.bound_text(value, proc.MAX_CAPTURE_BYTES)


def _receipt_detail(value: str, transport: str) -> str:
    if transport == "acpx":
        return proc.bound_text(sanitize_diagnostic(value, limit=proc.MAX_CAPTURE_BYTES), proc.MAX_CAPTURE_BYTES)
    return _bound_detail(value)


def _receipt_capture(value: str | None, transport: str) -> str | None:
    bounded = _bound_capture_text(value)
    if bounded is not None and transport == "acpx":
        safe = sanitize_diagnostic(bounded, limit=proc.MAX_CAPTURE_BYTES)
        return proc.bound_text(bounded if safe == bounded.strip() else safe, proc.MAX_CAPTURE_BYTES)
    return bounded


def _receipt_label(value: str | int | None, transport: str, *, limit: int = 128) -> str | int | None:
    if not isinstance(value, str) or transport != "acpx":
        return value
    safe = sanitize_diagnostic(value, limit=limit)
    return value if safe == value.strip() else safe


_ALLOWED_METADATA_PROVENANCE: dict[str, set[tuple[str, str, str | None]]] = {
    "requested_model": {
        ("declared", "launch-argument", "mutable-alias-not-backend-identity"),
        ("declared", "launch-argument", "launched-argument-not-backend-observation"),
        ("declared", "configured-request", "execution-not-started"),
        ("unknown", "configured-request", "execution-not-started"),
        ("unknown", "configured-request", "requested-model-not-recorded"),
        ("unknown", "launch-argument", "requested-model-not-recorded"),
    },
    "effective_model": {
        ("unknown", "no-backend-observation", "effective-model-not-reported"),
        ("observed", "acp-config-option-update", "backend-revision-unavailable"),
        ("observed", "acp-session-new-response", "backend-revision-unavailable"),
        ("observed", "acp-set-config-option-response", "backend-revision-unavailable"),
        ("unknown", "acp-config-option-update", "effective-model-not-reported"),
        ("unknown", "acp-session-new-response", "effective-model-not-reported"),
        ("unknown", "acp-set-config-option-response", "effective-model-not-reported"),
    },
    "acpx_version": {
        ("observed", "acpx-version-output", None),
        ("unknown", "acpx-version-output", "acpx-version-not-reported"),
    },
}
_METADATA_EVIDENCE_KEYS = {"source", "locator", "digest", "unknown_reason"}
_SAFE_ACPX_VERSION = re.compile(r"\d+\.\d+\.\d+")
_SENSITIVE_MODEL_LABEL = re.compile(r"(?i)\b(?:bearer|token|secret|password|api[ _-]?key)\b")


def _safe_display_model_label(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value.strip() != value
        or contains_controls(value)
        or contains_private_path(value)
        or contains_credential(value)
        or _SENSITIVE_MODEL_LABEL.search(value)
    ):
        return None
    return value


def _safe_metadata_label(key: str, value: object, *, strict_model_id: bool = False) -> str | None:
    if key in {"requested_model", "effective_model", "model"}:
        safe_label = _safe_display_model_label(value)
        if safe_label is None or (strict_model_id and not is_safe_model_id(safe_label)):
            return None
        return safe_label
    if key == "acpx_version" and isinstance(value, str) and len(value) <= 128 and _SAFE_ACPX_VERSION.fullmatch(value):
        return value
    return None


def _safe_metadata_provenance(
    value: object, *, available_labels: dict[str, str | None] | None = None
) -> dict[str, object] | None:
    """Copy the closed provenance vocabulary, dropping arbitrary adapter data."""
    if not isinstance(value, dict):
        return None
    observed_at = value.get("observed_at")
    if (
        not isinstance(observed_at, str)
        or len(observed_at) > 64
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            observed_at,
        )
    ):
        return None
    try:
        parsed_observed_at = datetime.fromisoformat(
            observed_at[:-1] + "+00:00" if observed_at.endswith("Z") else observed_at
        )
    except ValueError:
        return None
    if parsed_observed_at.tzinfo is None or parsed_observed_at.utcoffset() is None:
        return None
    fields = value.get("fields")
    if not isinstance(fields, dict):
        return None
    safe_fields: dict[str, object] = {}
    for name, evidence in fields.items():
        allowed_claims = _ALLOWED_METADATA_PROVENANCE.get(name) if isinstance(name, str) else None
        if allowed_claims is None:
            continue
        if not isinstance(evidence, dict) or set(evidence) != _METADATA_EVIDENCE_KEYS:
            return None
        source = evidence.get("source")
        locator = evidence.get("locator")
        digest = evidence.get("digest")
        unknown_reason = evidence.get("unknown_reason")
        if not isinstance(source, str) or not isinstance(locator, str):
            return None
        if unknown_reason is not None and not isinstance(unknown_reason, str):
            return None
        if available_labels is not None and source != "unknown" and available_labels.get(name) is None:
            source = "unknown"
            unknown_reason = {
                "requested_model": "requested-model-not-recorded",
                "effective_model": "effective-model-not-reported",
                "acpx_version": "acpx-version-not-reported",
            }[name]
        if (source, locator, unknown_reason) not in allowed_claims:
            return None
        if digest is not None:
            return None
        safe_fields[name] = {
            "source": source,
            "locator": locator,
            "digest": None,
            "unknown_reason": unknown_reason,
        }
    if not safe_fields:
        return None
    return {"observed_at": observed_at, "fields": safe_fields}


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
        safe_metadata_labels = {
            name: _safe_metadata_label(
                name,
                value,
                strict_model_id=name == "effective_model" and result.transport == "acpx",
            )
            for name, value in (
                ("requested_model", result.requested_model),
                ("effective_model", result.effective_model),
                ("acpx_version", result.acpx_version),
            )
        }
        entry: dict[str, object] = {
            "worker": result.worker,
            "task": result.task,
            "ok": result.ok,
            "detail": _receipt_detail(result.detail, result.transport),
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
                detail=_receipt_detail(result.detail, result.transport),
                timed_out=result.timed_out,
                status=result.status,
            )
            if failure is not None:
                entry["failure"] = failure.payload()
        if result.transport_warning is not None:
            entry["transport_warning"] = dict(result.transport_warning)
        if result.cloud_environment is not None:
            entry["cloud_environment"] = dict(result.cloud_environment)
        if result.metadata_provenance is not None:
            metadata_provenance = _safe_metadata_provenance(
                result.metadata_provenance, available_labels=safe_metadata_labels
            )
            if metadata_provenance is not None:
                entry["metadata_provenance"] = metadata_provenance
        if result.output_truncated:
            entry["output_truncated"] = True
            entry["output_bytes"] = result.output_bytes
            entry["output_cap_bytes"] = result.output_cap_bytes
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
            ("session_id", _receipt_label(result.session_id, result.transport)),
            ("request_id", _receipt_label(result.request_id, result.transport)),
            ("acpx_version", result.acpx_version),
        ):
            if value is not None:
                if key in {"requested_model", "effective_model", "acpx_version"}:
                    value = safe_metadata_labels[key]
                    if value is None:
                        continue
                elif key in {"reasoning", "stop_reason"}:
                    value = _receipt_label(value, result.transport)
                entry[key] = value
        if result.safe_events:
            events = sanitize_events(result.safe_events) if result.transport == "acpx" else list(result.safe_events)
            if events:
                entry["events"] = events
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
        "reasoning": attempt.reasoning,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "exit_code": attempt.exit_code,
        "terminal_reason": _receipt_label(attempt.terminal_reason, attempt.transport),
        "failure_phase": attempt.failure_phase,
        "failure_kind": attempt.failure_kind,
        "session_id": _receipt_label(attempt.session_id, attempt.transport),
        "selected": attempt.selected,
    }
    if attempt.model is not None:
        model = _safe_metadata_label("model", attempt.model, strict_model_id=attempt.transport == "acpx")
        if model is not None:
            payload["model"] = model
    if attempt.stdout_log is not None:
        payload["stdout_log"] = attempt.stdout_log
    if attempt.stderr_log is not None:
        payload["stderr_log"] = attempt.stderr_log
    if not attempt.ok or attempt.failure_phase is not None or attempt.failure_kind is not None:
        failure = normalized_failure(
            failure_phase=attempt.failure_phase,
            failure_kind=attempt.failure_kind,
            detail=_receipt_detail(attempt.detail, attempt.transport),
            timed_out=attempt.timed_out,
            status="",
            attempt=attempt_number,
        )
        if failure is not None:
            payload["failure"] = failure.payload()
    return payload


def agent_result_payload(result: agents.AgentResult) -> dict[str, object]:
    safe_metadata_labels = {
        name: _safe_metadata_label(
            name,
            value,
            strict_model_id=name == "effective_model" and result.transport == "acpx",
        )
        for name, value in (
            ("requested_model", result.requested_model),
            ("effective_model", result.effective_model),
            ("acpx_version", result.acpx_version),
        )
    }
    payload: dict[str, object] = {
        "ok": result.ok,
        "detail": _receipt_detail(result.detail, result.transport),
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
        ("session_id", _receipt_label(result.session_id, result.transport)),
        ("request_id", _receipt_label(result.request_id, result.transport)),
        ("acpx_version", result.acpx_version),
    ):
        if value is not None:
            if key in {"requested_model", "effective_model", "acpx_version"}:
                value = safe_metadata_labels[key]
                if value is None:
                    continue
            elif key in {"reasoning", "stop_reason"}:
                value = _receipt_label(value, result.transport)
            payload[key] = value
    if result.metadata_provenance is not None:
        metadata_provenance = _safe_metadata_provenance(
            result.metadata_provenance, available_labels=safe_metadata_labels
        )
        if metadata_provenance is not None:
            payload["metadata_provenance"] = metadata_provenance
    if result.safe_events:
        events = sanitize_events(result.safe_events) if result.transport == "acpx" else list(result.safe_events)
        if events:
            payload["events"] = events
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
        metadata_provenance=result.metadata_provenance,
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
            bounded_stdout = _receipt_capture(attempt.stdout, attempt.transport) or ""
            bounded_stderr = _receipt_capture(attempt.stderr, attempt.transport) or ""
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
            detail=_receipt_detail(result.detail, result.transport),
            stdout=_receipt_capture(result.stdout, result.transport),
            stderr=_receipt_capture(result.stderr, result.transport),
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
        detail=_receipt_detail(result.detail, result.transport),
        stdout=_receipt_capture(result.stdout, result.transport),
        stderr=_receipt_capture(result.stderr, result.transport),
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
