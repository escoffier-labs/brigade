"""Dashboard data access via the Brigade CLI only.

This module shells out to ``brigade … --json`` and never reads ``.brigade/``
state directly. Every panel must degrade gracefully when a command fails;
do not "optimize" by opening ledger files on disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from brigade.center_cmd.dashboard import timing as center_timing


def _command_error_detail(detail: str) -> str:
    """Prefer a JSON ``error`` field over the first line of pretty-printed JSON."""
    text = detail.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()[:200]
        return json.dumps(parsed, sort_keys=True)[:200]
    return text.splitlines()[0][:200]


def run_json(
    target: Path,
    args: Sequence[str],
    *,
    timeout: float = 20.0,
    ok_codes: Sequence[int] = (0,),
) -> dict:
    """Run a brigade subcommand and return parsed JSON, or ``{"error": ...}``.

    ``ok_codes`` lists exit codes whose stdout is still a valid payload.
    Doctor-style commands signal an unhealthy-but-projected state with a
    nonzero exit while printing their versioned JSON; pass ``(0, 1)`` there.
    """
    cmd = [sys.executable, "-m", "brigade", *args, "--target", str(target), "--json"]
    env = os.environ.copy()
    env["BRIGADE_EXTRAS"] = "1"
    phase_name = "cli:" + " ".join(args)
    try:
        with center_timing.phase(phase_name):
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
    except subprocess.TimeoutExpired:
        return {"error": "command timed out"}
    except OSError as exc:
        return {"error": f"command failed: {exc}"}

    if result.returncode not in ok_codes:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            return {"error": _command_error_detail(detail)}
        return {"error": f"command exited {result.returncode}"}

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "invalid JSON from command"}

    if not isinstance(parsed, dict):
        return {"error": "expected JSON object from command"}
    return parsed
