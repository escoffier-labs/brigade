# ruff: noqa: F401
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import actionqueue, config as brigade_config, reportstore, toml_compat as tomllib, work_cmd
from ..budgets import HANDOFF_BACKLOG_STALE_DAYS
from ..install import apply_gitignore
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)
from ..render import emit
from ..selection import Selection, WRITER_INBOXES
from . import constants, fleet, sweeps


def _actions_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "actions"


def _actions_path(target: Path) -> Path:
    return _actions_root(target) / "actions.json"


def _actions_archive_path(target: Path) -> Path:
    return _actions_root(target) / "archive.jsonl"


def _dispatch_reports_root(target: Path) -> Path:
    return _actions_root(target) / "dispatch-reports"


def _read_actions(target: Path) -> list[dict[str, Any]]:
    return actionqueue.read_actions(_actions_path(target))


def _write_actions(target: Path, actions: list[dict[str, Any]]) -> None:
    _write_json(_actions_path(target), {"updated_at": _now().isoformat(), "actions": actions})


def _dict_or_empty(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _read_action_archive(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_actions_archive_path(target))


def _action_target_entry(target: Path, action: dict[str, Any]) -> tuple[constants.RepoEntry | None, str | None]:
    repo_id = str(action.get("repo_id") or "")
    entries, errors, config_loaded = fleet._load_config(target)
    if not config_loaded:
        return None, "repo fleet config is missing"
    if errors:
        return None, "; ".join(fleet._safe_text(error, target, "repo-fleet", "repo fleet") for error in errors)
    for entry in entries:
        if entry.repo_id == repo_id:
            if not entry.path.is_dir():
                return None, f"target repo is not reachable: {repo_id}"
            return entry, None
    return None, f"repo not found: {repo_id}"


def _action_acceptance(action: dict[str, Any]) -> list[str]:
    summary = str(action.get("safe_summary") or "repo fleet action")
    return [
        "The target repo issue is resolved or explicitly deferred with rationale.",
        "Relevant local verification, review, or closeout evidence is captured in the target repo when applicable.",
        "No private repo names, paths, raw logs, scanner output, secrets, or guidance file contents are copied into public artifacts.",
        f"Fleet action remains traceable from {action.get('fleet_action_id')}: {work_cmd._short(summary, 120)}",
    ]


def _action_task_fields(action: dict[str, Any]) -> tuple[str, str, str]:
    subsystem = str(action.get("source_subsystem") or "")
    if subsystem in {"security", "security-scan"}:
        return "security", "high", "security-follow-up"
    if subsystem in {"code-review", "review-finding"}:
        return "bug", "high", "bugfix"
    if subsystem in {"handoff", "memory-care", "context"}:
        return "docs", "normal", "docs"
    return "docs", "normal", "docs"


def _action_import_record(action: dict[str, Any]) -> dict[str, Any]:
    task_type, priority, template = _action_task_fields(action)
    action_id = str(action.get("fleet_action_id") or "fleet-action")
    source_fingerprint = str(action.get("source_fingerprint") or fleet._fingerprint_payload(action))
    metadata = {
        "fleet_action_id": action_id,
        "source_item_key": action_id,
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "source_report_id": action.get("source_report_id"),
        "source_report_fingerprint": action.get("source_report_fingerprint"),
        "source_sweep_id": action.get("source_sweep_id"),
        "source_subsystem": action.get("source_subsystem"),
        "source_local_id": action.get("source_local_id"),
        "source_fingerprint": source_fingerprint,
        "suggested_command": action.get("suggested_command"),
        "safe_summary": action.get("safe_summary"),
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
    return {
        "text": f"Resolve repo fleet action {action_id}: {work_cmd._short(str(action.get('safe_summary') or action_id), 180)}",
        "kind": "task",
        "source": "repo-fleet",
        "type": task_type,
        "priority": priority,
        "template": template,
        "acceptance": _action_acceptance(action),
        "metadata": metadata,
    }


def _target_imports_for_action(repo_path: Path, action: dict[str, Any]) -> list[dict[str, Any]]:
    action_id = str(action.get("fleet_action_id") or "")
    matches: list[dict[str, Any]] = []
    for item in work_cmd._read_imports(repo_path):
        metadata = _dict_or_empty(item.get("metadata"))
        if metadata.get("fleet_action_id") == action_id:
            matches.append(item)
    matches.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("id") or ""), reverse=True
    )
    return matches


