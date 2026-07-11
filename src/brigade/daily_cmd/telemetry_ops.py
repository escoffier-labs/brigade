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
    lines: list[str] = [f"daily approvals: {payload['target']}"]
    lines.extend(
        f"- {approval.get('approval_id')} [{approval.get('status')}] {approval.get('safe_summary')}"
        for approval in payload["approvals"]
    )
    return emit(payload, json_output, lines, 0)


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


def _review_approval(
    target: Path, approval_id: str, status: str, reason: str | None, *, json_output: bool = False
) -> int:
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
            issues.append(
                {
                    "status": "warn",
                    "name": "approval_newer_matching_request",
                    "detail": str(newer[0].get("approval_id")),
                }
            )
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
    lines: list[str] = [
        f"daily approval compare: {approval_id}",
        f"issues: {payload['issue_count']}",
    ]
    lines.extend(f"[{issue.get('status')}] {issue.get('name')}: {issue.get('detail')}" for issue in payload["issues"])
    return emit(payload, json_output, lines, 0 if payload["ok"] else 1)


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
    lines: list[str] = [
        f"daily approval archive: {target}",
        f"archived: {len(archived)}",
    ]
    return emit(payload, json_output, lines, 0)


def init(*, target: Path, force: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    path = _config_path(target)
    if path.exists() and not force:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "target": str(target),
            "path": str(path),
            "written": False,
            "reason": "already exists",
        }
    else:
        _write_config(path, DEFAULT_CONFIG)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "target": str(target),
            "path": str(path),
            "written": True,
            "config": DEFAULT_CONFIG,
        }
    lines: list[str] = [
        f"daily config: {path}",
        f"written: {payload['written']}",
    ]
    return emit(payload, json_output, lines, 0)


def schema(*, target: Path, json_output: bool = False) -> int:
    payload = {"schema_version": SCHEMA_VERSION, "target": str(target.expanduser().resolve()), **_schemas()}
    lines: list[str] = [f"daily schema: {payload['target']}"]
    lines.extend(f"- {item['name']}" for item in payload["schemas"])
    return emit(payload, json_output, lines, 0)


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
    lines: list[str] = [
        f"daily history: {payload['target']}",
        f"runs: {payload['run_count']}",
    ]
    lines.extend(f"- {item.get('run_id')} [{item.get('status')}] {item.get('started_at')}" for item in payload["runs"])
    lines.append(f"plans: {payload['plan_count']}")
    return emit(payload, json_output, lines, 0)


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
    phase_health = phases_cmd.health(target)
    latest_phase_session = phases_cmd._latest_session(target)
    for error in [*run_errors, *plan_errors]:
        checks.append({"status": "fail", "name": "daily_receipt_parse", "detail": f"{error['path']}: {error['error']}"})
    for error in approval_errors:
        checks.append(
            {"status": "fail", "name": "daily_approval_parse", "detail": f"{error['path']}: {error['error']}"}
        )
    for error in telemetry_errors:
        checks.append(
            {"status": "fail", "name": "daily_telemetry_parse", "detail": f"{error['path']}: {error['error']}"}
        )
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
        if (
            latest_run.get("status") in {"completed", "failed", "blocked"}
            and not latest_run.get("closeout_status")
            and _is_stale(latest_run.get("completed_at") or latest_run.get("started_at"), run_hours)
        ):
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
            checks.append(
                {"status": "warn", "name": "daily_stale_pending_approval", "detail": str(approval.get("approval_id"))}
            )
            break
    if approved_approvals:
        checks.append(
            {
                "status": "warn",
                "name": "daily_approved_approval",
                "detail": str(approved_approvals[0].get("approval_id")),
            }
        )
    if held_approvals:
        checks.append(
            {"status": "warn", "name": "daily_held_approval", "detail": str(held_approvals[0].get("approval_id"))}
        )
    if rejected_approvals:
        checks.append(
            {
                "status": "warn",
                "name": "daily_rejected_approval",
                "detail": str(rejected_approvals[0].get("approval_id")),
            }
        )
    if phase_health.get("issue_count") and not _phase_ledger_issues_captured_by_report(phase_health):
        top_phase_issue = phase_health.get("top_issue") if isinstance(phase_health.get("top_issue"), dict) else {}
        checks.append(
            {
                "status": "warn",
                "name": "phase_ledger_issue",
                "detail": top_phase_issue.get("detail") or "phase execution ledger needs review",
            }
        )
    if isinstance(latest_phase_session, dict) and latest_phase_session.get("status") not in {"closed", "archived"}:
        checks.append(
            {"status": "warn", "name": "phase_session_active", "detail": str(latest_phase_session.get("session_id"))}
        )
    for approval in approvals:
        current = _current_action_for_approval(target, approval)
        if current is None and approval.get("status") in {"pending", "approved"}:
            checks.append(
                {
                    "status": "warn",
                    "name": "daily_approval_missing_evidence",
                    "detail": str(approval.get("approval_id")),
                }
            )
            break
        if current is not None and current.get("source_fingerprint") != approval.get("source_fingerprint"):
            checks.append(
                {
                    "status": "warn",
                    "name": "daily_approval_changed_evidence",
                    "detail": str(approval.get("approval_id")),
                }
            )
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
        "phase_ledger": phase_health,
        "phase_session": phases_cmd._session_summary(latest_phase_session)
        if isinstance(latest_phase_session, dict)
        else None,
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
    lines: list[str] = [f"daily doctor: {target}"]
    lines.extend(f"[{check.get('status')}] {check.get('name')}: {check.get('detail')}" for check in payload["checks"])
    return emit(
        payload, json_output, lines, 1 if any(check.get("status") == "fail" for check in payload["checks"]) else 0
    )


