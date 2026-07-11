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


def _reports_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "reports"


def _reports_archive_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "reports-archive"


def _report_diffs_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "report-diffs"


def _report_json_path(path: Path) -> Path:
    return reportstore.bundle_json_path(path, "CENTER_EVIDENCE.json")


def _read_report(path: Path) -> dict[str, Any] | None:
    return reportstore.read_bundle(path, "CENTER_EVIDENCE.json")


def _reports(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_reports_root(target)]
    if include_archived:
        roots.append(_reports_archive_root(target))
    return reportstore.list_bundles(
        roots, _read_report, id_field="report_id", skip_child=lambda name: name.endswith("archive")
    )


def latest_report(target: Path) -> dict[str, Any] | None:
    reports = _reports(target)
    return reports[0] if reports else None


def _report_diffs(target: Path) -> list[dict[str, Any]]:
    return _iter_json_files(_report_diffs_root(target), "*/diff.json")


def latest_report_diff(target: Path) -> dict[str, Any] | None:
    diffs = _report_diffs(target)
    return diffs[0] if diffs else None


def _resolve_report(target: Path, report_id: str) -> tuple[dict[str, Any] | None, str | None]:
    reports = _reports(target, include_archived=True)
    return reportstore.resolve_bundle(
        reports, report_id, id_field="report_id", label="operator report", latest=lambda: latest_report(target)
    )


def _receipt_references(payload: dict[str, Any]) -> list[str]:
    refs: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"path", "receipt_path", "log_path"} and isinstance(item, str) and item:
                    refs.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload.get("activity"))
    visit(payload.get("status"))
    return sorted(set(refs))


def _item_key(item: dict[str, Any]) -> str:
    return f"{item.get('subsystem')}:{item.get('local_id') or item.get('id')}"


def _fingerprint_payload(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _report_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    status_data = status_payload(target)
    activity_data = _activity(target)[:100]
    review_data = _reviews(target)[:100]
    release_ready = release_cmd._latest_release_receipt(target)
    release_candidate = release_cmd._latest_candidate(target)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-report"),
        "target": str(target),
        "generated_at": _now().isoformat(),
        "git": _git_snapshot(target),
        "status": status_data,
        "activity": activity_data,
        "reviews": review_data,
        "release": {
            "readiness": release_ready,
            "candidate": release_candidate,
        },
        "summaries": {
            "pending_review_count": len(review_data),
            "activity_count": len(activity_data),
            "scanner_sweep": status_data.get("scanner_sweeps"),
            "inbox_hygiene": work_cmd._inbox_hygiene_payload(target),
            "code_review": status_data.get("code_review"),
            "handoff_drafts": status_data.get("handoff_drafts"),
            "memory_care": status_data.get("memory_care")
            if "memory_care" in status_data
            else memory_cmd.health(target),
            "tool_catalog": status_data.get("tool_catalog"),
            "backup": work_cmd._backup_health(target),
            "security": status_data.get("security"),
            "projects": status_data.get("projects"),
            "learning": status_data.get("learning"),
            "context": status_data.get("context"),
            "repo_fleet": status_data.get("repo_fleet"),
            "roadmap": status_data.get("roadmap"),
        },
        "suggested_next_commands": _suggested_report_commands(status_data, review_data),
        "html_supported": True,
        "html_policy": "dependency-free escaped static report",
    }
    payload["receipt_references"] = _receipt_references(payload)
    payload["report_fingerprint"] = _fingerprint_payload(
        {
            "git": payload["git"],
            "reviews": payload["reviews"],
            "activity": payload["activity"],
            "receipt_references": payload["receipt_references"],
        }
    )
    return payload


