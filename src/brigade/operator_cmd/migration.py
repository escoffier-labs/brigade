from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import work_cmd
from .adoption import adoption_plan_payload
from .surfaces import (
    _read_latest_surfaces_capture,
    _safe_surface_review_reason,
    _surface_privacy_flags,
    _surface_review_summary,
)


def migration_status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    adoption = adoption_plan_payload(target)
    capture = _read_latest_surfaces_capture(target)
    review_summary = (
        _surface_review_summary(target, capture=capture)
        if capture is not None
        else {
            "surface_count": 0,
            "record_count": 0,
            "reviewed_count": 0,
            "unreviewed_count": 0,
            "stale_review_count": 0,
            "surfaces": [],
        }
    )
    imports = _operator_migration_import_summary(target)
    tasks = _operator_migration_task_summary(target)
    gaps = _operator_migration_gaps(adoption=adoption, capture=capture, review_summary=review_summary, imports=imports)
    if gaps["blocking_count"]:
        status = "needs-setup"
    elif gaps["remaining_count"]:
        status = "in-progress"
    else:
        status = "ready"
    return {
        "target": str(target),
        "status": status,
        "ready": status == "ready",
        "adoption": {
            "status": adoption.get("status"),
            "issue_count": adoption.get("issue_count"),
            "issues": adoption.get("issues"),
        },
        "surfaces": {
            "capture_present": capture is not None,
            "capture_fingerprint": capture.get("source_fingerprint") if isinstance(capture, dict) else None,
            "surface_count": review_summary.get("surface_count"),
            "record_count": review_summary.get("record_count"),
            "reviewed_count": review_summary.get("reviewed_count"),
            "unreviewed_count": review_summary.get("unreviewed_count"),
            "stale_review_count": review_summary.get("stale_review_count"),
            "surfaces": review_summary.get("surfaces"),
        },
        "work": {
            "pending_import_count": imports["pending_count"],
            "pending_imports_by_source": imports["pending_by_source"],
            "pending_imports_by_surface": imports["pending_by_surface"],
            "pending_task_count": tasks["pending_count"],
            "pending_tasks_by_source": tasks["pending_by_source"],
        },
        "gaps": gaps,
        "next_commands": _operator_migration_next_commands(gaps),
        "privacy": _surface_privacy_flags(),
        "boundaries": [
            "Uses redacted adoption, surface capture, review, work import, and task metadata.",
            "Does not include raw scheduler lines, job names, process names, command paths, environment values, host details, or private paths.",
            "Does not start services, activate hooks, mutate schedulers, mutate remotes, ingest memory, migrate secrets, or rotate credentials.",
        ],
    }


