"""The scheduling step of `brigade init`: turn on extras and install the care schedule.

Setup registers its own schedules, so a person never has to run a second command
for the weekly outcome ratchet to fire. This runs after the install itself
succeeded and fails open: a host with no scheduler (container, CI, no systemd
user session) still finishes init with exit 0, one warning that names the exact
command to run later, and a status the caller can read.
"""

from __future__ import annotations

import contextlib
import io
import json
import shlex
from pathlib import Path
from typing import Any

# Status values for the care step; the same words appear in `brigade init --json`.
INSTALLED = "installed"
SKIPPED = "skipped"
FAILED = "failed"
DRY_RUN = "dry-run"
NOT_RUN = "not-run"


def care_command(target: Path) -> str:
    """The command that installs the care schedule for *target* later."""
    return f"brigade care install --target {shlex.quote(str(target))}"


def plan(target: Path) -> dict[str, Any]:
    """What a dry run would do, with nothing written."""
    return {
        "extras": {"status": DRY_RUN, "command": "brigade extras on"},
        "care": {"status": DRY_RUN, "command": care_command(target)},
    }


def skipped(target: Path, reason: str) -> dict[str, Any]:
    return {
        "extras": {"status": SKIPPED, "reason": reason, "command": "brigade extras on"},
        "care": {"status": SKIPPED, "reason": reason, "command": care_command(target)},
    }


def not_run(reason: str) -> dict[str, Any]:
    return {"extras": {"status": NOT_RUN, "reason": reason}, "care": {"status": NOT_RUN, "reason": reason}}


def _enable_extras() -> dict[str, Any]:
    """`brigade extras on`, without rewriting a marker that already exists."""
    from . import extras

    marker = extras.marker_path()
    if marker.is_file():
        return {"status": "already-enabled", "path": str(marker)}
    try:
        path = extras.enable()
    except OSError as exc:
        return {"status": FAILED, "reason": str(exc), "command": "brigade extras on"}
    return {"status": "enabled", "path": str(path)}


def _last_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("error:"):
            return line[len("error:") :].strip()
    return lines[-1] if lines else ""


def _install_care(target: Path) -> dict[str, Any]:
    """Run `brigade care install` in-process with its default set and backend."""
    from . import care_cmd

    command = care_command(target)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = care_cmd.install(target=target, json_output=True)
    except Exception as exc:  # fail open: a scheduler problem must never fail init
        return {"status": FAILED, "reason": f"{type(exc).__name__}: {exc}", "command": command}
    try:
        payload = json.loads(out.getvalue())
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if rc != 0:
        reason = _last_error(err.getvalue()) or f"care install exited {rc}"
        return {"status": FAILED, "reason": reason, "command": command}
    enable = payload.get("enable")
    if not isinstance(enable, dict):
        enable = {}
    result: dict[str, Any] = {
        "status": INSTALLED,
        "backend": payload.get("backend"),
        "entries": len(payload.get("entries") or []),
        "command": command,
    }
    not_started = list(enable.get("start_failed") or [])
    if not_started:
        result["not_started"] = not_started
        result["next_commands"] = [c for c in payload.get("next_commands") or [] if "list-timers" not in c]
    return result


def run(target: Path) -> dict[str, Any]:
    """Turn on extras, then install the care schedule. Never raises."""
    target = target.expanduser().resolve()
    # Extras first: the scheduled runbook entries are extras-gated.
    extras_result = _enable_extras()
    return {"extras": extras_result, "care": _install_care(target)}


def report(result: dict[str, Any], *, stdout, stderr) -> None:
    """Human output for the care step: a status line each, one warning on failure."""
    extras_result = result["extras"]
    care = result["care"]
    if extras_result["status"] == DRY_RUN:
        print(
            f"brigade: [dry-run] would turn on extras and install the care schedule "
            f"({care['command']}); skip with --no-care",
            file=stdout,
        )
        return
    if extras_result["status"] == SKIPPED:
        print(
            f"brigade: care schedule skipped ({extras_result['reason']}); install it later with `{care['command']}`",
            file=stdout,
        )
        return
    if extras_result["status"] == NOT_RUN:
        return
    if extras_result["status"] == FAILED:
        print(
            f"warning: could not turn on extras ({extras_result['reason']}); run `{extras_result['command']}` later",
            file=stderr,
        )
    else:
        print(f"brigade: extras {extras_result['status'].replace('-', ' ')}", file=stdout)
    if care["status"] == INSTALLED:
        print(f"brigade: care schedule installed ({care['backend']}, {care['entries']} entries)", file=stdout)
        if care.get("not_started"):
            print(
                "warning: care timers were written but systemd could not start them; run:",
                file=stderr,
            )
            for command in care.get("next_commands") or []:
                print(f"  {command}", file=stderr)
    else:
        print(
            f"warning: care schedule not installed ({care.get('reason') or 'unknown error'}); "
            f"run `{care['command']}` later",
            file=stderr,
        )
