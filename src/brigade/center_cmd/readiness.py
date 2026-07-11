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

from . import schema_ops as _family_base

globals().update({name: value for name, value in vars(_family_base).items() if not name.startswith("__")})


def _readiness_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "readiness"


def _readiness_archive_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "readiness-archive"


def _readiness_waivers_path(target: Path) -> Path:
    return _readiness_root(target) / "waivers.jsonl"


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _readiness_waivers(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_readiness_waivers_path(target))


def _active_readiness_waivers(target: Path) -> dict[str, dict[str, Any]]:
    return {
        str(waiver.get("finding_id")): waiver
        for waiver in _readiness_waivers(target)
        if waiver.get("status") == "active" and waiver.get("finding_id")
    }


def _readiness_finding(
    subsystem: str, name: str, severity: str, summary: str, command: str, *, status: str = "warn"
) -> dict[str, Any]:
    fingerprint = _fingerprint_payload({"subsystem": subsystem, "name": name, "severity": severity, "summary": summary})
    return {
        "finding_id": f"readiness-{fingerprint[:16]}",
        "subsystem": subsystem,
        "name": name,
        "status": status,
        "severity": severity,
        "safe_summary": summary,
        "suggested_next_command": command,
        "source_fingerprint": fingerprint,
    }


def _readiness_safe_text(target: Path, value: str) -> str:
    text = value.replace(str(target), "<target>")
    return re.sub(r"/(?:tmp|home|Users|private|mnt|Volumes)/[A-Za-z0-9_.@/-]+", "<path>", text)


