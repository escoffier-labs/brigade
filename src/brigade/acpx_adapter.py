"""One-shot Cursor ACP transport through a user-installed acpx executable."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import proc
from .agents import AgentResult
from .result_integrity import validate_final_output

SUPPORTED_VERSION = "0.12.0"


def _timeout_arg(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def build_argv(
    *,
    prompt: str,
    cwd: Path,
    timeout: float,
    model: str,
    read_only: bool,
    writable_worktree: bool,
) -> list[str]:
    if not read_only and not writable_worktree:
        raise ValueError("writable acpx Cursor runs require a Brigade-created writable worktree")
    permission = "--approve-reads" if read_only else "--approve-all"
    argv = [
        "acpx",
        "--cwd",
        str(cwd.expanduser().resolve()),
        "--format",
        "json",
        "--json-strict",
        "--no-terminal",
        "--timeout",
        _timeout_arg(float(timeout)),
        "--model",
        model,
        permission,
    ]
    if read_only:
        argv.extend(["--non-interactive-permissions", "fail"])
    argv.extend(["--agent", "cursor-agent acp", "exec", prompt])
    return argv


def installed_version() -> tuple[str | None, str]:
    result = proc.run(["acpx", "--version"], timeout=10.0)
    if result.code != 0:
        return None, result.stderr.strip() or result.stdout.strip() or f"exit {result.code}"
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
    if match is None:
        return None, "acpx --version did not report a semantic version"
    return match.group(1), ""


def _objects(stdout: str) -> tuple[list[dict[str, Any]] | None, str]:
    messages: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            return None, f"invalid ACP NDJSON at line {line_number}: {exc.msg}"
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return None, f"invalid ACP NDJSON at line {line_number}: expected JSON-RPC 2.0 object"
        messages.append(message)
    if not messages:
        return None, "empty ACP NDJSON stream"
    return messages, ""


def _update(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("method") != "session/update":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    nested = params.get("update")
    return nested if isinstance(nested, dict) else params


def _model_from(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("effectiveModel", "modelId", "currentModelId", "model"):
        model = value.get(key)
        if isinstance(model, str) and model:
            return model
    for nested in value.values():
        found = _model_from(nested)
        if found is not None:
            return found
    return None


def parse_stream(stdout: str) -> tuple[dict[str, Any] | None, str]:
    messages, error = _objects(stdout)
    if messages is None:
        return None, error
    text_parts: list[str] = []
    protocol_version: int | None = None
    session_id: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    stop_reasons: dict[str, str] = {}
    effective_model: str | None = None
    safe_events: list[dict[str, Any]] = []
    for message in messages:
        result = message.get("result")
        if isinstance(result, dict):
            version = result.get("protocolVersion")
            if isinstance(version, int):
                protocol_version = version
            stop = result.get("stopReason")
            if isinstance(stop, str) and isinstance(message.get("id"), (str, int)):
                stop_reasons[str(message["id"])] = stop
        params = message.get("params")
        if isinstance(params, dict):
            candidate_session = params.get("sessionId")
            if isinstance(candidate_session, str):
                session_id = candidate_session
            if message.get("method") == "session/prompt" and isinstance(message.get("id"), (str, int)):
                request_id = str(message["id"])
        update = _update(message)
        if update is None:
            continue
        kind = update.get("sessionUpdate")
        safe_event: dict[str, Any] = {"type": str(kind or "session_update")}
        status = update.get("status")
        if isinstance(status, str):
            safe_event["status"] = status
        safe_events.append(safe_event)
        effective_model = _model_from(update) or effective_model
        if kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                text_parts.append(content["text"])
    if request_id is not None:
        stop_reason = stop_reasons.get(request_id)
    elif len(stop_reasons) == 1:
        request_id, stop_reason = next(iter(stop_reasons.items()))
    if protocol_version not in (None, 1):
        return None, f"unsupported ACP protocol version: {protocol_version}"
    text = "".join(text_parts).strip()
    if not text:
        return None, "ACP stream contained no final assistant text"
    return {
        "text": text,
        "protocol_version": protocol_version or 1,
        "session_id": session_id,
        "request_id": request_id,
        "stop_reason": stop_reason,
        "effective_model": effective_model,
        "events": safe_events,
    }, ""


def run_cursor(
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str,
    version: str,
    read_only: bool,
    writable_worktree: bool = False,
) -> AgentResult:
    if version != SUPPORTED_VERSION:
        return AgentResult(
            text="",
            ok=False,
            detail=f"unsupported acpx adapter version {version}; reviewed version is {SUPPORTED_VERSION}",
            transport="acpx",
        )
    if proc.which("acpx") is None:
        return AgentResult(text="", ok=False, detail="acpx not installed", transport="acpx")
    if proc.which("cursor-agent") is None:
        return AgentResult(text="", ok=False, detail="cursor-agent not installed", transport="acpx")
    installed, version_error = installed_version()
    if installed != version:
        found = installed or version_error
        return AgentResult(
            text="",
            ok=False,
            detail=f"seat requires acpx {version}; found {found}",
            transport="acpx",
            acpx_version=installed,
        )
    try:
        argv = build_argv(
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            model=model,
            read_only=read_only,
            writable_worktree=writable_worktree,
        )
    except ValueError as exc:
        return AgentResult(text="", ok=False, detail=str(exc), transport="acpx", acpx_version=installed)
    result = proc.run(argv, timeout=timeout + 5.0, cwd=cwd)
    parsed, parse_error = parse_stream(result.stdout)
    if result.code != 0:
        detail = result.stderr.strip() or parse_error or f"acpx exit {result.code}"
        return AgentResult(
            text=parsed["text"] if parsed is not None else "",
            ok=False,
            detail=detail[:200],
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            timed_out=result.code in {3, 124},
            transport="acpx",
            requested_model=model,
            effective_model=parsed.get("effective_model") if parsed is not None else None,
            acpx_version=installed,
        )
    if parsed is None:
        return AgentResult(
            text="",
            ok=False,
            detail=parse_error[:200],
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            transport="acpx",
            requested_model=model,
            acpx_version=installed,
        )
    if parsed["stop_reason"] != "end_turn":
        stop_reason = parsed["stop_reason"] or "missing"
        return AgentResult(
            text=parsed["text"],
            ok=False,
            detail=f"ACP stream ended without a final completion (stopReason={stop_reason})",
            failure_phase="output-validation",
            failure_kind="non-final-stop",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            transport="acpx",
            requested_model=model,
            effective_model=parsed["effective_model"],
            stop_reason=parsed["stop_reason"],
            protocol_version=parsed["protocol_version"],
            session_id=parsed["session_id"],
            request_id=parsed["request_id"],
            acpx_version=installed,
            safe_events=tuple(parsed["events"]),
        )
    output_failure = validate_final_output(parsed["text"])
    if output_failure is not None:
        return AgentResult(
            text=parsed["text"],
            ok=False,
            detail=output_failure.detail,
            failure_phase="output-validation",
            failure_kind=output_failure.kind,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            transport="acpx",
            requested_model=model,
            effective_model=parsed["effective_model"],
            stop_reason=parsed["stop_reason"],
            protocol_version=parsed["protocol_version"],
            session_id=parsed["session_id"],
            request_id=parsed["request_id"],
            acpx_version=installed,
            safe_events=tuple(parsed["events"]),
        )
    return AgentResult(
        text=parsed["text"],
        ok=True,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.code,
        transport="acpx",
        requested_model=model,
        effective_model=parsed["effective_model"],
        stop_reason=parsed["stop_reason"],
        protocol_version=parsed["protocol_version"],
        session_id=parsed["session_id"],
        request_id=parsed["request_id"],
        acpx_version=installed,
        safe_events=tuple(parsed["events"]),
    )