def _suggested_report_commands(status_data: dict[str, Any], reviews_data: list[dict[str, Any]]) -> dict[str, list[str]]:
    urgent: list[str] = []
    next_steps: list[str] = []
    maintenance: list[str] = ["brigade center report build", "brigade work brief"]
    for item in reviews_data[:10]:
        command = item.get("suggested_next_command")
        if isinstance(command, str) and command:
            if item.get("severity") in {"critical", "high"} or item.get("priority") in {"urgent", "high"}:
                urgent.append(command)
            else:
                next_steps.append(command)
    report_health_data = (
        status_data.get("operator_report") if isinstance(status_data.get("operator_report"), dict) else {}
    )
    top = report_health_data.get("top_issue") if isinstance(report_health_data.get("top_issue"), dict) else None
    if top:
        maintenance.insert(0, str(top.get("suggested_next_command") or "brigade center report build"))
    return {
        "urgent": list(dict.fromkeys(urgent)),
        "next": list(dict.fromkeys(next_steps[:10])),
        "maintenance": list(dict.fromkeys(maintenance)),
    }


def _report_markdown(payload: dict[str, Any]) -> str:
    status_data = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    commands = (
        payload.get("suggested_next_commands") if isinstance(payload.get("suggested_next_commands"), dict) else {}
    )
    lines = [
        "# Operator Report",
        "",
        f"- Report: `{payload.get('report_id', 'planned')}`",
        f"- Target: `{payload.get('target')}`",
        f"- Generated: {payload.get('generated_at')}",
        f"- Git: `{(payload.get('git') or {}).get('short_head')}`",
        "",
        "## Queue",
        "",
        f"- Pending tasks: {status_data.get('pending_task_count')}",
        f"- Pending imports: {status_data.get('pending_import_count')}",
        f"- Pending reviews: {len(payload.get('reviews') if isinstance(payload.get('reviews'), list) else [])}",
        "",
        "## Suggested Commands",
        "",
    ]
    for label in ("urgent", "next", "maintenance"):
        values = commands.get(label) if isinstance(commands.get(label), list) else []
        lines.append(f"### {label.title()}")
        lines.append("")
        lines.extend(f"- `{value}`" for value in values) if values else lines.append("- none")
        lines.append("")
    lines.extend(["## Review Queue", ""])
    reviews_data = payload.get("reviews") if isinstance(payload.get("reviews"), list) else []
    for item in reviews_data[:25]:
        lines.append(
            f"- `{item.get('subsystem')}` `{item.get('id')}` [{item.get('status')}] {item.get('safe_summary')}"
        )
        if item.get("suggested_next_command"):
            lines.append(f"  - next: `{item.get('suggested_next_command')}`")
    if not reviews_data:
        lines.append("- none")
    lines.extend(["", "## Activity", ""])
    activity_data = payload.get("activity") if isinstance(payload.get("activity"), list) else []
    for item in activity_data[:25]:
        lines.append(
            f"- `{item.get('subsystem')}` `{item.get('id')}` [{item.get('status')}] {item.get('safe_summary')}"
        )
    if not activity_data:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- local report only",
            "- no daemon",
            "- no web server",
            "- no remote mutation",
            "- no automatic promotion",
        ]
    )
    return "\n".join(lines) + "\n"


