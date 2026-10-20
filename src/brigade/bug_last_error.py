"""Best-effort local error record. This module never sends or prompts."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from types import TracebackType
from typing import Any

from .component_paths import data_root
from .localio import write_json


def error_path() -> Path:
    return Path(data_root()) / "brigade" / "last-error.json"


def command_path(parser: argparse.ArgumentParser, argv: list[str]) -> str:
    """Resolve only parser-defined names. Never persist user-supplied arguments."""
    names: list[str] = []
    for token in argv:
        choices = next((a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction)), {})
        if token not in choices:
            break
        names.append(token)
        parser = choices[token]
    return " ".join(names)


def frames(tb: TracebackType | None) -> list[str]:
    result: list[str] = []
    while tb is not None:
        module = str(tb.tb_frame.f_globals.get("__name__", ""))
        if module == "brigade" or module.startswith("brigade."):
            result.append(f"{module}:{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}")
        tb = tb.tb_next
    return result[-20:]


def record(
    command: str, exit_code: int, exc: BaseException | None = None, *, args: argparse.Namespace | None = None
) -> None:
    try:
        from .bug_redaction import Redactor, assert_clean

        # Scrub before truncating, so a truncated credential is never saved.
        redactor = Redactor(Path.cwd())
        paths = {str(Path.cwd())}

        def add_paths(value: Any) -> None:
            if isinstance(value, Path):
                paths.update((str(value), str(value.absolute()), str(value.resolve())))
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    add_paths(item)
            elif isinstance(value, dict):
                for item in value.values():
                    add_paths(item)

        if args is not None:
            add_paths(vars(args))
        message = str(exc) if exc is not None else ""
        for literal in sorted(paths - {"", ".", "/", "\\"}, key=len, reverse=True):
            message = message.replace(literal.replace("\\", "\\\\"), "<local>").replace(literal, "<local>")
        # Exceptions may name paths unrelated to any parsed argument. Quoted
        # paths include spaces, and the fallback removes unquoted absolute paths.
        # Do this before the general scrubber can replace only a path prefix.
        message = re.sub(
            r"""(["'])(?:[A-Za-z]:[\\/]|/|\\\\)[^\r\n]*?\1|(?:[A-Za-z]:[\\/]|/|\\\\)[^\r\n"'<>|]+""",
            "<path>",
            message,
        )
        message = redactor.text(message)[:500]
        payload = {
            "command": command,
            "exit_code": exit_code,
            "exception_type": type(exc).__name__ if exc is not None else None,
            "message_head": message,
            "frames": frames(exc.__traceback__) if exc is not None else [],
        }
        assert_clean(json.dumps(payload))
        write_json(error_path(), payload)
    except Exception:
        # Errors in diagnostic persistence must not mask the original failure.
        pass


def read() -> dict[str, Any] | None:
    try:
        value = json.loads(error_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return {
        "command": str(value.get("command", ""))[:200],
        "exit_code": value.get("exit_code") if type(value.get("exit_code")) is int else 1,
        "exception_type": str(value.get("exception_type") or "")[:100],
        "message_head": str(value.get("message_head", ""))[:500],
        "frames": [str(f)[:200] for f in value.get("frames", [])[:20]] if isinstance(value.get("frames"), list) else [],
    }
