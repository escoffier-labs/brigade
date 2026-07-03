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
from . import actions_dispatch, constants, fleet, release_ops, release_train, sweeps

health_commands = sweeps.health_commands


def release_train_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = release_train.latest_release_train(target)
    actions = release_ops.release_train_actions_health(target)
    evidence = release_ops.release_train_evidence_health(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_release_train_missing",
                "detail": "no repo fleet release train has been built",
                "suggested_next_command": "brigade repos release build",
            }
        )
        checks.extend(actions.get("checks") if isinstance(actions.get("checks"), list) else [])
        checks.extend(evidence.get("checks") if isinstance(evidence.get("checks"), list) else [])
        return {
            "latest": None,
            "actions": actions,
            "evidence": evidence,
            "checks": checks,
            "issue_count": len(checks),
            "top_issue": checks[0],
        }
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    if latest.get("status") == "blocked" or int(latest.get("blocker_count") or 0) > 0:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_release_train_blocked",
                "detail": f"{latest.get('train_id')} has blocker(s)",
                "suggested_next_command": f"brigade repos release show {latest.get('train_id')}",
            }
        )
    train_id = str(latest.get("train_id") or "")
    if train_id and not (release_train._release_trains_root(target) / train_id / "RELEASE_TRAIN_MATRIX.json").is_file():
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_release_matrix_missing",
                "detail": f"{train_id} has no release matrix",
                "suggested_next_command": f"brigade repos release matrix {train_id}",
            }
        )
    if not closeout or closeout.get("status") not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_release_train_unclosed",
                "detail": f"{latest.get('train_id')} has not been closed out",
                "suggested_next_command": f"brigade repos release closeout {latest.get('train_id')}",
            }
        )
    created = fleet._parse_time(latest.get("created_at") or latest.get("generated_at"))
    if created and (_now() - created).total_seconds() / 3600 > constants.RELEASE_TRAIN_STALE_HOURS:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_release_train_stale",
                "detail": f"{latest.get('train_id')} is stale",
                "suggested_next_command": "brigade repos release build",
            }
        )
    checks.extend(actions.get("checks") if isinstance(actions.get("checks"), list) else [])
    checks.extend(evidence.get("checks") if isinstance(evidence.get("checks"), list) else [])
    return {
        "latest": latest,
        "actions": actions,
        "evidence": evidence,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def _dispatch_health_checks(target: Path, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for action in actions:
        status = action.get("resolution_status")
        if status in {"broken-reference", "stale", "dismissed", "superseded"}:
            checks.append(
                {
                    "status": constants.WARN,
                    "name": f"repo_fleet_action_{status}",
                    "detail": f"{action.get('fleet_action_id')} is {status}",
                    "suggested_next_command": f"brigade repos actions dispatch report {action.get('fleet_action_id')}",
                }
            )
            continue
        if action.get("dispatch") and status in {None, "dispatched"}:
            dispatch = action.get("dispatch") if isinstance(action.get("dispatch"), dict) else {}
            entry, error = actions_dispatch._action_target_entry(target, action)
            if error or entry is None:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "repo_fleet_action_broken_reference",
                        "detail": f"{action.get('fleet_action_id')} target repo is missing",
                        "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}",
                    }
                )
                continue
            old_evidence = dispatch.get("target_evidence_fingerprint")
            new_evidence = fleet._fingerprint_payload(
                actions_dispatch._latest_safe_receipts(entry.path, entry.repo_id, entry.label)
            )
            if old_evidence and old_evidence != new_evidence:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "repo_fleet_action_evidence_changed",
                        "detail": f"{action.get('fleet_action_id')} target repo evidence changed after dispatch",
                        "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}",
                    }
                )
            imports = actions_dispatch._target_imports_for_action(entry.path, action)
            if not imports:
                checks.append(
                    {
                        "status": constants.WARN,
                        "name": "repo_fleet_action_missing_import",
                        "detail": f"{action.get('fleet_action_id')} target import is missing",
                        "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}",
                    }
                )
    return checks


