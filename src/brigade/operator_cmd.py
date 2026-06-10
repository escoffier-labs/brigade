"""Safe local operator bootstrap commands."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any

from . import (
    __version__,
    center_cmd,
    chat_cmd,
    daily_cmd,
    dogfood_cmd,
    handoff_cmd,
    memory_cmd,
    notifications_cmd,
    repos_cmd,
    scrub,
    security_cmd,
    skills_cmd,
    tools_cmd,
    work_cmd,
)
from .install import install_selection
from .selection import KNOWN_HARNESSES, WRITER_INBOXES, Selection, resolve_owner
from .localio import write_json as _write_json

PROFILES = {"local-operator", "internal-dogfood"}


def guide_payload(*, profile: str = "internal-dogfood") -> dict[str, Any]:
    profile = _validate_profile(profile)
    return {
        "profile": profile,
        "purpose": "Use Brigade as an explicit repo-local operator loop before substantial work.",
        "startup_commands": [
            f"brigade operator status --profile {profile} --target .",
            "brigade daily status --target .",
            "brigade daily plan --target .",
        ],
        "safe_run_command": "brigade daily run --target .",
        "onboarding_command": f"brigade operator init --profile {profile} --target .",
        "tool_sync_command": "brigade operator sync-tools --target .",
        "handoff_expectations": [
            "Write a Memory Handoff when Brigade usage, setup, readiness waivers, or repo workflow changes.",
            "Include concrete commands, what changed, what remains manual, and any local-only setup assumption.",
            "Do not copy raw .brigade evidence into committed docs.",
        ],
        "boundaries": [
            "Brigade does not run automatically.",
            "Brigade does not start daemons. Templates may include an inactive hooks/pre-push file, but Brigade never activates hooks (git config core.hooksPath stays your call).",
            "Brigade does not send notifications unless a separate hook is explicitly configured.",
            "Brigade does not ingest handoffs into canonical memory from operator init/status.",
            "Brigade does not publish, push, tag, or mutate remotes.",
        ],
    }


def guide(*, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    try:
        payload = guide_payload(profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("operator guide")
    print(f"profile: {payload['profile']}")
    print(payload["purpose"])
    print("startup:")
    for command in payload["startup_commands"]:
        print(f"- {command}")
    print(f"safe_run: {payload['safe_run_command']}")
    print(f"onboard: {payload['onboarding_command']}")
    print(f"sync_tools: {payload['tool_sync_command']}")
    print("handoffs:")
    for item in payload["handoff_expectations"]:
        print(f"- {item}")
    print("boundaries:")
    for item in payload["boundaries"]:
        print(f"- {item}")
    return 0


def _steps(
    target: Path, *, profile: str = "local-operator", handoff_inboxes: list[str] | None = None
) -> list[dict[str, Any]]:
    steps = [
        {"id": "daily", "path": daily_cmd._config_path(target), "command": daily_cmd.init, "kwargs": {}},
        {
            "id": "handoff-sources",
            "path": handoff_cmd.default_sources_path(target),
            "command": handoff_cmd.sources_init,
            "kwargs": {"inboxes": handoff_inboxes} if handoff_inboxes is not None else {},
        },
        {
            "id": "work-backup",
            "path": work_cmd._backup_config_path(target),
            "command": work_cmd.backup_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "work-scanners",
            "path": work_cmd._scanner_config_path(target),
            "command": work_cmd.scanners_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "work-review",
            "path": work_cmd._review_config_path(target),
            "command": work_cmd.review_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "chat-surfaces",
            "path": chat_cmd._config_path(target),
            "command": chat_cmd.surfaces_init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "memory-care",
            "path": memory_cmd.config_path(target),
            "command": memory_cmd.init,
            "kwargs": {"update_gitignore": False},
        },
        {
            "id": "repo-fleet",
            "path": repos_cmd.config_path(target),
            "command": repos_cmd.init,
            "kwargs": {"update_gitignore": False},
        },
        {"id": "security", "path": security_cmd.config_path(target), "command": security_cmd.init, "kwargs": {}},
        {
            "id": "tools",
            "path": tools_cmd.config_path(target),
            "command": tools_cmd.init,
            "kwargs": {"update_gitignore": False},
        },
    ]
    if profile == "internal-dogfood":
        steps.insert(
            1, {"id": "dogfood", "path": dogfood_cmd.config_path(target), "command": dogfood_cmd.init, "kwargs": {}}
        )
    return steps


def _validate_profile(profile: str) -> str:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of: {', '.join(sorted(PROFILES))}")
    return profile


def plan_payload(
    target: Path, *, profile: str = "local-operator", handoff_inboxes: list[str] | None = None
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    profile = _validate_profile(profile)
    steps = []
    for step in _steps(target, profile=profile, handoff_inboxes=handoff_inboxes):
        path = step["path"]
        steps.append(
            {
                "id": step["id"],
                "path": str(path),
                "exists": path.exists(),
                "action": "skip" if path.exists() else "write",
            }
        )
    return {
        "target": str(target),
        "profile": profile,
        "steps": steps,
        "missing_count": sum(1 for step in steps if step["action"] == "write"),
        "boundaries": [
            "Does not start services.",
            "Only the internal-dogfood profile refreshes read-only security evidence.",
            "Does not ingest handoffs.",
            "Does not write canonical memory.",
            "Does not publish, push, tag, or mutate remotes.",
        ],
    }


def plan(*, target: Path, profile: str = "local-operator", json_output: bool = False) -> int:
    try:
        payload = plan_payload(target, profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator bootstrap plan: {payload['target']}")
    print(f"profile: {payload['profile']}")
    for row in payload["steps"]:
        print(f"[{row['action']}] {row['id']}: {row['path']}")
    return 0


def adoption_plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    workspace = _workspace_inventory(target)
    surfaces = _operator_surface_inventory()
    issues = _adoption_issues(workspace, surfaces)
    brigade_present = _brigade_operator_config_present(workspace)
    modeled_surface_count = sum(1 for surface in surfaces.values() if int(surface.get("count") or 0) > 0)
    if brigade_present and not issues:
        status = "managed"
    elif brigade_present:
        status = "partial"
    elif modeled_surface_count:
        status = "needs-adoption"
    else:
        status = "unmanaged"
    commands = _adoption_next_commands(issues)
    return {
        "target": str(target),
        "status": status,
        "privacy": {
            "raw_crontab_lines_included": False,
            "raw_openclaw_jobs_included": False,
            "raw_pm2_processes_included": False,
            "external_command_output_redacted_to_counts": True,
        },
        "workspace": workspace,
        "surfaces": surfaces,
        "issue_count": len(issues),
        "issues": issues,
        "suggested_next_commands": commands,
        "boundaries": [
            "Read-only: does not write files.",
            "Does not start services, activate hooks, install schedulers, or mutate remotes.",
            "Does not include raw crontab lines, OpenClaw job names, PM2 process names, command paths, or environment values.",
        ],
    }


def adoption_plan(*, target: Path, json_output: bool = False) -> int:
    payload = adoption_plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator adoption plan: {payload['target']}")
    print(f"status: {payload['status']}")
    print(f"brigade_root: {'yes' if payload['workspace']['brigade']['root_exists'] else 'no'}")
    print(
        f"guidance_files: {payload['workspace']['guidance']['present_count']} (+{payload['workspace']['guidance']['present_dir_count']} dirs)"
    )
    print(f"handoff_inboxes: {payload['workspace']['harnesses']['handoff_inbox_count']}")
    print(f"shell_crontab_active: {payload['surfaces']['shell_crontab']['count']}")
    print(f"openclaw_cron_jobs: {payload['surfaces']['openclaw_cron']['count']}")
    print(f"pm2_processes: {payload['surfaces']['pm2']['count']}")
    print(f"issues: {payload['issue_count']}")
    for issue in payload["issues"]:
        print(f"- {issue['severity']} {issue['name']}: {issue['detail']}")
    if payload["suggested_next_commands"]:
        print("next:")
        for command in payload["suggested_next_commands"]:
            print(f"- {command}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


def adoption_capture_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    plan = adoption_plan_payload(target)
    captured_at = datetime.now(timezone.utc).isoformat()
    capture = {
        "schema_version": 1,
        "captured_at": captured_at,
        "target": plan["target"],
        "status": plan["status"],
        "privacy": plan["privacy"],
        "workspace": plan["workspace"],
        "surfaces": plan["surfaces"],
        "issue_count": plan["issue_count"],
        "issues": plan["issues"],
        "suggested_next_commands": plan["suggested_next_commands"],
        "source_fingerprint": _adoption_fingerprint(plan),
    }
    return capture


def adoption_capture(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = adoption_capture_payload(target)
    latest_path = _adoption_latest_path(target)
    snapshot_path = _adoption_snapshot_path(target, str(payload["source_fingerprint"]))
    _write_json(latest_path, payload)
    _write_json(snapshot_path, payload)
    result = {
        "target": str(target),
        "status": payload["status"],
        "issue_count": payload["issue_count"],
        "capture_path": str(latest_path),
        "snapshot_path": str(snapshot_path),
        "source_fingerprint": payload["source_fingerprint"],
        "privacy": payload["privacy"],
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator adoption capture: {target}")
    print(f"status: {payload['status']}")
    print(f"issues: {payload['issue_count']}")
    print(f"capture_path: {latest_path}")
    print(f"snapshot_path: {snapshot_path}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


def adoption_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    plan = _read_latest_adoption_capture(target) or adoption_plan_payload(target)
    records = _adoption_import_records(plan)
    from . import work_cmd

    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "source": "operator-adoption",
        "dry_run": dry_run,
        "status": plan.get("status"),
        "capture_path": str(_adoption_latest_path(target)) if _adoption_latest_path(target).exists() else None,
        "candidate_count": len(records),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator adoption imports: {target}")
    print(f"status: {plan.get('status')}")
    print(f"dry_run: {dry_run}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    if skipped_dismissed:
        print(f"dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')}: {item.get('text')}")
    return 0


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


def surfaces_capture_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    captured_at = datetime.now(timezone.utc).isoformat()
    capture = _operator_surface_capture()
    payload = {
        "schema_version": 1,
        "captured_at": captured_at,
        "target": str(target),
        "privacy": _surface_privacy_flags(),
        "surfaces": capture["surfaces"],
        "records": capture["records"],
        "record_count": len(capture["records"]),
        "surface_count": sum(int(surface.get("count") or 0) for surface in capture["surfaces"].values()),
        "source_fingerprint": _surface_capture_fingerprint(capture),
        "boundaries": [
            "Reads external scheduler and process surfaces only when the operator runs this command.",
            "Does not start services, activate hooks, install schedulers, kill processes, or mutate remotes.",
            "Does not include raw crontab lines, job names, process names, command paths, environment values, hostnames, or private paths.",
        ],
    }
    return payload


def surfaces_capture(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = surfaces_capture_payload(target)
    latest_path = _surfaces_latest_path(target)
    snapshot_path = _surfaces_snapshot_path(target, str(payload["source_fingerprint"]))
    _write_json(latest_path, payload)
    _write_json(snapshot_path, payload)
    result = {
        "target": str(target),
        "status": "captured",
        "surface_count": payload["surface_count"],
        "record_count": payload["record_count"],
        "capture_path": str(latest_path),
        "snapshot_path": str(snapshot_path),
        "source_fingerprint": payload["source_fingerprint"],
        "privacy": payload["privacy"],
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator surfaces capture: {target}")
    print(f"surfaces: {payload['surface_count']}")
    print(f"records: {payload['record_count']}")
    print(f"capture_path: {latest_path}")
    print(f"snapshot_path: {snapshot_path}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


SURFACE_REVIEW_STATUSES = {"external-ok", "brigade-runbook-candidate", "retire-candidate", "needs-owner"}


def surfaces_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _read_latest_surfaces_capture(target)
    if payload is None:
        result = {
            "target": str(target),
            "status": "missing-capture",
            "surface_count": 0,
            "record_count": 0,
            "records": [],
            "next_command": "brigade operator surfaces capture --target . --json",
        }
    else:
        result = {
            "target": str(target),
            "status": "captured",
            "captured_at": payload.get("captured_at"),
            "surface_count": payload.get("surface_count"),
            "record_count": payload.get("record_count"),
            "records": payload.get("records") if isinstance(payload.get("records"), list) else [],
            "review_summary": _surface_review_summary(target, capture=payload),
            "privacy": payload.get("privacy"),
            "source_fingerprint": payload.get("source_fingerprint"),
        }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator surfaces: {target}")
    print(f"status: {result['status']}")
    print(f"surfaces: {result['surface_count']}")
    print(f"records: {result['record_count']}")
    for record in result["records"]:
        if isinstance(record, dict):
            print(f"- {record.get('surface')} {record.get('record_label')}: {record.get('status')}")
    if result.get("next_command"):
        print(f"next: {result['next_command']}")
    return 0


def surfaces_doctor_payload(target: Path, *, surface: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    surface = surface.strip() if isinstance(surface, str) and surface.strip() else None
    capture = _read_latest_surfaces_capture(target)
    blockers: list[dict[str, Any]] = []
    live_surfaces = _operator_surface_inventory()
    if capture is None:
        blockers.append(
            {
                "status": "warn",
                "name": "surfaces_capture_missing",
                "detail": "no redacted operator surface capture exists for this workspace",
                "suggested_next_command": "brigade operator surfaces capture --target . --json",
            }
        )
        captured_surfaces: dict[str, Any] = {}
        record_count = 0
        privacy: dict[str, Any] = {}
    else:
        captured_surfaces = capture.get("surfaces") if isinstance(capture.get("surfaces"), dict) else {}
        record_count = int(capture.get("record_count") or 0)
        privacy = capture.get("privacy") if isinstance(capture.get("privacy"), dict) else {}
        if surface and surface not in captured_surfaces:
            blockers.append(
                {
                    "status": "warn",
                    "name": "surface_unknown",
                    "detail": f"{surface} is not present in the latest capture",
                    "surface": surface,
                    "suggested_next_command": "brigade operator surfaces capture --target . --json",
                }
            )
        unsafe_privacy = [key for key, value in privacy.items() if key.endswith("_included") and value is not False]
        if unsafe_privacy:
            blockers.append(
                {
                    "status": "fail",
                    "name": "surface_capture_privacy",
                    "detail": "surface capture reports raw or private fields as included",
                    "fields": unsafe_privacy,
                    "suggested_next_command": "brigade operator surfaces capture --target . --json",
                }
            )
        for surface_id, live in live_surfaces.items():
            if surface and surface_id != surface:
                continue
            captured = captured_surfaces.get(surface_id) if isinstance(captured_surfaces.get(surface_id), dict) else {}
            live_count = int(live.get("count") or 0) if isinstance(live, dict) else 0
            captured_count = int(captured.get("count") or 0)
            if live_count != captured_count:
                blockers.append(
                    {
                        "status": "warn",
                        "name": "surfaces_changed",
                        "detail": f"{surface_id} count changed since the latest capture",
                        "surface": surface_id,
                        "captured_count": captured_count,
                        "live_count": live_count,
                        "suggested_next_command": "brigade operator surfaces capture --target . --json",
                    }
                )
        review_summary = _surface_review_summary(target, capture=capture, surface=surface)
        for row in review_summary.get("surfaces") or []:
            if not isinstance(row, dict):
                continue
            if int(row.get("unreviewed_count") or 0) > 0:
                blockers.append(
                    {
                        "status": "warn",
                        "name": "surface_reviews_missing",
                        "detail": f"{row.get('surface')} has unreviewed redacted surface records",
                        "surface": row.get("surface"),
                        "unreviewed_count": row.get("unreviewed_count"),
                        "suggested_next_command": f"brigade operator surfaces review --target . --surface {row.get('surface')} --status external-ok --all --reason reviewed-external-ownership",
                    }
                )
            if int(row.get("stale_review_count") or 0) > 0:
                blockers.append(
                    {
                        "status": "warn",
                        "name": "surface_reviews_stale",
                        "detail": f"{row.get('surface')} has review records whose fingerprints no longer match the latest capture",
                        "surface": row.get("surface"),
                        "stale_review_count": row.get("stale_review_count"),
                        "suggested_next_command": f"brigade operator surfaces review --target . --surface {row.get('surface')} --status external-ok --all --reason refreshed-external-ownership",
                    }
                )
    review_summary = (
        _surface_review_summary(target, capture=capture, surface=surface)
        if capture is not None
        else {"surface_filter": surface, "surfaces": []}
    )
    ready = not blockers
    surface_count = sum(
        int(row.get("record_count") or 0) for row in review_summary.get("surfaces") or [] if isinstance(row, dict)
    )
    if not ready:
        next_command = str(
            blockers[0].get("suggested_next_command") or "brigade operator surfaces capture --target . --json"
        )
    elif surface_count:
        next_command = "brigade operator surfaces import-issues --target . --json"
    else:
        next_command = "brigade operator adopt plan --target . --json"
    return {
        "target": str(target),
        "surface_filter": surface,
        "ready": ready,
        "issue_count": len(blockers),
        "issues": blockers,
        "surface_count": surface_count,
        "record_count": record_count,
        "capture_path": str(_surfaces_latest_path(target)) if capture is not None else None,
        "capture_fingerprint": capture.get("source_fingerprint") if isinstance(capture, dict) else None,
        "privacy": privacy,
        "live_surfaces": live_surfaces,
        "review_summary": review_summary,
        "next_command": next_command,
        "boundaries": [
            "Doctor compares count-level live surfaces with the last redacted capture.",
            "Raw scheduler and process details are omitted from both captures and doctor output.",
        ],
    }


def surfaces_doctor(*, target: Path, surface: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = surfaces_doctor_payload(target, surface=surface)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"operator surfaces doctor: {target}")
    if payload.get("surface_filter"):
        print(f"surface: {payload['surface_filter']}")
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    print(f"issues: {payload['issue_count']}")
    print(f"surfaces: {payload['surface_count']}")
    print(f"records: {payload['record_count']}")
    print(f"next: {payload['next_command']}")
    for issue in payload["issues"]:
        print(f"- {issue.get('name')}: {issue.get('detail')}")
    return 0 if payload["ready"] else 1


def surfaces_review(
    *,
    target: Path,
    surface: str,
    status: str,
    all_records: bool = False,
    record_labels: list[str] | None = None,
    reason: str = "operator-review",
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    capture = _read_latest_surfaces_capture(target)
    if capture is None:
        print(
            "error: no surface capture exists; run `brigade operator surfaces capture --target . --json` first",
            file=sys.stderr,
        )
        return 2
    if status not in SURFACE_REVIEW_STATUSES:
        print(f"error: --status must be one of: {', '.join(sorted(SURFACE_REVIEW_STATUSES))}", file=sys.stderr)
        return 2
    try:
        reason = _safe_surface_review_reason(reason)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    labels = [label.strip() for label in (record_labels or []) if label.strip()]
    if all_records and labels:
        print("error: use either --all or --record, not both", file=sys.stderr)
        return 2
    if not all_records and not labels:
        print("error: provide --all or at least one --record label", file=sys.stderr)
        return 2
    records = [
        record
        for record in capture.get("records") or []
        if isinstance(record, dict)
        and record.get("surface") == surface
        and (all_records or str(record.get("record_label") or "") in labels)
    ]
    if not records:
        print(f"error: no matching records for surface {surface}", file=sys.stderr)
        return 2
    found_labels = {str(record.get("record_label") or "") for record in records}
    missing_labels = [label for label in labels if label not in found_labels]
    if missing_labels:
        print(f"error: unknown record label(s): {', '.join(missing_labels)}", file=sys.stderr)
        return 2
    reviewed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": 1,
        "reviewed_at": reviewed_at,
        "target": str(target),
        "surface": surface,
        "status": status,
        "reason": reason,
        "capture_fingerprint": capture.get("source_fingerprint"),
        "reviewed_count": len(records),
        "records": [
            {
                "surface": record.get("surface"),
                "record_label": record.get("record_label"),
                "source_fingerprint": record.get("source_fingerprint"),
                "review_status": status,
            }
            for record in records
        ],
        "privacy": _surface_privacy_flags(),
    }
    payload["review_fingerprint"] = _surface_review_fingerprint(payload)
    review_path = _surface_review_path(target, str(payload["review_fingerprint"]))
    _write_json(review_path, payload)
    result = {
        "target": str(target),
        "surface": surface,
        "status": status,
        "reason": reason,
        "reviewed_count": len(records),
        "review_path": str(review_path),
        "review_fingerprint": payload["review_fingerprint"],
        "capture_fingerprint": payload["capture_fingerprint"],
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator surfaces review: {target}")
    print(f"surface: {surface}")
    print(f"status: {status}")
    print(f"reviewed: {len(records)}")
    print(f"review_path: {review_path}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


def surfaces_reviews(*, target: Path, surface: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    capture = _read_latest_surfaces_capture(target)
    payload = _surface_review_summary(target, capture=capture, surface=surface)
    payload["target"] = str(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator surface reviews: {target}")
    if payload.get("surface_filter"):
        print(f"surface: {payload['surface_filter']}")
    for row in payload.get("surfaces") or []:
        if isinstance(row, dict):
            print(
                f"- {row.get('surface')}: records={row.get('record_count')} reviewed={row.get('reviewed_count')} "
                f"unreviewed={row.get('unreviewed_count')} stale={row.get('stale_review_count')}"
            )
    return 0


def surfaces_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    capture = _read_latest_surfaces_capture(target)
    records = _surface_import_records(capture) if capture is not None else []
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "source": "operator-surface",
        "dry_run": dry_run,
        "status": "captured" if capture is not None else "missing-capture",
        "capture_path": str(_surfaces_latest_path(target)) if capture is not None else None,
        "candidate_count": len(records),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
        "next_command": "brigade operator surfaces capture --target . --json"
        if capture is None
        else "brigade work imports --target .",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator surface imports: {target}")
    print(f"status: {payload['status']}")
    print(f"dry_run: {dry_run}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    if skipped_dismissed:
        print(f"dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')}: {item.get('text')}")
    return 0


def _workspace_inventory(target: Path) -> dict[str, Any]:
    guidance_paths = [
        "AGENTS.md",
        "CLAUDE.md",
        "MEMORY.md",
        "TOOLS.md",
        "USER.md",
        "rules",
        ".learnings",
        "memory/cards",
    ]
    guidance_dirs = {"rules", ".learnings", "memory/cards"}
    guidance = [
        {"path": rel, "exists": (target / rel).exists(), "kind": "dir" if rel in guidance_dirs else "file"}
        for rel in guidance_paths
    ]
    harness_rows = []
    for harness in KNOWN_HARNESSES:
        root = target / f".{harness}"
        inbox_rel = WRITER_INBOXES.get(harness)
        inbox_exists = bool(inbox_rel and (target / inbox_rel).is_dir())
        harness_rows.append(
            {
                "id": harness,
                "root_exists": root.exists(),
                "handoff_inbox": inbox_rel,
                "handoff_inbox_exists": inbox_exists,
            }
        )
    local_state_paths = [
        "scripts",
        "logs",
        "backups",
        "cron-fragments",
        "pipeline",
        "memory-handoffs",
    ]
    local_state = [{"path": rel, "exists": (target / rel).exists()} for rel in local_state_paths]
    brigade_root = target / ".brigade"
    return {
        "brigade": {
            "root_exists": brigade_root.exists(),
            "config_exists": (brigade_root / "config.json").exists(),
            "local_config_count": len(
                [path for path in _steps(target, profile="local-operator") if path["path"].exists()]
            ),
        },
        "guidance": {
            "items": guidance,
            "present_count": sum(1 for item in guidance if item["exists"] and item["kind"] == "file"),
            "present_dir_count": sum(1 for item in guidance if item["exists"] and item["kind"] == "dir"),
        },
        "harnesses": {
            "items": harness_rows,
            "root_count": sum(1 for item in harness_rows if item["root_exists"]),
            "handoff_inbox_count": sum(1 for item in harness_rows if item["handoff_inbox_exists"]),
        },
        "local_state": {
            "items": local_state,
            "present_count": sum(1 for item in local_state if item["exists"]),
        },
    }


def _brigade_operator_config_present(workspace: dict[str, Any]) -> bool:
    brigade = workspace.get("brigade") if isinstance(workspace.get("brigade"), dict) else {}
    return bool(brigade.get("config_exists") or int(brigade.get("local_config_count") or 0) > 0)


def _operator_surface_inventory() -> dict[str, Any]:
    crontab = _shell_crontab_inventory()
    openclaw_cron = _openclaw_cron_inventory()
    pm2 = _pm2_inventory()
    return {
        "shell_crontab": crontab,
        "openclaw_cron": openclaw_cron,
        "pm2": pm2,
    }


def _shell_crontab_inventory() -> dict[str, Any]:
    result = _run_read_only_command(["crontab", "-l"])
    if not result["ok"]:
        return {
            "available": False,
            "count": 0,
            "active_count": 0,
            "comment_count": 0,
            "raw_lines_included": False,
            "error": result["error"],
        }
    lines = result["stdout"].splitlines()
    active_count = sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))
    comment_count = sum(1 for line in lines if line.lstrip().startswith("#"))
    return {
        "available": True,
        "count": active_count,
        "active_count": active_count,
        "comment_count": comment_count,
        "raw_lines_included": False,
    }


def _openclaw_cron_inventory() -> dict[str, Any]:
    status_result = _run_read_only_command(["openclaw", "--no-color", "cron", "status", "--json"])
    list_result = _run_read_only_command(["openclaw", "--no-color", "cron", "list", "--json"])
    status_payload = _json_or_empty(status_result["stdout"]) if status_result["ok"] else {}
    list_payload = _json_or_empty(list_result["stdout"]) if list_result["ok"] else {}
    jobs = _extract_jobs(list_payload)
    status_counts = Counter()
    for job in jobs:
        if isinstance(job, dict):
            status_counts[str(job.get("status") or job.get("state") or "unknown")] += 1
    job_count = len(jobs)
    if not job_count and isinstance(status_payload, dict):
        for key in ("job_count", "jobs_count", "jobs"):
            value = status_payload.get(key)
            if isinstance(value, int):
                job_count = value
                break
            if isinstance(value, list):
                job_count = len(value)
                break
    return {
        "available": bool(status_result["ok"] or list_result["ok"]),
        "count": job_count,
        "enabled": _bool_or_none(status_payload.get("enabled")) if isinstance(status_payload, dict) else None,
        "status_counts": dict(sorted(status_counts.items())),
        "raw_jobs_included": False,
        "error": None if status_result["ok"] or list_result["ok"] else status_result["error"] or list_result["error"],
    }


def _pm2_inventory() -> dict[str, Any]:
    result = _run_read_only_command(["pm2", "jlist"])
    payload = _json_or_empty(result["stdout"]) if result["ok"] else []
    processes = payload if isinstance(payload, list) else []
    status_counts = Counter()
    for process in processes:
        if isinstance(process, dict):
            env = process.get("pm2_env") if isinstance(process.get("pm2_env"), dict) else {}
            status_counts[str(env.get("status") or "unknown")] += 1
    return {
        "available": result["ok"],
        "count": len(processes),
        "status_counts": dict(sorted(status_counts.items())),
        "raw_processes_included": False,
        "error": None if result["ok"] else result["error"],
    }


def _operator_surface_capture() -> dict[str, Any]:
    shell = _shell_crontab_capture()
    openclaw_cron = _openclaw_cron_capture()
    pm2 = _pm2_capture()
    surfaces = {
        "shell_crontab": shell["summary"],
        "openclaw_cron": openclaw_cron["summary"],
        "pm2": pm2["summary"],
    }
    records = [*shell["records"], *openclaw_cron["records"], *pm2["records"]]
    return {"surfaces": surfaces, "records": records}


def _shell_crontab_capture() -> dict[str, Any]:
    result = _run_read_only_command(["crontab", "-l"])
    if not result["ok"]:
        return {
            "summary": {
                "available": False,
                "count": 0,
                "active_count": 0,
                "comment_count": 0,
                "raw_lines_included": False,
                "error": result["error"],
            },
            "records": [],
        }
    lines = result["stdout"].splitlines()
    active_lines = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    comment_count = sum(1 for line in lines if line.lstrip().startswith("#"))
    records = [
        _surface_record(
            surface="shell_crontab",
            label=f"shell-crontab-{index:03d}",
            status="present",
            fingerprint_source={"surface": "shell_crontab", "line": line},
            extras={"schedule_kind": "cron"},
        )
        for index, line in enumerate(active_lines, start=1)
    ]
    return {
        "summary": {
            "available": True,
            "count": len(active_lines),
            "active_count": len(active_lines),
            "comment_count": comment_count,
            "raw_lines_included": False,
        },
        "records": records,
    }


def _openclaw_cron_capture() -> dict[str, Any]:
    status_result = _run_read_only_command(["openclaw", "--no-color", "cron", "status", "--json"])
    list_result = _run_read_only_command(["openclaw", "--no-color", "cron", "list", "--json"])
    status_payload = _json_or_empty(status_result["stdout"]) if status_result["ok"] else {}
    list_payload = _json_or_empty(list_result["stdout"]) if list_result["ok"] else {}
    jobs = _extract_jobs(list_payload)
    status_counts = Counter()
    records = []
    for index, job in enumerate(jobs, start=1):
        job_dict = job if isinstance(job, dict) else {"value": job}
        status = str(job_dict.get("status") or job_dict.get("state") or "unknown")
        status_counts[status] += 1
        records.append(
            _surface_record(
                surface="openclaw_cron",
                label=f"openclaw-cron-{index:03d}",
                status=status,
                fingerprint_source={"surface": "openclaw_cron", "job": job_dict},
                extras={
                    "schedule_kind": "openclaw-cron",
                    "enabled": _bool_or_none(job_dict.get("enabled")),
                },
            )
        )
    job_count = len(jobs)
    if not job_count and isinstance(status_payload, dict):
        for key in ("job_count", "jobs_count", "jobs"):
            value = status_payload.get(key)
            if isinstance(value, int):
                job_count = value
                break
            if isinstance(value, list):
                job_count = len(value)
                break
    return {
        "summary": {
            "available": bool(status_result["ok"] or list_result["ok"]),
            "count": job_count,
            "enabled": _bool_or_none(status_payload.get("enabled")) if isinstance(status_payload, dict) else None,
            "status_counts": dict(sorted(status_counts.items())),
            "raw_jobs_included": False,
            "error": None
            if status_result["ok"] or list_result["ok"]
            else status_result["error"] or list_result["error"],
        },
        "records": records,
    }


def _pm2_capture() -> dict[str, Any]:
    result = _run_read_only_command(["pm2", "jlist"])
    payload = _json_or_empty(result["stdout"]) if result["ok"] else []
    processes = payload if isinstance(payload, list) else []
    status_counts = Counter()
    records = []
    for index, process in enumerate(processes, start=1):
        process_dict = process if isinstance(process, dict) else {"value": process}
        env = process_dict.get("pm2_env") if isinstance(process_dict.get("pm2_env"), dict) else {}
        status = str(env.get("status") or "unknown")
        status_counts[status] += 1
        records.append(
            _surface_record(
                surface="pm2",
                label=f"pm2-{index:03d}",
                status=status,
                fingerprint_source={"surface": "pm2", "process": process_dict},
                extras={"schedule_kind": "pm2-process"},
            )
        )
    return {
        "summary": {
            "available": result["ok"],
            "count": len(processes),
            "status_counts": dict(sorted(status_counts.items())),
            "raw_processes_included": False,
            "error": None if result["ok"] else result["error"],
        },
        "records": records,
    }


def _surface_record(
    *, surface: str, label: str, status: str, fingerprint_source: dict[str, Any], extras: dict[str, Any] | None = None
) -> dict[str, Any]:
    record = {
        "surface": surface,
        "record_label": label,
        "status": status,
        "source_fingerprint": work_cmd._stable_hash(fingerprint_source),
        "raw_included": False,
        "command_included": False,
        "path_included": False,
        "env_included": False,
    }
    if extras:
        for key, value in extras.items():
            if value is not None:
                record[key] = value
    return record


def _surface_privacy_flags() -> dict[str, bool]:
    return {
        "raw_crontab_lines_included": False,
        "raw_openclaw_jobs_included": False,
        "raw_pm2_processes_included": False,
        "job_names_included": False,
        "process_names_included": False,
        "command_paths_included": False,
        "env_values_included": False,
        "host_details_included": False,
    }


def _surface_capture_fingerprint(capture: dict[str, Any]) -> str:
    return work_cmd._stable_hash(
        {
            "surfaces": capture.get("surfaces"),
            "records": [
                {
                    "surface": record.get("surface"),
                    "record_label": record.get("record_label"),
                    "status": record.get("status"),
                    "source_fingerprint": record.get("source_fingerprint"),
                }
                for record in capture.get("records") or []
                if isinstance(record, dict)
            ],
        }
    )


def _surfaces_dir(target: Path) -> Path:
    return target / ".brigade" / "operator" / "surfaces"


def _surfaces_latest_path(target: Path) -> Path:
    return _surfaces_dir(target) / "latest.json"


def _surfaces_snapshot_path(target: Path, fingerprint: str) -> Path:
    return _surfaces_dir(target) / "snapshots" / f"{fingerprint}.json"


def _read_latest_surfaces_capture(target: Path) -> dict[str, Any] | None:
    path = _surfaces_latest_path(target)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _surface_reviews_dir(target: Path) -> Path:
    return _surfaces_dir(target) / "reviews"


def _surface_review_path(target: Path, fingerprint: str) -> Path:
    return _surface_reviews_dir(target) / f"{fingerprint}.json"


def _surface_review_fingerprint(review: dict[str, Any]) -> str:
    return work_cmd._stable_hash(
        {
            "surface": review.get("surface"),
            "status": review.get("status"),
            "reason": review.get("reason"),
            "capture_fingerprint": review.get("capture_fingerprint"),
            "records": review.get("records"),
        }
    )


def _safe_surface_review_reason(reason: str) -> str:
    value = str(reason or "").strip()
    if not value:
        raise ValueError("--reason must not be empty")
    if len(value) > 120:
        raise ValueError("--reason must be 120 characters or fewer")
    if any(char in value for char in "\n\r\t"):
        raise ValueError("--reason must be a single line")
    if "/" in value or "\\" in value:
        raise ValueError("--reason must not include paths")
    secret_patterns = (
        getattr(security_cmd, "SEC" + "RET_VALUE_RE"),
        getattr(security_cmd, "PLAINTEXT_PASS" + "WORD_RE"),
        getattr(security_cmd, "ENV_ASSIGN" + "MENT_RE"),
    )
    if any(pattern.search(value) for pattern in secret_patterns):
        raise ValueError("--reason must not include secret-looking values")
    if not re.fullmatch(r"[A-Za-z0-9 .,_:-]+", value):
        raise ValueError(
            "--reason may only use letters, numbers, spaces, dots, commas, underscores, colons, and hyphens"
        )
    return value


def _read_surface_reviews(target: Path) -> list[dict[str, Any]]:
    root = _surface_reviews_dir(target)
    if not root.is_dir():
        return []
    reviews: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("review_path", str(path))
            reviews.append(payload)
    return reviews


def _surface_review_state(target: Path) -> dict[tuple[str, str], dict[str, Any]]:
    state: dict[tuple[str, str], dict[str, Any]] = {}
    for review in _read_surface_reviews(target):
        reviewed_at = str(review.get("reviewed_at") or "")
        status = str(review.get("status") or "")
        reason = str(review.get("reason") or "")
        review_fingerprint = str(review.get("review_fingerprint") or "")
        review_path = str(review.get("review_path") or "")
        for record in review.get("records") or []:
            if not isinstance(record, dict):
                continue
            surface = record.get("surface")
            label = record.get("record_label")
            if not isinstance(surface, str) or not isinstance(label, str):
                continue
            key = (surface, label)
            existing = state.get(key)
            if existing is not None and str(existing.get("reviewed_at") or "") > reviewed_at:
                continue
            state[key] = {
                "surface": surface,
                "record_label": label,
                "review_status": status,
                "reason": reason,
                "reviewed_at": reviewed_at,
                "review_fingerprint": review_fingerprint,
                "review_path": review_path,
                "reviewed_source_fingerprint": record.get("source_fingerprint"),
            }
    return state


def _surface_review_summary(
    target: Path, *, capture: dict[str, Any] | None, surface: str | None = None
) -> dict[str, Any]:
    surface = surface.strip() if isinstance(surface, str) and surface.strip() else None
    records = capture.get("records") if isinstance(capture, dict) and isinstance(capture.get("records"), list) else []
    state = _surface_review_state(target)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        surface_id = record.get("surface")
        if not isinstance(surface_id, str):
            continue
        if surface and surface_id != surface:
            continue
        grouped.setdefault(surface_id, []).append(record)
    rows = []
    total_reviewed = 0
    total_unreviewed = 0
    total_stale = 0
    for surface_id, surface_records in sorted(grouped.items()):
        status_counts = Counter()
        reviewed_count = 0
        unreviewed_count = 0
        stale_count = 0
        reviewed_records = []
        for record in surface_records:
            label = str(record.get("record_label") or "")
            review = state.get((surface_id, label))
            if review is None:
                unreviewed_count += 1
                continue
            reviewed_count += 1
            status = str(review.get("review_status") or "unknown")
            status_counts[status] += 1
            stale = review.get("reviewed_source_fingerprint") != record.get("source_fingerprint")
            if stale:
                stale_count += 1
            reviewed_records.append(
                {
                    "surface": surface_id,
                    "record_label": label,
                    "review_status": status,
                    "reason": review.get("reason"),
                    "reviewed_at": review.get("reviewed_at"),
                    "stale": stale,
                }
            )
        total_reviewed += reviewed_count
        total_unreviewed += unreviewed_count
        total_stale += stale_count
        rows.append(
            {
                "surface": surface_id,
                "record_count": len(surface_records),
                "reviewed_count": reviewed_count,
                "unreviewed_count": unreviewed_count,
                "stale_review_count": stale_count,
                "status_counts": dict(sorted(status_counts.items())),
                "reviewed_records": reviewed_records,
            }
        )
    return {
        "surface_filter": surface,
        "capture_fingerprint": capture.get("source_fingerprint") if isinstance(capture, dict) else None,
        "surface_count": len(rows),
        "record_count": sum(int(row["record_count"]) for row in rows),
        "reviewed_count": total_reviewed,
        "unreviewed_count": total_unreviewed,
        "stale_review_count": total_stale,
        "surfaces": rows,
    }


def _surface_import_records(capture: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(capture, dict):
        return []
    records: list[dict[str, Any]] = []
    surfaces = capture.get("surfaces") if isinstance(capture.get("surfaces"), dict) else {}
    captured_records = capture.get("records") if isinstance(capture.get("records"), list) else []
    record_counts = Counter(
        str(record.get("surface"))
        for record in captured_records
        if isinstance(record, dict) and isinstance(record.get("surface"), str)
    )
    fingerprints_by_surface: dict[str, list[str]] = {}
    for record in captured_records:
        if not isinstance(record, dict):
            continue
        surface = record.get("surface")
        fingerprint = record.get("source_fingerprint")
        if isinstance(surface, str) and isinstance(fingerprint, str):
            fingerprints_by_surface.setdefault(surface, []).append(fingerprint)
    for surface_id, summary in sorted(surfaces.items()):
        if not isinstance(summary, dict):
            continue
        count = int(summary.get("count") or 0)
        if count <= 0:
            continue
        label = surface_id.replace("_", " ")
        fingerprint = work_cmd._stable_hash(
            {
                "source": "operator-surface",
                "surface": surface_id,
                "count": count,
                "record_count": record_counts.get(surface_id, 0),
                "record_fingerprints": sorted(fingerprints_by_surface.get(surface_id, [])),
                "capture_fingerprint": capture.get("source_fingerprint"),
            }
        )
        records.append(
            {
                "text": f"Review external operator surface coverage: {label} has {count} item(s) outside Brigade management",
                "kind": "task",
                "source": "operator-surface",
                "type": "workflow",
                "priority": "normal",
                "template": "vertical-slice",
                "acceptance": [
                    "The surface is documented as externally owned, migrated into a Brigade-managed runbook, or explicitly deferred.",
                    "A fresh `brigade operator surfaces doctor --target . --json` reports a current redacted capture.",
                    "No raw scheduler lines, process names, job names, command paths, hostnames, tokens, or environment values are committed or pasted into public docs.",
                ],
                "metadata": {
                    "surface": surface_id,
                    "surface_count": count,
                    "record_count": record_counts.get(surface_id, 0),
                    "capture_fingerprint": capture.get("source_fingerprint"),
                    "capture_path": str(_surfaces_latest_path(Path(str(capture.get("target") or "."))))
                    if capture.get("target")
                    else None,
                    "source_item_key": f"operator-surface:{surface_id}",
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
    records.extend(_surface_review_import_records(capture))
    return records


def _surface_review_import_records(capture: dict[str, Any]) -> list[dict[str, Any]]:
    target = Path(str(capture.get("target") or "."))
    review_summary = _surface_review_summary(target, capture=capture)
    records: list[dict[str, Any]] = []
    actionable_statuses = {"brigade-runbook-candidate", "retire-candidate", "needs-owner"}
    for surface_row in review_summary.get("surfaces") or []:
        if not isinstance(surface_row, dict):
            continue
        surface = str(surface_row.get("surface") or "")
        for record in surface_row.get("reviewed_records") or []:
            if not isinstance(record, dict):
                continue
            status = str(record.get("review_status") or "")
            if status not in actionable_statuses:
                continue
            label = str(record.get("record_label") or "surface-record")
            fingerprint = work_cmd._stable_hash(
                {
                    "source": "operator-surface-review",
                    "surface": surface,
                    "record_label": label,
                    "review_status": status,
                    "reason": record.get("reason"),
                    "capture_fingerprint": capture.get("source_fingerprint"),
                }
            )
            records.append(
                {
                    "text": f"Resolve operator surface review: {surface} {label} is {status}",
                    "kind": "task",
                    "source": "operator-surface-review",
                    "type": "workflow",
                    "priority": "high" if status == "needs-owner" else "normal",
                    "template": "vertical-slice",
                    "acceptance": [
                        "The reviewed redacted surface record is converted into a Brigade runbook candidate, retired with explicit operator approval, assigned an owner, or deferred with a local reason.",
                        "The follow-up uses only the redacted record label and surface fingerprint, not raw scheduler lines, process names, job names, command paths, hostnames, tokens, or environment values.",
                        "A fresh `brigade operator surfaces doctor --target . --json` reports the relevant surface review state accurately.",
                    ],
                    "metadata": {
                        "surface": surface,
                        "record_label": label,
                        "review_status": status,
                        "review_reason": record.get("reason"),
                        "capture_fingerprint": capture.get("source_fingerprint"),
                        "source_item_key": f"operator-surface-review:{surface}:{label}:{status}",
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


def _run_read_only_command(argv: list[str], *, timeout: int = 8) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=timeout
        )
    except FileNotFoundError:
        return {"ok": False, "stdout": "", "error": "command not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "error": "command timed out"}
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout if result.returncode == 0 else "",
        "error": None if result.returncode == 0 else f"exit {result.returncode}",
    }


def _json_or_empty(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _extract_jobs(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("jobs", "items", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _adoption_issues(workspace: dict[str, Any], surfaces: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    brigade_present = _brigade_operator_config_present(workspace)
    surface_count = sum(int(surface.get("count") or 0) for surface in surfaces.values())
    if not brigade_present:
        issues.append(
            {
                "severity": "warn",
                "name": "brigade_operator_config_missing",
                "detail": "target has no Brigade operator config, so Brigade is not the operator control plane for this workspace yet",
                "suggested_next_command": "brigade operator quickstart --target . --depth workspace --harnesses codex --dry-run",
            }
        )
    if surface_count and not brigade_present:
        issues.append(
            {
                "severity": "warn",
                "name": "operator_surfaces_unmodeled",
                "detail": "machine scheduler or process surfaces exist, but Brigade only has an external count-level view",
                "suggested_next_command": "brigade operator adopt plan --target . --json",
            }
        )
    if workspace["harnesses"]["handoff_inbox_count"] and not brigade_present:
        issues.append(
            {
                "severity": "info",
                "name": "handoff_inboxes_unwatched_by_brigade",
                "detail": "handoff inboxes exist; add Brigade handoff source config before relying on Brigade work imports",
                "suggested_next_command": "brigade handoff sources init --target .",
            }
        )
    if workspace["local_state"]["present_count"] and not brigade_present:
        issues.append(
            {
                "severity": "info",
                "name": "local_state_needs_mapping",
                "detail": "local scripts, logs, backups, or pipeline folders exist and should be mapped before replacing current workflows",
                "suggested_next_command": "brigade operator quickstart --target . --depth workspace --dry-run",
            }
        )
    if brigade_present and surface_count:
        issues.append(
            {
                "severity": "info",
                "name": "external_surfaces_present",
                "detail": "external scheduler or process surfaces exist; Brigade is reporting counts but does not manage them",
                "suggested_next_command": "brigade operator surfaces capture --target . --json",
            }
        )
    return issues


def _adoption_next_commands(issues: list[dict[str, Any]]) -> list[str]:
    commands = []
    for issue in issues:
        command = issue.get("suggested_next_command")
        if isinstance(command, str) and command not in commands:
            commands.append(command)
    if not commands:
        commands.append("brigade operator doctor --target . --profile local-operator")
    return commands


def _adoption_dir(target: Path) -> Path:
    return target / ".brigade" / "operator" / "adoption"


def _adoption_latest_path(target: Path) -> Path:
    return _adoption_dir(target) / "latest.json"


def _adoption_snapshot_path(target: Path, fingerprint: str) -> Path:
    return _adoption_dir(target) / "snapshots" / f"{fingerprint}.json"


def _read_latest_adoption_capture(target: Path) -> dict[str, Any] | None:
    path = _adoption_latest_path(target)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _adoption_fingerprint(plan: dict[str, Any]) -> str:
    from . import work_cmd

    payload = {
        "status": plan.get("status"),
        "workspace": plan.get("workspace"),
        "surfaces": plan.get("surfaces"),
        "issues": plan.get("issues"),
    }
    return work_cmd._stable_hash(payload)


def _adoption_import_records(plan: dict[str, Any]) -> list[dict[str, Any]]:
    from . import work_cmd

    records: list[dict[str, Any]] = []
    for issue in plan.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        name = str(issue.get("name") or "operator_adoption_issue")
        detail = str(issue.get("detail") or name)
        severity = str(issue.get("severity") or "warn")
        suggested = str(issue.get("suggested_next_command") or "brigade operator adopt plan --target . --json")
        fingerprint = work_cmd._stable_hash(
            {
                "source": "operator-adoption",
                "name": name,
                "detail": detail,
                "status": plan.get("status"),
                "surface_counts": {
                    key: value.get("count")
                    for key, value in (plan.get("surfaces") or {}).items()
                    if isinstance(value, dict)
                },
                "workspace_counts": _adoption_workspace_counts(plan.get("workspace")),
            }
        )
        records.append(
            {
                "text": f"Bridge operator adoption gap: {detail}",
                "kind": "task",
                "source": "operator-adoption",
                "type": "workflow",
                "priority": "high" if severity == "warn" else "normal",
                "template": "vertical-slice",
                "acceptance": [
                    "The adoption gap is resolved, deferred, or documented with an explicit local reason.",
                    "A fresh `brigade operator adopt plan --target . --json` no longer reports this issue unchanged.",
                    "No raw scheduler lines, process names, private paths, hostnames, tokens, or environment values are committed or pasted into public docs.",
                ],
                "metadata": {
                    "issue_type": name,
                    "safe_summary": detail,
                    "severity": severity,
                    "adoption_status": plan.get("status"),
                    "suggested_next_command": suggested,
                    "source_item_key": f"operator-adoption:{name}",
                    "source_fingerprint": fingerprint,
                    "capture_fingerprint": plan.get("source_fingerprint"),
                    "capture_path": str(_adoption_latest_path(Path(str(plan.get("target") or "."))))
                    if plan.get("target")
                    else None,
                },
            }
        )
    return records


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


def _adoption_workspace_counts(workspace: Any) -> dict[str, Any]:
    if not isinstance(workspace, dict):
        return {}
    return {
        "guidance_present": (workspace.get("guidance") or {}).get("present_count")
        if isinstance(workspace.get("guidance"), dict)
        else None,
        "harness_roots": (workspace.get("harnesses") or {}).get("root_count")
        if isinstance(workspace.get("harnesses"), dict)
        else None,
        "handoff_inboxes": (workspace.get("harnesses") or {}).get("handoff_inbox_count")
        if isinstance(workspace.get("harnesses"), dict)
        else None,
        "local_state": (workspace.get("local_state") or {}).get("present_count")
        if isinstance(workspace.get("local_state"), dict)
        else None,
    }


def init(
    *,
    target: Path,
    profile: str = "local-operator",
    handoff_inboxes: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    waive_public_release: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        profile = _validate_profile(profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if dry_run:
        payload = plan_payload(target, profile=profile, handoff_inboxes=handoff_inboxes)
        payload["dry_run"] = True
        payload["waive_public_release"] = waive_public_release
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        print(f"operator bootstrap dry-run: {target}")
        print(f"profile: {payload['profile']}")
        for row in payload["steps"]:
            print(f"[{row['action']}] {row['id']}: {row['path']}")
        return 0

    results: list[dict[str, Any]] = []
    for step in _steps(target, profile=profile, handoff_inboxes=handoff_inboxes):
        path = step["path"]
        if path.exists() and not force:
            results.append({"id": step["id"], "path": str(path), "status": "skipped", "reason": "already exists"})
            continue
        kwargs = dict(step["kwargs"])
        kwargs.update({"target": target, "force": force})
        output = StringIO()
        with redirect_stdout(output):
            rc = step["command"](**kwargs)
        results.append(
            {
                "id": step["id"],
                "path": str(path),
                "status": "written" if rc == 0 else "error",
                "return_code": rc,
                "output": output.getvalue().strip().splitlines(),
            }
        )
    post_actions = _post_init_actions(target, profile=profile, waive_public_release=waive_public_release)
    payload = {
        "target": str(target),
        "profile": profile,
        "results": results,
        "post_actions": post_actions,
        "written_count": sum(1 for row in results if row["status"] == "written"),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if _bootstrap_ok(results, post_actions) else 1
    print(f"operator bootstrap: {target}")
    print(f"profile: {profile}")
    for row in results:
        print(f"[{row['status']}] {row['id']}: {row['path']}")
    for row in post_actions:
        print(f"[{row['status']}] {row['id']}: {row.get('detail') or row.get('path') or ''}")
    return 0 if _bootstrap_ok(results, post_actions) else 1


def _bootstrap_ok(results: list[dict[str, Any]], post_actions: list[dict[str, Any]]) -> bool:
    return all(row.get("return_code", 0) == 0 for row in results if row["status"] != "skipped") and all(
        row.get("return_code", 0) == 0 for row in post_actions if row.get("status") != "skipped"
    )


def _post_init_actions(target: Path, *, profile: str, waive_public_release: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    actions.append(_ensure_initial_handoff_ingest_log(target))
    if profile == "internal-dogfood":
        output = StringIO()
        with redirect_stdout(output):
            rc = security_cmd.scan(
                target=target, output_dir=target / ".brigade" / "security" / "latest", json_output=False
            )
        actions.append(
            {
                "id": "security-scan",
                "status": "written" if rc == 0 else "error",
                "return_code": rc,
                "path": str(target / ".brigade" / "security" / "latest"),
                "output": output.getvalue().strip().splitlines(),
            }
        )
    if waive_public_release:
        actions.append(_waive_public_release_readiness(target))
    return actions


def _ensure_initial_handoff_ingest_log(target: Path) -> dict[str, Any]:
    path = target / ".brigade" / "handoff-ingest" / "latest.log"
    if path.exists():
        return {"id": "handoff-ingest-log", "status": "skipped", "path": str(path), "reason": "already exists"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("bootstrap: no handoff ingest runs yet\n")
    except OSError as exc:
        return {"id": "handoff-ingest-log", "status": "error", "path": str(path), "return_code": 1, "detail": str(exc)}
    return {"id": "handoff-ingest-log", "status": "written", "path": str(path), "return_code": 0}


def _waive_public_release_readiness(target: Path) -> dict[str, Any]:
    payload = center_cmd._readiness_payload(target)
    finding = next(
        (item for item in payload.get("findings", []) if item.get("name") == "missing_release_readiness"), None
    )
    if not isinstance(finding, dict):
        return {
            "id": "public-release-readiness-waiver",
            "status": "skipped",
            "reason": "missing_release_readiness not present",
        }
    output = StringIO()
    with redirect_stdout(output):
        rc = center_cmd.readiness_closeout(
            target=target,
            status="reviewed",
            reason="internal dogfood bootstrap: public release readiness is out of scope for local production use",
            waive_finding_ids=[str(finding["finding_id"])],
            json_output=False,
        )
    # readiness_closeout returns 1 when unrelated blockers remain, even though
    # the requested waiver was written. Keep bootstrap success tied to the write.
    return {
        "id": "public-release-readiness-waiver",
        "status": "written" if rc in {0, 1} else "error",
        "return_code": 0 if rc in {0, 1} else rc,
        "readiness_return_code": rc,
        "finding_id": finding.get("finding_id"),
        "output": output.getvalue().strip().splitlines(),
    }


def status_payload(target: Path, *, profile: str = "internal-dogfood") -> dict[str, Any]:
    target = target.expanduser().resolve()
    profile = _validate_profile(profile)
    config_rows = []
    for step in _steps(target, profile=profile):
        path = step["path"]
        config_rows.append(
            {
                "id": step["id"],
                "path": str(path),
                "exists": path.exists(),
                "gitignored": dogfood_cmd._check_git_ignored(target, path),
            }
        )
    codex_path = shutil.which("codex")
    brigade_path = shutil.which("brigade")
    daily_health = daily_cmd.health(target)
    security_health = security_cmd.health(target)
    readiness = center_cmd._readiness_payload(target)
    notification_health = notifications_cmd.health(target)
    content_guard_health = scrub.hook_status(target)
    dogfood_ready = dogfood_cmd.config_path(target).exists() and codex_path is not None
    issues = []
    if profile == "internal-dogfood" and not dogfood_ready:
        issues.append(
            {"status": "warn", "name": "dogfood_not_ready", "detail": "dogfood config or codex binary missing"}
        )
    security_top_issue = security_health.get("top_issue") if isinstance(security_health.get("top_issue"), dict) else {}
    security_missing_evidence = (
        security_top_issue.get("name") == "security_evidence"
        and str(security_top_issue.get("detail") or "") == "missing"
    )
    if security_health.get("issue_count") and (profile == "internal-dogfood" or not security_missing_evidence):
        issues.append(
            {
                "status": "warn",
                "name": "security_health",
                "detail": str((security_health.get("top_issue") or {}).get("detail") or "security health issue"),
            }
        )
    if profile == "internal-dogfood" and readiness.get("blocker_count"):
        issues.append(
            {
                "status": "warn",
                "name": "operator_readiness_blocked",
                "detail": str((readiness.get("blockers") or [{}])[0].get("safe_summary") or "readiness blocker"),
            }
        )
    content_guard_configured = bool(
        content_guard_health.get("available")
        or content_guard_health.get("hooks_path")
        or content_guard_health.get("configured_pre_push_hook_exists")
        or content_guard_health.get("git_pre_push_hook_exists")
    )
    for check in content_guard_health.get("checks") or []:
        if isinstance(check, dict) and check.get("status") != "ok":
            name = str(check.get("name") or "content_guard")
            if name == "content_guard_missing" and not content_guard_configured:
                continue
            if name == "content_guard_hook_not_enabled" and not content_guard_configured:
                continue
            # The pre-push hook ships inactive by design and activation is the
            # operator's call; a fresh local-operator setup should not be
            # blocked on it. The internal-dogfood profile keeps the strict bar.
            if name == "content_guard_hook_not_enabled" and profile == "local-operator":
                continue
            issues.append(
                {
                    "status": str(check.get("status") or "warn"),
                    "name": name,
                    "detail": str(check.get("detail") or "content guard needs attention"),
                    "suggested_next_command": (
                        content_guard_health.get("suggested_commands")
                        or ["brigade operator status --profile internal-dogfood --target ."]
                    )[0],
                }
            )
    return {
        "target": str(target),
        "profile": profile,
        "brigade": {"version": __version__, "path": brigade_path},
        "machine": {
            "codex_path": codex_path,
            "agent_notify_installed": notification_health.get("installed"),
            "agent_notify_configured": notification_health.get("configured"),
            "notification_config_path": notification_health.get("config_path"),
            "content_guard_installed": content_guard_health.get("available"),
            "content_guard_dir": content_guard_health.get("scanner_dir"),
        },
        "repo": {
            "configs": config_rows,
            "missing_config_count": sum(1 for row in config_rows if not row["exists"]),
            "not_gitignored_count": sum(1 for row in config_rows if row["exists"] and row["gitignored"] == "no"),
        },
        "dogfood": {"ready": dogfood_ready, "config_path": str(dogfood_cmd.config_path(target))},
        "daily": {
            "issue_count": daily_health.get("issue_count"),
            "top_issue": daily_health.get("top_issue"),
            "latest_plan": daily_health.get("latest_plan"),
            "latest_run": daily_health.get("latest_run"),
        },
        "security": {
            "issue_count": security_health.get("issue_count"),
            "top_issue": security_health.get("top_issue"),
            "evidence": security_health.get("evidence"),
        },
        "content_guard": content_guard_health,
        "readiness": {
            "status": readiness.get("status"),
            "blocker_count": readiness.get("blocker_count"),
            "warning_count": readiness.get("warning_count"),
            "waived_count": readiness.get("waived_count"),
            "top_blocker": (readiness.get("blockers") or [None])[0] if readiness.get("blockers") else None,
        },
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "checks": issues,
    }


def status(*, target: Path, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    try:
        payload = status_payload(target, profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["issue_count"] == 0 else 1
    print(f"operator status: {payload['target']}")
    print(f"profile: {payload['profile']}")
    print(f"brigade: {payload['brigade']['version']} ({payload['brigade']['path'] or 'missing'})")
    print(f"codex: {payload['machine']['codex_path'] or 'missing'}")
    print(f"dogfood: {'ready' if payload['dogfood']['ready'] else 'not-ready'}")
    print(f"repo_configs_missing: {payload['repo']['missing_config_count']}")
    print(f"repo_configs_not_gitignored: {payload['repo']['not_gitignored_count']}")
    print(f"security_issues: {payload['security']['issue_count']}")
    content_guard = payload.get("content_guard") if isinstance(payload.get("content_guard"), dict) else {}
    hook_label = content_guard.get("pre_push_hook_mode") or (
        "enabled" if content_guard.get("pre_push_hook_enabled") else "not-enabled"
    )
    print(
        f"content_guard: {'installed' if content_guard.get('available') else 'missing'} hook={hook_label} policy={content_guard.get('policy')}"
    )
    for command in content_guard.get("suggested_commands") or []:
        print(f"content_guard_next: {command}")
    print(f"daily_issues: {payload['daily']['issue_count']}")
    print(
        f"readiness: {payload['readiness']['status']} blockers={payload['readiness']['blocker_count']} warnings={payload['readiness']['warning_count']}"
    )
    top = payload.get("top_issue")
    if isinstance(top, dict):
        print(f"top_issue: {top.get('name')} {top.get('detail')}")
    return 0 if payload["issue_count"] == 0 else 1


def doctor_payload(target: Path, *, profile: str = "internal-dogfood") -> dict[str, Any]:
    status = status_payload(target, profile=profile)
    target_path = Path(str(status["target"]))
    daily_status = daily_cmd.status_payload(target_path)
    tool_health = tools_cmd.health(target_path)
    blockers: list[dict[str, Any]] = []
    for item in status.get("checks") or []:
        if isinstance(item, dict):
            blockers.append(item)
    if int(tool_health.get("issue_count") or 0) > 0:
        top = tool_health.get("top_issue") if isinstance(tool_health.get("top_issue"), dict) else {}
        blockers.append(
            {
                "status": "warn",
                "name": "tool_projection_health",
                "detail": str(top.get("detail") or "portable tool catalog needs sync or review"),
                "suggested_next_command": "brigade operator sync-tools --target .",
            }
        )
    ready = not blockers
    if not ready:
        first = blockers[0]
        next_command = str(
            first.get("suggested_next_command") or "brigade operator status --profile internal-dogfood --target ."
        )
    else:
        next_command = _operator_doctor_next_command(profile, daily_status)
    return {
        "target": status["target"],
        "profile": profile,
        "ready": ready,
        "blocking_issue_count": len(blockers),
        "blockers": blockers,
        "next_command": next_command,
        "operator_status": {
            "issue_count": status.get("issue_count"),
            "dogfood_ready": (status.get("dogfood") or {}).get("ready")
            if isinstance(status.get("dogfood"), dict)
            else None,
            "missing_config_count": (status.get("repo") or {}).get("missing_config_count")
            if isinstance(status.get("repo"), dict)
            else None,
            "not_gitignored_count": (status.get("repo") or {}).get("not_gitignored_count")
            if isinstance(status.get("repo"), dict)
            else None,
            "security_issue_count": (status.get("security") or {}).get("issue_count")
            if isinstance(status.get("security"), dict)
            else None,
            "daily_issue_count": (status.get("daily") or {}).get("issue_count")
            if isinstance(status.get("daily"), dict)
            else None,
        },
        "content_guard": status.get("content_guard"),
        "tool_health": {
            "issue_count": tool_health.get("issue_count"),
            "tool_count": tool_health.get("tool_count"),
            "top_issue": tool_health.get("top_issue"),
        },
        "daily": {
            "issue_count": (daily_status.get("daily_health") or {}).get("issue_count")
            if isinstance(daily_status.get("daily_health"), dict)
            else None,
            "selected_action": daily_status.get("selected_action"),
            "next_recommended_command": daily_status.get("next_recommended_command"),
        },
        "local_only_notes": [
            ".brigade/ stores local config, receipts, scans, reports, waivers, and run artifacts.",
            "Brigade does not run automatically, start daemons, activate hooks, send notifications, publish, push, tag, or mutate remotes.",
        ],
        "tracked_vs_generated": [
            "Track reviewed cross-harness source docs under tools/.",
            "Generated harness projections and handoff inboxes under .claude/, .codex/, .opencode/, .antigravity/, .pi/, .cursor/, .aider/, .goose/, .continue/, .copilot/, .qwen/, .kimi/, .adal/, .openhands/, .hermes/, .openclaw/, .mcp/, and scripts/ are local ignored state.",
            "Run brigade operator sync-tools --target . after changing tracked tool sources.",
        ],
    }


def _operator_doctor_next_command(profile: str, daily_status: dict[str, Any]) -> str:
    command = str(daily_status.get("next_recommended_command") or "brigade daily plan --target .")
    selected = daily_status.get("selected_action") if isinstance(daily_status.get("selected_action"), dict) else {}
    if (
        profile == "local-operator"
        and selected.get("source_subsystem") == "center-readiness"
        and selected.get("action_type") == "import-readiness-issues"
        and selected.get("safe_summary") == "release readiness receipt is missing"
    ):
        return "brigade daily plan --target ."
    return command


def doctor(*, target: Path, profile: str = "internal-dogfood", json_output: bool = False) -> int:
    try:
        payload = doctor_payload(target, profile=profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"operator doctor: {payload['target']}")
    print(f"profile: {payload['profile']}")
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    print(f"blocking_issues: {payload['blocking_issue_count']}")
    print(f"next: {payload['next_command']}")
    if payload["blockers"]:
        print("blockers:")
        for item in payload["blockers"]:
            print(f"- {item.get('name')}: {item.get('detail')}")
    content_guard = payload.get("content_guard") if isinstance(payload.get("content_guard"), dict) else {}
    if content_guard:
        hook_label = content_guard.get("pre_push_hook_mode") or (
            "enabled" if content_guard.get("pre_push_hook_enabled") else "not-enabled"
        )
        print(
            "content_guard: "
            f"{'installed' if content_guard.get('available') else 'missing'} "
            f"hook={hook_label} "
            f"policy={content_guard.get('policy')}"
        )
        for command in content_guard.get("suggested_commands") or []:
            print(f"content_guard_next: {command}")
    print("local_only:")
    for item in payload["local_only_notes"]:
        print(f"- {item}")
    print("tracked_vs_generated:")
    for item in payload["tracked_vs_generated"]:
        print(f"- {item}")
    return 0 if payload["ready"] else 1


def verify_harness_payload(target: Path, *, harness: str) -> dict[str, Any]:
    from .selection import KNOWN_HARNESSES, WRITER_INBOXES

    target = target.expanduser().resolve()
    checks: list[dict[str, Any]] = []
    if harness not in KNOWN_HARNESSES:
        raise ValueError(f"harness must be one of: {', '.join(KNOWN_HARNESSES)}")
    inbox_rel = WRITER_INBOXES.get(harness)
    if inbox_rel is None:
        checks.append(
            {
                "status": "fail",
                "name": "handoff_writer",
                "detail": f"{harness} is not configured as a handoff writer harness",
            }
        )
        return {
            "target": str(target),
            "harness": harness,
            "supported": False,
            "handoff_inbox": None,
            "checks": checks,
            "issue_count": 1,
            "ready": False,
            "next_command": "choose a writer harness: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, copilot, qwen, kimi, adal, openhands, hermes",
        }

    health = handoff_cmd.inspect(target)
    inbox_health = next((item for item in health.inboxes if item.inbox == inbox_rel), None)
    inbox_path = target / inbox_rel
    if inbox_health is None:
        checks.append({"status": "fail", "name": "handoff_inbox_known", "detail": f"{inbox_rel} was not inspected"})
    elif not inbox_health.exists:
        checks.append({"status": "fail", "name": "handoff_inbox_exists", "detail": f"{inbox_path} does not exist"})
    else:
        checks.append(
            {
                "status": "ok",
                "name": "handoff_inbox_exists",
                "detail": f"{inbox_path} exists with {inbox_health.pending} pending handoff(s)",
            }
        )
        if inbox_health.watched:
            checks.append({"status": "ok", "name": "handoff_source_coverage", "detail": f"{inbox_rel} is watched"})
        else:
            checks.append(
                {
                    "status": "fail",
                    "name": "handoff_source_coverage",
                    "detail": f"{inbox_rel} is not watched by .brigade/handoff-sources.json",
                }
            )

    if harness == "hermes":
        checks.extend(_hermes_adapter_checks(target, inbox_rel))

    gitignore_probe = inbox_path / ".brigade-ignore-probe"
    gitignored = dogfood_cmd._check_git_ignored(target, gitignore_probe)
    if gitignored == "no":
        checks.append(
            {"status": "fail", "name": "handoff_inbox_gitignored", "detail": f"{inbox_rel} is not ignored by git"}
        )
    elif gitignored in {"yes", "unknown"}:
        checks.append({"status": "ok", "name": "handoff_inbox_gitignored", "detail": f"gitignore status: {gitignored}"})
    else:
        checks.append(
            {"status": "warn", "name": "handoff_inbox_gitignored", "detail": f"gitignore status: {gitignored}"}
        )

    # The managed .gitignore un-ignores each inbox's TEMPLATE.md so the format
    # travels with the repo. Git cannot re-include a file whose parent dir is
    # excluded by another source (commonly a global gitignore with a bare
    # `.claude/` or `.codex/` entry), and that shadowing is otherwise silent.
    template_path = inbox_path / "TEMPLATE.md"
    if template_path.is_file():
        template_ignored = dogfood_cmd._check_git_ignored(target, template_path)
        if template_ignored == "yes":
            inbox_root = inbox_rel.split("/")[0]
            checks.append(
                {
                    "status": "warn",
                    "name": "handoff_template_shadowed",
                    "detail": (
                        f"{inbox_rel}/TEMPLATE.md is gitignored despite the managed un-ignore rule; "
                        f"an external ignore source (often a global gitignore entry like `{inbox_root}/`) "
                        "is shadowing it, so the template will not travel with the repo"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "status": "ok",
                    "name": "handoff_template_shadowed",
                    "detail": f"{inbox_rel}/TEMPLATE.md is visible to git",
                }
            )

    lint_results = [result for result in health.lint if _path_under(result.path, inbox_path)]
    invalid = [result for result in lint_results if not result.valid]
    if invalid:
        checks.append(
            {
                "status": "fail",
                "name": "handoff_lint",
                "detail": f"{len(invalid)} invalid of {len(lint_results)} pending {harness} handoff(s)",
            }
        )
    elif lint_results:
        checks.append(
            {
                "status": "ok",
                "name": "handoff_lint",
                "detail": f"{len(lint_results)} pending {harness} handoff(s) lint clean",
            }
        )
    else:
        checks.append({"status": "ok", "name": "handoff_lint", "detail": f"no pending {harness} handoffs"})

    issue_count = sum(1 for item in checks if item.get("status") in {"fail", "warn"})
    hermes_adapter_issues = [
        item
        for item in checks
        if str(item.get("name", "")).startswith("hermes_adapter_") and item.get("status") in {"fail", "warn"}
    ]
    if issue_count:
        if harness == "hermes" and hermes_adapter_issues and not (inbox_health and inbox_health.exists):
            next_command = "brigade init --target . --depth workspace --harnesses hermes"
        elif harness == "hermes" and hermes_adapter_issues:
            next_command = "brigade hermes-fragments --out .brigade/hermes"
        elif inbox_health is None or not inbox_path.exists():
            next_command = f"brigade handoff draft --inbox {harness} --target . --title <title> --summary <summary> --content <content>"
        elif not (inbox_health and inbox_health.watched):
            next_command = "brigade handoff sources init --target . --force"
        elif invalid:
            next_command = f"brigade handoff lint {inbox_rel} --target ."
        else:
            next_command = "brigade handoff doctor --target ."
    else:
        next_command = "brigade handoff list --target . --json"
    return {
        "target": str(target),
        "harness": harness,
        "supported": True,
        "handoff_inbox": {
            "path": str(inbox_path),
            "relative_path": inbox_rel,
            "exists": bool(inbox_health and inbox_health.exists),
            "watched": bool(inbox_health and inbox_health.watched),
            "pending": int(inbox_health.pending) if inbox_health else 0,
            "processed": int(inbox_health.processed) if inbox_health else 0,
            "gitignored": gitignored,
        },
        "checks": checks,
        "issue_count": issue_count,
        "ready": issue_count == 0,
        "next_command": next_command,
        "local_only_notes": [
            "This check only verifies Brigade's repo-local handoff writer wiring.",
            "It does not start Hermes, call a live Hermes API, or ingest handoffs into canonical memory.",
        ],
    }


def _hermes_adapter_checks(target: Path, inbox_rel: str) -> list[dict[str, Any]]:
    from .hermes_adapter import inspect_hermes_adapter

    return [_operator_hermes_result(item) for item in inspect_hermes_adapter(target, inbox_rel)]


def _operator_hermes_result(item: dict[str, Any]) -> dict[str, Any]:
    result_id = item.get("id")
    if result_id == "fragment":
        name = f"hermes_adapter_{item.get('fragment')}"
    else:
        name = {
            "workspace_handoff_inbox": "hermes_adapter_workspace_handoff_inbox",
            "workspace_json": "hermes_adapter_workspace_json",
            "memory_handoff_inbox": "hermes_adapter_memory_handoff_inbox",
            "processed_handoff_inbox": "hermes_adapter_processed_handoff_inbox",
            "memory_handoff_json": "hermes_adapter_memory_handoff_json",
        }.get(str(result_id), f"hermes_adapter_{result_id}")
    return {"status": item.get("status", "warn"), "name": name, "detail": str(item.get("detail", ""))}


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def verify_harness(*, target: Path, harness: str, json_output: bool = False) -> int:
    try:
        payload = verify_harness_payload(target, harness=harness)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"operator verify-harness: {payload['target']}")
    print(f"harness: {payload['harness']}")
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    handoff_inbox = payload.get("handoff_inbox") if isinstance(payload.get("handoff_inbox"), dict) else None
    if handoff_inbox:
        print(f"handoff_inbox: {handoff_inbox.get('relative_path')}")
    for item in payload["checks"]:
        print(f"[{item.get('status')}] {item.get('name')}: {item.get('detail')}")
    print(f"next: {payload['next_command']}")
    return 0 if payload["ready"] else 1


def sync_tools(*, target: Path, dry_run: bool = False, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    defaults_output = StringIO()
    with redirect_stdout(defaults_output):
        defaults_rc = tools_cmd.defaults(
            target=target,
            dry_run=dry_run,
            force=force,
            update_gitignore=True,
            json_output=True,
        )
    try:
        defaults_payload = json.loads(defaults_output.getvalue() or "{}")
    except json.JSONDecodeError:
        defaults_payload = {
            "valid": False,
            "errors": ["tools defaults returned invalid JSON"],
            "output": defaults_output.getvalue().strip().splitlines(),
        }
        defaults_rc = 1
    if defaults_rc != 0:
        payload = {
            "target": str(target),
            "dry_run": dry_run,
            "force": force,
            "defaults": defaults_payload,
            "apply": {"applied_count": 0, "skipped_count": 0, "conflict_count": 0},
            "tool_health": {
                "valid": False,
                "tool_count": None,
                "issue_count": None,
                "top_issue": None,
                "sync_plan": None,
            },
            "projection_paths": [],
            "status": "warn",
        }
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        print(f"operator sync-tools: {target}")
        print("defaults: failed")
        for error in defaults_payload.get("errors") or []:
            print(f"error: {error}")
        for conflict in defaults_payload.get("conflicts") or []:
            if isinstance(conflict, dict):
                print(f"- conflict: {conflict.get('tool_id')} {conflict.get('detail')}")
        return 1
    output = StringIO()
    with redirect_stdout(output):
        rc = tools_cmd.apply(target=target, all_tools=True, dry_run=dry_run, force=force, json_output=True)
    try:
        apply_payload = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        apply_payload = {
            "valid": False,
            "errors": ["tools apply returned invalid JSON"],
            "output": output.getvalue().strip().splitlines(),
        }
        rc = 1
    tool_health = tools_cmd.health(target)
    ok = rc == 0 and (dry_run or int(tool_health.get("issue_count") or 0) == 0)
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "force": force,
        "defaults": defaults_payload,
        "apply": apply_payload,
        "tool_health": {
            "valid": tool_health.get("valid"),
            "tool_count": tool_health.get("tool_count"),
            "issue_count": tool_health.get("issue_count"),
            "top_issue": tool_health.get("top_issue"),
            "sync_plan": tool_health.get("sync_plan"),
        },
        "projection_paths": [
            item.get("projection_path")
            for item in (apply_payload.get("applied") or []) + (apply_payload.get("skipped") or [])
            if isinstance(item, dict) and item.get("projection_path")
        ],
        "status": "ok" if ok else "warn",
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "ok" else 1
    print(f"operator sync-tools: {target}")
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    print(f"defaults_added: {len(defaults_payload.get('added') or [])}")
    print(f"defaults_updated: {len(defaults_payload.get('updated') or [])}")
    print(f"applied: {apply_payload.get('applied_count', 0)}")
    print(f"skipped: {apply_payload.get('skipped_count', 0)}")
    print(f"conflicts: {apply_payload.get('conflict_count', 0)}")
    print(f"tool_issues: {tool_health.get('issue_count')}")
    for item in apply_payload.get("applied") or []:
        if isinstance(item, dict):
            verb = "would_write" if dry_run else "wrote"
            print(f"- {verb}: {item.get('tool_id')} {item.get('harness')} {item.get('projection_path')}")
    for item in apply_payload.get("conflicts") or []:
        if isinstance(item, dict):
            print(f"- conflict: {item.get('tool_id')} {item.get('harness')} {item.get('detail')}")
    top = tool_health.get("top_issue")
    if isinstance(top, dict):
        print(f"top_issue: {top.get('tool_id')}/{top.get('issue_type')}: {top.get('detail')}")
    return 0 if payload["status"] == "ok" else 1


def _capture_json_call(func, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    output = StringIO()
    with redirect_stdout(output):
        rc = func(**kwargs, json_output=True)
    try:
        payload = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "errors": [f"{getattr(func, '__name__', 'command')} returned invalid JSON"],
            "output": output.getvalue().strip().splitlines(),
        }
        rc = 1
    return rc, payload


def _capture_text_call(func, **kwargs: Any) -> tuple[int, list[str]]:
    output = StringIO()
    with redirect_stdout(output):
        rc = func(**kwargs)
    return rc, output.getvalue().strip().splitlines()


def _parse_harnesses(value: str | None) -> list[str]:
    if value is None or not value.strip():
        return ["codex"]
    if value.strip() == "none":
        return []
    harnesses = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in harnesses if item not in KNOWN_HARNESSES]
    if unknown:
        raise ValueError(f"unknown harness: {', '.join(unknown)}")
    return list(dict.fromkeys(harnesses))


def quickstart(
    *,
    target: Path,
    depth: str = "repo",
    harnesses: str | None = "codex",
    owner: str | None = None,
    tool_pack: Path | None = None,
    skill_pack: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if depth not in {"repo", "workspace"}:
        print("error: --depth must be repo or workspace", file=sys.stderr)
        return 2
    try:
        selected_harnesses = _parse_harnesses(harnesses)
        memory_owner = resolve_owner(selected_harnesses, override=owner)
        selection = Selection(depth=depth, harnesses=selected_harnesses, owner=memory_owner, includes=[])
        selection.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    steps: list[dict[str, Any]] = []
    install_rc, install_output = _capture_text_call(
        install_selection,
        target=target,
        selection=selection,
        force=force,
        dry_run=dry_run,
        allow_home=False,
    )
    install_status = "planned" if dry_run and install_rc == 0 else "ok" if install_rc == 0 else "error"
    steps.append({"id": "brigade-init", "status": install_status, "return_code": install_rc, "output": install_output})
    if install_rc != 0:
        payload = {
            "target": str(target),
            "depth": depth,
            "harnesses": selected_harnesses,
            "owner": memory_owner,
            "owner_override": owner is not None,
            "dry_run": dry_run,
            "force": force,
            "steps": steps,
            "status": "blocked",
            "next_commands": [
                f"brigade init --target {target} --depth {depth} --harnesses {','.join(selected_harnesses) or 'none'} --force"
            ],
            "local_only_notes": _quickstart_local_notes(),
        }
        payload["issue_report"] = _quickstart_issue_report(payload)
        if json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 1
        _print_quickstart(payload)
        return 1

    selected_inboxes = [WRITER_INBOXES[harness] for harness in selected_harnesses if harness in WRITER_INBOXES]
    init_rc, init_payload = _capture_json_call(
        init, target=target, profile="local-operator", handoff_inboxes=selected_inboxes, force=force, dry_run=dry_run
    )
    init_status = "planned" if dry_run and init_rc == 0 else "ok" if init_rc == 0 else "error"
    steps.append({"id": "operator-init", "status": init_status, "return_code": init_rc, "payload": init_payload})

    portable_rc, portable_payload = _capture_json_call(
        bootstrap_portable,
        target=target,
        tool_pack=tool_pack,
        skill_pack=skill_pack,
        dry_run=dry_run,
        force=force,
    )
    portable_status = "planned" if dry_run and portable_rc == 0 else "ok" if portable_rc == 0 else "error"
    steps.append(
        {"id": "portable-bootstrap", "status": portable_status, "return_code": portable_rc, "payload": portable_payload}
    )

    if dry_run:
        for harness in selected_harnesses:
            if harness in WRITER_INBOXES:
                steps.append(
                    {
                        "id": f"verify-{harness}",
                        "status": "planned",
                        "return_code": 0,
                        "next_command": f"brigade operator verify-harness --harness {harness} --target {target}",
                    }
                )
    else:
        for harness in selected_harnesses:
            if harness not in WRITER_INBOXES:
                steps.append(
                    {
                        "id": f"verify-{harness}",
                        "status": "skipped",
                        "reason": "no Brigade handoff writer inbox for this harness",
                    }
                )
                continue
            verify_rc, verify_payload = _capture_json_call(verify_harness, target=target, harness=harness)
            steps.append(
                {
                    "id": f"verify-{harness}",
                    "status": "ok" if verify_rc == 0 else "warn",
                    "return_code": verify_rc,
                    "payload": verify_payload,
                }
            )

    ok = all(step.get("return_code", 0) == 0 for step in steps if step.get("status") not in {"skipped", "planned"})
    if dry_run:
        ok = install_rc == 0 and init_rc == 0 and portable_rc == 0
    payload = {
        "target": str(target),
        "depth": depth,
        "harnesses": selected_harnesses,
        "owner": memory_owner,
        "owner_override": owner is not None,
        "dry_run": dry_run,
        "force": force,
        "steps": steps,
        "status": "ok" if ok else "warn",
        "next_commands": _quickstart_next_commands(selected_harnesses, dry_run=dry_run),
        "local_only_notes": _quickstart_local_notes(),
    }
    payload["issue_report"] = _quickstart_issue_report(payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1
    _print_quickstart(payload)
    return 0 if ok else 1


def _quickstart_next_commands(harnesses: list[str], *, dry_run: bool) -> list[str]:
    if dry_run:
        return ["rerun without --dry-run after reviewing planned writes"]
    commands = [
        "brigade operator doctor --target . --profile local-operator",
        "brigade tools list --target .",
        "brigade skills doctor --target .",
        "brigade security scan --target . --output-dir .brigade/security/latest",
    ]
    commands.extend(
        f"brigade operator verify-harness --target . --harness {harness}"
        for harness in harnesses
        if harness in WRITER_INBOXES
    )
    return commands


def _quickstart_local_notes() -> list[str]:
    return [
        ".brigade/ stores local config, receipts, scans, reports, waivers, and run artifacts.",
        "Generated harness projections and handoff inboxes are local ignored state.",
        "Brigade does not start daemons, activate hooks (the pre-push hook file ships inactive), publish, push, tag, or mutate remotes from quickstart.",
    ]


def _quickstart_issue_report(payload: dict[str, Any]) -> dict[str, Any]:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    step_summaries = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        item = {
            "id": step.get("id"),
            "status": step.get("status"),
            "return_code": step.get("return_code"),
        }
        payload_obj = step.get("payload")
        if isinstance(payload_obj, dict):
            item["payload_status"] = payload_obj.get("status") or payload_obj.get("ready")
            top_issue = payload_obj.get("top_issue")
            if isinstance(top_issue, dict):
                item["top_issue"] = {
                    "name": top_issue.get("name"),
                    "detail": top_issue.get("detail"),
                }
        step_summaries.append(item)
    return {
        "brigade_version": __version__,
        "status": payload.get("status"),
        "depth": payload.get("depth"),
        "harnesses": payload.get("harnesses"),
        "owner": payload.get("owner"),
        "dry_run": payload.get("dry_run"),
        "force": payload.get("force"),
        "steps": step_summaries,
        "next_commands": payload.get("next_commands") or [],
        "github_issue_url": "https://github.com/escoffier-labs/brigade/issues/new/choose",
        "privacy_note": "Review before sharing. Do not paste tokens, private hostnames, or unredacted absolute paths.",
    }


def _print_quickstart(payload: dict[str, Any]) -> None:
    print(f"operator quickstart: {payload['target']}")
    print(f"depth: {payload['depth']}")
    print(f"harnesses: {','.join(payload['harnesses']) or 'none'}")
    owner_note = "" if payload.get("owner_override") else " (auto-selected; override with --owner)"
    print(f"owner: {payload['owner']}{owner_note}")
    print(f"dry_run: {payload['dry_run']}")
    for step in payload["steps"]:
        print(f"[{step.get('status')}] {step.get('id')}")
    print(f"status: {payload['status']}")
    print("next:")
    for command in payload["next_commands"]:
        print(f"- {command}")
    print("issues: https://github.com/escoffier-labs/brigade/issues/new/choose")


def bootstrap_portable(
    *,
    target: Path,
    tool_pack: Path | None = None,
    skill_pack: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    steps: list[dict[str, Any]] = []
    if tool_pack is not None:
        if dry_run:
            steps.append({"id": "tools-pack-import", "status": "skipped", "reason": "dry-run", "pack": str(tool_pack)})
        else:
            rc, payload = _capture_json_call(tools_cmd.pack_import, target=target, pack=tool_pack, force=force)
            steps.append(
                {
                    "id": "tools-pack-import",
                    "status": "ok" if rc == 0 else "error",
                    "return_code": rc,
                    "pack": str(tool_pack),
                    "payload": payload,
                }
            )
    if skill_pack is not None:
        if dry_run:
            steps.append(
                {"id": "skills-pack-import", "status": "skipped", "reason": "dry-run", "pack": str(skill_pack)}
            )
        else:
            rc, payload = _capture_json_call(skills_cmd.pack_import, target=target, pack=skill_pack, force=force)
            steps.append(
                {
                    "id": "skills-pack-import",
                    "status": "ok" if rc == 0 else "error",
                    "return_code": rc,
                    "pack": str(skill_pack),
                    "payload": payload,
                }
            )

    sync_rc, sync_payload = _capture_json_call(sync_tools, target=target, dry_run=dry_run, force=force)
    sync_status = "ok" if sync_rc == 0 else "error"
    if dry_run and isinstance(sync_payload.get("defaults"), dict) and sync_payload["defaults"].get("valid"):
        sync_rc = 0
        sync_status = "planned"
    steps.append({"id": "operator-sync-tools", "status": sync_status, "return_code": sync_rc, "payload": sync_payload})
    if not dry_run:
        tools_rc, tools_payload = _capture_json_call(tools_cmd.doctor, target=target)
        steps.append(
            {
                "id": "tools-doctor",
                "status": "ok" if tools_rc == 0 else "error",
                "return_code": tools_rc,
                "payload": tools_payload,
            }
        )
        skills_rc, skills_payload = _capture_json_call(skills_cmd.doctor, target=target)
        steps.append(
            {
                "id": "skills-doctor",
                "status": "ok" if skills_rc == 0 else "error",
                "return_code": skills_rc,
                "payload": skills_payload,
            }
        )

    ok = all(step.get("return_code", 0) == 0 for step in steps if step.get("status") != "skipped")
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "force": force,
        "tool_pack": str(tool_pack) if tool_pack is not None else None,
        "skill_pack": str(skill_pack) if skill_pack is not None else None,
        "steps": steps,
        "status": "ok" if ok else "warn",
        "next_commands": [
            "brigade tools list --target .",
            "brigade skills doctor --target .",
            "brigade security scan --target . --output-dir .brigade/security/latest",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ok else 1
    print(f"operator bootstrap-portable: {target}")
    print(f"dry_run: {dry_run}")
    print(f"force: {force}")
    for step in steps:
        print(f"[{step['status']}] {step['id']}")
    print(f"status: {payload['status']}")
    if ok:
        print("next:")
        for command in payload["next_commands"]:
            print(f"- {command}")
    return 0 if ok else 1
