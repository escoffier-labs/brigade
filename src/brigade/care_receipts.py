"""Read-only scheduled care receipt summaries."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import runbook_cmd

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

# Timer units in these states exist on disk as Brigade's record of an
# installed entry. Mirrors care_cmd._INSTALLED_CARE_STATUSES.
_INSTALLED_TIMER_STATUSES = frozenset({"current", "stale", "tampered"})
_DEFAULT_CADENCE = "scheduled"


def _schedule_period_seconds(entry_id: str) -> int:
    """One schedule period for an entry, from the shared cadence thresholds."""
    from .memory_operations import CADENCE_STALE_SECONDS, CARE_CADENCE

    cadence = CARE_CADENCE.get(entry_id, _DEFAULT_CADENCE)
    return int(CADENCE_STALE_SECONDS.get(cadence) or CADENCE_STALE_SECONDS[_DEFAULT_CADENCE])


def _installed_timer_units(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    systemd = (status.get("backends") or {}).get("systemd") or {}
    units: dict[str, dict[str, Any]] = {}
    for unit in systemd.get("units") or []:
        if not isinstance(unit, dict):
            continue
        name = str(unit.get("name") or "")
        if name.endswith(".timer") and unit.get("status") in _INSTALLED_TIMER_STATUSES:
            units[name] = unit
    return units


def _timer_for_entry(units: dict[str, dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    suffix = f"-{entry_id}.timer"
    for name, unit in units.items():
        if name.endswith(suffix):
            return unit
    return None


def _unit_age_seconds(path: Path, now: float) -> float | None:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def _warning(code: str, entry_id: str, detail: str, fix_command: str) -> dict[str, str]:
    return {"code": code, "entry": entry_id, "detail": detail, "fix_command": fix_command}


def _file_only_status(target: Path, home: Path | None) -> dict[str, Any]:
    """Care status data assembled from unit files and receipts, with no subprocess."""
    from . import care_cmd as care

    target = target.expanduser().resolve()
    installed = care._installed_systemd_entries(target=target, home=home)
    entries = installed or care.CARE_ENTRIES
    identity = care._target_identity(target)
    unit_dir = care._systemd_user_dir(home)
    units: list[dict[str, Any]] = []
    for entry in entries:
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
            }
        },
        "entries": [care._entry_status(target, entry, receipts_by_runbook=receipts_by_runbook) for entry in entries],
    }


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
    same data is assembled from unit files and runbook receipts. No subprocess
    is ever started, so ``brigade work brief`` stays fast.
    """
    from .extras import enabled as extras_enabled

    view = status if status is not None else _file_only_status(target, home)
    crontab = (view.get("backends") or {}).get("crontab") or {}
    crontab_installed = (
        bool(crontab.get("target_match")) and str(crontab.get("status") or "") in _INSTALLED_TIMER_STATUSES
    )
    units = _installed_timer_units(view)
    extras_available = extras_enabled() if extras_on is None else extras_on
    stamp = time.time() if now is None else now
    warnings: list[dict[str, str]] = []
    for entry in view.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        timer = _timer_for_entry(units, entry_id)
        timer_name = str((timer or {}).get("name") or "")
        if timer is not None and timer.get("enabled") is False:
            warnings.append(
                _warning(
                    SCHEDULE_WARNING_TIMER_DISABLED,
                    entry_id,
                    f"timer {timer_name} is installed but not enabled, so it never fires",
                    f"systemctl --user enable --now {timer_name}",
                )
            )
        if timer is not None and entry.get("kind") == "runbook" and entry.get("last_receipt") is None:
            age = _unit_age_seconds(Path(str(timer.get("path") or "")), stamp)
            if age is not None and age > _schedule_period_seconds(entry_id):
                service_name = timer_name.removesuffix(".timer") + ".service"
                warnings.append(
                    _warning(
                        SCHEDULE_WARNING_NEVER_RAN,
                        entry_id,
                        f"timer unit {timer_name} was written {int(age // 86400)} days ago "
                        "and no run has ever recorded a receipt",
                        f"systemctl --user start {service_name}",
                    )
                )
        if entry.get("kind") == "runbook" and not extras_available:
            enabled_entry = (timer is not None and bool(timer.get("enabled"))) or crontab_installed
            if enabled_entry:
                warnings.append(
                    _warning(
                        SCHEDULE_WARNING_EXTRAS_OFF,
                        entry_id,
                        "runbook entry is enabled but `brigade extras` is off, so it exits 2 without writing a receipt",
                        "brigade extras on",
                    )
                )
    return {"count": len(warnings), "warnings": warnings}
