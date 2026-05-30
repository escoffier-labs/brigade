"""Agent-facing daily driver over local Brigade operator state."""
from __future__ import annotations

import json
import re
import sys
import tomllib
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import center_cmd, context_cmd, handoff_cmd, memory_cmd, security_cmd, tools_cmd, work_cmd

SCHEMA_VERSION = 1
RUN_STATUSES = {"reviewed", "deferred", "blocked", "archived"}
PREFERRED_MODES = {"task-first", "inbox-first", "readiness-first"}
RISK_LEVELS = {"low": 1, "medium": 2, "high": 3}
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "preferred_mode": "task-first",
    "max_risk_without_approval": "medium",
    "allow_context_pack_build": True,
    "allow_operator_report_build": True,
    "allow_readiness_imports": True,
    "allow_import_promotion_with_approval": True,
    "allow_work_run": True,
    "stale_plan_threshold_hours": 12,
    "stale_run_threshold_hours": 12,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _daily_root(target: Path) -> Path:
    return target / ".brigade" / "daily"


def _config_path(target: Path) -> Path:
    return target / ".brigade" / "daily.toml"


def _plans_root(target: Path) -> Path:
    return _daily_root(target) / "plans"


def _runs_root(target: Path) -> Path:
    return _daily_root(target) / "runs"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _schema(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": SCHEMA_VERSION,
        "item_fields": [
            "action_id",
            "source_subsystem",
            "source_local_id",
            "safe_summary",
            "score",
            "risk_level",
            "approval_required",
            "suggested_next_command",
        ],
    }


def _schemas() -> dict[str, Any]:
    base_fields = _schema("daily-item")["item_fields"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schemas": [
            {"name": "daily-status", "top_level_fields": ["target", "selected_action", "next_recommended_command", "daily_health"], "item_fields": base_fields},
            {"name": "daily-plan", "top_level_fields": ["plan_id", "candidate_actions", "selected_action", "approval_required", "recorded"], "item_fields": base_fields},
            {"name": "daily-review", "top_level_fields": ["selected_action", "selected_adapter", "source_evidence_refs", "acceptance", "config_blockers", "context_pack_would_build"], "item_fields": base_fields},
            {"name": "daily-run", "top_level_fields": ["run_id", "plan_id", "selected_action", "status", "commands_invoked", "receipts_created", "blockers"], "item_fields": base_fields},
            {"name": "daily-closeout", "top_level_fields": ["run_id", "closeout_status", "reviewed_at", "handoff_path"], "item_fields": []},
            {"name": "daily-history", "top_level_fields": ["runs", "plans", "run_count", "plan_count"], "item_fields": ["id", "status", "created_at", "path"]},
            {"name": "daily-doctor", "top_level_fields": ["checks", "issue_count", "top_issue", "health"], "item_fields": ["status", "name", "detail"]},
        ],
    }


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Local Brigade daily driver settings.", ""]
    for key, value in config.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(str(value))
        lines.append(f"{key} = {rendered}")
    path.write_text("\n".join(lines) + "\n")


