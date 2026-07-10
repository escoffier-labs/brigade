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
                source_fingerprint=_fingerprint(
                    {"task_id": task_id, "text": task.get("text"), "acceptance": acceptance}
                ),
                context_kind="task",
                metadata={"task_id": task_id},
            )
        )
    return candidates


def _pending_import_candidates(target: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    quality = work_cmd._inbox_quality_payload(target)
    quality_by_id = {
        str(item.get("import_id")): item for item in quality.get("scored_imports", []) if isinstance(item, dict)
    }
    all_imports = work_cmd._read_imports(target)
    dismissed_by_source = Counter(
        str(item.get("source") or "unknown") for item in all_imports if item.get("status") == "dismissed"
    )
    promoted_by_source = Counter(
        str(item.get("source") or "unknown") for item in all_imports if item.get("status") == "promoted"
    )
    for item in work_cmd._pending_imports(target):
        import_id = str(item.get("id") or "")
        source = str(item.get("source") or "unknown")
        acceptance = (
            [str(value) for value in item.get("acceptance", [])] if isinstance(item.get("acceptance"), list) else []
        )
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        has_provenance = bool(
            metadata.get("source_fingerprint") or metadata.get("scanner_run_id") or item.get("source")
        )
        noisy_source = dismissed_by_source[source] >= max(3, promoted_by_source[source] * 3)
        deferred = bool(metadata.get("deferred") or metadata.get("deferred_at") or item.get("deferred_at"))
        stale = _is_stale(item.get("created_at"), 72)
        quality_item = quality_by_id.get(import_id, {})
        quality_score = int(quality_item.get("quality_score") or 0) if isinstance(quality_item, dict) else 0
        score = 220 + _priority_score(item.get("priority")) + quality_score
        if noisy_source:
            score -= 50
        if deferred:
            score -= 80
        if stale:
            score -= 20
        ranking_reasons = [
            "pending import",
            "has acceptance criteria" if acceptance else "missing acceptance criteria",
            "complete provenance" if has_provenance else "missing provenance",
        ]
        if noisy_source:
            ranking_reasons.append("noisy source")
        if deferred:
            ranking_reasons.append("deferred import")
        if stale:
            ranking_reasons.append("stale import")
        ranking_reasons.append(f"quality={quality_score}")
        candidates.append(
            _candidate(
                target=target,
                action_type="promote-import",
                source_subsystem="work-import",
                source_local_id=import_id,
                safe_summary=str(item.get("text") or "pending import"),
                suggested_next_command=f"brigade work import promote {import_id}",
                score=score,
                ranking_reasons=ranking_reasons,
                approval_required=True,
                approval_reason="promotion changes the local task ledger",
                risk_level="medium",
                acceptance=acceptance,
                evidence_refs=[str(work_cmd._imports_path(target))],
                source_fingerprint=str(metadata.get("source_fingerprint") or _fingerprint(item)),
                context_kind="task" if item.get("kind", "task") == "task" else None,
                metadata={"import_id": import_id, "kind": item.get("kind", "task"), "inbox_quality": quality_item},
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
    readiness = _daily_readiness_payload(target)
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


def _daily_readiness_finding(
    subsystem: str, name: str, severity: str, summary: str, command: str, *, status: str = "warn"
) -> dict[str, Any]:
    fingerprint = _fingerprint({"subsystem": subsystem, "name": name, "severity": severity, "summary": summary})
    return {
        "finding_id": f"daily-readiness-{fingerprint}",
        "subsystem": subsystem,
        "name": name,
        "status": status,
        "severity": severity,
        "safe_summary": summary,
        "suggested_next_command": command,
        "source_fingerprint": fingerprint,
    }


def _daily_readiness_payload(target: Path) -> dict[str, Any]:
    from .. import release_cmd

    status_data = _daily_center_status_payload(target)
    findings: list[dict[str, Any]] = []
    pending_tasks = int(status_data.get("pending_task_count") or 0)
    pending_imports = int(status_data.get("pending_import_count") or 0)
    review_count = int(status_data.get("review_queue_count") or 0)
    if pending_tasks:
        findings.append(
            _daily_readiness_finding(
                "work", "pending_tasks", "warning", f"{pending_tasks} pending task(s)", "brigade work tasks"
            )
        )
    if pending_imports:
        findings.append(
            _daily_readiness_finding(
                "work",
                "pending_imports",
                "blocker",
                f"{pending_imports} pending import(s)",
                "brigade work inbox",
                status="blocked",
            )
        )
    if review_count:
        findings.append(
            _daily_readiness_finding(
                "center",
                "pending_reviews",
                "warning",
                f"{review_count} pending review item(s)",
                "brigade center reviews",
            )
        )
    release = release_cmd._latest_release_receipt(target)
    if not release:
        findings.append(
            _daily_readiness_finding(
                "release",
                "missing_release_readiness",
                "blocker",
                "release readiness receipt is missing",
                "brigade release run",
                status="blocked",
            )
        )
    elif release.get("ready") is False or release.get("status") in {"blocked", "failed"}:
        run_id = str(release.get("run_id") or "latest")
        findings.append(
            _daily_readiness_finding(
                "release",
                "blocked_release_readiness",
                "blocker",
                "release readiness is blocked",
                f"brigade release show {run_id}",
                status="blocked",
            )
        )
    for subsystem, command in (
        ("memory_care", "brigade memory care doctor"),
        ("security", "brigade security doctor"),
        ("notifications", "brigade notifications setup plan"),
        ("action_queue", "brigade center actions doctor"),
    ):
        health = status_data.get(subsystem)
        if not isinstance(health, dict):
            continue
        issue_count = int(health.get("issue_count") or health.get("open_count") or 0)
        top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else None
        if issue_count <= 0 and not top:
            continue
        detail = str(
            (top or {}).get("detail")
            or (top or {}).get("safe_summary")
            or f"{subsystem} has unresolved health issue(s)"
        )
        findings.append(
            _daily_readiness_finding(
                subsystem, str((top or {}).get("name") or "health"), "warning", _safe_text(target, detail), command
            )
        )
    blockers = [finding for finding in findings if finding.get("severity") == "blocker"]
    warnings = [finding for finding in findings if finding.get("severity") != "blocker"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-readiness", "version": SCHEMA_VERSION},
        "target": str(target),
        "created_at": _now().isoformat(),
        "ready": not blockers,
        "status": "ready" if not blockers else "blocked",
        "finding_count": len(findings),
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "waived_count": 0,
        "findings": findings,
        "blockers": blockers,
        "warnings": warnings,
        "waivers": [],
        "source_fingerprint": _fingerprint({"findings": findings}),
    }


def _handoff_ingest_candidates(target: Path) -> list[dict[str, Any]]:
    found = handoff_cmd.collect_issues(target)
    if not found:
        return []
    counts = Counter(issue.category for issue in found)
    category = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    top = next((issue for issue in found if issue.category == category), found[0])
    return [
        _candidate(
            target=target,
            action_type="import-handoff-issues",
            source_subsystem="handoff-ingest",
            source_local_id=category,
            safe_summary=f"{len(found)} handoff ingest issue(s); top category {category}: {top.text}",
            suggested_next_command="brigade handoff import-issues --target .",
            score=205,
            ranking_reasons=["handoff ingest issue", "route through work inbox before touching canonical memory"],
            approval_required=False,
            risk_level="low",
            acceptance=[
                "Handoff ingest issues are imported into the work inbox or explicitly deferred.",
                "OpenClaw remains the canonical memory owner; no canonical memory files are edited by this daily action.",
            ],
            evidence_refs=[str(handoff_cmd.default_sources_path(target)), ".brigade/handoffs/ingest-runs"],
            source_fingerprint=_fingerprint({"categories": dict(counts), "top_issue": top.as_dict()}),
            metadata={"issue_count": len(found), "by_category": dict(counts), "top_issue_id": top.id},
        )
    ]


def _health_issue_candidates(target: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    health_sources = [
        ("handoff", handoff_cmd.draft_queue_payload(target), "brigade handoff doctor", 150),
        ("memory-care", memory_cmd.health(target), "brigade memory care doctor", 140),
        ("security", _daily_security_health(target), "brigade security doctor", 135),
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


def _notification_candidates(target: Path) -> list[dict[str, Any]]:
    health = notifications_cmd.health(target)
    if int(health.get("issue_count") or 0) <= 0:
        return []
    top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else {}
    command = str(health.get("suggested_next_command") or "brigade notifications setup plan")
    if command.startswith("agent-notify "):
        command = "brigade notifications setup plan"
    return [
        _candidate(
            target=target,
            action_type="review-notification-health",
            source_subsystem="notifications",
            source_local_id=str(top.get("name") or "agent-notify"),
            safe_summary=str(top.get("detail") or "operator notifications need review"),
            suggested_next_command=command,
            score=40,
            ranking_reasons=["operator notification health issue", "review setup before enabling outbound messages"],
            approval_required=False,
            risk_level="low",
            evidence_refs=["~/.config/agent-notify/config.toml", "agent-notify environment variable names"],
            source_fingerprint=_fingerprint(
                {"subsystem": "notifications", "status": health.get("status"), "issue": top}
            ),
            metadata={"notification_health": health},
        )
    ]


def _report_candidate(target: Path) -> list[dict[str, Any]]:
    health = center_cmd.report_health(target)
    top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else None
    if not top:
        return []
    suggested_command = str(top.get("suggested_next_command") or "brigade center report build")
    needs_build = suggested_command == "brigade center report build"
    return [
        _candidate(
            target=target,
            action_type="build-operator-report" if needs_build else "review-operator-report",
            source_subsystem="center-report",
            source_local_id=str(top.get("name") or "report"),
            safe_summary=str(top.get("detail") or "operator report needs refresh"),
            suggested_next_command=suggested_command,
            score=120,
            ranking_reasons=[
                "operator report health issue",
                "local report build is safe" if needs_build else "review current report before more report builds",
            ],
            approval_required=False,
            risk_level="low",
            evidence_refs=[".brigade/center/reports"],
            source_fingerprint=_fingerprint(top),
        )
    ]


def _phase_ledger_action_candidates(target: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for action in phases_cmd._read_actions(target):
        if action.get("status") not in {"pending", "active"}:
            continue
        action_id = str(action.get("action_id") or "")
        issue_type = str(action.get("issue_type") or "")
        blocking_issue = any(
            token in issue_type
            for token in ("missing", "pushed", "committed", "blocked", "stale_unreviewed", "complete_without")
        )
        score = 185 if blocking_issue else 95
        candidates.append(
            _candidate(
                target=target,
                action_type="start-phase-action" if action.get("status") == "pending" else "review-phase-action",
                source_subsystem="phase-ledger-action",
                source_local_id=action_id,
                safe_summary=str(action.get("safe_summary") or "phase ledger action"),
                suggested_next_command=f"brigade work phases actions show {action_id}",
                score=score,
                ranking_reasons=[
                    "phase ledger action",
                    "blocks AFK or release completion" if blocking_issue else "phase ledger follow-up",
                    f"status={action.get('status')}",
                ],
                approval_required=False,
                risk_level="low",
                evidence_refs=[str(phases_cmd._actions_root(target))],
                source_fingerprint=str(action.get("source_fingerprint") or _fingerprint(action)),
                metadata={"action_id": action_id, "phase_id": action.get("phase_id"), "issue_type": issue_type},
            )
        )
    return candidates


def _phase_ledger_issue_candidates(target: Path) -> list[dict[str, Any]]:
    health = phases_cmd.health(target)
    if _phase_ledger_issues_captured_by_report(health):
        return []
    top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else None
    if not top:
        return []
    issue_type = str(top.get("name") or "phase-ledger-issue")
    blocking_issue = any(
        token in issue_type
        for token in ("missing", "pushed", "committed", "blocked", "stale_unreviewed", "complete_without")
    )
    score = 170 if blocking_issue else 80
    return [
        _candidate(
            target=target,
            action_type="build-phase-report",
            source_subsystem="phase-ledger",
            source_local_id=issue_type,
            safe_summary=str(top.get("detail") or "phase ledger issue"),
            suggested_next_command="brigade work phases report build",
            score=score,
            ranking_reasons=[
                "unresolved phase ledger issue",
                "blocks AFK or release completion" if blocking_issue else "review after higher priority daily work",
            ],
            approval_required=False,
            risk_level="low",
            evidence_refs=[str(phases_cmd._records_root(target))],
            source_fingerprint=_fingerprint(top),
            metadata={"issue_type": issue_type, "phase_id": top.get("phase_id")},
        )
    ]


def _phase_ledger_issues_captured_by_report(phase_health: dict[str, Any]) -> bool:
    issue_count = int(phase_health.get("issue_count") or 0)
    if issue_count <= 0:
        return False
    latest_report = phase_health.get("latest_report") if isinstance(phase_health.get("latest_report"), dict) else {}
    latest_compare = (
        phase_health.get("latest_report_compare") if isinstance(phase_health.get("latest_report_compare"), dict) else {}
    )
    report_issue_count = latest_report.get("issue_count")
    compare_issue_count = latest_compare.get("issue_count")
    try:
        return int(report_issue_count) == issue_count and int(compare_issue_count) == 0
    except (TypeError, ValueError):
        return False


def _phase_session_candidates(target: Path) -> list[dict[str, Any]]:
    session = phases_cmd._latest_session(target)
    if not isinstance(session, dict) or session.get("status") in {"closed", "archived"}:
        return []
    try:
        next_payload = phases_cmd._session_next_payload(target, session)
    except ValueError:
        return []
    step = next_payload.get("next_step") if isinstance(next_payload.get("next_step"), dict) else {}
    step_type = str(step.get("step_type") or "session")
    if step_type == "session_reviewed":
        return []
    session_id = str(session.get("session_id") or "latest")
    candidates: list[dict[str, Any]] = []
    checkpoint = next_payload.get("checkpoint") if isinstance(next_payload.get("checkpoint"), dict) else None
    if checkpoint and int(checkpoint.get("issue_count") or 0) > 0:
        latest = checkpoint.get("latest_checkpoint") if isinstance(checkpoint.get("latest_checkpoint"), dict) else {}
        checkpoint_id = str(latest.get("checkpoint_id") or "latest")
        top_issue = checkpoint.get("top_issue") if isinstance(checkpoint.get("top_issue"), dict) else {}
        candidates.append(
            _candidate(
                target=target,
                action_type="import-phase-checkpoint-issues",
                source_subsystem="phase-session-checkpoint",
                source_local_id=checkpoint_id,
                safe_summary=str(
                    (top_issue or {}).get("detail") or latest.get("summary") or "phase session checkpoint needs review"
                ),
                suggested_next_command=str(
                    checkpoint.get("suggested_next_command")
                    or f"brigade work phases session checkpoints import-issues {checkpoint_id}"
                ),
                score=190,
                ranking_reasons=[
                    "phase session checkpoint issue",
                    f"issue_count={checkpoint.get('issue_count')}",
                    "route through work inbox before continuing AFK session",
                ],
                approval_required=False,
                risk_level="low",
                evidence_refs=[str(latest.get("path") or phases_cmd._session_checkpoints_root(target))],
                source_fingerprint=str(latest.get("source_fingerprint") or _fingerprint(checkpoint)),
                metadata={
                    "checkpoint_id": checkpoint_id,
                    "session_id": session_id,
                    "issue_count": checkpoint.get("issue_count"),
                    "top_issue": top_issue,
                },
            )
        )
    elif step_type not in {"session_closeout_needed", "session_reviewed"}:
        candidates.append(
            _candidate(
                target=target,
                action_type="write-phase-session-checkpoint",
                source_subsystem="phase-session",
                source_local_id=session_id,
                safe_summary=str(step.get("detail") or "phase execution session needs a local checkpoint"),
                suggested_next_command=f"brigade work phases session checkpoint {session_id}",
                score=270,
                ranking_reasons=[
                    "active phase execution session",
                    f"next_step={step_type}",
                    "write a local checkpoint before continuing AFK work",
                ],
                approval_required=False,
                risk_level="low",
                evidence_refs=[str(phases_cmd._sessions_root(target))],
                source_fingerprint=_fingerprint({"session_id": session_id, "next_step": step, "checkpoint": "write"}),
                metadata={"session_id": session_id, "step_type": step_type},
            )
        )
    action_type = "closeout-phase-session" if step_type == "session_closeout_needed" else "build-phase-session-report"
    score = (
        260
        if step_type
        in {"missing_record", "pending_phase", "blocked_phase", "stale_in_progress_phase", "session_closeout_needed"}
        else 125
    )
    candidates.append(
        _candidate(
            target=target,
            action_type=action_type,
            source_subsystem="phase-session",
            source_local_id=session_id,
            safe_summary=str(step.get("detail") or "phase execution session needs review"),
            suggested_next_command=str(
                next_payload.get("suggested_next_command") or "brigade work phases session next latest"
            ),
            score=score,
            ranking_reasons=[
                "active phase execution session",
                f"next_step={step_type}",
                "blocks AFK completion" if score >= 180 else "session follow-up",
            ],
            approval_required=False,
            risk_level="low",
            evidence_refs=[str(phases_cmd._sessions_root(target))],
            source_fingerprint=_fingerprint({"session_id": session_id, "next_step": step}),
            metadata={"session_id": session_id, "step_type": step_type},
        )
    )
    return candidates


def _all_candidates(target: Path, diagnostics: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    config, _ = _load_config(target)
    candidates: list[dict[str, Any]] = []
    sections = [
        ("pending-tasks", _pending_task_candidates),
        ("pending-imports", _pending_import_candidates),
        ("center-actions", _center_action_candidates),
        ("readiness", _readiness_candidates),
        ("handoff-ingest", _handoff_ingest_candidates),
        ("phase-session", _phase_session_candidates),
        ("phase-ledger-actions", _phase_ledger_action_candidates),
        ("phase-ledger-issues", _phase_ledger_issue_candidates),
        ("health-issues", _health_issue_candidates),
        ("notifications", _notification_candidates),
        ("operator-report", _report_candidate),
    ]
    for label, builder in sections:
        result, check = _bounded_status_call(label, lambda builder=builder: builder(target), [])
        if diagnostics is not None:
            diagnostics.append(check)
        if isinstance(result, list):
            candidates.extend(item for item in result if isinstance(item, dict))
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
        if _remote_mutation_reason(item.get("suggested_next_command")):
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
        "import-handoff-issues": "brigade handoff import-issues",
        "build-operator-report": "brigade center report build",
        "start-phase-action": "brigade work phases actions start",
        "build-phase-report": "brigade work phases report build",
        "write-phase-session-checkpoint": "brigade work phases session checkpoint",
        "build-phase-session-report": "brigade work phases session report build",
        "closeout-phase-session": "brigade work phases session closeout",
        "review-center-action": "review-only",
        "review-phase-action": "review-only",
        "review-operator-report": "review-only",
        "review-health-issue": "review-only",
        "review-notification-health": "review-only",
    }.get(str(action.get("action_type")), "unsupported")


def _remote_mutation_reason(command: object) -> str | None:
    text = str(command or "")
    if re.search(
        r"\b(git\s+push|git\s+tag|gh\s+release|release\s+create|repo\s+transfer|git\s+pull|git\s+merge)\b",
        text,
        re.IGNORECASE,
    ):
        return "remote-mutating command is not eligible for daily run"
    return None


def _candidate_blockers(target: Path, config: dict[str, Any], action: dict[str, Any] | None) -> dict[str, list[str]]:
    if action is None:
        return {
            "safety_blockers": ["no selected action"],
            "approval_blockers": [],
            "stale_evidence_blockers": [],
            "quality_blockers": [],
            "config_blockers": [],
        }
    safety: list[str] = []
    remote = _remote_mutation_reason(action.get("suggested_next_command"))
    if remote:
        safety.append(remote)
    config_blockers = _config_blockers(config, action)
    evidence_blockers = _evidence_blockers(target, action)
    quality: list[str] = []
    if not action.get("acceptance"):
        quality.append("missing acceptance criteria")
    if "missing provenance" in action.get("ranking_reasons", []):
        quality.append("missing provenance")
    if "noisy source" in action.get("ranking_reasons", []):
        quality.append("noisy source")
    if "deferred import" in action.get("ranking_reasons", []):
        quality.append("deferred")
    approval = []
    if action.get("approval_required"):
        approval.append(str(action.get("approval_reason") or "explicit approval required"))
    return {
        "safety_blockers": safety,
        "approval_blockers": approval,
        "stale_evidence_blockers": evidence_blockers,
        "quality_blockers": quality,
        "config_blockers": config_blockers,
    }


def _candidate_explanation(
    target: Path, config: dict[str, Any], action: dict[str, Any], selected_id: str | None
) -> dict[str, Any]:
    blockers = _candidate_blockers(target, config, action)
    rejection_reasons: list[str] = []
    if action.get("action_id") != selected_id:
        rejection_reasons.extend(blockers["safety_blockers"])
        if not rejection_reasons and blockers["config_blockers"]:
            rejection_reasons.extend(blockers["config_blockers"])
        if not rejection_reasons and blockers["stale_evidence_blockers"]:
            rejection_reasons.extend(blockers["stale_evidence_blockers"])
        if not rejection_reasons:
            rejection_reasons.append("lower ranked than selected action")
    return {
        "action_id": action.get("action_id"),
        "selected": action.get("action_id") == selected_id,
        "score": action.get("score"),
        "scoring_reasons": action.get("ranking_reasons") or [],
        "rejection_reasons": rejection_reasons,
        **blockers,
    }


def _adapter_result(action: dict[str, Any] | None, *, status: str = "planned") -> dict[str, Any]:
    return {
        "adapter_id": _adapter_for(action),
        "action_type": action.get("action_type") if isinstance(action, dict) else None,
        "source_subsystem": action.get("source_subsystem") if isinstance(action, dict) else None,
        "source_local_id": action.get("source_local_id") if isinstance(action, dict) else None,
        "status": status,
        "commands_invoked": [],
        "receipts_created": [],
        "blockers": [],
        "warnings": [],
        "next_recommended_command": action.get("suggested_next_command") if isinstance(action, dict) else None,
        "evidence_references": action.get("evidence_refs") if isinstance(action, dict) else [],
    }