def _supersede_prior_dispatch_imports(
    repo_path: Path, action: dict[str, Any], current_import_ids: set[str]
) -> list[str]:
    source_fingerprint = str(action.get("source_fingerprint") or fleet._fingerprint_payload(action))
    imports = work_cmd._read_imports(repo_path)
    superseded: list[str] = []
    changed = False
    now = _now().isoformat()
    for item in imports:
        metadata = _dict_or_empty(item.get("metadata"))
        if metadata.get("fleet_action_id") != action.get("fleet_action_id"):
            continue
        if item.get("id") in current_import_ids:
            continue
        if metadata.get("source_fingerprint") == source_fingerprint:
            continue
        if item.get("status") == "superseded":
            continue
        item["status"] = "superseded"
        item["updated_at"] = now
        item["superseded_at"] = now
        item["superseded_by"] = sorted(current_import_ids)[0] if current_import_ids else None
        superseded.append(str(item.get("id")))
        changed = True
    if changed:
        work_cmd._write_imports(repo_path, imports)
    return superseded


def _dispatch_state(action: dict[str, Any], repo_path: Path | None = None) -> dict[str, Any]:
    dispatch = _dict_or_empty(action.get("dispatch"))
    return {
        "status": action.get("resolution_status") or dispatch.get("status"),
        "target_import_id": dispatch.get("target_import_id") or action.get("target_import_id"),
        "target_task_id": dispatch.get("target_task_id") or action.get("target_task_id"),
        "dispatched_at": dispatch.get("dispatched_at"),
        "reconciled_at": action.get("reconciled_at"),
        "repo_label": action.get("repo_label"),
        "repo_id": action.get("repo_id"),
        "repo_path_label": f"{action.get('repo_id')}:.brigade",
    }


