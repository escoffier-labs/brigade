"""Read-only local operator center views."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import html
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    actionqueue,
    chat_cmd,
    context_cmd,
    handoff_cmd,
    learn_cmd,
    memory_cmd,
    notifications_cmd,
    pantry_cmd,
    phases_cmd,
    projects_cmd,
    release_cmd,
    repos_cmd,
    reportstore,
    research_cmd,
    roadmap_cmd,
    security_cmd,
    tools_cmd,
    work_cmd,
)
from ..localio import (
    parse_iso_datetime,
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)
from ..render import emit

from . import schema_ops as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _action_priority_rank(action: dict[str, Any]) -> tuple[int, int]:
    severity = str(action.get("severity") or "")
    priority = str(action.get("priority") or "")
    status = str(action.get("status") or "")
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(severity, 4)
    priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(priority, 4)
    status_rank = {"active": 0, "pending": 1, "deferred": 2, "done": 3, "archived": 4}.get(status, 5)
    return (status_rank, min(severity_rank, priority_rank))


def _planned_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    plan = _action_plan(report)
    report_id = str(report.get("report_id") or "planned")
    report_fingerprint = str(
        report.get("report_fingerprint")
        or _fingerprint_payload({"reviews": report.get("reviews"), "activity": report.get("activity")})
    )
    reviewed_at = _report_reviewed_at(report)
    created = _now().isoformat()
    actions: list[dict[str, Any]] = []
    seen_source_items: set[str] = set()
    for group, items in plan["groups"].items():
        for item in items:
            if not isinstance(item, dict):
                continue
            source_subsystem = str(item.get("subsystem") or "unknown")
            source_local_id = str(item.get("local_id") or item.get("id") or "unknown")
            source_item_id = f"{source_subsystem}:{source_local_id}"
            if source_item_id in seen_source_items:
                continue
            seen_source_items.add(source_item_id)
            source_fingerprint = _fingerprint_payload(
                {
                    "report_fingerprint": report_fingerprint,
                    "source_item_id": source_item_id,
                }
            )
            action_id = f"act-{source_fingerprint[:16]}"
            actions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "action_id": action_id,
                    "source_report_id": report_id,
                    "source_report_path": report.get("path"),
                    "source_report_fingerprint": report_fingerprint,
                    "source_group": group,
                    "source_subsystem": source_subsystem,
                    "source_local_id": source_local_id,
                    "status": "pending",
                    "priority": item.get("priority") if isinstance(item.get("priority"), str) else None,
                    "severity": item.get("severity") if isinstance(item.get("severity"), str) else None,
                    "safe_summary": str(item.get("safe_summary") or "operator action"),
                    "suggested_command": str(item.get("suggested_next_command") or ""),
                    "created_at": created,
                    "updated_at": created,
                    "reviewed_at": reviewed_at,
                    "source_fingerprint": source_fingerprint,
                }
            )
    actions.sort(
        key=lambda action: (
            _action_priority_rank(action),
            str(action.get("source_group") or ""),
            str(action.get("source_local_id") or ""),
        )
    )
    return actions


def _find_action(target: Path, action_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    actions = _read_actions(target)
    action, error = actionqueue.find_action(actions, action_id, id_field="action_id", label="action")
    return actions, action, error


def _action_counts(actions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(ACTION_STATUSES)}
    for action in actions:
        status = str(action.get("status") or "pending")
        if status not in counts:
            counts[status] = 0
        counts[status] += 1
    return counts


def _action_age_hours(action: dict[str, Any], *, now: datetime, fields: tuple[str, ...]) -> float | None:
    for field in fields:
        stamp = _parse_time(action.get(field))
        if stamp is not None:
            return (now - stamp).total_seconds() / 3600
    return None


def _action_policy_issue(action: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    action_id = str(action.get("action_id") or "")
    status = str(action.get("status") or "pending")
    if status == "pending":
        age = _action_age_hours(action, now=now, fields=("created_at", "updated_at"))
        if age is not None and age > ACTION_PENDING_STALE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_stale_pending",
                "action_id": action_id,
                "detail": f"{action_id} has been pending for {age:.1f}h",
                "suggested_next_command": f"brigade center actions start {action_id}",
                "age_hours": round(age, 2),
            }
    elif status == "active":
        age = _action_age_hours(action, now=now, fields=("started_at", "updated_at", "created_at"))
        if age is not None and age > ACTION_ACTIVE_STALE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_stale_active",
                "action_id": action_id,
                "detail": f"{action_id} has been active for {age:.1f}h",
                "suggested_next_command": f"brigade center actions done {action_id}",
                "age_hours": round(age, 2),
            }
    elif status == "deferred":
        age = _action_age_hours(action, now=now, fields=("deferred_at", "updated_at", "created_at"))
        if age is not None and age > ACTION_DEFERRED_STALE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_deferred_too_long",
                "action_id": action_id,
                "detail": f"{action_id} has been deferred for {age:.1f}h",
                "suggested_next_command": f"brigade center actions show {action_id}",
                "age_hours": round(age, 2),
            }
    elif status == "done":
        age = _action_age_hours(action, now=now, fields=("completed_at", "updated_at", "created_at"))
        if age is not None and age > ACTION_DONE_ARCHIVE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_completed_unarchived",
                "action_id": action_id,
                "detail": f"{action_id} has been completed for {age:.1f}h and should be archived",
                "suggested_next_command": "brigade center actions archive --completed",
                "age_hours": round(age, 2),
            }
    return None


def _action_policy_issues(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = _now()
    issues = [_action_policy_issue(action, now=now) for action in actions]
    return [issue for issue in issues if issue is not None]


def actions_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    open_actions = [action for action in actions if action.get("status") in {"pending", "active", "deferred"}]
    open_actions.sort(key=_action_priority_rank)
    checks: list[dict[str, Any]] = []
    policy_issues = _action_policy_issues(actions)
    if open_actions:
        top = open_actions[0]
        checks.append(
            {
                "status": "warn",
                "name": "center_actions_open",
                "detail": f"{len(open_actions)} open operator action(s)",
                "suggested_next_command": f"brigade center actions show {top.get('action_id')}",
            }
        )
    checks.extend(policy_issues)
    return {
        "actions_path": str(_actions_path(target)),
        "action_count": len(actions),
        "open_count": len(open_actions),
        "counts": _action_counts(actions),
        "policy": {
            "pending_stale_hours": ACTION_PENDING_STALE_HOURS,
            "active_stale_hours": ACTION_ACTIVE_STALE_HOURS,
            "deferred_stale_hours": ACTION_DEFERRED_STALE_HOURS,
            "done_archive_hours": ACTION_DONE_ARCHIVE_HOURS,
        },
        "policy_issues": policy_issues,
        "policy_issue_count": len(policy_issues),
        "top_action": open_actions[0] if open_actions else None,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def actions_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-doctor"),
        "target": str(target),
        "health": actions_health(target),
    }
    if json_output:
        return emit(payload, json_output, [], 0)
    health = payload["health"]
    text_lines = [
        f"center actions doctor: {target}",
        f"actions: {health['action_count']}",
        f"open: {health['open_count']}",
        f"policy_issues: {health['policy_issue_count']}",
    ]
    for issue in health["policy_issues"]:
        text_lines.append(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return emit(payload, json_output, text_lines, 0)


def _action_policy_import_record(issue: dict[str, Any], actions_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    action_id = str(issue.get("action_id") or "")
    action = actions_by_id.get(action_id, {})
    source_key = f"center-action-policy:{issue.get('name')}:{action_id}"
    fingerprint = _fingerprint_payload(
        {
            "source_key": source_key,
            "source_fingerprint": action.get("source_fingerprint"),
            "status": action.get("status"),
            "issue": issue.get("name"),
        }
    )
    return {
        "kind": "task",
        "source": "center-action-policy",
        "text": str(issue.get("detail") or f"Review operator action {action_id}"),
        "type": "task",
        "priority": "high" if issue.get("name") == "center_action_stale_active" else "normal",
        "acceptance": [
            f"Operator action `{action_id}` is reviewed.",
            "The action is started, completed, deferred with a fresh reason, or archived as appropriate.",
            "No suggested command is executed automatically by this import.",
        ],
        "metadata": {
            "source_item_key": source_key,
            "source_fingerprint": fingerprint,
            "issue_type": issue.get("name"),
            "action_id": action_id,
            "action_status": action.get("status"),
            "source_report_id": action.get("source_report_id"),
            "source_subsystem": action.get("source_subsystem"),
            "source_local_id": action.get("source_local_id"),
            "safe_summary": action.get("safe_summary"),
            "suggested_command": action.get("suggested_command"),
            "suggested_next_command": issue.get("suggested_next_command"),
        },
    }


def actions_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    actions_by_id = {str(action.get("action_id") or ""): action for action in actions}
    issues = _action_policy_issues(actions)
    records = [_action_policy_import_record(issue, actions_by_id) for issue in issues]
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-import-issues"),
        "target": str(target),
        "dry_run": dry_run,
        "issue_count": len(issues),
        "created_count": len(imported),
        "skipped_count": len(skipped),
        "dismissed_count": len(skipped_dismissed),
        "imports_path": str(work_cmd._imports_path(target)),
        "issues": issues,
        "created": imported,
        "skipped": skipped,
        "dismissed": skipped_dismissed,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("center actions import-issues")
    print(f"dry_run: {dry_run}")
    print(f"issues: {len(issues)}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(skipped_dismissed)}")
    return 0


def actions_plan(*, target: Path, report_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actions = _planned_actions(report)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-plan"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": report.get("path"),
        "report_review_status": _report_review_status(report),
        "actions_root": str(_actions_root(target)),
        "actions": actions,
        "action_count": len(actions),
        "write_required": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center actions plan: {report.get('report_id')}")
    print(f"actions: {len(actions)}")
    print(f"report_status: {payload['report_review_status'] or 'unreviewed'}")
    print("run: brigade center actions build latest")
    for action in actions[:20]:
        print(
            f"- {action['action_id']} {action['source_group']} {action['source_local_id']} [{action['status']}] {action['safe_summary']}"
        )
        if action.get("suggested_command"):
            print(f"  next: {action['suggested_command']}")
    return 0


def actions_build(
    *, target: Path, report_id: str = "latest", allow_unreviewed: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    review_status = _report_review_status(report)
    if review_status not in {"reviewed", "deferred"} and not allow_unreviewed:
        print(
            "error: source report must be closed out as reviewed or deferred, or pass --allow-unreviewed",
            file=sys.stderr,
        )
        return 2
    planned = _planned_actions(report)
    existing = _read_actions(target)
    created, skipped = actionqueue.merge_planned(existing, _read_action_archive(target), planned)
    _write_actions(target, existing)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-build"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": report.get("path"),
        "report_review_status": review_status,
        "actions_path": str(_actions_path(target)),
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created_actions": created,
        "skipped_actions": skipped,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center actions build: {report.get('report_id')}")
    print(f"created: {len(created)}")
    print(f"skipped: {len(skipped)}")
    print(f"path: {_actions_path(target)}")
    return 0


def actions_list(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    actions.sort(key=lambda action: (_action_priority_rank(action), str(action.get("updated_at") or "")))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-list"),
        "target": str(target),
        "actions_path": str(_actions_path(target)),
        "actions": actions[:limit],
        "action_count": len(actions),
        "counts": _action_counts(actions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center actions: {target}")
    print(f"actions_path: {_actions_path(target)}")
    for action in actions[:limit]:
        print(
            f"- {action.get('action_id')} [{action.get('status')}] {action.get('source_group')} {action.get('source_local_id')}: {action.get('safe_summary')}"
        )
        if action.get("suggested_command"):
            print(f"  next: {action.get('suggested_command')}")
    return 0


def actions_show(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-show"),
        "target": str(target),
        "action": action,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center action: {action.get('action_id')}")
    print(f"status: {action.get('status')}")
    print(f"source: {action.get('source_group')} {action.get('source_subsystem')}:{action.get('source_local_id')}")
    print(f"summary: {action.get('safe_summary')}")
    if action.get("suggested_command"):
        print(f"next: {action.get('suggested_command')}")
    return 0


def _set_action_status(
    *,
    target: Path,
    action_id: str,
    status: str,
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if status not in ACTION_STATUSES:
        print(f"error: invalid action status: {status}", file=sys.stderr)
        return 2
    actions, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actionqueue.stamp_status(action, status, now=_now().isoformat(), reason=reason)
    _write_actions(target, actions)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema(f"center-actions-{status}"),
        "target": str(target),
        "action": action,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center action {status}: {action.get('action_id')}")
    print(f"status: {action.get('status')}")
    return 0


def actions_start(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_action_status(target=target, action_id=action_id, status="active", json_output=json_output)


def actions_done(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_action_status(target=target, action_id=action_id, status="done", json_output=json_output)


def actions_defer(*, target: Path, action_id: str, reason: str, json_output: bool = False) -> int:
    if not reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    return _set_action_status(
        target=target, action_id=action_id, status="deferred", reason=reason, json_output=json_output
    )


def actions_archive_completed(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    archived, remaining = actionqueue.split_archived_completed(actions, now=_now().isoformat())
    _write_actions(target, remaining)
    _append_action_archive(target, archived)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-archive"),
        "target": str(target),
        "archived_count": len(archived),
        "archive_path": str(_actions_archive_path(target)),
        "actions_path": str(_actions_path(target)),
        "archived_actions": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("center actions archive: completed")
    print(f"archived: {len(archived)}")
    print(f"path: {_actions_archive_path(target)}")
    return 0