def _append_action_archive(target: Path, actions: list[dict[str, Any]]) -> None:
    actionqueue.append_archive(actions_dispatch._actions_archive_path(target), actions)


def _report_review_status(report: dict[str, Any]) -> str | None:
    closeout = report.get("closeout") if isinstance(report.get("closeout"), dict) else None
    status = closeout.get("status") if isinstance(closeout, dict) else None
    return status if isinstance(status, str) else None


def _report_reviewed_at(report: dict[str, Any]) -> str | None:
    closeout = report.get("closeout") if isinstance(report.get("closeout"), dict) else None
    reviewed_at = closeout.get("reviewed_at") if isinstance(closeout, dict) else None
    return reviewed_at if isinstance(reviewed_at, str) else None


def _action_rank(action: dict[str, Any]) -> tuple[int, int, str]:
    status_rank = {"active": 0, "pending": 1, "deferred": 2, "done": 3, "archived": 4}.get(
        str(action.get("status") or ""), 5
    )
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(action.get("severity") or ""), 4)
    priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(str(action.get("priority") or ""), 4)
    return (status_rank, min(severity_rank, priority_rank), str(action.get("fleet_action_id") or ""))


def _planned_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    report_id = str(report.get("report_id") or "planned")
    report_fingerprint = str(report.get("report_fingerprint") or fleet._fingerprint_payload(report))
    reviewed_at = _report_reviewed_at(report)
    created = _now().isoformat()
    actions: list[dict[str, Any]] = []
    for repo in report.get("repos") if isinstance(report.get("repos"), list) else []:
        if not isinstance(repo, dict):
            continue
        repo_id = str(repo.get("repo_id") or "unknown")
        repo_label = str(repo.get("repo_label") or repo_id)
        repo_items: list[dict[str, Any]] = []
        for warning in repo.get("warnings") if isinstance(repo.get("warnings"), list) else []:
            if isinstance(warning, dict):
                repo_items.append(
                    {
                        "subsystem": "repo-fleet",
                        "local_id": warning.get("name"),
                        "summary": warning.get("detail"),
                        "severity": "medium",
                        "command": repo.get("suggested_command"),
                    }
                )
        top_action = (repo.get("action_queue") if isinstance(repo.get("action_queue"), dict) else {}).get("top_action")
        if isinstance(top_action, dict):
            repo_items.insert(
                0,
                {
                    "subsystem": "center-action",
                    "local_id": top_action.get("action_id"),
                    "summary": top_action.get("safe_summary"),
                    "priority": "high",
                    "command": top_action.get("suggested_command"),
                    "source_report_id": top_action.get("source_report_id"),
                    "source_fingerprint": top_action.get("source_fingerprint"),
                },
            )
        if int(repo.get("pending_import_count") or 0) > 0:
            repo_items.append(
                {
                    "subsystem": "work-import",
                    "local_id": "pending-imports",
                    "summary": f"{repo_id} has pending imports",
                    "priority": "normal",
                    "command": repo.get("suggested_command"),
                }
            )
        seen: set[str] = set()
        for item in repo_items:
            source_subsystem = str(item.get("subsystem") or "repo-fleet")
            source_local_id = str(item.get("local_id") or source_subsystem)
            source_basis = item.get("source_fingerprint") or fleet._fingerprint_payload(
                {
                    "repo_id": repo_id,
                    "subsystem": source_subsystem,
                    "local_id": source_local_id,
                    "summary": item.get("summary"),
                }
            )
            key = f"{repo_id}:{source_basis}"
            if key in seen:
                continue
            seen.add(key)
            source_fingerprint = fleet._fingerprint_payload(
                {"repo_id": repo_id, "report_fingerprint": report_fingerprint, "source": source_basis}
            )
            actions.append(
                {
                    "fleet_action_id": f"fleet-act-{source_fingerprint[:16]}",
                    "repo_id": repo_id,
                    "repo_label": repo_label,
                    "source_report_id": item.get("source_report_id") or report_id,
                    "source_report_fingerprint": report_fingerprint,
                    "source_subsystem": source_subsystem,
                    "source_local_id": source_local_id,
                    "status": "pending",
                    "priority": item.get("priority") if isinstance(item.get("priority"), str) else None,
                    "severity": item.get("severity") if isinstance(item.get("severity"), str) else None,
                    "safe_summary": str(item.get("summary") or "repo fleet action"),
                    "suggested_command": str(item.get("command") or f"brigade repos show {repo_id}"),
                    "created_at": created,
                    "updated_at": created,
                    "reviewed_at": reviewed_at,
                    "source_fingerprint": source_fingerprint,
                }
            )
    actions.sort(key=_action_rank)
    return actions