def _actions_for_dispatch(
    target: Path, *, action_id: str | None = None, all_reviewed: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    actions = _read_actions(target)
    if action_id:
        matches = [action for action in actions if str(action.get("fleet_action_id") or "").startswith(action_id)]
        if not matches:
            return actions, [], f"fleet action not found: {action_id}"
        if len(matches) > 1:
            return actions, [], f"fleet action id is ambiguous: {action_id}"
        return actions, [matches[0]], None
    if all_reviewed:
        selected = [
            action for action in actions if action.get("reviewed_at") and action.get("status") in {"pending", "active"}
        ]
        return actions, selected, None
    return actions, [], "fleet action id is required unless --all-reviewed is passed"


def _dispatch_plan_for_action(
    target: Path, action: dict[str, Any], *, include_deferred: bool = False
) -> dict[str, Any]:
    entry, error = _action_target_entry(target, action)
    blockers: list[str] = []
    if error:
        blockers.append(error)
    status = str(action.get("status") or "")
    if status == "deferred" and not include_deferred:
        blockers.append("deferred actions require --include-deferred")
    elif status not in {"reviewed", "pending", "active", "deferred"}:
        blockers.append(f"action status is not dispatchable: {status or 'unknown'}")
    record = _action_import_record(action)
    if entry is not None:
        record["text"] = fleet._safe_text(record.get("text"), entry.path, entry.repo_id, entry.label)
        record["acceptance"] = [
            fleet._safe_text(item, entry.path, entry.repo_id, entry.label) for item in record.get("acceptance", [])
        ]
        metadata = _dict_or_empty(record.get("metadata"))
        for key in ("safe_summary", "suggested_command"):
            if key in metadata:
                metadata[key] = fleet._safe_text(metadata[key], entry.path, entry.repo_id, entry.label)
    existing_imports = _target_imports_for_action(entry.path, action) if entry is not None else []
    same_fingerprint = []
    changed_fingerprint = []
    dismissed_same_fingerprint = []
    wanted_fingerprint = record["metadata"].get("source_fingerprint")
    for item in existing_imports:
        metadata = _dict_or_empty(item.get("metadata"))
        if metadata.get("source_fingerprint") == wanted_fingerprint:
            if item.get("status") == "dismissed":
                dismissed_same_fingerprint.append(item.get("id"))
            else:
                same_fingerprint.append(item.get("id"))
        else:
            changed_fingerprint.append(item.get("id"))
    return {
        "fleet_action_id": action.get("fleet_action_id"),
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "target_repo_label": action.get("repo_label"),
        "target_repo_id": action.get("repo_id"),
        "target_inbox_label": f"{action.get('repo_id')}:.brigade/work/imports/inbox.jsonl",
        "action_status": action.get("status"),
        "dispatchable": not blockers,
        "blockers": blockers,
        "record": record,
        "existing_same_fingerprint_import_ids": same_fingerprint,
        "existing_changed_fingerprint_import_ids": changed_fingerprint,
        "dismissed_same_fingerprint_import_ids": dismissed_same_fingerprint,
        "suggested_next_command": "brigade work import plan <import-id>",
    }


def actions_dispatch_plan(
    *,
    target: Path,
    action_id: str | None = None,
    all_reviewed: bool = False,
    include_deferred: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    _, selected, error = _actions_for_dispatch(target, action_id=action_id, all_reviewed=all_reviewed)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1 if "not found" in error else 2
    plans = [_dispatch_plan_for_action(target, action, include_deferred=include_deferred) for action in selected]
    payload = {
        "target": str(target),
        "plans": plans,
        "plan_count": len(plans),
        "blocker_count": sum(len(plan["blockers"]) for plan in plans),
    }
    rc = 0 if payload["blocker_count"] == 0 else 2
    text_lines = [
        "repo fleet action dispatch plan",
        f"actions: {len(plans)}",
        *[
            f"- {plan.get('fleet_action_id')} {plan.get('repo_id')} dispatchable={plan.get('dispatchable')}"
            for plan in plans
        ],
    ]
    return emit(payload, json_output, text_lines, rc)


def actions_dispatch_apply(
    *,
    target: Path,
    action_id: str | None = None,
    all_reviewed: bool = False,
    include_deferred: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    actions, selected, error = _actions_for_dispatch(target, action_id=action_id, all_reviewed=all_reviewed)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1 if "not found" in error else 2
    results: list[dict[str, Any]] = []
    now = _now().isoformat()
    changed = False
    for action in selected:
        plan = _dispatch_plan_for_action(target, action, include_deferred=include_deferred)
        if plan["blockers"]:
            results.append(
                {"fleet_action_id": action.get("fleet_action_id"), "status": "blocked", "blockers": plan["blockers"]}
            )
            continue
        entry, _ = _action_target_entry(target, action)
        assert entry is not None
        imported, skipped, skipped_dismissed = work_cmd._append_import_records(
            entry.path, [plan["record"]], dry_run=dry_run
        )
        imported_ids = {str(item.get("id")) for item in imported if item.get("id")}
        skipped_ids = {str(item_id) for item_id in plan.get("existing_same_fingerprint_import_ids", []) if item_id}
        dismissed_ids = {str(item_id) for item_id in plan.get("dismissed_same_fingerprint_import_ids", []) if item_id}
        superseded_ids = [] if dry_run else _supersede_prior_dispatch_imports(entry.path, action, imported_ids)
        target_import_id = next(iter(imported_ids or skipped_ids or dismissed_ids), None)
        status = (
            "dry-run" if dry_run else ("created" if imported else ("dismissed" if skipped_dismissed else "skipped"))
        )
        if not dry_run:
            action["dispatch"] = {
                "status": "dispatched" if imported or skipped else "dismissed" if skipped_dismissed else "dispatched",
                "target_import_id": target_import_id,
                "target_inbox_label": f"{action.get('repo_id')}:.brigade/work/imports/inbox.jsonl",
                "dispatched_at": now,
                "source_fingerprint": action.get("source_fingerprint"),
                "superseded_import_ids": superseded_ids,
                "target_evidence_fingerprint": fleet._fingerprint_payload(
                    _latest_safe_receipts(entry.path, entry.repo_id, entry.label)
                ),
            }
            action["resolution_status"] = (
                "dispatched" if imported or skipped else "dismissed" if skipped_dismissed else "dispatched"
            )
            action["updated_at"] = now
            changed = True
        results.append(
            {
                "fleet_action_id": action.get("fleet_action_id"),
                "repo_id": action.get("repo_id"),
                "repo_label": action.get("repo_label"),
                "status": status,
                "imported_count": len(imported),
                "skipped_count": len(skipped),
                "dismissed_count": len(skipped_dismissed),
                "target_import_id": target_import_id,
                "superseded_import_ids": superseded_ids,
            }
        )
    if changed:
        _write_actions(target, actions)
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "result_count": len(results),
        "created_count": sum(1 for item in results if item.get("status") == "created"),
        "skipped_count": sum(1 for item in results if item.get("status") == "skipped"),
        "dismissed_count": sum(1 for item in results if item.get("status") == "dismissed"),
        "blocked_count": sum(1 for item in results if item.get("status") == "blocked"),
        "results": results,
    }
    rc = 0 if payload["blocked_count"] == 0 else 2
    text_lines = [
        "repo fleet action dispatch apply",
        f"results: {len(results)}",
        f"created: {payload['created_count']}",
        f"blocked: {payload['blocked_count']}",
    ]
    return emit(payload, json_output, text_lines, rc)


def _dispatch_import_summary(
    entry: constants.RepoEntry, action: dict[str, Any], item: dict[str, Any]
) -> dict[str, Any]:
    metadata = _dict_or_empty(item.get("metadata"))
    wanted_fingerprint = str(action.get("source_fingerprint") or "")
    source_fingerprint = str(metadata.get("source_fingerprint") or "")
    return {
        "import_id": item.get("id"),
        "status": item.get("status"),
        "task_id": item.get("task_id"),
        "source_fingerprint": source_fingerprint,
        "fingerprint_matches_action": bool(source_fingerprint and source_fingerprint == wanted_fingerprint),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "dismiss_reason": fleet._safe_text(item.get("dismiss_reason"), entry.path, entry.repo_id, entry.label)
        if item.get("dismiss_reason")
        else None,
        "superseded_at": item.get("superseded_at"),
        "superseded_by": item.get("superseded_by"),
    }


def _dispatch_report_for_action(target: Path, action: dict[str, Any]) -> dict[str, Any]:
    entry, error = _action_target_entry(target, action)
    dispatch = _dict_or_empty(action.get("dispatch"))
    warnings: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    if error or entry is None:
        warnings.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_dispatch_target_missing",
                "detail": error or "target repo missing",
                "suggested_next_command": f"brigade repos actions show {action.get('fleet_action_id')}",
            }
        )
    else:
        imports = [
            _dispatch_import_summary(entry, action, item) for item in _target_imports_for_action(entry.path, action)
        ]
    target_import_id = dispatch.get("target_import_id")
    if dispatch and target_import_id and not any(item.get("import_id") == target_import_id for item in imports):
        warnings.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_dispatch_target_import_missing",
                "detail": f"{action.get('fleet_action_id')} target import is missing",
                "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}",
            }
        )
    changed = [
        item for item in imports if item.get("source_fingerprint") and not item.get("fingerprint_matches_action")
    ]
    dismissed = [item for item in imports if item.get("status") == "dismissed"]
    superseded = [item for item in imports if item.get("status") == "superseded"]
    if changed:
        warnings.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_dispatch_fingerprint_changed",
                "detail": f"{action.get('fleet_action_id')} has {len(changed)} target import(s) from older fingerprints",
                "suggested_next_command": f"brigade repos actions dispatch plan {action.get('fleet_action_id')}",
            }
        )
    if dismissed:
        warnings.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_dispatch_import_dismissed",
                "detail": f"{action.get('fleet_action_id')} has dismissed target import(s)",
                "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}",
            }
        )
    if superseded:
        warnings.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_dispatch_import_superseded",
                "detail": f"{action.get('fleet_action_id')} has superseded target import(s)",
                "suggested_next_command": f"brigade repos actions dispatch plan {action.get('fleet_action_id')}",
            }
        )
    resolution_status = action.get("resolution_status") or dispatch.get("status")
    if resolution_status in {"broken-reference", "stale", "dismissed", "superseded"}:
        warnings.append(
            {
                "status": constants.WARN,
                "name": f"repo_fleet_action_{resolution_status}",
                "detail": f"{action.get('fleet_action_id')} reconciliation status is {resolution_status}",
                "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}",
            }
        )
    history = [
        {"event": "action-created", "timestamp": action.get("created_at"), "status": action.get("status")},
        {"event": "action-reviewed", "timestamp": action.get("reviewed_at"), "status": action.get("status")}
        if action.get("reviewed_at")
        else None,
        {
            "event": "dispatch-applied",
            "timestamp": dispatch.get("dispatched_at"),
            "status": dispatch.get("status"),
            "target_import_id": dispatch.get("target_import_id"),
        }
        if dispatch
        else None,
        {"event": "reconciled", "timestamp": action.get("reconciled_at"), "status": resolution_status}
        if action.get("reconciled_at")
        else None,
    ]
    checks = warnings or [
        {
            "status": constants.OK,
            "name": "repo_fleet_dispatch_report",
            "detail": f"{action.get('fleet_action_id')} dispatch is traceable",
        }
    ]
    return {
        "fleet_action_id": action.get("fleet_action_id"),
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "source_report_id": action.get("source_report_id"),
        "source_sweep_id": action.get("source_sweep_id"),
        "source_subsystem": action.get("source_subsystem"),
        "source_local_id": action.get("source_local_id"),
        "action_status": action.get("status"),
        "resolution_status": resolution_status,
        "source_fingerprint": action.get("source_fingerprint"),
        "dispatch": {
            "status": dispatch.get("status"),
            "target_import_id": dispatch.get("target_import_id"),
            "target_inbox_label": dispatch.get("target_inbox_label"),
            "dispatched_at": dispatch.get("dispatched_at"),
            "source_fingerprint": dispatch.get("source_fingerprint"),
            "superseded_import_ids": _list_or_empty(dispatch.get("superseded_import_ids")),
        },
        "target_repo": {
            "repo_id": entry.repo_id if entry is not None else action.get("repo_id"),
            "repo_label": entry.label if entry is not None else action.get("repo_label"),
            "exists": entry.path.is_dir() if entry is not None else False,
            "path_label": f"{action.get('repo_id')}:.brigade",
        },
        "imports": imports,
        "import_count": len(imports),
        "dismissed_import_count": len(dismissed),
        "superseded_import_count": len(superseded),
        "changed_fingerprint_import_count": len(changed),
        "history": [item for item in history if item is not None],
        "checks": checks,
        "issue_count": len(warnings),
        "top_issue": warnings[0] if warnings else None,
    }


