"""Read-only scheduled care receipt summaries."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import managed_block, runbook_cmd

if TYPE_CHECKING:
    from .care_cmd import CareEntry

# care_cmd re-exports this module at import time, so care_cmd is imported
# inside each function that needs it, never at module top.


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


def _entry_status(
    target: Path, entry: CareEntry, *, receipts_by_runbook: dict[str, list[dict[str, Any]]] | None = None
) -> dict[str, Any]:
    from . import care_cmd as care

    payload: dict[str, Any] = {
        "id": entry.entry_id,
        "schedule": entry.schedule,
        "on_calendar": entry.on_calendar,
        "kind": entry.kind,
        "description": entry.description,
    }
    if entry.kind in ("reflect", "verify"):
        return care.hub_job_status(target, entry, payload)
    assert entry.kind == "runbook"
    assert entry.runbook_rel is not None
    path = target / entry.runbook_rel
    payload["runbook"] = entry.runbook_rel
    payload["runbook_id"] = entry.runbook_id
    payload["runbook_present"] = path.is_file()
    receipts = (
        _runbook_receipts(target, entry.runbook_id or "")
        if receipts_by_runbook is None
        else receipts_by_runbook.get(entry.runbook_id or "", [])
    )
    consecutive = _consecutive_failed_receipts(receipts)
    payload["last_receipt"] = _receipt_summary(receipts[0] if receipts else None)
    payload["consecutive_failures"] = consecutive
    payload["repeated_failure"] = consecutive >= care.REPEATED_FAILURE_THRESHOLD
    payload.update(care.care_patrol.receipt_fields(target, entry, payload["last_receipt"]))
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
    from . import care_cmd as care

    target = target.expanduser().resolve()
    # Share one newest-first read across the catalog. Keep the entire leading
    # failure streak so the reported consecutive count remains exact.
    receipts_by_runbook: dict[str, list[dict[str, Any]]] = {}
    closed: set[str] = set()
    runbook_ids = {entry.runbook_id for entry in care._catalog_by_id().values() if entry.runbook_id}
    for receipt in runbook_cmd._run_receipts(target, runbook_ids=runbook_ids):
        runbook_id = str(receipt.get("runbook_id") or "")
        if not runbook_id or runbook_id in closed:
            continue
        receipts_by_runbook.setdefault(runbook_id, []).append(receipt)
        if str(receipt.get("status") or "") != "failed":
            closed.add(runbook_id)
    seen_runbooks: set[str] = set()
    entry_payloads: list[dict[str, Any]] = []
    for entry in care._catalog_by_id().values():
        key = entry.runbook_id or entry.entry_id
        if key in seen_runbooks:
            continue
        seen_runbooks.add(key)
        entry_payloads.append(care._entry_status(target, entry, receipts_by_runbook=receipts_by_runbook))
    records = _repeated_failure_records(entry_payloads)
    return {
        "threshold": care.REPEATED_FAILURE_THRESHOLD,
        "count": len(records),
        "entries": records,
    }


SCHEDULE_WARNING_TIMER_DISABLED = "timer-disabled"
SCHEDULE_WARNING_NEVER_RAN = "never-ran"
SCHEDULE_WARNING_EXTRAS_OFF = "extras-off"

# Registrations in these states exist on disk as Brigade's record of an
# installed entry. Mirrors care_cmd._INSTALLED_CARE_STATUSES.
_INSTALLED_STATUSES = frozenset({"current", "stale", "tampered"})
_DEFAULT_CADENCE = "scheduled"
STATUS_NOT_INSPECTED = "not-inspected"

# A crontab is readable without a subprocess only when the spool file is
# (root, or a permissive host). Tests replace this tuple.
_CRONTAB_SPOOL_DIRS = (Path("/var/spool/cron/crontabs"), Path("/var/spool/cron"), Path("/usr/lib/cron/tabs"))


def _schedule_period_seconds(entry_id: str) -> int:
    """One schedule period for an entry, from the shared cadence thresholds."""
    from .memory_operations import CADENCE_STALE_SECONDS, CARE_CADENCE

    cadence = CARE_CADENCE.get(entry_id, _DEFAULT_CADENCE)
    return int(CADENCE_STALE_SECONDS.get(cadence) or CADENCE_STALE_SECONDS[_DEFAULT_CADENCE])


def _installed_by_name(status: dict[str, Any], backend: str, key: str, suffix: str) -> dict[str, dict[str, Any]]:
    """Installed registrations of one backend, keyed by name (``units`` or ``registrations``)."""
    section = (status.get("backends") or {}).get(backend) or {}
    found: dict[str, dict[str, Any]] = {}
    for item in section.get(key) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name.endswith(suffix) and item.get("status") in _INSTALLED_STATUSES:
            found[name] = item
    return found


def _age_seconds(path: Path, now: float) -> float | None:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def _age_text(seconds: float) -> str:
    """Whole days at a day or more, otherwise whole hours."""
    count, unit = (int(seconds // 86400), "day") if seconds >= 86400 else (int(seconds // 3600), "hour")
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _warning(code: str, entry_id: str, detail: str, fix_command: str) -> dict[str, str]:
    return {"code": code, "entry": entry_id, "detail": detail, "fix_command": fix_command}


def _crontab_spool_text() -> str | None:
    try:
        import pwd

        user = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return None
    for directory in _CRONTAB_SPOOL_DIRS:
        try:
            return (directory / user).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _crontab_file_status(target: Path) -> dict[str, Any]:
    """Crontab backend state from the spool file, or ``not-inspected`` without a subprocess."""
    from . import care_cmd as care

    text = _crontab_spool_text()
    if text is None:
        return {"backend": "crontab", "status": STATUS_NOT_INSPECTED, "target_match": False}
    parsed = managed_block.parse_blocks(text, kind=care.CARE_KIND, style=care.CARE_MARKER_STYLE)
    installed = care._workspace_from_care_body(parsed.body) if parsed.status == "ok" else None
    return {
        "backend": "crontab",
        "status": "current" if parsed.status == "ok" else "missing",
        "target_match": care._target_matches(installed, target),
    }


def _launchd_file_status(target: Path, home: Path | None) -> dict[str, Any]:
    from . import care_cmd as care

    directory = care._launchd_dir(home)
    identity = care._target_identity(target)
    registrations: list[dict[str, Any]] = []
    if directory.is_dir():
        for entry in care._catalog_by_id().values():
            name = f"dev.brigade.care.{identity}.{entry.entry_id}.plist"
            if care._path_exists_or_is_symlink(directory / name):
                registrations.append({"name": name, "entry": entry.entry_id, "status": "current"})
    return {
        "backend": "launchd",
        "status": "current" if registrations else "missing",
        "registrations": registrations,
    }


def _file_only_status(target: Path, home: Path | None) -> dict[str, Any]:
    """Care status data assembled from unit files and receipts, with no subprocess."""
    from . import care_cmd as care

    target = target.expanduser().resolve()
    identity = care._target_identity(target)
    unit_dir = care._systemd_user_dir(home)
    installed_systemd = care._installed_systemd_entries(target=target, home=home)
    units: list[dict[str, Any]] = []
    for entry in installed_systemd or care.CARE_ENTRIES:
        name = f"brigade-care-{identity}-{entry.entry_id}.timer"
        timer = unit_dir / name
        if not (timer.is_file() or timer.is_symlink()):
            continue
        units.append(
            {
                "name": name,
                "path": str(timer),
                "status": "current",
                "enabled": care._systemd_timer_is_enabled(unit_dir=unit_dir, timer_name=name),
            }
        )
    launchd = _launchd_file_status(target, home)
    crontab = _crontab_file_status(target)
    by_id = care._catalog_by_id()
    ids = [entry.entry_id for entry in installed_systemd]
    ids += [str(reg["entry"]) for reg in launchd["registrations"]]
    if crontab["status"] == "current" and crontab["target_match"]:
        ids += [entry.entry_id for entry in care.care_patrol.select(care.CARE_ENTRIES, None, "crontab", care.CareEntry)]
    entries = [by_id[entry_id] for entry_id in dict.fromkeys(ids) if entry_id in by_id] or list(care.CARE_ENTRIES)
    receipts_by_runbook: dict[str, list[dict[str, Any]]] = {}
    runbook_ids = {entry.runbook_id for entry in entries if entry.runbook_id}
    for receipt in runbook_cmd._run_receipts(target, runbook_ids=runbook_ids):
        receipts_by_runbook.setdefault(str(receipt.get("runbook_id") or ""), []).append(receipt)
    return {
        "backends": {
            "systemd": {
                "status": "current" if units else "missing",
                "target_match": bool(units),
                "unit_dir": str(unit_dir),
                "units": units,
            },
            "crontab": crontab,
            "launchd": launchd,
        },
        "entries": [care._entry_status(target, entry, receipts_by_runbook=receipts_by_runbook) for entry in entries],
    }


def status_schedule_health(payload: dict[str, Any], *, target: Path, home: Path | None = None) -> dict[str, Any]:
    """Schedule health for one backend's ``care status`` payload (systemd, crontab, or launchd)."""
    view = {"backends": {str(payload.get("backend") or ""): payload}, "entries": payload.get("entries") or []}
    return schedule_health(target, status=view, home=home)