def _review_groups(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    reviews_data = report.get("reviews") if isinstance(report.get("reviews"), list) else []
    status_data = report.get("status") if isinstance(report.get("status"), dict) else {}
    summaries = report.get("summaries") if isinstance(report.get("summaries"), dict) else {}
    release_data = report.get("release") if isinstance(report.get("release"), dict) else {}
    groups: dict[str, list[dict[str, Any]]] = {
        "urgent_blockers": [],
        "pending_work_imports": [],
        "code_review_findings": [],
        "handoff_drafts": [],
        "scanner_sweep_issues": [],
        "tool_approvals_checkpoints_runs": [],
        "backup_security_memory_care_issues": [],
        "release_readiness_candidate_issues": [],
        "project_learning_candidates": [],
    }
    for item in reviews_data:
        if not isinstance(item, dict):
            continue
        subsystem = str(item.get("subsystem") or "")
        priority = str(item.get("priority") or "")
        severity = str(item.get("severity") or "")
        if priority in {"urgent", "high"} or severity in {"critical", "high"}:
            groups["urgent_blockers"].append(item)
        if subsystem == "work-import":
            groups["pending_work_imports"].append(item)
        elif subsystem == "code-review":
            groups["code_review_findings"].append(item)
        elif subsystem == "handoff-draft":
            groups["handoff_drafts"].append(item)
        elif subsystem in {"scanner-run", "scanner-sweep"} or "scanner" in subsystem:
            groups["scanner_sweep_issues"].append(item)
        elif subsystem in {"tools", "tool-call", "tool-run", "checkpoint", "tool-pack"}:
            groups["tool_approvals_checkpoints_runs"].append(item)
        elif subsystem in {"backup", "security", "memory-care"}:
            groups["backup_security_memory_care_issues"].append(item)
        elif subsystem in {"release-readiness", "release-candidate"}:
            groups["release_readiness_candidate_issues"].append(item)
        elif subsystem in {"project-consolidation", "learning"}:
            groups["project_learning_candidates"].append(item)
    sweep_review = (
        summaries.get("scanner_sweep")
        if isinstance(summaries.get("scanner_sweep"), dict)
        else status_data.get("scanner_sweeps")
    )
    if isinstance(sweep_review, dict):
        top = (sweep_review.get("review") if isinstance(sweep_review.get("review"), dict) else {}).get(
            "top_pending_import"
        )
        if isinstance(top, dict):
            groups["scanner_sweep_issues"].append(
                _item(
                    "scanner-sweep",
                    str(top.get("id") or "pending-import"),
                    "pending",
                    str(top.get("text") or "pending sweep import"),
                    f"brigade work import plan {top.get('id')}",
                )
            )
    for name, command in (
        ("backup", "brigade work backup status"),
        ("security", "brigade security findings"),
        ("memory_care", "brigade memory care status"),
    ):
        value = summaries.get(name) if isinstance(summaries.get(name), dict) else None
        top = value.get("top_issue") or value.get("top_finding") if isinstance(value, dict) else None
        if isinstance(top, dict):
            groups["backup_security_memory_care_issues"].append(
                _item(
                    name.replace("_", "-"),
                    str(top.get("id") or top.get("name") or top.get("issue_type") or name),
                    str(top.get("status") or "warn"),
                    str(top.get("detail") or top.get("title") or name),
                    command,
                    severity=top.get("severity") if isinstance(top.get("severity"), str) else None,
                )
            )
    readiness = release_data.get("readiness") if isinstance(release_data.get("readiness"), dict) else None
    candidate = release_data.get("candidate") if isinstance(release_data.get("candidate"), dict) else None
    if isinstance(readiness, dict) and readiness.get("ready") is False:
        run_id = str(readiness.get("run_id") or "latest")
        groups["release_readiness_candidate_issues"].append(
            _item(
                "release-readiness",
                run_id,
                str(readiness.get("status") or "blocked"),
                "release readiness is blocked",
                f"brigade release show {run_id}",
            )
        )
    if isinstance(candidate, dict) and candidate.get("status") not in {None, "reviewed", "archived"}:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        groups["release_readiness_candidate_issues"].append(
            _item(
                "release-candidate",
                candidate_id,
                str(candidate.get("status") or "draft"),
                "release candidate awaits review",
                f"brigade release candidate compare {candidate_id}",
            )
        )
    return groups


def _action_plan(report: dict[str, Any]) -> dict[str, Any]:
    groups = _review_groups(report)
    commands: dict[str, list[str]] = {}
    for group, items in groups.items():
        values = [
            str(item.get("suggested_next_command"))
            for item in items
            if isinstance(item, dict) and item.get("suggested_next_command")
        ]
        commands[group] = list(dict.fromkeys(values))
    return {
        "groups": groups,
        "commands": commands,
        "unresolved_item_count": sum(len(items) for items in groups.values()),
    }


def _report_html(markdown: str, payload: dict[str, Any]) -> str:
    title = html.escape(f"Operator Report {payload.get('report_id', 'planned')}")
    body = html.escape(markdown)
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8"><title>'
        + title
        + "</title><style>body{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;line-height:1.45}pre{white-space:pre-wrap;background:#f6f8fa;padding:1rem;border:1px solid #d0d7de}</style></head>"
        "<body><pre>" + body + "</pre></body></html>\n"
    )


