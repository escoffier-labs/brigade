"""Backend-specific schedule parsing and systemd calendar inspection for care."""

from __future__ import annotations

import plistlib
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable


SCHTASKS_WEEKDAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
_CRON_FIELD = re.compile(r"^[0-9*/,-]+$")
_KNOWN_MISELEDGER_TIMERS = {"brigadeclaw-daily-report": "brigadeclaw-daily-report.timer"}
_SYSTEMD_ON_CALENDAR = re.compile(
    r"(?:^|[\s{])OnCalendar=(?P<calendar>.*?)(?=\s*;\s*[A-Za-z_][A-Za-z0-9_]*=|\s*})",
    re.DOTALL,
)


def schtasks_schedule_flags(schedule: str) -> str:
    """Map the supported cron subset to Task Scheduler flags."""
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


def launchd_interval_seconds(schedule: str) -> int | None:
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


def _validate_cron(schedule: str) -> None:
    fields = schedule.split()
    if len(fields) != 5 or any(not _CRON_FIELD.fullmatch(field) for field in fields):
        raise ValueError(f"invalid care cron schedule: {schedule}")
    for field, low, high in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7), strict=True):
        for part in field.split(","):
            base, slash, step = part.partition("/")
            if slash and (not step.isdigit() or int(step) <= 0):
                raise ValueError(f"invalid care cron schedule: {schedule}")
            bounds = base.split("-", 1)
            if base == "*":
                continue
            if not all(value.isdigit() and low <= int(value) <= high for value in bounds):
                raise ValueError(f"invalid care cron schedule: {schedule}")
            if len(bounds) == 2 and int(bounds[0]) > int(bounds[1]):
                raise ValueError(f"invalid care cron schedule: {schedule}")


def _validate_launchd(schedule: str) -> None:
    if launchd_interval_seconds(schedule) is not None:
        return
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported care schedule for launchd: {schedule}")
    minute, hour, day, month, weekday = fields
    try:
        minute_i, hour_i = int(minute), int(hour)
        weekday_i = None if weekday == "*" else int(weekday)
    except ValueError as exc:
        raise ValueError(f"unsupported care schedule for launchd: {schedule}") from exc
    if day != "*" or month != "*" or not (0 <= minute_i <= 59 and 0 <= hour_i <= 23):
        raise ValueError(f"unsupported care schedule for launchd: {schedule}")
    if weekday_i is not None and not 0 <= weekday_i <= 7:
        raise ValueError(f"unsupported care schedule for launchd: {schedule}")