def schedule_warning_lines(health: dict[str, Any]) -> list[str]:
    return [
        f"schedule_warning: {item['entry']} {item['code']}: {item['detail']}; run `{item['fix_command']}`"
        for item in health.get("warnings") or []
    ]


def schedule_health(
    target: Path,
    *,
    status: dict[str, Any] | None = None,
    home: Path | None = None,
    extras_on: bool | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Warn about installed care entries that cannot produce a receipt (#2589).

    Pass *status* to reuse an existing ``care status`` payload; otherwise the
    same data is assembled from unit files, LaunchAgents plists, the crontab spool
    file when readable, and runbook receipts. No subprocess is ever started, so
    ``brigade work brief`` stays fast. The result also carries ``installed`` (how
    many entries were found scheduled) and ``not_inspected`` (backends this file-only
    read could not look at), so callers never report a silent pass as healthy.
    """
    from . import care_cmd as care
    from .extras import enabled as extras_enabled

    target = target.expanduser().resolve()
    view = status if status is not None else _file_only_status(target, home)
    backends = view.get("backends") or {}
    crontab = backends.get("crontab") or {}
    crontab_installed = bool(crontab.get("target_match")) and str(crontab.get("status") or "") in _INSTALLED_STATUSES
    not_inspected = (
        [name for name, section in backends.items() if (section or {}).get("status") == STATUS_NOT_INSPECTED]
        if status is None
        else []
    )
    identity = care._target_identity(target)
    timers = _installed_by_name(view, "systemd", "units", ".timer")
    plists = _installed_by_name(view, "launchd", "registrations", ".plist")
    extras_available = extras_enabled() if extras_on is None else extras_on
    stamp = time.time() if now is None else now
    warnings: list[dict[str, str]] = []
    installed = 0
    for entry in view.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        timer_name = f"brigade-care-{identity}-{entry_id}.timer"
        timer = timers.get(timer_name)
        plist_name = f"dev.brigade.care.{identity}.{entry_id}.plist"
        plist = plists.get(plist_name)
        installed += 1 if (timer is not None or plist is not None or crontab_installed) else 0
        timer_enabled = timer is not None and timer.get("enabled") is not False
        if timer is not None and not timer_enabled:
            warnings.append(
                _warning(
                    SCHEDULE_WARNING_TIMER_DISABLED,
                    entry_id,
                    f"timer {timer_name} is installed but not enabled, so it never fires",
                    f"systemctl --user enable --now {timer_name}",
                )
            )
        is_runbook = entry.get("kind") == "runbook"
        if is_runbook and not extras_available and (timer_enabled or plist is not None or crontab_installed):
            warnings.append(
                _warning(
                    SCHEDULE_WARNING_EXTRAS_OFF,
                    entry_id,
                    "runbook entry is enabled but `brigade extras` is off, so it exits 2 without writing a receipt",
                    "brigade extras on",
                )
            )
        # Only an enabled schedule with extras on can reasonably have run by now;
        # a disabled timer or extras-off entry is already covered above.
        if not (is_runbook and extras_available and entry.get("last_receipt") is None):
            continue
        if timer_enabled:
            path = Path(str(timer.get("path") or "")) if timer else Path()
            fix = f"systemctl --user start {timer_name.removesuffix('.timer')}.service"
            subject = f"timer unit {timer_name}"
        elif plist is not None:
            path = care._launchd_dir(home) / plist_name
            fix = f"launchctl load -w {path}"
            subject = f"LaunchAgent {plist_name}"
        else:
            continue
        age = _age_seconds(path, stamp)
        if timer_enabled:
            # Count from when the timer was enabled, not when its unit was written:
            # a months-old unit that was just linked has not had a period to run yet.
            link = path.parent / "timers.target.wants" / timer_name
            try:
                enabled_age = stamp - link.lstat().st_mtime
            except OSError:
                enabled_age = None
            if age is not None and enabled_age is not None:
                age = min(age, enabled_age)
        if age is not None and age > _schedule_period_seconds(entry_id):
            warnings.append(
                _warning(
                    SCHEDULE_WARNING_NEVER_RAN,
                    entry_id,
                    f"{subject} was written or enabled {_age_text(age)} ago and no run has ever recorded a receipt",
                    fix,
                )
            )
    return {"count": len(warnings), "warnings": warnings, "installed": installed, "not_inspected": not_inspected}
