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
    lines: list[str] = [
        f"daily hardening plan: {payload['target']}",
        f"phases: {payload['phase_count']}",
    ]
    lines.extend(f"- {stream['phase_start']}-{stream['phase_end']} {stream['id']}" for stream in payload["workstreams"])
    return emit(payload, json_output, lines, 0)


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


def _hardening_quieted_findings(
    target: Path, findings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
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
    from .. import release_cmd, repos_cmd

    target = target.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    config, config_checks = _load_config(target)
    runs, run_errors = _iter_receipts(_runs_root(target), "run.json")
    plans, plan_errors = _iter_receipts(_plans_root(target), "plan.json")
    latest_run = runs[0] if runs else None
    daily_health = health(target)
    telemetry_data = telemetry_payload(target)
    config_issues = [check for check in config_checks if check.get("status") != "ok"]
    if any(check.get("status") == "fail" for check in config_checks):
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=115,
                name="daily_config_invalid",
                severity="high",
                safe_summary="daily config has invalid fields",
                suggested_command="brigade daily doctor",
                evidence_refs=[str(_config_path(target))],
                metadata={"checks": config_issues},
            )
        )
    unsafe_config_checks = [
        check
        for check in config_issues
        if check.get("name") in {"daily_disabled", "daily_risk_policy"}
        or str(check.get("name") or "").startswith("allow_")
    ]
    if unsafe_config_checks:
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=115,
                name="daily_config_policy_warning",
                severity="medium",
                safe_summary="daily config has unsafe or blocking local policy states",
                suggested_command="brigade daily doctor",
                evidence_refs=[str(_config_path(target))],
                metadata={"checks": unsafe_config_checks},
            )
        )
    malformed_runs = [run for run in runs[:10] if not _adapter_result_is_normalized(run)]
    malformed_run_errors = [
        {"run_id": None, "error": item.get("error"), "path": item.get("path")} for item in run_errors
    ]
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
    malformed_plan_errors = [
        {"plan_id": None, "error": item.get("error"), "path": item.get("path")} for item in plan_errors
    ]
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
        and (
            _age_hours(item.get("created_at")) is not None
            and (_age_hours(item.get("created_at")) or 0) > int(config.get("stale_run_threshold_hours") or 24)
        )
    ]
    if (
        int(approvals.get("pending_count") or 0) > 0
        or stale_approvals
        or approval_counts.get("held")
        or approval_counts.get("rejected")
        or approval_errors
    ):
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
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=119,
                name="daily_telemetry_issue",
                severity="medium",
                safe_summary="daily telemetry has warnings or parse errors",
                suggested_command="brigade daily telemetry doctor",
                evidence_refs=[str(_telemetry_root(target))],
                metadata={"checks": telemetry_data.get("checks"), "metrics": telemetry_data.get("metrics")},
            )
        )
    protocol_data = protocol_payload(target)
    protocol_steps = {str(item.get("step")) for item in protocol_data.get("steps", []) if isinstance(item, dict)}
    required_protocol_steps = {"status", "plan", "review", "approval", "run", "closeout", "recover"}
    if not required_protocol_steps <= protocol_steps:
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=122,
                name="daily_protocol_incomplete",
                severity="high",
                safe_summary="daily protocol is missing wrapper-facing steps",
                suggested_command="brigade daily protocol",
                evidence_refs=["daily protocol"],
                metadata={"required_steps": sorted(required_protocol_steps), "actual_steps": sorted(protocol_steps)},
            )
        )
    for command in protocol_data.get("commands", []) if isinstance(protocol_data.get("commands"), list) else []:
        if not str(command).startswith("brigade daily "):
            findings.append(
                _hardening_finding(
                    workstream="daily-production-hardening",
                    phase=122,
                    name="daily_protocol_command_scope",
                    severity="medium",
                    safe_summary="daily protocol includes a non-daily command",
                    suggested_command="brigade daily protocol",
                    evidence_refs=["daily protocol"],
                    metadata={"command": command},
                )
            )
            break
    latest_run_output = latest_run.get("wrapped_output") if isinstance(latest_run, dict) else None
    if isinstance(latest_run_output, (str, list, dict)):
        findings.append(
            _hardening_finding(
                workstream="daily-production-hardening",
                phase=123,
                name="wrapped_output_in_run_json",
                severity="medium",
                safe_summary="daily run receipt appears to include wrapped command output instead of receipt references",
                suggested_command="brigade daily show latest",
                evidence_refs=[str(_runs_root(target))],
            )
        )

    center_manifest = center_cmd._center_schema_manifest(target)
    center_contract = center_cmd._center_contract_health(target)
    if int(center_manifest.get("schema_count") or 0) < 1:
        findings.append(
            _hardening_finding(
                workstream="operator-center-contract-cleanup",
                phase=125,
                name="center_schema_missing",
                severity="high",
                safe_summary="center schema manifest is empty",
                suggested_command="brigade center schema",
                evidence_refs=["center schema"],
            )
        )
    center_reviews = center_cmd._reviews(target)
    required_review_fields = {"subsystem", "local_id", "status", "safe_summary", "suggested_next_command"}
    malformed_review = next((item for item in center_reviews if not required_review_fields <= set(item)), None)
    if malformed_review:
        findings.append(
            _hardening_finding(
                workstream="operator-center-contract-cleanup",
                phase=126,
                name="center_review_shape",
                severity="medium",
                safe_summary="center review item is missing wrapper-facing fields",
                suggested_command="brigade center reviews --json",
                evidence_refs=["center reviews"],
            )
        )
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
        if not (
            (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("source_fingerprint")
            or item.get("source")
        )
    ]
    if missing_acceptance:
        findings.append(
            _hardening_finding(
                workstream="inbox-evidence-quality",
                phase=135,
                name="pending_import_missing_acceptance",
                severity="medium",
                safe_summary=f"{len(missing_acceptance)} pending import(s) missing acceptance",
                suggested_command="brigade work inbox doctor",
                evidence_refs=[str(work_cmd._imports_path(target))],
            )
        )
    if missing_provenance:
        findings.append(
            _hardening_finding(
                workstream="inbox-evidence-quality",
                phase=136,
                name="pending_import_missing_provenance",
                severity="medium",
                safe_summary=f"{len(missing_provenance)} pending import(s) missing provenance",
                suggested_command="brigade work import provenance",
                evidence_refs=[str(work_cmd._imports_path(target))],
            )
        )
    inbox_hygiene = work_cmd._inbox_hygiene_payload(target)
    inbox_quality = work_cmd._inbox_quality_payload(target)
    if int(inbox_hygiene.get("issue_count") or 0) > 0:
        top = inbox_hygiene.get("top_issue") if isinstance(inbox_hygiene.get("top_issue"), dict) else {}
        findings.append(
            _hardening_finding(
                workstream="inbox-evidence-quality",
                phase=137,
                name="inbox_hygiene_issue",
                severity="medium",
                safe_summary=str(top.get("detail") or "work inbox has hygiene issues"),
                suggested_command="brigade work inbox doctor",
                evidence_refs=[str(work_cmd._imports_path(target))],
            )
        )
    quality_counts = inbox_quality.get("issue_counts") if isinstance(inbox_quality.get("issue_counts"), dict) else {}
    noisy_or_deferred = (
        int(quality_counts.get("noisy_source") or 0)
        + int(quality_counts.get("deferred") or 0)
        + int(quality_counts.get("stale") or 0)
    )
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
    if (
        inbox_quality.get("best_import")
        and int((inbox_quality.get("best_import") or {}).get("quality_score") or 0) < 80
    ):
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
    repo_daily_use = repos_cmd.daily_use_health(target)
    if int(repo_health.get("issue_count") or 0) > 0:
        top = repo_health.get("top_issue") if isinstance(repo_health.get("top_issue"), dict) else {}
        findings.append(
            _hardening_finding(
                workstream="repo-fleet-daily-use",
                phase=145,
                name="repo_fleet_health_issue",
                severity="medium",
                safe_summary=str(top.get("detail") or "repo fleet has health issues"),
                suggested_command="brigade repos doctor",
                evidence_refs=["repo fleet health"],
            )
        )
    for issue in repo_daily_use.get("checks", []) if isinstance(repo_daily_use.get("checks"), list) else []:
        if not isinstance(issue, dict) or issue.get("status") == "ok":
            continue
        findings.append(
            _hardening_finding(
                workstream="repo-fleet-daily-use",
                phase=issue.get("phase") if isinstance(issue.get("phase"), int) else 145,
                name=str(issue.get("name") or "repo_fleet_daily_use_issue"),
                severity="medium",
                safe_summary=str(issue.get("detail") or "repo fleet daily-use issue"),
                suggested_command=str(issue.get("suggested_next_command") or "brigade repos doctor"),
                evidence_refs=["repo fleet daily-use health"],
                metadata={"issue": issue},
            )
        )

    release_readiness = release_cmd._latest_release_receipt(target)
    release_candidate = release_cmd._latest_candidate(target)
    release_dogfood = release_cmd._release_dogfood_health(target)
    if not release_readiness:
        findings.append(
            _hardening_finding(
                workstream="self-dogfood-release-loop",
                phase=155,
                name="missing_release_readiness",
                severity="medium",
                safe_summary="latest release readiness receipt is missing",
                suggested_command="brigade release run",
                evidence_refs=[".brigade/release/runs"],
            )
        )
    elif not release_readiness.get("ready"):
        findings.append(
            _hardening_finding(
                workstream="self-dogfood-release-loop",
                phase=158,
                name="blocked_release_readiness",
                severity="high",
                safe_summary="latest release readiness is blocked",
                suggested_command=f"brigade release show {release_readiness.get('run_id')}",
                evidence_refs=[str(release_readiness.get("path") or ".brigade/release/runs")],
            )
        )
    if not release_candidate:
        findings.append(
            _hardening_finding(
                workstream="self-dogfood-release-loop",
                phase=156,
                name="missing_release_candidate",
                severity="low",
                safe_summary="latest release candidate packet is missing",
                suggested_command="brigade release candidate build",
                evidence_refs=[".brigade/release/candidates"],
            )
        )
    elif not isinstance(release_candidate.get("daily_driver"), dict):
        findings.append(
            _hardening_finding(
                workstream="self-dogfood-release-loop",
                phase=157,
                name="candidate_missing_daily_evidence",
                severity="medium",
                safe_summary="latest release candidate is missing daily driver evidence",
                suggested_command="brigade release candidate build",
                evidence_refs=[str(release_candidate.get("path") or ".brigade/release/candidates")],
            )
        )
    if release_readiness:
        evidence = release_readiness.get("evidence") if isinstance(release_readiness.get("evidence"), dict) else {}
        if not isinstance(evidence.get("daily_hardening"), dict):
            findings.append(
                _hardening_finding(
                    workstream="daily-production-hardening",
                    phase=124,
                    name="release_missing_daily_hardening",
                    severity="medium",
                    safe_summary="latest release readiness evidence is missing daily hardening state",
                    suggested_command="brigade release run",
                    evidence_refs=[str(release_readiness.get("path") or ".brigade/release/runs")],
                )
            )
    for issue in release_dogfood.get("checks", []) if isinstance(release_dogfood.get("checks"), list) else []:
        if not isinstance(issue, dict) or issue.get("status") == "ok":
            continue
        findings.append(
            _hardening_finding(
                workstream="self-dogfood-release-loop",
                phase=issue.get("phase") if isinstance(issue.get("phase"), int) else 155,
                name=str(issue.get("name") or "release_dogfood_issue"),
                severity="high" if issue.get("name") == "release_dogfood_readiness_blocked" else "medium",
                safe_summary=str(issue.get("detail") or "release dogfood issue"),
                suggested_command=str(issue.get("suggested_next_command") or "brigade release doctor"),
                evidence_refs=["release dogfood health"],
                metadata={"issue": issue},
            )
        )

    findings.sort(
        key=lambda item: (
            {"high": 3, "medium": 2, "low": 1}.get(str(item.get("severity")), 0),
            str(item.get("finding_id")),
        ),
        reverse=True,
    )
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
    lines: list[str] = [
        f"daily hardening audit: {payload['target']}",
        f"findings: {payload['finding_count']}",
    ]
    lines.extend(
        f"- [{finding['severity']}] {finding['finding_id']}: {finding['safe_summary']}"
        for finding in payload["findings"][:10]
    )
    return emit(payload, json_output, lines, 0)


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
    lines: list[str] = [
        f"daily hardening import-issues: {target}",
        f"created: {len(created)}",
        f"skipped: {len(skipped)}",
        f"dismissed: {len(skipped_dismissed)}",
    ]
    return emit(payload, json_output, lines, 0)


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
