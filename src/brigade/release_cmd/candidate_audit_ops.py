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


def _candidate_bundle_files(candidate: dict[str, Any]) -> list[Path]:
    path = candidate.get("path")
    if not isinstance(path, str) or not path:
        return []
    root = Path(path)
    names = candidate.get("bundle_files") if isinstance(candidate.get("bundle_files"), list) else []
    return [root / str(name) for name in names if str(name)]


def _candidate_privacy_issues(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for path in _candidate_bundle_files(candidate):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if RELEASE_PRIVATE_VALUE_RE.search(text):
            issues.append({"status": WARN, "name": "candidate_privacy_secret_like_value", "detail": path.name})
        if RELEASE_PRIVATE_PATH_RE.search(text):
            issues.append({"status": WARN, "name": "candidate_privacy_private_path", "detail": path.name})
    return issues


def _candidate_reference_issues(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for key, value in (
        (
            "release_readiness",
            (candidate.get("release_readiness") or {}).get("path")
            if isinstance(candidate.get("release_readiness"), dict)
            else None,
        ),
        (
            "work_closeout",
            (candidate.get("work_closeout") or {}).get("path")
            if isinstance(candidate.get("work_closeout"), dict)
            else None,
        ),
        (
            "verification",
            (candidate.get("verification") or {}).get("path")
            if isinstance(candidate.get("verification"), dict)
            else None,
        ),
    ):
        if not value:
            issues.append({"status": WARN, "name": f"missing_{key}_evidence", "detail": "not captured in candidate"})
        elif not Path(str(value)).exists():
            issues.append({"status": WARN, "name": f"missing_{key}_receipt", "detail": str(value)})
    return issues


def _candidate_docs_changed_after_build(candidate: dict[str, Any]) -> list[str]:
    path = candidate.get("path")
    if not isinstance(path, str) or not path:
        return []
    evidence_path = Path(path) / "EVIDENCE.json"
    try:
        evidence_mtime = evidence_path.stat().st_mtime
    except OSError:
        return []
    target = Path(str(candidate.get("target") or "."))
    changed: list[str] = []
    for item in ("README.md", "CHANGELOG.md", "ROADMAP.md"):
        repo_file = target / item
        if repo_file.exists() and repo_file.stat().st_mtime > evidence_mtime:
            changed.append(item)
    return changed


def _candidate_audit_payload(target: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    created = work_cmd._parse_iso_datetime(candidate.get("created_at"))
    if created is not None:
        age_hours = (_now() - created).total_seconds() / 3600
        if age_hours > RELEASE_CANDIDATE_STALE_HOURS:
            issues.append({"status": WARN, "name": "candidate_stale", "detail": f"{age_hours:.1f}h"})
    git = candidate.get("git") if isinstance(candidate.get("git"), dict) else {}
    current_head = _git_value(target, "rev-parse", "HEAD")
    if git.get("head") and current_head and git.get("head") != current_head:
        issues.append(
            {"status": WARN, "name": "candidate_head_changed", "detail": "current HEAD differs from candidate HEAD"}
        )
    issues.extend(_candidate_reference_issues(candidate))
    docs_changed = _candidate_docs_changed_after_build(candidate)
    if docs_changed:
        issues.append({"status": WARN, "name": "candidate_docs_changed", "detail": ", ".join(docs_changed)})
    current_contract = _command_contract_snapshot(target)
    candidate_contract = (
        candidate.get("command_contract") if isinstance(candidate.get("command_contract"), dict) else {}
    )
    if not candidate_contract.get("fingerprint"):
        issues.append(
            {
                "status": WARN,
                "name": "candidate_missing_command_contract",
                "detail": "candidate has no command contract fingerprint",
            }
        )
    elif candidate_contract.get("fingerprint") != current_contract.get("fingerprint"):
        issues.append(
            {
                "status": WARN,
                "name": "candidate_command_contract_changed",
                "detail": "current CLI/docs command contract differs from candidate",
            }
        )
    issues.extend(_candidate_privacy_issues(candidate))
    return {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_path": candidate.get("path"),
        "status": "current" if not issues else "needs-review",
        "issue_count": len(issues),
        "issues": issues,
        "command_contract": {
            "candidate": candidate_contract or None,
            "current": current_contract,
        },
        "suggested_next_commands": [
            f"brigade release candidate compare {candidate.get('candidate_id')}",
            f"brigade release candidate closeout {candidate.get('candidate_id')} --status reviewed",
            f"brigade release candidate import-issues {candidate.get('candidate_id')}",
        ],
    }


def candidate_audit(*, target: Path, candidate_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = _candidate_audit_payload(target, candidate)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["issue_count"] == 0 else 1
    print(f"release candidate audit: {candidate.get('candidate_id')}")
    print(f"status: {payload['status']}")
    print(f"issues: {payload['issue_count']}")
    for issue in payload["issues"]:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0 if payload["issue_count"] == 0 else 1


def candidate_import_issues(
    *, target: Path, candidate_id: str = "latest", dry_run: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    audit = _candidate_audit_payload(target, candidate)
    records = []
    for issue in audit["issues"]:
        name = str(issue.get("name") or "candidate_issue")
        records.append(
            {
                "text": f"Review release candidate {candidate.get('candidate_id')}: {name}",
                "kind": "task",
                "source": "release-candidate",
                "priority": "high" if "privacy" in name or "missing" in name else "normal",
                "metadata": {
                    "candidate_id": candidate.get("candidate_id"),
                    "candidate_path": candidate.get("path"),
                    "issue_name": name,
                    "detail": issue.get("detail"),
                    "source_item_key": f"release-candidate:{candidate.get('candidate_id')}:{name}",
                    "source_fingerprint": work_cmd._stable_hash(
                        {"candidate": candidate.get("candidate_id"), "issue": issue}
                    ),
                },
            }
        )
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "dry_run": dry_run,
        "issues": len(records),
        "imported": len(imported),
        "skipped_duplicates": len(skipped),
        "skipped_dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release candidate issue imports: {candidate.get('candidate_id')}")
    print(f"dry_run: {dry_run}")
    print(f"issues: {payload['issues']}")
    print(f"imported: {payload['imported']}")
    print(f"skipped_duplicates: {payload['skipped_duplicates']}")
    print(f"skipped_dismissed: {payload['skipped_dismissed']}")
    return 0


def _receipt_newer_than_candidate(receipt: dict[str, Any] | None, candidate_created: datetime | None) -> bool:
    if receipt is None or candidate_created is None:
        return False
    stamp = work_cmd._parse_iso_datetime(
        receipt.get("completed_at") or receipt.get("created_at") or receipt.get("started_at")
    )
    return bool(stamp and stamp > candidate_created)


def candidate_compare(*, target: Path, candidate_id: str = "latest", json_output: bool = False) -> int:
    from .. import center_cmd

    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    candidate_created = work_cmd._parse_iso_datetime(candidate.get("created_at"))
    current_git = _git_state(target)
    candidate_git = candidate.get("git") if isinstance(candidate.get("git"), dict) else {}
    latest_release = _latest_release_receipt(target)
    latest_verify = work_cmd._latest_verify_receipt(target)
    review_health = work_cmd._review_health(target)
    latest_review = review_health.get("latest_run") if isinstance(review_health.get("latest_run"), dict) else None
    latest_sweep = work_cmd._scanner_sweep_health(target).get("latest")
    latest_security = security_cmd.health(target).get("evidence")
    changed_docs_after_candidate = []
    evidence_path = Path(str(candidate.get("path") or "")) / "EVIDENCE.json"
    evidence_mtime = evidence_path.stat().st_mtime if evidence_path.is_file() else None
    for path in ("README.md", "CHANGELOG.md", "ROADMAP.md"):
        repo_file = target / path
        if evidence_mtime is not None and repo_file.exists() and repo_file.stat().st_mtime > evidence_mtime:
            changed_docs_after_candidate.append(path)
    issues: list[dict[str, Any]] = []
    if candidate_git.get("head") and current_git.get("head") and candidate_git.get("head") != current_git.get("head"):
        issues.append(
            {"status": WARN, "name": "candidate_head_changed", "detail": "current HEAD differs from candidate HEAD"}
        )
    if _receipt_newer_than_candidate(latest_release, candidate_created):
        issues.append({"status": WARN, "name": "newer_release_readiness", "detail": str(latest_release.get("run_id"))})
    if _receipt_newer_than_candidate(latest_verify, candidate_created):
        issues.append({"status": WARN, "name": "newer_verification", "detail": str(latest_verify.get("run_id"))})
    if _receipt_newer_than_candidate(latest_review, candidate_created):
        issues.append({"status": WARN, "name": "newer_review_run", "detail": str(latest_review.get("run_id"))})
    if _receipt_newer_than_candidate(latest_sweep, candidate_created):
        issues.append({"status": WARN, "name": "newer_scanner_sweep", "detail": str(latest_sweep.get("sweep_id"))})
    security_generated = work_cmd._parse_iso_datetime(
        (latest_security or {}).get("generated_at") if isinstance(latest_security, dict) else None
    )
    if candidate_created and security_generated and security_generated > candidate_created:
        issues.append(
            {"status": WARN, "name": "newer_security_report", "detail": str((latest_security or {}).get("path"))}
        )
    for key, value in (
        (
            "release_readiness",
            (candidate.get("release_readiness") or {}).get("path")
            if isinstance(candidate.get("release_readiness"), dict)
            else None,
        ),
        (
            "work_closeout",
            (candidate.get("work_closeout") or {}).get("path")
            if isinstance(candidate.get("work_closeout"), dict)
            else None,
        ),
        (
            "verification",
            (candidate.get("verification") or {}).get("path")
            if isinstance(candidate.get("verification"), dict)
            else None,
        ),
    ):
        if value and not Path(str(value)).exists():
            issues.append({"status": WARN, "name": f"missing_{key}_receipt", "detail": str(value)})
    if changed_docs_after_candidate:
        issues.append(
            {"status": WARN, "name": "docs_changed_after_candidate", "detail": ", ".join(changed_docs_after_candidate)}
        )
    operator_report = candidate.get("operator_report") if isinstance(candidate.get("operator_report"), dict) else {}
    candidate_report = operator_report.get("latest") if isinstance(operator_report.get("latest"), dict) else None
    current_report = center_cmd.latest_report(target)
    if isinstance(candidate_report, dict) and isinstance(current_report, dict):
        if candidate_report.get("report_id") != current_report.get("report_id"):
            issues.append(
                {"status": WARN, "name": "newer_operator_report", "detail": str(current_report.get("report_id"))}
            )
    elif (
        current_report is not None
        and candidate_created
        and _receipt_newer_than_candidate(current_report, candidate_created)
    ):
        issues.append({"status": WARN, "name": "newer_operator_report", "detail": str(current_report.get("report_id"))})
    report_health = center_cmd.report_health(target)
    top_report_issue = report_health.get("top_issue") if isinstance(report_health.get("top_issue"), dict) else None
    if (
        top_report_issue
        and top_report_issue.get("name") != "operator_report_newer_activity"
        and (current_report is not None or candidate_report is not None)
    ):
        issues.append({"status": WARN, "name": "operator_report_health", "detail": str(top_report_issue.get("detail"))})
    candidate_phase = candidate.get("phase_ledger") if isinstance(candidate.get("phase_ledger"), dict) else {}
    current_phase = phases_cmd.health(target)
    if int(current_phase.get("issue_count") or 0) != int(candidate_phase.get("issue_count") or 0):
        issues.append(
            {
                "status": WARN,
                "name": "phase_ledger_issue_count_changed",
                "detail": f"{candidate_phase.get('issue_count')} -> {current_phase.get('issue_count')}",
            }
        )
    candidate_closeout = (
        candidate_phase.get("latest_closeout") if isinstance(candidate_phase.get("latest_closeout"), dict) else None
    )
    current_closeout = (
        current_phase.get("latest_closeout") if isinstance(current_phase.get("latest_closeout"), dict) else None
    )
    if isinstance(current_closeout, dict) and (
        not isinstance(candidate_closeout, dict)
        or candidate_closeout.get("closeout_id") != current_closeout.get("closeout_id")
    ):
        issues.append(
            {"status": WARN, "name": "newer_phase_closeout", "detail": str(current_closeout.get("closeout_id"))}
        )
    candidate_report = (
        candidate_phase.get("latest_report") if isinstance(candidate_phase.get("latest_report"), dict) else None
    )
    current_phase_report = (
        current_phase.get("latest_report") if isinstance(current_phase.get("latest_report"), dict) else None
    )
    if isinstance(current_phase_report, dict) and (
        not isinstance(candidate_report, dict)
        or candidate_report.get("report_id") != current_phase_report.get("report_id")
    ):
        issues.append(
            {"status": WARN, "name": "newer_phase_report", "detail": str(current_phase_report.get("report_id"))}
        )
    candidate_session = (
        candidate_phase.get("latest_session") if isinstance(candidate_phase.get("latest_session"), dict) else None
    )
    current_session = (
        current_phase.get("latest_session") if isinstance(current_phase.get("latest_session"), dict) else None
    )
    if isinstance(current_session, dict) and (
        not isinstance(candidate_session, dict)
        or candidate_session.get("session_id") != current_session.get("session_id")
        or candidate_session.get("status") != current_session.get("status")
    ):
        issues.append({"status": WARN, "name": "newer_phase_session", "detail": str(current_session.get("session_id"))})
    candidate_session_report = (
        candidate_phase.get("latest_session_report")
        if isinstance(candidate_phase.get("latest_session_report"), dict)
        else None
    )
    current_session_report = (
        current_phase.get("latest_session_report")
        if isinstance(current_phase.get("latest_session_report"), dict)
        else None
    )
    if isinstance(current_session_report, dict) and (
        not isinstance(candidate_session_report, dict)
        or candidate_session_report.get("report_id") != current_session_report.get("report_id")
    ):
        issues.append(
            {
                "status": WARN,
                "name": "newer_phase_session_report",
                "detail": str(current_session_report.get("report_id")),
            }
        )
    candidate_session_checkpoint = (
        candidate_phase.get("latest_session_checkpoint")
        if isinstance(candidate_phase.get("latest_session_checkpoint"), dict)
        else None
    )
    current_session_checkpoint = (
        current_phase.get("latest_session_checkpoint")
        if isinstance(current_phase.get("latest_session_checkpoint"), dict)
        else None
    )
    if isinstance(candidate_session_checkpoint, dict) or isinstance(current_session_checkpoint, dict):
        candidate_checkpoint_key = (
            (
                candidate_session_checkpoint.get("checkpoint_id"),
                candidate_session_checkpoint.get("status"),
                candidate_session_checkpoint.get("suggested_next_command"),
            )
            if isinstance(candidate_session_checkpoint, dict)
            else None
        )
        current_checkpoint_key = (
            (
                current_session_checkpoint.get("checkpoint_id"),
                current_session_checkpoint.get("status"),
                current_session_checkpoint.get("suggested_next_command"),
            )
            if isinstance(current_session_checkpoint, dict)
            else None
        )
        if candidate_checkpoint_key != current_checkpoint_key:
            detail = str(
                (current_session_checkpoint or candidate_session_checkpoint or {}).get("checkpoint_id") or "missing"
            )
            issues.append({"status": WARN, "name": "phase_session_checkpoint_changed", "detail": detail})
    candidate_checkpoint_compare = (
        candidate_phase.get("latest_session_checkpoint_compare")
        if isinstance(candidate_phase.get("latest_session_checkpoint_compare"), dict)
        else None
    )
    current_checkpoint_compare = (
        current_phase.get("latest_session_checkpoint_compare")
        if isinstance(current_phase.get("latest_session_checkpoint_compare"), dict)
        else None
    )
    if isinstance(candidate_checkpoint_compare, dict) or isinstance(current_checkpoint_compare, dict):
        candidate_compare_key = (
            (
                candidate_checkpoint_compare.get("issue_count"),
                (candidate_checkpoint_compare.get("top_issue") or {}).get("name")
                if isinstance(candidate_checkpoint_compare, dict)
                and isinstance(candidate_checkpoint_compare.get("top_issue"), dict)
                else None,
            )
            if isinstance(candidate_checkpoint_compare, dict)
            else None
        )
        current_compare_key = (
            (
                current_checkpoint_compare.get("issue_count"),
                (current_checkpoint_compare.get("top_issue") or {}).get("name")
                if isinstance(current_checkpoint_compare, dict)
                and isinstance(current_checkpoint_compare.get("top_issue"), dict)
                else None,
            )
            if isinstance(current_checkpoint_compare, dict)
            else None
        )
        if candidate_compare_key != current_compare_key:
            issues.append(
                {
                    "status": WARN,
                    "name": "phase_session_checkpoint_compare_changed",
                    "detail": f"{candidate_compare_key} -> {current_compare_key}",
                }
            )
    candidate_session_gate = (
        candidate_phase.get("latest_session_gate")
        if isinstance(candidate_phase.get("latest_session_gate"), dict)
        else None
    )
    current_session_gate = (
        current_phase.get("latest_session_gate") if isinstance(current_phase.get("latest_session_gate"), dict) else None
    )
    if isinstance(candidate_session_gate, dict) or isinstance(current_session_gate, dict):
        candidate_gate_key = (
            (
                candidate_session_gate.get("safe_to_claim_complete"),
                candidate_session_gate.get("blocker_count"),
                (candidate_session_gate.get("top_blocker") or {}).get("name")
                if isinstance(candidate_session_gate, dict)
                and isinstance(candidate_session_gate.get("top_blocker"), dict)
                else None,
            )
            if isinstance(candidate_session_gate, dict)
            else None
        )
        current_gate_key = (
            (
                current_session_gate.get("safe_to_claim_complete"),
                current_session_gate.get("blocker_count"),
                (current_session_gate.get("top_blocker") or {}).get("name")
                if isinstance(current_session_gate, dict) and isinstance(current_session_gate.get("top_blocker"), dict)
                else None,
            )
            if isinstance(current_session_gate, dict)
            else None
        )
        if candidate_gate_key != current_gate_key:
            issues.append(
                {
                    "status": WARN,
                    "name": "phase_session_gate_changed",
                    "detail": f"{candidate_gate_key} -> {current_gate_key}",
                }
            )
    phase_checks = _phase_release_checks(target)
    issues.extend(
        {"status": check.get("status", WARN), "name": f"release_{check.get('name')}", "detail": check.get("detail")}
        for check in phase_checks
    )
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_path": candidate.get("path"),
        "candidate_head": candidate_git.get("head"),
        "current_head": current_git.get("head"),
        "changed_docs_after_candidate": changed_docs_after_candidate,
        "issues": issues,
        "issue_count": len(issues),
        "status": "current" if not issues else "stale",
        "suggested_next_commands": [
            "brigade release doctor",
            "brigade release candidate build",
            f"brigade release candidate closeout {candidate.get('candidate_id')} --status superseded",
        ],
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if not issues else 1
    print(f"release candidate compare: {candidate.get('candidate_id')}")
    print(f"status: {payload['status']}")
    print(f"issues: {len(issues)}")
    for issue in issues:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0 if not issues else 1


def candidate_closeout(
    *,
    target: Path,
    candidate_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    if status not in {"draft", "reviewed", "superseded", "archived"}:
        print("error: --status must be one of draft, reviewed, superseded, archived", file=sys.stderr)
        return 2
    candidate, error = _resolve_candidate(target, candidate_id)
    if candidate is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    created_at = _now().isoformat()
    payload = {
        "target": str(target),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_path": candidate.get("path"),
        "status": status,
        "reason": reason or f"release candidate marked {status}",
        "reviewed_at": created_at,
        "candidate_head": (candidate.get("git") or {}).get("head") if isinstance(candidate.get("git"), dict) else None,
        "ready": candidate.get("ready"),
        "blocker_count": len(candidate.get("blockers") or []),
        "warning_count": len(candidate.get("warnings") or []),
    }
    candidate_path = Path(str(candidate.get("path") or ""))
    if not candidate_path.is_dir():
        print(f"error: release candidate path is missing: {candidate.get('path')}", file=sys.stderr)
        return 2
    closeout_path = reportstore.write_closeout(candidate_path, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"release candidate closeout: {candidate.get('candidate_id')}")
    print(f"status: {status}")
    print(f"path: {closeout_path}")
    return 0
