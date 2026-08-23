"""Opt-in scheduler scaffold for scheduled memory care (issue #759).

``brigade care install|status|uninstall`` writes and audits user-owned
scheduler entries. Brigade never runs a daemon; the operator's crontab,
systemd user timers, or Task Scheduler remains the trigger.

Default entries match ``docs/scheduled-care.md``. Recipes that ship as
memory-care runbooks invoke those runbooks so each scheduled fire writes a
normal runbook receipt.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import plistlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import managed_block, memory_cmd, runbook_cmd

CARE_KIND = "CARE"
CARE_MARKER_VERSION = 1
CARE_MARKER_STYLE = managed_block.MARKER_STYLE_HASH
DEFAULT_BACKEND = "auto"
SUPPORTED_BACKENDS = ("auto", "crontab", "systemd", "launchd", "schtasks")
SCHTASKS_BACKEND = "schtasks"
SCHTASKS_WEEKDAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
# /NP (S4U) requires elevation: a non-admin `schtasks /Create /RU user /NP`
# prompts for a password and exits 1. /IT runs while the user is logged on
# and works without elevation — the intended zero-setup default. Care tasks
# do not fire while fully logged off.
SCHTASKS_S4U_FLAGS = '/RU "%USERNAME%" /IT'
SCHTASKS_CMD_EXE = r"C:\Windows\System32\cmd.exe"

MEMORY_CARE_RUNBOOK_REL = {
    "daily-care": ".brigade/memory-care/runbooks/daily-care-pass.json",
    "ingest-sweep": ".brigade/memory-care/runbooks/ingest-sweep.json",
    "weekly-outcome-ratchet": ".brigade/memory-care/runbooks/weekly-outcome-ratchet.json",
    "daily-observability": ".brigade/memory-care/runbooks/daily-observability.json",
}
NIGHTLY_RUNBOOK_REL = ".brigade/runbooks/nightly-maintenance.json"
DAILY_OBSERVABILITY_RUNBOOK_NAME = "daily-observability.json"
DAILY_OBSERVABILITY_RUNBOOK_PAYLOAD: dict[str, Any] = {
    "id": "daily-observability",
    "description": (
        "Daily handoff health, daily driver snapshot, and operator report bundle. "
        "Run from the repo or workspace root: "
        "brigade runbook run --approved .brigade/memory-care/runbooks/daily-observability.json"
    ),
    "allowed_commands": ["brigade"],
    "steps": [
        {"id": "handoff-doctor", "run": "brigade handoff doctor --target ."},
        {"id": "daily-status", "run": "brigade daily status --target ."},
        {"id": "center-report", "run": "brigade center report build --target ."},
    ],
}


@dataclass(frozen=True)
class CareEntry:
    """One scheduled care recipe from docs/scheduled-care.md or the #762 memory-job inventory."""

    entry_id: str
    schedule: str  # crontab five-field expression
    on_calendar: str  # systemd OnCalendar=
    description: str
    kind: Literal["runbook", "shell"]
    runbook_rel: str | None = None
    shell: str | None = None  # multi-command shell body without cd/PATH
    runbook_id: str | None = None
    requires_existing_runbook: bool = False


# Schedules and recipes mirror docs/scheduled-care.md. Where a shipped
# memory-care runbook covers the recipe, invoke that runbook for receipts.
CARE_ENTRIES: tuple[CareEntry, ...] = (
    CareEntry(
        entry_id="daily-care",
        schedule="15 6 * * *",
        on_calendar="*-*-* 06:15:00",
        description="Brigade daily memory care pass",
        kind="runbook",
        runbook_rel=MEMORY_CARE_RUNBOOK_REL["daily-care"],
        runbook_id="daily-care-pass",
    ),
    CareEntry(
        entry_id="ingest-sweep",
        schedule="*/30 * * * *",
        on_calendar="*:0/30",
        description="Brigade handoff ingest sweep",
        kind="runbook",
        runbook_rel=MEMORY_CARE_RUNBOOK_REL["ingest-sweep"],
        runbook_id="ingest-sweep",
    ),
    CareEntry(
        entry_id="weekly-outcome-ratchet",
        schedule="0 7 * * 1",
        on_calendar="Mon *-*-* 07:00:00",
        description="Brigade weekly outcome ratchet",
        kind="runbook",
        runbook_rel=MEMORY_CARE_RUNBOOK_REL["weekly-outcome-ratchet"],
        runbook_id="weekly-outcome-ratchet",
    ),
    CareEntry(
        entry_id="daily-observability",
        schedule="0 8 * * *",
        on_calendar="*-*-* 08:00:00",
        description="Brigade daily observability pass",
        kind="runbook",
        runbook_rel=MEMORY_CARE_RUNBOOK_REL["daily-observability"],
        runbook_id="daily-observability",
    ),
    CareEntry(
        entry_id="nightly-ops",
        schedule="0 4 * * *",
        on_calendar="*-*-* 04:00:00",
        description="Brigade nightly maintenance runbook",
        kind="runbook",
        runbook_rel=NIGHTLY_RUNBOOK_REL,
        runbook_id="nightly-maintenance",
    ),
)

# Maintainer memory jobs from docs/proposals/2026-08-12-care-managed-memory-jobs.md.
# Selected with ``brigade care install --entry <job_id>``. Not part of the
# default atomic scheduled-care set.
MEMORY_JOB_RUNBOOK_REL = {
    "handoff-ingest": MEMORY_CARE_RUNBOOK_REL["ingest-sweep"],
    "care-scan": MEMORY_CARE_RUNBOOK_REL["daily-care"],
    "memory-refresh": ".brigade/memory-care/runbooks/memory-refresh.json",
    "evidence-crawl": ".brigade/memory-care/runbooks/evidence-crawl.json",
    "memory-closeout": ".brigade/memory-care/runbooks/memory-closeout.json",
}
MEMORY_CLOSEOUT_RUNBOOK_NAME = "memory-closeout.json"
MEMORY_CLOSEOUT_RUNBOOK_PAYLOAD: dict[str, Any] = {
    "id": "memory-closeout",
    "description": (
        "Daily memory-care closeout. Run from the repo or workspace root: "
        "brigade runbook run --approved .brigade/memory-care/runbooks/memory-closeout.json"
    ),
    "allowed_commands": ["brigade"],
    "steps": [
        {"id": "closeout", "run": "brigade memory care closeout --target ."},
    ],
}
MEMORY_JOB_ENTRIES: tuple[CareEntry, ...] = (
    CareEntry(
        entry_id="handoff-ingest",
        schedule="*/30 * * * *",
        on_calendar="*:0/30",
        description="Brigade handoff ingest",
        kind="runbook",
        runbook_rel=MEMORY_JOB_RUNBOOK_REL["handoff-ingest"],
        runbook_id="ingest-sweep",
    ),
    CareEntry(
        entry_id="care-scan",
        schedule="15 6 * * *",
        on_calendar="*-*-* 06:15:00",
        description="Brigade memory care scan",
        kind="runbook",
        runbook_rel=MEMORY_JOB_RUNBOOK_REL["care-scan"],
        runbook_id="daily-care-pass",
    ),
    CareEntry(
        entry_id="memory-refresh",
        schedule="0 7 * * *",
        on_calendar="*-*-* 07:00:00",
        description="Brigade reviewed memory refresh",
        kind="runbook",
        runbook_rel=MEMORY_JOB_RUNBOOK_REL["memory-refresh"],
        runbook_id="memory-refresh",
        requires_existing_runbook=True,
    ),
    CareEntry(
        entry_id="evidence-crawl",
        schedule="0 8 * * *",
        on_calendar="*-*-* 08:00:00",
        description="Brigade local evidence crawl",
        kind="runbook",
        runbook_rel=MEMORY_JOB_RUNBOOK_REL["evidence-crawl"],
        runbook_id="evidence-crawl",
        requires_existing_runbook=True,
    ),
    CareEntry(
        entry_id="memory-closeout",
        schedule="0 9 * * *",
        on_calendar="*-*-* 09:00:00",
        description="Brigade memory care closeout",
        kind="runbook",
        runbook_rel=MEMORY_JOB_RUNBOOK_REL["memory-closeout"],
        runbook_id="memory-closeout",
    ),
)


# A care entry that fails this many receipts in a row is "repeatedly failing"
# and must surface on status, work brief, and the Center dashboard (#985).
REPEATED_FAILURE_THRESHOLD = 2


def _catalog_by_id() -> dict[str, CareEntry]:
    return {entry.entry_id: entry for entry in (*CARE_ENTRIES, *MEMORY_JOB_ENTRIES)}


