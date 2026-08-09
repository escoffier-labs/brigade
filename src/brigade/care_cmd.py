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
DEFAULT_BACKEND: Literal["crontab", "systemd"] = "crontab"
SUPPORTED_BACKENDS = ("crontab", "systemd")

MEMORY_CARE_RUNBOOK_REL = {
    "daily-care": ".brigade/memory-care/runbooks/daily-care-pass.json",
    "ingest-sweep": ".brigade/memory-care/runbooks/ingest-sweep.json",
    "weekly-outcome-ratchet": ".brigade/memory-care/runbooks/weekly-outcome-ratchet.json",
}
NIGHTLY_RUNBOOK_REL = ".brigade/runbooks/nightly-maintenance.json"


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
        kind="shell",
        shell=(
            "brigade handoff doctor --target . && "
            "brigade daily status --target . && "
            "brigade center report build --target ."
        ),
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


def _cron_command(entry: CareEntry, *, workspace: Path) -> str:
    quoted = str(workspace).replace('"', '\\"')
    if entry.kind == "runbook":
        assert entry.runbook_rel is not None
        body = f"brigade runbook run --approved {entry.runbook_rel} --target ."
    else:
        assert entry.shell is not None
        body = entry.shell
    return f'cd "{quoted}" && {body}'


def _crontab_body(*, workspace: Path, home: Path | None = None) -> str:
    lines = [f"PATH={_path_prefix(home)}"]
    for entry in CARE_ENTRIES:
        lines.append(f"{entry.schedule} {_cron_command(entry, workspace=workspace)}")
    return "\n".join(lines)