def _load_config(target: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = _config_path(target)
    checks: list[dict[str, Any]] = []
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        checks.append({"status": "warn", "name": "daily_config_missing", "detail": f"missing {path}; run `brigade daily init`"})
        return config, checks
    try:
        loaded = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        checks.append({"status": "fail", "name": "daily_config_invalid", "detail": str(exc)})
        return config, checks
    if not isinstance(loaded, dict):
        checks.append({"status": "fail", "name": "daily_config_invalid", "detail": "config must be a TOML table"})
        return config, checks
    config.update(loaded)
    checks.extend(_validate_config(config))
    if not checks:
        checks.append({"status": "ok", "name": "daily_config", "detail": str(path)})
    return config, checks


def _validate_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if config.get("preferred_mode") not in PREFERRED_MODES:
        checks.append({"status": "fail", "name": "daily_preferred_mode", "detail": "expected task-first, inbox-first, or readiness-first"})
    if config.get("max_risk_without_approval") not in RISK_LEVELS:
        checks.append({"status": "fail", "name": "daily_max_risk", "detail": "expected low, medium, or high"})
    for key in (
        "enabled",
        "allow_context_pack_build",
        "allow_operator_report_build",
        "allow_readiness_imports",
        "allow_import_promotion_with_approval",
        "allow_work_run",
    ):
        if not isinstance(config.get(key), bool):
            checks.append({"status": "fail", "name": key, "detail": "expected boolean"})
    for key in ("stale_plan_threshold_hours", "stale_run_threshold_hours"):
        value = config.get(key)
        if not isinstance(value, int) or value < 1:
            checks.append({"status": "fail", "name": key, "detail": "expected positive integer hours"})
    if config.get("enabled") is False:
        checks.append({"status": "warn", "name": "daily_disabled", "detail": "daily driver is disabled in local config"})
    if config.get("max_risk_without_approval") == "high":
        checks.append({"status": "warn", "name": "daily_risk_policy", "detail": "high risk actions are allowed without approval"})
    return checks


def _safe_text(target: Path, value: object) -> str:
    text = str(value or "")
    text = text.replace(str(target), "<target>")
    text = re.sub(r"/(?:tmp|home|Users|private|mnt|Volumes)/[A-Za-z0-9_.@/-]+", "<path>", text)
    text = re.sub(r"https?://[^\s`\"'<>]+", "<url>", text)
    text = re.sub(r"(?i)(token|secret|password|api[_-]?key)=\S+", r"\1=<redacted>", text)
    return text[:500]


def _fingerprint(value: Any) -> str:
    return work_cmd._stable_hash(value)


def _priority_score(priority: object) -> int:
    value = str(priority or "normal").casefold()
    return {"urgent": 45, "high": 35, "normal": 20, "low": 5}.get(value, 20)


def _candidate(
    *,
    target: Path,
    action_type: str,
    source_subsystem: str,
    source_local_id: str,
    safe_summary: str,
    suggested_next_command: str,
    score: int,
    ranking_reasons: list[str],
    approval_required: bool = False,
    approval_reason: str | None = None,
    risk_level: str = "low",
    acceptance: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    source_fingerprint: str | None = None,
    context_kind: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_fingerprint = source_fingerprint or _fingerprint(
        {
            "action_type": action_type,
            "source_subsystem": source_subsystem,
            "source_local_id": source_local_id,
            "safe_summary": safe_summary,
        }
    )
    action_id = f"daily-{source_subsystem}-{source_local_id}-{source_fingerprint[:10]}"
    return {
        "action_id": action_id,
        "action_type": action_type,
        "source_subsystem": source_subsystem,
        "source_local_id": source_local_id,
        "safe_summary": _safe_text(target, safe_summary),
        "suggested_next_command": suggested_next_command,
        "score": score,
        "ranking_reasons": ranking_reasons,
        "approval_required": approval_required,
        "approval_reason": approval_reason,
        "risk_level": risk_level,
        "acceptance": acceptance or [],
        "evidence_refs": evidence_refs or [],
        "source_fingerprint": source_fingerprint,
        "context_kind": context_kind,
        "metadata": metadata or {},
    }


def _pending_task_candidates(target: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for task in work_cmd._pending_tasks(target):
        task_id = str(task.get("id") or "")
        acceptance = work_cmd._task_acceptance(task)
        score = 300 + _priority_score(task.get("priority")) + (50 if acceptance else 0)
        candidates.append(
            _candidate(
                target=target,
                action_type="run-task",
                source_subsystem="work-task",
                source_local_id=task_id,
                safe_summary=str(task.get("text") or "pending task"),
                suggested_next_command="brigade work run",
                score=score,
                ranking_reasons=[
                    "pending ledger task",
                    "has acceptance criteria" if acceptance else "missing acceptance criteria",
                    f"priority={task.get('priority') or 'normal'}",
                ],
                approval_required=False,
                risk_level="medium",
                acceptance=acceptance,
                evidence_refs=[str(work_cmd._tasks_path(target))],
                source_fingerprint=_fingerprint({"task_id": task_id, "text": task.get("text"), "acceptance": acceptance}),
                context_kind="task",
                metadata={"task_id": task_id},
            )
        )
    return candidates


def _pending_import_candidates(target: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in work_cmd._pending_imports(target):
        import_id = str(item.get("id") or "")
        acceptance = [str(value) for value in item.get("acceptance", [])] if isinstance(item.get("acceptance"), list) else []
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        has_provenance = bool(metadata.get("source_fingerprint") or metadata.get("scanner_run_id") or item.get("source"))
        score = 240 + _priority_score(item.get("priority")) + (40 if acceptance else 0) + (20 if has_provenance else -35)
        candidates.append(
            _candidate(
                target=target,
                action_type="promote-import",
                source_subsystem="work-import",
                source_local_id=import_id,
                safe_summary=str(item.get("text") or "pending import"),
                suggested_next_command=f"brigade work import promote {import_id}",
                score=score,
                ranking_reasons=[
                    "pending import",
                    "has acceptance criteria" if acceptance else "missing acceptance criteria",
                    "complete provenance" if has_provenance else "missing provenance",
                ],
                approval_required=True,
                approval_reason="promotion changes the local task ledger",
                risk_level="medium",
                acceptance=acceptance,
                evidence_refs=[str(work_cmd._imports_path(target))],
                source_fingerprint=str(metadata.get("source_fingerprint") or _fingerprint(item)),
                context_kind="task" if item.get("kind", "task") == "task" else None,
                metadata={"import_id": import_id, "kind": item.get("kind", "task")},
            )
        )
    return candidates


def _center_action_candidates(target: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for action in center_cmd._read_actions(target):
        if action.get("status") not in {"pending", "active"}:
            continue
        action_id = str(action.get("action_id") or "")
        score = 210 + _priority_score(action.get("priority"))
        candidates.append(
            _candidate(
                target=target,
                action_type="start-center-action" if action.get("status") == "pending" else "review-center-action",
                source_subsystem="center-action",
                source_local_id=action_id,
                safe_summary=str(action.get("safe_summary") or "operator action"),
                suggested_next_command=f"brigade center actions show {action_id}",
                score=score,
                ranking_reasons=["reviewed daily action queue item", f"status={action.get('status')}"],
                approval_required=False,
                risk_level="low",
                evidence_refs=[str(center_cmd._actions_path(target))],
                source_fingerprint=str(action.get("source_fingerprint") or _fingerprint(action)),
                metadata={"action_id": action_id},
            )
        )
    return candidates


def _readiness_candidates(target: Path) -> list[dict[str, Any]]:
    readiness = center_cmd._readiness_payload(target)
    candidates: list[dict[str, Any]] = []
    for finding in readiness.get("blockers", []):
        finding_id = str(finding.get("finding_id") or "")
        candidates.append(
            _candidate(
                target=target,
                action_type="import-readiness-issues",
                source_subsystem="center-readiness",
                source_local_id=finding_id,
                safe_summary=str(finding.get("safe_summary") or "readiness blocker"),
                suggested_next_command="brigade center readiness import-issues",
                score=180,
                ranking_reasons=["readiness blocker", "can be routed into work imports"],
                approval_required=False,
                risk_level="low",
                evidence_refs=[".brigade/center/readiness"],
                source_fingerprint=str(finding.get("source_fingerprint") or _fingerprint(finding)),
                metadata={"finding_id": finding_id},
            )
        )
    return candidates


def _health_issue_candidates(target: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    health_sources = [
        ("handoff", center_cmd.status_payload(target).get("handoff_drafts"), "brigade handoff doctor", 150),
        ("memory-care", memory_cmd.health(target), "brigade memory care doctor", 140),
        ("security", security_cmd.health(target), "brigade security doctor", 135),
        ("tools", tools_cmd.health(target), "brigade tools doctor", 130),
    ]
    for subsystem, health, command, base_score in health_sources:
        if not isinstance(health, dict):
            continue
        issue_count = int(health.get("issue_count") or 0)
        top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else None
        if issue_count <= 0 and not top:
            continue
        local_id = str((top or {}).get("name") or (top or {}).get("id") or subsystem)
        summary = str((top or {}).get("detail") or (top or {}).get("safe_summary") or f"{subsystem} has local issue(s)")
        candidates.append(
            _candidate(
                target=target,
                action_type="review-health-issue",
                source_subsystem=subsystem,
                source_local_id=local_id,
                safe_summary=summary,
                suggested_next_command=command,
                score=base_score,
                ranking_reasons=[f"{subsystem} health issue", "review before action"],
                approval_required=True,
                approval_reason="health issue review may require choosing a repair path",
                risk_level="low",
                evidence_refs=[f"{subsystem} health"],
                source_fingerprint=_fingerprint({"subsystem": subsystem, "summary": summary}),
            )
        )
    return candidates


def _report_candidate(target: Path) -> list[dict[str, Any]]:
    health = center_cmd.report_health(target)
    top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else None
    if not top:
        return []
    return [
        _candidate(
            target=target,
            action_type="build-operator-report",
            source_subsystem="center-report",
            source_local_id=str(top.get("name") or "report"),
            safe_summary=str(top.get("detail") or "operator report needs refresh"),
            suggested_next_command="brigade center report build",
            score=120,
            ranking_reasons=["operator report health issue", "local report build is safe"],
            approval_required=False,
            risk_level="low",
            evidence_refs=[".brigade/center/reports"],
            source_fingerprint=_fingerprint(top),
        )
    ]


def _all_candidates(target: Path) -> list[dict[str, Any]]:
    config, _ = _load_config(target)
    candidates = [
        *_pending_task_candidates(target),
        *_pending_import_candidates(target),
        *_center_action_candidates(target),
        *_readiness_candidates(target),
        *_health_issue_candidates(target),
        *_report_candidate(target),
    ]
    _apply_preferred_mode(candidates, str(config.get("preferred_mode") or "task-first"))
    candidates.sort(key=lambda item: (int(item.get("score") or 0), str(item.get("action_id") or "")), reverse=True)
    return candidates


def _apply_preferred_mode(candidates: list[dict[str, Any]], mode: str) -> None:
    for item in candidates:
        subsystem = item.get("source_subsystem")
        if mode == "inbox-first":
            if subsystem == "work-import":
                item["score"] = int(item.get("score") or 0) + 160
                item.setdefault("ranking_reasons", []).append("preferred_mode=inbox-first")
            elif subsystem == "work-task":
                item["score"] = int(item.get("score") or 0) - 80
        elif mode == "readiness-first":
            if subsystem == "center-readiness":
                item["score"] = int(item.get("score") or 0) + 220
                item.setdefault("ranking_reasons", []).append("preferred_mode=readiness-first")
            elif subsystem in {"work-task", "work-import"}:
                item["score"] = int(item.get("score") or 0) - 80


def _selected(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in candidates:
        command = str(item.get("suggested_next_command") or "")
        if re.search(r"\b(git\s+push|git\s+tag|gh\s+release|release\s+create|repo\s+transfer)\b", command, re.IGNORECASE):
            continue
        return item
    return None


def _adapter_for(action: dict[str, Any] | None) -> str | None:
    if not action:
        return None
    return {
        "run-task": "brigade work run",
        "promote-import": "brigade work import promote",
        "start-center-action": "brigade center actions start",
        "import-readiness-issues": "brigade center readiness import-issues",
        "build-operator-report": "brigade center report build",
        "review-center-action": "review-only",
        "review-health-issue": "review-only",
    }.get(str(action.get("action_type")), "unsupported")


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
        if not any(str(item.get("action_id")) == action_id and item.get("status") in {"pending", "active"} for item in center_cmd._read_actions(target)):
            return [f"center action not found: {action_id}"]
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


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    center = center_cmd.status_payload(target)
    readiness = center_cmd._readiness_payload(target)
    config, config_checks = _load_config(target)
    candidates = _all_candidates(target)
    selected = _selected(candidates)
    handoffs = center.get("handoff_drafts") if isinstance(center.get("handoff_drafts"), dict) else {}
    memory = center.get("memory_care") if isinstance(center.get("memory_care"), dict) else {}
    security = center.get("security") if isinstance(center.get("security"), dict) else {}
    tools = center.get("tool_catalog") if isinstance(center.get("tool_catalog"), dict) else {}
    latest_report = center_cmd.latest_report(target)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("daily-status"),
        "target": str(target),
        "config": config,
        "config_checks": config_checks,
        "daily_health": health(target),
        "active_session": center.get("active_session"),
        "pending_task_count": center.get("pending_task_count", 0),
        "pending_import_count": center.get("pending_import_count", 0),
        "center_review_count": center.get("review_queue_count", 0),
        "open_daily_action_count": (center.get("action_queue") or {}).get("open_count", 0) if isinstance(center.get("action_queue"), dict) else 0,
        "top_readiness_blocker": readiness.get("blockers", [None])[0] if readiness.get("blockers") else None,
        "pending_handoff_draft_count": (handoffs.get("counts") or {}).get("pending", 0) if isinstance(handoffs.get("counts"), dict) else int(handoffs.get("draft_count") or 0),
        "memory_care_issue_count": int(memory.get("issue_count") or 0),
        "security_issue_count": int(security.get("issue_count") or security.get("finding_count") or 0),
        "tool_approval_count": int(((tools.get("call_queue") or {}) if isinstance(tools.get("call_queue"), dict) else {}).get("pending_count") or 0),
        "tool_checkpoint_count": int(((tools.get("checkpoints") or {}) if isinstance(tools.get("checkpoints"), dict) else {}).get("open_count") or 0),
        "latest_release_readiness": center.get("release_readiness"),
        "latest_operator_report": {
            "report_id": latest_report.get("report_id"),
            "status": latest_report.get("status"),
            "path": latest_report.get("path"),
        } if isinstance(latest_report, dict) else None,
        "next_recommended_command": selected.get("suggested_next_command") if selected else "brigade daily plan",
        "selected_action": selected,
    }


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"daily status: {payload['target']}")
    print(f"pending_tasks: {payload['pending_task_count']}")
    print(f"pending_imports: {payload['pending_import_count']}")
    print(f"center_reviews: {payload['center_review_count']}")
    print(f"open_actions: {payload['open_daily_action_count']}")
    blocker = payload.get("top_readiness_blocker")
    print(f"top_readiness_blocker: {blocker.get('safe_summary') if isinstance(blocker, dict) else 'none'}")
    print(f"next: {payload['next_recommended_command']}")
    return 0


def plan_payload(target: Path, *, record: bool = False) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config, config_checks = _load_config(target)
    candidates = _all_candidates(target)
    selected = _selected(candidates)
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
        "candidate_count": len(candidates),
        "selected_action": selected,
        "selected_action_id": selected.get("action_id") if selected else None,
        "source_subsystem": selected.get("source_subsystem") if selected else None,
        "source_local_id": selected.get("source_local_id") if selected else None,
        "source_fingerprint": selected.get("source_fingerprint") if selected else None,
        "approval_required": bool(selected.get("approval_required")) if selected else False,
        "approval_requirement": selected.get("approval_reason") if selected and selected.get("approval_required") else None,
        "ranking_reasons": selected.get("ranking_reasons") if selected else [],
        "suggested_next_command": selected.get("suggested_next_command") if selected else "brigade daily status",
        "can_run_without_approval": bool(selected and not selected.get("approval_required")),
        "requires_explicit_approval": bool(selected and selected.get("approval_required")),
        "config_blockers": _config_blockers(config, selected),
        "evidence_blockers": _evidence_blockers(target, selected),
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"daily plan: {payload['target']}")
    print(f"candidates: {payload['candidate_count']}")
    selected = payload.get("selected_action")
    if isinstance(selected, dict):
        print(f"selected: {selected['action_id']}")
        print(f"summary: {selected['safe_summary']}")
        print(f"approval_required: {selected['approval_required']}")
        print(f"next: {selected['suggested_next_command']}")
    else:
        print("selected: none")
    if record:
        print(f"recorded: {payload.get('path')}")
    return 0


def _review_payload(target: Path, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config, config_checks = _load_config(target)
    action = selected or _selected(_all_candidates(target))
    context_plan = None
    context_would_build = bool(action and action.get("context_kind") and config.get("allow_context_pack_build", True))
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
        "approval_boundary": action.get("approval_reason") if action and action.get("approval_required") else "no explicit approval required",
        "likely_next_command": action.get("suggested_next_command") if action else None,
        "context_pack_plan": context_plan,
        "context_pack_would_build": context_would_build,
        "config_blockers": _config_blockers(config, action),
        "evidence_blockers": _evidence_blockers(target, action),
        "writes": [],
    }


def review(*, target: Path, json_output: bool = False) -> int:
    payload = _review_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"daily review: {payload['target']}")
    if payload.get("selected_action"):
        print(f"selected: {payload['selected_action']['action_id']}")
        print(f"summary: {payload['safe_summary']}")
        print(f"risk: {payload['risk_level']}")
        print(f"adapter: {payload['selected_adapter']}")
        print(f"approval: {payload['approval_boundary']}")
        if payload.get("config_blockers"):
            print(f"config_blockers: {len(payload['config_blockers'])}")
        if payload.get("evidence_blockers"):
            print(f"evidence_blockers: {len(payload['evidence_blockers'])}")
        print(f"next: {payload['likely_next_command']}")
    else:
        print("selected: none")
    return 0


def _latest_run(target: Path) -> dict[str, Any] | None:
    root = _runs_root(target)
    if not root.is_dir():
        return None
    runs: list[dict[str, Any]] = []
    for child in root.iterdir():
        if child.is_dir():
            payload = _read_json(child / "run.json")
            if payload is not None:
                runs.append(payload)
    runs.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return runs[0] if runs else None


def _iter_receipts(root: Path, filename: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    receipts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if not root.is_dir():
        return receipts, errors
    for child in root.iterdir():
        if not child.is_dir():
            continue
        path = child / filename
        payload = _read_json(path)
        if payload is None:
            errors.append({"path": str(path), "error": "missing or invalid JSON"})
            continue
        receipts.append(payload)
    receipts.sort(key=lambda item: str(item.get("started_at") or item.get("created_at") or item.get("run_id") or item.get("plan_id") or ""), reverse=True)
    return receipts, errors


def _latest_plan(target: Path) -> dict[str, Any] | None:
    plans, _ = _iter_receipts(_plans_root(target), "plan.json")
    return plans[0] if plans else None


def _resolve_plan(target: Path, plan_id: str | None) -> dict[str, Any] | None:
    if plan_id in (None, "", "latest"):
        return _latest_plan(target)
    path = _plans_root(target) / str(plan_id) / "plan.json"
    return _read_json(path)


def _record_run(target: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    run_id = str(receipt["run_id"])
    run_dir = _runs_root(target) / run_id
    receipt["path"] = str(run_dir)
    _write_json(run_dir / "run.json", receipt)
    return receipt


def _invoke_context_build(target: Path, action: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    if not action.get("context_kind"):
        return None, []
    task_id = (action.get("metadata") or {}).get("task_id")
    before = {str(pack.get("pack_id")) for pack in context_cmd._packs(target)}
    with redirect_stdout(StringIO()):
        rc = context_cmd.build(target=target, kind=str(action.get("context_kind")), task_id=str(task_id) if task_id else None, json_output=False)
    after = context_cmd._packs(target)
    created = next((pack for pack in after if str(pack.get("pack_id")) not in before), None)
    return (str(created.get("pack_id")) if isinstance(created, dict) else None), [{"command": "brigade context build", "exit_code": rc}]


def _blocked_run(
    *,
    target: Path,
    receipt: dict[str, Any],
    blockers: list[str],
    json_output: bool,
    next_command: str = "brigade daily review",
) -> int:
    receipt["status"] = "blocked"
    receipt["completed_at"] = _now().isoformat()
    receipt["next_recommended_command"] = next_command
    receipt["blockers"].extend(blockers)
    _record_run(target, receipt)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"daily run: {receipt['run_id']}")
        print("status: blocked")
        for blocker in blockers:
            print(f"blocker: {blocker}")
    return 1


def run(*, target: Path, approved: bool = False, plan_id: str | None = None, replan: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    config, _ = _load_config(target)
    if plan_id and not replan:
        plan_data = _resolve_plan(target, plan_id)
        if plan_data is None:
            plan_data = plan_payload(target, record=True)
            plan_data["plan_load_error"] = f"plan not found: {plan_id}"
    else:
        plan_data = plan_payload(target, record=True)
    action = plan_data.get("selected_action") if isinstance(plan_data.get("selected_action"), dict) else None
    run_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-daily-run-{uuid4().hex[:6]}"
    started = _now().isoformat()
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("daily-run"),
        "target": str(target),
        "run_id": run_id,
        "plan_id": plan_data.get("plan_id"),
        "selected_action_id": action.get("action_id") if action else None,
        "selected_action": action,
        "status": "running" if action else "blocked",
        "started_at": started,
        "completed_at": None,
        "commands_invoked": [],
        "receipts_created": [str(Path(str(plan_data.get("path") or "")) / "plan.json")] if plan_data.get("path") else [],
        "work_session_id": None,
        "task_id": None,
        "context_pack_id": None,
        "verification_receipt": None,
        "handoff_path": None,
        "blockers": [],
        "next_recommended_command": "brigade daily status",
        "config": config,
    }
    if plan_data.get("plan_load_error"):
        return _blocked_run(target=target, receipt=receipt, blockers=[str(plan_data["plan_load_error"])], json_output=json_output)
    if plan_id and not replan and _is_stale(plan_data.get("created_at"), int(config.get("stale_plan_threshold_hours") or 12)):
        return _blocked_run(target=target, receipt=receipt, blockers=[f"recorded plan is stale: {plan_data.get('plan_id')}"], json_output=json_output, next_command="brigade daily plan --record")
    if action is None:
        return _blocked_run(target=target, receipt=receipt, blockers=["no daily action selected"], json_output=json_output, next_command="brigade daily plan")
    config_blockers = _config_blockers(config, action, approved=approved)
    evidence_blockers = _evidence_blockers(target, action)
    if config_blockers or evidence_blockers:
        return _blocked_run(target=target, receipt=receipt, blockers=[*config_blockers, *evidence_blockers], json_output=json_output)
    if action.get("approval_required") and not approved:
        return _blocked_run(target=target, receipt=receipt, blockers=[str(action.get("approval_reason") or "explicit approval required")], json_output=json_output)
    if action.get("context_kind") and config.get("allow_context_pack_build", True):
        context_pack_id, context_commands = _invoke_context_build(target, action)
    else:
        context_pack_id, context_commands = None, []
    receipt["context_pack_id"] = context_pack_id
    receipt["commands_invoked"].extend(context_commands)
    if context_pack_id:
        receipt["receipts_created"].append(str(context_cmd._packs_root(target) / context_pack_id / "context.json"))
    rc = 0
    action_type = str(action.get("action_type"))
    if action_type == "run-task":
        task_id = str((action.get("metadata") or {}).get("task_id") or "")
        with redirect_stdout(StringIO()):
            rc = work_cmd.run(None, target=target, task_id=task_id or None, inspect=False)
        receipt["task_id"] = task_id or None
        receipt["commands_invoked"].append({"command": "brigade work run", "exit_code": rc})
        active = work_cmd._active_session_info(target)
        receipt["work_session_id"] = active.get("id") if isinstance(active, dict) else None
    elif action_type == "promote-import":
        import_id = str((action.get("metadata") or {}).get("import_id") or action.get("source_local_id"))
        with redirect_stdout(StringIO()):
            rc = work_cmd.import_promote(target=target, import_id=import_id)
        receipt["commands_invoked"].append({"command": f"brigade work import promote {import_id}", "exit_code": rc})
    elif action_type == "start-center-action":
        action_id = str((action.get("metadata") or {}).get("action_id") or action.get("source_local_id"))
        with redirect_stdout(StringIO()):
            rc = center_cmd.actions_start(target=target, action_id=action_id)
        receipt["commands_invoked"].append({"command": f"brigade center actions start {action_id}", "exit_code": rc})
    elif action_type == "import-readiness-issues":
        with redirect_stdout(StringIO()):
            rc = center_cmd.readiness_import_issues(target=target)
        receipt["commands_invoked"].append({"command": "brigade center readiness import-issues", "exit_code": rc})
    elif action_type == "build-operator-report":
        with redirect_stdout(StringIO()):
            rc = center_cmd.report_build(target=target)
        receipt["commands_invoked"].append({"command": "brigade center report build", "exit_code": rc})
    else:
        receipt["blockers"].append(f"selected action is review-only: {action_type}")
        rc = 1
    receipt["status"] = "completed" if rc == 0 else "failed"
    receipt["completed_at"] = _now().isoformat()
    receipt["next_recommended_command"] = "brigade daily closeout"
    _record_run(target, receipt)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"daily run: {run_id}")
        print(f"status: {receipt['status']}")
        print(f"selected: {receipt['selected_action_id']}")
        print(f"next: {receipt['next_recommended_command']}")
    return rc


def init(*, target: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = _config_path(target)
    if path.exists() and not force:
        payload = {"schema_version": SCHEMA_VERSION, "target": str(target), "path": str(path), "written": False, "reason": "already exists"}
    else:
        _write_config(path, DEFAULT_CONFIG)
        payload = {"schema_version": SCHEMA_VERSION, "target": str(target), "path": str(path), "written": True, "config": DEFAULT_CONFIG}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily config: {path}")
        print(f"written: {payload['written']}")
    return 0


def schema(*, target: Path, json_output: bool = False) -> int:
    payload = {"schema_version": SCHEMA_VERSION, "target": str(target.expanduser().resolve()), **_schemas()}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily schema: {payload['target']}")
        for item in payload["schemas"]:
            print(f"- {item['name']}")
    return 0


def history_payload(target: Path, *, limit: int = 20) -> dict[str, Any]:
    target = target.expanduser().resolve()
    runs, run_errors = _iter_receipts(_runs_root(target), "run.json")
    plans, plan_errors = _iter_receipts(_plans_root(target), "plan.json")
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-history", "version": SCHEMA_VERSION},
        "target": str(target),
        "runs": runs[:limit],
        "plans": plans[:limit],
        "run_count": len(runs),
        "plan_count": len(plans),
        "parse_errors": [*run_errors, *plan_errors],
    }


def history(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    payload = history_payload(target, limit=limit)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily history: {payload['target']}")
        print(f"runs: {payload['run_count']}")
        for item in payload["runs"]:
            print(f"- {item.get('run_id')} [{item.get('status')}] {item.get('started_at')}")
        print(f"plans: {payload['plan_count']}")
    return 0


def show(*, target: Path, run_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if run_id == "latest":
        payload = _latest_run(target)
    else:
        payload = _read_json(_runs_root(target) / run_id / "run.json")
    if payload is None:
        print(f"error: daily run not found: {run_id}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily run: {payload.get('run_id')}")
        print(f"status: {payload.get('status')}")
        print(f"selected: {payload.get('selected_action_id')}")
        print(f"next: {payload.get('next_recommended_command')}")
    return 0


def health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config, config_checks = _load_config(target)
    checks = list(config_checks)
    runs, run_errors = _iter_receipts(_runs_root(target), "run.json")
    plans, plan_errors = _iter_receipts(_plans_root(target), "plan.json")
    for error in [*run_errors, *plan_errors]:
        checks.append({"status": "fail", "name": "daily_receipt_parse", "detail": f"{error['path']}: {error['error']}"})
    plan_hours = int(config.get("stale_plan_threshold_hours") or 12)
    run_hours = int(config.get("stale_run_threshold_hours") or 12)
    latest_plan = plans[0] if plans else None
    latest_run = runs[0] if runs else None
    if latest_plan and _is_stale(latest_plan.get("created_at"), plan_hours):
        checks.append({"status": "warn", "name": "daily_stale_plan", "detail": str(latest_plan.get("plan_id"))})
    if latest_run:
        if latest_run.get("status") in {"running", "planned"} and _is_stale(latest_run.get("started_at"), run_hours):
            checks.append({"status": "warn", "name": "daily_stale_run", "detail": str(latest_run.get("run_id"))})
        if latest_run.get("status") == "blocked":
            checks.append({"status": "warn", "name": "daily_blocked_run", "detail": str(latest_run.get("run_id"))})
        if latest_run.get("status") in {"completed", "failed", "blocked"} and not latest_run.get("closeout_status") and _is_stale(latest_run.get("completed_at") or latest_run.get("started_at"), run_hours):
            checks.append({"status": "warn", "name": "daily_unclosed_run", "detail": str(latest_run.get("run_id"))})
    for run_receipt in runs[:10]:
        action = run_receipt.get("selected_action") if isinstance(run_receipt.get("selected_action"), dict) else None
        for blocker in _evidence_blockers(target, action):
            checks.append({"status": "warn", "name": "daily_missing_evidence", "detail": blocker})
            break
    active_checks = [check for check in checks if check.get("status") != "ok"]
    top_issue = active_checks[0] if active_checks else None
    return {
        "schema_version": SCHEMA_VERSION,
        "config_path": str(_config_path(target)),
        "run_count": len(runs),
        "plan_count": len(plans),
        "latest_run": latest_run,
        "latest_plan": latest_plan,
        "checks": checks,
        "issue_count": len(active_checks),
        "top_issue": top_issue,
    }


def doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-doctor", "version": SCHEMA_VERSION},
        "target": str(target),
        "health": health(target),
    }
    payload["checks"] = payload["health"]["checks"]
    payload["issue_count"] = payload["health"]["issue_count"]
    payload["top_issue"] = payload["health"]["top_issue"]
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily doctor: {target}")
        for check in payload["checks"]:
            print(f"[{check.get('status')}] {check.get('name')}: {check.get('detail')}")
    return 1 if any(check.get("status") == "fail" for check in payload["checks"]) else 0


def _write_handoff(target: Path, run_receipt: dict[str, Any], status: str, reason: str | None) -> Path:
    inbox = target / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y-%m-%d-%H%M")
    path = inbox / f"{stamp}-brigade-daily-closeout.md"
    content = f"""# Memory Handoff

## Type
workflow

## Title
Brigade daily loop closeout

## Summary
Brigade daily closeout recorded status `{status}` for daily run `{run_receipt.get('run_id')}`. The receipt preserves the selected action, invoked local commands, blockers, and next recommendation for future operator review.

## Durable facts
- Daily run id: `{run_receipt.get('run_id')}`
- Selected action: `{run_receipt.get('selected_action_id')}`
- Closeout status: `{status}`
- Reason: `{_safe_text(target, reason or 'not provided')}`

## Evidence
- daily receipt: `{run_receipt.get('path')}`
- next command: `{run_receipt.get('next_recommended_command')}`

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Brigade daily loop closeout

Daily run `{run_receipt.get('run_id')}` closed with status `{status}`. Review the local daily receipt for selected action, commands invoked, blockers, and next recommendation.
"""
    path.write_text(content)
    lint = handoff_cmd.lint_file(path)
    if not lint.valid:
        raise RuntimeError("; ".join(lint.errors))
    return path


def closeout(
    *,
    target: Path,
    status: str = "reviewed",
    reason: str | None = None,
    handoff: bool = False,
    json_output: bool = False,
) -> int:
    if status not in RUN_STATUSES:
        print(f"error: invalid daily closeout status: {status}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    receipt = _latest_run(target)
    if receipt is None:
        print("error: no daily run receipt found", file=sys.stderr)
        return 1
    receipt["closeout_status"] = status
    receipt["closeout_reason"] = reason
    receipt["reviewed_at"] = _now().isoformat()
    receipt["latest_work_closeout"] = work_cmd._latest_work_closeout_payload(target)
    receipt["latest_verification"] = work_cmd._latest_verify_receipt(target)
    receipt["handoff_drafts"] = center_cmd.status_payload(target).get("handoff_drafts")
    receipt["center_report"] = center_cmd.latest_report(target)
    receipt["center_readiness"] = center_cmd._latest_readiness(target)
    if handoff:
        try:
            handoff_path = _write_handoff(target, receipt, status, reason)
        except RuntimeError as exc:
            print(f"error: handoff lint failed: {exc}", file=sys.stderr)
            return 1
        receipt["handoff_path"] = str(handoff_path)
    _record_run(target, receipt)
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    print(f"daily closeout: {receipt.get('run_id')}")
    print(f"status: {status}")
    print(f"run_status: {receipt.get('status')}")
    if receipt.get("handoff_path"):
        print(f"handoff: {receipt['handoff_path']}")
    return 0
