"""Managed Claude hook package specification and identity helpers."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

PACKAGE_ID = "brigade-claude-work-loop"
PACKAGE_VERSION = "1.0.0"
PACKAGE_REF = f"{PACKAGE_ID}@{PACKAGE_VERSION}"
COMMAND_PREFIX = "brigade work hook-run"
MANAGED_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")
LEGACY_SCRIPT_NAME = "brigade-work-loop.py"


def managed_command(event: str) -> str:
    return f"{COMMAND_PREFIX} --event {event} --package {PACKAGE_REF}"


def managed_groups() -> dict[str, list[dict[str, Any]]]:
    def group(event: str, matcher: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"hooks": [{"type": "command", "command": managed_command(event), "timeout": 15}]}
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


def is_managed_handler(value: object, event: str | None = None) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("type") != "command":
        return False
    command = value.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
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
    if not isinstance(value, dict):
        return False
    if value.get("type") != "command":
        return False
    command = value.get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
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