def _runbook_identity_key(entry: CareEntry) -> str:
    """Identity used to recognize predecessor ids that schedule the same runbook."""
    if entry.runbook_id:
        return f"id:{entry.runbook_id}"
    if entry.runbook_rel:
        return f"path:{entry.runbook_rel}"
    return f"entry:{entry.entry_id}"


def _adopt_installed_aliases(
    requested: tuple[CareEntry, ...],
    installed: tuple[CareEntry, ...],
) -> tuple[tuple[CareEntry, ...], list[dict[str, Any]]]:
    """Replace requested entries with installed predecessors that share a runbook.

    Bare ``brigade care install`` of the atomic set must not stack ``daily-care``
    next to ``care-scan`` (same ``daily-care-pass`` runbook) or ``ingest-sweep``
    next to ``handoff-ingest``. Adopt the installed id instead of creating a
    second timer (#986).
    """
    installed_by_runbook: dict[str, CareEntry] = {}
    for entry in installed:
        installed_by_runbook.setdefault(_runbook_identity_key(entry), entry)

    resolved: list[CareEntry] = []
    adopted: list[dict[str, Any]] = []
    seen_runbooks: set[str] = set()
    for entry in requested:
        key = _runbook_identity_key(entry)
        covering = installed_by_runbook.get(key)
        if covering is not None and covering.entry_id != entry.entry_id:
            adopted.append(
                {
                    "requested": entry.entry_id,
                    "using": covering.entry_id,
                    "runbook_id": covering.runbook_id,
                    "runbook": covering.runbook_rel,
                }
            )
            if key not in seen_runbooks:
                resolved.append(covering)
                seen_runbooks.add(key)
            continue
        if key in seen_runbooks:
            continue
        resolved.append(entry)
        seen_runbooks.add(key)
    return tuple(resolved), adopted


def _resolve_selected_entries(
    entry_ids: list[str] | None,
) -> tuple[tuple[CareEntry, ...] | None, str | None]:
    """Return the default atomic set, or the named catalog entries in request order."""
    if not entry_ids:
        return CARE_ENTRIES, None
    catalog = _catalog_by_id()
    selected: list[CareEntry] = []
    seen: set[str] = set()
    for raw in entry_ids:
        job_id = str(raw or "").strip()
        if not job_id or job_id in seen:
            continue
        entry = catalog.get(job_id)
        if entry is None:
            return None, f"unknown care entry: {job_id}"
        selected.append(entry)
        seen.add(job_id)
    if not selected:
        return None, "unknown care entry: "
    return tuple(selected), None