def actions_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    actions = actions_dispatch._read_actions(target)
    open_actions = [action for action in actions if action.get("status") in {"pending", "active", "deferred"}]
    open_actions.sort(key=_action_rank)
    checks: list[dict[str, Any]] = []
    checks.extend(_dispatch_health_checks(target, actions))
    if open_actions:
        top = open_actions[0]
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_actions_open",
                "detail": f"{len(open_actions)} open fleet action(s)",
                "suggested_next_command": f"brigade repos actions show {top.get('fleet_action_id')}",
            }
        )
    return {
        "actions_path": str(actions_dispatch._actions_path(target)),
        "action_count": len(actions),
        "open_count": len(open_actions),
        "top_action": open_actions[0] if open_actions else None,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def actions_plan(*, target: Path, report_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = sweeps._resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actions = _planned_actions(report)
    payload = {
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_review_status": _report_review_status(report),
        "actions_root": str(actions_dispatch._actions_root(target)),
        "actions": actions,
        "action_count": len(actions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet actions plan: {report.get('report_id')}")
    print(f"actions: {len(actions)}")
    for action in actions[:20]:
        print(
            f"- {action.get('fleet_action_id')} {action.get('repo_id')} [{action.get('status')}] {action.get('safe_summary')}"
        )
    return 0


def actions_build(
    *, target: Path, report_id: str = "latest", allow_unreviewed: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    report, error = sweeps._resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    review_status = _report_review_status(report)
    if review_status not in {"reviewed", "deferred"} and not allow_unreviewed:
        print(
            "error: source fleet report must be closed out as reviewed or deferred, or pass --allow-unreviewed",
            file=sys.stderr,
        )
        return 2
    existing = actions_dispatch._read_actions(target)
    created, skipped = actionqueue.merge_planned(
        existing, actions_dispatch._read_action_archive(target), _planned_actions(report)
    )
    actions_dispatch._write_actions(target, existing)
    payload = {
        "target": str(target),
        "report_id": report.get("report_id"),
        "actions_path": str(actions_dispatch._actions_path(target)),
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created_actions": created,
        "skipped_actions": skipped,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet actions build: {report.get('report_id')}")
    print(f"created: {len(created)}")
    print(f"skipped: {len(skipped)}")
    return 0


def actions_list(*, target: Path, limit: int = 50, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    actions = actions_dispatch._read_actions(target)
    actions.sort(key=_action_rank)
    payload = {
        "target": str(target),
        "actions_path": str(actions_dispatch._actions_path(target)),
        "actions": actions[:limit],
        "action_count": len(actions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet actions: {target}")
    for action in actions[:limit]:
        print(
            f"- {action.get('fleet_action_id')} {action.get('repo_id')} [{action.get('status')}] {action.get('safe_summary')}"
        )
    return 0


def _find_action(target: Path, action_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    actions = actions_dispatch._read_actions(target)
    action, error = actionqueue.find_action(actions, action_id, id_field="fleet_action_id", label="fleet action")
    return actions, action, error


def actions_show(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target": str(target), "action": action}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet action: {action.get('fleet_action_id')}")
    print(f"status: {action.get('status')}")
    print(f"repo: {action.get('repo_id')} {action.get('repo_label')}")
    print(f"summary: {action.get('safe_summary')}")
    return 0


def _set_action_status(
    *, target: Path, action_id: str, status: str, reason: str | None = None, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    actions, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actionqueue.stamp_status(action, status, now=_now().isoformat(), reason=reason)
    actions_dispatch._write_actions(target, actions)
    if json_output:
        print(json.dumps({"target": str(target), "action": action}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet action {status}: {action.get('fleet_action_id')}")
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
    actions = actions_dispatch._read_actions(target)
    archived, remaining = actionqueue.split_archived_completed(actions, now=_now().isoformat())
    actions_dispatch._write_actions(target, remaining)
    _append_action_archive(target, archived)
    payload = {
        "target": str(target),
        "archived_count": len(archived),
        "archive_path": str(actions_dispatch._actions_archive_path(target)),
        "archived_actions": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet actions archive: completed")
    print(f"archived: {len(archived)}")
    return 0


def health(target: Path) -> dict[str, Any]:
    payload = fleet.scan_payload(target)
    report = report_health(target)
    actions = actions_health(target)
    sweep = sweeps.sweep_health(target)
    health_registry = sweeps._health_command_registry_payload(target)
    release_train_data = release_train_health(target)
    issue_count = (
        payload["issue_count"]
        + int(report.get("issue_count") or 0)
        + int(actions.get("issue_count") or 0)
        + int(sweep.get("issue_count") or 0)
        + int(health_registry.get("issue_count") or 0)
        + int(release_train_data.get("issue_count") or 0)
    )
    top_issue = (
        payload["top_issue"]
        or report.get("top_issue")
        or actions.get("top_issue")
        or sweep.get("top_issue")
        or health_registry.get("top_issue")
        or release_train_data.get("top_issue")
    )
    return {
        "target": payload["target"],
        "config_path": payload["config_path"],
        "repo_count": payload["repo_count"],
        "issue_count": issue_count,
        "top_issue": top_issue,
        "checks": payload["checks"],
        "report": report,
        "actions": actions,
        "sweep": sweep,
        "health_commands": health_registry,
        "release_train": release_train_data,
    }


def daily_use_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    data = health(target)
    checks: list[dict[str, Any]] = []
    phase_by_bucket = {
        "report": 145,
        "actions": 146,
        "sweep": 147,
        "release_train": 148,
    }
    for bucket_name, phase in phase_by_bucket.items():
        bucket = data.get(bucket_name) if isinstance(data.get(bucket_name), dict) else {}
        for check in bucket.get("checks", []) if isinstance(bucket.get("checks"), list) else []:
            if not isinstance(check, dict) or check.get("status") == constants.OK:
                continue
            checks.append({**check, "bucket": bucket_name, "phase": phase})
    action_health = data.get("actions") if isinstance(data.get("actions"), dict) else {}
    if int(action_health.get("open_count") or 0):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_actions_need_review",
                "detail": f"{action_health.get('open_count')} open fleet action(s)",
                "bucket": "actions",
                "phase": 149,
                "suggested_next_command": "brigade repos actions list",
            }
        )
    action_checks = action_health.get("checks") if isinstance(action_health.get("checks"), list) else []
    dispatch_checks = [
        check
        for check in action_checks
        if isinstance(check, dict) and str(check.get("name") or "").startswith("repo_fleet_action_")
    ]
    if dispatch_checks:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_dispatch_manual_review",
                "detail": f"{len(dispatch_checks)} dispatched fleet action(s) need manual reconciliation",
                "bucket": "dispatch",
                "phase": 150,
                "suggested_next_command": "brigade repos actions reconcile",
            }
        )
    release_train_data = data.get("release_train") if isinstance(data.get("release_train"), dict) else {}
    latest_train = release_train_data.get("latest") if isinstance(release_train_data.get("latest"), dict) else None
    if latest_train is not None:
        train_id = str(latest_train.get("train_id") or "")
        if (
            train_id
            and not (release_train._release_trains_root(target) / train_id / "MANUAL_PUBLISH_PLAN.md").is_file()
        ):
            checks.append(
                {
                    "status": constants.WARN,
                    "name": "repo_fleet_release_manual_plan_missing",
                    "detail": f"{train_id} is missing the manual publish plan",
                    "bucket": "release_train",
                    "phase": 151,
                    "suggested_next_command": f"brigade repos release show {train_id}",
                }
            )
    rendered = json.dumps(data, sort_keys=True, default=str)
    privacy_checks: list[dict[str, Any]] = []
    if re.search(r"https?://|[A-Za-z0-9_]*(?:token|secret|password|api[_-]?key)[A-Za-z0-9_]*\s*[:=]", rendered):
        privacy_checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_private_reference",
                "detail": "repo fleet health includes a private-looking reference",
                "bucket": "privacy",
                "phase": 152,
                "suggested_next_command": "brigade repos doctor",
            }
        )
    checks.extend(privacy_checks)
    return {
        "schema_version": 1,
        "schema": {"name": "repo-fleet-daily-use-health", "version": 1},
        "target_label": "repo-fleet",
        "repo_count": data.get("repo_count"),
        "report_issue_count": int((data.get("report") or {}).get("issue_count") or 0)
        if isinstance(data.get("report"), dict)
        else 0,
        "action_issue_count": int((data.get("actions") or {}).get("issue_count") or 0)
        if isinstance(data.get("actions"), dict)
        else 0,
        "sweep_issue_count": int((data.get("sweep") or {}).get("issue_count") or 0)
        if isinstance(data.get("sweep"), dict)
        else 0,
        "release_train_issue_count": int((data.get("release_train") or {}).get("issue_count") or 0)
        if isinstance(data.get("release_train"), dict)
        else 0,
        "health_command_issue_count": int((data.get("health_commands") or {}).get("issue_count") or 0)
        if isinstance(data.get("health_commands"), dict)
        else 0,
        "manual_only": True,
        "privacy": {
            "safe_labels_only": True,
            "stores_raw_repo_paths": False,
            "stores_raw_logs": False,
        },
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
        "suggested_next_commands": [
            "brigade repos report build",
            "brigade repos actions list",
            "brigade repos release build",
        ]
        if checks
        else [],
    }


def first_run_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    data = health(target)
    config_loaded = bool(data.get("repo_count"))
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    sweep = data.get("sweep") if isinstance(data.get("sweep"), dict) else {}
    release_train_data = data.get("release_train") if isinstance(data.get("release_train"), dict) else {}
    latest_report_value = report.get("latest") if isinstance(report.get("latest"), dict) else None
    latest_sweep_value = sweep.get("latest") if isinstance(sweep.get("latest"), dict) else None
    latest_train = release_train_data.get("latest") if isinstance(release_train_data.get("latest"), dict) else None

    def step(step_id: str, label: str, command: str, ready: bool, detail: str) -> dict[str, Any]:
        return {
            "id": step_id,
            "label": label,
            "command": command,
            "ready": ready,
            "status": constants.OK if ready else constants.WARN,
            "detail": detail,
        }

    train_id = str(latest_train.get("train_id") or "") if latest_train else ""
    release_report_ready = bool(
        latest_train
        and train_id
        and (release_train._release_trains_root(target) / train_id / "RELEASE_TRAIN_REPORT.json").is_file()
        and (release_train._release_trains_root(target) / train_id / "RELEASE_TRAIN_MATRIX.json").is_file()
    )
    steps = [
        step(
            "config",
            "Create local repo fleet config",
            "brigade repos init --target .",
            config_loaded,
            "repo fleet config exists" if config_loaded else "missing .brigade/repos.toml",
        ),
        step(
            "scan",
            "Inspect configured repos",
            "brigade repos scan --target .",
            config_loaded and int(data.get("repo_count") or 0) > 0,
            f"{data.get('repo_count') or 0} configured repo(s)",
        ),
        step(
            "sweep",
            "Run a read-only fleet sweep",
            "brigade repos sweep run --target .",
            latest_sweep_value is not None,
            f"latest sweep {latest_sweep_value.get('sweep_id')}" if latest_sweep_value else "no sweep receipt yet",
        ),
        step(
            "sweep-closeout",
            "Review and close out the fleet sweep",
            "brigade repos sweep closeout latest --target .",
            latest_sweep_value is not None and isinstance(latest_sweep_value.get("closeout"), dict),
            "latest sweep has closeout"
            if latest_sweep_value and isinstance(latest_sweep_value.get("closeout"), dict)
            else "sweep still needs review closeout",
        ),
        step(
            "report",
            "Build a fleet report",
            "brigade repos report build --target .",
            latest_report_value is not None,
            f"latest report {latest_report_value.get('report_id')}" if latest_report_value else "no fleet report yet",
        ),
        step(
            "report-closeout",
            "Review and close out the fleet report",
            "brigade repos report closeout latest --target .",
            latest_report_value is not None and isinstance(latest_report_value.get("closeout"), dict),
            "latest report has closeout"
            if latest_report_value and isinstance(latest_report_value.get("closeout"), dict)
            else "report still needs review closeout",
        ),
        step(
            "release-train",
            "Build a release train bundle",
            "brigade repos release build --target .",
            latest_train is not None,
            f"latest train {latest_train.get('train_id')}" if latest_train else "no release train yet",
        ),
        step(
            "release-report",
            "Build release report and matrix",
            "brigade repos release report latest --target . && brigade repos release matrix latest --target .",
            release_report_ready,
            "release report and matrix are present"
            if release_report_ready
            else "release report and matrix are not both present",
        ),
        step(
            "ready",
            "Check the manual release gate",
            "brigade repos release ready latest --target .",
            latest_train is not None and not release_train.get("top_issue"),
            "release train has no fleet health issue"
            if latest_train and not release_train.get("top_issue")
            else "release train still has review work",
        ),
    ]
    next_step = next((item for item in steps if not item["ready"]), None)
    return {
        "schema_version": 1,
        "schema": {"name": "repo-fleet-first-run-plan", "version": 1},
        "target": str(target),
        "manual_only": True,
        "would_write": False,
        "would_run_commands": False,
        "repo_count": data.get("repo_count") or 0,
        "ready": next_step is None,
        "step_count": len(steps),
        "remaining_step_count": len([item for item in steps if not item["ready"]]),
        "next_step": next_step,
        "steps": steps,
        "privacy": {
            "safe_labels_only": True,
            "stores_raw_repo_paths": False,
            "stores_raw_logs": False,
        },
    }


def first_run_plan(*, target: Path, json_output: bool = False) -> int:
    payload = first_run_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet first-run plan: {payload['target']}")
    print(f"ready: {'yes' if payload['ready'] else 'no'}")
    print(f"remaining_steps: {payload['remaining_step_count']}")
    next_step = payload.get("next_step") if isinstance(payload.get("next_step"), dict) else None
    if next_step:
        print(f"next: {next_step.get('command')}")
    for item in payload["steps"]:
        marker = "ok" if item["ready"] else "todo"
        print(f"- [{marker}] {item['id']}: {item['command']}")
    return 0


def report_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = sweeps.latest_report(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_report_missing",
                "detail": "no local repo fleet report has been built",
                "suggested_next_command": "brigade repos report build",
            }
        )
        return {"latest": None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    if not closeout or closeout.get("status") not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_report_unclosed",
                "detail": f"{latest.get('report_id')} has not been closed out",
                "suggested_next_command": f"brigade repos report closeout {latest.get('report_id')}",
            }
        )
    created = fleet._parse_time(latest.get("created_at") or latest.get("generated_at"))
    if created and (_now() - created).total_seconds() / 3600 > constants.REPORT_STALE_HOURS:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_fleet_report_stale",
                "detail": f"{latest.get('report_id')} is stale",
                "suggested_next_command": "brigade repos report build",
            }
        )
    return {"latest": latest, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


__all__ = tuple(name for name in globals() if not name.startswith("__"))
