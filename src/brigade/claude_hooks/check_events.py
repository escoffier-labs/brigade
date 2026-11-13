"""Silent, bounded capture of checks and Claude session identity."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import agent_identity, observed_checks, work_mode, dirfd
from . import verification_mode

IDENTITY_FIELDS = ("model", "model_source")
AGENT_SESSION_ENV = "BRIGADE_AGENT_SESSION"


def _rt() -> Any:
    from . import runtime

    return runtime


def restore_identity(normalized: dict[str, Any], persisted: Mapping[str, Any]) -> None:
    model = agent_identity.map_identity("claude-code", persisted.get("model"), "unknown")["model"]
    source = persisted.get("model_source")
    if isinstance(persisted.get("model"), str) and source in agent_identity.MODEL_SOURCES:
        normalized.update(model=model, model_source=source)


def claude_identity(payload: Mapping[str, Any], state: Mapping[str, Any]) -> agent_identity.AgentIdentity:
    model, source = agent_identity.env_model()
    if isinstance(state.get("model"), str):
        model, source = state["model"], state.get("model_source", "unknown")
    kind = payload.get("agent_type") or ("subagent" if payload.get("agent_id") else "main")
    mapped = agent_identity.map_identity("claude-code", model, kind)
    return agent_identity.AgentIdentity(**mapped, harness_source="hook-event", model_source=source)


def claude_status(event: str, payload: Mapping[str, Any]) -> tuple[str, int | None]:
    if payload.get("is_interrupt") is True:
        return "unknown", None
    if event == "PostToolUse":
        return "ok", None
    error = payload.get("error")
    match = re.match(r"Exit code ([0-9]{1,9})(?:\s|$)", error) if isinstance(error, str) else None
    return ("failed", int(match[1])) if match else ("unknown", None)


def _on_event(
    event: str, payload: dict[str, Any], target: Path, session_id: str, state: dict[str, Any], pin: Path | None = None
) -> None:
    observed_checks.check_capture_deadline()
    rt = _rt()
    rt._enforce_hook_stdin_shape(payload)
    if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
        return
    target = target.resolve()
    if event in {"SessionStart", "PostModelSwitch"}:
        if work_mode.resolve_mode(target) == "off":
            return
        observed_checks.check_capture_deadline()
        field = "model" if event == "SessionStart" else "to_model"
        model = agent_identity.map_identity("claude-code", payload.get(field), "main")["model"]
        if isinstance(payload.get(field), str):
            state.update(
                model=model, model_source="hook-session-start" if event == "SessionStart" else "hook-model-switch"
            )
            rt._write_session_state_preserving_latch(
                target, session_id, state, log_target=target, patch_fields=IDENTITY_FIELDS
            )
        if event == "SessionStart":
            env_file = os.environ.get("CLAUDE_ENV_FILE", "")
            if env_file and Path(env_file).is_absolute():
                observed_checks.check_capture_deadline()
                flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NONBLOCK", 0)
                fd = dirfd.open_file_nofollow(Path(env_file), flags, 0o600)
                try:
                    if stat.S_ISREG(os.fstat(fd).st_mode):
                        observed_checks.check_capture_deadline()
                        os.lseek(fd, 0, os.SEEK_END)
                        os.write(fd, f"export {AGENT_SESSION_ENV}={rt._session_fingerprint(session_id)}\n".encode())
                finally:
                    os.close(fd)
        return
    if event not in {"PostToolUse", "PostToolUseFailure"} or payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or len(command) > 65536 or not rt.is_raw_verification(command):
        return
    status, code = claude_status(event, payload)
    duration = payload.get("duration_ms")
    duration = duration if type(duration) is int and duration >= 0 else None
    for governed, mode in verification_mode.governing_modes(command, payload.get("cwd"), target):
        if mode == "observe":
            # The pin and home policy apply to every destination, as they do for write tracking.
            if governed != target and rt.effective_hook_target({"cwd": str(governed)}, pin=pin) != governed:
                continue
            observed_checks.check_capture_deadline()
            identity = claude_identity(payload, state)
            observed_checks.check_capture_deadline()
            observed_checks.append_check(
                governed,
                observed_checks.build_check(
                    target=governed,
                    command=command,
                    status=status,
                    exit_code=code,
                    duration_ms=duration,
                    identity=identity,
                    fingerprint_tree=False,
                    session_fingerprint=session_id,
                ),
            )


def failure_followup(
    payload: dict[str, Any], target: Path, session_id: str, state: dict[str, Any]
) -> dict[str, Any] | None:
    rt = _rt()
    raw_tool_input = payload.get("tool_input")
    tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
    command = tool_input.get("command")
    verifier = rt.is_raw_verification(command) or rt._is_routed_verify(command)
    if (
        payload.get("tool_name") == "Bash"
        and verifier
        and verification_mode.verification_enforced(command, payload.get("cwd"), target)
    ):
        from .. import outcome_cmd

        capture_id = outcome_cmd.resolve_capture_artifact_id(
            rt._parse_capture_flag(command),
            state.get("exercised_artifact_id") if isinstance(state.get("exercised_artifact_id"), str) else None,
        )
        return rt._additional_context(
            "PostToolUseFailure",
            "",
            target=target,
            session_id=session_id,
            records=[
                "The failed or rejected verification must remain recorded in Brigade before retrying. "
                "Inspect the receipt, fix the cause, then rerun through "
                f"`brigade work verify run --capture {capture_id}`.",
            ],
        )
    return None


def after_handling(event: str, payload: dict[str, Any], *, pin: Path | None) -> None:
    """Run once after all existing primary and secondary event handling."""
    try:
        with observed_checks.capture_budget(1.0 if event in {"SessionStart", "PostModelSwitch"} else 0.25):
            rt = _rt()
            target = rt.effective_hook_target(payload, pin=pin)
            observed_checks.check_capture_deadline()
            session_id = payload.get("session_id")
            if target is not None and isinstance(session_id, str) and session_id:
                state = rt.read_session_state(target, session_id)
                observed_checks.check_capture_deadline()
                _on_event(event, payload, target, session_id, state, pin)
    except Exception:
        pass
