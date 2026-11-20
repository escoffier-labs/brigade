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
from pathlib import Path
from typing import Any

# Status values for the care step; the same words appear in `brigade init --json`.
INSTALLED = "installed"
UNSUPPORTED = "unsupported"
SKIPPED = "skipped"
FAILED = "failed"
DRY_RUN = "dry-run"
NOT_RUN = "not-run"


def care_command(target: Path) -> str:
    """The command that installs the care schedule for *target* later.

    Quoted for this platform's shell. A Windows path that cannot be quoted safely is
    withheld behind a `<target>` placeholder instead of printing a broken command.
    """
    from . import shell_quote

    value = str(target)
    shown = shell_quote.quote_argument(value) if shell_quote.is_safe_argument(value) else "<target>"
    return f"brigade care install --target {shown}"


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

    try:
        marker = extras.marker_path()
        if marker.is_file():
            return {"status": "already-enabled", "path": str(marker)}
        path = extras.enable()
    except Exception as exc:  # fail open: OSError, or ValueError from a malformed XDG_CONFIG_HOME
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
        backend = care_cmd._resolve_backend(care_cmd.DEFAULT_BACKEND)
        if backend == care_cmd.SCHTASKS_BACKEND:
            # care install only prints Task Scheduler commands on Windows in this release,
            # so report that nothing was scheduled instead of suggesting a command that cannot help.
            return {
                "status": UNSUPPORTED,
                "backend": backend,
                "reason": "care install does not write the Windows scheduler in this release",
            }
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
        "target": str(target),
        "command": command,
    }
    not_started = list(enable.get("start_failed") or [])
    if not_started:
        result["not_started"] = not_started
        result["next_commands"] = [c for c in payload.get("next_commands") or [] if "list-timers" not in c]
    return result


def run(target: Path) -> dict[str, Any]:
    """Turn on extras, then install the care schedule.

    Never raises an ``Exception``: any failure becomes a ``failed`` status so init still exits 0.
    ``KeyboardInterrupt`` and ``SystemExit`` are not caught and propagate.
    """
    command = care_command(target)
    try:
        target = target.expanduser().resolve()
        command = care_command(target)
        # Extras first: the scheduled runbook entries are extras-gated.
        extras_result = _enable_extras()
        care_result = _install_care(target)
    except Exception as exc:  # fail open: a scheduling problem must never fail init
        reason = f"{type(exc).__name__}: {exc}"
        return {
            "extras": {"status": FAILED, "reason": reason, "command": "brigade extras on"},
            "care": {"status": FAILED, "reason": reason, "command": command},
        }
    return {"extras": extras_result, "care": care_result}


def step_note(result: dict[str, Any]) -> str | None:
    """One line a caller that prints its own step list shows for a care step that needs attention."""
    care = result["care"]
    if care["status"] == UNSUPPORTED:
        return "care schedule: not supported on Windows yet, nothing was scheduled"
    if care["status"] == INSTALLED and care.get("not_started"):
        return f"{len(care['not_started'])} care timers were written but not started, run `brigade care status`"
    if care["status"] == FAILED:
        return f"care schedule not installed ({care.get('reason') or 'unknown error'})"
    return None


def follow_ups(result: dict[str, Any]) -> list[str]:
    """Commands to run later for the steps that did not complete."""
    extras_result, care = result["extras"], result["care"]
    commands = []
    if extras_result["status"] in {SKIPPED, FAILED}:
        commands.append(extras_result["command"])
    if care["status"] in {SKIPPED, FAILED}:
        commands.append(care["command"])
    return commands


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
            f"brigade: care schedule skipped ({extras_result['reason']}). Turn on extras later with "
            f"`{extras_result['command']}` and install the schedule with `{care['command']}`",
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
    if care["status"] == UNSUPPORTED:
        print("brigade: care schedule: not supported on Windows yet, nothing was scheduled", file=stdout)
    elif care["status"] == INSTALLED:
        not_started = care.get("not_started") or []
        if not_started:
            print(
                f"brigade: care schedule written ({care['backend']}, {care['entries']} entries), "
                f"{len(not_started)} timers not started",
                file=stdout,
            )
            print(
                "warning: care timers were written but systemd could not start them "
                "(no reachable systemd user manager?), start them from a session that has one:",
                file=stderr,
            )
            for command in care.get("next_commands") or []:
                print(f"  {command}", file=stderr)
            print(f"  or check the state with `brigade care status --target {care['target']}`", file=stderr)
        else:
            print(f"brigade: care schedule installed ({care['backend']}, {care['entries']} entries)", file=stdout)
    else:
        print(
            f"warning: care schedule not installed ({care.get('reason') or 'unknown error'}); "
            f"run `{care['command']}` later",
            file=stderr,
        )