def _validate_systemd(calendar: str) -> None:
    try:
        result = subprocess.run(
            ["systemd-analyze", "calendar", calendar],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise ValueError("cannot validate systemd calendar: systemd-analyze command not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("cannot validate systemd calendar: systemd-analyze timed out") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "invalid calendar").strip()
        raise ValueError(f"invalid systemd calendar: {detail}")


def validate_schedule(backend: str, value: str) -> None:
    """Reject a schedule that the selected scheduler cannot represent."""
    if not value.strip():
        raise ValueError("care schedule must not be empty")
    if backend == "systemd":
        _validate_systemd(value)
    elif backend == "launchd":
        _validate_launchd(value)
    elif backend == "schtasks":
        schtasks_schedule_flags(value)
    elif backend == "crontab":
        _validate_cron(value)


def parse_schedule_specs(
    specs: list[str] | None,
    *,
    selected_ids: Iterable[str],
    known_ids: Iterable[str],
    backend: str,
) -> dict[str, str]:
    selected = set(selected_ids)
    known = set(known_ids)
    parsed: dict[str, str] = {}
    for raw in specs or []:
        entry_id, sep, value = raw.partition("=")
        entry_id, value = entry_id.strip(), value.strip()
        if not sep or not entry_id:
            raise ValueError("care --schedule must use JOB_ID=VALUE")
        if entry_id not in known:
            raise ValueError(f"unknown care entry in --schedule: {entry_id}")
        if entry_id not in selected:
            raise ValueError(f"care --schedule requires selected entry: {entry_id}")
        if entry_id in parsed:
            raise ValueError(f"duplicate care schedule for entry: {entry_id}")
        validate_schedule(backend, value)
        parsed[entry_id] = value
    return parsed


def apply_schedule_overrides(entries: tuple[Any, ...], overrides: dict[str, str], *, backend: str) -> tuple[Any, ...]:
    field = "on_calendar" if backend == "systemd" else "schedule"
    return tuple(replace(entry, **{field: overrides.get(entry.entry_id, getattr(entry, field))}) for entry in entries)


def install_schedule_overrides(
    entries: tuple[Any, ...],
    *,
    schedule_specs: list[str] | None,
    backend: str,
    known_ids: Iterable[str],
    systemd_unit_dir: Path | None = None,
    launchd_directory: Path | None = None,
    identity: str | None = None,
    crontab_body: str | None = None,
) -> tuple[Any, ...]:
    """Apply requested schedules and readable managed schedules before rendering.

    Task Scheduler is a printed-plan backend, so it has no installed schedule
    to read. An unreadable crontab deliberately falls back to its normal install
    path, which reports the read failure before any scheduler write.
    """
    overrides = parse_schedule_specs(
        schedule_specs, selected_ids=(entry.entry_id for entry in entries), known_ids=known_ids, backend=backend
    )
    persisted: dict[str, str] = {}
    if identity and backend == "systemd" and systemd_unit_dir is not None:
        persisted = persisted_systemd_schedules(entries, unit_dir=systemd_unit_dir, identity=identity)
    elif identity and backend == "launchd" and launchd_directory is not None:
        persisted = persisted_launchd_schedules(entries, directory=launchd_directory, identity=identity)
    elif backend == "crontab" and crontab_body is not None:
        persisted = persisted_crontab_schedules(entries, crontab_body)
    persisted.update(overrides)
    return apply_schedule_overrides(entries, persisted, backend=backend)


def systemd_managed_calendar(text: str) -> str | None:
    return _canonical_systemd_calendars(systemd_managed_calendars(text))


def systemd_managed_calendars(text: str) -> tuple[str, ...]:
    in_timer = False
    calendars: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_timer = stripped == "[Timer]"
            continue
        if in_timer and stripped.startswith("OnCalendar="):
            value = stripped.split("=", 1)[1].strip()
            if value:
                calendars.append(" ".join(value.split()))
    return tuple(calendars)


def persisted_systemd_schedules(entries: tuple[Any, ...], *, unit_dir: Path, identity: str) -> dict[str, str]:
    schedules: dict[str, str] = {}
    for entry in entries:
        path = unit_dir / f"brigade-care-{identity}-{entry.entry_id}.timer"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            calendars = systemd_managed_calendars(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if calendars:
            schedules[entry.entry_id] = "\n".join(calendars)
    return schedules


def persisted_launchd_schedules(entries: tuple[Any, ...], *, directory: Path, identity: str) -> dict[str, str]:
    schedules: dict[str, str] = {}
    for entry in entries:
        path = directory / f"dev.brigade.care.{identity}.{entry.entry_id}.plist"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        interval = payload.get("StartInterval")
        if isinstance(interval, int) and interval > 0 and interval % 60 == 0:
            schedules[entry.entry_id] = f"*/{interval // 60} * * * *"
            continue
        calendar = payload.get("StartCalendarInterval")
        if not isinstance(calendar, dict):
            continue
        minute, hour = calendar.get("Minute"), calendar.get("Hour")
        weekday = calendar.get("Weekday", "*")
        if isinstance(minute, int) and isinstance(hour, int) and isinstance(weekday, int | str):
            schedules[entry.entry_id] = f"{minute} {hour} * * {weekday}"
    return schedules


def persisted_crontab_schedules(entries: tuple[Any, ...], body: str) -> dict[str, str]:
    schedules: dict[str, str] = {}
    for line in body.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6:
            continue
        cron = " ".join(fields[:5])
        for entry in entries:
            if entry.runbook_rel and entry.runbook_rel in fields[5]:
                schedules[entry.entry_id] = cron
    return schedules


def effective_systemd_calendar(timer_name: str) -> tuple[str | None, str | None]:
    """Read the resolved timer calendar, which includes systemd drop-ins."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", timer_name, "--property=TimersCalendar", "--value"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError:
        return None, "systemctl command not found"
    except subprocess.TimeoutExpired:
        return None, "systemctl --user show timed out"
    if result.returncode != 0:
        return None, (result.stderr or result.stdout or f"systemctl exited {result.returncode}").strip()
    output = (result.stdout or "").strip()
    if not output:
        return None, "systemd returned no timer calendar"
    calendar = _canonical_systemd_calendars(match.group("calendar") for match in _SYSTEMD_ON_CALENDAR.finditer(output))
    if calendar:
        return calendar, None
    if "OnCalendar=" in output:
        return None, "systemd returned malformed timer calendar"
    return None, "systemd returned no timer calendar"


def _canonical_systemd_calendars(calendars: Iterable[str]) -> str | None:
    values = [" ".join(calendar.split()) for calendar in calendars]
    return "; ".join(value for value in values if value) or None


def miseledger_schedule_warnings(timer_calendars: dict[str, str | None]) -> list[dict[str, Any]]:
    """Report known same-host writers that systemd schedules on one calendar."""
    calendars = {entry_id: value for entry_id, value in timer_calendars.items() if value}
    if "evidence-crawl" in calendars:
        for entry_id, timer_name in _KNOWN_MISELEDGER_TIMERS.items():
            value, _ = effective_systemd_calendar(timer_name)
            if value:
                calendars[entry_id] = value
    grouped: dict[str, list[str]] = {}
    for entry_id, calendar_set in calendars.items():
        for calendar in calendar_set.split("; "):
            grouped.setdefault(calendar, []).append(entry_id)
    return [
        {"kind": "miseledger-calendar-collision", "calendar": calendar, "entries": entry_ids}
        for calendar, entry_ids in sorted(grouped.items())
        if len(entry_ids) > 1
    ]
