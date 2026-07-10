"""Local release readiness receipts."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    context_cmd,
    handoff_cmd,
    learn_cmd,
    memory_cmd,
    phases_cmd,
    projects_cmd,
    repos_cmd,
    reportstore,
    research_cmd,
    roadmap_cmd,
    scrub,
    security_cmd,
    tools_cmd,
    work_cmd,
)
from ..selection import KNOWN_HARNESSES
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)

from . import paths as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _read_release_receipt(path: Path) -> dict[str, Any] | None:
    receipt = path / "receipt.json" if path.is_dir() else path
    payload = _read_json(receipt)
    if payload is not None:
        payload.setdefault("path", str(receipt.parent))
    return payload


def _release_receipts(target: Path) -> list[dict[str, Any]]:
    root = _release_runs_root(target)
    if not root.is_dir():
        return []
    receipts = [_read_release_receipt(path) for path in root.iterdir() if path.is_dir()]
    valid = [item for item in receipts if isinstance(item, dict)]
    valid.sort(key=lambda item: str(item.get("started_at") or item.get("run_id") or ""), reverse=True)
    return valid


def _latest_release_receipt(target: Path) -> dict[str, Any] | None:
    receipts = _release_receipts(target)
    return receipts[0] if receipts else None


def _resolve_release_receipt(target: Path, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _release_receipts(target)
    if run_id == "latest":
        return (receipts[0], None) if receipts else (None, "release run not found: latest")
    matches = [item for item in receipts if str(item.get("run_id") or "").startswith(run_id)]
    if not matches:
        return None, f"release run not found: {run_id}"
    if len(matches) > 1:
        return None, f"release run id is ambiguous: {run_id}"
    return matches[0], None


def _read_candidate(path: Path) -> dict[str, Any] | None:
    return reportstore.read_bundle(path, "EVIDENCE.json")


def _release_candidates(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_release_candidates_root(target)]
    if include_archived:
        roots.append(_release_candidates_archive_root(target))
    return reportstore.list_bundles(
        roots, _read_candidate, id_field="candidate_id", skip_child=lambda name: name == "archive"
    )


def _latest_candidate(target: Path) -> dict[str, Any] | None:
    candidates = _release_candidates(target)
    return candidates[0] if candidates else None


def _phase_release_checks(target: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    health = phases_cmd.health(target)
    latest_closeout = health.get("latest_closeout") if isinstance(health.get("latest_closeout"), dict) else None
    latest_report = health.get("latest_report") if isinstance(health.get("latest_report"), dict) else None
    if latest_closeout is not None and int(latest_closeout.get("unresolved_issue_count") or 0) > 0:
        checks.append(
            {
                "status": WARN,
                "name": "phase_ledger_unresolved_closeout",
                "detail": str(latest_closeout.get("closeout_id")),
            }
        )
    latest_report_compare = (
        health.get("latest_report_compare") if isinstance(health.get("latest_report_compare"), dict) else None
    )
    if latest_report_compare is not None and int(latest_report_compare.get("issue_count") or 0) > 0:
        top_compare = (
            latest_report_compare.get("top_issue") if isinstance(latest_report_compare.get("top_issue"), dict) else {}
        )
        checks.append(
            {
                "status": WARN,
                "name": "phase_ledger_report_compare_issue",
                "detail": str(top_compare.get("name") or latest_report_compare.get("issue_count")),
            }
        )
    latest_session = health.get("latest_session") if isinstance(health.get("latest_session"), dict) else None
    latest_session_checkpoint = (
        health.get("latest_session_checkpoint") if isinstance(health.get("latest_session_checkpoint"), dict) else None
    )
    latest_session_checkpoint_compare = (
        health.get("latest_session_checkpoint_compare")
        if isinstance(health.get("latest_session_checkpoint_compare"), dict)
        else None
    )
    latest_session_report = (
        health.get("latest_session_report") if isinstance(health.get("latest_session_report"), dict) else None
    )
    latest_session_gate = (
        health.get("latest_session_gate") if isinstance(health.get("latest_session_gate"), dict) else None
    )
    if latest_session and latest_session.get("status") not in {"closed", "archived"}:
        checks.append({"status": WARN, "name": "phase_session_active", "detail": str(latest_session.get("session_id"))})
        if latest_session_report is None:
            checks.append(
                {
                    "status": WARN,
                    "name": "phase_session_missing_report",
                    "detail": str(latest_session.get("session_id")),
                }
            )
        if latest_session.get("closeout_status") is None and latest_session.get("current_phase_id") is None:
            checks.append(
                {
                    "status": WARN,
                    "name": "phase_session_missing_closeout",
                    "detail": str(latest_session.get("session_id")),
                }
            )
    if latest_session_gate and not latest_session_gate.get("safe_to_claim_complete"):
        top_blocker = (
            latest_session_gate.get("top_blocker") if isinstance(latest_session_gate.get("top_blocker"), dict) else {}
        )
        checks.append(
            {
                "status": WARN,
                "name": "phase_session_gate_blocked",
                "detail": str(top_blocker.get("name") or latest_session_gate.get("blocker_count")),
            }
        )
    if latest_session_checkpoint and latest_session_checkpoint.get("status") == "blocked":
        checks.append(
            {
                "status": WARN,
                "name": "phase_session_checkpoint_blocked",
                "detail": str(latest_session_checkpoint.get("checkpoint_id")),
            }
        )
    if latest_session_checkpoint_compare and int(latest_session_checkpoint_compare.get("issue_count") or 0) > 0:
        top_checkpoint = (
            latest_session_checkpoint_compare.get("top_issue")
            if isinstance(latest_session_checkpoint_compare.get("top_issue"), dict)
            else {}
        )
        checks.append(
            {
                "status": WARN,
                "name": "phase_session_checkpoint_compare_issue",
                "detail": str(top_checkpoint.get("name") or latest_session_checkpoint_compare.get("issue_count")),
            }
        )
    if int(health.get("open_action_count") or 0) > 0:
        checks.append(
            {"status": WARN, "name": "phase_session_unresolved_actions", "detail": str(health.get("open_action_count"))}
        )
    records = phases_cmd._records(target)
    pushed_without_closeout = []
    for record in records:
        if record.get("status") != "pushed":
            continue
        phase_id = str(record.get("phase_id") or "")
        if phase_id and not phases_cmd._phase_has_current_closeout(target, phase_id, record):
            pushed_without_closeout.append(phase_id)
    if pushed_without_closeout:
        checks.append(
            {
                "status": WARN,
                "name": "phase_ledger_unreviewed_pushed_phase",
                "detail": ", ".join(pushed_without_closeout[:5]),
            }
        )
    if records:
        record_times = [
            parsed
            for parsed in (
                work_cmd._parse_iso_datetime(
                    record.get("updated_at") or record.get("completed_at") or record.get("created_at")
                )
                for record in records
            )
            if parsed is not None
        ]
        latest_record_time = max(record_times) if record_times else None
        report_time = work_cmd._parse_iso_datetime(latest_report.get("created_at")) if latest_report else None
        if latest_report is None:
            checks.append(
                {"status": WARN, "name": "phase_ledger_missing_report", "detail": "no phase ledger report found"}
            )
        elif latest_record_time and report_time and latest_record_time > report_time:
            checks.append(
                {"status": WARN, "name": "phase_ledger_stale_report", "detail": str(latest_report.get("report_id"))}
            )
        elif report_time and _now() - report_time > timedelta(hours=PHASE_REPORT_STALE_HOURS):
            checks.append(
                {"status": WARN, "name": "phase_ledger_stale_report", "detail": str(latest_report.get("report_id"))}
            )
    return checks


def _resolve_candidate(target: Path, candidate_id: str) -> tuple[dict[str, Any] | None, str | None]:
    candidates = _release_candidates(target, include_archived=True)
    return reportstore.resolve_bundle(
        candidates,
        candidate_id,
        id_field="candidate_id",
        label="release candidate",
        latest=lambda: _latest_candidate(target),
    )


def _evidence(target: Path, *, base_ref: str | None) -> dict[str, Any]:
    from .. import center_cmd, daily_cmd

    sweep = work_cmd._scanner_sweep_health(target)
    review = work_cmd._review_health(target)
    handoffs = handoff_cmd.draft_queue_payload(target)
    research_handoffs = research_cmd.health(target)
    context_health = context_cmd.health(target)
    learning_health = learn_cmd.health(target)
    projects_health = projects_cmd.health(target)
    repo_health = repos_cmd.health(target)
    repo_daily_use = repos_cmd.daily_use_health(target)
    roadmap_health = roadmap_cmd.health(target)
    dogfood_health = _release_dogfood_health(target)
    tool_health = tools_cmd.health(target)
    memory_health = memory_cmd.health(target)
    backup_health = work_cmd._backup_health(target)
    acceptance = work_cmd._acceptance_payload(target)
    inbox_quality = work_cmd._inbox_quality_payload(target)
    phase_ledger = phases_cmd.health(target)
    operator_report_health = center_cmd.report_health(target)
    operator_actions_health = center_cmd.actions_health(target)
    ci_platform = ci_platform_payload(target)
    install_smoke = install_smoke_health(target)
    return {
        "git": _git_state(target),
        "latest_work_closeout": _latest_work_closeout(target),
        "latest_verification": work_cmd._latest_verify_receipt(target),
        "latest_review_closeout": _latest_review_closeout(target),
        "scanner_sweep": {
            "latest": sweep.get("latest"),
            "review": sweep.get("review"),
            "due_count": sweep.get("due_count"),
        },
        "code_review": {
            "latest_run": review.get("latest_run"),
            "latest_unclosed_run": review.get("latest_unclosed_run"),
            "unresolved_finding_count": review.get("unresolved_finding_count"),
            "top_unresolved_finding": review.get("top_unresolved_finding"),
        },
        "security": _security_summary(target),
        "ci_platform": {
            "status": ci_platform.get("status"),
            "issue_count": ci_platform.get("issue_count"),
            "top_issue": ci_platform.get("top_issue"),
            "findings": ci_platform.get("findings"),
            "logs": ci_platform.get("logs"),
            "workflows": ci_platform.get("workflows"),
        },
        "install_smoke": {
            "issue_count": install_smoke.get("issue_count"),
            "top_issue": install_smoke.get("top_issue"),
            "matrix_count": install_smoke.get("matrix_count"),
            "receipt_count": install_smoke.get("receipt_count"),
            "latest_by_matrix": install_smoke.get("latest_by_matrix"),
            "issues": install_smoke.get("issues"),
        },
        "handoff_drafts": {
            "counts": handoffs.get("counts"),
            "issue_count": handoffs.get("issue_count"),
            "top_issue": handoffs.get("top_issue"),
            "latest_ingest_run": handoffs.get("latest_ingest_run"),
            "latest_closeout": _latest_closeout_json(target / ".brigade" / "handoffs" / "closeouts"),
        },
        "research_handoffs": {
            "run_count": research_handoffs.get("run_count"),
            "issue_count": research_handoffs.get("issue_count"),
            "top_issue": research_handoffs.get("top_issue"),
        },
        "backup": {
            "valid": backup_health.get("valid"),
            "issue_count": backup_health.get("issue_count"),
            "raw_issue_count": backup_health.get("raw_issue_count"),
            "quieted_issue_count": backup_health.get("quieted_issue_count"),
            "restore_rehearsal_issue_count": backup_health.get("restore_rehearsal_issue_count"),
            "changed_fingerprint_count": backup_health.get("changed_fingerprint_count"),
            "operator_summary": backup_health.get("operator_summary"),
            "top_issue": backup_health.get("top_issue"),
            "restore_rehearsal_issues": backup_health.get("restore_rehearsal_issues"),
            "latest_closeout": backup_health.get("latest_closeout"),
        },
        "tool_catalog": {
            "valid": tool_health.get("valid"),
            "issue_count": tool_health.get("issue_count"),
            "raw_issue_count": tool_health.get("raw_issue_count"),
            "top_issue": tool_health.get("top_issue"),
            "packs": tool_health.get("packs"),
            "parity": tool_health.get("parity"),
            "sync_plan": tool_health.get("sync_plan"),
            "call_queue": tool_health.get("call_queue"),
            "run_history": tool_health.get("run_history"),
            "checkpoints": tool_health.get("checkpoints"),
        },
        "task_acceptance": {
            "coverage": acceptance.get("coverage"),
            "issue_count": acceptance.get("issue_count"),
            "top_issue": acceptance.get("top_issue"),
            "pending_with_acceptance": acceptance.get("pending_with_acceptance"),
            "pending_missing_acceptance": acceptance.get("pending_missing_acceptance"),
            "done_with_completion": acceptance.get("done_with_completion"),
            "done_missing_completion": acceptance.get("done_missing_completion"),
            "done_missing_completed_acceptance": acceptance.get("done_missing_completed_acceptance"),
            "review_findings": acceptance.get("review_findings"),
            "latest_work_closeout": acceptance.get("latest_work_closeout"),
            "issues": acceptance.get("issues"),
        },
        "inbox_quality": {
            "pending_count": inbox_quality.get("pending_count"),
            "issue_count": inbox_quality.get("issue_count"),
            "issue_counts": inbox_quality.get("issue_counts"),
            "top_issue": inbox_quality.get("top_issue"),
            "best_import": inbox_quality.get("best_import"),
            "noisy_sources": inbox_quality.get("noisy_sources"),
        },
        "memory_care": {
            "valid": memory_health.get("valid"),
            "issue_count": memory_health.get("issue_count"),
            "top_issue": memory_health.get("top_issue"),
            "latest_closeout": memory_health.get("latest_closeout"),
        },
        "context": {
            "pack_count": context_health.get("pack_count"),
            "issue_count": context_health.get("issue_count"),
            "top_issue": context_health.get("top_issue"),
            "latest": context_health.get("latest"),
            "sync": context_health.get("sync"),
        },
        "projects": {
            "project_count": projects_health.get("project_count"),
            "issue_count": projects_health.get("issue_count"),
            "top_issue": projects_health.get("top_issue"),
            "readiness": projects_health.get("readiness"),
            "closeout": projects_health.get("closeout"),
        },
        "learning": {
            "candidate_count": learning_health.get("candidate_count"),
            "raw_candidate_count": learning_health.get("raw_candidate_count"),
            "quieted_candidate_count": learning_health.get("quieted_candidate_count"),
            "changed_fingerprint_count": learning_health.get("changed_fingerprint_count"),
            "issue_count": learning_health.get("issue_count"),
            "top_issue": learning_health.get("top_issue"),
            "latest_closeout": learning_health.get("latest_closeout"),
            "replay": learning_health.get("replay"),
        },
        "repo_fleet": {
            "repo_count": repo_health.get("repo_count"),
            "issue_count": repo_health.get("issue_count"),
            "top_issue": repo_health.get("top_issue"),
            "report": repo_health.get("report"),
            "actions": repo_health.get("actions"),
            "sweep": repo_health.get("sweep"),
            "release_train": repo_health.get("release_train"),
        },
        "repo_fleet_daily_use": {
            "repo_count": repo_daily_use.get("repo_count"),
            "issue_count": repo_daily_use.get("issue_count"),
            "top_issue": repo_daily_use.get("top_issue"),
            "report_issue_count": repo_daily_use.get("report_issue_count"),
            "action_issue_count": repo_daily_use.get("action_issue_count"),
            "sweep_issue_count": repo_daily_use.get("sweep_issue_count"),
            "release_train_issue_count": repo_daily_use.get("release_train_issue_count"),
            "manual_only": repo_daily_use.get("manual_only"),
            "privacy": repo_daily_use.get("privacy"),
        },
        "roadmap": {
            "issue_count": roadmap_health.get("issue_count"),
            "top_issue": roadmap_health.get("top_issue"),
        },
        "phase_ledger": {
            "record_count": phase_ledger.get("record_count"),
            "open_count": phase_ledger.get("open_count"),
            "issue_count": phase_ledger.get("issue_count"),
            "top_issue": phase_ledger.get("top_issue"),
            "latest": phase_ledger.get("latest"),
            "latest_closeout": phase_ledger.get("latest_closeout"),
            "latest_report": phase_ledger.get("latest_report"),
            "latest_report_compare": phase_ledger.get("latest_report_compare"),
            "latest_session": phase_ledger.get("latest_session"),
            "latest_session_checkpoint": phase_ledger.get("latest_session_checkpoint"),
            "latest_session_checkpoint_compare": phase_ledger.get("latest_session_checkpoint_compare"),
            "latest_session_gate": phase_ledger.get("latest_session_gate"),
            "latest_session_report": phase_ledger.get("latest_session_report"),
            "closeout_count": phase_ledger.get("closeout_count"),
        },
        "operator_report": {
            "issue_count": operator_report_health.get("issue_count"),
            "top_issue": operator_report_health.get("top_issue"),
            "latest": operator_report_health.get("latest"),
            "latest_diff": operator_report_health.get("latest_diff"),
        },
        "operator_center_contract": {
            key: value
            for key, value in center_cmd._center_contract_health(target).items()
            if key
            in {
                "schema_version",
                "schema",
                "required_schema_ids",
                "schema_ids",
                "missing_schema_ids",
                "required_item_fields",
                "activity_count",
                "review_count",
                "template_count",
                "issue_count",
                "top_issue",
            }
        },
        "operator_actions": {
            "action_count": operator_actions_health.get("action_count"),
            "open_count": operator_actions_health.get("open_count"),
            "top_action": operator_actions_health.get("top_action"),
            "issue_count": operator_actions_health.get("issue_count"),
            "top_issue": operator_actions_health.get("top_issue"),
        },
        "daily_driver": {
            "health": daily_cmd.health(target),
            "latest_run": daily_cmd._latest_run(target),
            "latest_plan": daily_cmd._latest_plan(target),
            "telemetry": daily_cmd.telemetry_payload(target).get("metrics"),
        },
        "daily_hardening": {
            "audit": {
                key: value
                for key, value in daily_cmd.hardening_audit_payload(target).items()
                if key
                in {
                    "phase_range",
                    "phase_count",
                    "implemented_phase_count",
                    "finding_count",
                    "raw_finding_count",
                    "quieted_count",
                    "top_issue",
                    "workstreams",
                }
            },
            "latest_closeout": daily_cmd._latest_hardening_closeout(target),
        },
        "release_dogfood": {
            "issue_count": dogfood_health.get("issue_count"),
            "top_issue": dogfood_health.get("top_issue"),
            "latest_readiness": dogfood_health.get("latest_readiness"),
            "latest_candidate": dogfood_health.get("latest_candidate"),
            "latest_daily_run": dogfood_health.get("latest_daily_run"),
        },
        "security_closeout": _latest_closeout_json(target / ".brigade" / "security" / "closeouts"),
        "docs": {
            "base_ref": base_ref,
            "changed_files": _changed_files(target, base_ref),
        },
    }


def _assess(
    evidence: dict[str, Any], checks: list[dict[str, Any]], docs_warnings: list[str]
) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings = list(docs_warnings)
    git = evidence.get("git") if isinstance(evidence.get("git"), dict) else {}
    if git.get("tracked_dirty_count"):
        blockers.append(f"tracked files are dirty: {git.get('tracked_dirty_count')}")
    closeout = evidence.get("latest_work_closeout") if isinstance(evidence.get("latest_work_closeout"), dict) else None
    if closeout is None:
        blockers.append("missing work closeout")
    elif not closeout.get("ready"):
        blockers.append(f"latest work closeout is not ready: {closeout.get('closeout_id')}")
    verify = evidence.get("latest_verification") if isinstance(evidence.get("latest_verification"), dict) else None
    if verify is None:
        blockers.append("missing verification receipt")
    elif verify.get("status") != "completed":
        blockers.append(f"latest verification did not complete: {verify.get('run_id')}")
    review = evidence.get("code_review") if isinstance(evidence.get("code_review"), dict) else {}
    if review.get("latest_unclosed_run"):
        run = review["latest_unclosed_run"]
        blockers.append(f"review run is not closed out: {run.get('run_id') if isinstance(run, dict) else run}")
    if int(review.get("unresolved_finding_count") or 0) > 0:
        blockers.append(f"code review has unresolved finding(s): {review.get('unresolved_finding_count')}")
    task_acceptance = evidence.get("task_acceptance") if isinstance(evidence.get("task_acceptance"), dict) else {}
    if int(task_acceptance.get("issue_count") or 0) > 0:
        top_acceptance = task_acceptance.get("top_issue") if isinstance(task_acceptance.get("top_issue"), dict) else {}
        blockers.append(
            f"task acceptance has issue(s): {top_acceptance.get('detail') or task_acceptance.get('issue_count')}"
        )
    sweep = evidence.get("scanner_sweep") if isinstance(evidence.get("scanner_sweep"), dict) else {}
    sweep_review = sweep.get("review") if isinstance(sweep.get("review"), dict) else {}
    if int(sweep_review.get("issue_count") or 0) > 0:
        blockers.append(f"scanner sweep has unresolved issue(s): {sweep_review.get('issue_count')}")
    security = evidence.get("security") if isinstance(evidence.get("security"), dict) else {}
    if int(security.get("issue_count") or 0) > 0:
        blockers.append(f"security has open issue(s): {security.get('issue_count')}")
    ci_platform = evidence.get("ci_platform") if isinstance(evidence.get("ci_platform"), dict) else {}
    if int(ci_platform.get("issue_count") or 0) > 0:
        top_ci = ci_platform.get("top_issue") if isinstance(ci_platform.get("top_issue"), dict) else {}
        warnings.append(
            f"ci platform deprecation warning(s): {top_ci.get('safe_excerpt') or ci_platform.get('issue_count')}"
        )
    install_smoke = evidence.get("install_smoke") if isinstance(evidence.get("install_smoke"), dict) else {}
    if int(install_smoke.get("issue_count") or 0) > 0:
        top_smoke = install_smoke.get("top_issue") if isinstance(install_smoke.get("top_issue"), dict) else {}
        warnings.append(f"install smoke matrix issue(s): {top_smoke.get('detail') or install_smoke.get('issue_count')}")
    handoffs = evidence.get("handoff_drafts") if isinstance(evidence.get("handoff_drafts"), dict) else {}
    if int(handoffs.get("issue_count") or 0) > 0:
        blockers.append(f"handoff draft queue has issue(s): {handoffs.get('issue_count')}")
    research_handoffs = evidence.get("research_handoffs") if isinstance(evidence.get("research_handoffs"), dict) else {}
    if int(research_handoffs.get("issue_count") or 0) > 0:
        top_research = (
            research_handoffs.get("top_issue") if isinstance(research_handoffs.get("top_issue"), dict) else {}
        )
        warnings.append(
            f"research handoff export issue(s): {top_research.get('run_id') or research_handoffs.get('issue_count')}"
        )
    operator_report = evidence.get("operator_report") if isinstance(evidence.get("operator_report"), dict) else {}
    if int(operator_report.get("issue_count") or 0) > 0:
        top_report = operator_report.get("top_issue") if isinstance(operator_report.get("top_issue"), dict) else {}
        warnings.append(
            f"operator report has issue(s): {top_report.get('detail') or operator_report.get('issue_count')}"
        )
    operator_actions = evidence.get("operator_actions") if isinstance(evidence.get("operator_actions"), dict) else {}
    if int(operator_actions.get("open_count") or 0) > 0:
        top_action = operator_actions.get("top_action") if isinstance(operator_actions.get("top_action"), dict) else {}
        warnings.append(
            f"operator action queue has open action(s): {top_action.get('action_id') or operator_actions.get('open_count')}"
        )
    repo_fleet = evidence.get("repo_fleet") if isinstance(evidence.get("repo_fleet"), dict) else {}
    repo_actions = repo_fleet.get("actions") if isinstance(repo_fleet.get("actions"), dict) else {}
    if int(repo_actions.get("open_count") or 0) > 0:
        top_action = repo_actions.get("top_action") if isinstance(repo_actions.get("top_action"), dict) else {}
        warnings.append(
            f"repo fleet action queue has open action(s): {top_action.get('fleet_action_id') or repo_actions.get('open_count')}"
        )
    repo_sweep = repo_fleet.get("sweep") if isinstance(repo_fleet.get("sweep"), dict) else {}
    if int(repo_sweep.get("issue_count") or 0) > 0:
        top_sweep = repo_sweep.get("top_issue") if isinstance(repo_sweep.get("top_issue"), dict) else {}
        warnings.append(f"repo fleet sweep has issue(s): {top_sweep.get('detail') or repo_sweep.get('issue_count')}")
    repo_release = repo_fleet.get("release_train") if isinstance(repo_fleet.get("release_train"), dict) else {}
    if int(repo_release.get("issue_count") or 0) > 0:
        top_release = repo_release.get("top_issue") if isinstance(repo_release.get("top_issue"), dict) else {}
        warnings.append(
            f"repo fleet release train has issue(s): {top_release.get('detail') or repo_release.get('issue_count')}"
        )
    for check in checks:
        if check.get("status") == FAIL:
            blockers.append(f"{check.get('name')}: {check.get('detail')}")
        elif check.get("status") == WARN:
            warnings.append(f"{check.get('name')}: {check.get('detail')}")
    return blockers, warnings


def _payload(target: Path, *, base_ref: str | None, run_checks: bool, policy: str = "public-repo") -> dict[str, Any]:
    evidence = _evidence(target, base_ref=base_ref)
    checks: list[dict[str, Any]] = []
    if run_checks:
        checks.append(_run_content_guard_check(target, name="tip", policy=policy, base_ref=base_ref))
        if base_ref:
            checks.append(_run_content_guard_check(target, name="introduced", policy=policy, base_ref=base_ref))
    elif not _content_guard_available(target):
        checks.append(
            {"name": "content_guard", "status": WARN, "detail": "content-guard not available", "available": False}
        )
    blockers, warnings = _assess(evidence, checks, _docs_warnings(target, base_ref))
    return {
        "target": str(target),
        "base_ref": base_ref,
        "policy": policy,
        "release_runs_root": str(_release_runs_root(target)),
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "evidence": evidence,
    }


def _payload_with_candidate_health(payload: dict[str, Any], target: Path) -> dict[str, Any]:
    candidate_health = _candidate_health(target)
    checks = list(payload.get("checks") if isinstance(payload.get("checks"), list) else [])
    checks.extend(candidate_health.get("checks") if isinstance(candidate_health.get("checks"), list) else [])
    checks.extend(_phase_release_checks(target))
    latest_candidate = candidate_health.get("latest") if isinstance(candidate_health.get("latest"), dict) else None
    if latest_candidate is not None:
        audit = _candidate_audit_payload(target, latest_candidate)
        checks.extend(
            {
                "status": issue.get("status", WARN),
                "name": f"release_candidate_audit_{issue.get('name')}",
                "detail": issue.get("detail"),
            }
            for issue in audit.get("issues", [])
        )
    blockers, warnings = _assess(
        payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {},
        checks,
        _docs_warnings(target, payload.get("base_ref") if isinstance(payload.get("base_ref"), str) else None),
    )
    updated = {
        **payload,
        "status": "ready" if not blockers else "blocked",
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "release_candidate_health": candidate_health,
    }
    return updated


def _write_release_markdown(path: Path, receipt: dict[str, Any]) -> None:
    lines = [
        "# Brigade Release Readiness",
        "",
        f"- Run: `{receipt.get('run_id')}`",
        f"- Status: {receipt.get('status')}",
        f"- Ready: {receipt.get('ready')}",
        f"- Target: `{receipt.get('target')}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = receipt.get("blockers") if isinstance(receipt.get("blockers"), list) else []
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    warnings = receipt.get("warnings") if isinstance(receipt.get("warnings"), list) else []
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- none")
    path.with_name("summary.md").write_text("\n".join(lines) + "\n")


def _candidate_docs_touch(changed_files: list[str]) -> dict[str, bool]:
    return {name: name in changed_files for name in ("README.md", "CHANGELOG.md", "ROADMAP.md")}


def _release_safe_text(text: str) -> str:
    redacted = RELEASE_PRIVATE_VALUE_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return RELEASE_PRIVATE_PATH_RE.sub("[redacted-path]", redacted)
