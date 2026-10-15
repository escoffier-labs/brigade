"""One-shot Cursor ACP transport through a user-installed acpx executable."""

from __future__ import annotations

import inspect
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import proc
from .agents import AgentResult
from .result_integrity import validate_final_output
from .worklore_validate import contains_credential, contains_private_path

SUPPORTED_VERSION = "0.12.0"
CURSOR_AUTH_TIMEOUT_SECONDS = 10.0
CURSOR_AUTH_RECOVERY = "run `cursor-agent login` once, then verify with `cursor-agent status`"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_AUTH_IDENTITY = re.compile(r"(?i)(\blogged in as)\s+[^\r\n]+")
_SENSITIVE_VALUE = re.compile(r"(?i)\b(token|secret|password|api[_ -]?key)\b(?:\s*[:=]\s*|\s+)[^\s,;]+")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[^\s,;]+")
_MODEL_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}"
    r"(?:\[(?:[A-Za-z][A-Za-z0-9._-]{0,31}=[A-Za-z0-9._:+/-]{1,64}"
    r"(?:,[A-Za-z][A-Za-z0-9._-]{0,31}=[A-Za-z0-9._:+/-]{1,64}){0,7})?\])?"
)
_SAFE_EVENT_TYPES = frozenset(
    {
        "agent_message_chunk",
        "agent_thought_chunk",
        "available_commands_update",
        "config_option_update",
        "current_mode_update",
        "plan",
        "provider_auth",
        "session_info_update",
        "tool_call",
        "tool_call_update",
        "usage_update",
        "user_message_chunk",
    }
)
_SAFE_PROVIDER_AUTH_STATUSES = frozenset({"authenticated", "unauthenticated", "unavailable", "unrecognized"})
_SAFE_TOOL_CALL_STATUSES = frozenset({"pending", "in_progress", "completed", "failed"})
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


def sanitize_diagnostic(text: str, *, limit: int = 2000) -> str:
    safe = _ANSI_ESCAPE.sub("", text).strip()
    safe = _AUTH_IDENTITY.sub(r"\1 <redacted>", safe)
    safe = _EMAIL.sub("<redacted>", safe)
    safe = _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}=<redacted>", safe)
    safe = _BEARER.sub(lambda match: f"{match.group(1)} <redacted>", safe)
    if contains_credential(safe) or contains_private_path(safe):
        safe = "<redacted diagnostic>"
    return safe[:limit]


def sanitize_events(events: object) -> list[dict[str, str]]:
    """Retain only closed ACP event labels and statuses in receipts."""
    if not isinstance(events, (list, tuple)):
        return []
    safe: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in _SAFE_EVENT_TYPES:
            continue
        clean: dict[str, str] = {"type": kind}
        status = event.get("status")
        safe_statuses = (
            _SAFE_PROVIDER_AUTH_STATUSES
            if kind == "provider_auth"
            else _SAFE_TOOL_CALL_STATUSES
            if kind in {"tool_call", "tool_call_update"}
            else frozenset()
        )
        if isinstance(status, str) and status in safe_statuses:
            clean["status"] = status
        detail = event.get("detail")
        if kind == "provider_auth" and isinstance(detail, str):
            clean["detail"] = sanitize_diagnostic(detail)
        safe.append(clean)
    return safe


def _diagnostic_line(stdout: str, stderr: str) -> str:
    text = stderr or stdout
    return " ".join(text.split())[:500] or "no diagnostic output"


