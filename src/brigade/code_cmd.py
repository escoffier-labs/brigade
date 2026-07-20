"""Thin GraphTrail command facade.

GraphTrail remains the owner of graph storage, discovery, and command output.
Brigade only resolves its executable and relays an argv list across the process
boundary.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Sequence

from . import context_cmd, proc


# `brigade search` executable aliases remain supported through at least this
# compatibility window. Keep these public constants so release tooling and
# downstream wrappers can inspect the policy without parsing documentation.
SEARCH_ALIAS_RETENTION_MINOR_RELEASES = 2
SEARCH_ALIAS_RETENTION_DAYS = 90

_SHORT_OPERATION_TIMEOUT_SECONDS = 30.0
_SYNC_TIMEOUT_SECONDS = 900.0
_SYNC_TIMEOUT_ENV = "BRIGADE_CODE_SYNC_TIMEOUT_SECONDS"


def _configured_timeout(environment_variable: str, default: float) -> float | None:
    raw = os.environ.get(environment_variable)
    if raw is None:
        return default
    try:
        timeout = float(raw)
    except ValueError:
        return None
    return timeout if math.isfinite(timeout) and timeout > 0 else None


def run(verb: str, arguments: Sequence[str]) -> int:
    """Run one GraphTrail command and relay its output and exit status."""

    timeout = _SHORT_OPERATION_TIMEOUT_SECONDS
    if verb == "sync":
        configured_timeout = _configured_timeout(_SYNC_TIMEOUT_ENV, _SYNC_TIMEOUT_SECONDS)
        if configured_timeout is None:
            print(f"error: {_SYNC_TIMEOUT_ENV} must be a positive finite number of seconds", file=sys.stderr)
            return 2
        timeout = configured_timeout

    binary = context_cmd._graphtrail_bin()
    if binary is None:
        print("error: graphtrail is not installed; run `brigade setup`", file=sys.stderr)
        return 127

    result = proc.run([binary, verb, *arguments], timeout=timeout)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.code