def protocol_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    steps = [
        {"step": "status", "command": "brigade daily status --json", "purpose": "inspect local operating state"},
        {"step": "plan", "command": "brigade daily plan --json", "purpose": "rank safe local actions"},
        {
            "step": "review",
            "command": "brigade daily review --json",
            "purpose": "preview evidence, acceptance, risk, and approval boundary",
        },
        {
            "step": "approval",
            "command": "brigade daily approvals approve <approval-id> --json",
            "purpose": "approve only when the selected action requires it",
        },
        {"step": "run", "command": "brigade daily run --json", "purpose": "execute one bounded safe adapter action"},
        {
            "step": "closeout",
            "command": "brigade daily closeout --json",
            "purpose": "record review, verification, and evidence state",
        },
        {
            "step": "recover",
            "command": "brigade daily resume --json",
            "purpose": "resume or explain recovery when blocked",
        },
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
    lines: list[str] = [f"daily protocol: {payload['target']}"]
    lines.extend(f"- {step['step']}: {step['command']}" for step in payload["steps"])
    return emit(payload, json_output, lines, 0)


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
    lines: list[str] = [f"daily repair: {payload['repair_id']}"]
    lines.extend(
        f"- {suggestion.get('name')}: {suggestion.get('suggested_command')}" for suggestion in payload["suggestions"]
    )
    return emit(payload, json_output, lines, 0)


def unblock_payload(target: Path, *, dry_run: bool = False) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = _latest_run(target)
    config, _ = _load_config(target)
    created_imports: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    approval_request = None
    blockers: list[str] = []
    action = (
        latest.get("selected_action")
        if isinstance(latest, dict) and isinstance(latest.get("selected_action"), dict)
        else None
    )
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
                    "source_fingerprint": _fingerprint(
                        {"run_id": latest.get("run_id"), "blockers": latest.get("blockers")}
                    ),
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
    lines: list[str] = [
        f"daily unblock: {payload['unblock_id']}",
        f"created_imports: {len(payload['created_imports'])}",
    ]
    if payload.get("approval_request"):
        lines.append(f"approval: {payload['approval_request'].get('approval_id')}")
    return emit(payload, json_output, lines, 1 if payload.get("blockers") else 0)


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
    action_types = Counter(
        str((run.get("selected_action") or {}).get("action_type") or "unknown")
        for run in runs
        if isinstance(run.get("selected_action"), dict)
    )
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
        "stale_evidence_rate": sum(
            1 for check in health(target).get("checks", []) if "evidence" in str(check.get("name"))
        )
        / max(1, len(runs)),
        "failed_run_rate": statuses.get("failed", 0) / max(1, len(runs)),
        "closeout_status_counts": dict(Counter(str(run.get("closeout_status") or "open") for run in runs)),
        "repeated_blocker_fingerprints": [key for key, count in blocker_counts.items() if count > 1],
        "ignored_or_deferred_recommendations": statuses.get("blocked", 0)
        + sum(1 for run in runs if run.get("closeout_status") == "deferred"),
        "average_run_to_closeout_hours": round(sum(closed_ages) / len(closed_ages), 2) if closed_ages else None,
    }
    checks: list[dict[str, Any]] = []
    if statuses.get("failed", 0):
        checks.append(
            {"status": "warn", "name": "daily_telemetry_failed_runs", "detail": str(statuses.get("failed", 0))}
        )
    if metrics["repeated_blocker_fingerprints"]:
        checks.append(
            {
                "status": "warn",
                "name": "daily_telemetry_repeated_blockers",
                "detail": str(metrics["repeated_blocker_fingerprints"][0]),
            }
        )
    for error in errors:
        checks.append(
            {"status": "fail", "name": "daily_telemetry_parse", "detail": f"{error['path']}: {error['error']}"}
        )
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
    lines: list[str] = [
        f"daily telemetry: {payload['target']}",
        f"runs: {payload['metrics']['run_count']}",
        f"approvals: {payload['metrics']['approval_frequency']}",
        f"issues: {payload['issue_count']}",
    ]
    return emit(payload, json_output, lines, 0)


def telemetry_doctor(*, target: Path, json_output: bool = False) -> int:
    payload = telemetry_payload(target)
    lines: list[str] = [f"daily telemetry doctor: {payload['target']}"]
    lines.extend(f"[{check.get('status')}] {check.get('name')}: {check.get('detail')}" for check in payload["checks"])
    return emit(
        payload, json_output, lines, 1 if any(check.get("status") == "fail" for check in payload["checks"]) else 0
    )
