"""Thin GraphTrail command facade.

GraphTrail remains the owner of graph storage, discovery, and command output.
Brigade only resolves its executable and relays an argv list across the process
boundary.
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from . import context_cmd, proc


# `brigade search` executable aliases remain supported through at least this
# compatibility window. Keep these public constants so release tooling and
# downstream wrappers can inspect the policy without parsing documentation.
SEARCH_ALIAS_RETENTION_MINOR_RELEASES = 2
SEARCH_ALIAS_RETENTION_DAYS = 90

_SHORT_OPERATION_TIMEOUT_SECONDS = 30.0
_LONG_OPERATION_TIMEOUT_SECONDS = 900.0
# sync writes the graph; evaluate runs the same extraction as a dry run. Both
# scale with repo size, so both get the long timeout and their own env knob.
_LONG_OPERATION_TIMEOUT_ENVS = {
    "sync": "BRIGADE_CODE_SYNC_TIMEOUT_SECONDS",
    "evaluate": "BRIGADE_CODE_EVALUATE_TIMEOUT_SECONDS",
}

# The engine's usage text names its own binary; the branded surface should
# point users back at the command they actually typed.
_ENGINE_USAGE_LINE = re.compile(r"(?m)^(\s*)Usage: graphtrail\b")


def _configured_timeout(environment_variable: str, default: float) -> float | None:
    raw = os.environ.get(environment_variable)
    if raw is None:
        return default
    try:
        timeout = float(raw)
    except ValueError:
        return None
    return timeout if math.isfinite(timeout) and timeout > 0 else None


def _extract_target(arguments: Sequence[str]) -> tuple[Path | None, list[str], str | None]:
    """Split Brigade's standard --target flag out of a passthrough argv."""
    forwarded: list[str] = []
    target: Path | None = None
    tokens = list(arguments)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--target":
            if index + 1 >= len(tokens):
                return None, [], "--target requires a directory"
            target = Path(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--target="):
            value = token.split("=", 1)[1]
            if not value:
                return None, [], "--target requires a directory"
            target = Path(value)
            index += 1
            continue
        forwarded.append(token)
        index += 1
    return target, forwarded, None


def _rebrand(text: str) -> str:
    return _ENGINE_USAGE_LINE.sub(r"\1Usage: brigade code", text)


def run(verb: str, arguments: Sequence[str]) -> int:
    """Run one GraphTrail command and relay its output and exit status."""

    timeout = _SHORT_OPERATION_TIMEOUT_SECONDS
    timeout_env = _LONG_OPERATION_TIMEOUT_ENVS.get(verb)
    if timeout_env is not None:
        configured_timeout = _configured_timeout(timeout_env, _LONG_OPERATION_TIMEOUT_SECONDS)
        if configured_timeout is None:
            print(f"error: {timeout_env} must be a positive finite number of seconds", file=sys.stderr)
            return 2
        timeout = configured_timeout

    target, forwarded, target_error = _extract_target(arguments)
    if target_error is not None:
        print(
            f"error: {target_error} (usage: brigade code {verb} --target <dir> [engine arguments])",
            file=sys.stderr,
        )
        return 2
    cwd: Path | None = None
    if target is not None:
        cwd = target.expanduser()
        if not cwd.is_dir():
            print(f"error: --target is not a directory: {target}", file=sys.stderr)
            return 2

    binary = context_cmd._graphtrail_bin()
    if binary is None:
        print("error: graphtrail is not installed; run `brigade setup`", file=sys.stderr)
        return 127

    argv = [binary, verb, *forwarded]
    if cwd is None:
        result = proc.run(argv, timeout=timeout)
    else:
        result = proc.run(argv, timeout=timeout, cwd=cwd)
    if result.stdout:
        print(_rebrand(result.stdout), end="")
    if result.stderr:
        print(_rebrand(result.stderr), end="", file=sys.stderr)
    return result.code