def _status_payload(stdout: str) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, sanitize_diagnostic(stdout)
    if not isinstance(payload, dict):
        return None, sanitize_diagnostic(stdout)
    safe_payload = {key: value for key in _SAFE_STATUS_KEYS if isinstance((value := payload.get(key)), (bool, str))}
    safe_stdout = json.dumps(safe_payload, sort_keys=True, separators=(",", ":"))
    return payload, sanitize_diagnostic(safe_stdout)


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
    stderr = sanitize_diagnostic(result.stderr)
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
        has_result = "result" in message
        has_error = "error" in message
        if has_result or has_error:
            response_id = message.get("id")
            valid_response_id = (
                (has_error and response_id is None)
                or isinstance(response_id, str)
                or (isinstance(response_id, int) and not isinstance(response_id, bool))
            )
            if has_result == has_error or "method" in message or not valid_response_id:
                return None, f"invalid ACP NDJSON at line {line_number}: malformed JSON-RPC response"
            if has_error:
                error_object = message["error"]
                if (
                    not isinstance(error_object, dict)
                    or not isinstance(error_object.get("code"), int)
                    or isinstance(error_object.get("code"), bool)
                    or not isinstance(error_object.get("message"), str)
                ):
                    return None, f"invalid ACP NDJSON at line {line_number}: malformed JSON-RPC response"
        messages.append(message)
    if not messages:
        return None, "empty ACP NDJSON stream"
    return messages, ""


def _complete_retained_frames(stdout: str) -> str:
    """Return complete NDJSON lines from the retained head of an overflowed stream."""
    marker_at = stdout.find(proc.TRUNCATION_MARKER)
    head = stdout if marker_at < 0 else stdout[:marker_at]
    last_line_end = head.rfind("\n")
    return head[: last_line_end + 1] if last_line_end >= 0 else ""


def _update(message: dict[str, Any]) -> dict[str, Any] | None:
    if message.get("method") != "session/update":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    nested = params.get("update")
    return nested if isinstance(nested, dict) else params


