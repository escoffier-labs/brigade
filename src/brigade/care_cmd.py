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
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import managed_block, memory_cmd, runbook_cmd

CARE_KIND = "CARE"
CARE_MARKER_VERSION = 1
CARE_MARKER_STYLE = managed_block.MARKER_STYLE_HASH
DEFAULT_BACKEND: Literal["crontab", "systemd"] = "crontab"
SUPPORTED_BACKENDS = ("crontab", "systemd")

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
    """One scheduled care recipe from docs/scheduled-care.md."""

    entry_id: str
    schedule: str  # crontab five-field expression
    on_calendar: str  # systemd OnCalendar=
    description: str
    kind: Literal["runbook", "shell"]
    runbook_rel: str | None = None
    shell: str | None = None  # multi-command shell body without cd/PATH
    runbook_id: str | None = None


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


def _is_windows() -> bool:
    return sys.platform == "win32"


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


def _crontab_body(*, workspace: Path, home: Path | None = None) -> str:
    lines = [f"PATH={_path_prefix(home)}"]
    for entry in CARE_ENTRIES:
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
    receipt = _latest_runbook_receipt(target, entry.runbook_id or "")
    if receipt is None:
        payload["last_receipt"] = None
    else:
        payload["last_receipt"] = {
            "run_id": receipt.get("run_id"),
            "status": receipt.get("status"),
            "started_at": receipt.get("started_at"),
            "completed_at": receipt.get("completed_at"),
            "receipt_path": receipt.get("receipt_path"),
        }
    return payload


def _windows_print(*, workspace: Path) -> int:
    """Print Task Scheduler equivalents; never silently no-op on Windows."""
    print("care install: Windows Task Scheduler is not written automatically.")
    print("Print the equivalent schtasks commands below, then create the tasks yourself.")
    print(f"workspace: {workspace}")
    print("")
    for entry in CARE_ENTRIES:
        task_name = f"BrigadeCare-{entry.entry_id}"
        command = _cron_command(entry, workspace=workspace)
        # schtasks /SC requires a schedule family; print a reviewable template.
        print(f":: {entry.description}")
        print(f'schtasks /Create /TN "{task_name}" /SC DAILY /ST 06:15 /TR "cmd /c {command}" /F')
        print(f"schedule_hint: cron '{entry.schedule}' / OnCalendar={entry.on_calendar}")
        print("")
    print("next: create the tasks above in Task Scheduler, then re-run care status after the first fire.")
    print("error: care install does not mutate the Windows scheduler in this release", file=sys.stderr)
    return 3


def _print_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    for key, value in payload.items():
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
                print(
                    f"  - {entry.get('id')}: schedule={entry.get('schedule')} last_receipt={receipt_bit}{present_bit}"
                )
            continue
        if key == "units" and isinstance(value, list):
            print(f"units: {len(value)}")
            for unit in value:
                if isinstance(unit, dict):
                    print(f"  - {unit.get('name')}: {unit.get('path')} ({unit.get('status')})")
            continue
        if isinstance(value, (dict, list)):
            print(f"{key}: {json.dumps(value, sort_keys=True)}")
        else:
            print(f"{key}: {value}")


def install(
    *,
    target: Path,
    backend: str = DEFAULT_BACKEND,
    dry_run: bool = False,
    adopt: bool = False,
    json_output: bool = False,
    home: Path | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if backend not in SUPPORTED_BACKENDS:
        print(f"error: unsupported care backend: {backend}", file=sys.stderr)
        return 2
    if _is_windows():
        return _windows_print(workspace=target)

    rc = _ensure_care_runbooks(target)
    if rc != 0:
        return rc

    if backend == "crontab":
        return _install_crontab(target=target, dry_run=dry_run, adopt=adopt, json_output=json_output, home=home)
    return _install_systemd(target=target, dry_run=dry_run, adopt=adopt, json_output=json_output, home=home)


def _install_crontab(
    *,
    target: Path,
    dry_run: bool,
    adopt: bool,
    json_output: bool,
    home: Path | None,
) -> int:
    current, error = _read_crontab()
    if error:
        print(f"error: failed to read crontab: {error}", file=sys.stderr)
        return 2
    desired_body = _crontab_body(workspace=target, home=home)
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
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
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
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
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
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
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
        "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
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


def _systemd_unit_bodies(*, workspace: Path, home: Path | None = None) -> dict[str, str]:
    path_value = _path_prefix(home)
    units: dict[str, str] = {}
    for entry in CARE_ENTRIES:
        service_name = f"brigade-{entry.entry_id}.service"
        timer_name = f"brigade-{entry.entry_id}.timer"
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
) -> int:
    unit_dir = _systemd_user_dir(home)
    units = _systemd_unit_bodies(workspace=target, home=home)
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
    payload = {
        "target": str(target),
        "backend": "systemd",
        "unit_dir": str(unit_dir),
        "dry_run": dry_run,
        "blocked": blocked,
        "units": results,
        "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
        "next_commands": [
            "systemctl --user daemon-reload",
            "systemctl --user enable --now brigade-daily-care.timer",
            "systemctl --user list-timers 'brigade-*.timer'",
        ],
    }
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


