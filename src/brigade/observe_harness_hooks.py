"""Optional, silent capture after existing harness hook handling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from . import agent_identity, observed_checks, work_mode
from .claude_hooks import runtime, verification_mode
from .wiring import resolve_wired_target

CODEX_HOOK_CONTEXT = "codex-observe"
CODEX_HOOK_COMMAND = "brigade work presence-hook --harness codex --stdin --context codex-observe"
CODEX_POST_TOOL_MATCHER = "^(Bash|apply_patch)$"


def cursor_identity(payload: Mapping[str, Any]) -> agent_identity.AgentIdentity:
    model = payload.get("model_id") or payload.get("model")
    return _identity("cursor", model)


def _identity(harness: str, model: Any) -> agent_identity.AgentIdentity:
    mapped = agent_identity.map_identity(harness, model, "unknown")
    return agent_identity.AgentIdentity(
        **mapped, harness_source="hook-event", model_source="hook-event" if mapped["model"] != "unknown" else "unknown"
    )


def codex_identity(payload: Mapping[str, Any]) -> agent_identity.AgentIdentity:
    return _identity("codex", payload.get("model"))


def cursor_status(event: str, payload: Mapping[str, Any]) -> tuple[str, int | None] | None:
    if event == "postToolUseFailure":
        failure = payload.get("failure_type")
        if not isinstance(failure, str) or failure == "permission_denied":
            return None
        if payload.get("is_interrupt") is True:
            return "unknown", None
        return ("failed" if failure == "error" else "unknown"), None
    if payload.get("is_interrupt") is True:
        return "unknown", None
    output = payload.get("tool_output")
    try:
        result = json.loads(output) if isinstance(output, str) else None
        runtime._enforce_hook_stdin_shape(result)
    except (ValueError, RecursionError, runtime.HookDegraded):
        result = None
    code = result.get("exitCode") if isinstance(result, dict) else None
    if type(code) is int and 0 <= code <= 2147483647:
        return ("ok" if code == 0 else "failed"), code
    return "unknown", None


def _allowed(target: Path, harness: str, pin: Path | None) -> bool:
    target = target.resolve()
    if runtime.is_operator_home(target):
        return False
    if pin is not None and not target.is_relative_to(pin.expanduser().resolve()):
        return False
    return resolve_wired_target(str(target), harness=harness) == target


def _capture(harness: str, payload: dict[str, Any], target: Path | None, pin: Path | None) -> None:
    observed_checks.check_capture_deadline()
    runtime._enforce_hook_stdin_shape(payload)
    cursor = harness == "cursor"
    if cursor:
        from .cursor_hook_compat import recording_available

        if target is None or not recording_available(target):
            return
    event = payload.get("hook_event_name")
    if event not in ({"postToolUse", "postToolUseFailure"} if cursor else {"PostToolUse"}):
        return
    if any(key in payload and not isinstance(payload[key], str) for key in ("conversation_id", "session_id")):
        return
    session = payload.get("conversation_id" if cursor else "session_id")
    if not isinstance(session, str) or not session or len(session) > 4096:
        return
    cwd = payload.get("cwd")
    if cwd is None and cursor and target is not None:
        cwd = str(target)
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        return
    target = target or resolve_wired_target(cwd, harness=harness)
    if target is None or not _allowed(target, harness, pin):
        return
    observed_checks.check_capture_deadline()
    if not cursor and payload.get("tool_name") == "apply_patch":
        if work_mode.is_observe(target):
            from .codex_observe_hooks import record_write

            record_write(target, session)
        return
    if payload.get("tool_name") != ("Shell" if cursor else "Bash"):
        return
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or len(command) > 65536 or not runtime.is_raw_verification(command):
        return
    status = cursor_status(str(event), payload) if cursor else ("unknown", None)
    if status is None:
        return
    identity = cursor_identity(payload) if cursor else codex_identity(payload)
    for governed, mode in verification_mode.governing_modes(command, cwd, target, harness=harness):
        observed_checks.check_capture_deadline()
        if mode != "observe" or not _allowed(governed, harness, pin):
            continue
        observed_checks.append_check(
            governed,
            observed_checks.build_check(
                target=governed,
                command=command,
                status=status[0],
                exit_code=status[1],
                identity=identity,
                session_fingerprint=session,
                fingerprint_tree=False,
                duration_ms=payload.get("duration") if cursor else None,
            ),
        )


def after_handling(
    harness: str, payload: dict[str, Any] | None, target: Path | None = None, *, pin: Path | None = None
) -> None:
    try:
        with observed_checks.capture_budget(0.25):
            if isinstance(payload, dict):
                _capture(harness, payload, target, pin)
    except Exception:
        pass


def capture_cursor_event(event: str, payload: dict[str, Any], target: Path, *, pin: Path | None = None) -> None:
    after_handling("cursor", {**payload, "hook_event_name": event}, target, pin=pin)


def run_codex_hook(*, pin: Path | None = None) -> None:
    from .fleet_session_presence import _read_bounded_json_object

    try:
        payload = _read_bounded_json_object()
    except Exception:
        payload = None
    # Flush the harness response before optional work, including on malformed input.
    print("{}", flush=True)
    after_handling("codex", payload, pin=pin)
