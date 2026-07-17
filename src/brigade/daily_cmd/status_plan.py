"""Agent-facing daily driver over local Brigade operator state."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    center_cmd,
    context_cmd,
    handoff_cmd,
    memory_cmd,
    notifications_cmd,
    phases_cmd,
    security_cmd,
    toml_compat as tomllib,
    tools_cmd,
    work_cmd,
)
from ..localio import read_json_dict as _read_json, utc_now as _now, write_json as _write_json
from ..render import emit

from . import config as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _config_blockers(config: dict[str, Any], action: dict[str, Any] | None, *, approved: bool = False) -> list[str]:
    blockers: list[str] = []
    if not config.get("enabled", True):
        blockers.append("daily driver is disabled")
    if action is None:
        return blockers
    risk = str(action.get("risk_level") or "low")
    max_risk = str(config.get("max_risk_without_approval") or "medium")
    if RISK_LEVELS.get(risk, 99) > RISK_LEVELS.get(max_risk, 2) and not approved:
        blockers.append(f"risk level {risk} exceeds max_risk_without_approval={max_risk}")
    action_type = str(action.get("action_type"))
    if action_type == "run-task" and not config.get("allow_work_run", True):
        blockers.append("work run adapter disabled by daily config")
    if action_type == "promote-import" and not config.get("allow_import_promotion_with_approval", True):
        blockers.append("import promotion adapter disabled by daily config")
    if action_type == "import-readiness-issues" and not config.get("allow_readiness_imports", True):
        blockers.append("readiness import adapter disabled by daily config")
    if action_type == "build-operator-report" and not config.get("allow_operator_report_build", True):
        blockers.append("operator report adapter disabled by daily config")
    return blockers


def _evidence_blockers(target: Path, action: dict[str, Any] | None) -> list[str]:
    if action is None:
        return ["no selected action"]
    action_type = str(action.get("action_type"))
    source_id = str(action.get("source_local_id") or "")
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    if action_type == "run-task":
        task_id = str(metadata.get("task_id") or source_id)
        if not any(str(task.get("id")) == task_id for task in work_cmd._pending_tasks(target)):
            return [f"pending task not found: {task_id}"]
    if action_type == "promote-import":
        import_id = str(metadata.get("import_id") or source_id)
        if not any(str(item.get("id")) == import_id for item in work_cmd._pending_imports(target)):
            return [f"pending import not found: {import_id}"]
    if action_type == "start-center-action":
        action_id = str(metadata.get("action_id") or source_id)
        if not any(
            str(item.get("action_id")) == action_id and item.get("status") in {"pending", "active"}
            for item in center_cmd._read_actions(target)
        ):
            return [f"center action not found: {action_id}"]
    if action_type == "start-phase-action":
        action_id = str(metadata.get("action_id") or source_id)
        if not any(
            str(item.get("action_id")) == action_id and item.get("status") in {"pending", "active"}
            for item in phases_cmd._read_actions(target)
        ):
            return [f"phase action not found: {action_id}"]
    if action_type in {"write-phase-session-checkpoint", "build-phase-session-report", "closeout-phase-session"}:
        session_id = str(metadata.get("session_id") or source_id)
        _path, session, _error = phases_cmd._resolve_session(target, session_id)
        if session is None:
            return [f"phase session not found: {session_id}"]
    return []


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale(value: object, hours: int) -> bool:
    parsed = _parse_time(value)
    if parsed is None:
        return False
    return _now() - parsed > timedelta(hours=hours)


def _age_hours(value: object) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return (_now() - parsed).total_seconds() / 3600


def _daily_center_status_payload(target: Path) -> dict[str, Any]:
    active = work_cmd._active_session_info(target)
    pending_tasks = work_cmd._pending_tasks(target)
    pending_imports = work_cmd._pending_imports(target)
    action_queue = center_cmd.actions_health(target)
    review_queue_count = len(pending_imports) + int(action_queue.get("open_count") or 0)
    handoffs = handoff_cmd.draft_queue_payload(target)
    memory = memory_cmd.health(target)
    security = _daily_security_health(target)
    notifications = notifications_cmd.health(target)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-center-status", "version": SCHEMA_VERSION},
        "target": str(target),
        "active_session": active,
        "pending_task_count": len(pending_tasks),
        "pending_import_count": len(pending_imports),
        "review_queue_count": review_queue_count,
        "action_queue": action_queue,
        "handoff_drafts": handoffs,
        "memory_care": memory,
        "security": security,
        "notifications": notifications,
        "tool_catalog": {},
        "release_readiness": None,
    }


def _daily_security_health(target: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    config_exists = security_cmd.config_path(target).is_file()
    if config_exists:
        checks.append({"status": "ok", "name": "security_config", "detail": str(security_cmd.config_path(target))})
    else:
        checks.append({"status": "warn", "name": "security_config", "detail": "missing"})
    bundle = security_cmd.inspect_evidence_bundle(security_cmd.default_artifacts_dir(target))
    if bundle.get("ready"):
        checks.append(
            {"status": "ok", "name": "security_evidence", "detail": f"findings={bundle.get('finding_count')}"}
        )
    else:
        checks.append({"status": "warn", "name": "security_evidence", "detail": str(bundle.get("reason"))})
    issues = [item for item in checks if item.get("status") != "ok"]
    top_finding = None
    if bundle.get("ready") and int(bundle.get("finding_count") or 0) > 0:
        top_finding = {
            "id": "latest",
            "title": "latest security scan has findings",
            "severity": "unknown",
            "status": "warn",
        }
        checks.append(
            {
                "status": "warn",
                "name": "security_open_findings",
                "detail": f"{bundle.get('finding_count')} finding(s) in latest evidence",
            }
        )
        issues = [item for item in checks if item.get("status") != "ok"]
    return {
        "target": str(target),
        "config_path": str(security_cmd.config_path(target)),
        "valid": not any(item.get("status") == "fail" for item in checks),
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "top_finding": top_finding,
        "checks": checks,
        "evidence": bundle,
        "daily_lightweight": True,
    }


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    status_section_checks: list[dict[str, Any]] = []
    center, check = _bounded_status_call("center-status", lambda: _daily_center_status_payload(target), {})
    status_section_checks.append(check)
    readiness, check = _bounded_status_call(
        "center-readiness", lambda: _daily_readiness_payload(target), {"blockers": []}
    )
    status_section_checks.append(check)
    config, config_checks = _load_config(target)
    operator_report_health, check = _bounded_status_call(
        "operator-report-health",
        lambda: center_cmd.report_health(target),
        {"latest": None, "checks": [], "issue_count": 0, "top_issue": None},
    )
    status_section_checks.append(check)
    candidates = _all_candidates(
        target,
        diagnostics=status_section_checks,
        operator_report_health=operator_report_health,
    )
    selected = _selected(candidates)
    handoffs = center.get("handoff_drafts") if isinstance(center.get("handoff_drafts"), dict) else {}
    memory = center.get("memory_care") if isinstance(center.get("memory_care"), dict) else {}
    security = center.get("security") if isinstance(center.get("security"), dict) else {}
    notifications = center.get("notifications") if isinstance(center.get("notifications"), dict) else {}
    tools = center.get("tool_catalog") if isinstance(center.get("tool_catalog"), dict) else {}
    latest_report = (
        operator_report_health.get("latest")
        if isinstance(operator_report_health, dict) and isinstance(operator_report_health.get("latest"), dict)
        else None
    )
    daily_health_fallback = {
        "schema_version": SCHEMA_VERSION,
        "config_path": str(_config_path(target)),
        "run_count": 0,
        "plan_count": 0,
        "latest_run": None,
        "latest_plan": None,
        "approvals": {},
        "telemetry": {},
        "phase_ledger": {},
        "phase_session": None,
        "checks": [],
        "issue_count": 0,
        "top_issue": None,
    }
    daily_health, check = _bounded_status_call("daily-health", lambda: health(target), daily_health_fallback)
    status_section_checks.append(check)
    phase_health, check = _bounded_status_call("phase-health", lambda: phases_cmd.health(target), {})
    status_section_checks.append(check)
    latest_phase_session, check = _bounded_status_call(
        "latest-phase-session", lambda: phases_cmd._latest_session(target), None
    )
    status_section_checks.append(check)
    approvals = daily_health.get("approvals") if isinstance(daily_health.get("approvals"), dict) else {}
    status_section_issues = [item for item in status_section_checks if item.get("status") != "ok"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("daily-status"),
        "target": str(target),
        "config": config,
        "config_checks": config_checks,
        "status_section_checks": status_section_checks,
        "status_section_issue_count": len(status_section_issues),
        "top_status_section_issue": status_section_issues[0] if status_section_issues else None,
        "daily_health": daily_health,
        "phase_ledger": phase_health,
        "phase_session": phases_cmd._session_summary(latest_phase_session)
        if isinstance(latest_phase_session, dict)
        else None,
        "top_pending_approval": approvals.get("top_pending"),
        "telemetry": daily_health.get("telemetry"),
        "active_session": center.get("active_session"),
        "pending_task_count": center.get("pending_task_count", 0),
        "pending_import_count": center.get("pending_import_count", 0),
        "center_review_count": center.get("review_queue_count", 0),
        "open_daily_action_count": (center.get("action_queue") or {}).get("open_count", 0)
        if isinstance(center.get("action_queue"), dict)
        else 0,
        "top_readiness_blocker": readiness.get("blockers", [None])[0] if readiness.get("blockers") else None,
        "pending_handoff_draft_count": (handoffs.get("counts") or {}).get("pending", 0)
        if isinstance(handoffs.get("counts"), dict)
        else int(handoffs.get("draft_count") or 0),
        "memory_care_issue_count": int(memory.get("issue_count") or 0),
        "security_issue_count": int(security.get("issue_count") or security.get("finding_count") or 0),
        "notification_issue_count": int(notifications.get("issue_count") or 0),
        "notifications": notifications,
        "tool_approval_count": int(
            ((tools.get("call_queue") or {}) if isinstance(tools.get("call_queue"), dict) else {}).get("pending_count")
            or 0
        ),
        "tool_checkpoint_count": int(
            ((tools.get("checkpoints") or {}) if isinstance(tools.get("checkpoints"), dict) else {}).get("open_count")
            or 0
        ),
        "latest_release_readiness": center.get("release_readiness"),
        "latest_operator_report": {
            "report_id": latest_report.get("report_id"),
            "status": latest_report.get("status"),
            "path": latest_report.get("path"),
        }
        if isinstance(latest_report, dict)
        else None,
        "next_recommended_command": selected.get("suggested_next_command") if selected else "brigade daily plan",
        "selected_action": selected,
    }


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    lines: list[str] = [
        f"daily status: {payload['target']}",
        f"pending_tasks: {payload['pending_task_count']}",
        f"pending_imports: {payload['pending_import_count']}",
        f"center_reviews: {payload['center_review_count']}",
        f"open_actions: {payload['open_daily_action_count']}",
    ]
    notifications = payload.get("notifications") if isinstance(payload.get("notifications"), dict) else {}
    if notifications:
        lines.append(f"notifications: {notifications.get('status')} configured={notifications.get('configured')}")
    if payload.get("status_section_issue_count"):
        lines.append(f"status_section_issues: {payload['status_section_issue_count']}")
        top = payload.get("top_status_section_issue")
        if isinstance(top, dict):
            lines.append(f"top_status_section_issue: {top.get('name')} {top.get('detail')}")
    phase_ledger = payload.get("phase_ledger") if isinstance(payload.get("phase_ledger"), dict) else {}
    if phase_ledger:
        lines.append(f"phase_records: {phase_ledger.get('record_count', 0)}")
        lines.append(f"phase_issues: {phase_ledger.get('issue_count', 0)}")
    phase_session = payload.get("phase_session") if isinstance(payload.get("phase_session"), dict) else None
    if phase_session:
        lines.append(f"phase_session: {phase_session.get('session_id')} [{phase_session.get('status')}]")
    blocker = payload.get("top_readiness_blocker")
    lines.append(f"top_readiness_blocker: {blocker.get('safe_summary') if isinstance(blocker, dict) else 'none'}")
    lines.append(f"next: {payload['next_recommended_command']}")
    return emit(payload, json_output, lines, 0)


def plan_payload(target: Path, *, record: bool = False) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config, config_checks = _load_config(target)
    candidates = _all_candidates(target)
    selected = _selected(candidates)
    selected_id = selected.get("action_id") if selected else None
    candidate_explanations = [_candidate_explanation(target, config, action, selected_id) for action in candidates]
    selection_blockers = (
        _candidate_blockers(target, config, selected) if selected else _candidate_blockers(target, config, None)
    )
    created = _now().isoformat()
    plan_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-daily-plan-{uuid4().hex[:6]}"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("daily-plan"),
        "target": str(target),
        "config": config,
        "config_checks": config_checks,
        "plan_id": plan_id,
        "created_at": created,
        "candidate_actions": candidates,
        "ranked_candidates": candidates,
        "candidate_explanations": candidate_explanations,
        "candidate_count": len(candidates),
        "selected_action": selected,
        "selected_action_id": selected.get("action_id") if selected else None,
        "source_subsystem": selected.get("source_subsystem") if selected else None,
        "source_local_id": selected.get("source_local_id") if selected else None,
        "source_fingerprint": selected.get("source_fingerprint") if selected else None,
        "approval_required": bool(selected.get("approval_required")) if selected else False,
        "approval_requirement": selected.get("approval_reason")
        if selected and selected.get("approval_required")
        else None,
        "ranking_reasons": selected.get("ranking_reasons") if selected else [],
        "selection_reasons": selected.get("ranking_reasons") if selected else [],
        "rejection_reasons": {
            str(item["action_id"]): item["rejection_reasons"]
            for item in candidate_explanations
            if item.get("action_id") != selected_id
        },
        "safety_blockers": selection_blockers["safety_blockers"],
        "approval_blockers": selection_blockers["approval_blockers"],
        "stale_evidence_blockers": selection_blockers["stale_evidence_blockers"],
        "quality_blockers": selection_blockers["quality_blockers"],
        "suggested_next_command": selected.get("suggested_next_command") if selected else "brigade daily status",
        "can_run_without_approval": bool(selected and not selected.get("approval_required")),
        "requires_explicit_approval": bool(selected and selected.get("approval_required")),
        "config_blockers": selection_blockers["config_blockers"],
        "evidence_blockers": selection_blockers["stale_evidence_blockers"],
        "recorded": False,
    }
    if record:
        plan_dir = _plans_root(target) / plan_id
        payload["recorded"] = True
        payload["path"] = str(plan_dir)
        _write_json(plan_dir / "plan.json", payload)
    return payload


def plan(*, target: Path, record: bool = False, json_output: bool = False) -> int:
    payload = plan_payload(target, record=record)
    lines: list[str] = [
        f"daily plan: {payload['target']}",
        f"candidates: {payload['candidate_count']}",
    ]
    selected = payload.get("selected_action")
    if isinstance(selected, dict):
        lines.append(f"selected: {selected['action_id']}")
        lines.append(f"summary: {selected['safe_summary']}")
        lines.append(f"approval_required: {selected['approval_required']}")
        lines.append(f"next: {selected['suggested_next_command']}")
    else:
        lines.append("selected: none")
    if record:
        lines.append(f"recorded: {payload.get('path')}")
    return emit(payload, json_output, lines, 0)


def _review_payload(target: Path, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config, config_checks = _load_config(target)
    action = selected or _selected(_all_candidates(target))
    explain = (
        _candidate_explanation(target, config, action, action.get("action_id") if action else None) if action else None
    )
    context_plan = None
    context_would_build = bool(action and action.get("context_kind") and config.get("allow_context_pack_build", True))
    approval_request = None
    if action and action.get("approval_required"):
        approval_request = next(
            (
                approval
                for approval in _matching_approvals(target, action, config)
                if approval.get("status") in {"pending", "approved"}
            ),
            None,
        )
    if action and action.get("context_kind") and config.get("allow_context_pack_build", True):
        context_plan = context_cmd._context_payload(
            target,
            kind=str(action.get("context_kind")),
            task_id=str((action.get("metadata") or {}).get("task_id") or "") or None,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("daily-review"),
        "target": str(target),
        "config": config,
        "config_checks": config_checks,
        "selected_action": action,
        "selected_adapter": _adapter_for(action),
        "source_subsystem": action.get("source_subsystem") if action else None,
        "source_local_id": action.get("source_local_id") if action else None,
        "safe_summary": action.get("safe_summary") if action else None,
        "source_evidence_refs": action.get("evidence_refs") if action else [],
        "acceptance": action.get("acceptance") if action else [],
        "risk_level": action.get("risk_level") if action else None,
        "approval_required": bool(action.get("approval_required")) if action else False,
        "approval_boundary": action.get("approval_reason")
        if action and action.get("approval_required")
        else "no explicit approval required",
        "approval_request": approval_request,
        "likely_next_command": action.get("suggested_next_command") if action else None,
        "context_pack_plan": context_plan,
        "context_pack_would_build": context_would_build,
        "selection_reasons": action.get("ranking_reasons") if action else [],
        "candidate_explanation": explain,
        "safety_blockers": explain.get("safety_blockers") if isinstance(explain, dict) else [],
        "approval_blockers": explain.get("approval_blockers") if isinstance(explain, dict) else [],
        "quality_blockers": explain.get("quality_blockers") if isinstance(explain, dict) else [],
        "config_blockers": _config_blockers(config, action),
        "evidence_blockers": _evidence_blockers(target, action),
        "writes": [],
    }


def review(*, target: Path, json_output: bool = False) -> int:
    payload = _review_payload(target)
    lines: list[str] = [f"daily review: {payload['target']}"]
    if payload.get("selected_action"):
        lines.append(f"selected: {payload['selected_action']['action_id']}")
        lines.append(f"summary: {payload['safe_summary']}")
        lines.append(f"risk: {payload['risk_level']}")
        lines.append(f"adapter: {payload['selected_adapter']}")
        lines.append(f"approval: {payload['approval_boundary']}")
        if payload.get("config_blockers"):
            lines.append(f"config_blockers: {len(payload['config_blockers'])}")
        if payload.get("evidence_blockers"):
            lines.append(f"evidence_blockers: {len(payload['evidence_blockers'])}")
        lines.append(f"next: {payload['likely_next_command']}")
    else:
        lines.append("selected: none")
    return emit(payload, json_output, lines, 0)
