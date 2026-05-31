"""Agent-facing daily driver over local Brigade operator state."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from contextlib import redirect_stdout
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import center_cmd, context_cmd, handoff_cmd, memory_cmd, security_cmd, tools_cmd, work_cmd

SCHEMA_VERSION = 1
RUN_STATUSES = {"reviewed", "deferred", "blocked", "archived"}
APPROVAL_STATUSES = {"pending", "approved", "rejected", "held", "consumed"}
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
    "verification_required_for_work_run": False,
    "verification_required_for_import_promotion": False,
    "verification_required_for_release_actions": False,
    "allowed_verification_commands": "",
    "verification_timeout": 600,
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


def _approvals_root(target: Path) -> Path:
    return _daily_root(target) / "approvals"


def _approvals_archive_root(target: Path) -> Path:
    return _daily_root(target) / "approval-archive"


def _repairs_root(target: Path) -> Path:
    return _daily_root(target) / "repairs"


def _unblocks_root(target: Path) -> Path:
    return _daily_root(target) / "unblocks"


def _telemetry_root(target: Path) -> Path:
    return _daily_root(target) / "telemetry"


def _hardening_root(target: Path) -> Path:
    return _daily_root(target) / "hardening"


def _hardening_closeouts_root(target: Path) -> Path:
    return _hardening_root(target) / "closeouts"


HARDENING_WORKSTREAMS: list[dict[str, Any]] = [
    {
        "id": "daily-production-hardening",
        "phase_start": 115,
        "phase_end": 124,
        "focus": "make the daily loop recoverable, explainable, and consistently receipt-backed",
        "checks": ["daily config", "adapter receipts", "plan explanations", "approval hygiene", "telemetry health"],
    },
    {
        "id": "operator-center-contract-cleanup",
        "phase_start": 125,
        "phase_end": 134,
        "focus": "normalize center status, activity, reviews, templates, and schema contracts",
        "checks": ["center schema", "review item shape", "receipt references", "suggested commands"],
    },
    {
        "id": "inbox-evidence-quality",
        "phase_start": 135,
        "phase_end": 144,
        "focus": "reduce inbox noise and improve provenance, acceptance, and evidence quality",
        "checks": ["pending import acceptance", "source provenance", "stale imports", "noisy sources"],
    },
    {
        "id": "repo-fleet-daily-use",
        "phase_start": 145,
        "phase_end": 154,
        "focus": "keep fleet reports, actions, dispatch, and release trains visible in daily planning",
        "checks": ["repo fleet health", "fleet actions", "fleet sweeps", "release trains"],
    },
    {
        "id": "self-dogfood-release-loop",
        "phase_start": 155,
        "phase_end": 164,
        "focus": "make Brigade's own release path readable through daily receipts and release evidence",
        "checks": ["release readiness", "release candidate", "verification", "daily evidence in release"],
    },
]


HARDENING_PHASE_TITLES: dict[int, str] = {
    115: "audit daily config and unsafe local policy states",
    116: "verify daily run receipts have normalized adapter results",
    117: "verify daily plan receipts have candidate explanations",
    118: "track approval hygiene and stale approval requests",
    119: "track telemetry warnings and repeated blockers",
    120: "route unresolved daily reliability findings into the work inbox",
    121: "close out reviewed daily reliability findings",
    122: "keep daily protocol output stable for wrappers",
    123: "keep JSON output clean when wrapped commands print noise",
    124: "carry daily reliability state into release evidence",
    125: "audit center schema manifest presence",
    126: "audit center review item field coverage",
    127: "verify review items include suggested next commands",
    128: "verify receipt references stay local and safe",
    129: "surface center contract findings in daily hardening audit",
    130: "route center contract findings into the work inbox",
    131: "keep center status readable without subsystem-specific parsing",
    132: "keep center reviews as the unified local review queue",
    133: "document center schema expectations for wrappers",
    134: "carry center contract state into release evidence",
    135: "audit pending imports missing acceptance",
    136: "audit pending imports missing provenance",
    137: "audit inbox hygiene issues",
    138: "penalize noisy and deferred imports in daily planning",
    139: "preserve changed-fingerprint resurfacing",
    140: "route inbox quality findings into the work inbox",
    141: "keep imported findings deduped",
    142: "bias daily action selection toward high-evidence items",
    143: "document inbox quality expectations",
    144: "carry inbox quality state into release evidence",
    145: "audit repo fleet health from the daily hardening layer",
    146: "surface fleet action queue health",
    147: "surface fleet sweep health",
    148: "surface fleet release train health",
    149: "route fleet daily-use findings into the work inbox",
    150: "keep fleet dispatch manual and local",
    151: "keep fleet release plans manual-only",
    152: "keep safe repo labels and receipt labels only",
    153: "document fleet daily-use expectations",
    154: "carry fleet state into release evidence",
    155: "audit latest release readiness receipt",
    156: "audit latest release candidate packet",
    157: "verify release candidate evidence includes daily driver state",
    158: "surface blocked release readiness in daily hardening audit",
    159: "route release dogfood findings into the work inbox",
    160: "keep publish steps manual-only",
    161: "keep daily closeout verification evidence visible",
    162: "document the daily-to-release self-dogfood path",
    163: "keep release schema output stable for wrappers",
    164: "close out the hardening tranche with verification and handoff",
}


IMPLEMENTED_HARDENING_PHASES: set[int] = set(range(115, 145))


def _hardening_phases() -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    for stream in HARDENING_WORKSTREAMS:
        for phase in range(int(stream["phase_start"]), int(stream["phase_end"]) + 1):
            phases.append(
                {
                    "phase": phase,
                    "workstream": stream["id"],
                    "title": HARDENING_PHASE_TITLES.get(phase, f"{stream['focus']} #{phase - int(stream['phase_start']) + 1}"),
                    "status": "implemented" if phase in IMPLEMENTED_HARDENING_PHASES else "planned",
                }
            )
    return phases


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
            {"name": "daily-approval", "top_level_fields": ["approval_id", "status", "selected_action", "selected_adapter", "source_fingerprint"], "item_fields": ["approval_id", "status", "safe_summary", "suggested_next_command"]},
            {"name": "daily-approval-compare", "top_level_fields": ["approval_id", "issues", "ok"], "item_fields": ["name", "status", "detail"]},
            {"name": "daily-approval-archive", "top_level_fields": ["archived", "archived_count"], "item_fields": ["approval_id", "status", "archive_path"]},
            {"name": "daily-resume", "top_level_fields": ["status", "latest_run", "action_taken", "next_recommended_command"], "item_fields": ["name", "detail", "status"]},
            {"name": "daily-repair", "top_level_fields": ["repair_id", "checks", "suggestions", "writes"], "item_fields": ["name", "detail", "status"]},
            {"name": "daily-unblock", "top_level_fields": ["unblock_id", "created_imports", "approval_request", "blockers"], "item_fields": ["id", "source", "kind", "status"]},
            {"name": "daily-protocol", "top_level_fields": ["steps", "commands", "safety_boundaries"], "item_fields": ["step", "command", "purpose"]},
            {"name": "daily-telemetry", "top_level_fields": ["metrics", "issue_count", "top_issue"], "item_fields": ["name", "value", "detail"]},
            {"name": "daily-hardening-plan", "top_level_fields": ["phase_count", "workstreams", "phases"], "item_fields": ["phase", "workstream", "title", "status"]},
            {"name": "daily-hardening-audit", "top_level_fields": ["workstreams", "findings", "issue_count", "top_issue"], "item_fields": ["finding_id", "workstream", "severity", "safe_summary"]},
            {"name": "daily-hardening-import-issues", "top_level_fields": ["created_imports", "skipped_imports", "finding_count"], "item_fields": ["id", "source", "kind", "status"]},
            {"name": "daily-hardening-closeout", "top_level_fields": ["closeout_id", "status", "finding_count", "unresolved_count"], "item_fields": ["finding_id", "severity", "safe_summary"]},
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
        "verification_required_for_work_run",
        "verification_required_for_import_promotion",
        "verification_required_for_release_actions",
    ):
        if not isinstance(config.get(key), bool):
            checks.append({"status": "fail", "name": key, "detail": "expected boolean"})
    for key in ("stale_plan_threshold_hours", "stale_run_threshold_hours", "verification_timeout"):
        value = config.get(key)
        if not isinstance(value, int) or value < 1:
            checks.append({"status": "fail", "name": key, "detail": "expected positive integer"})
    commands = config.get("allowed_verification_commands")
    if not isinstance(commands, str) and not (
        isinstance(commands, list) and all(isinstance(item, str) for item in commands)
    ):
        checks.append({"status": "fail", "name": "allowed_verification_commands", "detail": "expected string or list of strings"})
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


def _slug(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "item").strip().lower()).strip("-")
    return text[:80] or "item"


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
    quality = work_cmd._inbox_quality_payload(target)
    quality_by_id = {
        str(item.get("import_id")): item
        for item in quality.get("scored_imports", [])
        if isinstance(item, dict)
    }
    all_imports = work_cmd._read_imports(target)
    dismissed_by_source = Counter(str(item.get("source") or "unknown") for item in all_imports if item.get("status") == "dismissed")
    promoted_by_source = Counter(str(item.get("source") or "unknown") for item in all_imports if item.get("status") == "promoted")
    for item in work_cmd._pending_imports(target):
        import_id = str(item.get("id") or "")
        source = str(item.get("source") or "unknown")
        acceptance = [str(value) for value in item.get("acceptance", [])] if isinstance(item.get("acceptance"), list) else []
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        has_provenance = bool(metadata.get("source_fingerprint") or metadata.get("scanner_run_id") or item.get("source"))
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
        "build-operator-report": "brigade center report build",
        "review-center-action": "review-only",
        "review-health-issue": "review-only",
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


def _candidate_explanation(target: Path, config: dict[str, Any], action: dict[str, Any], selected_id: str | None) -> dict[str, Any]:
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


def _config_fingerprint(config: dict[str, Any]) -> str:
    stable = {key: config.get(key) for key in sorted(DEFAULT_CONFIG)}
    return _fingerprint(stable)


def _approval_id(action: dict[str, Any], config: dict[str, Any]) -> str:
    source = _slug(action.get("source_subsystem"))
    local = _slug(action.get("source_local_id"))
    digest = _fingerprint(
        {
            "action_id": action.get("action_id"),
            "source_fingerprint": action.get("source_fingerprint"),
            "config_fingerprint": _config_fingerprint(config),
        }
    )[:12]
    return f"approval-{source}-{local}-{digest}"


def _approval_path(target: Path, approval_id: str) -> Path:
    return _approvals_root(target) / approval_id / "approval.json"


def _read_approvals(target: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _iter_receipts(_approvals_root(target), "approval.json")


def _write_approval(target: Path, approval: dict[str, Any]) -> dict[str, Any]:
    approval_id = str(approval["approval_id"])
    approval["path"] = str(_approvals_root(target) / approval_id)
    _write_json(_approval_path(target, approval_id), approval)
    return approval


def _find_approval(target: Path, approval_id: str) -> dict[str, Any] | None:
    return _read_json(_approval_path(target, approval_id))


def _matching_approvals(target: Path, action: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    approvals, _ = _read_approvals(target)
    config_fp = _config_fingerprint(config)
    return [
        approval
        for approval in approvals
        if approval.get("selected_action_id") == action.get("action_id")
        and approval.get("source_fingerprint") == action.get("source_fingerprint")
        and approval.get("config_fingerprint") == config_fp
    ]


def _ensure_approval(target: Path, plan_data: dict[str, Any], action: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    matches = _matching_approvals(target, action, config)
    for status in ("pending", "approved"):
        existing = next((approval for approval in matches if approval.get("status") == status), None)
        if existing:
            return existing
    for status in ("rejected", "held", "consumed"):
        existing = next((approval for approval in matches if approval.get("status") == status), None)
        if existing:
            return existing
    approval = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": _approval_id(action, config),
        "created_at": _now().isoformat(),
        "status": "pending",
        "source_plan_id": plan_data.get("plan_id"),
        "selected_action_id": action.get("action_id"),
        "selected_action": action,
        "selected_adapter": _adapter_for(action),
        "source_subsystem": action.get("source_subsystem"),
        "source_local_id": action.get("source_local_id"),
        "source_fingerprint": action.get("source_fingerprint"),
        "config_fingerprint": _config_fingerprint(config),
        "acceptance": action.get("acceptance") if isinstance(action.get("acceptance"), list) else [],
        "safe_summary": action.get("safe_summary"),
        "evidence_refs": action.get("evidence_refs") if isinstance(action.get("evidence_refs"), list) else [],
        "risk_level": action.get("risk_level"),
        "approval_reason": action.get("approval_reason") or "explicit approval required",
        "config": config,
        "suggested_next_command": action.get("suggested_next_command"),
        "reviewed_at": None,
        "review_reason": None,
        "consumed_run_id": None,
    }
    return _write_approval(target, approval)


def _current_action_for_approval(target: Path, approval: dict[str, Any]) -> dict[str, Any] | None:
    selected_action = approval.get("selected_action") if isinstance(approval.get("selected_action"), dict) else {}
    action_type = str(selected_action.get("action_type") or "")
    source_id = str(approval.get("source_local_id") or selected_action.get("source_local_id") or "")
    candidate_builders = {
        "run-task": _pending_task_candidates,
        "promote-import": _pending_import_candidates,
        "start-center-action": _center_action_candidates,
        "import-readiness-issues": _readiness_candidates,
        "build-operator-report": _report_candidate,
    }
    builder = candidate_builders.get(action_type)
    if builder is None:
        return selected_action if selected_action and not _evidence_blockers(target, selected_action) else None
    for action in builder(target):
        if action.get("source_local_id") == source_id:
            return action
    return None


def _approval_blockers(target: Path, approval: dict[str, Any], config: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    status = str(approval.get("status") or "")
    if status != "approved":
        blockers.append(f"approval status is {status or 'unknown'}")
    if approval.get("consumed_run_id"):
        blockers.append(f"approval already consumed by {approval.get('consumed_run_id')}")
    if approval.get("config_fingerprint") != _config_fingerprint(config):
        blockers.append("daily config changed since approval")
    action = approval.get("selected_action") if isinstance(approval.get("selected_action"), dict) else None
    blockers.extend(_evidence_blockers(target, action))
    current = _current_action_for_approval(target, approval)
    if current is None:
        blockers.append("selected action is no longer available")
    elif current.get("source_fingerprint") != approval.get("source_fingerprint"):
        blockers.append("selected action fingerprint changed since approval")
    return list(dict.fromkeys(blockers))


def _consume_approval(target: Path, approval: dict[str, Any], run_id: str) -> None:
    approval["status"] = "consumed"
    approval["consumed_run_id"] = run_id
    approval["consumed_at"] = _now().isoformat()
    _write_approval(target, approval)


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


def _age_hours(value: object) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return (_now() - parsed).total_seconds() / 3600


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
    daily_health = health(target)
    approvals = daily_health.get("approvals") if isinstance(daily_health.get("approvals"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("daily-status"),
        "target": str(target),
        "config": config,
        "config_checks": config_checks,
        "daily_health": daily_health,
        "top_pending_approval": approvals.get("top_pending"),
        "telemetry": daily_health.get("telemetry"),
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
    selected_id = selected.get("action_id") if selected else None
    candidate_explanations = [_candidate_explanation(target, config, action, selected_id) for action in candidates]
    selection_blockers = _candidate_blockers(target, config, selected) if selected else _candidate_blockers(target, config, None)
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
        "approval_requirement": selected.get("approval_reason") if selected and selected.get("approval_required") else None,
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
    explain = _candidate_explanation(target, config, action, action.get("action_id") if action else None) if action else None
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
        "approval_boundary": action.get("approval_reason") if action and action.get("approval_required") else "no explicit approval required",
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


def _record_telemetry_event(target: Path, event: dict[str, Any]) -> None:
    event_id = str(event.get("event_id") or f"{_now().strftime('%Y%m%d-%H%M%S')}-telemetry-{uuid4().hex[:6]}")
    event["event_id"] = event_id
    event.setdefault("created_at", _now().isoformat())
    _write_json(_telemetry_root(target) / "events" / event_id / "event.json", event)


def _invoke_context_build(target: Path, action: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    if not action.get("context_kind"):
        return None, []
    task_id = (action.get("metadata") or {}).get("task_id")
    before = {str(pack.get("pack_id")) for pack in context_cmd._packs(target)}
    with redirect_stdout(StringIO()):
        rc = context_cmd.build(target=target, kind=str(action.get("context_kind")), task_id=str(task_id) if task_id else None, json_output=False)
    after = context_cmd._packs(target)
    created = next((pack for pack in after if str(pack.get("pack_id")) not in before), None)
    if isinstance(created, dict):
        pack_id = str(created.get("pack_id") or "")
        context_path = context_cmd._packs_root(target) / pack_id / "context.json"
        context_payload = _read_json(context_path)
        if isinstance(context_payload, dict):
            context_payload["daily_action"] = {
                "action_id": action.get("action_id"),
                "safe_summary": action.get("safe_summary"),
                "acceptance": action.get("acceptance") if isinstance(action.get("acceptance"), list) else [],
                "evidence_refs": action.get("evidence_refs") if isinstance(action.get("evidence_refs"), list) else [],
                "approval_required": bool(action.get("approval_required")),
                "approval_reason": action.get("approval_reason"),
            }
            context_payload["daily_recent_failed_runs"] = [
                {"run_id": run.get("run_id"), "status": run.get("status"), "blockers": run.get("blockers", [])}
                for run in (_iter_receipts(_runs_root(target), "run.json")[0])
                if run.get("status") in {"failed", "blocked"}
            ][:5]
            excluded = context_payload.get("excluded_private_evidence") if isinstance(context_payload.get("excluded_private_evidence"), list) else []
            for item in ("raw scanner output", "raw chat text", "private repo names", "owner names", "org names", "hostnames"):
                if item not in excluded:
                    excluded.append(item)
            context_payload["excluded_private_evidence"] = excluded
            _write_json(context_path, context_payload)
    return (str(created.get("pack_id")) if isinstance(created, dict) else None), [{"command": "brigade context build", "exit_code": rc}]


def _blocked_run(
    *,
    target: Path,
    receipt: dict[str, Any],
    blockers: list[str],
    json_output: bool,
    next_command: str = "brigade daily review",
    approval: dict[str, Any] | None = None,
) -> int:
    receipt["status"] = "blocked"
    receipt["completed_at"] = _now().isoformat()
    receipt["next_recommended_command"] = next_command
    receipt["blockers"].extend(blockers)
    adapter = receipt.get("adapter_result") if isinstance(receipt.get("adapter_result"), dict) else _adapter_result(receipt.get("selected_action") if isinstance(receipt.get("selected_action"), dict) else None)
    adapter["status"] = "blocked"
    adapter["blockers"] = list(dict.fromkeys([*(adapter.get("blockers") or []), *blockers]))
    adapter["next_recommended_command"] = receipt["next_recommended_command"]
    receipt["adapter_result"] = adapter
    if approval is not None:
        receipt["approval_id"] = approval.get("approval_id")
        receipt["approval_request"] = approval
        receipt["next_recommended_command"] = f"brigade daily approvals show {approval.get('approval_id')}"
    _record_run(target, receipt)
    _record_telemetry_event(
        target,
        {
            "type": "daily-run",
            "run_id": receipt.get("run_id"),
            "status": "blocked",
            "action_type": (receipt.get("selected_action") or {}).get("action_type") if isinstance(receipt.get("selected_action"), dict) else None,
            "blockers": blockers,
            "approval_id": receipt.get("approval_id"),
        },
    )
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"daily run: {receipt['run_id']}")
        print("status: blocked")
        for blocker in blockers:
            print(f"blocker: {blocker}")
    return 1


def run(
    *,
    target: Path,
    approved: bool = False,
    approval_id: str | None = None,
    plan_id: str | None = None,
    replan: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    config, _ = _load_config(target)
    approval: dict[str, Any] | None = None
    if approval_id:
        approval = _find_approval(target, approval_id)
        if approval is not None and isinstance(approval.get("selected_action"), dict):
            plan_data = {
                "plan_id": approval.get("source_plan_id"),
                "selected_action": approval.get("selected_action"),
                "path": None,
            }
        else:
            plan_data = plan_payload(target, record=True)
            plan_data["approval_load_error"] = f"approval not found: {approval_id}"
    elif plan_id and not replan:
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
        "adapter_result": _adapter_result(action, status="running" if action else "blocked"),
        "work_session_id": None,
        "task_id": None,
        "context_pack_id": None,
        "verification_receipt": None,
        "handoff_path": None,
        "approval_id": approval.get("approval_id") if approval else None,
        "blockers": [],
        "next_recommended_command": "brigade daily status",
        "config": config,
    }
    if plan_data.get("approval_load_error"):
        return _blocked_run(target=target, receipt=receipt, blockers=[str(plan_data["approval_load_error"])], json_output=json_output)
    if plan_data.get("plan_load_error"):
        return _blocked_run(target=target, receipt=receipt, blockers=[str(plan_data["plan_load_error"])], json_output=json_output)
    if plan_id and not replan and _is_stale(plan_data.get("created_at"), int(config.get("stale_plan_threshold_hours") or 12)):
        return _blocked_run(target=target, receipt=receipt, blockers=[f"recorded plan is stale: {plan_data.get('plan_id')}"], json_output=json_output, next_command="brigade daily plan --record")
    if action is None:
        return _blocked_run(target=target, receipt=receipt, blockers=["no daily action selected"], json_output=json_output, next_command="brigade daily plan")
    approval_granted = approved or approval is not None
    config_blockers = _config_blockers(config, action, approved=approval_granted)
    if approval is not None:
        approval_blockers = _approval_blockers(target, approval, config)
        if config_blockers or approval_blockers:
            return _blocked_run(target=target, receipt=receipt, blockers=[*config_blockers, *approval_blockers], json_output=json_output)
    evidence_blockers = _evidence_blockers(target, action)
    if config_blockers or evidence_blockers:
        return _blocked_run(target=target, receipt=receipt, blockers=[*config_blockers, *evidence_blockers], json_output=json_output)
    if action.get("approval_required") and not approval_granted:
        approval = _ensure_approval(target, plan_data, action, config)
        blockers = [str(action.get("approval_reason") or "explicit approval required")]
        if approval.get("status") not in {"pending", "approved"}:
            blockers.append(f"approval status is {approval.get('status')}")
        return _blocked_run(target=target, receipt=receipt, blockers=blockers, json_output=json_output, approval=approval)
    if action.get("context_kind") and config.get("allow_context_pack_build", True):
        context_pack_id, context_commands = _invoke_context_build(target, action)
    else:
        context_pack_id, context_commands = None, []
    receipt["context_pack_id"] = context_pack_id
    receipt["commands_invoked"].extend(context_commands)
    receipt["adapter_result"]["commands_invoked"].extend(context_commands)
    if context_pack_id:
        receipt["receipts_created"].append(str(context_cmd._packs_root(target) / context_pack_id / "context.json"))
        receipt["adapter_result"]["receipts_created"].append(str(context_cmd._packs_root(target) / context_pack_id / "context.json"))
    if approval is not None:
        _consume_approval(target, approval, run_id)
    rc = 0
    action_type = str(action.get("action_type"))
    if action_type == "run-task":
        task_id = str((action.get("metadata") or {}).get("task_id") or "")
        with redirect_stdout(StringIO()):
            rc = work_cmd.run(None, target=target, task_id=task_id or None, inspect=False)
        receipt["task_id"] = task_id or None
        receipt["commands_invoked"].append({"command": "brigade work run", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append({"command": "brigade work run", "exit_code": rc})
        active = work_cmd._active_session_info(target)
        receipt["work_session_id"] = active.get("id") if isinstance(active, dict) else None
    elif action_type == "promote-import":
        import_id = str((action.get("metadata") or {}).get("import_id") or action.get("source_local_id"))
        with redirect_stdout(StringIO()):
            rc = work_cmd.import_promote(target=target, import_id=import_id)
        receipt["commands_invoked"].append({"command": f"brigade work import promote {import_id}", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append({"command": f"brigade work import promote {import_id}", "exit_code": rc})
    elif action_type == "start-center-action":
        action_id = str((action.get("metadata") or {}).get("action_id") or action.get("source_local_id"))
        with redirect_stdout(StringIO()):
            rc = center_cmd.actions_start(target=target, action_id=action_id)
        receipt["commands_invoked"].append({"command": f"brigade center actions start {action_id}", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append({"command": f"brigade center actions start {action_id}", "exit_code": rc})
    elif action_type == "import-readiness-issues":
        with redirect_stdout(StringIO()):
            rc = center_cmd.readiness_import_issues(target=target)
        receipt["commands_invoked"].append({"command": "brigade center readiness import-issues", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append({"command": "brigade center readiness import-issues", "exit_code": rc})
    elif action_type == "build-operator-report":
        with redirect_stdout(StringIO()):
            rc = center_cmd.report_build(target=target)
        receipt["commands_invoked"].append({"command": "brigade center report build", "exit_code": rc})
        receipt["adapter_result"]["commands_invoked"].append({"command": "brigade center report build", "exit_code": rc})
    else:
        receipt["blockers"].append(f"selected action is review-only: {action_type}")
        rc = 1
    receipt["status"] = "completed" if rc == 0 else "failed"
    receipt["adapter_result"]["status"] = receipt["status"]
    receipt["adapter_result"]["blockers"] = receipt["blockers"]
    receipt["adapter_result"]["next_recommended_command"] = "brigade daily closeout" if rc == 0 else "brigade daily repair"
    receipt["completed_at"] = _now().isoformat()
    receipt["next_recommended_command"] = "brigade daily closeout"
    _record_run(target, receipt)
    _record_telemetry_event(
        target,
        {
            "type": "daily-run",
            "run_id": run_id,
            "status": receipt["status"],
            "action_type": action_type,
            "adapter_id": receipt["adapter_result"].get("adapter_id"),
            "blockers": receipt["blockers"],
            "approval_id": receipt.get("approval_id"),
        },
    )
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"daily run: {run_id}")
        print(f"status: {receipt['status']}")
        print(f"selected: {receipt['selected_action_id']}")
        print(f"next: {receipt['next_recommended_command']}")
    return rc


def approvals_payload(target: Path, *, limit: int = 50) -> dict[str, Any]:
    target = target.expanduser().resolve()
    approvals, errors = _read_approvals(target)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-approvals", "version": SCHEMA_VERSION},
        "target": str(target),
        "approvals": approvals[:limit],
        "approval_count": len(approvals),
        "parse_errors": errors,
    }


def approvals_list(*, target: Path, limit: int = 50, json_output: bool = False) -> int:
    payload = approvals_payload(target, limit=limit)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily approvals: {payload['target']}")
        for approval in payload["approvals"]:
            print(f"- {approval.get('approval_id')} [{approval.get('status')}] {approval.get('safe_summary')}")
    return 0


def approvals_show(*, target: Path, approval_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    approval = _find_approval(target, approval_id)
    if approval is None:
        print(f"error: approval not found: {approval_id}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(approval, indent=2, sort_keys=True))
    else:
        print(f"daily approval: {approval.get('approval_id')}")
        print(f"status: {approval.get('status')}")
        print(f"summary: {approval.get('safe_summary')}")
        print(f"adapter: {approval.get('selected_adapter')}")
        print(f"next: {approval.get('suggested_next_command')}")
    return 0


def _review_approval(target: Path, approval_id: str, status: str, reason: str | None, *, json_output: bool = False) -> int:
    if status not in {"approved", "rejected", "held"}:
        print(f"error: invalid approval status: {status}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    approval = _find_approval(target, approval_id)
    if approval is None:
        print(f"error: approval not found: {approval_id}", file=sys.stderr)
        return 1
    if approval.get("status") == "consumed":
        print(f"error: approval already consumed: {approval_id}", file=sys.stderr)
        return 1
    approval["status"] = status
    approval["reviewed_at"] = _now().isoformat()
    approval["review_reason"] = reason
    _write_approval(target, approval)
    if json_output:
        print(json.dumps(approval, indent=2, sort_keys=True))
    else:
        print(f"daily approval: {approval_id}")
        print(f"status: {status}")
    return 0


def approvals_approve(*, target: Path, approval_id: str, json_output: bool = False) -> int:
    return _review_approval(target, approval_id, "approved", None, json_output=json_output)


def approvals_reject(*, target: Path, approval_id: str, reason: str, json_output: bool = False) -> int:
    return _review_approval(target, approval_id, "rejected", reason, json_output=json_output)


def approvals_hold(*, target: Path, approval_id: str, reason: str, json_output: bool = False) -> int:
    return _review_approval(target, approval_id, "held", reason, json_output=json_output)


def approvals_compare_payload(target: Path, approval_id: str) -> dict[str, Any]:
    target = target.expanduser().resolve()
    config, _ = _load_config(target)
    approval = _find_approval(target, approval_id)
    issues: list[dict[str, Any]] = []
    current = None
    if approval is None:
        issues.append({"status": "fail", "name": "approval_missing", "detail": approval_id})
    else:
        current = _current_action_for_approval(target, approval)
        if approval.get("config_fingerprint") != _config_fingerprint(config):
            issues.append({"status": "warn", "name": "approval_config_changed", "detail": approval_id})
        if current is None:
            issues.append({"status": "warn", "name": "approval_missing_source_evidence", "detail": approval_id})
        elif current.get("source_fingerprint") != approval.get("source_fingerprint"):
            issues.append({"status": "warn", "name": "approval_source_fingerprint_changed", "detail": approval_id})
        if current is not None and _adapter_for(current) != approval.get("selected_adapter"):
            issues.append({"status": "warn", "name": "approval_adapter_changed", "detail": approval_id})
        selected_action = approval.get("selected_action") if isinstance(approval.get("selected_action"), dict) else {}
        matches = _matching_approvals(target, selected_action, config) if selected_action else []
        newer = [
            item
            for item in matches
            if item.get("approval_id") != approval_id
            and str(item.get("created_at") or "") > str(approval.get("created_at") or "")
        ]
        if newer:
            issues.append({"status": "warn", "name": "approval_newer_matching_request", "detail": str(newer[0].get("approval_id"))})
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-approval-compare", "version": SCHEMA_VERSION},
        "target": str(target),
        "approval_id": approval_id,
        "approval": approval,
        "current_action": current,
        "issues": issues,
        "issue_count": len(issues),
        "ok": not issues,
    }


def approvals_compare(*, target: Path, approval_id: str, json_output: bool = False) -> int:
    payload = approvals_compare_payload(target, approval_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily approval compare: {approval_id}")
        print(f"issues: {payload['issue_count']}")
        for issue in payload["issues"]:
            print(f"[{issue.get('status')}] {issue.get('name')}: {issue.get('detail')}")
    return 0 if payload["ok"] else 1


def approvals_archive(*, target: Path, consumed: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    approvals, errors = _read_approvals(target)
    archiveable = {"consumed", "rejected", "superseded"} if consumed else set()
    archived: list[dict[str, Any]] = []
    for approval in approvals:
        if approval.get("status") not in archiveable:
            continue
        approval_id = str(approval.get("approval_id") or "")
        source = _approvals_root(target) / approval_id
        destination = _approvals_archive_root(target) / approval_id
        if not source.is_dir() or destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        record = {
            "approval_id": approval_id,
            "status": approval.get("status"),
            "archived_at": _now().isoformat(),
            "archive_path": str(destination),
        }
        _write_json(destination / "archive.json", record)
        archived.append(record)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-approval-archive", "version": SCHEMA_VERSION},
        "target": str(target),
        "archived": archived,
        "archived_count": len(archived),
        "parse_errors": errors,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily approval archive: {target}")
        print(f"archived: {len(archived)}")
    return 0


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
    approvals, approval_errors = _read_approvals(target)
    telemetry_events, telemetry_errors = _telemetry_events(target)
    for error in [*run_errors, *plan_errors]:
        checks.append({"status": "fail", "name": "daily_receipt_parse", "detail": f"{error['path']}: {error['error']}"})
    for error in approval_errors:
        checks.append({"status": "fail", "name": "daily_approval_parse", "detail": f"{error['path']}: {error['error']}"})
    for error in telemetry_errors:
        checks.append({"status": "fail", "name": "daily_telemetry_parse", "detail": f"{error['path']}: {error['error']}"})
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
    pending_approvals = [approval for approval in approvals if approval.get("status") == "pending"]
    approved_approvals = [approval for approval in approvals if approval.get("status") == "approved"]
    held_approvals = [approval for approval in approvals if approval.get("status") == "held"]
    rejected_approvals = [approval for approval in approvals if approval.get("status") == "rejected"]
    top_pending = pending_approvals[0] if pending_approvals else None
    for approval in pending_approvals:
        if _is_stale(approval.get("created_at"), run_hours):
            checks.append({"status": "warn", "name": "daily_stale_pending_approval", "detail": str(approval.get("approval_id"))})
            break
    if approved_approvals:
        checks.append({"status": "warn", "name": "daily_approved_approval", "detail": str(approved_approvals[0].get("approval_id"))})
    if held_approvals:
        checks.append({"status": "warn", "name": "daily_held_approval", "detail": str(held_approvals[0].get("approval_id"))})
    if rejected_approvals:
        checks.append({"status": "warn", "name": "daily_rejected_approval", "detail": str(rejected_approvals[0].get("approval_id"))})
    for approval in approvals:
        current = _current_action_for_approval(target, approval)
        if current is None and approval.get("status") in {"pending", "approved"}:
            checks.append({"status": "warn", "name": "daily_approval_missing_evidence", "detail": str(approval.get("approval_id"))})
            break
        if current is not None and current.get("source_fingerprint") != approval.get("source_fingerprint"):
            checks.append({"status": "warn", "name": "daily_approval_changed_evidence", "detail": str(approval.get("approval_id"))})
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
        "approvals": {
            "approval_count": len(approvals),
            "pending_count": len(pending_approvals),
            "approved_count": len(approved_approvals),
            "held_count": len(held_approvals),
            "rejected_count": len(rejected_approvals),
            "top_pending": top_pending,
            "top_approved": approved_approvals[0] if approved_approvals else None,
        },
        "telemetry": {
            "event_count": len(telemetry_events),
            "failed_run_count": sum(1 for run in runs if run.get("status") == "failed"),
            "blocked_run_count": sum(1 for run in runs if run.get("status") == "blocked"),
        },
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


def protocol_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    steps = [
        {"step": "status", "command": "brigade daily status --json", "purpose": "inspect local operating state"},
        {"step": "plan", "command": "brigade daily plan --json", "purpose": "rank safe local actions"},
        {"step": "review", "command": "brigade daily review --json", "purpose": "preview evidence, acceptance, risk, and approval boundary"},
        {"step": "approval", "command": "brigade daily approvals approve <approval-id> --json", "purpose": "approve only when the selected action requires it"},
        {"step": "run", "command": "brigade daily run --json", "purpose": "execute one bounded safe adapter action"},
        {"step": "closeout", "command": "brigade daily closeout --json", "purpose": "record review, verification, and evidence state"},
        {"step": "recover", "command": "brigade daily resume --json", "purpose": "resume or explain recovery when blocked"},
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-protocol", "version": SCHEMA_VERSION},
        "target": str(target),
        "steps": steps,
        "commands": [step["command"] for step in steps],
        "safety_boundaries": [
            "no arbitrary command execution",
            "no automatic scanner, reviewer, tool, or fleet sweep execution",
            "no remote mutation",
            "no canonical memory edits",
        ],
    }


def protocol(*, target: Path, json_output: bool = False) -> int:
    payload = protocol_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily protocol: {payload['target']}")
        for step in payload["steps"]:
            print(f"- {step['step']}: {step['command']}")
    return 0


def _repair_suggestions(target: Path) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for check in health(target).get("checks", []):
        name = str(check.get("name") or "")
        command = "brigade daily doctor"
        if name == "daily_config_missing":
            command = "brigade daily init"
        elif name in {"daily_blocked_run", "daily_approved_approval"}:
            command = "brigade daily resume"
        elif name in {"daily_stale_pending_approval", "daily_held_approval", "daily_rejected_approval"}:
            command = "brigade daily approvals list"
        elif name in {"daily_missing_evidence", "daily_approval_missing_evidence", "daily_approval_changed_evidence"}:
            command = "brigade daily unblock"
        suggestions.append({"name": name, "detail": check.get("detail"), "suggested_command": command})
    return suggestions


def repair_payload(target: Path, *, write: bool = True) -> dict[str, Any]:
    target = target.expanduser().resolve()
    repair_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-daily-repair-{uuid4().hex[:6]}"
    suggestions = _repair_suggestions(target)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-repair", "version": SCHEMA_VERSION},
        "target": str(target),
        "repair_id": repair_id,
        "created_at": _now().isoformat(),
        "checks": health(target).get("checks", []),
        "suggestions": suggestions,
        "writes": [],
    }
    if write:
        path = _repairs_root(target) / repair_id / "repair.json"
        payload["path"] = str(path.parent)
        payload["writes"].append(str(path))
        _write_json(path, payload)
    return payload


def repair(*, target: Path, json_output: bool = False) -> int:
    payload = repair_payload(target, write=True)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily repair: {payload['repair_id']}")
        for suggestion in payload["suggestions"]:
            print(f"- {suggestion.get('name')}: {suggestion.get('suggested_command')}")
    return 0


def unblock_payload(target: Path, *, dry_run: bool = False) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = _latest_run(target)
    config, _ = _load_config(target)
    created_imports: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    approval_request = None
    blockers: list[str] = []
    action = latest.get("selected_action") if isinstance(latest, dict) and isinstance(latest.get("selected_action"), dict) else None
    if action and action.get("approval_required"):
        plan_data = {"plan_id": latest.get("plan_id") if isinstance(latest, dict) else None}
        approval_request = _ensure_approval(target, plan_data, action, config)
    elif latest:
        records = [
            {
                "kind": "task",
                "text": f"Resolve daily blocker for {latest.get('run_id')}",
                "source": "daily-driver",
                "type": "bugfix",
                "priority": "high",
                "acceptance": ["Daily blocker is reviewed.", "Daily driver can plan or run the next safe action."],
                "metadata": {
                    "daily_run_id": latest.get("run_id"),
                    "source_fingerprint": _fingerprint({"run_id": latest.get("run_id"), "blockers": latest.get("blockers")}),
                    "source_item_key": f"daily-driver:{latest.get('run_id')}",
                },
            }
        ]
        created_imports, skipped, _ = work_cmd._append_import_records(target, records, dry_run=dry_run)
    else:
        blockers.append("no daily run to unblock")
    unblock_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-daily-unblock-{uuid4().hex[:6]}"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-unblock", "version": SCHEMA_VERSION},
        "target": str(target),
        "unblock_id": unblock_id,
        "created_at": _now().isoformat(),
        "latest_run": latest,
        "approval_request": approval_request,
        "created_imports": created_imports,
        "skipped_imports": skipped,
        "blockers": blockers,
        "dry_run": dry_run,
    }
    if not dry_run:
        path = _unblocks_root(target) / unblock_id / "unblock.json"
        payload["path"] = str(path.parent)
        _write_json(path, payload)
    return payload


def unblock(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    payload = unblock_payload(target, dry_run=dry_run)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily unblock: {payload['unblock_id']}")
        print(f"created_imports: {len(payload['created_imports'])}")
        if payload.get("approval_request"):
            print(f"approval: {payload['approval_request'].get('approval_id')}")
    return 1 if payload.get("blockers") else 0


def resume(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    latest = _latest_run(target)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-resume", "version": SCHEMA_VERSION},
        "target": str(target),
        "latest_run": latest,
        "status": "blocked",
        "action_taken": None,
        "next_recommended_command": "brigade daily plan",
        "blockers": [],
    }
    if latest is None:
        payload["blockers"].append("no daily run to resume")
    else:
        approval_id = latest.get("approval_id")
        approval = _find_approval(target, str(approval_id)) if approval_id else None
        if isinstance(approval, dict) and approval.get("status") == "approved":
            payload["action_taken"] = "run-approved-approval"
            payload["next_recommended_command"] = f"brigade daily run --approval {approval_id}"
            if json_output:
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(f"daily resume: {payload['next_recommended_command']}")
            return 0
        if latest.get("status") in {"blocked", "failed"}:
            payload["action_taken"] = "repair-suggested"
            payload["next_recommended_command"] = "brigade daily repair"
        elif latest.get("status") == "completed" and not latest.get("closeout_status"):
            payload["action_taken"] = "closeout-suggested"
            payload["next_recommended_command"] = "brigade daily closeout"
            payload["status"] = "ready"
        else:
            payload["status"] = "ready"
            payload["action_taken"] = "plan-next"
            payload["next_recommended_command"] = "brigade daily plan"
    if payload["blockers"]:
        payload["status"] = "blocked"
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily resume: {payload['status']}")
        print(f"next: {payload['next_recommended_command']}")
    return 1 if payload["blockers"] else 0


def _telemetry_events(target: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    return _iter_receipts(_telemetry_root(target) / "events", "event.json")


def telemetry_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    events, errors = _telemetry_events(target)
    runs, _ = _iter_receipts(_runs_root(target), "run.json")
    approvals, _ = _read_approvals(target)
    statuses = Counter(str(run.get("status") or "unknown") for run in runs)
    action_types = Counter(str((run.get("selected_action") or {}).get("action_type") or "unknown") for run in runs if isinstance(run.get("selected_action"), dict))
    blocker_counts = Counter()
    for run in runs:
        for blocker in run.get("blockers", []) if isinstance(run.get("blockers"), list) else []:
            blocker_counts[str(blocker)] += 1
    closed_ages: list[float] = []
    for run in runs:
        completed = _parse_time(run.get("completed_at"))
        reviewed = _parse_time(run.get("reviewed_at"))
        if completed and reviewed:
            closed_ages.append((reviewed - completed).total_seconds() / 3600)
    metrics = {
        "event_count": len(events),
        "run_count": len(runs),
        "selected_action_types": dict(action_types),
        "approval_frequency": len(approvals),
        "block_reasons": dict(blocker_counts),
        "stale_evidence_rate": sum(1 for check in health(target).get("checks", []) if "evidence" in str(check.get("name"))) / max(1, len(runs)),
        "failed_run_rate": statuses.get("failed", 0) / max(1, len(runs)),
        "closeout_status_counts": dict(Counter(str(run.get("closeout_status") or "open") for run in runs)),
        "repeated_blocker_fingerprints": [key for key, count in blocker_counts.items() if count > 1],
        "ignored_or_deferred_recommendations": statuses.get("blocked", 0) + sum(1 for run in runs if run.get("closeout_status") == "deferred"),
        "average_run_to_closeout_hours": round(sum(closed_ages) / len(closed_ages), 2) if closed_ages else None,
    }
    checks: list[dict[str, Any]] = []
    if statuses.get("failed", 0):
        checks.append({"status": "warn", "name": "daily_telemetry_failed_runs", "detail": str(statuses.get("failed", 0))})
    if metrics["repeated_blocker_fingerprints"]:
        checks.append({"status": "warn", "name": "daily_telemetry_repeated_blockers", "detail": str(metrics["repeated_blocker_fingerprints"][0])})
    for error in errors:
        checks.append({"status": "fail", "name": "daily_telemetry_parse", "detail": f"{error['path']}: {error['error']}"})
    issues = [check for check in checks if check.get("status") != "ok"]
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-telemetry", "version": SCHEMA_VERSION},
        "target": str(target),
        "metrics": metrics,
        "events": events[:50],
        "checks": checks,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def telemetry(*, target: Path, json_output: bool = False) -> int:
    payload = telemetry_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily telemetry: {payload['target']}")
        print(f"runs: {payload['metrics']['run_count']}")
        print(f"approvals: {payload['metrics']['approval_frequency']}")
        print(f"issues: {payload['issue_count']}")
    return 0


def telemetry_doctor(*, target: Path, json_output: bool = False) -> int:
    payload = telemetry_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily telemetry doctor: {payload['target']}")
        for check in payload["checks"]:
            print(f"[{check.get('status')}] {check.get('name')}: {check.get('detail')}")
    return 1 if any(check.get("status") == "fail" for check in payload["checks"]) else 0


def hardening_plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    phases = _hardening_phases()
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-hardening-plan", "version": SCHEMA_VERSION},
        "target": str(target),
        "phase_range": "115-164",
        "phase_count": len(phases),
        "implemented_phase_count": sum(1 for phase in phases if phase.get("status") == "implemented"),
        "workstreams": HARDENING_WORKSTREAMS,
        "phases": phases,
        "safety_boundaries": [
            "no daemon",
            "no scheduler mutation",
            "no web UI",
            "no database",
            "no arbitrary command execution",
            "no automatic scanner, reviewer, tool, or fleet sweep execution",
            "no remote mutation",
            "no canonical memory edits",
            "no new dependencies",
        ],
        "source_of_truth": "docs/phase-115-164-plan.md",
        "suggested_next_commands": [
            "brigade daily hardening audit",
            "brigade daily hardening import-issues",
            "brigade daily plan",
        ],
    }


def hardening_plan(*, target: Path, json_output: bool = False) -> int:
    payload = hardening_plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily hardening plan: {payload['target']}")
        print(f"phases: {payload['phase_count']}")
        for stream in payload["workstreams"]:
            print(f"- {stream['phase_start']}-{stream['phase_end']} {stream['id']}")
    return 0


def _hardening_finding(
    *,
    workstream: str,
    phase: int | None = None,
    name: str,
    severity: str,
    safe_summary: str,
    suggested_command: str,
    evidence_refs: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "workstream": workstream,
        "phase": phase,
        "phase_title": HARDENING_PHASE_TITLES.get(phase) if phase is not None else None,
        "name": name,
        "severity": severity,
        "safe_summary": safe_summary,
        "suggested_command": suggested_command,
        "evidence_refs": evidence_refs or [],
        "metadata": metadata or {},
    }
    payload["source_fingerprint"] = _fingerprint(payload)
    payload["finding_id"] = f"daily-hardening-{_slug(workstream)}-{_slug(name)}-{payload['source_fingerprint'][:10]}"
    return payload


def _latest_hardening_closeout(target: Path) -> dict[str, Any] | None:
    closeouts, _ = _iter_receipts(_hardening_closeouts_root(target), "closeout.json")
    return closeouts[0] if closeouts else None


def _hardening_quieted_findings(target: Path, findings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    closeout = _latest_hardening_closeout(target)
    if not closeout or closeout.get("status") not in {"reviewed", "archived"}:
        return findings, [], closeout
    closed_fingerprints = closeout.get("finding_fingerprints")
    if not isinstance(closed_fingerprints, list):
        closed_fingerprints = []
    closed_set = {str(item) for item in closed_fingerprints}
    if not closed_set and closeout.get("audit_fingerprint") == _fingerprint(findings):
        return [], findings, closeout
    unresolved = [item for item in findings if str(item.get("source_fingerprint")) not in closed_set]
    quieted = [item for item in findings if str(item.get("source_fingerprint")) in closed_set]
    return unresolved, quieted, closeout


def _adapter_result_is_normalized(run: dict[str, Any]) -> bool:
    result = run.get("adapter_result")
    if not isinstance(result, dict):
        return False
    required = {
        "adapter_id",
        "source_subsystem",
        "source_local_id",
        "status",
        "commands_invoked",
        "receipts_created",
        "blockers",
        "warnings",
        "next_recommended_command",
        "evidence_references",
    }
    return required <= set(result)


def _plan_explanations_are_complete(plan: dict[str, Any]) -> bool:
    candidates = plan.get("candidate_actions")
    explanations = plan.get("candidate_explanations")
    if not isinstance(candidates, list) or not isinstance(explanations, list):
        return False
    explanation_ids = {str(item.get("action_id")) for item in explanations if isinstance(item, dict)}
    candidate_ids = {str(item.get("action_id")) for item in candidates if isinstance(item, dict)}
    if candidate_ids and not candidate_ids <= explanation_ids:
        return False
    for item in explanations:
        if not isinstance(item, dict):
            return False
        if "scoring_reasons" not in item or "rejection_reasons" not in item:
            return False
    return True


def hardening_audit_payload(target: Path) -> dict[str, Any]:
    from . import release_cmd, repos_cmd

    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    config, config_checks = _load_config(target)
    runs, run_errors = _iter_receipts(_runs_root(target), "run.json")
    plans, plan_errors = _iter_receipts(_plans_root(target), "plan.json")
    latest_run = runs[0] if runs else None
    latest_plan = plans[0] if plans else None
    daily_health = health(target)
    telemetry_data = telemetry_payload(target)
    config_issues = [check for check in config_checks if check.get("status") != "ok"]
    if any(check.get("status") == "fail" for check in config_checks):
        findings.append(_hardening_finding(workstream="daily-production-hardening", phase=115, name="daily_config_invalid", severity="high", safe_summary="daily config has invalid fields", suggested_command="brigade daily doctor", evidence_refs=[str(_config_path(target))], metadata={"checks": config_issues}))
    unsafe_config_checks = [
        check
        for check in config_issues
        if check.get("name") in {"daily_disabled", "daily_risk_policy"}
        or str(check.get("name") or "").startswith("allow_")
    ]
    if unsafe_config_checks:
        findings.append(_hardening_finding(workstream="daily-production-hardening", phase=115, name="daily_config_policy_warning", severity="medium", safe_summary="daily config has unsafe or blocking local policy states", suggested_command="brigade daily doctor", evidence_refs=[str(_config_path(target))], metadata={"checks": unsafe_config_checks}))
    malformed_runs = [run for run in runs[:10] if not _adapter_result_is_normalized(run)]
    malformed_run_errors = [{"run_id": None, "error": item.get("error"), "path": item.get("path")} for item in run_errors]
    if malformed_runs or malformed_run_errors:
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=116,
                name="missing_adapter_result",
                severity="high",
                safe_summary=f"{len(malformed_runs) + len(malformed_run_errors)} recent daily run receipt(s) lack normalized adapter results",
                suggested_command="brigade daily show latest",
                evidence_refs=[str(_runs_root(target))],
                metadata={
                    "run_ids": [run.get("run_id") for run in malformed_runs[:10]],
                    "parse_errors": malformed_run_errors[:10],
                    "required_fields": [
                        "adapter_id",
                        "source_subsystem",
                        "source_local_id",
                        "status",
                        "commands_invoked",
                        "receipts_created",
                        "blockers",
                        "warnings",
                        "next_recommended_command",
                        "evidence_references",
                    ],
                },
            )
        )
    malformed_plans = [plan for plan in plans[:10] if not _plan_explanations_are_complete(plan)]
    malformed_plan_errors = [{"plan_id": None, "error": item.get("error"), "path": item.get("path")} for item in plan_errors]
    if malformed_plans or malformed_plan_errors:
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=117,
                name="missing_plan_explanations",
                severity="medium",
                safe_summary=f"{len(malformed_plans) + len(malformed_plan_errors)} recent daily plan receipt(s) lack candidate explanations",
                suggested_command="brigade daily plan --record",
                evidence_refs=[str(_plans_root(target))],
                metadata={
                    "plan_ids": [plan.get("plan_id") for plan in malformed_plans[:10]],
                    "plan_fingerprints": [_fingerprint(plan) for plan in malformed_plans[:10]],
                    "parse_errors": malformed_plan_errors[:10],
                },
            )
        )
    approvals = daily_health.get("approvals") if isinstance(daily_health.get("approvals"), dict) else {}
    approval_items, approval_errors = _read_approvals(target)
    approval_counts = Counter(str(item.get("status") or "unknown") for item in approval_items)
    stale_approvals = [
        item
        for item in approval_items
        if item.get("status") in {"pending", "approved"}
        and (_age_hours(item.get("created_at")) is not None and (_age_hours(item.get("created_at")) or 0) > int(config.get("stale_run_threshold_hours") or 24))
    ]
    if int(approvals.get("pending_count") or 0) > 0 or stale_approvals or approval_counts.get("held") or approval_counts.get("rejected") or approval_errors:
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=118,
                name="daily_approval_hygiene",
                severity="medium",
                safe_summary="daily approvals need review, consumption, or archive",
                suggested_command="brigade daily approvals list",
                evidence_refs=[str(_approvals_root(target))],
                metadata={
                    "status_counts": dict(approval_counts),
                    "stale_approval_ids": [item.get("approval_id") for item in stale_approvals[:10]],
                    "parse_errors": approval_errors[:10],
                },
            )
        )
    if telemetry_data.get("issue_count"):
        findings.append(_hardening_finding(workstream="daily-production-hardening", phase=119, name="daily_telemetry_issue", severity="medium", safe_summary="daily telemetry has warnings or parse errors", suggested_command="brigade daily telemetry doctor", evidence_refs=[str(_telemetry_root(target))], metadata={"checks": telemetry_data.get("checks"), "metrics": telemetry_data.get("metrics")}))
    protocol_data = protocol_payload(target)
    protocol_steps = {str(item.get("step")) for item in protocol_data.get("steps", []) if isinstance(item, dict)}
    required_protocol_steps = {"status", "plan", "review", "approval", "run", "closeout", "recover"}
    if not required_protocol_steps <= protocol_steps:
        findings.append(_hardening_finding(workstream="daily-production-hardening", phase=122, name="daily_protocol_incomplete", severity="high", safe_summary="daily protocol is missing wrapper-facing steps", suggested_command="brigade daily protocol", evidence_refs=["daily protocol"], metadata={"required_steps": sorted(required_protocol_steps), "actual_steps": sorted(protocol_steps)}))
    for command in protocol_data.get("commands", []) if isinstance(protocol_data.get("commands"), list) else []:
        if not str(command).startswith("brigade daily "):
            findings.append(_hardening_finding(workstream="daily-production-hardening", phase=122, name="daily_protocol_command_scope", severity="medium", safe_summary="daily protocol includes a non-daily command", suggested_command="brigade daily protocol", evidence_refs=["daily protocol"], metadata={"command": command}))
            break
    latest_run_output = latest_run.get("wrapped_output") if isinstance(latest_run, dict) else None
    if isinstance(latest_run_output, (str, list, dict)):
        findings.append(_hardening_finding(workstream="daily-production-hardening", phase=123, name="wrapped_output_in_run_json", severity="medium", safe_summary="daily run receipt appears to include wrapped command output instead of receipt references", suggested_command="brigade daily show latest", evidence_refs=[str(_runs_root(target))]))

    center_manifest = center_cmd._center_schema_manifest(target)
    center_contract = center_cmd._center_contract_health(target)
    if int(center_manifest.get("schema_count") or 0) < 1:
        findings.append(_hardening_finding(workstream="operator-center-contract-cleanup", phase=125, name="center_schema_missing", severity="high", safe_summary="center schema manifest is empty", suggested_command="brigade center schema", evidence_refs=["center schema"]))
    center_reviews = center_cmd._reviews(target)
    required_review_fields = {"subsystem", "local_id", "status", "safe_summary", "suggested_next_command"}
    malformed_review = next((item for item in center_reviews if not required_review_fields <= set(item)), None)
    if malformed_review:
        findings.append(_hardening_finding(workstream="operator-center-contract-cleanup", phase=126, name="center_review_shape", severity="medium", safe_summary="center review item is missing wrapper-facing fields", suggested_command="brigade center reviews --json", evidence_refs=["center reviews"]))
    for issue in center_contract.get("issues", []) if isinstance(center_contract.get("issues"), list) else []:
        phase = issue.get("phase") if isinstance(issue.get("phase"), int) else 129
        findings.append(
            _hardening_finding(
                workstream="operator-center-contract-cleanup",
                phase=phase,
                name=str(issue.get("name") or "center_contract_issue"),
                severity="high" if issue.get("status") == "fail" else "medium",
                safe_summary=str(issue.get("detail") or "center contract has an issue"),
                suggested_command=str(issue.get("suggested_next_command") or "brigade center status --json"),
                evidence_refs=["center contract health"],
                metadata={"issue": issue},
            )
        )

    pending_imports = work_cmd._pending_imports(target)
    missing_acceptance = [item for item in pending_imports if not item.get("acceptance")]
    missing_provenance = [
        item
        for item in pending_imports
        if not ((item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("source_fingerprint") or item.get("source"))
    ]
    if missing_acceptance:
        findings.append(_hardening_finding(workstream="inbox-evidence-quality", phase=135, name="pending_import_missing_acceptance", severity="medium", safe_summary=f"{len(missing_acceptance)} pending import(s) missing acceptance", suggested_command="brigade work inbox doctor", evidence_refs=[str(work_cmd._imports_path(target))]))
    if missing_provenance:
        findings.append(_hardening_finding(workstream="inbox-evidence-quality", phase=136, name="pending_import_missing_provenance", severity="medium", safe_summary=f"{len(missing_provenance)} pending import(s) missing provenance", suggested_command="brigade work import provenance", evidence_refs=[str(work_cmd._imports_path(target))]))
    inbox_hygiene = work_cmd._inbox_hygiene_payload(target)
    inbox_quality = work_cmd._inbox_quality_payload(target)
    if int(inbox_hygiene.get("issue_count") or 0) > 0:
        top = inbox_hygiene.get("top_issue") if isinstance(inbox_hygiene.get("top_issue"), dict) else {}
        findings.append(_hardening_finding(workstream="inbox-evidence-quality", phase=137, name="inbox_hygiene_issue", severity="medium", safe_summary=str(top.get("detail") or "work inbox has hygiene issues"), suggested_command="brigade work inbox doctor", evidence_refs=[str(work_cmd._imports_path(target))]))
    quality_counts = inbox_quality.get("issue_counts") if isinstance(inbox_quality.get("issue_counts"), dict) else {}
    noisy_or_deferred = int(quality_counts.get("noisy_source") or 0) + int(quality_counts.get("deferred") or 0) + int(quality_counts.get("stale") or 0)
    if noisy_or_deferred:
        findings.append(
            _hardening_finding(
                workstream="inbox-evidence-quality",
                phase=138,
                name="inbox_selection_penalties",
                severity="medium",
                safe_summary=f"{noisy_or_deferred} pending import(s) are stale, deferred, or from noisy sources",
                suggested_command="brigade daily plan",
                evidence_refs=[str(work_cmd._imports_path(target))],
                metadata={"issue_counts": quality_counts},
            )
        )
    if int(quality_counts.get("changed_dismissed") or 0):
        findings.append(
            _hardening_finding(
                workstream="inbox-evidence-quality",
                phase=139,
                name="inbox_changed_dismissed",
                severity="medium",
                safe_summary=f"{quality_counts.get('changed_dismissed')} dismissed import(s) resurfaced with changed fingerprints",
                suggested_command="brigade work inbox doctor",
                evidence_refs=[str(work_cmd._imports_path(target))],
                metadata={"changed_dismissed_import_ids": inbox_quality.get("changed_dismissed_import_ids")},
            )
        )
    if int(quality_counts.get("duplicate_pending") or 0):
        findings.append(
            _hardening_finding(
                workstream="inbox-evidence-quality",
                phase=141,
                name="inbox_duplicate_pending",
                severity="medium",
                safe_summary=f"{quality_counts.get('duplicate_pending')} duplicate pending import(s) need dedupe review",
                suggested_command="brigade work inbox doctor",
                evidence_refs=[str(work_cmd._imports_path(target))],
                metadata={"issue_counts": quality_counts},
            )
        )
    if inbox_quality.get("best_import") and int((inbox_quality.get("best_import") or {}).get("quality_score") or 0) < 80:
        findings.append(
            _hardening_finding(
                workstream="inbox-evidence-quality",
                phase=142,
                name="inbox_low_evidence_top_candidate",
                severity="low",
                safe_summary="best pending import has weak acceptance or provenance quality",
                suggested_command="brigade work inbox",
                evidence_refs=[str(work_cmd._imports_path(target))],
                metadata={"best_import": inbox_quality.get("best_import")},
            )
        )

    repo_health = repos_cmd.health(target)
    if int(repo_health.get("issue_count") or 0) > 0:
        top = repo_health.get("top_issue") if isinstance(repo_health.get("top_issue"), dict) else {}
        findings.append(_hardening_finding(workstream="repo-fleet-daily-use", phase=145, name="repo_fleet_health_issue", severity="medium", safe_summary=str(top.get("detail") or "repo fleet has health issues"), suggested_command="brigade repos doctor", evidence_refs=["repo fleet health"]))

    release_readiness = release_cmd._latest_release_receipt(target)
    release_candidate = release_cmd._latest_candidate(target)
    if not release_readiness:
        findings.append(_hardening_finding(workstream="self-dogfood-release-loop", phase=155, name="missing_release_readiness", severity="medium", safe_summary="latest release readiness receipt is missing", suggested_command="brigade release run", evidence_refs=[".brigade/release/runs"]))
    elif not release_readiness.get("ready"):
        findings.append(_hardening_finding(workstream="self-dogfood-release-loop", phase=158, name="blocked_release_readiness", severity="high", safe_summary="latest release readiness is blocked", suggested_command=f"brigade release show {release_readiness.get('run_id')}", evidence_refs=[str(release_readiness.get("path") or ".brigade/release/runs")]))
    if not release_candidate:
        findings.append(_hardening_finding(workstream="self-dogfood-release-loop", phase=156, name="missing_release_candidate", severity="low", safe_summary="latest release candidate packet is missing", suggested_command="brigade release candidate build", evidence_refs=[".brigade/release/candidates"]))
    elif not isinstance(release_candidate.get("daily_driver"), dict):
        findings.append(_hardening_finding(workstream="self-dogfood-release-loop", phase=157, name="candidate_missing_daily_evidence", severity="medium", safe_summary="latest release candidate is missing daily driver evidence", suggested_command="brigade release candidate build", evidence_refs=[str(release_candidate.get("path") or ".brigade/release/candidates")]))
    if release_readiness:
        evidence = release_readiness.get("evidence") if isinstance(release_readiness.get("evidence"), dict) else {}
        if not isinstance(evidence.get("daily_hardening"), dict):
            findings.append(_hardening_finding(workstream="daily-production-hardening", phase=124, name="release_missing_daily_hardening", severity="medium", safe_summary="latest release readiness evidence is missing daily hardening state", suggested_command="brigade release run", evidence_refs=[str(release_readiness.get("path") or ".brigade/release/runs")]))

    findings.sort(key=lambda item: ({"high": 3, "medium": 2, "low": 1}.get(str(item.get("severity")), 0), str(item.get("finding_id"))), reverse=True)
    raw_findings = list(findings)
    findings, quieted_findings, latest_closeout = _hardening_quieted_findings(target, findings)
    by_workstream = {
        stream["id"]: {
            "phase_start": stream["phase_start"],
            "phase_end": stream["phase_end"],
            "finding_count": len([item for item in findings if item.get("workstream") == stream["id"]]),
            "quieted_count": len([item for item in quieted_findings if item.get("workstream") == stream["id"]]),
            "status": "needs-attention" if any(item.get("workstream") == stream["id"] for item in findings) else "ok",
        }
        for stream in HARDENING_WORKSTREAMS
    }
    phases = _hardening_phases()
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-hardening-audit", "version": SCHEMA_VERSION},
        "target": str(target),
        "phase_range": "115-164",
        "phase_count": len(phases),
        "implemented_phase_count": sum(1 for phase in phases if phase.get("status") == "implemented"),
        "phases": phases,
        "workstreams": by_workstream,
        "findings": findings,
        "finding_count": len(findings),
        "raw_findings": raw_findings,
        "raw_finding_count": len(raw_findings),
        "quieted_findings": quieted_findings,
        "quieted_count": len(quieted_findings),
        "latest_closeout": latest_closeout,
        "issue_count": len(findings),
        "top_issue": findings[0] if findings else None,
        "suggested_next_commands": ["brigade daily hardening import-issues", "brigade daily plan"],
    }


def hardening_audit(*, target: Path, json_output: bool = False) -> int:
    payload = hardening_audit_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily hardening audit: {payload['target']}")
        print(f"findings: {payload['finding_count']}")
        for finding in payload["findings"][:10]:
            print(f"- [{finding['severity']}] {finding['finding_id']}: {finding['safe_summary']}")
    return 0


def hardening_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    audit = hardening_audit_payload(target)
    records = []
    for finding in audit["findings"]:
        records.append(
            {
                "kind": "task",
                "text": f"Review daily hardening finding: {finding['safe_summary']}",
                "source": "daily-hardening",
                "type": "bugfix",
                "priority": "high" if finding.get("severity") == "high" else "normal",
                "template": "bugfix",
                "acceptance": [
                    "The hardening finding is reviewed.",
                    "The related daily, center, inbox, fleet, or release evidence is updated or explicitly deferred.",
                    "Daily hardening audit no longer reports this unchanged finding as unresolved, or the deferral is documented.",
                ],
                "metadata": {
                    "finding_id": finding.get("finding_id"),
                    "workstream": finding.get("workstream"),
                    "phase": finding.get("phase"),
                    "phase_title": finding.get("phase_title"),
                    "severity": finding.get("severity"),
                    "suggested_command": finding.get("suggested_command"),
                    "source_item_key": f"daily-hardening:{finding.get('finding_id')}",
                    "source_fingerprint": finding.get("source_fingerprint"),
                    "safe_summary": finding.get("safe_summary"),
                },
            }
        )
    created, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-hardening-import-issues", "version": SCHEMA_VERSION},
        "target": str(target),
        "dry_run": dry_run,
        "finding_count": audit["finding_count"],
        "created_imports": created,
        "skipped_imports": skipped,
        "dismissed_imports": skipped_dismissed,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "dismissed_count": len(skipped_dismissed),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily hardening import-issues: {target}")
        print(f"created: {len(created)}")
        print(f"skipped: {len(skipped)}")
        print(f"dismissed: {len(skipped_dismissed)}")
    return 0


def hardening_closeout(
    *,
    target: Path,
    status: str = "reviewed",
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    if status not in RUN_STATUSES:
        print(f"error: invalid hardening closeout status: {status}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    audit = hardening_audit_payload(target)
    closeout_id = f"{_now().strftime('%Y%m%d-%H%M%S')}-daily-hardening-closeout-{uuid4().hex[:6]}"
    unresolved = audit["findings"] if status not in {"reviewed", "archived"} else []
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": {"name": "daily-hardening-closeout", "version": SCHEMA_VERSION},
        "target": str(target),
        "closeout_id": closeout_id,
        "status": status,
        "reason": reason,
        "created_at": _now().isoformat(),
        "phase_range": "115-164",
        "finding_count": audit["finding_count"],
        "raw_finding_count": audit.get("raw_finding_count"),
        "quieted_count": audit.get("quieted_count"),
        "unresolved_count": len(unresolved),
        "unresolved_findings": unresolved,
        "audit_fingerprint": _fingerprint(audit["findings"]),
        "raw_audit_fingerprint": _fingerprint(audit.get("raw_findings", [])),
        "finding_fingerprints": [finding.get("source_fingerprint") for finding in audit.get("findings", [])],
        "quieted_fingerprints": [finding.get("source_fingerprint") for finding in audit.get("quieted_findings", [])],
    }
    path = _hardening_closeouts_root(target) / closeout_id / "closeout.json"
    payload["path"] = str(path.parent)
    _write_json(path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"daily hardening closeout: {closeout_id}")
        print(f"status: {status}")
        print(f"findings: {audit['finding_count']}")
    return 0


def _changed_files_summary(target: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=target,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return {"available": False, "tracked_dirty_count": None, "untracked_count": None, "files": []}
    files = []
    tracked = 0
    untracked = 0
    for line in proc.stdout.splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        files.append({"status": status.strip(), "path": _safe_text(target, path)})
        if status == "??":
            untracked += 1
        else:
            tracked += 1
    return {"available": proc.returncode == 0, "tracked_dirty_count": tracked, "untracked_count": untracked, "files": files[:50]}


def _verification_expectation(config: dict[str, Any], run_receipt: dict[str, Any]) -> dict[str, Any]:
    action = run_receipt.get("selected_action") if isinstance(run_receipt.get("selected_action"), dict) else {}
    action_type = str(action.get("action_type") or "")
    required = False
    if action_type == "run-task":
        required = bool(config.get("verification_required_for_work_run"))
    elif action_type == "promote-import":
        required = bool(config.get("verification_required_for_import_promotion"))
    elif action_type in {"import-readiness-issues", "build-operator-report"}:
        required = bool(config.get("verification_required_for_release_actions"))
    return {
        "required": required,
        "action_type": action_type,
        "allowed_commands": config.get("allowed_verification_commands"),
        "timeout": config.get("verification_timeout"),
    }


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
    config, _ = _load_config(target)
    latest_verification = work_cmd._latest_verify_receipt(target)
    verification_expectation = _verification_expectation(config, receipt)
    verification_blockers: list[str] = []
    if verification_expectation["required"] and not latest_verification:
        verification_blockers.append("verification receipt required by daily config")
    elif verification_expectation["required"] and latest_verification.get("status") != "completed":
        verification_blockers.append(f"latest verification did not complete: {latest_verification.get('run_id')}")
    receipt["closeout_status"] = status
    receipt["closeout_reason"] = reason
    receipt["reviewed_at"] = _now().isoformat()
    receipt["latest_work_closeout"] = work_cmd._latest_work_closeout_payload(target)
    receipt["latest_verification"] = latest_verification
    receipt["verification_status"] = "missing" if latest_verification is None else str(latest_verification.get("status") or "unknown")
    receipt["verification_expectation"] = verification_expectation
    receipt["verification_blockers"] = verification_blockers
    receipt["changed_files_summary"] = _changed_files_summary(target)
    receipt["review_closeout_state"] = work_cmd._review_health(target).get("latest_closeout")
    receipt["handoff_drafts"] = center_cmd.status_payload(target).get("handoff_drafts")
    receipt["center_report"] = center_cmd.latest_report(target)
    receipt["center_readiness"] = center_cmd._latest_readiness(target)
    receipt["release_readiness_impact"] = {
        "latest_release_readiness": center_cmd.status_payload(target).get("release_readiness"),
        "improved": not verification_blockers and status == "reviewed",
    }
    if handoff:
        try:
            handoff_path = _write_handoff(target, receipt, status, reason)
        except RuntimeError as exc:
            print(f"error: handoff lint failed: {exc}", file=sys.stderr)
            return 1
        receipt["handoff_path"] = str(handoff_path)
    _record_run(target, receipt)
    _record_telemetry_event(
        target,
        {
            "type": "daily-closeout",
            "run_id": receipt.get("run_id"),
            "status": status,
            "verification_status": receipt.get("verification_status"),
            "blockers": verification_blockers,
        },
    )
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    print(f"daily closeout: {receipt.get('run_id')}")
    print(f"status: {status}")
    print(f"run_status: {receipt.get('status')}")
    if receipt.get("handoff_path"):
        print(f"handoff: {receipt['handoff_path']}")
    return 0