def _required_runbook_error(target: Path, entries: tuple[CareEntry, ...]) -> str | None:
    """Refuse operator-authored jobs whose runbook is missing or malformed."""
    for entry in entries:
        if not entry.requires_existing_runbook:
            continue
        assert entry.runbook_rel is not None
        path = target / entry.runbook_rel
        if not path.is_file():
            return (
                f"care entry {entry.entry_id} is not installable: missing operator-approved runbook {entry.runbook_rel}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"care entry {entry.entry_id} is not installable: invalid runbook {entry.runbook_rel}: {exc}"
        if not isinstance(payload, dict):
            return f"care entry {entry.entry_id} is not installable: runbook {entry.runbook_rel} is not a JSON object"
        found_id = str(payload.get("id") or "")
        if entry.runbook_id and found_id != entry.runbook_id:
            return (
                f"care entry {entry.entry_id} is not installable: runbook id {found_id!r} "
                f"does not match {entry.runbook_id!r}"
            )
    return None


def _is_windows() -> bool:
    return sys.platform == "win32"


def _target_identity(target: Path) -> str:
    """Return the stable, non-secret namespace for one absolute target path."""
    absolute = target.expanduser().resolve()
    return hashlib.sha256(os.fsencode(absolute)).hexdigest()[:16]


def _resolve_backend(backend: str) -> str | None:
    if backend != "auto":
        return backend
    if sys.platform.startswith("linux"):
        return "systemd"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "win32":
        return SCHTASKS_BACKEND
    return None


def _uses_schtasks(backend: str) -> bool:
    """True only after auto resolved to schtasks, or an explicit schtasks request.

    Explicit systemd/launchd/crontab plans are host-agnostic file generation.
    Do not intercept them on win32 — that hid plan-generation tests behind the
    Windows printer (#1045).
    """
    return backend == SCHTASKS_BACKEND


def _windows_path(value: Path | str) -> str:
    """Emit a Windows path with single backslashes (no cron-style escaping)."""
    return os.fspath(value).replace("/", "\\")


def _schtasks_schedule_flags(schedule: str) -> str:
    """Map a five-field cron expression to schtasks /SC flags.

    Unsupported expressions fail closed rather than falling back to a daily
    06:15 template — that fallback is what silently mis-scheduled 4 of 5
    default care entries (#1021).
    """
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported care schedule for schtasks: {schedule}")
    minute, hour, day, month, weekday = fields
    if hour == day == month == weekday == "*" and minute.startswith("*/"):
        try:
            every = int(minute[2:])
        except ValueError as exc:
            raise ValueError(f"unsupported care schedule for schtasks: {schedule}") from exc
        if every <= 0:
            raise ValueError(f"unsupported care schedule for schtasks: {schedule}")
        return f"/SC MINUTE /MO {every}"
    try:
        minute_i = int(minute)
        hour_i = int(hour)
    except ValueError as exc:
        raise ValueError(f"unsupported care schedule for schtasks: {schedule}") from exc
    if not (0 <= minute_i <= 59 and 0 <= hour_i <= 23):
        raise ValueError(f"unsupported care schedule for schtasks: {schedule}")
    start = f"{hour_i:02d}:{minute_i:02d}"
    if day == month == weekday == "*":
        return f"/SC DAILY /ST {start}"
    if day == month == "*" and weekday != "*":
        try:
            weekday_i = int(weekday)
        except ValueError as exc:
            raise ValueError(f"unsupported care schedule for schtasks: {schedule}") from exc
        if weekday_i == 7:
            weekday_i = 0
        if weekday_i < 0 or weekday_i > 6:
            raise ValueError(f"unsupported care schedule for schtasks: {schedule}")
        return f"/SC WEEKLY /D {SCHTASKS_WEEKDAYS[weekday_i]} /ST {start}"
    raise ValueError(f"unsupported care schedule for schtasks: {schedule}")


def _schtasks_task_run(entry: CareEntry, *, workspace: Path) -> str:
    workspace_text = _windows_path(workspace)
    if " " in workspace_text or "\t" in workspace_text:
        # Escape inner quotes so the outer /TR "..." stays one argv for schtasks.
        cd_target = f'\\"{workspace_text}\\"'
    else:
        cd_target = workspace_text
    if entry.kind == "runbook":
        assert entry.runbook_rel is not None
        body = f"brigade runbook run --approved {entry.runbook_rel} --target ."
    else:
        assert entry.shell is not None
        body = entry.shell
    # Bare "cmd" is not on Task Scheduler's search path ("The system cannot
    # find the file specified" on windows-latest). Use the absolute console host.
    return f"{SCHTASKS_CMD_EXE} /c cd /d {cd_target} && {body}"


def _schtasks_task_name(entry: CareEntry) -> str:
    return f"BrigadeCare-{entry.entry_id}"


def _schtasks_create_command(entry: CareEntry, *, workspace: Path) -> str:
    task_name = _schtasks_task_name(entry)
    schedule_flags = _schtasks_schedule_flags(entry.schedule)
    task_run = _schtasks_task_run(entry, workspace=workspace)
    # Official create flags: /TN /TR /SC [/MO|/D] [/ST] /RU /IT /F.
    # Do not emit /NP (needs elevation) or /RL /F:String.
    return f'schtasks /Create /TN "{task_name}" /TR "{task_run}" {schedule_flags} {SCHTASKS_S4U_FLAGS} /F'


def _schtasks_delete_command(entry: CareEntry) -> str:
    return f'schtasks /Delete /TN "{_schtasks_task_name(entry)}" /F'


def _parse_schtasks_list(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key:
            parsed[key] = value.strip()
    return parsed


def _query_schtasks(task_name: str) -> dict[str, Any]:
    """Read one Task Scheduler entry. Missing tasks are a valid uninstalled state."""
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError:
        return {
            "task_name": task_name,
            "exists": False,
            "status": "unreadable",
            "last_run": None,
            "next_run": None,
            "error": "schtasks command not found",
        }
    except subprocess.TimeoutExpired:
        return {
            "task_name": task_name,
            "exists": False,
            "status": "unreadable",
            "last_run": None,
            "next_run": None,
            "error": "schtasks /Query timed out",
        }
    if result.returncode != 0:
        return {
            "task_name": task_name,
            "exists": False,
            "status": "missing",
            "last_run": None,
            "next_run": None,
            "error": None,
        }
    parsed = _parse_schtasks_list(result.stdout)
    last_run = parsed.get("Last Run Time") or None
    if last_run in {"N/A", "Never"}:
        last_run = None
    return {
        "task_name": task_name,
        "exists": True,
        "status": parsed.get("Status") or parsed.get("Scheduled Task State") or "present",
        "last_run": last_run,
        "next_run": parsed.get("Next Run Time") or None,
        "logon_mode": parsed.get("Logon Mode"),
        "error": None,
    }


def _schtasks_task_records(*, entries: tuple[CareEntry, ...]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entry in entries:
        query = _query_schtasks(_schtasks_task_name(entry))
        records.append(
            {
                "id": entry.entry_id,
                "task_name": query["task_name"],
                "schedule": entry.schedule,
                "schedule_flags": _schtasks_schedule_flags(entry.schedule),
                "exists": query["exists"],
                "status": query["status"],
                "last_run": query["last_run"],
                "next_run": query.get("next_run"),
                "error": query.get("error"),
            }
        )
    return records


def _schtasks_aggregate_status(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    if tasks and all(task.get("status") == "unreadable" for task in tasks):
        state = "unreadable"
        enabled = False
    elif tasks and all(task.get("exists") for task in tasks):
        state = "current"
        enabled = True
    elif any(task.get("exists") for task in tasks):
        state = "partial"
        enabled = True
    else:
        state = "missing"
        enabled = False
    return {
        "backend": SCHTASKS_BACKEND,
        "status": state,
        "enabled": enabled,
        "error": next((task.get("error") for task in tasks if task.get("error")), None),
        "tasks": tasks,
    }


def _path_prefix(home: Path | None = None) -> str:
    root = home if home is not None else Path.home()
    local_bin = root / ".local" / "bin"
    return f"{local_bin}:/usr/local/bin:/usr/bin:/bin"


def _public_block_status(status: str) -> str:
    if status == managed_block.STATUS_LOCALLY_MODIFIED:
        return "tampered"
    return status


def _block_body_from_rendered(text: str) -> str:
    parsed = managed_block.parse_blocks(text, kind=CARE_KIND, style=CARE_MARKER_STYLE)
    return parsed.body if parsed.status == "ok" else ""


def _owned_digest_from_text(text: str) -> str | None:
    parsed = managed_block.parse_blocks(text, kind=CARE_KIND, style=CARE_MARKER_STYLE)
    return parsed.actual_hash if parsed.status == "ok" else None


def _systemd_quote_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if value and all(ch not in ' \t\n\r"\\%' for ch in value):
        return value
    return f'"{escaped}"'


def _systemd_environment_path(path_value: str) -> str:
    inner = path_value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if any(ch in ' \t\n\r"\\%' for ch in path_value):
        return f'Environment="PATH={inner}"'
    return f"Environment=PATH={path_value}"


def _systemd_exec_start_runbook(runbook_rel: str) -> str:
    argv = ["/usr/bin/env", "brigade", "runbook", "run", "--approved", runbook_rel, "--target", "."]
    return "ExecStart=" + " ".join(_systemd_quote_value(arg) for arg in argv)


def _systemd_working_directory(workspace: Path) -> str:
    return f"WorkingDirectory={_systemd_quote_value(str(workspace))}"


def _cron_escape_path(value: str) -> str:
    """Escape cron metacharacters in a path embedded in a double-quoted shell fragment."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`").replace("%", "\\%")


def _cron_command(entry: CareEntry, *, workspace: Path) -> str:
    quoted_workspace = _cron_escape_path(str(workspace))
    if entry.kind == "runbook":
        assert entry.runbook_rel is not None
        quoted_rel = _cron_escape_path(entry.runbook_rel)
        body = f'brigade runbook run --approved "{quoted_rel}" --target .'
    else:
        assert entry.shell is not None
        body = entry.shell
    return f'cd "{quoted_workspace}" && {body}'


def _crontab_body(
    *,
    workspace: Path,
    home: Path | None = None,
    entries: tuple[CareEntry, ...] | None = None,
) -> str:
    selected = entries if entries is not None else CARE_ENTRIES
    lines = [f"PATH={_path_prefix(home)}"]
    for entry in selected:
        lines.append(f"{entry.schedule} {_cron_command(entry, workspace=workspace)}")
    return "\n".join(lines)


def render_crontab_block(*, workspace: Path, home: Path | None = None) -> str:
    """Render the managed crontab block including markers."""
    return managed_block.render_block(
        _crontab_body(workspace=workspace, home=home),
        kind=CARE_KIND,
        profile="crontab",
        style=CARE_MARKER_STYLE,
    )


def _read_crontab() -> tuple[str, str | None]:
    """Return (text, error). Empty crontab is success with empty text."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError:
        return "", "crontab command not found"
    except subprocess.TimeoutExpired:
        return "", "crontab -l timed out"
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        return result.stdout, None
    # Common empty-crontab messages across implementations.
    lowered = stderr.lower()
    if "no crontab" in lowered or result.returncode == 1 and not (result.stdout or "").strip():
        return "", None
    return "", stderr or f"crontab -l exited {result.returncode}"


def _write_crontab(text: str) -> str | None:
    payload = text if text.endswith("\n") or text == "" else text + "\n"
    try:
        result = subprocess.run(
            ["crontab", "-"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError:
        return "crontab command not found"
    except subprocess.TimeoutExpired:
        return "crontab - timed out"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return detail or f"crontab - exited {result.returncode}"
    return None


def _ensure_memory_care_runbooks(target: Path) -> int:
    """Write shipped memory-care runbooks when any are missing."""
    runbook_dir = memory_cmd._memory_care_runbooks_dir(target)
    missing = [name for name in memory_cmd.MEMORY_CARE_RUNBOOK_NAMES if not (runbook_dir / name).is_file()]
    if not missing:
        return 0
    return memory_cmd._write_memory_care_runbooks(target)


def _write_daily_observability_runbook(target: Path) -> int:
    """Materialize the daily-observability runbook with binary pins when missing."""
    dest = memory_cmd._memory_care_runbooks_dir(target) / DAILY_OBSERVABILITY_RUNBOOK_NAME
    if dest.is_file():
        return 0
    temp = dest.parent / f".{DAILY_OBSERVABILITY_RUNBOOK_NAME}.init-tmp"
    try:
        from .localio import write_json as _write_json

        temp.parent.mkdir(parents=True, exist_ok=True)
        _write_json(temp, dict(DAILY_OBSERVABILITY_RUNBOOK_PAYLOAD))
        validated, error = runbook_cmd._read_runbook(temp, require_pin_hashes=False)
        if validated is None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        pins, pin_error = runbook_cmd._pin_payload_from_runbook(target, validated)
        if pins is None:
            print(f"error: {pin_error}", file=sys.stderr)
            return 2
        validated["pins"] = pins
        _write_json(dest, validated)
        return 0
    finally:
        if temp.is_file():
            temp.unlink()


def _write_memory_closeout_runbook(target: Path) -> int:
    """Materialize the shipped memory-closeout runbook when missing."""
    dest = memory_cmd._memory_care_runbooks_dir(target) / MEMORY_CLOSEOUT_RUNBOOK_NAME
    if dest.is_file():
        return 0
    temp = dest.parent / f".{MEMORY_CLOSEOUT_RUNBOOK_NAME}.init-tmp"
    try:
        from .localio import write_json as _write_json

        temp.parent.mkdir(parents=True, exist_ok=True)
        _write_json(temp, dict(MEMORY_CLOSEOUT_RUNBOOK_PAYLOAD))
        validated, error = runbook_cmd._read_runbook(temp, require_pin_hashes=False)
        if validated is None:
            print(f"error: {error}", file=sys.stderr)
            return 2
        pins, pin_error = runbook_cmd._pin_payload_from_runbook(target, validated)
        if pins is None:
            print(f"error: {pin_error}", file=sys.stderr)
            return 2
        validated["pins"] = pins
        _write_json(dest, validated)
        return 0
    finally:
        if temp.is_file():
            temp.unlink()


def _ensure_care_runbooks(target: Path) -> int:
    rc = _ensure_memory_care_runbooks(target)
    if rc != 0:
        return rc
    return _write_daily_observability_runbook(target)


def _latest_runbook_receipt(target: Path, runbook_id: str) -> dict[str, Any] | None:
    for receipt in runbook_cmd._run_receipts(target):
        if str(receipt.get("runbook_id") or "") == runbook_id:
            return receipt
    return None


def _runbook_receipts(target: Path, runbook_id: str) -> list[dict[str, Any]]:
    """Newest-first receipts for one runbook. Empty when *runbook_id* is blank."""
    if not runbook_id:
        return []
    return [
        receipt for receipt in runbook_cmd._run_receipts(target) if str(receipt.get("runbook_id") or "") == runbook_id
    ]


def _consecutive_failed_receipts(receipts: list[dict[str, Any]]) -> int:
    """Count leading ``failed`` receipts. *receipts* must be newest-first."""
    count = 0
    for receipt in receipts:
        if str(receipt.get("status") or "") == "failed":
            count += 1
            continue
        break
    return count


def _receipt_summary(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "run_id": receipt.get("run_id"),
        "status": receipt.get("status"),
        "started_at": receipt.get("started_at"),
        "completed_at": receipt.get("completed_at"),
        "receipt_path": receipt.get("receipt_path"),
    }


def _entry_status(target: Path, entry: CareEntry) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": entry.entry_id,
        "schedule": entry.schedule,
        "on_calendar": entry.on_calendar,
        "kind": entry.kind,
        "description": entry.description,
    }
    assert entry.kind == "runbook"
    assert entry.runbook_rel is not None
    path = target / entry.runbook_rel
    payload["runbook"] = entry.runbook_rel
    payload["runbook_id"] = entry.runbook_id
    payload["runbook_present"] = path.is_file()
    receipts = _runbook_receipts(target, entry.runbook_id or "")
    consecutive = _consecutive_failed_receipts(receipts)
    payload["last_receipt"] = _receipt_summary(receipts[0] if receipts else None)
    payload["consecutive_failures"] = consecutive
    payload["repeated_failure"] = consecutive >= REPEATED_FAILURE_THRESHOLD
    return payload


def _repeated_failure_records(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate repeated failures by runbook so aliases do not double-count."""
    records: list[dict[str, Any]] = []
    seen_runbooks: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("repeated_failure"):
            continue
        runbook_id = str(entry.get("runbook_id") or entry.get("id") or "")
        if runbook_id in seen_runbooks:
            continue
        seen_runbooks.add(runbook_id)
        records.append(
            {
                "id": entry.get("id"),
                "runbook_id": entry.get("runbook_id"),
                "consecutive_failures": entry.get("consecutive_failures"),
                "last_receipt": entry.get("last_receipt"),
            }
        )
    return records


def repeated_failure_summary(target: Path) -> dict[str, Any]:
    """Receipt-only view of care entries with N consecutive failed receipts.

    Used by ``brigade work brief`` and the Center dashboard. Scans the full
    catalog (atomic set plus #762 memory jobs) and collapses aliases that
    share a runbook so ``care-scan`` / ``daily-care`` count once.
    """
    target = target.expanduser().resolve()
    seen_runbooks: set[str] = set()
    entry_payloads: list[dict[str, Any]] = []
    for entry in _catalog_by_id().values():
        key = entry.runbook_id or entry.entry_id
        if key in seen_runbooks:
            continue
        seen_runbooks.add(key)
        entry_payloads.append(_entry_status(target, entry))
    records = _repeated_failure_records(entry_payloads)
    return {
        "threshold": REPEATED_FAILURE_THRESHOLD,
        "count": len(records),
        "entries": records,
    }


def _windows_plan_tasks(*, workspace: Path, entries: tuple[CareEntry, ...]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for entry in entries:
        command = _schtasks_create_command(entry, workspace=workspace)
        tasks.append(
            {
                "id": entry.entry_id,
                "task_name": _schtasks_task_name(entry),
                "description": entry.description,
                "schedule": entry.schedule,
                "on_calendar": entry.on_calendar,
                "schedule_flags": _schtasks_schedule_flags(entry.schedule),
                "command": command,
            }
        )
    return tasks


def _windows_install(
    *,
    workspace: Path,
    entries: tuple[CareEntry, ...],
    json_output: bool,
    dry_run: bool,
) -> int:
    """Print Task Scheduler equivalents; never silently no-op on Windows."""
    try:
        tasks = _windows_plan_tasks(workspace=workspace, entries=entries)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = {
        "target": str(workspace),
        "backend": SCHTASKS_BACKEND,
        "dry_run": dry_run,
        "mutates_scheduler": False,
        "status": "printed-plan",
        "tasks": tasks,
    }
    if json_output:
        _print_payload(payload, json_output=True)
    else:
        print("care install: Windows Task Scheduler is not written automatically.")
        print("Print the equivalent schtasks commands below, then create the tasks yourself.")
        print(f"workspace: {workspace}")
        print("")
        for task in tasks:
            print(f":: {task['description']}")
            print(task["command"])
            print(f"schedule_hint: cron '{task['schedule']}' / OnCalendar={task['on_calendar']}")
            print("")
        print("next: create the tasks above in Task Scheduler, then re-run care status after the first fire.")
    print("error: care install does not mutate the Windows scheduler in this release", file=sys.stderr)
    return 3


def _windows_status(
    *,
    target: Path,
    entries: tuple[CareEntry, ...],
    json_output: bool,
) -> int:
    try:
        tasks = _schtasks_task_records(entries=entries)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    backend = _schtasks_aggregate_status(tasks)
    payload: dict[str, Any] = {
        "target": str(target),
        "backend": SCHTASKS_BACKEND,
        "status": backend["status"],
        "enabled": backend["enabled"],
        "tasks": tasks,
        "entries": [_entry_status(target, entry) for entry in entries],
    }
    payload["repeated_failures"] = _repeated_failure_records(payload["entries"])
    _print_payload(payload, json_output=json_output)
    return 0


def _windows_uninstall(
    *,
    entries: tuple[CareEntry, ...],
    json_output: bool,
    dry_run: bool,
    target: Path,
) -> int:
    commands = [_schtasks_delete_command(entry) for entry in entries]
    payload = {
        "target": str(target),
        "backend": SCHTASKS_BACKEND,
        "dry_run": dry_run,
        "mutates_scheduler": False,
        "commands": commands,
    }
    if json_output:
        _print_payload(payload, json_output=True)
    else:
        print("care uninstall: Windows Task Scheduler is not mutated automatically.")
        print("Remove BrigadeCare-* tasks with schtasks /Delete, or Task Scheduler UI.")
        for command in commands:
            print(command)
    print("error: care uninstall does not mutate the Windows scheduler in this release", file=sys.stderr)
    return 3


def _print_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
        if key == "repeated_failures" and isinstance(value, list):
            print(f"repeated_failures: {len(value)}")
            for rec in value:
                if not isinstance(rec, dict):
                    continue
                print(f"  - {rec.get('id')}: {rec.get('consecutive_failures')} consecutive failed receipts")
            continue
        if key == "adopted" and isinstance(value, list):
            print(f"adopted: {len(value)}")
            for rec in value:
                if not isinstance(rec, dict):
                    continue
                print(
                    f"  - {rec.get('requested')} -> {rec.get('using')} "
                    f"(runbook {rec.get('runbook_id') or rec.get('runbook')})"
                )
            continue
        if key == "entries" and isinstance(value, list):
            print(f"entries: {len(value)}")
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                receipt = entry.get("last_receipt")
                receipt_bit = "none"
                if isinstance(receipt, dict):
                    receipt_bit = f"{receipt.get('status')}@{receipt.get('started_at')}"
                present = entry.get("runbook_present")
                present_bit = ""
                if present is False:
                    present_bit = " runbook=missing"
                elif present is True:
                    present_bit = " runbook=present"
                fail_bit = ""
                consecutive = entry.get("consecutive_failures")
                if isinstance(consecutive, int) and consecutive >= REPEATED_FAILURE_THRESHOLD:
                    fail_bit = f" repeated_failure={consecutive}"
                print(
                    f"  - {entry.get('id')}: schedule={entry.get('schedule')} "
                    f"last_receipt={receipt_bit}{present_bit}{fail_bit}"
                )
            continue
        if key == "units" and isinstance(value, list):
            print(f"units: {len(value)}")
            for unit in value:
                if isinstance(unit, dict):
                    print(f"  - {unit.get('name')}: {unit.get('path')} ({unit.get('status')})")
            continue
        if key == "tasks" and isinstance(value, list):
            print(f"tasks: {len(value)}")
            for task in value:
                if not isinstance(task, dict):
                    continue
                if "exists" in task:
                    last_run = task.get("last_run") or "none"
                    print(
                        f"  - {task.get('id')}: exists={task.get('exists')} "
                        f"last_run={last_run} schedule={task.get('schedule')}"
                    )
                    continue
                print(f"  - {task.get('id')}: {task.get('command')}")
            continue
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def _launchd_dir(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / "Library" / "LaunchAgents"


def _launchd_interval_seconds(schedule: str) -> int | None:
    fields = schedule.split()
    if len(fields) != 5:
        return None
    minute, hour, day, month, weekday = fields
    if hour == day == month == weekday == "*" and minute.startswith("*/"):
        try:
            every = int(minute[2:])
        except ValueError:
            return None
        if every > 0:
            return every * 60
    return None


def _launchd_plists(
    *,
    workspace: Path,
    home: Path | None = None,
    entries: tuple[CareEntry, ...] | None = None,
) -> dict[str, bytes]:
    identity = _target_identity(workspace)
    selected = entries if entries is not None else CARE_ENTRIES
    result: dict[str, bytes] = {}
    for entry in selected:
        assert entry.runbook_rel is not None
        label = f"dev.brigade.care.{identity}.{entry.entry_id}"
        payload: dict[str, Any] = {
            "Label": label,
            "ProgramArguments": [
                "/usr/bin/env",
                "brigade",
                "runbook",
                "run",
                "--approved",
                entry.runbook_rel,
                "--target",
                ".",
            ],
            "WorkingDirectory": str(workspace),
            "EnvironmentVariables": {"PATH": _path_prefix(home)},
        }
        # launchd supports interval timers directly; calendar recipes use their
        # documented local hour/minute (weekly additionally pins Monday).
        # launchd Weekday uses the same numbering as cron (0/7=Sunday, 1=Monday).
        interval = _launchd_interval_seconds(entry.schedule)
        if interval is not None:
            payload["StartInterval"] = interval
        else:
            fields = entry.schedule.split()
            calendar: dict[str, int] = {"Minute": int(fields[0]), "Hour": int(fields[1])}
            if fields[4] != "*":
                calendar["Weekday"] = int(fields[4])
            payload["StartCalendarInterval"] = calendar
        result[f"{label}.plist"] = plistlib.dumps(payload, sort_keys=True)
    return result


def _installed_launchd_entries(*, target: Path, home: Path | None) -> tuple[CareEntry, ...]:
    """Return catalog entries with a target-namespaced LaunchAgent registration."""
    directory = _launchd_dir(home)
    installed: list[CareEntry] = []
    for entry in _catalog_by_id().values():
        name = next(iter(_launchd_plists(workspace=target, home=home, entries=(entry,))))
        if _path_exists_or_is_symlink(directory / name):
            installed.append(entry)
    return tuple(installed)


def _launchd_install(
    *,
    target: Path,
    dry_run: bool,
    json_output: bool,
    home: Path | None,
    entries: tuple[CareEntry, ...],
    adopted: list[dict[str, Any]] | None = None,
) -> int:
    directory = _launchd_dir(home)
    plists = _launchd_plists(workspace=target, home=home, entries=entries)
    if not dry_run:
        directory.mkdir(parents=True, exist_ok=True)
        for name, contents in plists.items():
            path = directory / name
            if path.is_symlink():
                print(f"error: refusing to follow symlink registration: {path}", file=sys.stderr)
                return 2
            path.write_bytes(contents)
    payload: dict[str, Any] = {
        "target": str(target),
        "backend": "launchd",
        "dry_run": dry_run,
        "registrations": [str(directory / name) for name in plists],
        "entries": [_entry_status(target, entry) for entry in entries],
    }
    if adopted:
        payload["adopted"] = adopted
    _print_payload(payload, json_output=json_output)
    return 0


def _launchd_status(
    *,
    target: Path,
    json_output: bool,
    home: Path | None,
    entries: tuple[CareEntry, ...],
) -> int:
    directory = _launchd_dir(home)
    expected = _launchd_plists(workspace=target, home=home, entries=entries)
    states = []
    for name, desired in expected.items():
        path = directory / name
        if path.is_symlink():
            registration_status = "tampered"
        elif not path.is_file():
            registration_status = "missing"
        else:
            registration_status = "current" if path.read_bytes() == desired else "tampered"
        states.append({"name": name, "status": registration_status})
    state = "missing"
    if states and all(item["status"] == "current" for item in states):
        state = "current"
    if any(item["status"] == "tampered" for item in states):
        state = "tampered"
    elif any(item["status"] == "missing" for item in states):
        state = "missing"
    entry_payloads = [_entry_status(target, entry) for entry in entries]
    _print_payload(
        {
            "target": str(target),
            "backend": "launchd",
            "status": state,
            "registrations": states,
            "entries": entry_payloads,
            "repeated_failures": _repeated_failure_records(entry_payloads),
        },
        json_output=json_output,
    )
    return 1 if state == "tampered" else 0


def _launchd_uninstall(
    *,
    target: Path,
    dry_run: bool,
    json_output: bool,
    home: Path | None,
    entries: tuple[CareEntry, ...],
) -> int:
    directory = _launchd_dir(home)
    removed: list[str] = []
    refused: list[str] = []
    for name, expected in _launchd_plists(workspace=target, home=home, entries=entries).items():
        path = directory / name
        if path.is_symlink():
            print(f"error: refusing to remove symlink registration: {path}", file=sys.stderr)
            return 2
        if path.is_file():
            if path.read_bytes() != expected:
                refused.append(name)
                continue
            removed.append(name)
            if not dry_run:
                path.unlink()
    _print_payload(
        {"target": str(target), "backend": "launchd", "dry_run": dry_run, "removed": removed, "refused": refused},
        json_output=json_output,
    )
    if not removed and not refused:
        print(f"no scheduler registration found for target: {target}")
    if refused:
        print("error: one or more launchd registrations were modified; refusing to remove", file=sys.stderr)
        return 1
    return 0


def install(
    *,
    target: Path,
    backend: str = DEFAULT_BACKEND,
    dry_run: bool = False,
    adopt: bool = False,
    json_output: bool = False,
    home: Path | None = None,
    entry_ids: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if backend not in SUPPORTED_BACKENDS:
        print(f"error: unsupported care backend: {backend}", file=sys.stderr)
        return 2
    entries, entry_error = _resolve_selected_entries(entry_ids)
    if entries is None:
        print(f"error: {entry_error}", file=sys.stderr)
        return 2
    runbook_error = _required_runbook_error(target, entries)
    if runbook_error:
        print(f"error: {runbook_error}", file=sys.stderr)
        return 2
    backend = _resolve_backend(backend) or ""
    if not backend:
        print(
            f"error: care scheduler is unsupported on {sys.platform}; supported platforms: Linux (systemd user timers), macOS (launchd), Windows (schtasks)",
            file=sys.stderr,
        )
        return 3
    adopted: list[dict[str, Any]] = []
    if not entry_ids:
        if backend == "systemd":
            installed = _installed_systemd_entries(target=target, home=home)
            entries, adopted = _adopt_installed_aliases(entries, installed)
        elif backend == "launchd":
            installed = _installed_launchd_entries(target=target, home=home)
            entries, adopted = _adopt_installed_aliases(entries, installed)

    if _uses_schtasks(backend):
        return _windows_install(workspace=target, entries=entries, json_output=json_output, dry_run=dry_run)

    rc = _ensure_care_runbooks(target)
    if rc != 0:
        return rc
    if any(entry.entry_id == "memory-closeout" for entry in entries):
        rc = _write_memory_closeout_runbook(target)
        if rc != 0:
            return rc

    if backend == "launchd":
        return _launchd_install(
            target=target,
            dry_run=dry_run,
            json_output=json_output,
            home=home,
            entries=entries,
            adopted=adopted,
        )

    if backend == "crontab":
        if entry_ids:
            print(
                "error: care --entry is not supported on the crontab backend; "
                "crontab remains one atomic managed block. Use systemd or launchd.",
                file=sys.stderr,
            )
            return 2
        return _install_crontab(
            target=target,
            dry_run=dry_run,
            adopt=adopt,
            json_output=json_output,
            home=home,
            entries=entries,
        )
    return _install_systemd(
        target=target,
        dry_run=dry_run,
        adopt=adopt,
        json_output=json_output,
        home=home,
        entries=entries,
        adopted=adopted,
    )


def _install_crontab(
    *,
    target: Path,
    dry_run: bool,
    adopt: bool,
    json_output: bool,
    home: Path | None,
    entries: tuple[CareEntry, ...],
) -> int:
    current, error = _read_crontab()
    if error:
        print(f"error: failed to read crontab: {error}", file=sys.stderr)
        return 2
    desired_body = _crontab_body(workspace=target, home=home, entries=entries)
    assessment = managed_block.assess_block(
        current,
        desired=desired_body,
        kind=CARE_KIND,
        profile="crontab",
        style=CARE_MARKER_STYLE,
    )
    if assessment.status == managed_block.STATUS_CURRENT:
        payload = {
            "target": str(target),
            "backend": "crontab",
            "status": "current",
            "action": managed_block.ACTION_NONE,
            "hash": assessment.desired_hash,
            "dry_run": dry_run,
            "entries": [_entry_status(target, entry) for entry in entries],
        }
        _print_payload(payload, json_output=json_output)
        return 0
    if assessment.status == managed_block.STATUS_LOCALLY_MODIFIED and not adopt:
        payload = {
            "target": str(target),
            "backend": "crontab",
            "status": "tampered",
            "action": managed_block.ACTION_PRESERVE,
            "recorded_hash": assessment.recorded_hash,
            "live_hash": assessment.actual_hash,
            "desired_hash": assessment.desired_hash,
            "detail": assessment.detail,
            "fix_command": "brigade care install --target . --adopt",
            "entries": [_entry_status(target, entry) for entry in entries],
        }
        _print_payload(payload, json_output=json_output)
        print("error: care crontab block was hand-edited; refusing to clobber without --adopt", file=sys.stderr)
        return 1
    if assessment.status == managed_block.STATUS_MALFORMED:
        payload = {
            "target": str(target),
            "backend": "crontab",
            "status": managed_block.STATUS_MALFORMED,
            "action": managed_block.ACTION_PRESERVE,
            "desired_hash": assessment.desired_hash,
            "detail": assessment.detail,
            "entries": [_entry_status(target, entry) for entry in entries],
        }
        _print_payload(payload, json_output=json_output)
        print("error: care crontab block has malformed markers; refusing to install", file=sys.stderr)
        return 1

    plan = managed_block.plan_install(
        current,
        desired=desired_body,
        kind=CARE_KIND,
        profile="crontab",
        adopt=adopt,
        style=CARE_MARKER_STYLE,
    )
    rendered = plan.rendered if plan.rendered is not None else current
    payload = {
        "target": str(target),
        "backend": "crontab",
        "status": _public_block_status(plan.status),
        "action": plan.action,
        "hash": plan.desired_hash,
        "dry_run": dry_run,
        "entries": [_entry_status(target, entry) for entry in entries],
    }
    if dry_run:
        payload["rendered_block"] = managed_block.render_block(
            desired_body,
            kind=CARE_KIND,
            profile="crontab",
            style=CARE_MARKER_STYLE,
        )
        _print_payload(payload, json_output=json_output)
        return 0
    if plan.action == managed_block.ACTION_NONE:
        _print_payload(payload, json_output=json_output)
        if not json_output:
            print("next_command: brigade care status --target .")
        return 0
    if plan.action == managed_block.ACTION_PRESERVE:
        payload["detail"] = plan.detail
        _print_payload(payload, json_output=json_output)
        if plan.status == managed_block.STATUS_MALFORMED:
            print("error: care crontab block has malformed markers; refusing to install", file=sys.stderr)
        else:
            print("error: care crontab block was hand-edited; refusing to clobber without --adopt", file=sys.stderr)
        return 1
    write_error = _write_crontab(rendered)
    if write_error:
        print(f"error: failed to write crontab: {write_error}", file=sys.stderr)
        return 2
    _print_payload(payload, json_output=json_output)
    if not json_output:
        print("next_command: brigade care status --target .")
        print("note: scheduled runbook entries require `brigade extras on` (or BRIGADE_EXTRAS=1).")
    return 0


def _systemd_user_dir(home: Path | None = None) -> Path:
    if home is not None:
        return home / ".config" / "systemd" / "user"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_unit_bodies(
    *,
    workspace: Path,
    home: Path | None = None,
    entries: tuple[CareEntry, ...] | None = None,
) -> dict[str, str]:
    path_value = _path_prefix(home)
    identity = _target_identity(workspace)
    selected = entries if entries is not None else CARE_ENTRIES
    units: dict[str, str] = {}
    for entry in selected:
        service_name = f"brigade-care-{identity}-{entry.entry_id}.service"
        timer_name = f"brigade-care-{identity}-{entry.entry_id}.timer"
        assert entry.kind == "runbook" and entry.runbook_rel is not None
        exec_start = _systemd_exec_start_runbook(entry.runbook_rel)
        service_body = "\n".join(
            [
                "[Unit]",
                f"Description={entry.description}",
                "",
                "[Service]",
                "Type=oneshot",
                _systemd_working_directory(workspace),
                _systemd_environment_path(path_value),
                exec_start,
            ]
        )
        timer_body = "\n".join(
            [
                "[Unit]",
                f"Description={entry.description} timer",
                "",
                "[Timer]",
                f"OnCalendar={entry.on_calendar}",
                "Persistent=true",
                "",
                "[Install]",
                "WantedBy=timers.target",
            ]
        )
        units[service_name] = managed_block.render_block(
            service_body, kind=CARE_KIND, profile="systemd", style=CARE_MARKER_STYLE
        )
        units[timer_name] = managed_block.render_block(
            timer_body, kind=CARE_KIND, profile="systemd", style=CARE_MARKER_STYLE
        )
    return units


def _install_systemd(
    *,
    target: Path,
    dry_run: bool,
    adopt: bool,
    json_output: bool,
    home: Path | None,
    entries: tuple[CareEntry, ...],
    adopted: list[dict[str, Any]] | None = None,
) -> int:
    unit_dir = _systemd_user_dir(home)
    identity = _target_identity(target)
    units = _systemd_unit_bodies(workspace=target, home=home, entries=entries)
    results: list[dict[str, Any]] = []
    blocked = False
    for name, desired_text in units.items():
        path = unit_dir / name
        desired_body = _block_body_from_rendered(desired_text)
        existing_text: str | None
        if path.is_file():
            if path.is_symlink():
                print(f"error: refusing to follow symlink unit: {path}", file=sys.stderr)
                return 2
            try:
                existing_text = managed_block.read_text_nofollow(path)
            except (OSError, UnicodeDecodeError) as exc:
                print(f"error: failed to read unit {path}: {exc}", file=sys.stderr)
                return 2
        else:
            existing_text = None
        plan = managed_block.plan_install(
            existing_text,
            desired=desired_body,
            kind=CARE_KIND,
            profile="systemd",
            adopt=adopt,
            style=CARE_MARKER_STYLE,
        )
        public_status = _public_block_status(plan.status)
        if plan.action == managed_block.ACTION_PRESERVE:
            blocked = True
            results.append(
                {
                    "name": name,
                    "path": str(path),
                    "status": public_status,
                    "action": managed_block.ACTION_PRESERVE,
                    "detail": plan.detail,
                }
            )
            continue
        if plan.status == managed_block.STATUS_CURRENT and plan.action == managed_block.ACTION_NONE:
            results.append({"name": name, "path": str(path), "status": "current", "action": managed_block.ACTION_NONE})
            continue
        results.append(
            {
                "name": name,
                "path": str(path),
                "status": public_status,
                "action": plan.action,
            }
        )
        if dry_run:
            continue
        unit_dir.mkdir(parents=True, exist_ok=True)
        _, outcome = managed_block.install_block(
            path,
            desired_body,
            kind=CARE_KIND,
            profile="systemd",
            adopt=adopt,
            style=CARE_MARKER_STYLE,
        )
        if outcome.status == managed_block.WRITE_ERROR:
            print(f"error: failed to write unit {path}: {outcome.detail}", file=sys.stderr)
            return 2
        if outcome.status == managed_block.WRITE_SKIPPED_SYMLINK:
            print(f"error: refusing to follow symlink unit: {path}", file=sys.stderr)
            return 2
        if outcome.status == managed_block.WRITE_REFUSED:
            blocked = True
            results[-1] = {
                "name": name,
                "path": str(path),
                "status": public_status,
                "action": managed_block.ACTION_PRESERVE,
                "detail": outcome.detail or plan.detail,
            }
            continue
    payload: dict[str, Any] = {
        "target": str(target),
        "backend": "systemd",
        "unit_dir": str(unit_dir),
        "dry_run": dry_run,
        "blocked": blocked,
        "units": results,
        "entries": [_entry_status(target, entry) for entry in entries],
        "next_commands": [
            "systemctl --user daemon-reload",
            *(
                [
                    f"systemctl --user enable --now brigade-care-{identity}-{entries[0].entry_id}.timer",
                    f"systemctl --user list-timers 'brigade-care-{identity}-*.timer'",
                ]
                if entries
                else [f"systemctl --user list-timers 'brigade-care-{identity}-*.timer'"]
            ),
        ],
    }
    if adopted:
        payload["adopted"] = adopted
    payload["repeated_failures"] = _repeated_failure_records(payload["entries"])
    _print_payload(payload, json_output=json_output)
    if blocked:
        if any(unit.get("status") == managed_block.STATUS_MALFORMED for unit in results):
            print("error: one or more systemd units have malformed markers; refusing to install", file=sys.stderr)
        else:
            print("error: one or more systemd units were hand-edited; refusing without --adopt", file=sys.stderr)
        return 1
    if not json_output and not dry_run:
        print("note: Brigade wrote unit files only; enable timers with systemctl --user.")
        print("note: scheduled runbook entries require `brigade extras on` (or BRIGADE_EXTRAS=1).")
    return 0


_INSTALLED_CARE_STATUSES = frozenset(
    {
        managed_block.STATUS_CURRENT,
        managed_block.STATUS_STALE,
        "tampered",
    }
)

_SYSTEMD_STATUS_RANK: dict[str, int] = {
    managed_block.STATUS_MALFORMED: 0,
    "tampered": 1,
    managed_block.STATUS_STALE: 2,
    managed_block.STATUS_MISSING: 3,
    managed_block.STATUS_CURRENT: 4,
    "unreadable": 5,
}

_WORKSPACE_CD_RE = re.compile(r'cd\s+"((?:[^"\\]|\\.)*)"')


def _backend_is_installed(status: str) -> bool:
    return status in _INSTALLED_CARE_STATUSES


def _normalize_workspace_path(value: str) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except OSError:
        return None


def _workspace_from_care_body(body: str) -> Path | None:
    for line in body.splitlines():
        match = _WORKSPACE_CD_RE.search(line)
        if match:
            raw = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
            return _normalize_workspace_path(raw)
    for line in body.splitlines():
        if not line.startswith("WorkingDirectory="):
            continue
        value = line.split("=", 1)[1].strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        return _normalize_workspace_path(value)
    return None


def _target_matches(installed: Path | None, target: Path) -> bool:
    if installed is None:
        return False
    try:
        return installed == target.expanduser().resolve()
    except OSError:
        return False


def _aggregate_systemd_status(unit_statuses: list[str]) -> str:
    if not unit_statuses:
        return managed_block.STATUS_MISSING
    worst = managed_block.STATUS_CURRENT
    for status in unit_statuses:
        if _SYSTEMD_STATUS_RANK.get(status, 99) < _SYSTEMD_STATUS_RANK.get(worst, 99):
            worst = status
    return worst


def _path_exists_or_is_symlink(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _installed_systemd_entries(*, target: Path, home: Path | None) -> tuple[CareEntry, ...]:
    """Return catalog entries with at least one target-namespaced unit on disk."""
    unit_dir = _systemd_user_dir(home)
    identity = _target_identity(target)
    installed: list[CareEntry] = []
    for entry in _catalog_by_id().values():
        prefix = f"brigade-care-{identity}-{entry.entry_id}"
        if any(_path_exists_or_is_symlink(unit_dir / f"{prefix}{suffix}") for suffix in (".service", ".timer")):
            installed.append(entry)
    return tuple(installed)


def _systemd_timer_is_enabled(*, unit_dir: Path, timer_name: str) -> bool:
    enabled_link = unit_dir / "timers.target.wants" / timer_name
    timer = unit_dir / timer_name
    if not enabled_link.is_symlink() or not timer.is_file():
        return False
    try:
        return enabled_link.resolve() == timer.resolve()
    except OSError:
        return False


def _crontab_backend_status(
    *,
    target: Path,
    home: Path | None = None,
    entries: tuple[CareEntry, ...] | None = None,
) -> dict[str, Any]:
    current, error = _read_crontab()
    if error:
        return {
            "backend": "crontab",
            "status": "unreadable",
            "target_match": False,
            "error": error,
            "action": None,
            "recorded_hash": None,
            "live_hash": None,
            "desired_hash": None,
            "detail": error,
            "profile": None,
        }
    desired_body = _crontab_body(workspace=target, home=home, entries=entries)
    assessment = managed_block.assess_block(
        current,
        desired=desired_body,
        kind=CARE_KIND,
        profile="crontab",
        style=CARE_MARKER_STYLE,
    )
    plan = managed_block.plan_install(
        current,
        desired=desired_body,
        kind=CARE_KIND,
        profile="crontab",
        style=CARE_MARKER_STYLE,
    )
    parsed = managed_block.parse_blocks(current, kind=CARE_KIND, style=CARE_MARKER_STYLE)
    public_status = _public_block_status(assessment.status)
    installed_workspace = _workspace_from_care_body(parsed.body) if parsed.status == "ok" else None
    payload: dict[str, Any] = {
        "backend": "crontab",
        "status": public_status,
        "target_match": _target_matches(installed_workspace, target),
        "error": None,
        "action": plan.action,
        "recorded_hash": assessment.recorded_hash,
        "live_hash": assessment.actual_hash,
        "desired_hash": assessment.desired_hash,
        "detail": assessment.detail,
        "profile": parsed.meta.profile if parsed.meta else None,
    }
    if public_status == "tampered":
        payload["fix_command"] = "brigade care install --target . --adopt"
    elif public_status == "stale":
        payload["fix_command"] = "brigade care install --target ."
    elif public_status == managed_block.STATUS_MISSING:
        payload["fix_command"] = "brigade care install --target ."
    return payload


def _systemd_backend_status(
    *,
    target: Path,
    home: Path | None = None,
    entries: tuple[CareEntry, ...] | None = None,
) -> dict[str, Any]:
    unit_dir = _systemd_user_dir(home)
    units = _systemd_unit_bodies(workspace=target, home=home, entries=entries)
    results: list[dict[str, Any]] = []
    unit_statuses: list[str] = []
    installed_workspace: Path | None = None
    timer_enabled: list[bool] = []
    for name, desired_text in units.items():
        path = unit_dir / name
        if path.is_symlink():
            public_status = managed_block.STATUS_MALFORMED
            result: dict[str, Any] = {
                "name": name,
                "path": str(path),
                "status": public_status,
                "recorded_hash": None,
                "live_hash": None,
                "desired_hash": None,
            }
            if name.endswith(".timer"):
                result["enabled"] = False
                timer_enabled.append(False)
            results.append(result)
            unit_statuses.append(public_status)
            continue
        if not path.is_file():
            existing_text = ""
        else:
            try:
                existing_text = managed_block.read_text_nofollow(path)
            except OSError:
                existing_text = ""
        desired_body = _block_body_from_rendered(desired_text)
        assessment = managed_block.assess_block(
            existing_text,
            desired=desired_body,
            kind=CARE_KIND,
            profile="systemd",
            style=CARE_MARKER_STYLE,
        )
        public_status = _public_block_status(assessment.status)
        if installed_workspace is None and name.endswith(".service") and existing_text:
            parsed = managed_block.parse_blocks(existing_text, kind=CARE_KIND, style=CARE_MARKER_STYLE)
            if parsed.status == "ok":
                installed_workspace = _workspace_from_care_body(parsed.body)
        results.append(
            {
                "name": name,
                "path": str(path),
                "status": public_status,
                "recorded_hash": assessment.recorded_hash,
                "live_hash": assessment.actual_hash,
                "desired_hash": assessment.desired_hash,
            }
        )
        if name.endswith(".timer"):
            timer_enabled.append(_systemd_timer_is_enabled(unit_dir=unit_dir, timer_name=name))
            results[-1]["enabled"] = timer_enabled[-1]
        unit_statuses.append(public_status)
    return {
        "backend": "systemd",
        "status": _aggregate_systemd_status(unit_statuses),
        "enabled": bool(timer_enabled) and all(timer_enabled),
        "target_match": _target_matches(installed_workspace, target),
        "error": None,
        "unit_dir": str(unit_dir),
        "units": results,
    }


def status_payload(*, target: Path, home: Path | None = None) -> dict[str, Any]:
    """Public structured care scheduler status across crontab and systemd.

    Topology and other read-only consumers should use this instead of scraping
    crontab text or private markers. ``enabled`` is true when either backend has
    a Brigade care block installed for *target* (current, stale, or tampered).

    Systemd discovery matches bare ``brigade care status``: when a target has
    per-entry registrations (including the #762 memory-job inventory), report
    those entries instead of treating the absent atomic CARE_ENTRIES set as a
    missing install.
    """
    target = target.expanduser().resolve()
    if _is_windows():
        tasks = _schtasks_task_records(entries=CARE_ENTRIES)
        schtasks = _schtasks_aggregate_status(tasks)
        return {
            "enabled": bool(schtasks.get("enabled")),
            "backends": {
                "crontab": {"backend": "crontab", "status": "unsupported-backend", "error": None},
                "systemd": {"backend": "systemd", "status": "unsupported-backend", "error": None},
                SCHTASKS_BACKEND: schtasks,
            },
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
            "tasks": tasks,
        }
    crontab = _crontab_backend_status(target=target, home=home)
    installed_systemd = _installed_systemd_entries(target=target, home=home)
    systemd_entries = installed_systemd if installed_systemd else CARE_ENTRIES
    systemd = _systemd_backend_status(target=target, home=home, entries=systemd_entries)
    crontab_enabled = _backend_is_installed(str(crontab.get("status"))) and bool(crontab.get("target_match"))
    systemd_enabled = _backend_is_installed(str(systemd.get("status"))) and bool(systemd.get("target_match"))
    if installed_systemd:
        selected_entries = installed_systemd
    else:
        selected_entries = CARE_ENTRIES
    entry_payloads = [_entry_status(target, entry) for entry in selected_entries]
    return {
        "enabled": crontab_enabled or systemd_enabled,
        "target_match": bool(crontab.get("target_match")) or bool(systemd.get("target_match")),
        "backends": {"crontab": crontab, "systemd": systemd},
        "entries": entry_payloads,
        "repeated_failures": _repeated_failure_records(entry_payloads),
    }


def status(
    *,
    target: Path,
    backend: str = DEFAULT_BACKEND,
    json_output: bool = False,
    home: Path | None = None,
    entry_ids: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if backend not in SUPPORTED_BACKENDS:
        print(f"error: unsupported care backend: {backend}", file=sys.stderr)
        return 2
    entries, entry_error = _resolve_selected_entries(entry_ids)
    if entries is None:
        print(f"error: {entry_error}", file=sys.stderr)
        return 2
    backend = _resolve_backend(backend) or ""
    if not backend:
        print(
            f"error: care scheduler is unsupported on {sys.platform}; supported platforms: Linux (systemd user timers), macOS (launchd), Windows (schtasks)",
            file=sys.stderr,
        )
        return 3
    if _uses_schtasks(backend):
        return _windows_status(target=target, entries=entries, json_output=json_output)

    if backend == "launchd":
        if not entry_ids:
            entries = _installed_launchd_entries(target=target, home=home)
        return _launchd_status(target=target, json_output=json_output, home=home, entries=entries)

    if backend == "crontab":
        if entry_ids:
            print(
                "error: care --entry is not supported on the crontab backend; "
                "crontab remains one atomic managed block. Use systemd or launchd.",
                file=sys.stderr,
            )
            return 2
        crontab = _crontab_backend_status(target=target, home=home, entries=entries)
        if crontab.get("error"):
            print(f"error: failed to read crontab: {crontab['error']}", file=sys.stderr)
            return 2
        entry_payloads = [_entry_status(target, entry) for entry in entries]
        crontab_payload: dict[str, Any] = {
            "target": str(target),
            **{k: v for k, v in crontab.items() if k != "error"},
            "entries": entry_payloads,
            "repeated_failures": _repeated_failure_records(entry_payloads),
        }
        public_status = str(crontab_payload.get("status"))
        _print_payload(crontab_payload, json_output=json_output)
        return 0 if public_status in {"current", managed_block.STATUS_MISSING} else 1

    if not entry_ids:
        entries = _installed_systemd_entries(target=target, home=home)
    systemd = _systemd_backend_status(target=target, home=home, entries=entries)
    entry_payloads = [_entry_status(target, entry) for entry in entries]
    payload = {
        "target": str(target),
        **{k: v for k, v in systemd.items() if k != "error"},
        "entries": entry_payloads,
        "repeated_failures": _repeated_failure_records(entry_payloads),
    }
    worst = str(payload.get("status"))
    _print_payload(payload, json_output=json_output)
    return 0 if worst in {"current", managed_block.STATUS_MISSING} else 1


def uninstall(
    *,
    target: Path,
    backend: str = DEFAULT_BACKEND,
    dry_run: bool = False,
    json_output: bool = False,
    home: Path | None = None,
    entry_ids: list[str] | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if backend not in SUPPORTED_BACKENDS:
        print(f"error: unsupported care backend: {backend}", file=sys.stderr)
        return 2
    entries, entry_error = _resolve_selected_entries(entry_ids)
    if entries is None:
        print(f"error: {entry_error}", file=sys.stderr)
        return 2
    backend = _resolve_backend(backend) or ""
    if not backend:
        print(
            f"error: care scheduler is unsupported on {sys.platform}; supported platforms: Linux (systemd user timers), macOS (launchd), Windows (schtasks)",
            file=sys.stderr,
        )
        return 3
    if _uses_schtasks(backend):
        return _windows_uninstall(entries=entries, json_output=json_output, dry_run=dry_run, target=target)

    if backend == "launchd":
        return _launchd_uninstall(target=target, dry_run=dry_run, json_output=json_output, home=home, entries=entries)

    if backend == "crontab":
        if entry_ids:
            print(
                "error: care --entry is not supported on the crontab backend; "
                "crontab remains one atomic managed block. Use systemd or launchd.",
                file=sys.stderr,
            )
            return 2
        current, error = _read_crontab()
        if error:
            print(f"error: failed to read crontab: {error}", file=sys.stderr)
            return 2
        owned_digest = _owned_digest_from_text(current)
        plan = managed_block.plan_remove(current, kind=CARE_KIND, owned_digest=owned_digest, style=CARE_MARKER_STYLE)
        public_status = _public_block_status(plan.status)
        payload = {
            "target": str(target),
            "backend": "crontab",
            "status": public_status,
            "action": plan.action,
            "dry_run": dry_run,
            "detail": plan.detail,
        }
        if plan.action == managed_block.ACTION_PRESERVE:
            _print_payload(payload, json_output=json_output)
            print(
                "error: care crontab block was hand-edited; refusing to remove without repair",
                file=sys.stderr,
            )
            return 1
        if plan.action == managed_block.ACTION_NONE:
            _print_payload(payload, json_output=json_output)
            return 0
        rendered = plan.rendered if plan.rendered is not None else ""
        if dry_run:
            payload["would_write"] = True
            _print_payload(payload, json_output=json_output)
            return 0
        write_error = _write_crontab(rendered)
        if write_error:
            print(f"error: failed to write crontab: {write_error}", file=sys.stderr)
            return 2
        _print_payload(payload, json_output=json_output)
        return 0

    unit_dir = _systemd_user_dir(home)
    removed: list[str] = []
    skipped: list[str] = []
    refused: list[str] = []
    blocked = False
    identity = _target_identity(target)
    for entry in entries:
        for suffix in (".service", ".timer"):
            name = f"brigade-care-{identity}-{entry.entry_id}{suffix}"
            path = unit_dir / name
            if not path.exists():
                skipped.append(name)
                continue
            if path.is_symlink():
                print(f"error: refusing to remove symlink unit: {path}", file=sys.stderr)
                return 2
            try:
                text = managed_block.read_text_nofollow(path)
            except OSError as exc:
                print(f"error: failed to read unit {path}: {exc}", file=sys.stderr)
                return 2
            owned_digest = _owned_digest_from_text(text)
            plan = managed_block.plan_remove(text, kind=CARE_KIND, owned_digest=owned_digest, style=CARE_MARKER_STYLE)
            if plan.action == managed_block.ACTION_PRESERVE:
                blocked = True
                refused.append(name)
                continue
            if plan.action == managed_block.ACTION_NONE:
                skipped.append(name)
                continue
            if dry_run:
                removed.append(name)
                continue
            remove_plan, outcome = managed_block.remove_block(
                path,
                kind=CARE_KIND,
                owned_digest=owned_digest,
                style=CARE_MARKER_STYLE,
            )
            if remove_plan.action == managed_block.ACTION_PRESERVE:
                blocked = True
                refused.append(name)
                continue
            if outcome.status == managed_block.WRITE_ERROR:
                print(f"error: failed to update unit {path}: {outcome.detail}", file=sys.stderr)
                return 2
            if outcome.status == managed_block.WRITE_SKIPPED_SYMLINK:
                print(f"error: refusing to remove symlink unit: {path}", file=sys.stderr)
                return 2
            if path.exists() and not managed_block.read_text_nofollow(path).strip():
                path.unlink()
            removed.append(name)
    payload = {
        "target": str(target),
        "backend": "systemd",
        "dry_run": dry_run,
        "removed": removed,
        "skipped": skipped,
        "refused": refused,
        "next_commands": ["systemctl --user daemon-reload"],
    }
    _print_payload(payload, json_output=json_output)
    if not removed and not refused:
        print(f"no scheduler registration found for target: {target}")
    if blocked:
        print(
            "error: one or more systemd units were hand-edited; refusing to remove without repair",
            file=sys.stderr,
        )
        return 1
    return 0