def render_crontab_block(*, workspace: Path, home: Path | None = None) -> str:
    """Render the managed crontab block including markers."""
    return managed_block.wrap_block(
        kind=CARE_KIND,
        profile="crontab",
        body=_crontab_body(workspace=workspace, home=home),
        style="hash",
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
    if entry.kind == "runbook":
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
    else:
        payload["shell"] = entry.shell
        payload["last_receipt"] = None
        payload["receipt_note"] = (
            "shell recipes do not write a runbook receipt; inspect local artifacts under .brigade/"
        )
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

    rc = _ensure_memory_care_runbooks(target)
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
    plan = managed_block.classify_block(current, kind=CARE_KIND, desired_body=desired_body)
    if plan.status == "current":
        payload = {
            "target": str(target),
            "backend": "crontab",
            "status": "current",
            "action": "none",
            "hash": plan.desired_hash,
            "dry_run": dry_run,
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
        }
        _print_payload(payload, json_output=json_output)
        return 0
    if plan.status == "tampered" and not adopt:
        payload = {
            "target": str(target),
            "backend": "crontab",
            "status": "tampered",
            "action": "preserve",
            "recorded_hash": plan.recorded_hash,
            "live_hash": plan.live_hash,
            "desired_hash": plan.desired_hash,
            "detail": plan.detail,
            "fix_command": "brigade care install --target . --adopt",
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
        }
        _print_payload(payload, json_output=json_output)
        print("error: care crontab block was hand-edited; refusing to clobber without --adopt", file=sys.stderr)
        return 1

    rendered, apply_plan = managed_block.apply_block(
        current,
        kind=CARE_KIND,
        profile="crontab",
        desired_body=desired_body,
        style="hash",
        adopt=adopt,
    )
    payload = {
        "target": str(target),
        "backend": "crontab",
        "status": apply_plan.status,
        "action": apply_plan.action,
        "hash": apply_plan.desired_hash,
        "dry_run": dry_run,
        "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
    }
    if dry_run:
        payload["rendered_block"] = managed_block.wrap_block(
            kind=CARE_KIND, profile="crontab", body=desired_body, style="hash"
        )
        _print_payload(payload, json_output=json_output)
        return 0
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
        if entry.kind == "runbook":
            assert entry.runbook_rel is not None
            exec_start = f"ExecStart=brigade runbook run --approved {entry.runbook_rel} --target ."
        else:
            assert entry.shell is not None
            exec_start = f"ExecStart=/bin/sh -c '{entry.shell}'"
        service_body = "\n".join(
            [
                "[Unit]",
                f"Description={entry.description}",
                "",
                "[Service]",
                "Type=oneshot",
                f"WorkingDirectory={workspace}",
                f"Environment=PATH={path_value}",
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
        units[service_name] = managed_block.wrap_block(
            kind=CARE_KIND, profile="systemd", body=service_body, style="hash"
        )
        units[timer_name] = managed_block.wrap_block(kind=CARE_KIND, profile="systemd", body=timer_body, style="hash")
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
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        desired_match = managed_block.find_block(desired_text, kind=CARE_KIND)
        assert desired_match is not None
        plan = managed_block.classify_block(existing, kind=CARE_KIND, desired_body=desired_match.body)
        if plan.status == "tampered" and not adopt:
            blocked = True
            results.append(
                {
                    "name": name,
                    "path": str(path),
                    "status": "tampered",
                    "action": "preserve",
                    "detail": plan.detail,
                }
            )
            continue
        if plan.status == "current":
            results.append({"name": name, "path": str(path), "status": "current", "action": "none"})
            continue
        results.append(
            {
                "name": name,
                "path": str(path),
                "status": plan.status,
                "action": "create" if plan.status == "missing" else "update",
            }
        )
        if dry_run:
            continue
        unit_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            try:
                if path.is_symlink():
                    print(f"error: refusing to follow symlink unit: {path}", file=sys.stderr)
                    return 2
            except OSError:
                pass
        path.write_text(desired_text, encoding="utf-8")
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
        plan = managed_block.classify_block(current, kind=CARE_KIND, desired_body=desired_body)
        match = managed_block.find_block(current, kind=CARE_KIND)
        crontab_payload: dict[str, Any] = {
            "target": str(target),
            "backend": "crontab",
            "status": plan.status,
            "action": plan.action,
            "recorded_hash": plan.recorded_hash,
            "live_hash": plan.live_hash,
            "desired_hash": plan.desired_hash,
            "detail": plan.detail,
            "profile": match.profile if match else None,
            "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
        }
        if plan.status == "tampered":
            crontab_payload["fix_command"] = "brigade care install --target . --adopt"
        elif plan.status == "stale":
            crontab_payload["fix_command"] = "brigade care install --target ."
        elif plan.status == "missing":
            crontab_payload["fix_command"] = "brigade care install --target ."
        _print_payload(crontab_payload, json_output=json_output)
        return 0 if plan.status in {"current", "missing"} else 1

    unit_dir = _systemd_user_dir(home)
    units = _systemd_unit_bodies(workspace=target, home=home)
    results: list[dict[str, Any]] = []
    worst = "current"
    for name, desired_text in units.items():
        path = unit_dir / name
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        desired_match = managed_block.find_block(desired_text, kind=CARE_KIND)
        assert desired_match is not None
        plan = managed_block.classify_block(existing, kind=CARE_KIND, desired_body=desired_match.body)
        results.append(
            {
                "name": name,
                "path": str(path),
                "status": plan.status,
                "recorded_hash": plan.recorded_hash,
                "live_hash": plan.live_hash,
                "desired_hash": plan.desired_hash,
            }
        )
        if plan.status == "tampered":
            worst = "tampered"
        elif plan.status == "stale" and worst != "tampered":
            worst = "stale"
        elif plan.status == "missing" and worst == "current":
            worst = "missing"
    payload = {
        "target": str(target),
        "backend": "systemd",
        "status": worst,
        "unit_dir": str(unit_dir),
        "units": results,
        "entries": [_entry_status(target, entry) for entry in CARE_ENTRIES],
    }
    _print_payload(payload, json_output=json_output)
    return 0 if worst in {"current", "missing"} else 1


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
        rendered, plan = managed_block.remove_block(current, kind=CARE_KIND)
        payload = {
            "target": str(target),
            "backend": "crontab",
            "status": plan.status,
            "action": plan.action,
            "dry_run": dry_run,
        }
        if plan.action == "none":
            _print_payload(payload, json_output=json_output)
            return 0
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
            text = path.read_text(encoding="utf-8")
            match = managed_block.find_block(text, kind=CARE_KIND)
            if match is None:
                skipped.append(name)
                continue
            if dry_run:
                removed.append(name)
                continue
            path.unlink()
            removed.append(name)
    payload = {
        "target": str(target),
        "backend": "systemd",
        "dry_run": dry_run,
        "removed": removed,
        "skipped": skipped,
        "next_commands": ["systemctl --user daemon-reload"],
    }
    _print_payload(payload, json_output=json_output)
    return 0
