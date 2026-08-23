"""Managed Claude hook package specification and identity helpers."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

PACKAGE_ID = "brigade-claude-work-loop"
PACKAGE_VERSION = "1.2.0"
PACKAGE_REF = f"{PACKAGE_ID}@{PACKAGE_VERSION}"
COMMAND_PREFIX = "brigade work hook-run"
MANAGED_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "Stop",
)
LEGACY_SCRIPT_NAME = "brigade-work-loop.py"
MANAGED_MARKER_KEY = "_brigade"
HOOK_SCRIPT_NAME = "brigade-claude-work-loop.sh"
SESSION_START_TIMEOUT_SECONDS = 20
DEFAULT_HOOK_TIMEOUT_SECONDS = 15


def managed_command(event: str) -> str:
    return f"{COMMAND_PREFIX} --event {event} --package {PACKAGE_REF}"


def managed_handler(event: str) -> dict[str, Any]:
    timeout = SESSION_START_TIMEOUT_SECONDS if event == "SessionStart" else DEFAULT_HOOK_TIMEOUT_SECONDS
    return {"type": "command", "command": managed_command(event), "timeout": timeout}


def managed_groups() -> dict[str, list[dict[str, Any]]]:
    def group(event: str, matcher: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"hooks": [managed_handler(event)]}
        if matcher is not None:
            payload["matcher"] = matcher
        return payload

    return {
        "SessionStart": [group("SessionStart")],
        "UserPromptSubmit": [group("UserPromptSubmit")],
        "PreToolUse": [group("PreToolUse", "Bash"), group("PreToolUse", "Edit|Write|NotebookEdit")],
        "PostToolUse": [group("PostToolUse", "Bash|Edit|Write|NotebookEdit")],
        "PostToolUseFailure": [group("PostToolUseFailure", "Bash")],
        "PreCompact": [group("PreCompact")],
        "Stop": [group("Stop")],
    }


def hook_script_text(*, pin: Path | None = None) -> str:
    pin_flag = f" --target {shlex.quote(str(pin))}" if pin is not None else ""
    return (
        "#!/usr/bin/env sh\n"
        f"# Brigade-managed Claude Code work-loop hook ({PACKAGE_REF}).\n"
        "# Installed by `brigade work hooks install --scope user`; do not edit.\n"
        "set -eu\n"
        'event=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    --event)\n"
        '      event="$2"\n'
        "      shift 2\n"
        "      ;;\n"
        "    *)\n"
        "      shift\n"
        "      ;;\n"
        "  esac\n"
        "done\n"
        'if [ -z "$event" ]; then\n'
        "  exit 0\n"
        "fi\n"
        f'exec brigade work hook-run --event "$event" --package "{PACKAGE_REF}"{pin_flag}\n'
    )


def managed_user_command(event: str, script_path: Path) -> str:
    return f"{shlex.quote(str(script_path))} --event {event}"


def managed_user_handler(event: str, script_path: Path) -> dict[str, Any]:
    timeout = SESSION_START_TIMEOUT_SECONDS if event == "SessionStart" else DEFAULT_HOOK_TIMEOUT_SECONDS
    return {
        "type": "command",
        "command": managed_user_command(event, script_path),
        "timeout": timeout,
        MANAGED_MARKER_KEY: PACKAGE_REF,
    }


def managed_user_groups(script_path: Path) -> dict[str, list[dict[str, Any]]]:
    def group(event: str, matcher: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"hooks": [managed_user_handler(event, script_path)]}
        if matcher is not None:
            payload["matcher"] = matcher
        return payload

    return {
        "SessionStart": [group("SessionStart")],
        "UserPromptSubmit": [group("UserPromptSubmit")],
        "PreToolUse": [group("PreToolUse", "Bash"), group("PreToolUse", "Edit|Write|NotebookEdit")],
        "PostToolUse": [group("PostToolUse", "Bash|Edit|Write|NotebookEdit")],
        "PostToolUseFailure": [group("PostToolUseFailure", "Bash")],
        "PreCompact": [group("PreCompact")],
        "Stop": [group("Stop")],
    }


def _command_tokens(value: object) -> list[str] | None:
    """Shell-split a command-type hook handler's command, or None if it is not one.

    Shared by is_managed_handler and is_legacy_handler so both predicates apply
    the same tokenization rules.
    """
    if not isinstance(value, dict):
        return None
    if value.get("type") != "command":
        return None
    command = value.get("command")
    if not isinstance(command, str):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def is_managed_user_handler(value: object, script_path: Path, event: str | None = None) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get(MANAGED_MARKER_KEY) != PACKAGE_REF:
        return False
    tokens = _command_tokens(value)
    if tokens is None:
        return False
    script = str(script_path)
    parsed_event = None
    index = 0
    if tokens and Path(tokens[0]).resolve() == Path(script).resolve():
        index = 1
    elif tokens and tokens[0] == script:
        index = 1
    if index < len(tokens) and tokens[index] == "--event" and index + 1 < len(tokens):
        parsed_event = tokens[index + 1]
    return parsed_event in MANAGED_EVENTS and (event is None or parsed_event == event)


def is_managed_handler(value: object, event: str | None = None) -> bool:
    tokens = _command_tokens(value)
    if tokens is None or len(tokens) < 7:
        return False
    parsed_event = tokens[4]
    if not (
        Path(tokens[0]).name == "brigade"
        and tokens[1:4] == ["work", "hook-run", "--event"]
        and parsed_event in MANAGED_EVENTS
        and (event is None or parsed_event == event)
    ):
        return False
    package: str | None = None
    index = 5
    while index < len(tokens):
        flag = tokens[index]
        if flag in {"--package", "--target"} and index + 1 < len(tokens):
            if flag == "--package":
                package = tokens[index + 1]
            index += 2
            continue
        return False
    return bool(package is not None and package.startswith(f"{PACKAGE_ID}@") and len(package) > len(PACKAGE_ID) + 1)


def _is_python_interpreter(token: str) -> bool:
    name = Path(token).name
    if name in {"python", "python3"}:
        return True
    if name.startswith("python3.") and name[8:].isdigit():
        return True
    return False


def is_legacy_handler(value: object) -> bool:
    """Identify obsolete standalone Brigade work-loop hook registrations.

    Matches a command-type handler whose command executes the standalone
    ``brigade-work-loop.py`` script, either directly as the first token or as
    the first non-flag argument after an explicit Python interpreter. This is
    anchored to the executable position so unrelated foreign user hooks that
    merely mention the filename are never touched.
    """
    tokens = _command_tokens(value)
    if not tokens:
        return False
    if Path(tokens[0]).name == LEGACY_SCRIPT_NAME:
        return True
    if not _is_python_interpreter(tokens[0]):
        return False
    for token in tokens[1:]:
        if not token.startswith("-"):
            return Path(token).name == LEGACY_SCRIPT_NAME
    return False