def _dispatch_report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Repo Fleet Dispatch Report",
        "",
        f"- Report: `{payload.get('report_id', 'planned')}`",
        f"- Generated: {payload.get('generated_at')}",
        f"- Actions: {payload.get('action_count')}",
        f"- Issues: {payload.get('issue_count')}",
        "",
        "## Actions",
        "",
    ]
    for action in _list_or_empty(payload.get("actions")):
        lines.append(
            f"- `{action.get('fleet_action_id')}` repo={action.get('repo_id')} status={action.get('resolution_status') or action.get('action_status')} issues={action.get('issue_count')}"
        )
        for check in _list_or_empty(action.get("checks")):
            if check.get("status") != constants.OK:
                lines.append(f"  - {check.get('name')}: {check.get('detail')}")
    if not payload.get("actions"):
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- local dispatch report only",
            "- no target command execution",
            "- no promotion",
            "- no remote mutation",
        ]
    )
    return "\n".join(lines) + "\n"


def actions_dispatch_report(
    *,
    target: Path,
    action_id: str | None = None,
    all_actions: bool = False,
    record: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    if action_id:
        selected = [action for action in actions if str(action.get("fleet_action_id") or "").startswith(action_id)]
        if not selected:
            print(f"error: fleet action not found: {action_id}", file=sys.stderr)
            return 1
        if len(selected) > 1:
            print(f"error: fleet action id is ambiguous: {action_id}", file=sys.stderr)
            return 2
    elif all_actions:
        selected = actions
    else:
        selected = [
            action
            for action in actions
            if action.get("status") in {"pending", "active", "deferred"}
            or action.get("resolution_status") in {"broken-reference", "stale", "dismissed", "superseded"}
        ]
    generated = _now()
    reports = [_dispatch_report_for_action(target, action) for action in selected]
    payload = {
        "schema_version": 1,
        "target_label": "repo-fleet",
        "report_id": "planned",
        "generated_at": generated.isoformat(),
        "recorded": False,
        "actions": reports,
        "action_count": len(reports),
        "issue_count": sum(int(report.get("issue_count") or 0) for report in reports),
        "top_issue": next((report.get("top_issue") for report in reports if report.get("top_issue")), None),
        "suggested_next_commands": [
            f"brigade repos actions reconcile {report.get('fleet_action_id')}"
            for report in reports
            if report.get("issue_count")
        ],
    }
    if record:
        report_id = f"{generated.strftime('%Y%m%d-%H%M%S')}-dispatch-report-{uuid4().hex[:6]}"
        report_dir = _dispatch_reports_root(target) / report_id
        payload.update(
            {
                "report_id": report_id,
                "recorded": True,
                "path_label": f".brigade/repos/actions/dispatch-reports/{report_id}",
                "bundle_files": ["DISPATCH_REPORT.json", "DISPATCH_REPORT.md"],
            }
        )
        _write_json(report_dir / "DISPATCH_REPORT.json", payload)
        (report_dir / "DISPATCH_REPORT.md").write_text(_dispatch_report_markdown(payload))
    text_lines = [
        "repo fleet dispatch report",
        f"actions: {payload['action_count']}",
        f"issues: {payload['issue_count']}",
    ]
    if record:
        text_lines.append(f"path_label: {payload.get('path_label')}")
    text_lines.extend(f"- {report.get('fleet_action_id')} issues={report.get('issue_count')}" for report in reports)
    return emit(payload, json_output, text_lines, 0)


def _latest_safe_receipts(repo_path: Path, repo_id: str, label: str) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for root, filename, kind in (
        (repo_path / ".brigade" / "center" / "reports", "CENTER_EVIDENCE.json", "operator-report"),
        (repo_path / ".brigade" / "work" / "closeouts", "closeout.json", "work-closeout"),
        (repo_path / ".brigade" / "release" / "runs", "release.json", "release-readiness"),
    ):
        receipt = fleet._safe_receipt(fleet._latest_json(root, filename), repo_id, label)
        if receipt:
            receipt["kind"] = kind
            receipts.append(receipt)
    return receipts


def _action_context_payload(target: Path, action: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    entry, error = _action_target_entry(target, action)
    if error or entry is None:
        return None, error
    guidance = {
        "has_agents": (entry.path / "AGENTS.md").is_file(),
        "has_claude": (entry.path / "CLAUDE.md").is_file() or (entry.path / ".claude" / "CLAUDE.md").is_file(),
        "source_labels": [
            name for name in ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md") if (entry.path / name).is_file()
        ],
    }
    payload = {
        "kind": "repo-fleet-action",
        "fleet_action_id": action.get("fleet_action_id"),
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "safe_summary": fleet._safe_text(action.get("safe_summary"), entry.path, entry.repo_id, entry.label),
        "suggested_command": fleet._safe_text(action.get("suggested_command"), entry.path, entry.repo_id, entry.label),
        "acceptance": [
            fleet._safe_text(item, entry.path, entry.repo_id, entry.label) for item in _action_acceptance(action)
        ],
        "guidance_presence": guidance,
        "latest_receipts": _latest_safe_receipts(entry.path, entry.repo_id, entry.label),
        "dispatch": _dispatch_state(action, entry.path),
        "excluded_private_evidence": [
            "raw guidance file contents",
            "raw scanner output",
            "raw local logs",
            "private absolute paths",
            "exact private repo names",
            "owner names and organization names",
            "hostnames and secrets",
        ],
        "source_references": [
            {"label": f"{entry.repo_id}:AGENTS.md", "exists": guidance["has_agents"]},
            {
                "label": f"{entry.repo_id}:.brigade/work/imports/inbox.jsonl",
                "exists": work_cmd._imports_path(entry.path).is_file(),
            },
        ],
        "checks": [{"status": constants.OK, "name": "repo_fleet_action_context", "detail": "ready"}],
    }
    return payload, None


def actions_context_plan(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    from . import fleet_health

    _, action, error = fleet_health._find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload, context_error = _action_context_payload(target, action)
    if context_error or payload is None:
        print(f"error: {context_error}", file=sys.stderr)
        return 2
    text_lines = [
        f"repo fleet action context plan: {payload.get('fleet_action_id')}",
        f"repo: {payload.get('repo_id')} {payload.get('repo_label')}",
        "writes: 0",
    ]
    return emit(payload, json_output, text_lines, 0)


def actions_context_build(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    from . import fleet_health

    actions, action, error = fleet_health._find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    entry, entry_error = _action_target_entry(target, action)
    payload, context_error = _action_context_payload(target, action)
    if entry_error or context_error or entry is None or payload is None:
        print(f"error: {entry_error or context_error}", file=sys.stderr)
        return 2
    now = _now()
    pack_id = f"{now.strftime('%Y%m%d-%H%M%S')}-fleet-action-context-{uuid4().hex[:6]}"
    payload.update(
        {
            "pack_id": pack_id,
            "status": "built",
            "created_at": now.isoformat(),
            "path_label": f"{entry.repo_id}:.brigade/context/packs/{pack_id}",
        }
    )
    pack_dir = entry.path / ".brigade" / "context" / "packs" / pack_id
    _write_json(pack_dir / "context.json", payload)
    markdown = [
        f"# Fleet Action Context {pack_id}",
        "",
        f"- repo: {payload.get('repo_id')} {payload.get('repo_label')}",
        f"- action: {payload.get('fleet_action_id')}",
        f"- summary: {payload.get('safe_summary')}",
        "",
        "## Acceptance",
        *[f"- {item}" for item in payload["acceptance"]],
        "",
        "## Excluded Private Evidence",
        *[f"- {item}" for item in payload["excluded_private_evidence"]],
        "",
    ]
    (pack_dir / "CONTEXT.md").write_text("\n".join(markdown))
    action["context_pack"] = {
        "pack_id": pack_id,
        "path_label": payload["path_label"],
        "created_at": payload["created_at"],
    }
    action["updated_at"] = payload["created_at"]
    _write_actions(target, actions)
    text_lines = [f"repo fleet action context: {pack_id}", f"path_label: {payload['path_label']}"]
    return emit(payload, json_output, text_lines, 0)


def _task_by_id(repo_path: Path, task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    for task in work_cmd._read_task_ledger(repo_path).get("tasks", []):
        if isinstance(task, dict) and task.get("id") == task_id:
            return task
    return None


def _reconcile_one(target: Path, action: dict[str, Any]) -> dict[str, Any]:
    entry, error = _action_target_entry(target, action)
    now = _now().isoformat()
    if error or entry is None:
        action["resolution_status"] = "broken-reference"
        action["reconciled_at"] = now
        action["updated_at"] = now
        return {
            "fleet_action_id": action.get("fleet_action_id"),
            "status": "broken-reference",
            "detail": error or "target repo missing",
        }
    dispatch = _dict_or_empty(action.get("dispatch"))
    imports = _target_imports_for_action(entry.path, action)
    target_import = None
    target_import_id = dispatch.get("target_import_id")
    if target_import_id:
        target_import = next((item for item in imports if item.get("id") == target_import_id), None)
    if target_import is None and imports:
        target_import = imports[0]
    if target_import is None:
        status = "broken-reference" if dispatch else "stale"
        action["resolution_status"] = status
        action["reconciled_at"] = now
        action["updated_at"] = now
        return {"fleet_action_id": action.get("fleet_action_id"), "status": status, "detail": "target import not found"}
    task = _task_by_id(
        entry.path, target_import.get("task_id") if isinstance(target_import.get("task_id"), str) else None
    )
    if target_import.get("status") == "superseded":
        status = "superseded"
    elif target_import.get("status") == "dismissed":
        status = "dismissed"
    elif task and task.get("status") == "done":
        status = "completed"
    elif target_import.get("status") == "promoted":
        status = "in-progress"
    elif target_import.get("status") == "pending":
        created = fleet._parse_time(str(target_import.get("created_at") or ""))
        if created and (_now() - created).total_seconds() / 3600 > constants.DISPATCH_STALE_HOURS:
            status = "stale"
        else:
            status = "dispatched"
    else:
        status = str(target_import.get("status") or "dispatched")
    latest_closeout = fleet._safe_receipt(
        fleet._latest_json(entry.path / ".brigade" / "work" / "closeouts", "closeout.json"), entry.repo_id, entry.label
    )
    latest_release = fleet._safe_receipt(
        fleet._latest_json(entry.path / ".brigade" / "release" / "runs", "release.json"), entry.repo_id, entry.label
    )
    result = {
        "fleet_action_id": action.get("fleet_action_id"),
        "status": status,
        "target_import_id": target_import.get("id"),
        "target_import_status": target_import.get("status"),
        "target_task_id": target_import.get("task_id"),
        "target_task_status": task.get("status") if isinstance(task, dict) else None,
        "completion": task.get("completion")
        if isinstance(task, dict) and isinstance(task.get("completion"), dict)
        else None,
        "closeout": latest_closeout,
        "release": latest_release,
        "reconciled_at": now,
    }
    action["resolution_status"] = status
    action["reconciled_at"] = now
    action["target_import_id"] = target_import.get("id")
    if target_import.get("task_id"):
        action["target_task_id"] = target_import.get("task_id")
    if latest_closeout:
        action["target_closeout"] = latest_closeout
    if latest_release:
        action["target_release"] = latest_release
    if isinstance(task, dict) and isinstance(task.get("completion"), dict):
        action["completion"] = task.get("completion")
    if status == "completed":
        action["status"] = "done"
        action["completed_at"] = now
    action["updated_at"] = now
    return result


def actions_reconcile(*, target: Path, action_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    selected = actions
    if action_id:
        selected = [action for action in actions if str(action.get("fleet_action_id") or "").startswith(action_id)]
        if not selected:
            print(f"error: fleet action not found: {action_id}", file=sys.stderr)
            return 1
        if len(selected) > 1:
            print(f"error: fleet action id is ambiguous: {action_id}", file=sys.stderr)
            return 2
    results = [_reconcile_one(target, action) for action in selected]
    _write_actions(target, actions)
    payload = {"target": str(target), "results": results, "result_count": len(results)}
    text_lines = [
        "repo fleet actions reconcile",
        *[f"- {result.get('fleet_action_id')} [{result.get('status')}]" for result in results],
    ]
    return emit(payload, json_output, text_lines, 0)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