def _model_from_config_options(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    primary_options = [option for option in value if isinstance(option, dict) and option.get("id") == "model"]
    if len(primary_options) == 1:
        model_option = primary_options[0]
    elif primary_options:
        return None
    else:
        category_options = [
            option for option in value if isinstance(option, dict) and option.get("category") == "model"
        ]
        if len(category_options) != 1:
            return None
        model_option = category_options[0]
    model = model_option.get("currentValue")
    return model if is_safe_model_id(model) else None


def _model_from(update: dict[str, Any]) -> str | None:
    if update.get("sessionUpdate") != "config_option_update":
        return None
    if "modelId" in update:
        model = update["modelId"]
        return model if is_safe_model_id(model) else None
    return _model_from_config_options(update.get("configOptions"))


def is_safe_model_id(value: object) -> bool:
    """Accept bounded ACP model identifiers and reject common credential shapes."""
    return (
        isinstance(value, str)
        and len(value) <= 128
        and _MODEL_ID.fullmatch(value) is not None
        and not contains_credential(value)
    )


def parse_stream(stdout: str | None, *, allow_empty_text: bool = False) -> tuple[dict[str, Any] | None, str]:
    messages, error = _objects(stdout)
    if messages is None:
        return None, error
    text_parts: list[str] = []
    protocol_version: int | None = None
    session_id: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    active_prompt_id: tuple[str, str | int] | None = None
    client_request_types: dict[tuple[str, str | int], str] = {}
    outstanding_agent_request_ids: set[tuple[str, str | int]] = set()
    effective_model: str | None = None
    safe_events: list[dict[str, Any]] = []
    prompt_completed = False
    late_permission: dict[str, object] | None = None
    stream_finalized = False
    has_structured_jsonrpc_error = False
    structured_error_detail: str | None = None
    permission_error_detail: str | None = None
    ambiguous_prompt_error: dict[str, Any] | None = None
    effective_model_locator: str | None = None
    for message in messages:
        method = message.get("method")
        message_id = message.get("id")
        message_id_key: tuple[str, str | int] | None
        if isinstance(message_id, str):
            message_id_key = ("string", message_id)
        elif isinstance(message_id, int) and not isinstance(message_id, bool):
            message_id_key = ("integer", message_id)
        else:
            message_id_key = None
        client_request_methods = {"initialize", "session/new", "session/prompt", "session/set_config_option"}
        if method == "session/prompt" and active_prompt_id is not None:
            return None, "malformed ACP stream: overlapping session/prompt requests"
        if method in client_request_methods and message_id_key is not None:
            if message_id_key in client_request_types:
                return None, "malformed ACP stream: duplicate outstanding JSON-RPC request id"
            client_request_types[message_id_key] = str(method)
        is_response = "result" in message or "error" in message
        if isinstance(method, str) and method not in client_request_methods and message_id_key is not None:
            outstanding_agent_request_ids.add(message_id_key)
        result = message.get("result")
        client_request_type = client_request_types.get(message_id_key) if message_id_key is not None else None
        client_response_shape = isinstance(result, dict) and (
            (client_request_type == "initialize" and "protocolVersion" in result)
            or (client_request_type == "session/new" and "sessionId" in result)
            or (client_request_type == "session/prompt" and "stopReason" in result)
            or (client_request_type == "session/set_config_option" and "configOptions" in result)
        )
        rpc_error = message.get("error")
        ambiguous_prompt_error_response = (
            is_response
            and isinstance(rpc_error, dict)
            and message_id_key is not None
            and message_id_key == active_prompt_id
            and client_request_type == "session/prompt"
            and message_id_key in outstanding_agent_request_ids
        )
        agent_request_response = (
            is_response
            and message_id_key is not None
            and message_id_key in outstanding_agent_request_ids
            and not client_response_shape
            and not ambiguous_prompt_error_response
        )
        matched_prompt_request = (
            is_response
            and not agent_request_response
            and not ambiguous_prompt_error_response
            and client_request_type == "session/prompt"
            and message_id_key == active_prompt_id
        )
        if agent_request_response and message_id_key is not None:
            outstanding_agent_request_ids.discard(message_id_key)
        if matched_prompt_request and message_id_key is not None:
            active_prompt_id = None
        matched_config_request = (
            is_response
            and not agent_request_response
            and client_request_type in {"session/new", "session/set_config_option"}
        )
        response_locator = (
            "acp-session-new-response"
            if client_request_type == "session/new"
            else "acp-set-config-option-response"
            if client_request_type == "session/set_config_option"
            else None
        )
        matched_initialize_request = is_response and not agent_request_response and client_request_type == "initialize"
        if (
            is_response
            and message_id_key is not None
            and not agent_request_response
            and not ambiguous_prompt_error_response
        ):
            client_request_types.pop(message_id_key, None)
        if isinstance(result, dict):
            if "protocolVersion" in result:
                version = result["protocolVersion"]
                if not matched_initialize_request:
                    return None, "malformed ACP stream: protocolVersion outside initialize response"
                if type(version) is not int:
                    return None, "malformed ACP stream: malformed ACP protocol version"
                protocol_version = version
            if "error" not in message and "configOptions" in result and matched_config_request:
                effective_model = _model_from_config_options(result["configOptions"])
                if effective_model is not None:
                    effective_model_locator = response_locator
            stop = result.get("stopReason")
            if matched_prompt_request and isinstance(stop, str):
                stop_reason = stop
                prompt_completed = stop == "end_turn"
                if prompt_completed:
                    ambiguous_prompt_error = None
        params = message.get("params")
        if isinstance(params, dict):
            candidate_session = params.get("sessionId")
            if isinstance(candidate_session, str):
                session_id = candidate_session
            if message.get("method") == "session/prompt" and isinstance(message.get("id"), (str, int)):
                text_parts = []
                stop_reason = None
                prompt_completed = False
                late_permission = None
                stream_finalized = False
                has_structured_jsonrpc_error = False
                structured_error_detail = None
                permission_error_detail = None
                ambiguous_prompt_error = None
                request_id = str(message["id"])
                if message_id_key is not None:
                    active_prompt_id = message_id_key
        update = _update(message)
        if update is not None:
            kind = update.get("sessionUpdate")
            safe_event: dict[str, Any] = {"type": kind} if isinstance(kind, str) else {}
            status = update.get("status")
            if isinstance(status, str):
                safe_event["status"] = status
            safe_events.extend(sanitize_events([safe_event]))
            if kind == "config_option_update" and ("modelId" in update or "configOptions" in update):
                effective_model = _model_from(update)
                if effective_model is not None:
                    effective_model_locator = "acp-config-option-update"
            if not stream_finalized and kind == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                    text_parts.append(content["text"])
        if ambiguous_prompt_error_response and isinstance(rpc_error, dict):
            if ambiguous_prompt_error is None:
                ambiguous_prompt_error = rpc_error
        elif isinstance(rpc_error, dict) and not agent_request_response:
            has_structured_jsonrpc_error = True
            error_message = rpc_error.get("message")
            if structured_error_detail is None and isinstance(error_message, str):
                structured_error_detail = sanitize_diagnostic(error_message, limit=200)
        permission_error = (
            None
            if agent_request_response or ambiguous_prompt_error_response
            else _permission_prompt_diagnostic(rpc_error)
        )
        if permission_error is not None:
            if prompt_completed:
                if "".join(text_parts).strip():
                    late_permission = permission_error
                stream_finalized = True
            else:
                if not allow_empty_text:
                    return None, str(permission_error["detail"])
                permission_error_detail = str(permission_error["detail"])
                break
    if protocol_version not in (None, 1):
        return None, f"unsupported ACP protocol version: {protocol_version}"
    if ambiguous_prompt_error is not None and not prompt_completed:
        has_structured_jsonrpc_error = True
        error_message = ambiguous_prompt_error.get("message")
        if structured_error_detail is None and isinstance(error_message, str):
            structured_error_detail = sanitize_diagnostic(error_message, limit=200)
        permission_error = _permission_prompt_diagnostic(ambiguous_prompt_error)
        if permission_error is not None:
            if not allow_empty_text:
                return None, str(permission_error["detail"])
            permission_error_detail = str(permission_error["detail"])
    text = "".join(text_parts).strip()
    if not text and not allow_empty_text:
        return None, "ACP stream contained no final assistant text"
    parsed: dict[str, Any] = {
        "text": text,
        "protocol_version": protocol_version or 1,
        "session_id": session_id,
        "request_id": request_id,
        "stop_reason": stop_reason,
        "effective_model": effective_model,
        "effective_model_locator": effective_model_locator,
        "events": safe_events,
        "prompt_completed": prompt_completed,
        "has_structured_jsonrpc_error": has_structured_jsonrpc_error,
    }
    if structured_error_detail:
        parsed["structured_error_detail"] = structured_error_detail
    if late_permission is not None:
        parsed["late_permission"] = late_permission
    if permission_error_detail is not None:
        return parsed, permission_error_detail
    return parsed, "" if text else structured_error_detail or "ACP stream contained no final assistant text"


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
    env: dict[str, str] | None = None,
) -> AgentResult:
    if version != SUPPORTED_VERSION:
        return AgentResult(
            text="",
            ok=False,
            detail=f"unsupported acpx adapter version {version}; reviewed version is {SUPPORTED_VERSION}",
            failure_phase="preflight",
            failure_kind="version-mismatch",
            transport="acpx",
            requested_model=model,
        )
    if proc.which("acpx") is None:
        return AgentResult(
            text="",
            ok=False,
            detail="acpx not installed",
            failure_phase="preflight",
            failure_kind="missing-executable",
            transport="acpx",
            requested_model=model,
        )
    if proc.which("cursor-agent") is None:
        return AgentResult(
            text="",
            ok=False,
            detail="cursor-agent not installed",
            failure_phase="preflight",
            failure_kind="missing-executable",
            transport="acpx",
            requested_model=model,
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
            requested_model=model,
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
            requested_model=model,
            acpx_version=installed,
            safe_events=(auth_event,),
        )
    child_env = None
    if env is not None:
        child_env = dict(os.environ)
        child_env.update(env)
    result = proc.run(
        argv,
        timeout=timeout + 5.0,
        cwd=cwd,
        env=child_env,
        process_registry=process_registry,
    )
    if result.output_limit_exceeded:
        detail = (result.stderr.strip() or f"combined output exceeded {proc.MAX_CAPTURE_BYTES} byte limit")[:200]
        retained, _ = parse_stream(_complete_retained_frames(result.stdout), allow_empty_text=True)
        retained_metadata = retained or {}
        return AgentResult(
            text="",
            ok=False,
            detail=detail,
            failure_phase="harness",
            failure_kind="output-limit",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            timed_out=result.code == 124,
            transport="acpx",
            requested_model=model,
            effective_model=retained_metadata.get("effective_model"),
            effective_model_locator=retained_metadata.get("effective_model_locator"),
            protocol_version=retained_metadata.get("protocol_version"),
            session_id=retained_metadata.get("session_id"),
            request_id=retained_metadata.get("request_id"),
            acpx_version=installed,
            safe_events=(auth_event, *retained_metadata.get("events", [])),
        )
    if result.decode_failed:
        detail = (result.stderr.strip() or result.decode_failure_detail)[:200]
        decoded_stdout, _parse_error = parse_stream(result.stdout, allow_empty_text=True)
        parsed_metadata = decoded_stdout or {}
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
            effective_model=parsed_metadata.get("effective_model"),
            effective_model_locator=parsed_metadata.get("effective_model_locator"),
            stop_reason=parsed_metadata.get("stop_reason"),
            protocol_version=parsed_metadata.get("protocol_version"),
            session_id=parsed_metadata.get("session_id"),
            request_id=parsed_metadata.get("request_id"),
            acpx_version=installed,
            safe_events=(auth_event, *parsed_metadata.get("events", [])),
        )
    parsed, parse_error = parse_stream(result.stdout, allow_empty_text=True)
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
                    effective_model_locator=parsed["effective_model_locator"],
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
                effective_model_locator=parsed["effective_model_locator"],
                stop_reason=parsed["stop_reason"],
                protocol_version=parsed["protocol_version"],
                session_id=parsed["session_id"],
                request_id=parsed["request_id"],
                acpx_version=installed,
                safe_events=(auth_event, *parsed["events"]),
                transport_warning=late_warning,
            )
        structured_error = parsed.get("structured_error_detail") if parsed is not None else None
        raw_detail = result.stderr.strip() or parse_error or structured_error or f"acpx exit {result.code}"
        permission_denied = bool(_PERMISSION_FAILURE.search(raw_detail))
        detail = sanitize_diagnostic(raw_detail, limit=200)
        timed_out = result.code in {3, 124}
        provider_startup = parsed is None or not parsed["text"]
        return AgentResult(
            text=parsed["text"] if parsed is not None else "",
            ok=False,
            detail=detail,
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
            effective_model_locator=parsed.get("effective_model_locator") if parsed is not None else None,
            stop_reason=parsed.get("stop_reason") if parsed is not None else None,
            protocol_version=parsed.get("protocol_version") if parsed is not None else None,
            session_id=parsed.get("session_id") if parsed is not None else None,
            request_id=parsed.get("request_id") if parsed is not None else None,
            acpx_version=installed,
            safe_events=(auth_event, *(parsed["events"] if parsed is not None else [])),
        )
    if parsed is None or not parsed["text"]:
        empty_final = parse_error == "ACP stream contained no final assistant text"
        return AgentResult(
            text=parsed["text"] if parsed is not None else "",
            ok=False,
            detail=parse_error[:200],
            failure_phase="output-validation",
            failure_kind="empty-output" if empty_final else "malformed-transport",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            transport="acpx",
            requested_model=model,
            effective_model=parsed.get("effective_model") if parsed is not None else None,
            effective_model_locator=parsed.get("effective_model_locator") if parsed is not None else None,
            stop_reason=parsed.get("stop_reason") if parsed is not None else None,
            protocol_version=parsed.get("protocol_version") if parsed is not None else None,
            session_id=parsed.get("session_id") if parsed is not None else None,
            request_id=parsed.get("request_id") if parsed is not None else None,
            acpx_version=installed,
            safe_events=(auth_event, *(parsed["events"] if parsed is not None else [])),
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
            effective_model_locator=parsed["effective_model_locator"],
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
            effective_model_locator=parsed["effective_model_locator"],
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
        effective_model_locator=parsed["effective_model_locator"],
        stop_reason=parsed["stop_reason"],
        protocol_version=parsed["protocol_version"],
        session_id=parsed["session_id"],
        request_id=parsed["request_id"],
        acpx_version=installed,
        safe_events=(auth_event, *parsed["events"]),
    )
