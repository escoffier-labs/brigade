"""Optional, silent capture after existing harness hook handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from . import agent_identity, observed_checks, work_mode
from .claude_hooks import runtime, verification_mode
from .claude_hooks.command_tokens import command_event, event_wired_target as resolve_wired_target

CODEX_HOOK_CONTEXT = "codex-observe"
CODEX_HOOK_COMMAND = "brigade work presence-hook --harness codex --stdin --context codex-observe"
CODEX_POST_TOOL_MATCHER = "^(Bash|apply_patch)$"


def _identity(harness: str, model: Any) -> agent_identity.AgentIdentity:
    mapped = agent_identity.map_identity(harness, model, "unknown")
    return agent_identity.AgentIdentity(
        **mapped, harness_source="hook-event", model_source="hook-event" if mapped["model"] != "unknown" else "unknown"
    )


def codex_identity(payload: Mapping[str, Any]) -> agent_identity.AgentIdentity:
    return _identity("codex", payload.get("model"))


def _allowed(target: Path, harness: str, pin: Path | None) -> bool:
    target = runtime._resolve_command_path(str(target), Path.cwd(), expand_vars=False)
    if runtime.is_operator_home(target):
        return False
    if pin is not None and target != runtime._resolve_command_path(str(pin), Path.cwd(), expand_vars=False):
        return False
    return resolve_wired_target(str(target), harness=harness) == target


def _capture(harness: str, payload: dict[str, Any], target: Path | None, pin: Path | None) -> None:
    observed_checks.check_capture_deadline()
    runtime._enforce_hook_stdin_shape(payload)
    if harness != "codex":
        return
    event = payload.get("hook_event_name")
    if event not in {"PostToolUse", "UserPromptSubmit"}:
        return
    if any(key in payload and not isinstance(payload[key], str) for key in ("session_id",)):
        return
    session = payload.get("session_id")
    if not isinstance(session, str) or not session or len(session) > 4096:
        return
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        return
    target = target or resolve_wired_target(cwd, harness=harness)
    if target is None or not _allowed(target, harness, pin):
        return
    from . import lesson_activity

    if event == "UserPromptSubmit":
        lesson_activity.on_event(target, event, payload)
        return

    try:
        if payload.get("tool_name") == "apply_patch":
            if work_mode.is_observe(target):
                from .codex_observe_hooks import record_write

                record_write(target, session)
            return
        if payload.get("tool_name") != "Bash":
            return
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        verdict = runtime._recognize(command)
        if not isinstance(command, str) or verdict is runtime.Recognition.NO:
            return
        unclassified = verdict is runtime.Recognition.UNDECIDED
        identity = codex_identity(payload)
        modes = (
            [(target, work_mode.resolve_mode(target))]
            if unclassified
            else verification_mode.governing_modes(command, cwd, target, harness=harness)
        )
        for governed, mode in modes:
            observed_checks.check_capture_deadline()
            if (mode != "observe" and not (unclassified and mode == "off")) or not _allowed(governed, harness, pin):
                continue
            observed_checks.append_check(
                governed,
                observed_checks.build_check(
                    target=governed,
                    command=command,
                    status="unknown",
                    exit_code=None,
                    identity=identity,
                    session_fingerprint=session,
                    fingerprint_tree=False,
                    duration_ms=None,
                    unclassified=unclassified,
                ),
            )
    finally:
        lesson_activity.on_event(target, event, payload)


def after_handling(
    harness: str, payload: dict[str, Any] | None, target: Path | None = None, *, pin: Path | None = None
) -> None:
    try:
        with observed_checks.capture_budget(0.25):
            if isinstance(payload, dict):
                with command_event() as budget, budget.in_phase("observation"):
                    _capture(harness, payload, target, pin)
    except Exception:
        pass


def run_codex_hook(*, pin: Path | None = None) -> None:
    from .fleet_session_presence import _read_bounded_json_object

    try:
        payload = _read_bounded_json_object()
    except Exception:
        payload = None
    # Flush the harness response before optional work, including on malformed input.
    print("{}", flush=True)
    after_handling("codex", payload, pin=pin)