def migration_status(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = migration_status_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator migration status: {target}")
    print(f"status: {payload['status']}")
    print(f"adoption: {payload['adoption']['status']} issues={payload['adoption']['issue_count']}")
    print(
        "surfaces: "
        f"records={payload['surfaces']['record_count']} "
        f"reviewed={payload['surfaces']['reviewed_count']} "
        f"unreviewed={payload['surfaces']['unreviewed_count']} "
        f"stale={payload['surfaces']['stale_review_count']}"
    )
    print(f"pending_imports: {payload['work']['pending_import_count']}")
    print(f"pending_tasks: {payload['work']['pending_task_count']}")
    print(f"gaps: blocking={payload['gaps']['blocking_count']} remaining={payload['gaps']['remaining_count']}")
    for gap in payload["gaps"]["items"]:
        print(f"- {gap['severity']} {gap['name']}: {gap['detail']}")
    if payload["next_commands"]:
        print("next:")
        for command in payload["next_commands"]:
            print(f"- {command}")
    return 0


def migration_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = migration_status_payload(target)
    result = {
        "target": payload["target"],
        "ready": payload["ready"],
        "status": payload["status"],
        "blocking_issue_count": payload["gaps"]["blocking_count"],
        "remaining_issue_count": payload["gaps"]["remaining_count"],
        "issues": payload["gaps"]["items"],
        "next_command": payload["next_commands"][0] if payload["next_commands"] else "brigade daily status --target .",
        "privacy": payload["privacy"],
        "summary": {
            "surface_record_count": payload["surfaces"]["record_count"],
            "surface_reviewed_count": payload["surfaces"]["reviewed_count"],
            "surface_unreviewed_count": payload["surfaces"]["unreviewed_count"],
            "pending_operator_import_count": payload["work"]["pending_import_count"],
            "pending_operator_task_count": payload["work"]["pending_task_count"],
        },
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["blocking_issue_count"] == 0 else 1
    print(f"operator migration doctor: {target}")
    print(f"ready: {'yes' if result['ready'] else 'no'}")
    print(f"status: {result['status']}")
    print(f"blocking_issues: {result['blocking_issue_count']}")
    print(f"remaining_issues: {result['remaining_issue_count']}")
    print(f"next: {result['next_command']}")
    for issue in result["issues"]:
        print(f"- {issue.get('severity')} {issue.get('name')}: {issue.get('detail')}")
    return 0 if result["blocking_issue_count"] == 0 else 1


def migration_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = migration_status_payload(target)
    records = _operator_migration_import_records(payload)
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    superseded = _supersede_stale_operator_migration_imports(target, records, dry_run=dry_run)
    superseded_sources = _supersede_stale_operator_source_imports(target, payload, dry_run=dry_run)
    result = {
        "target": str(target),
        "source": "operator-migration",
        "status": payload["status"],
        "dry_run": dry_run,
        "candidate_count": len(records),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "superseded": len(superseded),
        "superseded_import_ids": superseded,
        "superseded_source_imports": len(superseded_sources),
        "superseded_source_import_ids": superseded_sources,
        "imports": imported,
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator migration imports: {target}")
    print(f"status: {payload['status']}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    if skipped_dismissed:
        print(f"dismissed: {len(skipped_dismissed)}")
    if superseded:
        print(f"superseded: {len(superseded)}")
    if superseded_sources:
        print(f"superseded_source_imports: {len(superseded_sources)}")
    for item in imported:
        print(f"- {item.get('id')}: {item.get('text')}")
    return 0


def migration_consolidate(
    *,
    target: Path,
    surface: str | None = None,
    review_status: str | None = None,
    reason: str = "superseded-by-migration-rollup",
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        reason = _safe_surface_review_reason(reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    imports = work_cmd._read_imports(target)
    rollup_ids = [
        str(item.get("id"))
        for item in imports
        if isinstance(item, dict)
        and item.get("status", "pending") == "pending"
        and item.get("source") == "operator-migration"
        and ((item.get("metadata") or {}).get("gap_name") if isinstance(item.get("metadata"), dict) else None)
        in {"surface_reviews_missing", "surface_records_need_owner"}
    ]
    candidates = []
    now = datetime.now(timezone.utc).isoformat()
    for item in imports:
        if (
            not isinstance(item, dict)
            or item.get("status", "pending") != "pending"
            or item.get("source") != "operator-surface-review"
        ):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if surface and metadata.get("surface") != surface:
            continue
        if review_status and metadata.get("review_status") != review_status:
            continue
        candidates.append(item)
    if rollup_ids and not dry_run:
        rollup_id = rollup_ids[0]
        for item in candidates:
            item["status"] = "dismissed"
            item["dismissed_at"] = now
            item["dismiss_reason"] = reason
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            metadata["superseded_by_import_id"] = rollup_id
            metadata["superseded_by_source"] = "operator-migration"
            item["metadata"] = metadata
        if candidates:
            work_cmd._write_imports(target, imports)
    result = {
        "target": str(target),
        "surface": surface,
        "review_status": review_status,
        "dry_run": dry_run,
        "reason": reason,
        "rollup_import_count": len(rollup_ids),
        "rollup_import_id": rollup_ids[0] if rollup_ids else None,
        "candidate_count": len(candidates),
        "dismissed": len(candidates) if rollup_ids and not dry_run else 0,
        "blocked": not bool(rollup_ids),
        "blocker": "no pending operator-migration rollup import found" if not rollup_ids else None,
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if rollup_ids else 1
    print(f"operator migration consolidate: {target}")
    print(f"surface: {surface or 'all'}")
    print(f"review_status: {review_status or 'all'}")
    print(f"rollup_imports: {len(rollup_ids)}")
    print(f"candidates: {len(candidates)}")
    print(f"dismissed: {result['dismissed']}")
    if result["blocker"]:
        print(f"blocker: {result['blocker']}")
    return 0 if rollup_ids else 1


def _operator_migration_import_summary(target: Path) -> dict[str, Any]:
    sources = {"operator-adoption", "operator-surface", "operator-surface-review", "operator-migration"}
    pending = [
        item
        for item in work_cmd._read_imports(target)
        if isinstance(item, dict)
        and item.get("status", "pending") == "pending"
        and str(item.get("source") or "") in sources
    ]
    by_source = Counter(str(item.get("source") or "unknown") for item in pending)
    by_surface = Counter()
    for item in pending:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        surface = metadata.get("surface")
        if isinstance(surface, str) and surface:
            by_surface[surface] += 1
    return {
        "pending_count": len(pending),
        "pending_by_source": dict(sorted(by_source.items())),
        "pending_by_surface": dict(sorted(by_surface.items())),
    }


def _operator_migration_task_summary(target: Path) -> dict[str, Any]:
    sources = {
        "operator-adoption",
        "operator-surface",
        "operator-surface-review",
        "operator-migration",
        "import:operator-adoption",
        "import:operator-surface",
        "import:operator-surface-review",
        "import:operator-migration",
    }
    try:
        ledger = work_cmd._read_task_ledger(target)
    except Exception:
        ledger = {"tasks": []}
    pending = []
    for task in ledger.get("tasks") or []:
        if not isinstance(task, dict) or task.get("status", "pending") != "pending":
            continue
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        source = str(metadata.get("source") or task.get("source") or "")
        if source in sources or str(task.get("text") or "").lower().startswith(
            ("bridge operator", "review external operator", "resolve operator surface")
        ):
            pending.append(task)
    by_source = Counter()
    for task in pending:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        source = str(metadata.get("source") or task.get("source") or "operator-work")
        by_source[source] += 1
    return {"pending_count": len(pending), "pending_by_source": dict(sorted(by_source.items()))}


def _operator_migration_gaps(
    *,
    adoption: dict[str, Any],
    capture: dict[str, Any] | None,
    review_summary: dict[str, Any],
    imports: dict[str, Any],
) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    if adoption.get("status") in {"unmanaged", "needs-adoption"}:
        gaps.append(
            {
                "severity": "blocker",
                "name": "operator_config_not_adopted",
                "detail": "target does not have enough Brigade operator wiring to drive replacement work",
                "suggested_next_command": "brigade operator quickstart --target . --depth workspace --dry-run",
            }
        )
    if capture is None:
        gaps.append(
            {
                "severity": "blocker",
                "name": "surface_capture_missing",
                "detail": "external scheduler/process surfaces have not been captured as redacted local evidence",
                "suggested_next_command": "brigade operator surfaces capture --target . --json",
            }
        )
    unreviewed = int(review_summary.get("unreviewed_count") or 0)
    stale = int(review_summary.get("stale_review_count") or 0)
    if unreviewed:
        gaps.append(
            {
                "severity": "remaining",
                "name": "surface_reviews_missing",
                "detail": f"{unreviewed} external surface record(s) still need redacted ownership review",
                "suggested_next_command": "brigade operator surfaces reviews --target . --json",
            }
        )
    if stale:
        gaps.append(
            {
                "severity": "remaining",
                "name": "surface_reviews_stale",
                "detail": f"{stale} surface review record(s) no longer match the latest capture",
                "suggested_next_command": "brigade operator surfaces capture --target . --json",
            }
        )
    pending = int(imports.get("pending_count") or 0)
    if pending:
        gaps.append(
            {
                "severity": "remaining",
                "name": "operator_migration_imports_pending",
                "detail": f"{pending} operator migration import(s) are pending promotion, dismissal, or deferral",
                "suggested_next_command": "brigade daily status --target .",
            }
        )
    needs_owner = 0
    for surface in review_summary.get("surfaces") or []:
        if isinstance(surface, dict):
            needs_owner += (
                int((surface.get("status_counts") or {}).get("needs-owner") or 0)
                if isinstance(surface.get("status_counts"), dict)
                else 0
            )
    if needs_owner:
        gaps.append(
            {
                "severity": "remaining",
                "name": "surface_records_need_owner",
                "detail": f"{needs_owner} reviewed surface record(s) still need an owner or migration decision",
                "suggested_next_command": "brigade operator surfaces import-issues --target . --json",
            }
        )
    return {
        "blocking_count": sum(1 for gap in gaps if gap["severity"] == "blocker"),
        "remaining_count": sum(1 for gap in gaps if gap["severity"] != "blocker"),
        "items": gaps,
    }


def _operator_migration_next_commands(gaps: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for gap in gaps.get("items") or []:
        if isinstance(gap, dict):
            command = gap.get("suggested_next_command")
            if isinstance(command, str) and command not in commands:
                commands.append(command)
    return commands or ["brigade daily status --target ."]


def _operator_migration_import_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for gap in payload.get("gaps", {}).get("items", []):
        if not isinstance(gap, dict):
            continue
        if gap.get("name") == "operator_migration_imports_pending":
            continue
        if gap.get("severity") == "blocker":
            priority = "high"
        else:
            priority = "normal"
        name = str(gap.get("name") or "operator_migration_gap")
        detail = str(gap.get("detail") or name)
        fingerprint = work_cmd._stable_hash(
            {
                "source": "operator-migration",
                "name": name,
                "detail": detail,
                "status": payload.get("status"),
                "surface_record_count": (payload.get("surfaces") or {}).get("record_count")
                if isinstance(payload.get("surfaces"), dict)
                else None,
                "surface_reviewed_count": (payload.get("surfaces") or {}).get("reviewed_count")
                if isinstance(payload.get("surfaces"), dict)
                else None,
                "surface_unreviewed_count": (payload.get("surfaces") or {}).get("unreviewed_count")
                if isinstance(payload.get("surfaces"), dict)
                else None,
            }
        )
        records.append(
            {
                "text": f"Resolve operator migration gap: {detail}",
                "kind": "task",
                "source": "operator-migration",
                "type": "workflow",
                "priority": priority,
                "template": "vertical-slice",
                "acceptance": [
                    "A fresh `brigade operator migration status --target . --json` shows this gap resolved, reduced, explicitly deferred, or documented with a local reason.",
                    "The resolution uses redacted adoption/surface/work metadata and does not expose raw scheduler lines, job names, process names, command paths, host details, tokens, or environment values.",
                    "Daily status continues to select the next replacement follow-up without status-section issues.",
                ],
                "metadata": {
                    "gap_name": name,
                    "safe_summary": detail,
                    "migration_status": payload.get("status"),
                    "suggested_next_command": gap.get("suggested_next_command"),
                    "source_item_key": f"operator-migration:{name}",
                    "source_fingerprint": fingerprint,
                    "private_fields_omitted": [
                        "raw_crontab_lines",
                        "job_names",
                        "process_names",
                        "command_paths",
                        "environment_values",
                        "host_details",
                    ],
                },
            }
        )
    return records


def _supersede_stale_operator_migration_imports(
    target: Path, records: list[dict[str, Any]], *, dry_run: bool = False
) -> list[str]:
    current_by_identity: dict[tuple[str, str, str], str] = {}
    for record in records:
        identity = work_cmd._import_source_identity(record)
        fingerprint = work_cmd._import_fingerprint(record)
        if identity is not None and isinstance(fingerprint, str):
            current_by_identity[identity] = fingerprint
    if not current_by_identity:
        return []
    imports = work_cmd._read_imports(target)
    superseded: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for item in imports:
        if (
            not isinstance(item, dict)
            or item.get("status", "pending") != "pending"
            or item.get("source") != "operator-migration"
        ):
            continue
        identity = work_cmd._import_source_identity(item)
        if identity not in current_by_identity:
            continue
        current = current_by_identity[identity]
        existing = work_cmd._import_fingerprint(item)
        if existing == current:
            continue
        item_id = str(item.get("id") or "")
        if item_id:
            superseded.append(item_id)
        if not dry_run:
            item["status"] = "dismissed"
            item["dismissed_at"] = now
            item["dismiss_reason"] = "superseded-by-current-migration-rollup"
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            metadata["superseded_by_source"] = "operator-migration"
            metadata["current_source_fingerprint"] = current
            item["metadata"] = metadata
    if superseded and not dry_run:
        work_cmd._write_imports(target, imports)
    return superseded


def _supersede_stale_operator_source_imports(
    target: Path, payload: dict[str, Any], *, dry_run: bool = False
) -> list[str]:
    current_adoption_issue_keys = {
        f"operator-adoption:{issue.get('name')}"
        for issue in (payload.get("adoption") or {}).get("issues") or []
        if isinstance(issue, dict) and isinstance(issue.get("name"), str)
    }
    reviewed_surfaces = set()
    for row in (payload.get("surfaces") or {}).get("surfaces") or []:
        if not isinstance(row, dict):
            continue
        surface = row.get("surface")
        if (
            isinstance(surface, str)
            and int(row.get("record_count") or 0) > 0
            and int(row.get("unreviewed_count") or 0) == 0
            and int(row.get("stale_review_count") or 0) == 0
        ):
            reviewed_surfaces.add(surface)
    migration_gap_names = {
        gap.get("name") for gap in (payload.get("gaps") or {}).get("items") or [] if isinstance(gap, dict)
    }
    has_surface_rollup = "surface_records_need_owner" in migration_gap_names
    imports = work_cmd._read_imports(target)
    superseded: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for item in imports:
        if not isinstance(item, dict) or item.get("status", "pending") != "pending":
            continue
        source = item.get("source")
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        source_key = metadata.get("source_item_key")
        should_supersede = False
        reason = "superseded-by-current-migration-status"
        if (
            source == "operator-adoption"
            and isinstance(source_key, str)
            and source_key not in current_adoption_issue_keys
        ):
            should_supersede = True
        elif source == "operator-surface" and has_surface_rollup:
            surface = metadata.get("surface")
            if isinstance(surface, str) and surface in reviewed_surfaces:
                should_supersede = True
                reason = "superseded-by-reviewed-surface-rollup"
        if not should_supersede:
            continue
        item_id = str(item.get("id") or "")
        if item_id:
            superseded.append(item_id)
        if not dry_run:
            item["status"] = "dismissed"
            item["dismissed_at"] = now
            item["dismiss_reason"] = reason
            metadata["superseded_by_source"] = "operator-migration"
            item["metadata"] = metadata
    if superseded and not dry_run:
        work_cmd._write_imports(target, imports)
    return superseded
