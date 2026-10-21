"""Best-effort local error record. This module never sends or prompts."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from .component_paths import data_root
from .localio import write_json
from .bug_io import read_bytes

_LAST_ERROR_BYTE_LIMIT = 64 * 1024


def error_path() -> Path:
    return Path(data_root()).resolve() / "brigade" / "last-error.json"


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
        from .bug_redaction import Redactor, ReportBlocked, assert_clean

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
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "exit_code": exit_code,
            "exception_type": type(exc).__name__ if exc is not None else None,
            "message_head": message,
            "frames": frames(exc.__traceback__) if exc is not None else [],
        }
        try:
            assert_clean(json.dumps(payload))
        except ReportBlocked:
            payload.update(message_head="", frames=[])
            try:
                assert_clean(json.dumps(payload))
            except ReportBlocked:
                error_path().unlink(missing_ok=True)
                return
        write_json(error_path(), payload)
    except Exception:
        # Errors in diagnostic persistence must not mask the original failure.
        pass


def read() -> dict[str, Any] | None:
    try:
        value = json.loads(read_bytes(error_path(), limit=_LAST_ERROR_BYTE_LIMIT) or b"")
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    recorded_at = value.get("recorded_at")
    if recorded_at is not None:
        try:
            when = datetime.fromisoformat(recorded_at)
            age = (datetime.now(timezone.utc) - when).total_seconds()
            if age < 0 or age > 24 * 3600:
                return None
        except (TypeError, ValueError):
            return None
    return {
        "recorded_at": recorded_at,
        "command": str(value.get("command", ""))[:200],
        "exit_code": value.get("exit_code") if type(value.get("exit_code")) is int else 1,
        "exception_type": str(value.get("exception_type") or "")[:100],
        "message_head": str(value.get("message_head", ""))[:500],
        "frames": [str(f)[:200] for f in value.get("frames", [])[:20]] if isinstance(value.get("frames"), list) else [],
    }
