"""Session lifecycle, run/status/doctor/brief, and task operations."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import inspect
import os
import re
import shlex
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from ... import dogfood_cmd, localio, memory_care_instructions, outcome_ratchet_report
from ...config import load_config
from ...install import apply_gitignore
from .. import brief_values, constants, helpers, ledger as ledger_mod, config as config_mod, services as services_mod
from .. import scanners as scanners_mod, reviews as reviews_mod

from .lifecycle import _resolve_next_task

# A section deadline keeps one unreachable provider from holding the whole
# brief. The total budget bounds the sum: once it is spent the remaining
# sections report a skip instead of calling their provider. Both are
# env-overridable so tests and hooks can tighten them.
DEFAULT_BRIEF_SECTION_TIMEOUT_SECONDS = 5.0
DEFAULT_BRIEF_BUDGET_SECONDS = 20.0
BRIEF_SECTION_TIMEOUT_ENV = "BRIGADE_WORK_BRIEF_SECTION_TIMEOUT"
BRIEF_BUDGET_ENV = "BRIGADE_WORK_BRIEF_BUDGET"


class _UnavailableSection(dict):
    """Render-safe placeholder for a section that did not finish in time.

    The brief packs section fields straight into its payload, so a timed-out
    dict section still has to answer lookups. Missing keys read as ``None``, and
    the owning payload entry is dropped before it is returned.
    """

    def __missing__(self, key: str) -> None:
        return None


def _brief_seconds_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default


class _BriefSections:
    """Bound each work-brief provider with a per-section deadline and a budget."""

    def __init__(self, *, section_timeout: float | None = None, budget: float | None = None) -> None:
        self.section_timeout = (
            section_timeout
            if section_timeout is not None
            else _brief_seconds_env(BRIEF_SECTION_TIMEOUT_ENV, DEFAULT_BRIEF_SECTION_TIMEOUT_SECONDS)
        )
        self.budget = (
            budget if budget is not None else _brief_seconds_env(BRIEF_BUDGET_ENV, DEFAULT_BRIEF_BUDGET_SECONDS)
        )
        self.deadline = time.monotonic() + self.budget
        self.checks: list[dict[str, Any]] = []
        self.unavailable_keys: set[str] = set()

    def call(self, label: str, func, fallback: Any, *, key: str | None = None) -> Any:
        """Return the section value, its fallback, or a skip once the budget is spent."""
        from ...daily_cmd.config import _bounded_status_call

        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            value: Any = fallback
            check = {
                "status": "warn",
                "name": f"work_brief_section:{label}",
                "detail": f"skipped after the {self.budget:g}s brief budget",
                "elapsed_ms": 0,
            }
        else:
            value, check = _bounded_status_call(
                label,
                func,
                fallback,
                timeout_seconds=min(self.section_timeout, remaining),
                check_prefix="work_brief_section",
                # A corrupt or unsupported ledger must keep failing closed with
                # exit 2 for the session-start hooks, not degrade to a warning.
                raise_types=(ledger_mod.TaskLedgerError,),
            )
        self.checks.append(check)
        if check.get("status") != "ok" and key:
            self.unavailable_keys.add(key)
        return value

    @property
    def issue_count(self) -> int:
        return sum(1 for check in self.checks if check.get("status") != "ok")

    @property
    def top_issue(self) -> dict[str, Any] | None:
        # The session facade copies sibling command names into this module, so
        # avoid the ``next`` builtin here.
        for check in self.checks:
            if check.get("status") != "ok":
                return check
        return None


def _security_health_for_brief(security_cmd: Any, target: Path) -> dict[str, Any]:
    signature = inspect.signature(security_cmd.health)
    supports_cache_only = "suppression_cache_only" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
    )
    if "known_wiring_roots_only" in signature.parameters:
        return security_cmd.health(target, suppression_cache_only=True, known_wiring_roots_only=True)
    if supports_cache_only:
        return security_cmd.health(target, suppression_cache_only=True)
    return security_cmd.health(target)


def _doctor_ignore_level(value: str) -> str:
    if value in {"yes", "outside-target"}:
        return constants.OK
    if value == "no":
        return constants.WARN
    return constants.WARN


def _workflow_rule_repair_command(target: Path) -> str:
    # init keeps the recorded harnesses and settings, so the hint only has to
    # match the recorded depth; --full matters at repo depth alone.
    try:
        cfg = load_config(target)
    except (ValueError, AttributeError, TypeError):
        cfg = None
    depth = cfg.selection.depth if cfg is not None else "repo"
    full = " --full" if depth == "repo" else ""
    return f"brigade init --target {shlex.quote(str(target))} --depth {depth}{full}"


def _workflow_rule_health(target: Path) -> dict[str, Any]:
    missing = [rel for rel in constants.WORKFLOW_RULE_TEMPLATES if not (target / rel).is_file()]
    return {
        "status": constants.OK if not missing else constants.WARN,
        "name": "workflow_rules",
        "detail": (
            "repo-shareable workflow rules installed"
            if not missing
            else f"missing {', '.join(missing)}; run `{_workflow_rule_repair_command(target)}` to install the missing templates"
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


def _suggested_command_parts(
    active: dict[str, Any] | None, next_text: object, source: object
) -> tuple[str, str | None]:
    """The suggested command and the untrusted text it embeds, if it embeds one.

    The text is returned beside the command because `shlex.quote` rewrites a quote,
    so the quoted argument is not a substring of the raw text. The renderer needs
    the raw value to decide whether the command may be printed at all.
    """
    if active is not None:
        return 'brigade work end --note "..." --handoff', None
    if source == "task_ledger":
        return "brigade work run", None
    if source == "task_ledger_blocked":
        return "brigade work ready --explain --json", None
    if isinstance(next_text, str) and next_text.strip() and source != "default_review":
        text = next_text.strip()
        return f"brigade work run {shlex.quote(text)}", text
    return "brigade work run", None


def _suggested_command(active: dict[str, Any] | None, next_text: object, source: object) -> str:
    return _suggested_command_parts(active, next_text, source)[0]


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
    raw_release_train = payload.get("release_train")
    release_train: dict[str, Any] = raw_release_train if isinstance(raw_release_train, dict) else {}
    compact_release = _compact_health_section(release_train) or {}
    actions = release_train.get("actions")
    if isinstance(actions, dict):
        compact_release["actions"] = _compact_health_section(actions)
    evidence = release_train.get("evidence")
    if isinstance(evidence, dict):
        compact_release["evidence"] = _compact_health_section(evidence)
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
    from ... import (
        aboyeur,
        care_cmd,
        care_receipts,
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
        update_notify,
    )

    target = target.expanduser().resolve()
    sections = _BriefSections()
    active = sections.call("active-session", lambda: helpers._active_session_info(target), None, key="active_session")
    session_rows = sections.call(
        "sessions",
        lambda: helpers._collect_sessions(helpers._work_root(target)),
        ([], 0),
        key="recent_sessions",
    )
    sessions, skipped = session_rows
    latest_session = helpers._session_info(sessions[0][0], sessions[0][1]) if sessions else None
    recent_sessions = [helpers._session_info(path, payload) for path, payload in sessions[:limit]]
    resolved = sections.call(
        "next-task",
        lambda: _resolve_next_task(target),
        {"task": "", "source": "default_review", "dogfood": {}, "ledger_task": None},
    )
    task_text = str(resolved.get("task") or "").strip()
    code_graph = (
        sections.call("code-graph", lambda: aboyeur.code_graph_brief(target, task_text), None)
        if include_code_graph and task_text and resolved.get("source") != "default_review"
        else None
    )
    ledger_task = resolved.get("ledger_task") if isinstance(resolved.get("ledger_task"), dict) else None
    git = sections.call("git", lambda: helpers._git_snapshot(target), {}, key="git")
    from ... import run_reap, runs_patrol

    orphaned_runs = sections.call("orphaned-runs", lambda: run_reap.list_orphaned_runs(target), [], key="orphaned_runs")
    patrol = sections.call("patrol", lambda: runs_patrol.brief_summary(target), {}, key="patrol")
    suggested = _suggested_command(active, resolved["task"], resolved["source"])
    pending = sections.call("pending-tasks", lambda: ledger_mod._pending_tasks(target), [], key="pending_tasks")
    ready_tasks = sections.call("ready-tasks", lambda: ledger_mod._ready_tasks(target), [], key="ready_tasks")
    readiness = sections.call(
        "readiness",
        lambda: ledger_mod._readiness_payload(target, explain=False),
        _UnavailableSection(),
        key="ready",
    )
    pending_imports = sections.call(
        "pending-imports", lambda: ledger_mod._pending_imports(target), [], key="pending_imports"
    )
    if "pending_imports" in sections.unavailable_keys:
        # Counting a fallback empty inbox would report zero pending imports
        # when the inbox was never read; the pending-imports check carries it.
        pending_import_counts = _UnavailableSection()
        sections.unavailable_keys.add("pending_import_counts")
    else:
        pending_import_counts = sections.call(
            "pending-import-counts",
            lambda: ledger_mod._import_counts(pending_imports),
            _UnavailableSection(),
            key="pending_import_counts",
        )
    scanner_candidate = sections.call("scanner-candidate", lambda: ledger_mod._scanner_candidate(pending_imports), None)
    handoff_candidate = sections.call("handoff-candidate", lambda: ledger_mod._handoff_candidate(pending_imports), None)
    inbox_hygiene = sections.call(
        "inbox-hygiene", lambda: services_mod._inbox_hygiene_payload(target), _UnavailableSection(), key="inbox_hygiene"
    )
    scanner_health = sections.call(
        "scanner-health", lambda: scanners_mod._scanner_health(target), _UnavailableSection(), key="scanner_health"
    )
    sweep_health = sections.call(
        "scanner-sweeps",
        lambda: scanners_mod._scanner_sweep_health(target),
        _UnavailableSection(),
        key="scanner_sweeps",
    )
    review_health = sections.call(
        "code-review-health", lambda: reviews_mod._review_health(target), _UnavailableSection(), key="code_review"
    )
    chat_health = sections.call(
        "chat-surfaces", lambda: chat_cmd.health(target), _UnavailableSection(), key="chat_surfaces"
    )
    memory_health = sections.call(
        "memory-care-health", lambda: memory_cmd.health(target), _UnavailableSection(), key="memory_care"
    )
    security_health = sections.call(
        "security-health",
        lambda: _security_health_for_brief(security_cmd, target),
        _UnavailableSection(),
        key="security_health",
    )
    backup_health = sections.call(
        "backup-health", lambda: config_mod._backup_health(target), _UnavailableSection(), key="backup_health"
    )
    tool_health = sections.call(
        "tool-catalog", lambda: tools_cmd.health(target), _UnavailableSection(), key="tool_catalog"
    )
    roadmap_health = sections.call(
        "roadmap-completion", lambda: roadmap_cmd.health(target), _UnavailableSection(), key="roadmap_completion"
    )
    # Reduce immediately: the raw fleet health embeds per-repo scan state that
    # scales with repo count; holding it (plus every other section payload)
    # until the return literal was the unbounded working set.
    repo_fleet_section = sections.call(
        "repo-fleet",
        lambda: _compact_repo_fleet_health(repos_cmd.health(target)),
        _UnavailableSection(),
        key="repo_fleet",
    )
    pantry_health = sections.call(
        "pantry", lambda: pantry_cmd.status_payload(target), _UnavailableSection(), key="pantry"
    )
    notification_health = sections.call(
        "notifications", lambda: notifications_cmd.health(target), _UnavailableSection(), key="notifications"
    )
    context_health = sections.call(
        "context-packs", lambda: context_cmd.health(target), _UnavailableSection(), key="context_packs"
    )
    projects_health = sections.call(
        "project-consolidation", lambda: projects_cmd.health(target), _UnavailableSection(), key="project_consolidation"
    )
    learning_health = sections.call("learning", lambda: learn_cmd.health(target), _UnavailableSection(), key="learning")
    research_health = sections.call(
        "research-handoffs", lambda: research_cmd.health(target), _UnavailableSection(), key="research_handoffs"
    )
    center_report_health = sections.call(
        "operator-report", lambda: center_cmd.report_health(target), _UnavailableSection(), key="operator_report"
    )
    center_actions_health = sections.call(
        "operator-actions", lambda: center_cmd.actions_health(target), _UnavailableSection(), key="operator_actions"
    )
    daily_health = sections.call(
        "daily-driver", lambda: daily_cmd.health(target), _UnavailableSection(), key="daily_driver"
    )
    phase_health = sections.call(
        "phase-ledger", lambda: phases_cmd.health(target), _UnavailableSection(), key="phase_ledger"
    )
    outcome_health = sections.call(
        "outcome-loop", lambda: outcome_cmd.health(target), _UnavailableSection(), key="outcome_loop"
    )
    outcome_ratchet = sections.call(
        "outcome-ratchet",
        lambda: outcome_ratchet_report.brief_payload(outcome_ratchet_report.load_report(target)),
        _UnavailableSection(),
        key="outcome_ratchet",
    )
    handoff_issues = sections.call(
        "handoff-issues", lambda: handoff_cmd.collect_issues(target), [], key="handoff_issues"
    )
    known_handoff_issue_ids = sections.call(
        "handoff-known-issues", lambda: handoff_cmd._known_local_issue_ids(target), set()
    )
    new_handoff_issues = [issue for issue in handoff_issues if issue.id not in known_handoff_issue_ids]
    handoff_drafts_full = sections.call(
        "handoff-drafts", lambda: handoff_cmd.draft_queue_payload(target), _UnavailableSection(), key="handoff_drafts"
    )
    handoff_drafts = {
        "counts": handoff_drafts_full.get("counts"),
        "issue_count": handoff_drafts_full.get("issue_count"),
        "top_issue": handoff_drafts_full.get("top_issue"),
        "latest_ingest_run": handoff_drafts_full.get("latest_ingest_run"),
        "drafts": (handoff_drafts_full.get("drafts") or [])[:limit],
    }
    del handoff_drafts_full
    from ... import brief_sections, work_mode

    pin_errors: list[str] = []
    sections.call("work-mode-pin", lambda: work_mode.pin_explicit_mode(target, errors=pin_errors), None)
    work = sections.call("work-mode", lambda: work_mode.summary(target), {"mode": "unknown", "rails": "unknown"})
    if pin_errors:
        work["pin_error"] = pin_errors[0]
    plan_coverage = sections.call(
        "plan-coverage", lambda: ledger_mod._plan_coverage_payload(target), _UnavailableSection(), key="plan_coverage"
    )
    scheduled_care = sections.call(
        "scheduled-care", lambda: care_cmd.repeated_failure_summary(target), _UnavailableSection(), key="scheduled_care"
    )
    care_schedule_health = sections.call(
        "care-schedule-health",
        lambda: care_receipts.schedule_health(target),
        _UnavailableSection(),
        key="care_schedule_health",
    )
    instruction_budget = sections.call(
        "instruction-budget",
        lambda: memory_care_instructions.brief_payload(target),
        _UnavailableSection(),
        key="instruction_budget",
    )
    cloud_tracker = sections.call(
        "cloud-tracker", lambda: _cloud_tracker_for_brief(target), _UnavailableSection(), key="cloud_tracker"
    )
    fleet_routing = sections.call("fleet-routing", lambda: _fleet_routing_for_brief(), None, key="fleet_routing")
    interactive_presence = sections.call(
        "interactive-presence",
        lambda: _interactive_presence_for_brief(target),
        _UnavailableSection(),
        key="interactive_sessions",
    )
    update = sections.call("update", lambda: update_notify.available_update(), None)
    payload: dict[str, Any] = {
        "target": str(target),
        "session_close": {
            "items": list(brief_sections.session_close_items(mode=work["mode"])),
        },
        "work_mode": work,
        "git": git,
        "active_session": active,
        "latest_session": latest_session,
        "recent_sessions": recent_sessions,
        "skipped_sessions": skipped,
        "tasks_path": str(helpers._tasks_path(target)),
        "pending_tasks": pending,
        "ready_tasks": ready_tasks,
        "ready": readiness,
        "plan_coverage": plan_coverage,
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
        "scheduled_care": scheduled_care,
        "care_schedule_health": care_schedule_health,
        "instruction_budget": instruction_budget,
        "security_health": {
            "config_path": security_health["config_path"],
            "valid": security_health["valid"],
            "issue_count": security_health["issue_count"],
            "top_issue": security_health["top_issue"],
            "top_finding": security_health["top_finding"],
            "suppression_cache": security_health.get("suppression_cache"),
            "wiring_scan_mode": security_health.get("harness_wiring", {}).get("scan_mode"),
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
            **repo_fleet_section,
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
            "drafts": (handoff_drafts.get("drafts") or [])[:limit],
        },
        "outcome_loop": {
            "records_path": outcome_health["records_path"],
            "verify_run_count": outcome_health["verify_run_count"],
            "record_count": outcome_health["record_count"],
            "legacy_records_audit_only": outcome_health["legacy_records_audit_only"],
            "legacy_records_note": outcome_health["legacy_records_note"],
            "scored_artifact_count": outcome_health["scored_artifact_count"],
            "promoted_count": outcome_health["promoted_count"],
            "attributed_receipt_count": outcome_health["attributed_receipt_count"],
            "unattributed_receipt_count": outcome_health["unattributed_receipt_count"],
            "eligible_receipt_count": outcome_health["eligible_receipt_count"],
            "ineligible_receipt_count": outcome_health["ineligible_receipt_count"],
            "attributed_ineligible_receipt_count": outcome_health["attributed_ineligible_receipt_count"],
            "ineligibility_rate": outcome_health["ineligibility_rate"],
            "leading_ineligibility_reason": outcome_health["leading_ineligibility_reason"],
            "registered_verify_manifest_ids": outcome_health["registered_verify_manifest_ids"],
            "exploration_bands": outcome_health["exploration_bands"],
            "latest_receipt_window": outcome_health["latest_receipt_window"],
            "issue_count": outcome_health["issue_count"],
            "top_issue": outcome_health["top_issue"],
        },
        "outcome_ratchet": outcome_ratchet,
        "dogfood": resolved["dogfood"],
        "next_source": resolved["source"],
        "task_id": resolved.get("task_id"),
        "next_task": ledger_mod._task_summary(ledger_task) if ledger_task else None,
        "next_issue": ledger_mod._task_issue_metadata(ledger_task) if ledger_task else None,
        "next": str(resolved["task"]),
        "suggested_command": suggested,
        "orphaned_runs": orphaned_runs,
        "patrol": patrol,
        "update": update,
        "cloud_tracker": cloud_tracker,
        "fleet_routing": fleet_routing,
        **interactive_presence,
        "status_section_checks": sections.checks,
        "status_section_issue_count": sections.issue_count,
        "top_status_section_issue": sections.top_issue,
    }
    # A section that timed out is named in ``status_section_checks`` instead of
    # pretending its packed fields are real. The text renderer skips absent
    # sections, so only the sections that completed are printed.
    for key in sections.unavailable_keys:
        payload.pop(key, None)
    return payload


def _interactive_presence_for_brief(target: Path) -> dict[str, Any]:
    from ... import fleet_client
    from ... import fleet_session_presence as presence

    session_id = (os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not session_id:
        return {"interactive_sessions": {"published": False}, "overlap_warnings": []}
    if not presence._hub_url_configured():
        return {
            "interactive_sessions": {"published": False, "status": "hub_unconfigured"},
            "overlap_warnings": [],
        }
    try:
        from ...fleet_session_heartbeat_state import publish_tracked
        from ...fleet_session_root import codex_session_root, session_repository_root

        cwd = codex_session_root(session_id) or Path.cwd()
        root = session_repository_root(cwd, harness="codex")
        snapshot = presence.build_snapshot(root, harness="codex", session_id=session_id)
        result = publish_tracked(
            snapshot, lambda: fleet_client.publish_session(snapshot), successful=lambda result: result.ok
        )
        published = bool(result.ok)
    except Exception as exc:
        return {
            "interactive_sessions": {"published": False, "error": presence._bounded_presence_error(exc)},
            "overlap_warnings": [],
        }
    interactive: dict[str, Any] = {"published": published}
    if not published:
        interactive["status"] = result.reason or "unpublished"
    try:
        rows = fleet_client.fetch_sessions()
    except Exception as exc:
        interactive["error"] = presence._bounded_presence_error(exc)
        return {"interactive_sessions": interactive, "overlap_warnings": []}
    try:
        # Warnings concern the requested checkout; this snapshot is never published.
        overlap = (
            snapshot if root == target else presence.build_snapshot(target, harness="codex", session_id=session_id)
        )
        warnings = presence.overlap_warnings(
            overlap,
            rows,
            current_node=fleet_client.resolve_node_id(target),
        )
    except Exception:
        warnings = []
    return {"interactive_sessions": interactive, "overlap_warnings": warnings}


def _cloud_tracker_for_brief(target: Path) -> dict[str, Any]:
    try:
        from ... import cloud_tracker

        return cloud_tracker.stale_entries_for_brief(target)
    except Exception as exc:  # noqa: BLE001 - brief stay fail-open
        return {
            "stale_count": 0,
            "stale_entries": [],
            "error": f"{type(exc).__name__}: {exc}",
            "suggested_command": "brigade run cloud status --json",
        }


def _fleet_routing_for_brief() -> dict[str, Any] | None:
    try:
        from . import fleet_routing

        return fleet_routing.fleet_routing_for_brief()
    except Exception:  # noqa: BLE001 - brief stays fail-open
        return None


def _print_bootstrap_line(level: str, name: str, detail: object) -> None:
    print(f"[{level}] {name}: {detail}")


def _outcome_ratchet_line(ratchet: object) -> str:
    """Return the one-line session-brief summary of the last advisory ratchet report."""
    report = ratchet if isinstance(ratchet, dict) else {}
    if not report.get("present"):
        return "outcome_ratchet: no report yet"
    ids = [brief_values.name(item) for item in report.get("regression_hold_ids") or []]
    raw_total = report.get("regression_holds")
    total = raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else len(ids)
    if ids:
        listed = ", ".join(ids)
        if total > len(ids):
            listed += f" +{total - len(ids)} more"
    elif total > 0:
        # The id list is bounded and the rows may be capped out; never say "none"
        # beside a nonzero hold count.
        listed = f"{total} not listed"
    else:
        listed = "none"
    return (
        f"outcome_ratchet: generated_at={brief_values.name(report.get('generated_at'))} "
        f"mode={brief_values.name(report.get('mode') or 'dry-run')} "
        f"regression_holds={brief_values.name(report.get('regression_holds'))} ({listed}) "
        f"ineligibility_rate={brief_values.name(report.get('ineligibility_rate'))} "
        f"leading={brief_values.name(report.get('leading_reason') or 'none')} "
        f"pending_physical_actions={brief_values.name(report.get('pending_physical_actions') or 0)}"
        + (
            f" stale={brief_values.name(report['stale_physical_actions'])}"
            if report.get("stale_physical_actions")
            else ""
        )
    )


def _brief_section_failed(payload: dict[str, Any], label: str) -> bool:
    """True when the brief recorded a non-ok check for ``label``."""
    checks = payload.get("status_section_checks")
    if not isinstance(checks, list):
        return False
    name = f"work_brief_section:{label}"
    return any(
        isinstance(check, dict) and check.get("name") == name and check.get("status") != "ok" for check in checks
    )


def _outcome_proposals_line(ratchet: object) -> str:
    """Return the one-line brief summary of inbox proposals filed by the last ratchet run."""
    report = ratchet if isinstance(ratchet, dict) else {}
    if not report.get("present"):
        return "outcome_proposals: no report yet"
    counts = report.get("outcome_proposals")
    if not isinstance(counts, dict):
        return "outcome_proposals: not requested (run `brigade outcome reconcile --proposals --report`)"
    keys = ("filed", "existing", "flagged", "skipped")
    return "outcome_proposals: " + " ".join(f"{key}={counts.get(key, 0)}" for key in keys)


def _top_issue_line(label: str, issue: object) -> str:
    """A health row's top issue: the name and the detail both encoded.

    A detail carries exception text, a stderr summary, a blocker reason or a path,
    so it is data even when Brigade assembled the sentence around it.
    """
    row = issue if isinstance(issue, dict) else {}
    return f"{label}: {brief_values.name(row.get('name'))} {brief_values.text(row.get('detail'))}"


def _next_line(payload: dict[str, Any]) -> str:
    """The next task text, encoded unless Brigade itself wrote the default review text."""
    raw = str(payload.get("next") or "")
    shown = helpers._short(raw) if payload.get("next_source") == "default_review" else brief_values.text(raw)
    return f"next: {shown}"


def _pin_error_line(raw: object) -> str:
    """The pin failure: Brigade's fixed prefix readable, the exception text encoded."""
    from ... import work_mode

    error = str(raw)
    if not error.startswith(work_mode.PIN_ERROR_PREFIX):
        return f"work_mode_pin: {brief_values.text(error)}"
    detail = error[len(work_mode.PIN_ERROR_PREFIX) :]
    return f"work_mode_pin: {work_mode.PIN_ERROR_PREFIX}{brief_values.text(detail)}"


