"""One-shot Cursor ACP transport through a user-installed acpx executable."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import proc
from .agents import AgentResult
from .result_integrity import validate_final_output

SUPPORTED_VERSION = "0.12.0"
CURSOR_AUTH_TIMEOUT_SECONDS = 10.0
CURSOR_AUTH_RECOVERY = "run `cursor-agent login` once, then verify with `cursor-agent status`"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_AUTH_IDENTITY = re.compile(r"(?i)(\blogged in as)\s+[^\r\n]+")
_SENSITIVE_VALUE = re.compile(r"(?i)\b(token|secret|password|api[_ -]?key)\b(?:\s*[:=]\s*|\s+)[^\s,;]+")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[^\s,;]+")
_UNAUTHENTICATED_LINE = re.compile(
    r"^(?:[x✗✘]\s*)?(?:not logged in|not authenticated|unauthenticated|authentication required)[.!]?$",
    re.IGNORECASE,
)
_AUTHENTICATED_LINE = re.compile(
    r"^(?:[✓✔]\s*)?(?:logged in as\s+\S+|authenticated)[.!]?$",
    re.IGNORECASE,
)
_PERMISSION_FAILURE = re.compile(
    r"\b(?:permission denied|permission required|not permitted|auto-denied)\b",
    re.IGNORECASE,
)
_LATE_PERMISSION_CODE = -32072
_LATE_PERMISSION_DETAIL = "PERMISSION_PROMPT_UNAVAILABLE"
_LATE_PERMISSION_MARKER = re.compile(
    r"PERMISSION_PROMPT_UNAVAILABLE|-32072",
    re.IGNORECASE,
)
_SAFE_STATUS_KEYS = ("hasAccessToken", "hasRefreshToken", "isAuthenticated", "status")


@dataclass(frozen=True)
class CursorAuthStatus:
    state: Literal["authenticated", "unauthenticated", "unavailable", "unrecognized"]
    detail: str
    stdout: str
    stderr: str
    exit_code: int


def _safe_diagnostic(text: str, *, limit: int = 2000) -> str:
    safe = _ANSI_ESCAPE.sub("", text).strip()
    safe = _AUTH_IDENTITY.sub(r"\1 <redacted>", safe)
    safe = _EMAIL.sub("<redacted>", safe)
    safe = _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}=<redacted>", safe)
    safe = _BEARER.sub(lambda match: f"{match.group(1)} <redacted>", safe)
    return safe[:limit]


def _diagnostic_line(stdout: str, stderr: str) -> str:
    text = stderr or stdout
    return " ".join(text.split())[:500] or "no diagnostic output"


def _status_payload(stdout: str) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, _safe_diagnostic(stdout)
    if not isinstance(payload, dict):
        return None, _safe_diagnostic(stdout)
    safe_payload = {key: value for key in _SAFE_STATUS_KEYS if isinstance((value := payload.get(key)), (bool, str))}
    safe_stdout = json.dumps(safe_payload, sort_keys=True, separators=(",", ":"))
    return payload, _safe_diagnostic(safe_stdout)


def cursor_auth_status(*, process_registry: proc.ProcessRegistry | None = None) -> CursorAuthStatus:
    """Return a bounded, prompt-free diagnosis for the headless Cursor CLI."""
    if proc.which("cursor-agent") is None:
        return CursorAuthStatus(
            "unavailable",
            "cursor-agent is not installed",
            "",
            "",
            127,
        )
    argv = ["cursor-agent", "status", "--format", "json"]
    if process_registry is None:
        result = proc.run(argv, timeout=CURSOR_AUTH_TIMEOUT_SECONDS)
    else:
        result = proc.run(argv, timeout=CURSOR_AUTH_TIMEOUT_SECONDS, process_registry=process_registry)
    payload, stdout = _status_payload(result.stdout)
    stderr = _safe_diagnostic(result.stderr)
    diagnostic = _diagnostic_line(stdout, stderr)
    authenticated = payload.get("isAuthenticated") if payload is not None else None
    primary_line = next((line.strip() for line in stdout.splitlines() if line.strip()), "")
    if authenticated is False or (payload is None and _UNAUTHENTICATED_LINE.fullmatch(primary_line)):
        return CursorAuthStatus(
            "unauthenticated",
            f"cursor-agent CLI is not logged in; {CURSOR_AUTH_RECOVERY}",
            stdout,
            stderr,
            result.code,
        )
    if result.code != 0:
        return CursorAuthStatus(
            "unavailable",
            f"cursor-agent status failed (exit {result.code}): {diagnostic}",
            stdout,
            stderr,
            result.code,
        )
    if authenticated is True or (payload is None and _AUTHENTICATED_LINE.fullmatch(primary_line)):
        return CursorAuthStatus(
            "authenticated",
            "cursor-agent CLI is authenticated",
            stdout,
            stderr,
            result.code,
        )
    return CursorAuthStatus(
        "unrecognized",
        f"cursor-agent status returned an unrecognized response: {diagnostic}",
        stdout,
        stderr,
        result.code,
    )


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


def installed_version(*, process_registry: proc.ProcessRegistry | None = None) -> tuple[str | None, str]:
    if process_registry is None:
        result = proc.run(["acpx", "--version"], timeout=10.0)
    else:
        result = proc.run(["acpx", "--version"], timeout=10.0, process_registry=process_registry)
    if result.code != 0:
        return None, result.stderr.strip() or result.stdout.strip() or f"exit {result.code}"
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
    if match is None:
        return None, "acpx --version did not report a semantic version"
    return match.group(1), ""


def _call_with_process_registry(function, *, process_registry: proc.ProcessRegistry | None):
    parameters = inspect.signature(function).parameters.values()
    accepts_registry = any(
        parameter.name == "process_registry" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if accepts_registry:
        return function(process_registry=process_registry)
    return function()


def _permission_prompt_diagnostic(error: object) -> dict[str, object] | None:
    if not isinstance(error, dict):
        return None
    if error.get("code") != _LATE_PERMISSION_CODE:
        return None
    return {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": _LATE_PERMISSION_CODE,
        "detail": _LATE_PERMISSION_DETAIL,
    }


def _late_permission_from_stderr(stderr: str, *, prompt_completed: bool) -> dict[str, object] | None:
    if not prompt_completed or not stderr.strip():
        return None
    if not _LATE_PERMISSION_MARKER.search(stderr):
        return None
    return {
        "phase": "post-final",
        "kind": "permission-prompt-unavailable",
        "code": _LATE_PERMISSION_CODE,
        "detail": _LATE_PERMISSION_DETAIL,
    }


def _usable_with_late_permission(parsed: dict[str, Any], warning: dict[str, object]) -> bool:
    return (
        parsed.get("stop_reason") == "end_turn"
        and bool(str(parsed.get("text", "")).strip())
        and warning.get("phase") == "post-final"
        and warning.get("kind") == "permission-prompt-unavailable"
    )


def _late_permission_detail(warning: dict[str, object]) -> str:
    code = warning.get("code")
    code_text = f"code {code}" if isinstance(code, int) else "code -32072"
    base = str(warning.get("detail") or "PERMISSION_PROMPT_UNAVAILABLE")
    return f"late permission prompt unavailable ({code_text}); preserved completed final answer: {base}"[:200]


def _objects(stdout: str | None) -> tuple[list[dict[str, Any]] | None, str]:
    messages: list[dict[str, Any]] = []
    for line_number, line in enumerate((stdout or "").splitlines(), start=1):
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


def parse_stream(stdout: str | None) -> tuple[dict[str, Any] | None, str]:
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
    prompt_completed = False
    late_permission: dict[str, object] | None = None
    stream_finalized = False
    has_structured_jsonrpc_error = False
    for message in messages:
        result = message.get("result")
        if isinstance(result, dict):
            version = result.get("protocolVersion")
            if isinstance(version, int):
                protocol_version = version
            stop = result.get("stopReason")
            if isinstance(stop, str) and isinstance(message.get("id"), (str, int)):
                stop_reasons[str(message["id"])] = stop
                if request_id is not None and str(message["id"]) == request_id and stop == "end_turn":
                    prompt_completed = True
        params = message.get("params")
        if isinstance(params, dict):
            candidate_session = params.get("sessionId")
            if isinstance(candidate_session, str):
                session_id = candidate_session
            if message.get("method") == "session/prompt" and isinstance(message.get("id"), (str, int)):
                request_id = str(message["id"])
        update = _update(message)
        if update is not None:
            kind = update.get("sessionUpdate")
            safe_event: dict[str, Any] = {"type": str(kind or "session_update")}
            status = update.get("status")
            if isinstance(status, str):
                safe_event["status"] = status
            safe_events.append(safe_event)
            effective_model = _model_from(update) or effective_model
            if not stream_finalized and kind == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                    text_parts.append(content["text"])
        rpc_error = message.get("error")
        if isinstance(rpc_error, dict):
            has_structured_jsonrpc_error = True
        permission_error = _permission_prompt_diagnostic(rpc_error)
        if permission_error is not None:
            if prompt_completed:
                if "".join(text_parts).strip():
                    late_permission = permission_error
                stream_finalized = True
            else:
                return None, str(permission_error["detail"])
    if request_id is not None:
        stop_reason = stop_reasons.get(request_id)
    elif len(stop_reasons) == 1:
        request_id, stop_reason = next(iter(stop_reasons.items()))
    if protocol_version not in (None, 1):
        return None, f"unsupported ACP protocol version: {protocol_version}"
    text = "".join(text_parts).strip()
    if not text:
        return None, "ACP stream contained no final assistant text"
    parsed: dict[str, Any] = {
        "text": text,
        "protocol_version": protocol_version or 1,
        "session_id": session_id,
        "request_id": request_id,
        "stop_reason": stop_reason,
        "effective_model": effective_model,
        "events": safe_events,
        "prompt_completed": prompt_completed,
        "has_structured_jsonrpc_error": has_structured_jsonrpc_error,
    }
    if late_permission is not None:
        parsed["late_permission"] = late_permission
    return parsed, ""


def run_cursor(
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    model: str,
    version: str,
    read_only: bool,
    writable_worktree: bool = False,
    process_registry: proc.ProcessRegistry | None = None,
) -> AgentResult:
    if version != SUPPORTED_VERSION:
        return AgentResult(
            text="",
            ok=False,
            detail=f"unsupported acpx adapter version {version}; reviewed version is {SUPPORTED_VERSION}",
            failure_phase="preflight",
            failure_kind="version-mismatch",
            transport="acpx",
        )
    if proc.which("acpx") is None:
        return AgentResult(
            text="",
            ok=False,
            detail="acpx not installed",
            failure_phase="preflight",
            failure_kind="missing-executable",
            transport="acpx",
        )
    if proc.which("cursor-agent") is None:
        return AgentResult(
            text="",
            ok=False,
            detail="cursor-agent not installed",
            failure_phase="preflight",
            failure_kind="missing-executable",
            transport="acpx",
        )
    installed, version_error = _call_with_process_registry(
        installed_version,
        process_registry=process_registry,
    )
    if installed != version:
        found = installed or version_error
        return AgentResult(
            text="",
            ok=False,
            detail=f"seat requires acpx {version}; found {found}",
            failure_phase="preflight",
            failure_kind="version-mismatch",
            transport="acpx",
            acpx_version=installed,
        )
    auth = _call_with_process_registry(
        cursor_auth_status,
        process_registry=process_registry,
    )
    auth_event: dict[str, object] = {
        "type": "provider_auth",
        "status": auth.state,
        "detail": auth.detail,
    }
    if auth.state != "authenticated":
        failure_kind = {
            "unauthenticated": "provider-auth",
            "unavailable": "auth-status-unavailable",
            "unrecognized": "auth-status-unrecognized",
        }.get(auth.state, "auth-status-unrecognized")
        return AgentResult(
            text="",
            ok=False,
            detail=auth.detail,
            failure_phase="preflight",
            failure_kind=failure_kind,
            stdout=auth.stdout,
            stderr=auth.stderr,
            exit_code=auth.exit_code,
            timed_out=auth.exit_code == 124,
            transport="acpx",
            requested_model=model,
            acpx_version=installed,
            safe_events=(auth_event,),
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
        return AgentResult(
            text="",
            ok=False,
            detail=str(exc),
            failure_phase="preflight",
            failure_kind="unsafe-worktree",
            transport="acpx",
            acpx_version=installed,
            safe_events=(auth_event,),
        )
    result = proc.run(
        argv,
        timeout=timeout + 5.0,
        cwd=cwd,
        process_registry=process_registry,
    )
    if result.decode_failed:
        detail = (result.stderr.strip() or result.decode_failure_detail)[:200]
        return AgentResult(
            text="",
            ok=False,
            detail=detail,
            failure_phase="harness",
            failure_kind="decode-failure",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            timed_out=result.code == 124,
            transport="acpx",
            requested_model=model,
            acpx_version=installed,
            safe_events=(auth_event,),
        )
    parsed, parse_error = parse_stream(result.stdout)
    if result.code != 0:
        late_warning: dict[str, object] | None = None
        if parsed is not None:
            candidate = parsed.get("late_permission")
            late_warning = candidate if isinstance(candidate, dict) else None
            if late_warning is None and not parsed.get("has_structured_jsonrpc_error"):
                late_warning = _late_permission_from_stderr(
                    result.stderr,
                    prompt_completed=bool(parsed.get("prompt_completed")),
                )
        if parsed is not None and late_warning is not None and _usable_with_late_permission(parsed, late_warning):
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
                    safe_events=(auth_event, *parsed["events"]),
                    transport_warning=late_warning,
                )
            warning_detail = _late_permission_detail(late_warning)
            return AgentResult(
                text=parsed["text"],
                ok=True,
                detail=warning_detail,
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
                safe_events=(auth_event, *parsed["events"]),
                transport_warning=late_warning,
            )
        detail = result.stderr.strip() or parse_error or f"acpx exit {result.code}"
        timed_out = result.code in {3, 124}
        provider_startup = parsed is None
        permission_denied = bool(_PERMISSION_FAILURE.search(detail))
        return AgentResult(
            text=parsed["text"] if parsed is not None else "",
            ok=False,
            detail=detail[:200],
            failure_phase="inference" if timed_out or not provider_startup else "dispatch",
            failure_kind=(
                "timeout"
                if timed_out
                else "permission-denied"
                if permission_denied
                else "provider-startup"
                if provider_startup
                else "transport-error"
            ),
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            timed_out=timed_out,
            transport="acpx",
            requested_model=model,
            effective_model=parsed.get("effective_model") if parsed is not None else None,
            stop_reason=parsed.get("stop_reason") if parsed is not None else None,
            protocol_version=parsed.get("protocol_version") if parsed is not None else None,
            session_id=parsed.get("session_id") if parsed is not None else None,
            request_id=parsed.get("request_id") if parsed is not None else None,
            acpx_version=installed,
            safe_events=(auth_event, *(parsed["events"] if parsed is not None else [])),
        )
    if parsed is None:
        empty_final = parse_error == "ACP stream contained no final assistant text"
        return AgentResult(
            text="",
            ok=False,
            detail=parse_error[:200],
            failure_phase="output-validation",
            failure_kind="empty-output" if empty_final else "malformed-transport",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            transport="acpx",
            requested_model=model,
            acpx_version=installed,
            safe_events=(auth_event,),
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
            safe_events=(auth_event, *parsed["events"]),
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
            safe_events=(auth_event, *parsed["events"]),
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
        safe_events=(auth_event, *parsed["events"]),
    )