def status(
    *,
    target: Path,
    backend: str = DEFAULT_BACKEND,
    json_output: bool = False,
    home: Path | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if backend not in SUPPORTED_BACKENDS:
        print(f"error: unsupported care backend: {backend}", file=sys.stderr)
        return 2
    if _is_windows():
        windows_payload: dict[str, Any] = {
            "target": str(target),
            "backend": "windows",
            "status": "unsupported-backend",
            "detail": "care status does not read Task Scheduler in this release; use schtasks /Query",
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
        }
        _print_payload(windows_payload, json_output=json_output)
        return 3

    if backend == "crontab":
        current, error = _read_crontab()
        if error:
            print(f"error: failed to read crontab: {error}", file=sys.stderr)
            return 2
        desired_body = _crontab_body(workspace=target, home=home)
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
        crontab_payload: dict[str, Any] = {
            "target": str(target),
            "backend": "crontab",
            "status": public_status,
            "action": plan.action,
            "recorded_hash": assessment.recorded_hash,
            "live_hash": assessment.actual_hash,
            "desired_hash": assessment.desired_hash,
            "detail": assessment.detail,
            "profile": parsed.meta.profile if parsed.meta else None,
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
        }
        if public_status == "tampered":
            crontab_payload["fix_command"] = "brigade care install --target . --adopt"
        elif public_status == "stale":
            crontab_payload["fix_command"] = "brigade care install --target ."
        elif public_status == managed_block.STATUS_MISSING:
            crontab_payload["fix_command"] = "brigade care install --target ."
        _print_payload(crontab_payload, json_output=json_output)
        return 0 if public_status in {"current", managed_block.STATUS_MISSING} else 1

    unit_dir = _systemd_user_dir(home)
    units = _systemd_unit_bodies(workspace=target, home=home)
    results: list[dict[str, Any]] = []
    worst = "current"
    for name, desired_text in units.items():
        path = unit_dir / name
        if path.is_symlink():
            existing_text = ""
            public_status = managed_block.STATUS_MALFORMED
            results.append(
                {
                    "name": name,
                    "path": str(path),
                    "status": public_status,
                    "recorded_hash": None,
                    "live_hash": None,
                    "desired_hash": None,
                }
            )
            worst = public_status if worst == "current" else worst
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
        if public_status == "tampered":
            worst = "tampered"
        elif public_status == managed_block.STATUS_STALE and worst != "tampered":
            worst = "stale"
        elif public_status == managed_block.STATUS_MISSING and worst == "current":
            worst = managed_block.STATUS_MISSING
    payload = {
        "target": str(target),
        "backend": "systemd",
        "status": worst,
        "unit_dir": str(unit_dir),
        "units": results,
        "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
    }
    _print_payload(payload, json_output=json_output)
    return 0 if worst in {"current", managed_block.STATUS_MISSING} else 1


def uninstall(
    *,
    target: Path,
    backend: str = DEFAULT_BACKEND,
    dry_run: bool = False,
    json_output: bool = False,
    home: Path | None = None,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if backend not in SUPPORTED_BACKENDS:
        print(f"error: unsupported care backend: {backend}", file=sys.stderr)
        return 2
    if _is_windows():
        print("care uninstall: Windows Task Scheduler is not mutated automatically.")
        print("Remove BrigadeCare-* tasks with schtasks /Delete, or Task Scheduler UI.")
        for entry in CARE_ENTRIES:
            print(f'schtasks /Delete /TN "BrigadeCare-{entry.entry_id}" /F')
        print("error: care uninstall does not mutate the Windows scheduler in this release", file=sys.stderr)
        return 3

    if backend == "crontab":
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
    for entry in CARE_ENTRIES:
        for suffix in (".service", ".timer"):
            name = f"brigade-{entry.entry_id}{suffix}"
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
    if blocked:
        print(
            "error: one or more systemd units were hand-edited; refusing to remove without repair",
            file=sys.stderr,
        )
        return 1
    return 0
