"""Read-only local operator center views."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import html
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import (
    actionqueue,
    chat_cmd,
    context_cmd,
    handoff_cmd,
    learn_cmd,
    memory_cmd,
    notifications_cmd,
    pantry_cmd,
    phases_cmd,
    projects_cmd,
    release_cmd,
    repos_cmd,
    reportstore,
    research_cmd,
    roadmap_cmd,
    security_cmd,
    tools_cmd,
    work_cmd,
)
from ..localio import (
    parse_iso_datetime,
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)
from ..render import emit

from . import schema as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def report_review(*, target: Path, report_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    plan = _action_plan(report)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-report-review"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": report.get("path"),
        "action_plan": plan,
        "suggested_next_commands": plan["commands"],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator report review: {report.get('report_id')}")
    print(f"unresolved_items: {plan['unresolved_item_count']}")
    for group, items in plan["groups"].items():
        print(f"{group}: {len(items)}")
        for item in items[:10]:
            print(
                f"- {item.get('subsystem')} {item.get('local_id') or item.get('id')} [{item.get('status')}] {item.get('safe_summary')}"
            )
            if item.get("suggested_next_command"):
                print(f"  next: {item.get('suggested_next_command')}")
    return 0


def _receipt_newer_than_report(receipt: dict[str, Any] | None, report_created: datetime | None) -> bool:
    if receipt is None or report_created is None:
        return False
    stamp = parse_iso_datetime(
        receipt.get("completed_at")
        or receipt.get("created_at")
        or receipt.get("started_at")
        or receipt.get("generated_at")
    )
    return bool(stamp and stamp > report_created)


def _report_queue_changed(report: dict[str, Any], current_reviews: list[dict[str, Any]]) -> bool:
    old = sorted(_item_key(item) for item in report.get("reviews", []) if isinstance(item, dict))
    new = sorted(_item_key(item) for item in current_reviews if isinstance(item, dict))
    return old != new


def _report_review_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reviews_data = report.get("reviews") if isinstance(report.get("reviews"), list) else []
    return {_item_key(item): item for item in reviews_data if isinstance(item, dict)}


def _is_blocker_item(item: dict[str, Any]) -> bool:
    return item.get("priority") in {"urgent", "high"} or item.get("severity") in {"critical", "high"}


def _missing_receipt_refs(report: dict[str, Any]) -> list[dict[str, Any]]:
    report_id = str(report.get("report_id") or "unknown")
    stale: list[dict[str, Any]] = []
    target = Path(str(report.get("target") or ".")).expanduser().resolve()
    for ref in report.get("receipt_references") if isinstance(report.get("receipt_references"), list) else []:
        if isinstance(ref, str) and ref and not _receipt_reference_exists(target, ref):
            stale.append({"report_id": report_id, "path": _path_label(target, ref)})
    return stale


def _report_diff_payload(
    *,
    target: Path,
    base_report: dict[str, Any],
    compare_report: dict[str, Any],
    diff_id: str = "planned",
    path: Path | None = None,
) -> dict[str, Any]:
    base_map = _report_review_map(base_report)
    compare_map = _report_review_map(compare_report)
    base_keys = set(base_map)
    compare_keys = set(compare_map)
    new_items = [compare_map[key] for key in sorted(compare_keys - base_keys)]
    resolved_items = [base_map[key] for key in sorted(base_keys - compare_keys)]
    changed_items = [
        {
            "before": base_map[key],
            "after": compare_map[key],
            "item_key": key,
        }
        for key in sorted(base_keys & compare_keys)
        if _fingerprint_payload(base_map[key]) != _fingerprint_payload(compare_map[key])
    ]
    new_blockers: list[dict[str, Any]] = []
    for key in sorted(compare_keys):
        current = compare_map[key]
        previous = base_map.get(key)
        if key not in base_map and _is_blocker_item(current):
            new_blockers.append(current)
        elif previous is not None and not _is_blocker_item(previous) and _is_blocker_item(current):
            new_blockers.append(current)
    stale_references = _missing_receipt_refs(base_report) + _missing_receipt_refs(compare_report)
    status = "changed" if new_items or resolved_items or changed_items or stale_references else "unchanged"
    summary = {
        "base_review_count": len(base_map),
        "compare_review_count": len(compare_map),
        "new_item_count": len(new_items),
        "resolved_item_count": len(resolved_items),
        "changed_item_count": len(changed_items),
        "new_blocker_count": len(new_blockers),
        "stale_reference_count": len(stale_references),
    }
    created_at = _now().isoformat()
    fingerprint_payload = {
        "base_report_id": base_report.get("report_id"),
        "compare_report_id": compare_report.get("report_id"),
        "summary": summary,
        "new_items": new_items,
        "resolved_items": resolved_items,
        "changed_items": changed_items,
        "new_blockers": new_blockers,
        "stale_references": stale_references,
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-report-diff"),
        "target": str(target),
        "diff_id": diff_id,
        "created_at": created_at,
        "base_report_id": base_report.get("report_id"),
        "base_report_path": base_report.get("path"),
        "base_report_fingerprint": base_report.get("report_fingerprint"),
        "compare_report_id": compare_report.get("report_id"),
        "compare_report_path": compare_report.get("path"),
        "compare_report_fingerprint": compare_report.get("report_fingerprint"),
        "status": status,
        "summary": summary,
        "new_items": new_items,
        "resolved_items": resolved_items,
        "changed_items": changed_items,
        "new_blockers": new_blockers,
        "stale_references": stale_references,
        "issue_count": len(new_blockers) + len(stale_references),
        "diff_fingerprint": _fingerprint_payload(fingerprint_payload),
        "path": str(path / "diff.json") if path is not None else None,
        "write_required": path is None,
    }
    return payload


def report_diff(
    *,
    target: Path,
    base_report_id: str,
    compare_report_id: str,
    record: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    base_report, base_error = _resolve_report(target, base_report_id)
    compare_report, compare_error = _resolve_report(target, compare_report_id)
    if base_report is None:
        print(f"error: {base_error}", file=sys.stderr)
        return 1 if base_error and "not found" in base_error else 2
    if compare_report is None:
        print(f"error: {compare_error}", file=sys.stderr)
        return 1 if compare_error and "not found" in compare_error else 2
    if base_report.get("report_id") == compare_report.get("report_id"):
        print("error: base and compare reports must be different", file=sys.stderr)
        return 2
    diff_id = "planned"
    diff_dir: Path | None = None
    if record:
        created = _now()
        diff_id = f"{created.strftime('%Y%m%d-%H%M%S')}-report-diff-{uuid4().hex[:6]}"
        diff_dir = _report_diffs_root(target) / diff_id
    payload = _report_diff_payload(
        target=target, base_report=base_report, compare_report=compare_report, diff_id=diff_id, path=diff_dir
    )
    payload["write_required"] = bool(record)
    if record and diff_dir is not None:
        _write_json(diff_dir / "diff.json", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator report diff: {payload['base_report_id']} -> {payload['compare_report_id']}")
    print(f"status: {payload['status']}")
    print(f"new_items: {payload['summary']['new_item_count']}")
    print(f"resolved_items: {payload['summary']['resolved_item_count']}")
    print(f"new_blockers: {payload['summary']['new_blocker_count']}")
    print(f"stale_references: {payload['summary']['stale_reference_count']}")
    if record:
        print(f"path: {payload['path']}")
    else:
        print("run: brigade center report diff <base> <compare> --record")
    return 0


def report_compare(*, target: Path, report_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    report_created = _parse_time(report.get("created_at") or report.get("generated_at"))
    issues: list[dict[str, Any]] = []
    current_head = _git_value(target, "rev-parse", "HEAD")
    report_git = report.get("git") if isinstance(report.get("git"), dict) else {}
    if report_git.get("head") and current_head and report_git.get("head") != current_head:
        issues.append(
            {
                "status": "warn",
                "name": "operator_report_head_changed",
                "detail": "current HEAD differs from report HEAD",
            }
        )
    for ref in report.get("receipt_references") if isinstance(report.get("receipt_references"), list) else []:
        if isinstance(ref, str) and ref and not _receipt_reference_exists(target, ref):
            issues.append(
                {
                    "status": "warn",
                    "name": "operator_report_missing_receipt",
                    "detail": f"missing receipt reference: {_path_label(target, ref)}",
                }
            )
            break
    current_activity = _activity(target)
    report_activity = report.get("activity") if isinstance(report.get("activity"), list) else []
    current_activity_time = _parse_time(current_activity[0].get("updated_at")) if current_activity else None
    report_activity_time = _parse_time(report_activity[0].get("updated_at")) if report_activity else report_created
    if (
        current_activity_time is not None
        and report_activity_time is not None
        and current_activity_time > report_activity_time
    ):
        issues.append(
            {"status": "warn", "name": "operator_report_newer_activity", "detail": "newer center activity exists"}
        )
    latest_release = release_cmd._latest_release_receipt(target)
    latest_candidate = release_cmd._latest_candidate(target)
    latest_verify = work_cmd._latest_verify_receipt(target)
    review_health = work_cmd._review_health(target)
    latest_review = review_health.get("latest_run") if isinstance(review_health.get("latest_run"), dict) else None
    latest_sweep = work_cmd._scanner_sweep_health(target).get("latest")
    latest_security = security_cmd.health(target).get("evidence")
    for name, receipt, key in (
        ("newer_release_readiness", latest_release, "run_id"),
        ("newer_release_candidate", latest_candidate, "candidate_id"),
        ("newer_verification", latest_verify, "run_id"),
        ("newer_review_run", latest_review, "run_id"),
        ("newer_scanner_sweep", latest_sweep, "sweep_id"),
    ):
        if _receipt_newer_than_report(receipt if isinstance(receipt, dict) else None, report_created):
            issues.append({"status": "warn", "name": name, "detail": str((receipt or {}).get(key))})
    security_generated = parse_iso_datetime(
        (latest_security or {}).get("generated_at") if isinstance(latest_security, dict) else None
    )
    if report_created and security_generated and security_generated > report_created:
        issues.append(
            {"status": "warn", "name": "newer_security_report", "detail": str((latest_security or {}).get("path"))}
        )
    current_reviews = _reviews(target)
    if _report_queue_changed(report, current_reviews):
        issues.append(
            {
                "status": "warn",
                "name": "operator_report_review_queue_changed",
                "detail": "current review queue differs from report",
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-report-compare"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": report.get("path"),
        "report_head": report_git.get("head"),
        "current_head": current_head,
        "issues": issues,
        "issue_count": len(issues),
        "status": "current" if not issues else "stale",
        "suggested_next_commands": [
            "brigade center report build",
            f"brigade center report closeout {report.get('report_id')} --status superseded",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not issues else 1
    print(f"operator report compare: {report.get('report_id')}")
    print(f"status: {payload['status']}")
    print(f"issues: {len(issues)}")
    for issue in issues:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0 if not issues else 1


def report_closeout(
    *,
    target: Path,
    report_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    deferred_item_ids: list[str] | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if status not in reportstore.CLOSEOUT_STATUSES:
        print("error: --status must be one of reviewed, deferred, superseded, archived", file=sys.stderr)
        return 2
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    report_path = Path(str(report.get("path") or ""))
    if not report_path.is_dir():
        print(f"error: operator report path is missing: {report.get('path')}", file=sys.stderr)
        return 2
    plan = _action_plan(report)
    deferred = list(deferred_item_ids or [])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": report.get("path"),
        "status": status,
        "reason": reason or f"operator report marked {status}",
        "reviewed_at": _now().isoformat(),
        "unresolved_item_count": plan["unresolved_item_count"],
        "deferred_item_ids": deferred,
        "report_fingerprint": report.get("report_fingerprint")
        or _fingerprint_payload({"reviews": report.get("reviews"), "activity": report.get("activity")}),
    }
    closeout_path = reportstore.write_closeout(report_path, payload)
    report["closeout"] = payload
    _write_json(report_path / "CENTER_EVIDENCE.json", report)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator report closeout: {report.get('report_id')}")
    print(f"status: {status}")
    print(f"unresolved_items: {payload['unresolved_item_count']}")
    print(f"path: {closeout_path}")
    return 0


def _report_review_status(report: dict[str, Any]) -> str | None:
    closeout = report.get("closeout") if isinstance(report.get("closeout"), dict) else None
    status = closeout.get("status") if isinstance(closeout, dict) else None
    return status if isinstance(status, str) else None


def _report_reviewed_at(report: dict[str, Any]) -> str | None:
    closeout = report.get("closeout") if isinstance(report.get("closeout"), dict) else None
    reviewed_at = closeout.get("reviewed_at") if isinstance(closeout, dict) else None
    return reviewed_at if isinstance(reviewed_at, str) else None
