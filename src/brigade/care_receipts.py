"""Read-only scheduled care receipt summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import care_cmd as care, runbook_cmd


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
    target: Path, entry: care.CareEntry, *, receipts_by_runbook: dict[str, list[dict[str, Any]]] | None = None
) -> dict[str, Any]:
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