def _readiness_findings(target: Path, status_data: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if status_data.get("pending_task_count", 0) > 0:
        findings.append(
            _readiness_finding(
                "work",
                "pending_tasks",
                "warning",
                f"{status_data['pending_task_count']} pending task(s)",
                "brigade work tasks",
            )
        )
    if status_data.get("pending_import_count", 0) > 0:
        findings.append(
            _readiness_finding(
                "work",
                "pending_imports",
                "blocker",
                f"{status_data['pending_import_count']} pending import(s)",
                "brigade work inbox",
                status="blocked",
            )
        )
    if status_data.get("review_queue_count", 0) > 0:
        findings.append(
            _readiness_finding(
                "center",
                "pending_reviews",
                "warning",
                f"{status_data['review_queue_count']} pending review item(s)",
                "brigade center reviews",
            )
        )
    release = status_data.get("release_readiness") if isinstance(status_data.get("release_readiness"), dict) else None
    if not release:
        findings.append(
            _readiness_finding(
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
            _readiness_finding(
                "release",
                "blocked_release_readiness",
                "blocker",
                "release readiness is blocked",
                f"brigade release show {run_id}",
                status="blocked",
            )
        )
    for subsystem, command in (
        ("roadmap", "brigade roadmap audit"),
        ("repo_fleet", "brigade repos doctor"),
        ("security", "brigade security doctor"),
        ("memory_care", "brigade memory care doctor"),
        ("backup", "brigade work backup doctor"),
        ("tool_catalog", "brigade tools doctor"),
        ("learning", "brigade learn doctor"),
        ("context", "brigade context doctor"),
        ("projects", "brigade projects audit"),
        ("action_queue", "brigade center actions doctor"),
    ):
        health = status_data.get(subsystem)
        if not isinstance(health, dict):
            continue
        issue_count = int(health.get("issue_count") or health.get("open_count") or 0)
        top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else None
        if issue_count <= 0 and not top:
            continue
        detail = _readiness_safe_text(
            target,
            str(
                (top or {}).get("detail")
                or (top or {}).get("safe_summary")
                or f"{subsystem} has unresolved health issue(s)"
            ),
        )
        findings.append(
            _readiness_finding(subsystem, str((top or {}).get("name") or "health"), "warning", detail, command)
        )
    command_health = roadmap_cmd.command_contract_payload(target)
    if not command_health.get("inventory_current"):
        findings.append(
            _readiness_finding(
                "roadmap",
                "command_inventory_stale",
                "blocker",
                "docs command inventory is missing or stale",
                "brigade roadmap commands --write",
                status="blocked",
            )
        )
    return findings


def _readiness_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    status_data = status_payload(target)
    status_data["review_queue_count"] = len(
        [item for item in _reviews(target) if item.get("subsystem") != "center-readiness"]
    )
    findings = _readiness_findings(target, status_data)
    waivers = _active_readiness_waivers(target)
    for finding in findings:
        waiver = waivers.get(str(finding.get("finding_id")))
        finding["waived"] = bool(waiver)
        if waiver:
            finding["waiver_id"] = waiver.get("waiver_id")
            finding["waiver_reason"] = waiver.get("reason")
    unwaived_blockers = [
        finding for finding in findings if finding.get("severity") == "blocker" and not finding.get("waived")
    ]
    warnings = [finding for finding in findings if finding.get("severity") != "blocker" and not finding.get("waived")]
    manual_checklist = [
        {"label": "roadmap audit", "command": "brigade roadmap audit --json", "manual_only": True},
        {"label": "command inventory check", "command": "brigade roadmap commands --check", "manual_only": True},
        {"label": "operator center status", "command": "brigade center status --json", "manual_only": True},
        {"label": "release doctor", "command": "brigade release doctor", "manual_only": True},
        {
            "label": "release candidate compare",
            "command": "brigade release candidate compare latest",
            "manual_only": True,
        },
        {"label": "diff check", "command": "git diff --check", "manual_only": True},
        {"label": "full tests", "command": "PYTHONPATH=src python3 -m pytest -q", "manual_only": True},
        {
            "label": "manual tag placeholder",
            "command": "manual tag command after explicit operator approval",
            "manual_only": True,
            "remote_mutation": True,
        },
        {
            "label": "manual push placeholder",
            "command": "manual push command after explicit operator approval",
            "manual_only": True,
            "remote_mutation": True,
        },
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-readiness"),
        "target": str(target),
        "created_at": _now().isoformat(),
        "ready": not unwaived_blockers,
        "status": "ready" if not unwaived_blockers else "blocked",
        "finding_count": len(findings),
        "blocker_count": len(unwaived_blockers),
        "warning_count": len(warnings),
        "waived_count": sum(1 for finding in findings if finding.get("waived")),
        "findings": findings,
        "blockers": unwaived_blockers,
        "warnings": warnings,
        "waivers": list(waivers.values()),
        "manual_publish_checklist": manual_checklist,
        "source_fingerprint": _fingerprint_payload({"findings": findings, "checklist": manual_checklist}),
    }
    return payload


def _readiness_receipts(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_readiness_root(target)]
    if include_archived:
        roots.append(_readiness_archive_root(target))
    receipts: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            payload = _read_json(child / "readiness.json")
            if payload is not None:
                payload.setdefault("path", str(child))
                receipts.append(payload)
    receipts.sort(key=lambda item: str(item.get("created_at") or item.get("readiness_id") or ""), reverse=True)
    return receipts


def _latest_readiness(target: Path) -> dict[str, Any] | None:
    receipts = _readiness_receipts(target)
    return receipts[0] if receipts else None


def readiness_health(target: Path) -> dict[str, Any]:
    latest = _latest_readiness(target)
    issues: list[dict[str, Any]] = []
    if latest is None:
        issues.append(
            {
                "status": "warn",
                "name": "center_readiness_missing",
                "detail": "no local operator readiness closeout found",
            }
        )
    elif latest.get("ready") is False and latest.get("review_status") != "deferred":
        issues.append(
            {
                "status": "warn",
                "name": "center_readiness_blocked",
                "detail": str(latest.get("readiness_id") or "latest"),
            }
        )
    return {
        "latest": latest,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "checks": issues,
    }


def readiness_plan(*, target: Path, json_output: bool = False) -> int:
    payload = _readiness_payload(target)
    rc = 0 if payload["ready"] else 1
    if json_output:
        return emit(payload, json_output, [], rc)
    text_lines = [
        f"center readiness plan: {payload['target']}",
        f"status: {payload['status']}",
        f"blockers: {payload['blocker_count']}",
        f"warnings: {payload['warning_count']}",
    ]
    for finding in payload["findings"][:20]:
        waived = " waived" if finding.get("waived") else ""
        text_lines.append(f"- {finding['finding_id']} {finding['severity']}{waived}: {finding['safe_summary']}")
        text_lines.append(f"  next: {finding['suggested_next_command']}")
    return emit(payload, json_output, text_lines, rc)


def readiness_closeout(
    *,
    target: Path,
    status: str = "reviewed",
    reason: str | None = None,
    waive_finding_ids: list[str] | None = None,
    json_output: bool = False,
) -> int:
    if status not in READINESS_STATUSES:
        print(f"error: invalid readiness closeout status: {status}", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    initial = _readiness_payload(target)
    now = _now().isoformat()
    waivers = _readiness_waivers(target)
    waive_finding_ids = waive_finding_ids or []
    findings_by_id = {str(finding.get("finding_id")): finding for finding in initial["findings"]}
    for finding_id in waive_finding_ids:
        finding = findings_by_id.get(finding_id)
        if finding is None:
            print(f"error: readiness finding not found: {finding_id}", file=sys.stderr)
            return 1
        waiver_id = f"readiness-waiver-{_fingerprint_payload({'finding_id': finding_id, 'source': finding.get('source_fingerprint')})[:16]}"
        waivers = [waiver for waiver in waivers if waiver.get("waiver_id") != waiver_id]
        waivers.append(
            {
                "waiver_id": waiver_id,
                "finding_id": finding_id,
                "status": "active",
                "reason": reason or "operator-reviewed local waiver",
                "created_at": now,
                "source_fingerprint": finding.get("source_fingerprint"),
            }
        )
    if waive_finding_ids:
        _write_jsonl(_readiness_waivers_path(target), waivers)
    payload = _readiness_payload(target)
    readiness_id = f"readiness-{now.replace(':', '').replace('+', 'Z')}"
    payload.update(
        {
            "readiness_id": readiness_id,
            "review_status": status,
            "review_reason": reason,
            "completed_at": now,
        }
    )
    path = _readiness_root(target) / readiness_id
    payload["path"] = str(path)
    _write_json(path / "readiness.json", payload)
    (path / "MANUAL_PUBLISH_CHECKLIST.md").write_text(_readiness_checklist_markdown(payload))
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] or status == "deferred" else 1
    print(f"center readiness closeout: {readiness_id}")
    print(f"status: {payload['status']}")
    print(f"review_status: {status}")
    print(f"blockers: {payload['blocker_count']}")
    return 0 if payload["ready"] or status == "deferred" else 1


def _readiness_checklist_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Local Operator Readiness Closeout",
        "",
        f"- Readiness id: `{payload.get('readiness_id')}`",
        f"- Status: `{payload.get('status')}`",
        f"- Review status: `{payload.get('review_status')}`",
        f"- Blockers: {payload.get('blocker_count')}",
        f"- Warnings: {payload.get('warning_count')}",
        "",
        "## Manual Publish Checklist",
        "",
    ]
    for item in payload.get("manual_publish_checklist", []):
        if isinstance(item, dict):
            marker = "manual only"
            if item.get("remote_mutation"):
                marker = "manual only, remote mutation"
            lines.append(f"- [{marker}] {item.get('label')}: `{item.get('command')}`")
    lines.append("")
    return "\n".join(lines)


def readiness_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipts = _readiness_receipts(target, include_archived=True)[:limit]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-readiness-list"),
        "target": str(target),
        "readiness": receipts,
        "readiness_count": len(receipts),
    }
    if json_output:
        return emit(payload, json_output, [], 0)
    text_lines = [f"center readiness list: {target}"]
    for receipt in receipts:
        text_lines.append(f"- {receipt.get('readiness_id')} [{receipt.get('status')}] {receipt.get('review_status')}")
    return emit(payload, json_output, text_lines, 0)


