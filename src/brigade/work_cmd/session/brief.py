"""Session lifecycle, run/status/doctor/brief, and task operations."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import inspect
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from ... import dogfood_cmd, localio
from ...install import apply_gitignore
from .. import constants, helpers, ledger as ledger_mod, config as config_mod, services as services_mod
from .. import scanners as scanners_mod, reviews as reviews_mod

from . import lifecycle as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _security_health_for_brief(security_cmd: Any, target: Path) -> dict[str, Any]:
    signature = inspect.signature(security_cmd.health)
    supports_cache_only = "suppression_cache_only" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )
    if supports_cache_only:
        return security_cmd.health(target, suppression_cache_only=True)
    return security_cmd.health(target)


def _doctor_ignore_level(value: str) -> str:
    if value in {"yes", "outside-target"}:
        return constants.OK
    if value == "no":
        return constants.WARN
    return constants.WARN


def _workflow_rule_health(target: Path) -> dict[str, Any]:
    missing = [rel for rel in constants.WORKFLOW_RULE_TEMPLATES if not (target / rel).is_file()]
    return {
        "status": constants.OK if not missing else constants.WARN,
        "name": "workflow_rules",
        "detail": (
            "repo-shareable workflow rules installed"
            if not missing
            else f"missing {', '.join(missing)}; run `brigade init --target {target} --depth repo --force` to refresh templates"
        ),
        "missing": missing,
    }


def _next_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    active = helpers._active_session_info(target)
    resolved = _resolve_next_task(target)
    dogfood = resolved["dogfood"]
    ledger_task = resolved.get("ledger_task") if isinstance(resolved.get("ledger_task"), dict) else None
    suggested = 'brigade work end --note "..." --handoff' if active is not None else "brigade work run"
    return {
        "target": str(target),
        "active_session": active,
        "dogfood": dogfood,
        "next_source": resolved["source"],
        "task_id": resolved.get("task_id"),
        "next_task": ledger_mod._task_summary(ledger_task) if ledger_task else None,
        "next_issue": ledger_mod._task_issue_metadata(ledger_task) if ledger_task else None,
        "next": str(resolved["task"]),
        "suggested_command": suggested,
    }


def _suggested_command(active: dict[str, Any] | None, next_text: object, source: object) -> str:
    if active is not None:
        return 'brigade work end --note "..." --handoff'
    if source == "task_ledger":
        return "brigade work run"
    if isinstance(next_text, str) and next_text.strip() and source != "default_review":
        return f"brigade work run {shlex.quote(next_text.strip())}"
    return "brigade work run"


def _pick_fields(payload: object, fields: list[str]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    compact: dict[str, Any] = {}
    for field in fields:
        if field not in payload:
            continue
        value = payload.get(field)
        if isinstance(value, str | int | float | bool | type(None)):
            compact[field] = value
    return compact


def _compact_top(payload: object) -> dict[str, Any] | None:
    return _pick_fields(
        payload,
        [
            "status",
            "name",
            "detail",
            "issue_type",
            "id",
            "local_id",
            "priority",
            "severity",
            "safe_summary",
            "suggested_next_command",
            "suggested_command",
        ],
    )


def _compact_operator_report_latest(payload: object) -> dict[str, Any] | None:
    compact = _pick_fields(
        payload,
        [
            "report_id",
            "id",
            "created_at",
            "status",
            "review_status",
            "blocker_count",
            "warning_count",
            "fingerprint",
        ],
    )
    if compact is None:
        return None
    activity = payload.get("activity") if isinstance(payload, dict) else None
    if isinstance(activity, list):
        compact["activity_count"] = len(activity)
    reviews = payload.get("reviews") if isinstance(payload, dict) else None
    if isinstance(reviews, list):
        compact["review_count"] = len(reviews)
    return compact


def _compact_repo_fleet_latest(payload: object) -> dict[str, Any] | None:
    compact = _pick_fields(
        payload,
        [
            "sweep_id",
            "train_id",
            "report_id",
            "path_label",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "repo_count",
            "failed_count",
            "warning_count",
            "blocker_count",
            "open_count",
            "action_count",
            "classification_counts",
            "suggested_next_commands",
        ],
    )
    if compact is None:
        return None
    repos = payload.get("repos") if isinstance(payload, dict) else None
    if isinstance(repos, list):
        compact["repo_count"] = compact.get("repo_count", len(repos))
    commands = payload.get("commands") if isinstance(payload, dict) else None
    if isinstance(commands, list):
        compact["command_count"] = len(commands)
    closeout = payload.get("closeout") if isinstance(payload, dict) else None
    if isinstance(closeout, dict):
        compact["closeout"] = _pick_fields(closeout, ["status", "reviewed_at", "blocker_count", "warning_count"])
    return compact


def _compact_health_section(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    compact: dict[str, Any] = {}
    for key in (
        "config_path",
        "repo_count",
        "report_count",
        "action_count",
        "open_count",
        "issue_count",
        "due_count",
        "suggested_command",
        "suggested_next_command",
    ):
        if key in payload:
            compact[key] = payload[key]
    if "top_issue" in payload:
        compact["top_issue"] = _compact_top(payload.get("top_issue"))
    if "top_action" in payload:
        compact["top_action"] = _compact_top(payload.get("top_action"))
    if "counts" in payload:
        compact["counts"] = payload.get("counts")
    if "checks" in payload and isinstance(payload.get("checks"), list):
        compact["checks"] = payload["checks"][:5]
        compact["check_count"] = len(payload["checks"])
    if "latest" in payload:
        compact["latest"] = _compact_repo_fleet_latest(payload.get("latest"))
    if "review" in payload and isinstance(payload.get("review"), dict):
        review = payload["review"]
        compact["review"] = {
            "issue_count": review.get("issue_count", 0),
            "top_issue": _compact_top(review.get("top_issue")),
            "top_pending_import": ledger_mod._import_summary(review.get("top_pending_import"))
            if review.get("top_pending_import")
            else None,
        }
    return compact


def _compact_repo_fleet_health(payload: dict[str, Any]) -> dict[str, Any]:
    release_train = payload.get("release_train") if isinstance(payload.get("release_train"), dict) else {}
    compact_release = _compact_health_section(release_train) or {}
    if isinstance(release_train.get("actions"), dict):
        compact_release["actions"] = _compact_health_section(release_train.get("actions"))
    if isinstance(release_train.get("evidence"), dict):
        compact_release["evidence"] = _compact_health_section(release_train.get("evidence"))
    return {
        "config_path": payload["config_path"],
        "repo_count": payload["repo_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "report": _compact_health_section(payload.get("report")),
        "actions": _compact_health_section(payload.get("actions")),
        "sweep": _compact_health_section(payload.get("sweep")),
        "release_train": compact_release,
    }


def _brief_payload(target: Path, *, limit: int = 3, include_code_graph: bool = False) -> dict[str, Any]:
    from .. import (
        aboyeur,
        center_cmd,
        chat_cmd,
        context_cmd,
        daily_cmd,
        handoff_cmd,
        learn_cmd,
        memory_cmd,
        notifications_cmd,
        outcome_cmd,
        pantry_cmd,
        phases_cmd,
        projects_cmd,
        repos_cmd,
        research_cmd,
        roadmap_cmd,
        security_cmd,
        tools_cmd,
    )

    target = target.expanduser().resolve()
    active = helpers._active_session_info(target)
    sessions, skipped = helpers._collect_sessions(helpers._work_root(target))
    latest_session = helpers._session_info(sessions[0][0], sessions[0][1]) if sessions else None
    recent_sessions = [helpers._session_info(path, payload) for path, payload in sessions[:limit]]
    resolved = _resolve_next_task(target)
    task_text = str(resolved.get("task") or "").strip()
    code_graph = (
        aboyeur.code_graph_brief(target, task_text)
        if include_code_graph and task_text and resolved.get("source") != "default_review"
        else None
    )
    ledger_task = resolved.get("ledger_task") if isinstance(resolved.get("ledger_task"), dict) else None
    git = helpers._git_snapshot(target)
    suggested = _suggested_command(active, resolved["task"], resolved["source"])
    pending = ledger_mod._pending_tasks(target)
    pending_imports = ledger_mod._pending_imports(target)
    pending_import_counts = ledger_mod._import_counts(pending_imports)
    scanner_candidate = ledger_mod._scanner_candidate(pending_imports)
    handoff_candidate = ledger_mod._handoff_candidate(pending_imports)
    inbox_hygiene = services_mod._inbox_hygiene_payload(target)
    scanner_health = scanners_mod._scanner_health(target)
    sweep_health = scanners_mod._scanner_sweep_health(target)
    review_health = reviews_mod._review_health(target)
    chat_health = chat_cmd.health(target)
    memory_health = memory_cmd.health(target)
    security_health = _security_health_for_brief(security_cmd, target)
    backup_health = config_mod._backup_health(target)
    tool_health = tools_cmd.health(target)
    roadmap_health = roadmap_cmd.health(target)
    repo_health = repos_cmd.health(target)
    pantry_health = pantry_cmd.status_payload(target)
    notification_health = notifications_cmd.health(target)
    context_health = context_cmd.health(target)
    projects_health = projects_cmd.health(target)
    learning_health = learn_cmd.health(target)
    research_health = research_cmd.health(target)
    center_report_health = center_cmd.report_health(target)
    center_actions_health = center_cmd.actions_health(target)
    daily_health = daily_cmd.health(target)
    phase_health = phases_cmd.health(target)
    outcome_health = outcome_cmd.health(target)
    handoff_issues = handoff_cmd.collect_issues(target)
    known_handoff_issue_ids = handoff_cmd._known_local_issue_ids(target)
    new_handoff_issues = [issue for issue in handoff_issues if issue.id not in known_handoff_issue_ids]
    handoff_drafts = handoff_cmd.draft_queue_payload(target)
    return {
        "target": str(target),
        "git": git,
        "active_session": active,
        "latest_session": latest_session,
        "recent_sessions": recent_sessions,
        "skipped_sessions": skipped,
        "tasks_path": str(helpers._tasks_path(target)),
        "pending_tasks": pending,
        "plan_coverage": ledger_mod._plan_coverage_payload(target),
        "imports_path": str(helpers._imports_path(target)),
        "pending_imports": pending_imports,
        "pending_import_counts": pending_import_counts,
        "scanner_candidate": ledger_mod._import_summary(scanner_candidate) if scanner_candidate else None,
        "handoff_candidate": ledger_mod._import_summary(handoff_candidate) if handoff_candidate else None,
        "inbox_hygiene": {
            "issue_count": inbox_hygiene["issue_count"],
            "top_issue": inbox_hygiene["top_issue"],
        },
        "scanner_health": {
            "config_path": scanner_health["config_path"],
            "checks": scanner_health["checks"],
            "next_run": scanner_health["next_run"],
            "latest_run": scanner_health.get("latest_run"),
            "due": scanner_health.get("due"),
        },
        "scanner_sweeps": {
            "sweeps_root": sweep_health["sweeps_root"],
            "latest": sweep_health["latest"],
            "checks": sweep_health["checks"],
            "due_count": sweep_health["due_count"],
            "suggested_command": sweep_health["suggested_command"],
            "review": sweep_health["review"],
        },
        "code_review": {
            "config_path": review_health["config_path"],
            "checks": review_health["checks"],
            "latest_run": review_health["latest_run"],
            "latest_success": review_health["latest_success"],
            "latest_unclosed_run": review_health["latest_unclosed_run"],
            "pending_finding_count": review_health["pending_finding_count"],
            "unresolved_finding_count": review_health["unresolved_finding_count"],
            "top_pending_finding": review_health["top_pending_finding"],
            "top_unresolved_finding": review_health["top_unresolved_finding"],
        },
        "chat_surfaces": {
            "config_path": chat_health["config_path"],
            "checks": chat_health["checks"],
            "issue_count": chat_health["issue_count"],
            "top_issue": chat_health["top_issue"],
        },
        "memory_care": {
            "config_path": memory_health["config_path"],
            "scan_path": memory_health["scan_path"],
            "queue_path": memory_health["queue_path"],
            "valid": memory_health["valid"],
            "issue_count": memory_health["issue_count"],
            "top_issue": memory_health["top_issue"],
            "autofix_plan": memory_health.get("autofix_plan"),
        },
        "security_health": {
            "config_path": security_health["config_path"],
            "valid": security_health["valid"],
            "issue_count": security_health["issue_count"],
            "top_issue": security_health["top_issue"],
            "top_finding": security_health["top_finding"],
            "suppression_cache": security_health.get("suppression_cache"),
        },
        "backup_health": {
            "config_path": backup_health["config_path"],
            "issue_count": backup_health["issue_count"],
            "raw_issue_count": backup_health.get("raw_issue_count"),
            "quieted_issue_count": backup_health.get("quieted_issue_count"),
            "restore_rehearsal_issue_count": backup_health.get("restore_rehearsal_issue_count"),
            "changed_fingerprint_count": backup_health.get("changed_fingerprint_count"),
            "operator_summary": backup_health.get("operator_summary"),
            "top_issue": backup_health["top_issue"],
            "valid": backup_health["valid"],
        },
        "tool_catalog": {
            "config_path": tool_health["config_path"],
            "valid": tool_health["valid"],
            "tool_count": tool_health["tool_count"],
            "issue_count": tool_health["issue_count"],
            "top_issue": tool_health["top_issue"],
            "call_queue": tool_health.get("call_queue"),
            "run_history": tool_health.get("run_history"),
            "checkpoints": tool_health.get("checkpoints"),
        },
        "roadmap_completion": {
            "issue_count": roadmap_health["issue_count"],
            "top_issue": roadmap_health["top_issue"],
            "audit": roadmap_health["audit"],
            "patterns": roadmap_health["patterns"],
        },
        "repo_fleet": {
            **_compact_repo_fleet_health(repo_health),
        },
        "pantry": pantry_health,
        "notifications": notification_health,
        "code_graph_context": code_graph.text if code_graph is not None and code_graph.attached else None,
        "code_graph_brief": {
            "attached": bool(code_graph.attached) if code_graph is not None else False,
            "bytes": code_graph.bytes if code_graph is not None else 0,
        },
        "context_packs": {
            "pack_count": context_health["pack_count"],
            "issue_count": context_health["issue_count"],
            "top_issue": context_health["top_issue"],
            "latest": context_health["latest"],
        },
        "project_consolidation": {
            "project_count": projects_health["project_count"],
            "issue_count": projects_health["issue_count"],
            "top_issue": projects_health["top_issue"],
        },
        "learning": {
            "candidate_count": learning_health["candidate_count"],
            "issue_count": learning_health["issue_count"],
            "top_issue": learning_health["top_issue"],
        },
        "research_handoffs": {
            "run_count": research_health["run_count"],
            "issue_count": research_health["issue_count"],
            "top_issue": research_health["top_issue"],
        },
        "operator_report": {
            "issue_count": center_report_health["issue_count"],
            "top_issue": center_report_health["top_issue"],
            "latest": _compact_operator_report_latest(center_report_health["latest"]),
            "latest_diff": _compact_repo_fleet_latest(center_report_health.get("latest_diff")),
        },
        "operator_actions": {
            "actions_path": center_actions_health["actions_path"],
            "action_count": center_actions_health["action_count"],
            "open_count": center_actions_health["open_count"],
            "counts": center_actions_health["counts"],
            "top_action": center_actions_health["top_action"],
            "issue_count": center_actions_health["issue_count"],
            "top_issue": center_actions_health["top_issue"],
        },
        "daily_driver": {
            "config_path": daily_health["config_path"],
            "run_count": daily_health["run_count"],
            "plan_count": daily_health["plan_count"],
            "issue_count": daily_health["issue_count"],
            "top_issue": daily_health["top_issue"],
            "latest_run": daily_health["latest_run"],
            "latest_plan": daily_health["latest_plan"],
            "approvals": daily_health.get("approvals"),
            "telemetry": daily_health.get("telemetry"),
        },
        "phase_ledger": {
            "records_path": phase_health["records_path"],
            "record_count": phase_health["record_count"],
            "open_count": phase_health["open_count"],
            "issue_count": phase_health["issue_count"],
            "top_issue": phase_health["top_issue"],
            "latest": phase_health["latest"],
            "latest_session": phase_health.get("latest_session"),
            "latest_session_checkpoint": phase_health.get("latest_session_checkpoint"),
            "latest_session_checkpoint_compare": phase_health.get("latest_session_checkpoint_compare"),
            "latest_session_report": phase_health.get("latest_session_report"),
            "open_action_count": phase_health.get("open_action_count", 0),
            "top_action": phase_health.get("top_action"),
            "action_counts": phase_health.get("action_counts", {}),
        },
        "handoff_issues": {
            "count": len(new_handoff_issues),
            "known_count": len(handoff_issues) - len(new_handoff_issues),
            "total_count": len(handoff_issues),
            "by_category": handoff_cmd._issue_counts(new_handoff_issues),
            "known_by_category": handoff_cmd._issue_counts(
                [issue for issue in handoff_issues if issue.id in known_handoff_issue_ids]
            ),
        },
        "handoff_drafts": {
            "counts": handoff_drafts["counts"],
            "issue_count": handoff_drafts["issue_count"],
            "top_issue": handoff_drafts["top_issue"],
            "latest_ingest_run": handoff_drafts.get("latest_ingest_run"),
            "drafts": handoff_drafts["drafts"][:limit],
        },
        "outcome_loop": {
            "records_path": outcome_health["records_path"],
            "verify_run_count": outcome_health["verify_run_count"],
            "record_count": outcome_health["record_count"],
            "scored_artifact_count": outcome_health["scored_artifact_count"],
            "promoted_count": outcome_health["promoted_count"],
            "issue_count": outcome_health["issue_count"],
            "top_issue": outcome_health["top_issue"],
        },
        "dogfood": resolved["dogfood"],
        "next_source": resolved["source"],
        "task_id": resolved.get("task_id"),
        "next_task": ledger_mod._task_summary(ledger_task) if ledger_task else None,
        "next_issue": ledger_mod._task_issue_metadata(ledger_task) if ledger_task else None,
        "next": str(resolved["task"]),
        "suggested_command": suggested,
    }


def _print_bootstrap_line(level: str, name: str, detail: object) -> None:
    print(f"[{level}] {name}: {detail}")


def brief(*, target: Path, limit: int = 3, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    payload = _brief_payload(target, limit=limit, include_code_graph=json_output)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"work brief: {target}")
    git = payload["git"]
    if isinstance(git, dict) and git.get("available"):
        print(f"branch: {git.get('branch')}")
        dirty = git.get("dirty_files") if isinstance(git.get("dirty_files"), list) else []
        print(f"dirty_files: {len(dirty)}")
        for item in dirty[:8]:
            print(f"  {item}")
        if len(dirty) > 8:
            print(f"  ... {len(dirty) - 8} more")
    else:
        print("git: unavailable")

    active = payload["active_session"]
    if isinstance(active, dict):
        if active.get("valid"):
            print(f"active_session: {active.get('path')}")
            if active.get("title"):
                print(f"active_session_title: {helpers._short(str(active['title']))}")
        else:
            print(f"active_session: invalid ({active.get('path')})")
    else:
        print("active_session: none")

    latest_session = payload["latest_session"]
    if isinstance(latest_session, dict):
        print(f"latest_session: {latest_session.get('path')}")
        if latest_session.get("title"):
            print(f"latest_session_title: {helpers._short(str(latest_session['title']))}")
        if latest_session.get("note"):
            print(f"latest_session_note: {helpers._short(str(latest_session['note']))}")
        if latest_session.get("handoff"):
            print(f"latest_session_handoff: {latest_session['handoff']}")
    else:
        print(f"latest_session: none ({helpers._work_root(target)})")

    dogfood = payload["dogfood"]
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {dogfood['error']}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{latest_run.get('started_at', '')} "
            f"[{latest_run.get('status', 'unknown')}] {latest_run.get('path')}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {helpers._short(str(latest_run['task']))}")
    else:
        print("latest_run: none")

    outcome_loop = payload.get("outcome_loop") if isinstance(payload.get("outcome_loop"), dict) else {}
    if outcome_loop:
        print(
            "outcome_loop: "
            f"verify_runs={outcome_loop.get('verify_run_count')} "
            f"records={outcome_loop.get('record_count')} "
            f"scored={outcome_loop.get('scored_artifact_count')} "
            f"promoted={outcome_loop.get('promoted_count')}"
        )
        top_outcome = outcome_loop.get("top_issue") if isinstance(outcome_loop.get("top_issue"), dict) else None
        if top_outcome:
            print(f"outcome_loop_issue: {top_outcome.get('name')} {helpers._short(str(top_outcome.get('detail', '')))}")

    pantry = payload.get("pantry") if isinstance(payload.get("pantry"), dict) else {}
    if pantry:
        print(f"pantry: {pantry.get('summary')}")
    notifications = payload.get("notifications") if isinstance(payload.get("notifications"), dict) else {}
    if notifications:
        print(f"notifications: {notifications.get('status')} configured={notifications.get('configured')}")
        top_notification = notifications.get("top_issue") if isinstance(notifications.get("top_issue"), dict) else None
        if top_notification:
            print(
                f"notifications_top_issue: {top_notification.get('name')} {helpers._short(str(top_notification.get('detail', '')))}"
            )

    print(f"next_source: {payload['next_source']}")
    if payload.get("task_id"):
        print(f"task_id: {payload['task_id']}")
    next_task = payload.get("next_task") if isinstance(payload.get("next_task"), dict) else None
    if next_task:
        print(f"next_type: {next_task.get('type')}")
        print(f"next_priority: {next_task.get('priority')}")
        if next_task.get("template"):
            print(f"next_template: {next_task.get('template')}")
        if next_task.get("acceptance_missing"):
            print("next_acceptance: missing")
        else:
            print(f"next_acceptance: {next_task.get('acceptance_count')}")
    next_issue = payload.get("next_issue") if isinstance(payload.get("next_issue"), dict) else None
    if next_issue:
        print(f"issue: {next_issue.get('url') or next_issue.get('number')}")
        if next_issue.get("state"):
            print(f"issue_state: {next_issue['state']}")
        labels = next_issue.get("labels")
        if isinstance(labels, list) and labels:
            print(f"issue_labels: {', '.join(str(label) for label in labels)}")
    print(f"next: {helpers._short(str(payload['next']))}")
    print(f"suggested_command: {payload['suggested_command']}")

    pending = payload["pending_tasks"]
    if isinstance(pending, list) and pending:
        print("pending_tasks:")
        for task in pending[:5]:
            if not isinstance(task, dict):
                continue
            summary = ledger_mod._task_summary(task)
            print(
                "  - "
                f"{task.get('id')} "
                f"[{summary['type']} {summary['priority']} acceptance={summary['acceptance_count']}] "
                f"{helpers._short(str(task.get('text', '')))}"
            )
        if len(pending) > 5:
            print(f"  ... {len(pending) - 5} more")

    plan_coverage = payload.get("plan_coverage")
    if isinstance(plan_coverage, dict):
        if plan_coverage.get("significant_without_plan"):
            ids = ", ".join(plan_coverage.get("task_ids", [])[:5])
            print(f"plans: {plan_coverage['significant_without_plan']} pending task(s) without a plan artifact ({ids})")
        elif not plan_coverage.get("pending_total"):
            print("plans: no pending tasks")
        else:
            print("plans: significant pending tasks have plan artifacts")

    pending_imports = payload["pending_imports"]
    if isinstance(pending_imports, list) and pending_imports:
        counts = payload.get("pending_import_counts")
        if isinstance(counts, dict):
            print(f"pending_import_count: {counts.get('total', len(pending_imports))}")
            by_source = counts.get("by_source") if isinstance(counts.get("by_source"), dict) else {}
            if by_source:
                print("pending_imports_by_source:")
                for source, count in by_source.items():
                    print(f"  {source}: {count}")
            by_kind = counts.get("by_kind") if isinstance(counts.get("by_kind"), dict) else {}
            if by_kind:
                print("pending_imports_by_kind:")
                for kind, count in by_kind.items():
                    print(f"  {kind}: {count}")
        print("pending_imports:")
        for item in pending_imports[:5]:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or "unknown"
            kind = item.get("kind") or "task"
            print(f"  - {item.get('id')} [{kind}] {source}: {helpers._short(str(item.get('text', '')))}")
        if len(pending_imports) > 5:
            print(f"  ... {len(pending_imports) - 5} more")
    scanner_candidate = payload.get("scanner_candidate")
    if isinstance(scanner_candidate, dict):
        print(f"scanner_next_import: {scanner_candidate.get('id')}")
        print(f"scanner_next_source: {scanner_candidate.get('source')}")
        print(f"scanner_next_kind: {scanner_candidate.get('kind')}")
        if scanner_candidate.get("kind") == "task":
            print(
                "scanner_next_task: "
                f"[{scanner_candidate.get('type')} {scanner_candidate.get('priority')} "
                f"acceptance={scanner_candidate.get('acceptance_count')}] "
                f"{helpers._short(str(scanner_candidate.get('text', '')))}"
            )
            print(f"scanner_next_command: brigade work import plan {scanner_candidate.get('id')}")
    handoff_candidate = payload.get("handoff_candidate")
    pending_tasks = payload.get("pending_tasks") if isinstance(payload.get("pending_tasks"), list) else []
    if isinstance(handoff_candidate, dict) and not pending_tasks:
        print(f"handoff_next_import: {handoff_candidate.get('id')}")
        print(f"handoff_next_source: {handoff_candidate.get('source')}")
        print(f"handoff_next_kind: {handoff_candidate.get('kind')}")
        print(f"handoff_next_target: {handoff_candidate.get('target_document')}")
        print(f"handoff_next_command: brigade work import plan-handoff {handoff_candidate.get('id')}")

    inbox_hygiene = payload.get("inbox_hygiene") if isinstance(payload.get("inbox_hygiene"), dict) else {}
    if inbox_hygiene:
        issue_count = inbox_hygiene.get("issue_count")
        print(f"inbox_hygiene: {helpers._count_status(issue_count)}")
        top_inbox = inbox_hygiene.get("top_issue") if isinstance(inbox_hygiene.get("top_issue"), dict) else None
        if top_inbox:
            print(f"inbox_top_issue: {top_inbox.get('name')} {helpers._short(str(top_inbox.get('detail', '')))}")

    scanner_health = payload.get("scanner_health") if isinstance(payload.get("scanner_health"), dict) else {}
    scanner_checks = scanner_health.get("checks") if isinstance(scanner_health.get("checks"), list) else []
    if scanner_checks:
        warnings = [
            check for check in scanner_checks if isinstance(check, dict) and check.get("status") != constants.OK
        ]
        print(f"scanner_config: {scanner_health.get('config_path')}")
        print(f"scanner_health: {helpers._count_status(len(warnings), 'warning')}")
        next_scanner = scanner_health.get("next_run") if isinstance(scanner_health.get("next_run"), dict) else None
        if next_scanner:
            print(
                f"scanner_next_run: {next_scanner.get('id')} {next_scanner.get('start')} {next_scanner.get('cadence')}"
            )
        latest_scanner_run = (
            scanner_health.get("latest_run") if isinstance(scanner_health.get("latest_run"), dict) else None
        )
        if latest_scanner_run:
            print(
                "scanner_latest_run: "
                f"{latest_scanner_run.get('scanner_id')} "
                f"[{latest_scanner_run.get('status')}] {latest_scanner_run.get('run_id')}"
            )
        due_scanners = scanner_health.get("due") if isinstance(scanner_health.get("due"), list) else []
        if due_scanners:
            print(
                f"scanner_due: {', '.join(str(item.get('id')) for item in due_scanners[:5] if isinstance(item, dict))}"
            )

    scanner_sweeps = payload.get("scanner_sweeps") if isinstance(payload.get("scanner_sweeps"), dict) else {}
    if scanner_sweeps:
        latest_sweep = scanner_sweeps.get("latest") if isinstance(scanner_sweeps.get("latest"), dict) else None
        if latest_sweep:
            print(f"scanner_latest_sweep: {latest_sweep.get('sweep_id')} [{latest_sweep.get('status')}]")
        if scanner_sweeps.get("suggested_command"):
            print(f"scanner_sweep_command: {scanner_sweeps.get('suggested_command')}")
        review = scanner_sweeps.get("review") if isinstance(scanner_sweeps.get("review"), dict) else {}
        top_pending = review.get("top_pending_import") if isinstance(review.get("top_pending_import"), dict) else None
        if top_pending and latest_sweep:
            print(f"scanner_unreviewed_sweep: {latest_sweep.get('sweep_id')}")
            print(
                f"scanner_sweep_import: {top_pending.get('id')} {top_pending.get('source')} {helpers._short(str(top_pending.get('text', '')))}"
            )
            print(f"scanner_sweep_review: brigade work sweep-review {latest_sweep.get('sweep_id')}")

    chat_surfaces = payload.get("chat_surfaces") if isinstance(payload.get("chat_surfaces"), dict) else {}
    if chat_surfaces:
        print(f"chat_surfaces_config: {chat_surfaces.get('config_path')}")
        chat_issue_count = int(chat_surfaces.get("issue_count", 0) or 0)
        print(f"chat_surfaces_health: {helpers._count_status(chat_issue_count)}")
        top_chat = chat_surfaces.get("top_issue") if isinstance(chat_surfaces.get("top_issue"), dict) else None
        if top_chat:
            print(f"chat_surfaces_top_issue: {top_chat.get('name')} {helpers._short(str(top_chat.get('detail', '')))}")

    memory_care = payload.get("memory_care") if isinstance(payload.get("memory_care"), dict) else {}
    if memory_care:
        print(f"memory_care_config: {memory_care.get('config_path')}")
        issue_count = memory_care.get("issue_count")
        print(f"memory_care_health: {helpers._count_status(issue_count)}")
        top_memory = memory_care.get("top_issue") if isinstance(memory_care.get("top_issue"), dict) else None
        if top_memory:
            print(
                "memory_care_top_issue: "
                f"{top_memory.get('issue_type') or top_memory.get('name')} "
                f"{top_memory.get('file') or helpers._short(str(top_memory.get('detail', '')))}"
            )
        autofix_plan = memory_care.get("autofix_plan") if isinstance(memory_care.get("autofix_plan"), dict) else {}
        if autofix_plan.get("plan_count"):
            print(
                "memory_care_fix_plan: "
                f"planned={autofix_plan.get('plan_count')} "
                f"blocked={autofix_plan.get('blocked_count')} "
                f"command={autofix_plan.get('suggested_next_command')}"
            )

    security_health = payload.get("security_health") if isinstance(payload.get("security_health"), dict) else {}
    if security_health:
        print(f"security_config: {security_health.get('config_path')}")
        issue_count = security_health.get("issue_count")
        print(f"security_health: {helpers._count_status(issue_count)}")
        suppression_cache = (
            security_health.get("suppression_cache")
            if isinstance(security_health.get("suppression_cache"), dict)
            else None
        )
        if suppression_cache and suppression_cache.get("status") != "ok":
            print(
                "security_suppressions_cache: "
                f"{suppression_cache.get('status')} {helpers._short(str(suppression_cache.get('detail', '')))}"
            )
            if suppression_cache.get("next_command"):
                print(f"security_suppressions_next_command: {suppression_cache.get('next_command')}")
        top_security = (
            security_health.get("top_finding") if isinstance(security_health.get("top_finding"), dict) else None
        )
        if top_security:
            print(
                "security_top_finding: "
                f"{top_security.get('id')} [{top_security.get('severity')}] "
                f"{top_security.get('path')}:{top_security.get('line')} "
                f"{helpers._short(str(top_security.get('title', '')))}"
            )

    backup_health = payload.get("backup_health") if isinstance(payload.get("backup_health"), dict) else {}
    if backup_health:
        print(f"backup_config: {backup_health.get('config_path')}")
        issue_count = backup_health.get("issue_count")
        print(f"backup_health: {helpers._count_status(issue_count)}")
        if backup_health.get("operator_summary"):
            print(f"backup_summary: {backup_health.get('operator_summary')}")
        top_backup = backup_health.get("top_issue") if isinstance(backup_health.get("top_issue"), dict) else None
        if top_backup:
            print(
                "backup_top_issue: "
                f"{top_backup.get('destination')}/{top_backup.get('issue_type')} "
                f"{helpers._short(str(top_backup.get('detail', '')))}"
            )

    daily_driver = payload.get("daily_driver") if isinstance(payload.get("daily_driver"), dict) else {}
    if daily_driver:
        print(f"daily_config: {daily_driver.get('config_path')}")
        print(f"daily_driver: {helpers._count_status(daily_driver.get('issue_count'))}")
        latest_daily = daily_driver.get("latest_run") if isinstance(daily_driver.get("latest_run"), dict) else None
        if latest_daily:
            print(f"daily_latest_run: {latest_daily.get('run_id')} [{latest_daily.get('status')}]")
        top_daily = daily_driver.get("top_issue") if isinstance(daily_driver.get("top_issue"), dict) else None
        if top_daily:
            print(f"daily_top_issue: {top_daily.get('name')} {helpers._short(str(top_daily.get('detail', '')))}")
        approvals = daily_driver.get("approvals") if isinstance(daily_driver.get("approvals"), dict) else {}
        if approvals.get("pending_count"):
            top_approval = approvals.get("top_pending") if isinstance(approvals.get("top_pending"), dict) else {}
            print(
                f"daily_pending_approval: {top_approval.get('approval_id')} {helpers._short(str(top_approval.get('safe_summary', '')))}"
            )

    phase_ledger = payload.get("phase_ledger") if isinstance(payload.get("phase_ledger"), dict) else {}
    if phase_ledger:
        print(f"phase_ledger: {helpers._count_status(phase_ledger.get('issue_count'))}")
        print(f"phase_records: {phase_ledger.get('record_count', 0)}")
        print(f"phase_actions: {phase_ledger.get('open_action_count', 0)}")
        latest_phase_session = (
            phase_ledger.get("latest_session") if isinstance(phase_ledger.get("latest_session"), dict) else None
        )
        if latest_phase_session:
            print(f"phase_session: {latest_phase_session.get('session_id')} [{latest_phase_session.get('status')}]")
        latest_checkpoint = (
            phase_ledger.get("latest_session_checkpoint")
            if isinstance(phase_ledger.get("latest_session_checkpoint"), dict)
            else None
        )
        latest_checkpoint_compare = (
            phase_ledger.get("latest_session_checkpoint_compare")
            if isinstance(phase_ledger.get("latest_session_checkpoint_compare"), dict)
            else None
        )
        if latest_checkpoint:
            compare_count = (
                latest_checkpoint_compare.get("issue_count") if isinstance(latest_checkpoint_compare, dict) else 0
            )
            print(
                f"phase_checkpoint: {latest_checkpoint.get('checkpoint_id')} [{latest_checkpoint.get('status')}] issues={compare_count}"
            )
        top_phase = phase_ledger.get("top_issue") if isinstance(phase_ledger.get("top_issue"), dict) else None
        if top_phase:
            print(f"phase_top_issue: {top_phase.get('name')} {helpers._short(str(top_phase.get('detail', '')))}")
        top_phase_action = phase_ledger.get("top_action") if isinstance(phase_ledger.get("top_action"), dict) else None
        if top_phase_action:
            print(
                f"phase_top_action: {top_phase_action.get('action_id')} {helpers._short(str(top_phase_action.get('safe_summary', '')))}"
            )

    tool_catalog = payload.get("tool_catalog") if isinstance(payload.get("tool_catalog"), dict) else {}
    if tool_catalog:
        print(f"tool_config: {tool_catalog.get('config_path')}")
        issue_count = tool_catalog.get("issue_count")
        print(f"tool_catalog: {helpers._count_status(issue_count)}")
        top_tool = tool_catalog.get("top_issue") if isinstance(tool_catalog.get("top_issue"), dict) else None
        if top_tool:
            print(
                "tool_top_issue: "
                f"{top_tool.get('tool_id') or 'catalog'}/{top_tool.get('issue_type')} "
                f"{helpers._short(str(top_tool.get('detail', '')))}"
            )
        call_queue = tool_catalog.get("call_queue") if isinstance(tool_catalog.get("call_queue"), dict) else {}
        if call_queue:
            print(f"tool_call_pending: {call_queue.get('pending_count', 0)}")
            call_top = call_queue.get("top_issue") if isinstance(call_queue.get("top_issue"), dict) else None
            if call_top:
                print(
                    "tool_call_top_issue: "
                    f"{call_top.get('call_id')} {call_top.get('issue_type')} "
                    f"{helpers._short(str(call_top.get('detail', '')))}"
                )
        run_history = tool_catalog.get("run_history") if isinstance(tool_catalog.get("run_history"), dict) else {}
        if run_history:
            print(f"tool_runs: {run_history.get('run_count', 0)}")
            run_top = run_history.get("top_issue") if isinstance(run_history.get("top_issue"), dict) else None
            if run_top:
                print(
                    "tool_run_top_issue: "
                    f"{run_top.get('run_id')} {run_top.get('issue_type')} "
                    f"{helpers._short(str(run_top.get('detail', '')))}"
                )
        checkpoints = tool_catalog.get("checkpoints") if isinstance(tool_catalog.get("checkpoints"), dict) else {}
        if checkpoints:
            print(f"tool_checkpoints: {checkpoints.get('checkpoint_count', 0)}")
            checkpoint_top = checkpoints.get("top_issue") if isinstance(checkpoints.get("top_issue"), dict) else None
            if checkpoint_top:
                print(
                    "tool_checkpoint_top_issue: "
                    f"{checkpoint_top.get('checkpoint_id')} {checkpoint_top.get('issue_type')} "
                    f"{helpers._short(str(checkpoint_top.get('detail', '')))}"
                )

    roadmap_completion = (
        payload.get("roadmap_completion") if isinstance(payload.get("roadmap_completion"), dict) else {}
    )
    if roadmap_completion:
        issue_count = roadmap_completion.get("issue_count")
        print(f"roadmap_completion: {helpers._count_status(issue_count)}")
        top_roadmap = (
            roadmap_completion.get("top_issue") if isinstance(roadmap_completion.get("top_issue"), dict) else None
        )
        if top_roadmap:
            print(f"roadmap_top_issue: {top_roadmap.get('name')} {helpers._short(str(top_roadmap.get('detail', '')))}")

    repo_fleet = payload.get("repo_fleet") if isinstance(payload.get("repo_fleet"), dict) else {}
    if repo_fleet:
        print(f"repo_fleet_config: {repo_fleet.get('config_path')}")
        issue_count = repo_fleet.get("issue_count")
        print(f"repo_fleet: {helpers._count_status(issue_count)}")
        top_repo = repo_fleet.get("top_issue") if isinstance(repo_fleet.get("top_issue"), dict) else None
        if top_repo:
            print(f"repo_fleet_top_issue: {top_repo.get('name')} {helpers._short(str(top_repo.get('detail', '')))}")

    context_packs = payload.get("context_packs") if isinstance(payload.get("context_packs"), dict) else {}
    if context_packs:
        print(f"context_packs: {context_packs.get('pack_count', 0)}")
        if context_packs.get("issue_count"):
            top_context = context_packs.get("top_issue") if isinstance(context_packs.get("top_issue"), dict) else None
            if top_context:
                print(
                    f"context_top_issue: {top_context.get('name')} {helpers._short(str(top_context.get('detail', '')))}"
                )

    project_consolidation = (
        payload.get("project_consolidation") if isinstance(payload.get("project_consolidation"), dict) else {}
    )
    if project_consolidation:
        issue_count = project_consolidation.get("issue_count")
        print(f"project_consolidation: {helpers._count_status(issue_count)}")
        top_project = (
            project_consolidation.get("top_issue") if isinstance(project_consolidation.get("top_issue"), dict) else None
        )
        if top_project:
            print(
                f"project_consolidation_top_issue: {top_project.get('name')} {helpers._short(str(top_project.get('detail', '')))}"
            )

    learning = payload.get("learning") if isinstance(payload.get("learning"), dict) else {}
    if learning:
        print(f"learning_candidates: {learning.get('candidate_count', 0)}")
        top_learning = learning.get("top_issue") if isinstance(learning.get("top_issue"), dict) else None
        if top_learning:
            print(
                f"learning_top_issue: {top_learning.get('name')} {helpers._short(str(top_learning.get('detail', '')))}"
            )

    research_handoffs = payload.get("research_handoffs") if isinstance(payload.get("research_handoffs"), dict) else {}
    if research_handoffs:
        print(f"research_handoffs: {helpers._count_status(research_handoffs.get('issue_count'))}")
        top_research = (
            research_handoffs.get("top_issue") if isinstance(research_handoffs.get("top_issue"), dict) else None
        )
        if top_research:
            print(f"research_handoff_top_issue: {top_research.get('run_id')} {top_research.get('status')}")
            if top_research.get("suggested_next_command"):
                print(f"research_handoff_command: {top_research.get('suggested_next_command')}")

    operator_report = payload.get("operator_report") if isinstance(payload.get("operator_report"), dict) else {}
    if operator_report:
        latest_report = operator_report.get("latest") if isinstance(operator_report.get("latest"), dict) else None
        if latest_report:
            print(f"operator_report_latest: {latest_report.get('report_id')} {latest_report.get('created_at')}")
        issue_count = operator_report.get("issue_count")
        print(f"operator_report: {helpers._count_status(issue_count)}")
        top_report = operator_report.get("top_issue") if isinstance(operator_report.get("top_issue"), dict) else None
        if top_report:
            print(
                f"operator_report_top_issue: {top_report.get('name')} {helpers._short(str(top_report.get('detail', '')))}"
            )
            if top_report.get("suggested_next_command"):
                print(f"operator_report_command: {top_report.get('suggested_next_command')}")

    operator_actions = payload.get("operator_actions") if isinstance(payload.get("operator_actions"), dict) else {}
    if operator_actions:
        print(f"operator_actions: {operator_actions.get('open_count', 0)} open")
        top_action = (
            operator_actions.get("top_action") if isinstance(operator_actions.get("top_action"), dict) else None
        )
        if top_action:
            print(
                f"operator_action_top: {top_action.get('action_id')} {top_action.get('source_group')} {helpers._short(str(top_action.get('safe_summary', '')))}"
            )
            if top_action.get("suggested_command"):
                print(f"operator_action_command: {top_action.get('suggested_command')}")

    code_review = payload.get("code_review")
    if isinstance(code_review, dict):
        latest_review = code_review.get("latest_run") if isinstance(code_review.get("latest_run"), dict) else None
        if latest_review:
            print(
                f"review_latest: {latest_review.get('run_id')} "
                f"{latest_review.get('reviewer_id')} [{latest_review.get('status')}]"
            )
        unclosed_review = (
            code_review.get("latest_unclosed_run") if isinstance(code_review.get("latest_unclosed_run"), dict) else None
        )
        if unclosed_review:
            print(f"review_unclosed: {unclosed_review.get('run_id')} {unclosed_review.get('reviewer_id')}")
        if code_review.get("pending_finding_count"):
            print(f"review_pending_findings: {code_review.get('pending_finding_count')}")
        if code_review.get("unresolved_finding_count"):
            print(f"review_unresolved_findings: {code_review.get('unresolved_finding_count')}")
        top_review = (
            code_review.get("top_pending_finding") if isinstance(code_review.get("top_pending_finding"), dict) else None
        )
        if not top_review:
            top_review = (
                code_review.get("top_unresolved_finding")
                if isinstance(code_review.get("top_unresolved_finding"), dict)
                else None
            )
        if top_review:
            finding_id = top_review.get("id") or top_review.get("import_id")
            print(f"review_top_finding: {finding_id} {helpers._short(str(top_review.get('text', '')))}")
            print(f"review_top_command: brigade work review finding-show {finding_id}")

    handoff_issues = payload.get("handoff_issues")
    if isinstance(handoff_issues, dict) and handoff_issues.get("count"):
        print(f"handoff_ingest_issues_new: {handoff_issues.get('count')}")
        by_category = handoff_issues.get("by_category")
        if isinstance(by_category, dict) and by_category:
            print("handoff_ingest_issues_by_category:")
            for category, count in by_category.items():
                print(f"  {category}: {count}")
    if isinstance(handoff_issues, dict) and handoff_issues.get("known_count"):
        print(f"handoff_ingest_issues_known: {handoff_issues.get('known_count')}")
    handoff_drafts = payload.get("handoff_drafts")
    if isinstance(handoff_drafts, dict):
        counts = handoff_drafts.get("counts") if isinstance(handoff_drafts.get("counts"), dict) else {}
        total = int(counts.get("total", 0) or 0)
        if total:
            print(f"handoff_drafts_pending: {counts.get('pending', 0)}")
            print(f"handoff_drafts_reviewed: {counts.get('reviewed', 0)}")
            latest_ingest = (
                handoff_drafts.get("latest_ingest_run")
                if isinstance(handoff_drafts.get("latest_ingest_run"), dict)
                else None
            )
            if latest_ingest:
                print(
                    f"handoff_ingest_latest: {latest_ingest.get('run_id')} "
                    f"completed={latest_ingest.get('completed_at')}"
                )
            top_issue = handoff_drafts.get("top_issue") if isinstance(handoff_drafts.get("top_issue"), dict) else None
            if top_issue:
                print(
                    f"handoff_draft_top_issue: {top_issue.get('name')} {helpers._short(str(top_issue.get('detail', '')))}"
                )
            drafts = handoff_drafts.get("drafts") if isinstance(handoff_drafts.get("drafts"), list) else []
            if drafts:
                first = drafts[0]
                print(f"handoff_draft_next: {first.get('id')} {first.get('status')} {first.get('path')}")
                print(f"handoff_draft_next_command: brigade handoff show {first.get('id')}")

    recent = payload["recent_sessions"]
    if isinstance(recent, list) and recent:
        print("recent_sessions:")
        for item in recent:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("id")
            print(f"  - {item.get('started_at')} [{item.get('status')}] {helpers._short(str(title))}")
    if payload.get("skipped_sessions"):
        print(f"skipped_sessions: {payload['skipped_sessions']}", file=sys.stderr)
    return 0
