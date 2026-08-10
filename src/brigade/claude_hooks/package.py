"""Managed Claude hook package specification and identity helpers."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

PACKAGE_ID = "brigade-claude-work-loop"
PACKAGE_VERSION = "1.1.0"
PACKAGE_REF = f"{PACKAGE_ID}@{PACKAGE_VERSION}"
COMMAND_PREFIX = "brigade work hook-run"
MANAGED_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")
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
        "PreToolUse": [group("PreToolUse", "Bash"), group("PreToolUse", "Edit|Write|NotebookEdit")],
        "PostToolUse": [group("PostToolUse", "Bash|Edit|Write|NotebookEdit")],
        "PostToolUseFailure": [group("PostToolUseFailure", "Bash")],
        "Stop": [group("Stop")],
    }


def hook_script_text() -> str:
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
        f'exec brigade work hook-run --event "$event" --package "{PACKAGE_REF}"\n'
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
        "PreToolUse": [group("PreToolUse", "Bash"), group("PreToolUse", "Edit|Write|NotebookEdit")],
        "PostToolUse": [group("PostToolUse", "Bash|Edit|Write|NotebookEdit")],
        "PostToolUseFailure": [group("PostToolUseFailure", "Bash")],
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
    if tokens is None:
        return False
    parsed_event = tokens[4] if len(tokens) > 4 else None
    return bool(
        len(tokens) == 7
        and Path(tokens[0]).name == "brigade"
        and tokens[1:4] == ["work", "hook-run", "--event"]
        and parsed_event in MANAGED_EVENTS
        and (event is None or parsed_event == event)
        and tokens[5] == "--package"
        and tokens[6].startswith(f"{PACKAGE_ID}@")
        and len(tokens[6]) > len(PACKAGE_ID) + 1
    )


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