def _resolve_readiness(target: Path, readiness_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _readiness_receipts(target, include_archived=True)
    if readiness_id == "latest":
        latest = _latest_readiness(target)
        return (latest, None) if latest else (None, "readiness closeout not found: latest")
    matches = [item for item in receipts if str(item.get("readiness_id") or "").startswith(readiness_id)]
    if not matches:
        return None, f"readiness closeout not found: {readiness_id}"
    if len(matches) > 1:
        return None, f"readiness closeout id is ambiguous: {readiness_id}"
    return matches[0], None


def readiness_show(*, target: Path, readiness_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipt, error = _resolve_readiness(target, readiness_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    print(f"center readiness: {receipt.get('readiness_id')}")
    print(f"status: {receipt.get('status')}")
    print(f"review_status: {receipt.get('review_status')}")
    print(f"blockers: {receipt.get('blocker_count')}")
    for finding in receipt.get("findings", [])[:20]:
        if isinstance(finding, dict):
            print(f"- {finding.get('finding_id')} {finding.get('severity')}: {finding.get('safe_summary')}")
    return 0


def readiness_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _readiness_payload(target)
    records = []
    for finding in payload["findings"]:
        if finding.get("waived"):
            continue
        records.append(
            {
                "text": f"Resolve local operator readiness issue: {finding['safe_summary']}",
                "kind": "task",
                "source": "center-readiness",
                "type": "ops",
                "priority": "high" if finding.get("severity") == "blocker" else "normal",
                "template": "bugfix",
                "acceptance": [
                    "The readiness issue is resolved, deferred, or explicitly waived with a local reason.",
                    "The local operator readiness plan no longer reports this item as an unreviewed blocker.",
                ],
                "metadata": {
                    "issue_type": finding.get("name"),
                    "subsystem": finding.get("subsystem"),
                    "safe_summary": finding.get("safe_summary"),
                    "source_item_key": finding.get("finding_id"),
                    "source_fingerprint": finding.get("source_fingerprint"),
                    "suggested_next_command": finding.get("suggested_next_command"),
                },
            }
        )
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    dismissed: list[dict[str, Any]] = []
    if not dry_run:
        imported, skipped, dismissed = work_cmd._append_import_records(target, records)
    result = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-readiness-import-issues"),
        "target": str(target),
        "dry_run": dry_run,
        "candidate_count": len(records),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
        "records": records if dry_run else [],
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("center readiness import-issues")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    return 0