def _pantry_line(pantry: dict[str, Any]) -> str:
    """The pantry summary with the external role, peer and last sync encoded in place."""
    shown = str(pantry.get("summary") or "")
    raw_status = pantry.get("status")
    status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
    raw_reported = status.get("stdout_json")
    reported: dict[str, Any] = raw_reported if isinstance(raw_reported, dict) else {}
    for field in ("role", "peer", "last_sync"):
        value = reported.get(field)
        if isinstance(value, str) and value and not brief_values.is_plain(value):
            shown = shown.replace(value, brief_values.name(value))
    return f"pantry: {shown}"


def _suggested_command_line(payload: dict[str, Any]) -> str:
    """The suggested command, or the withheld marker when its task text is not plain.

    `shlex.quote` rewrites a quote, so an unsafe token cannot be encoded in place; a
    task text that is not plain enough to survive quoting untouched withholds the
    command instead. The JSON payload keeps the real command.
    """
    command, untrusted = _suggested_command_parts(
        payload.get("active_session"), payload.get("next"), payload.get("next_source")
    )
    return f"suggested_command: {brief_values.command_text(command, untrusted)}"


def brief(*, target: Path, limit: int = 3, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2

    if json_output:
        payload = _brief_payload(target, limit=limit, include_code_graph=True)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    # One choke point for the text brief: every line printed here passes the stream
    # sanitizer, so a value a call site forgot to encode still cannot start a line,
    # hide text or reorder it. JSON is data and keeps its real values.
    with brief_values.sanitized_stdout():
        return _render_brief_text(target=target, limit=limit)


def _render_brief_text(*, target: Path, limit: int) -> int:
    # Print the header before collecting, so an outer timeout still leaves
    # a non-empty brief instead of a silent hang.
    print(f"work brief: {brief_values.name(target)}", flush=True)
    payload = _brief_payload(target, limit=limit, include_code_graph=False)

    from ... import brief_sections

    work_mode_payload = payload.get("work_mode")
    if not isinstance(work_mode_payload, dict):
        work_mode_payload = {"mode": "unknown", "rails": "unknown"}
    for line in brief_sections.render_brief_session_close_lines(mode=str(work_mode_payload.get("mode") or "unknown")):
        print(line)
    if "pin_error" in work_mode_payload:
        print(_pin_error_line(work_mode_payload["pin_error"]))
    if work_mode_payload.get("rails") == "off":
        print("safety_rails: off (pre-push content guard and secret scanning are skipped)")
    section_issues = payload.get("status_section_issue_count")
    if section_issues:
        print(f"status_section_issues: {section_issues}")
        top_section_issue = payload.get("top_status_section_issue")
        if isinstance(top_section_issue, dict):
            print(
                "top_status_section_issue: "
                f"{brief_values.code(str(top_section_issue.get('name') or ''))} "
                f"{brief_values.text(top_section_issue.get('detail'))}"
            )
    update = payload.get("update")
    if isinstance(update, dict):
        print(
            f"update_available: {brief_values.name(update.get('latest'))} "
            f'(installed {brief_values.name(update.get("installed"))}); run "brigade update"'
        )
    git = payload.get("git")
    if isinstance(git, dict) and git.get("available"):
        print(f"branch: {brief_values.name(git.get('branch'))}")
        raw_dirty = git.get("dirty_files")
        dirty: list[Any] = raw_dirty if isinstance(raw_dirty, list) else []
        print(f"dirty_files: {len(dirty)}")
        for item in dirty[:8]:
            print(f"  {brief_values.status_entry(item)}")
        if len(dirty) > 8:
            print(f"  ... {len(dirty) - 8} more")
    else:
        print("git: unavailable")
    warnings = payload.get("overlap_warnings")
    if isinstance(warnings, list):
        for warning in warnings[:3]:
            if not isinstance(warning, dict):
                continue
            raw_paths = warning.get("paths")
            paths: list[Any] = raw_paths if isinstance(raw_paths, list) else []
            shown = [brief_values.name(path) for path in paths[:8]]
            print(
                "overlap: "
                f"{brief_values.name(warning.get('node_id'))} {brief_values.name(warning.get('harness'))} "
                f"{brief_values.name(warning.get('session_id'))} {' '.join(shown)}"
            )

    active = payload.get("active_session")
    if isinstance(active, dict):
        if active.get("valid"):
            print(f"active_session: {brief_values.name(active.get('path'))}")
            if active.get("title"):
                print(f"active_session_title: {brief_values.text(active['title'])}")
        else:
            print(f"active_session: invalid ({brief_values.name(active.get('path'))})")
    elif _brief_section_failed(payload, "active-session"):
        print("active_session: unavailable (section timed out or failed)")
    else:
        print("active_session: none")

    latest_session = payload.get("latest_session")
    if isinstance(latest_session, dict):
        print(f"latest_session: {brief_values.name(latest_session.get('path'))}")
        if latest_session.get("title"):
            print(f"latest_session_title: {brief_values.text(latest_session['title'])}")
        if latest_session.get("note"):
            print(f"latest_session_note: {brief_values.text(latest_session['note'])}")
        if latest_session.get("handoff"):
            print(f"latest_session_handoff: {brief_values.text(latest_session['handoff'])}")
    elif _brief_section_failed(payload, "sessions"):
        print("latest_session: unavailable (section timed out or failed)")
    else:
        print(f"latest_session: none ({brief_values.name(helpers._work_root(target))})")

    dogfood = payload.get("dogfood")
    if not isinstance(dogfood, dict):
        dogfood = {}
    print(f"dogfood_ready: {dogfood.get('ready')}")
    if dogfood.get("error"):
        print(f"dogfood_error: {brief_values.text(dogfood['error'])}")
    latest_run = dogfood.get("latest_run")
    if isinstance(latest_run, dict):
        print(
            "latest_run: "
            f"{brief_values.name(latest_run.get('started_at', ''))} "
            f"[{brief_values.name(latest_run.get('status', 'unknown'))}] "
            f"{brief_values.name(latest_run.get('path'))}"
        )
        if latest_run.get("task"):
            print(f"latest_task: {brief_values.text(latest_run['task'])}")
    else:
        print("latest_run: none")

    routing = payload.get("fleet_routing")
    if isinstance(routing, dict):
        from . import fleet_routing

        fleet_routing.print_fleet_routing(routing)

    outcome_loop = payload.get("outcome_loop") if isinstance(payload.get("outcome_loop"), dict) else {}
    if outcome_loop:
        print(
            "outcome_loop: "
            f"verify_runs={outcome_loop.get('verify_run_count')} "
            f"attributed_receipts={outcome_loop.get('attributed_receipt_count')} "
            f"eligible={outcome_loop.get('eligible_receipt_count')} "
            f"ineligible={outcome_loop.get('ineligible_receipt_count')} "
            f"unattributed={outcome_loop.get('unattributed_receipt_count')} "
            f"ineligibility_rate={outcome_loop.get('ineligibility_rate')} "
            f"promoted={outcome_loop.get('promoted_count')}"
        )
        manifest_ids = outcome_loop.get("registered_verify_manifest_ids")
        if isinstance(manifest_ids, list):
            listed = ", ".join(brief_values.name(item) for item in manifest_ids) if manifest_ids else "none"
            print(f"outcome_verify_manifests: {listed}")
        bands = outcome_loop.get("exploration_bands") if isinstance(outcome_loop.get("exploration_bands"), dict) else {}
        if bands:
            print(
                "outcome_exploration_bands: "
                f"unseen={bands.get('unseen', 0)} "
                f"candidate={bands.get('candidate', 0)} "
                f"provisional={bands.get('provisional', 0)} "
                f"promoted={bands.get('promoted', 0)}"
            )
        top_outcome = outcome_loop.get("top_issue") if isinstance(outcome_loop.get("top_issue"), dict) else None
        if top_outcome:
            print(_top_issue_line("outcome_loop_issue", top_outcome))
    ratchet = payload.get("outcome_ratchet")
    if isinstance(ratchet, dict):
        print(_outcome_ratchet_line(ratchet))
        print(_outcome_proposals_line(ratchet))
    elif _brief_section_failed(payload, "outcome-ratchet"):
        print("outcome_ratchet: unavailable (section timed out or failed)")

    pantry = payload.get("pantry") if isinstance(payload.get("pantry"), dict) else {}
    if pantry:
        print(_pantry_line(pantry))
    notifications = payload.get("notifications") if isinstance(payload.get("notifications"), dict) else {}
    if notifications:
        print(
            f"notifications: {brief_values.name(notifications.get('status'))} "
            f"configured={notifications.get('configured')}"
        )
        top_notification = notifications.get("top_issue") if isinstance(notifications.get("top_issue"), dict) else None
        if top_notification:
            print(_top_issue_line("notifications_top_issue", top_notification))

    raw_cloud = payload.get("cloud_tracker")
    cloud: dict[str, Any] = raw_cloud if isinstance(raw_cloud, dict) else {}
    if cloud.get("stale_count"):
        print(
            f"cloud_stale: {cloud.get('stale_count')} ready beyond "
            f"{cloud.get('stale_ready_hours', '?')}h "
            f"(run `{cloud.get('suggested_command', 'brigade run cloud status --json')}`)"
        )
        for row in cloud.get("stale_entries") or []:
            if not isinstance(row, dict):
                continue
            branch = row.get("branch")
            if not branch and isinstance(row.get("artifact_refs"), dict):
                branch = (row.get("artifact_refs") or {}).get("branch")
            print(
                f"  - {brief_values.name(row.get('label'))} provider={brief_values.name(row.get('provider'))} "
                f"task={brief_values.name(row.get('task_id') or row.get('job_id'))} "
                f"branch={brief_values.name(branch)}"
            )

    print(f"next_source: {payload.get('next_source')}")
    if payload.get("task_id"):
        print(f"task_id: {brief_values.name(payload['task_id'])}")
    next_task = payload.get("next_task") if isinstance(payload.get("next_task"), dict) else None
    if next_task:
        print(f"next_type: {brief_values.name(next_task.get('type'))}")
        print(f"next_priority: {brief_values.name(next_task.get('priority'))}")
        if next_task.get("template"):
            print(f"next_template: {brief_values.name(next_task.get('template'))}")
        if next_task.get("acceptance_missing"):
            print("next_acceptance: missing")
        else:
            print(f"next_acceptance: {next_task.get('acceptance_count')}")
    next_issue = payload.get("next_issue") if isinstance(payload.get("next_issue"), dict) else None
    if next_issue:
        print(f"issue: {brief_values.name(next_issue.get('url') or next_issue.get('number'))}")
        if next_issue.get("state"):
            print(f"issue_state: {brief_values.name(next_issue['state'])}")
        labels = next_issue.get("labels")
        if isinstance(labels, list) and labels:
            print(f"issue_labels: {', '.join(brief_values.name(label) for label in labels)}")
    print(_next_line(payload))
    print(_suggested_command_line(payload))

    patrol = payload.get("patrol")
    if isinstance(patrol, dict):
        counts = patrol.get("counts", {})
        patrol_summary = (
            ", ".join(f"{brief_values.name(state)}={count}" for state, count in counts.items()) or "no local lanes"
        )
        print(f"patrol: {patrol_summary}" + (" (truncated)" if patrol.get("truncated") else ""))
    orphaned_runs = payload.get("orphaned_runs")
    if isinstance(orphaned_runs, list) and orphaned_runs:
        print(f"orphaned_runs: {len(orphaned_runs)}")
        for row in orphaned_runs:
            if not isinstance(row, dict):
                continue
            print(
                f"  {brief_values.name(row.get('run_id'))} "
                f"last={brief_values.name(row.get('last_observed_status'))} "
                f"dirty={row.get('uncommitted_change_count')}"
            )
            command = row.get("suggested_command")
            if isinstance(command, str) and command.strip():
                print(f"  {brief_values.command_text(command, row.get('run_id'))}")
        print("orphaned_trap: recorded dirty count only; inspect before assuming the tree is clean")

    pending = payload.get("pending_tasks")
    if isinstance(pending, list) and pending:
        print("pending_tasks:")
        for task in pending[:5]:
            if not isinstance(task, dict):
                continue
            summary = ledger_mod._task_summary(task)
            print(
                "  - "
                f"{brief_values.name(task.get('id'))} "
                f"[{brief_values.name(summary['type'])} {brief_values.name(summary['priority'])} "
                f"acceptance={summary['acceptance_count']}] "
                f"{brief_values.text(task.get('text', ''))}"
            )
        if len(pending) > 5:
            print(f"  ... {len(pending) - 5} more")

    plan_coverage = payload.get("plan_coverage")
    if isinstance(plan_coverage, dict):
        if plan_coverage.get("significant_without_plan"):
            ids = ", ".join(brief_values.name(item) for item in plan_coverage.get("task_ids", [])[:5])
            print(f"plans: {plan_coverage['significant_without_plan']} pending task(s) without a plan artifact ({ids})")
        elif not plan_coverage.get("pending_total"):
            print("plans: no pending tasks")
        else:
            print("plans: significant pending tasks have plan artifacts")

    pending_imports = payload.get("pending_imports")
    if isinstance(pending_imports, list) and pending_imports:
        counts = payload.get("pending_import_counts")
        if isinstance(counts, dict):
            print(f"pending_import_count: {counts.get('total', len(pending_imports))}")
            by_source = counts.get("by_source") if isinstance(counts.get("by_source"), dict) else {}
            if by_source:
                print("pending_imports_by_source:")
                for source, count in by_source.items():
                    print(f"  {brief_values.name(source)}: {count}")
            by_kind = counts.get("by_kind") if isinstance(counts.get("by_kind"), dict) else {}
            if by_kind:
                print("pending_imports_by_kind:")
                for kind, count in by_kind.items():
                    print(f"  {brief_values.name(kind)}: {count}")
        print("pending_imports:")
        for item in pending_imports[:5]:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or "unknown"
            kind = item.get("kind") or "task"
            print(
                f"  - {brief_values.name(item.get('id'))} [{brief_values.name(kind)}] "
                f"{brief_values.name(source)}: {brief_values.text(item.get('text', ''))}"
            )
        if len(pending_imports) > 5:
            print(f"  ... {len(pending_imports) - 5} more")
    scanner_candidate = payload.get("scanner_candidate")
    if isinstance(scanner_candidate, dict):
        print(f"scanner_next_import: {brief_values.name(scanner_candidate.get('id'))}")
        print(f"scanner_next_source: {brief_values.name(scanner_candidate.get('source'))}")
        print(f"scanner_next_kind: {brief_values.name(scanner_candidate.get('kind'))}")
        if scanner_candidate.get("kind") == "task":
            print(
                "scanner_next_task: "
                f"[{brief_values.name(scanner_candidate.get('type'))} "
                f"{brief_values.name(scanner_candidate.get('priority'))} "
                f"acceptance={scanner_candidate.get('acceptance_count')}] "
                f"{brief_values.text(scanner_candidate.get('text', ''))}"
            )
            print(f"scanner_next_command: brigade work import plan {brief_values.name(scanner_candidate.get('id'))}")
    handoff_candidate = payload.get("handoff_candidate")
    pending_tasks = payload.get("pending_tasks") if isinstance(payload.get("pending_tasks"), list) else []
    if isinstance(handoff_candidate, dict) and not pending_tasks:
        print(f"handoff_next_import: {brief_values.name(handoff_candidate.get('id'))}")
        print(f"handoff_next_source: {brief_values.name(handoff_candidate.get('source'))}")
        print(f"handoff_next_kind: {brief_values.name(handoff_candidate.get('kind'))}")
        print(f"handoff_next_target: {brief_values.name(handoff_candidate.get('target_document'))}")
        print(
            f"handoff_next_command: brigade work import plan-handoff {brief_values.name(handoff_candidate.get('id'))}"
        )

    inbox_hygiene = payload.get("inbox_hygiene") if isinstance(payload.get("inbox_hygiene"), dict) else {}
    if inbox_hygiene:
        issue_count = inbox_hygiene.get("issue_count")
        print(f"inbox_hygiene: {helpers._count_status(issue_count)}")
        top_inbox = inbox_hygiene.get("top_issue") if isinstance(inbox_hygiene.get("top_issue"), dict) else None
        if top_inbox:
            print(_top_issue_line("inbox_top_issue", top_inbox))

    raw_scanner_health = payload.get("scanner_health")
    scanner_health: dict[str, Any] = raw_scanner_health if isinstance(raw_scanner_health, dict) else {}
    scanner_checks = scanner_health.get("checks") if isinstance(scanner_health.get("checks"), list) else []
    if scanner_checks:
        warnings = [
            check for check in scanner_checks if isinstance(check, dict) and check.get("status") != constants.OK
        ]
        print(f"scanner_config: {brief_values.name(scanner_health.get('config_path'))}")
        print(f"scanner_health: {helpers._count_status(len(warnings), 'warning')}")
        next_scanner = scanner_health.get("next_run") if isinstance(scanner_health.get("next_run"), dict) else None
        if next_scanner:
            print(
                f"scanner_next_run: {brief_values.name(next_scanner.get('id'))} "
                f"{brief_values.name(next_scanner.get('start'))} "
                f"{brief_values.name(next_scanner.get('cadence'))}"
            )
        latest_scanner_run = (
            scanner_health.get("latest_run") if isinstance(scanner_health.get("latest_run"), dict) else None
        )
        if latest_scanner_run:
            print(
                "scanner_latest_run: "
                f"{brief_values.name(latest_scanner_run.get('scanner_id'))} "
                f"[{brief_values.name(latest_scanner_run.get('status'))}] "
                f"{brief_values.name(latest_scanner_run.get('run_id'))}"
            )
        due_scanners = scanner_health.get("due") if isinstance(scanner_health.get("due"), list) else []
        if due_scanners:
            print(
                "scanner_due: "
                f"{', '.join(brief_values.name(item.get('id')) for item in due_scanners[:5] if isinstance(item, dict))}"
            )

    raw_scanner_sweeps = payload.get("scanner_sweeps")
    scanner_sweeps: dict[str, Any] = raw_scanner_sweeps if isinstance(raw_scanner_sweeps, dict) else {}
    if scanner_sweeps:
        latest_sweep = scanner_sweeps.get("latest") if isinstance(scanner_sweeps.get("latest"), dict) else None
        if latest_sweep:
            print(
                f"scanner_latest_sweep: {brief_values.name(latest_sweep.get('sweep_id'))} "
                f"[{brief_values.name(latest_sweep.get('status'))}]"
            )
        if scanner_sweeps.get("suggested_command"):
            print(f"scanner_sweep_command: {scanner_sweeps.get('suggested_command')}")
        raw_review = scanner_sweeps.get("review")
        review: dict[str, Any] = raw_review if isinstance(raw_review, dict) else {}
        top_pending = review.get("top_pending_import") if isinstance(review.get("top_pending_import"), dict) else None
        if top_pending and latest_sweep:
            print(f"scanner_unreviewed_sweep: {brief_values.name(latest_sweep.get('sweep_id'))}")
            print(
                f"scanner_sweep_import: {brief_values.name(top_pending.get('id'))} "
                f"{brief_values.name(top_pending.get('source'))} "
                f"{brief_values.text(top_pending.get('text', ''))}"
            )
            print(f"scanner_sweep_review: brigade work sweep-review {brief_values.name(latest_sweep.get('sweep_id'))}")

    raw_chat_surfaces = payload.get("chat_surfaces")
    chat_surfaces: dict[str, Any] = raw_chat_surfaces if isinstance(raw_chat_surfaces, dict) else {}
    if chat_surfaces:
        print(f"chat_surfaces_config: {brief_values.name(chat_surfaces.get('config_path'))}")
        chat_issue_count = int(chat_surfaces.get("issue_count", 0) or 0)
        print(f"chat_surfaces_health: {helpers._count_status(chat_issue_count)}")
        top_chat = chat_surfaces.get("top_issue") if isinstance(chat_surfaces.get("top_issue"), dict) else None
        if top_chat:
            print(_top_issue_line("chat_surfaces_top_issue", top_chat))

    raw_memory_care = payload.get("memory_care")
    memory_care: dict[str, Any] = raw_memory_care if isinstance(raw_memory_care, dict) else {}
    if memory_care:
        print(f"memory_care_config: {brief_values.name(memory_care.get('config_path'))}")
        issue_count = memory_care.get("issue_count")
        print(f"memory_care_health: {helpers._count_status(issue_count)}")
        top_memory = memory_care.get("top_issue") if isinstance(memory_care.get("top_issue"), dict) else None
        if top_memory:
            memory_issue = brief_values.name(top_memory.get("issue_type") or top_memory.get("name"))
            memory_subject = brief_values.name(top_memory["file"]) if top_memory.get("file") else ""
            memory_subject = memory_subject or brief_values.text(top_memory.get("detail"))
            print(f"memory_care_top_issue: {memory_issue} {memory_subject}")
        raw_autofix_plan = memory_care.get("autofix_plan")
        autofix_plan: dict[str, Any] = raw_autofix_plan if isinstance(raw_autofix_plan, dict) else {}
        if autofix_plan.get("plan_count"):
            print(
                "memory_care_fix_plan: "
                f"planned={autofix_plan.get('plan_count')} "
                f"blocked={autofix_plan.get('blocked_count')} "
                f"command={autofix_plan.get('suggested_next_command')}"
            )

    raw_scheduled_care = payload.get("scheduled_care")
    scheduled_care: dict[str, Any] = raw_scheduled_care if isinstance(raw_scheduled_care, dict) else {}
    if scheduled_care:
        fail_count = int(scheduled_care.get("count") or 0)
        print(f"care_repeated_failures: {fail_count}")
        for rec in scheduled_care.get("entries") or []:
            if not isinstance(rec, dict):
                continue
            print(
                f"care_repeated_failure: {brief_values.name(rec.get('id'))} "
                f"consecutive={rec.get('consecutive_failures')}"
            )

    raw_schedule_health = payload.get("care_schedule_health")
    schedule_health: dict[str, Any] = raw_schedule_health if isinstance(raw_schedule_health, dict) else {}
    for warning in schedule_health.get("warnings") or []:
        if not isinstance(warning, dict):
            continue
        print(
            f"care_schedule_warning: {brief_values.name(warning.get('entry'))} "
            f"{warning.get('code')} "
            f"fix={brief_values.command_text(warning.get('fix_command'), warning.get('entry'))}"
        )

    raw_instruction_budget = payload.get("instruction_budget")
    instruction_budget: dict[str, Any] = raw_instruction_budget if isinstance(raw_instruction_budget, dict) else {}
    for line in instruction_budget.get("alerts") or []:
        print(line)

    raw_security_health = payload.get("security_health")
    security_health: dict[str, Any] = raw_security_health if isinstance(raw_security_health, dict) else {}
    if security_health:
        print(f"security_config: {brief_values.name(security_health.get('config_path'))}")
        if security_health.get("wiring_scan_mode"):
            print("security_wiring_scan: known-roots (full search: brigade security health)")
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
                f"{suppression_cache.get('status')} {brief_values.text(suppression_cache.get('detail'))}"
            )
            if suppression_cache.get("next_command"):
                command = brief_values.command_text(suppression_cache.get("next_command"), target)
                print(f"security_suppressions_next_command: {command}")
        top_security = (
            security_health.get("top_finding") if isinstance(security_health.get("top_finding"), dict) else None
        )
        if top_security:
            print(
                "security_top_finding: "
                f"{brief_values.name(top_security.get('id'))} "
                f"[{brief_values.name(top_security.get('severity'))}] "
                f"{brief_values.name(top_security.get('path'))}:{top_security.get('line')} "
                f"{brief_values.text(top_security.get('title', ''))}"
            )

    raw_backup_health = payload.get("backup_health")
    backup_health: dict[str, Any] = raw_backup_health if isinstance(raw_backup_health, dict) else {}
    if backup_health:
        print(f"backup_config: {brief_values.name(backup_health.get('config_path'))}")
        issue_count = backup_health.get("issue_count")
        print(f"backup_health: {helpers._count_status(issue_count)}")
        if backup_health.get("operator_summary"):
            print(f"backup_summary: {backup_health.get('operator_summary')}")
        top_backup = backup_health.get("top_issue") if isinstance(backup_health.get("top_issue"), dict) else None
        if top_backup:
            print(
                "backup_top_issue: "
                f"{brief_values.name(top_backup.get('destination'))}/"
                f"{brief_values.name(top_backup.get('issue_type'))} "
                f"{brief_values.text(top_backup.get('detail'))}"
            )

    raw_daily_driver = payload.get("daily_driver")
    daily_driver: dict[str, Any] = raw_daily_driver if isinstance(raw_daily_driver, dict) else {}
    if daily_driver:
        print(f"daily_config: {brief_values.name(daily_driver.get('config_path'))}")
        print(f"daily_driver: {helpers._count_status(daily_driver.get('issue_count'))}")
        latest_daily = daily_driver.get("latest_run") if isinstance(daily_driver.get("latest_run"), dict) else None
        if latest_daily:
            print(
                f"daily_latest_run: {brief_values.name(latest_daily.get('run_id'))} "
                f"[{brief_values.name(latest_daily.get('status'))}]"
            )
        top_daily = daily_driver.get("top_issue") if isinstance(daily_driver.get("top_issue"), dict) else None
        if top_daily:
            print(_top_issue_line("daily_top_issue", top_daily))
        raw_approvals = daily_driver.get("approvals")
        approvals: dict[str, Any] = raw_approvals if isinstance(raw_approvals, dict) else {}
        if approvals.get("pending_count"):
            raw_top_approval = approvals.get("top_pending")
            top_approval: dict[str, Any] = raw_top_approval if isinstance(raw_top_approval, dict) else {}
            print(
                f"daily_pending_approval: {brief_values.name(top_approval.get('approval_id'))} "
                f"{brief_values.text(top_approval.get('safe_summary', ''))}"
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
            print(
                f"phase_session: {brief_values.name(latest_phase_session.get('session_id'))} "
                f"[{brief_values.name(latest_phase_session.get('status'))}]"
            )
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
                f"phase_checkpoint: {brief_values.name(latest_checkpoint.get('checkpoint_id'))} "
                f"[{brief_values.name(latest_checkpoint.get('status'))}] issues={compare_count}"
            )
        top_phase = phase_ledger.get("top_issue") if isinstance(phase_ledger.get("top_issue"), dict) else None
        if top_phase:
            print(_top_issue_line("phase_top_issue", top_phase))
        top_phase_action = phase_ledger.get("top_action") if isinstance(phase_ledger.get("top_action"), dict) else None
        if top_phase_action:
            print(
                f"phase_top_action: {brief_values.name(top_phase_action.get('action_id'))} "
                f"{brief_values.text(top_phase_action.get('safe_summary', ''))}"
            )

    tool_catalog = payload.get("tool_catalog") if isinstance(payload.get("tool_catalog"), dict) else {}
    if tool_catalog:
        print(f"tool_config: {brief_values.name(tool_catalog.get('config_path'))}")
        issue_count = tool_catalog.get("issue_count")
        print(f"tool_catalog: {helpers._count_status(issue_count)}")
        top_tool = tool_catalog.get("top_issue") if isinstance(tool_catalog.get("top_issue"), dict) else None
        if top_tool:
            print(
                "tool_top_issue: "
                f"{brief_values.name(top_tool.get('tool_id') or 'catalog')}/"
                f"{brief_values.name(top_tool.get('issue_type'))} "
                f"{brief_values.text(top_tool.get('detail'))}"
            )
        call_queue = tool_catalog.get("call_queue") if isinstance(tool_catalog.get("call_queue"), dict) else {}
        if call_queue:
            print(f"tool_call_pending: {call_queue.get('pending_count', 0)}")
            call_top = call_queue.get("top_issue") if isinstance(call_queue.get("top_issue"), dict) else None
            if call_top:
                print(
                    "tool_call_top_issue: "
                    f"{brief_values.name(call_top.get('call_id'))} "
                    f"{brief_values.name(call_top.get('issue_type'))} "
                    f"{brief_values.text(call_top.get('detail'))}"
                )
        run_history = tool_catalog.get("run_history") if isinstance(tool_catalog.get("run_history"), dict) else {}
        if run_history:
            print(f"tool_runs: {run_history.get('run_count', 0)}")
            run_top = run_history.get("top_issue") if isinstance(run_history.get("top_issue"), dict) else None
            if run_top:
                print(
                    "tool_run_top_issue: "
                    f"{brief_values.name(run_top.get('run_id'))} "
                    f"{brief_values.name(run_top.get('issue_type'))} "
                    f"{brief_values.text(run_top.get('detail'))}"
                )
        checkpoints = tool_catalog.get("checkpoints") if isinstance(tool_catalog.get("checkpoints"), dict) else {}
        if checkpoints:
            print(f"tool_checkpoints: {checkpoints.get('checkpoint_count', 0)}")
            checkpoint_top = checkpoints.get("top_issue") if isinstance(checkpoints.get("top_issue"), dict) else None
            if checkpoint_top:
                print(
                    "tool_checkpoint_top_issue: "
                    f"{brief_values.name(checkpoint_top.get('checkpoint_id'))} "
                    f"{brief_values.name(checkpoint_top.get('issue_type'))} "
                    f"{brief_values.text(checkpoint_top.get('detail'))}"
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
            print(_top_issue_line("roadmap_top_issue", top_roadmap))

    repo_fleet = payload.get("repo_fleet") if isinstance(payload.get("repo_fleet"), dict) else {}
    if repo_fleet:
        print(f"repo_fleet_config: {brief_values.name(repo_fleet.get('config_path'))}")
        issue_count = repo_fleet.get("issue_count")
        print(f"repo_fleet: {helpers._count_status(issue_count)}")
        top_repo = repo_fleet.get("top_issue") if isinstance(repo_fleet.get("top_issue"), dict) else None
        if top_repo:
            print(_top_issue_line("repo_fleet_top_issue", top_repo))

    context_packs = payload.get("context_packs") if isinstance(payload.get("context_packs"), dict) else {}
    if context_packs:
        print(f"context_packs: {context_packs.get('pack_count', 0)}")
        if context_packs.get("issue_count"):
            top_context = context_packs.get("top_issue") if isinstance(context_packs.get("top_issue"), dict) else None
            if top_context:
                print(_top_issue_line("context_top_issue", top_context))

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
            print(_top_issue_line("project_consolidation_top_issue", top_project))

    learning = payload.get("learning") if isinstance(payload.get("learning"), dict) else {}
    if learning:
        print(f"learning_candidates: {learning.get('candidate_count', 0)}")
        top_learning = learning.get("top_issue") if isinstance(learning.get("top_issue"), dict) else None
        if top_learning:
            print(_top_issue_line("learning_top_issue", top_learning))

    research_handoffs = payload.get("research_handoffs") if isinstance(payload.get("research_handoffs"), dict) else {}
    if research_handoffs:
        print(f"research_handoffs: {helpers._count_status(research_handoffs.get('issue_count'))}")
        top_research = (
            research_handoffs.get("top_issue") if isinstance(research_handoffs.get("top_issue"), dict) else None
        )
        if top_research:
            print(
                f"research_handoff_top_issue: {brief_values.name(top_research.get('run_id'))} "
                f"{brief_values.name(top_research.get('status'))}"
            )
            if top_research.get("suggested_next_command"):
                command = brief_values.command_text(
                    top_research.get("suggested_next_command"), top_research.get("run_id")
                )
                print(f"research_handoff_command: {command}")

    operator_report = payload.get("operator_report") if isinstance(payload.get("operator_report"), dict) else {}
    if operator_report:
        latest_report = operator_report.get("latest") if isinstance(operator_report.get("latest"), dict) else None
        if latest_report:
            print(
                f"operator_report_latest: {brief_values.name(latest_report.get('report_id'))} "
                f"{brief_values.name(latest_report.get('created_at'))}"
            )
        issue_count = operator_report.get("issue_count")
        print(f"operator_report: {helpers._count_status(issue_count)}")
        top_report = operator_report.get("top_issue") if isinstance(operator_report.get("top_issue"), dict) else None
        if top_report:
            print(_top_issue_line("operator_report_top_issue", top_report))
            if top_report.get("suggested_next_command"):
                raw_latest = operator_report.get("latest")
                latest: dict[str, Any] = raw_latest if isinstance(raw_latest, dict) else {}
                report_id = latest.get("report_id")
                command = brief_values.command_text(
                    top_report.get("suggested_next_command"), report_id, top_report.get("report_id")
                )
                print(f"operator_report_command: {command}")

    operator_actions = payload.get("operator_actions") if isinstance(payload.get("operator_actions"), dict) else {}
    if operator_actions:
        print(f"operator_actions: {operator_actions.get('open_count', 0)} open")
        top_action = (
            operator_actions.get("top_action") if isinstance(operator_actions.get("top_action"), dict) else None
        )
        if top_action:
            print(
                f"operator_action_top: {brief_values.name(top_action.get('action_id'))} "
                f"{brief_values.name(top_action.get('source_group'))} "
                f"{brief_values.text(top_action.get('safe_summary', ''))}"
            )
            if top_action.get("suggested_command"):
                command = brief_values.command_text(top_action.get("suggested_command"), top_action.get("action_id"))
                print(f"operator_action_command: {command}")

    code_review = payload.get("code_review")
    if isinstance(code_review, dict):
        latest_review = code_review.get("latest_run") if isinstance(code_review.get("latest_run"), dict) else None
        if latest_review:
            print(
                f"review_latest: {brief_values.name(latest_review.get('run_id'))} "
                f"{brief_values.name(latest_review.get('reviewer_id'))} "
                f"[{brief_values.name(latest_review.get('status'))}]"
            )
        unclosed_review = (
            code_review.get("latest_unclosed_run") if isinstance(code_review.get("latest_unclosed_run"), dict) else None
        )
        if unclosed_review:
            print(
                f"review_unclosed: {brief_values.name(unclosed_review.get('run_id'))} "
                f"{brief_values.name(unclosed_review.get('reviewer_id'))}"
            )
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
            print(
                f"review_top_finding: {brief_values.name(finding_id)} {brief_values.text(top_review.get('text', ''))}"
            )
            print(f"review_top_command: brigade work review finding-show {brief_values.name(finding_id)}")

    handoff_issues = payload.get("handoff_issues")
    if isinstance(handoff_issues, dict) and handoff_issues.get("count"):
        print(f"handoff_ingest_issues_new: {handoff_issues.get('count')}")
        by_category = handoff_issues.get("by_category")
        if isinstance(by_category, dict) and by_category:
            print("handoff_ingest_issues_by_category:")
            for category, count in by_category.items():
                print(f"  {brief_values.name(category)}: {count}")
    if isinstance(handoff_issues, dict) and handoff_issues.get("known_count"):
        print(f"handoff_ingest_issues_known: {handoff_issues.get('known_count')}")
    handoff_drafts = payload.get("handoff_drafts")
    if isinstance(handoff_drafts, dict):
        raw_draft_counts = handoff_drafts.get("counts")
        draft_counts: dict[str, Any] = raw_draft_counts if isinstance(raw_draft_counts, dict) else {}
        total = int(draft_counts.get("total", 0) or 0)
        if total:
            print(f"handoff_drafts_pending: {draft_counts.get('pending', 0)}")
            print(f"handoff_drafts_reviewed: {draft_counts.get('reviewed', 0)}")
            latest_ingest = (
                handoff_drafts.get("latest_ingest_run")
                if isinstance(handoff_drafts.get("latest_ingest_run"), dict)
                else None
            )
            if latest_ingest:
                print(
                    f"handoff_ingest_latest: {brief_values.name(latest_ingest.get('run_id'))} "
                    f"completed={brief_values.name(latest_ingest.get('completed_at'))}"
                )
            top_issue = handoff_drafts.get("top_issue") if isinstance(handoff_drafts.get("top_issue"), dict) else None
            if top_issue:
                print(_top_issue_line("handoff_draft_top_issue", top_issue))
            drafts = handoff_drafts.get("drafts") if isinstance(handoff_drafts.get("drafts"), list) else []
            if drafts:
                first = drafts[0]
                print(
                    f"handoff_draft_next: {brief_values.name(first.get('id'))} "
                    f"{brief_values.name(first.get('status'))} {brief_values.name(first.get('path'))}"
                )
                print(f"handoff_draft_next_command: brigade handoff show {brief_values.name(first.get('id'))}")

    recent = payload.get("recent_sessions")
    if isinstance(recent, list) and recent:
        print("recent_sessions:")
        for item in recent:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("id")
            print(
                f"  - {brief_values.name(item.get('started_at'))} "
                f"[{brief_values.name(item.get('status'))}] {brief_values.text(title)}"
            )
    if payload.get("skipped_sessions"):
        print(f"skipped_sessions: {payload['skipped_sessions']}", file=sys.stderr)
    return 0