def _write_report_bundle(report_dir: Path, payload: dict[str, Any]) -> None:
    markdown = _report_markdown(payload)
    reportstore.write_bundle(
        report_dir,
        payload,
        evidence_name="CENTER_EVIDENCE.json",
        documents={"OPERATOR_REPORT.md": markdown, "OPERATOR_REPORT.html": _report_html(markdown, payload)},
    )


def report_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = latest_report(target)
    reports = _reports(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append(
            {
                "status": "warn",
                "name": "operator_report_missing",
                "detail": "no local operator report has been built",
                "suggested_next_command": "brigade center report build",
            }
        )
        return {"latest": None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    closeout_status = str(closeout.get("status") or "") if closeout else ""
    if closeout_status not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append(
            {
                "status": "warn",
                "name": "operator_report_unclosed",
                "detail": f"{latest.get('report_id')} has not been closed out",
                "suggested_next_command": f"brigade center report review {latest.get('report_id')}",
            }
        )
    created = _parse_time(latest.get("created_at") or latest.get("generated_at"))
    if created is not None:
        age_hours = (_now() - created).total_seconds() / 3600
        if age_hours > REPORT_STALE_HOURS:
            checks.append(
                {
                    "status": "warn",
                    "name": "operator_report_stale",
                    "detail": f"{latest.get('report_id')}={age_hours:.1f}h",
                    "suggested_next_command": "brigade center report build",
                }
            )
    current_head = _git_value(target, "rev-parse", "HEAD")
    git = latest.get("git") if isinstance(latest.get("git"), dict) else {}
    if git.get("head") and current_head and git.get("head") != current_head:
        checks.append(
            {
                "status": "warn",
                "name": "operator_report_head_changed",
                "detail": f"{latest.get('report_id')} head changed",
                "suggested_next_command": "brigade center report build",
            }
        )
    for ref in latest.get("receipt_references") if isinstance(latest.get("receipt_references"), list) else []:
        if isinstance(ref, str) and ref and not _receipt_reference_exists(target, ref):
            checks.append(
                {
                    "status": "warn",
                    "name": "operator_report_missing_receipt",
                    "detail": f"missing receipt reference: {_path_label(target, ref)}",
                    "suggested_next_command": f"brigade center report show {latest.get('report_id')}",
                }
            )
            break
    latest_activity = [item for item in _activity(target) if item.get("subsystem") != "center-report-diff"]
    report_activity = latest.get("activity") if isinstance(latest.get("activity"), list) else []
    latest_time = _parse_time(latest_activity[0].get("updated_at")) if latest_activity else None
    report_time = _parse_time(report_activity[0].get("updated_at")) if report_activity else created
    if latest_time is not None and report_time is not None and latest_time > report_time:
        checks.append(
            {
                "status": "warn",
                "name": "operator_report_newer_activity",
                "detail": f"{latest.get('report_id')} is older than local activity",
                "suggested_next_command": "brigade center report build",
            }
        )
    latest_diff = latest_report_diff(target)
    if len(reports) >= 2:
        compare_report = reports[0]
        base_report = reports[1]
        if latest_diff is None:
            checks.append(
                {
                    "status": "warn",
                    "name": "operator_report_diff_missing",
                    "detail": f"{base_report.get('report_id')} -> {compare_report.get('report_id')} has no local diff receipt",
                    "suggested_next_command": f"brigade center report diff {base_report.get('report_id')} {compare_report.get('report_id')} --record",
                }
            )
        elif latest_diff.get("base_report_id") != base_report.get("report_id") or latest_diff.get(
            "compare_report_id"
        ) != compare_report.get("report_id"):
            checks.append(
                {
                    "status": "warn",
                    "name": "operator_report_diff_stale",
                    "detail": f"latest diff does not cover {base_report.get('report_id')} -> {compare_report.get('report_id')}",
                    "suggested_next_command": f"brigade center report diff {base_report.get('report_id')} {compare_report.get('report_id')} --record",
                }
            )
        elif (
            int((latest_diff.get("summary") or {}).get("new_blocker_count") or 0) > 0
            or int((latest_diff.get("summary") or {}).get("stale_reference_count") or 0) > 0
        ):
            checks.append(
                {
                    "status": "warn",
                    "name": "operator_report_diff_has_issues",
                    "detail": f"{latest_diff.get('diff_id')} has new blockers or stale references",
                    "suggested_next_command": f"brigade center report diff {latest_diff.get('base_report_id')} {latest_diff.get('compare_report_id')}",
                }
            )
    return {
        "latest": latest,
        "latest_diff": latest_diff,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def report_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = _report_payload(target)
    payload.update(
        {
            "report_id": "planned",
            "report_root": str(_reports_root(target)),
            "bundle_files": ["OPERATOR_REPORT.md", "OPERATOR_REPORT.html", "CENTER_EVIDENCE.json"],
        }
    )
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator report plan: {target}")
    print(f"reviews: {len(payload['reviews'])}")
    print(f"activity: {len(payload['activity'])}")
    print(f"report_root: {payload['report_root']}")
    print("run: brigade center report build")
    return 0


def report_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    created = _now()
    report_id = f"{created.strftime('%Y%m%d-%H%M%S')}-operator-report-{uuid4().hex[:6]}"
    report_dir = _reports_root(target) / report_id
    payload = _report_payload(target)
    payload.update(
        {
            "report_id": report_id,
            "created_at": created.isoformat(),
            "path": str(report_dir),
            "bundle_files": ["OPERATOR_REPORT.md", "OPERATOR_REPORT.html", "CENTER_EVIDENCE.json"],
        }
    )
    _write_report_bundle(report_dir, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator report: {report_id}")
    print(f"reviews: {len(payload['reviews'])}")
    print(f"activity: {len(payload['activity'])}")
    print(f"path: {report_dir}")
    return 0


def report_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    reports = _reports(target)[:limit]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-report-list"),
        "target": str(target),
        "reports_root": str(_reports_root(target)),
        "reports": reports,
        "report_count": len(reports),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator reports: {target}")
    print(f"reports_root: {payload['reports_root']}")
    for item in reports:
        print(
            f"- {item.get('report_id')} reviews={len(item.get('reviews') if isinstance(item.get('reviews'), list) else [])} {item.get('created_at')}"
        )
    return 0


def report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "schema": _schema("center-report-show"),
                    "target": str(target),
                    "report": report,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"operator report: {report.get('report_id')}")
    print(f"path: {report.get('path')}")
    print(f"created_at: {report.get('created_at')}")
    print(f"reviews: {len(report.get('reviews') if isinstance(report.get('reviews'), list) else [])}")
    print(f"activity: {len(report.get('activity') if isinstance(report.get('activity'), list) else [])}")
    return 0


def report_archive(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    source = Path(str(report.get("path") or _reports_root(target) / str(report.get("report_id"))))
    if not source.is_dir():
        print(f"error: operator report path is missing: {source}", file=sys.stderr)
        return 2
    destination, moved = reportstore.move_bundle(source, _reports_archive_root(target))
    if not moved:
        print(f"error: archived operator report already exists: {destination}", file=sys.stderr)
        return 2
    payload = {
        "schema_version": SCHEMA_VERSION,
        "target": str(target),
        "report_id": report.get("report_id"),
        "status": "archived",
        "archive_path": str(destination),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived operator report: {report.get('report_id')}")
    print(f"path: {destination}")
    return 0
