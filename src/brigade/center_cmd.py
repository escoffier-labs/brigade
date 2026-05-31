"""Read-only local operator center views."""
from __future__ import annotations

import html
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import chat_cmd, context_cmd, handoff_cmd, learn_cmd, memory_cmd, phases_cmd, projects_cmd, release_cmd, repos_cmd, roadmap_cmd, security_cmd, tools_cmd, work_cmd

SCHEMA_VERSION = 1
SCHEMA_MANIFEST_VERSION = 1
REPORT_STALE_HOURS = 24
ACTION_STATUSES = {"pending", "active", "done", "deferred", "archived"}
ACTION_PENDING_STALE_HOURS = 24
ACTION_ACTIVE_STALE_HOURS = 8
ACTION_DEFERRED_STALE_HOURS = 72
ACTION_DONE_ARCHIVE_HOURS = 24
READINESS_STATUSES = {"reviewed", "deferred", "blocked", "archived"}
CENTER_REQUIRED_SCHEMA_IDS = {
    "center-status",
    "center-activity",
    "center-reviews",
    "center-templates",
    "center-report",
    "center-report-review",
    "center-report-diff",
    "center-actions",
    "center-readiness",
    "center-contract-health",
}
CENTER_REQUIRED_ITEM_FIELDS = {
    "subsystem",
    "id",
    "local_id",
    "status",
    "safe_summary",
    "suggested_next_command",
}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _schema(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": SCHEMA_VERSION,
        "item_fields": [
            "subsystem",
            "id",
            "local_id",
            "status",
            "priority",
            "severity",
            "safe_summary",
            "created_at",
            "updated_at",
            "receipt_path",
            "path",
            "suggested_next_command",
        ],
    }


def _action_schema(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "version": SCHEMA_VERSION,
        "action_fields": [
            "action_id",
            "source_report_id",
            "source_group",
            "source_subsystem",
            "source_local_id",
            "status",
            "priority",
            "severity",
            "safe_summary",
            "suggested_command",
            "created_at",
            "updated_at",
            "reviewed_at",
            "source_fingerprint",
        ],
    }


def _schema_field(name: str, value_type: str, *, required: bool = True, description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "type": value_type,
        "required": required,
        "description": description,
    }


def _center_schema_manifest_schemas() -> list[dict[str, Any]]:
    item_fields = [
        _schema_field("subsystem", "string", description="Owning local Brigade subsystem."),
        _schema_field("id", "string", description="Stable local item id."),
        _schema_field("local_id", "string", description="Subsystem-local id used for drill-down commands."),
        _schema_field("status", "string", description="Current local status."),
        _schema_field("priority", "string|null", required=False, description="Priority when available."),
        _schema_field("severity", "string|null", required=False, description="Severity when available."),
        _schema_field("safe_summary", "string", description="Redacted operator-facing summary."),
        _schema_field("created_at", "string|null", required=False, description="ISO timestamp when available."),
        _schema_field("updated_at", "string|null", required=False, description="ISO timestamp when available."),
        _schema_field("receipt_path", "string|null", required=False, description="Local receipt path label when available."),
        _schema_field("path", "string|null", required=False, description="Local artifact path label when available."),
        _schema_field("suggested_next_command", "string", description="Manual local command to inspect or act."),
    ]
    action_fields = [
        _schema_field("action_id", "string", description="Stable action id."),
        _schema_field("source_report_id", "string", description="Operator report that produced the action."),
        _schema_field("source_group", "string", description="Review group from the source report."),
        _schema_field("source_subsystem", "string", description="Subsystem that owns the source item."),
        _schema_field("source_local_id", "string", description="Subsystem-local source item id."),
        _schema_field("status", "string", description="One of pending, active, done, deferred, archived."),
        _schema_field("priority", "string|null", required=False, description="Priority when available."),
        _schema_field("severity", "string|null", required=False, description="Severity when available."),
        _schema_field("safe_summary", "string", description="Redacted operator-facing summary."),
        _schema_field("suggested_command", "string", description="Manual local command, never auto-executed."),
        _schema_field("created_at", "string", description="ISO timestamp."),
        _schema_field("updated_at", "string", description="ISO timestamp."),
        _schema_field("reviewed_at", "string|null", required=False, description="Source report review timestamp when available."),
        _schema_field("source_fingerprint", "string", description="Stable dedupe fingerprint."),
    ]
    return [
        {
            "id": "center-status",
            "command": "brigade center status --json",
            "description": "Read-only aggregate of local operator-center subsystem health.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("active_session", "object|null", required=False),
                _schema_field("pending_task_count", "integer"),
                _schema_field("pending_import_count", "integer"),
                _schema_field("review_queue_count", "integer"),
                _schema_field("operator_report", "object"),
                _schema_field("action_queue", "object"),
                _schema_field("release_readiness", "object|null", required=False),
                _schema_field("release_candidate", "object|null", required=False),
            ],
        },
        {
            "id": "center-activity",
            "command": "brigade center activity --json",
            "description": "Unified local receipt activity ledger.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("activity", "array"),
                _schema_field("activity_count", "integer"),
            ],
            "item_fields": item_fields,
        },
        {
            "id": "center-reviews",
            "command": "brigade center reviews --json",
            "description": "Unified pending local review queue.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("reviews", "array"),
                _schema_field("review_count", "integer"),
            ],
            "item_fields": item_fields,
        },
        {
            "id": "center-templates",
            "command": "brigade center templates --json",
            "description": "Local templates exposed to wrappers.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("templates", "array"),
                _schema_field("template_count", "integer"),
            ],
            "item_fields": item_fields,
        },
        {
            "id": "center-report",
            "command": "brigade center report plan --json",
            "description": "Operator report evidence contract used by planned and built report bundles.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("generated_at", "string"),
                _schema_field("git", "object"),
                _schema_field("status", "object"),
                _schema_field("activity", "array"),
                _schema_field("reviews", "array"),
                _schema_field("summaries", "object"),
                _schema_field("suggested_next_commands", "object"),
                _schema_field("receipt_references", "array"),
                _schema_field("report_fingerprint", "string"),
                _schema_field("report_id", "string", required=False),
                _schema_field("bundle_files", "array", required=False),
            ],
            "item_fields": item_fields,
        },
        {
            "id": "center-report-review",
            "command": "brigade center report review latest --json",
            "description": "Grouped action-plan view over one operator report.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("report_id", "string"),
                _schema_field("report_path", "string|null", required=False),
                _schema_field("action_plan", "object"),
                _schema_field("suggested_next_commands", "object"),
            ],
            "item_fields": item_fields,
        },
        {
            "id": "center-report-diff",
            "command": "brigade center report diff <base-report-id> <compare-report-id> --json",
            "description": "Two-report diff contract for changed review queues, resolved items, new blockers, and stale references.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("diff_id", "string"),
                _schema_field("base_report_id", "string"),
                _schema_field("compare_report_id", "string"),
                _schema_field("status", "string"),
                _schema_field("summary", "object"),
                _schema_field("new_items", "array"),
                _schema_field("resolved_items", "array"),
                _schema_field("changed_items", "array"),
                _schema_field("new_blockers", "array"),
                _schema_field("stale_references", "array"),
                _schema_field("diff_fingerprint", "string"),
                _schema_field("path", "string|null", required=False),
            ],
            "item_fields": item_fields,
        },
        {
            "id": "center-actions",
            "command": "brigade center actions list --json",
            "description": "Daily operator action queue contract.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("actions_path", "string"),
                _schema_field("actions", "array"),
                _schema_field("action_count", "integer"),
                _schema_field("counts", "object"),
            ],
            "action_fields": action_fields,
        },
        {
            "id": "center-readiness",
            "command": "brigade center readiness plan --json",
            "description": "Local operator readiness closeout over roadmap, center, release, repo fleet, security, memory, tools, context, learning, and docs command contracts.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("ready", "boolean"),
                _schema_field("status", "string"),
                _schema_field("findings", "array"),
                _schema_field("blockers", "array"),
                _schema_field("warnings", "array"),
                _schema_field("manual_publish_checklist", "array"),
            ],
            "item_fields": item_fields,
        },
        {
            "id": "center-contract-health",
            "command": "internal: center contract health",
            "description": "Read-only contract audit used by daily hardening and release evidence.",
            "top_level_fields": [
                _schema_field("schema_version", "integer"),
                _schema_field("schema", "object"),
                _schema_field("target", "string"),
                _schema_field("required_schema_ids", "array"),
                _schema_field("schema_ids", "array"),
                _schema_field("required_item_fields", "array"),
                _schema_field("issues", "array"),
                _schema_field("issue_count", "integer"),
            ],
            "item_fields": item_fields,
        },
    ]


def _center_schema_manifest(target: Path) -> dict[str, Any]:
    schemas = _center_schema_manifest_schemas()
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_version": SCHEMA_MANIFEST_VERSION,
        "schema": {
            "name": "center-schema-manifest",
            "version": SCHEMA_MANIFEST_VERSION,
        },
        "target": str(target.expanduser().resolve()),
        "read_only": True,
        "write_required": False,
        "schema_count": len(schemas),
        "schemas": schemas,
        "checks": [
            {
                "status": "ok",
                "name": "center_schema_manifest_read_only",
                "detail": "schema export does not inspect or mutate local receipts",
            },
            {
                "status": "ok",
                "name": "wrapper_field_contracts_present",
                "detail": "status, activity, reviews, templates, reports, report review, report diff, actions, readiness, and contract health are described",
            },
        ],
    }


def _unsafe_reference(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if re.search(r"(?i)(token|secret|password|api[_-]?key)=\S+", value):
        return "secret-looking value"
    if re.search(r"https?://", value):
        return "url reference"
    return None


def _center_contract_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    manifest = _center_schema_manifest(target)
    schemas = manifest.get("schemas") if isinstance(manifest.get("schemas"), list) else []
    schema_ids = {str(item.get("id")) for item in schemas if isinstance(item, dict)}
    issues: list[dict[str, Any]] = []
    missing_schema_ids = sorted(CENTER_REQUIRED_SCHEMA_IDS - schema_ids)
    if missing_schema_ids:
        issues.append(
            {
                "status": "fail",
                "name": "center_schema_missing_required_ids",
                "detail": ", ".join(missing_schema_ids),
                "phase": 125,
                "suggested_next_command": "brigade center schema",
            }
        )

    outputs: dict[str, Any] = {
        "activity": _activity(target)[:100],
        "reviews": _reviews(target)[:100],
        "templates": [
            _item("context", "task", "available", "Task context pack template", "brigade context plan --kind task"),
            _item("context", "repo", "available", "Repo context pack template", "brigade context plan --kind repo"),
            _item("context", "release", "available", "Release context pack template", "brigade context plan --kind release"),
            _item("tools", "tool-pack", "available", "Portable tool pack template", "brigade tools pack build"),
            _item("projects", "audit-plan", "available", "Project audit plan template", "brigade projects audit"),
            _item("release", "candidate", "available", "Release candidate checklist template", "brigade release candidate plan"),
            _item("review", "closeout", "available", "Review closeout template", "brigade work review closeout latest"),
        ],
    }
    status_data = status_payload(target)
    status_required = {"schema_version", "schema", "target", "pending_task_count", "pending_import_count", "review_queue_count", "operator_report", "action_queue"}
    missing_status_fields = sorted(status_required - set(status_data))
    if missing_status_fields:
        issues.append(
            {
                "status": "fail",
                "name": "center_status_missing_fields",
                "detail": ", ".join(missing_status_fields),
                "phase": 131,
                "suggested_next_command": "brigade center status --json",
            }
        )
    item_issues: list[dict[str, Any]] = []
    for output_name, items in outputs.items():
        for item in items:
            if not isinstance(item, dict):
                item_issues.append({"output": output_name, "field": "item", "local_id": None})
                continue
            missing = sorted(CENTER_REQUIRED_ITEM_FIELDS - set(item))
            if missing:
                item_issues.append({"output": output_name, "field": ",".join(missing), "local_id": item.get("local_id") or item.get("id")})
            command = item.get("suggested_next_command")
            if not isinstance(command, str) or not command.startswith("brigade "):
                item_issues.append({"output": output_name, "field": "suggested_next_command", "local_id": item.get("local_id") or item.get("id")})
            for key in ("path", "receipt_path"):
                reason = _unsafe_reference(item.get(key))
                if reason:
                    item_issues.append({"output": output_name, "field": key, "local_id": item.get("local_id") or item.get("id"), "reason": reason})
    if item_issues:
        issues.append(
            {
                "status": "warn",
                "name": "center_item_contract_gap",
                "detail": f"{len(item_issues)} center item contract gap(s)",
                "phase": 126,
                "suggested_next_command": "brigade center reviews --json",
                "items": item_issues[:20],
            }
        )
    command_gaps = [
        {"output": issue["output"], "local_id": issue.get("local_id")}
        for issue in item_issues
        if issue.get("field") == "suggested_next_command"
    ]
    if command_gaps:
        issues.append(
            {
                "status": "warn",
                "name": "center_missing_suggested_commands",
                "detail": f"{len(command_gaps)} center item(s) have missing suggested commands",
                "phase": 127,
                "suggested_next_command": "brigade center reviews --json",
                "items": command_gaps[:20],
            }
        )
    reference_gaps = [
        issue
        for issue in item_issues
        if issue.get("field") in {"path", "receipt_path"}
    ]
    if reference_gaps:
        issues.append(
            {
                "status": "warn",
                "name": "center_unsafe_references",
                "detail": f"{len(reference_gaps)} center reference(s) need safer labels",
                "phase": 128,
                "suggested_next_command": "brigade center activity --json",
                "items": reference_gaps[:20],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-contract-health"),
        "target": str(target),
        "required_schema_ids": sorted(CENTER_REQUIRED_SCHEMA_IDS),
        "schema_ids": sorted(schema_ids),
        "missing_schema_ids": missing_schema_ids,
        "required_item_fields": sorted(CENTER_REQUIRED_ITEM_FIELDS),
        "status_field_count": len(status_data),
        "activity_count": len(outputs["activity"]),
        "review_count": len(outputs["reviews"]),
        "template_count": len(outputs["templates"]),
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _git_value(target: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(target), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _git_snapshot(target: Path) -> dict[str, Any]:
    return {
        "head": _git_value(target, "rev-parse", "HEAD"),
        "short_head": _git_value(target, "rev-parse", "--short", "HEAD"),
        "branch": _git_value(target, "branch", "--show-current"),
    }


def _item(
    subsystem: str,
    local_id: str,
    status: str,
    summary: str,
    command: str,
    *,
    priority: str | None = None,
    severity: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    receipt_path: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    return {
        "subsystem": subsystem,
        "id": local_id,
        "local_id": local_id,
        "status": status,
        "priority": priority,
        "severity": severity,
        "safe_summary": summary,
        "created_at": created_at,
        "updated_at": updated_at,
        "receipt_path": receipt_path,
        "path": path,
        "suggested_next_command": command,
    }


def _iter_json_files(root: Path, pattern: str) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(root.glob(pattern)):
        payload = _read_json(path)
        if payload is not None:
            payload.setdefault("path", str(path))
            items.append(payload)
    items.sort(key=lambda item: str(item.get("completed_at") or item.get("created_at") or item.get("started_at") or item.get("path") or ""), reverse=True)
    return items


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _actions_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "actions"


def _actions_path(target: Path) -> Path:
    return _actions_root(target) / "actions.json"


def _actions_archive_path(target: Path) -> Path:
    return _actions_root(target) / "archive.jsonl"


def _read_actions(target: Path) -> list[dict[str, Any]]:
    payload = _read_json(_actions_path(target))
    if payload is None:
        return []
    actions = payload.get("actions")
    if not isinstance(actions, list):
        return []
    return [item for item in actions if isinstance(item, dict)]


def _read_action_archive(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_actions_archive_path(target))


def _write_actions(target: Path, actions: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-store"),
        "updated_at": _now().isoformat(),
        "actions": actions,
    }
    _write_json(_actions_path(target), payload)


def _append_action_archive(target: Path, actions: list[dict[str, Any]]) -> None:
    if not actions:
        return
    path = _actions_archive_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for action in actions:
            handle.write(json.dumps(action, sort_keys=True) + "\n")


def _activity(target: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for session in work_cmd._collect_sessions(target / ".brigade" / "work")[0][:20]:
        path, payload = session
        items.append(
            _item(
                "work-session",
                str(payload.get("id") or path.name),
                str(payload.get("status") or "unknown"),
                str(payload.get("title") or "work session"),
                f"brigade work show {path.name}",
                created_at=payload.get("started_at") if isinstance(payload.get("started_at"), str) else None,
                updated_at=payload.get("ended_at") or payload.get("started_at"),
                receipt_path=str(path / "session.json"),
                path=str(path),
            )
        )
    for receipt in work_cmd._verify_receipts(target)[:20]:
        run_id = str(receipt.get("run_id") or "")
        items.append(
            _item(
                "verification-run",
                run_id,
                str(receipt.get("status") or "unknown"),
                "work verification",
                f"brigade work verify show {run_id}",
                created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None,
                updated_at=receipt.get("completed_at") or receipt.get("started_at"),
                receipt_path=str(Path(str(receipt.get("path") or "")) / "receipt.json") if receipt.get("path") else None,
                path=receipt.get("path") if isinstance(receipt.get("path"), str) else None,
            )
        )
    for receipt in work_cmd._scanner_receipts(target)[:20]:
        run_id = str(receipt.get("run_id") or "")
        items.append(
            _item(
                "scanner-run",
                run_id,
                str(receipt.get("status") or "unknown"),
                str(receipt.get("scanner_id") or "scanner run"),
                f"brigade work scanners run-show {run_id}",
                created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None,
                updated_at=receipt.get("completed_at") or receipt.get("started_at"),
                receipt_path=str(Path(str(receipt.get("path") or "")) / "receipt.json") if receipt.get("path") else None,
                path=receipt.get("path") if isinstance(receipt.get("path"), str) else None,
            )
        )
    for sweep in work_cmd._scanner_sweeps(target)[:20]:
        sweep_id = str(sweep.get("sweep_id") or "")
        path = str(Path(str(sweep.get("path") or "")) / "sweep.json") if sweep.get("path") else None
        items.append(_item("scanner-sweep", sweep_id, str(sweep.get("status") or "unknown"), "scanner sweep", f"brigade work sweep-show {sweep_id}", created_at=sweep.get("started_at") if isinstance(sweep.get("started_at"), str) else None, updated_at=sweep.get("completed_at") or sweep.get("started_at"), receipt_path=path, path=sweep.get("path") if isinstance(sweep.get("path"), str) else None))
    for receipt in work_cmd._review_receipts(target)[:20]:
        run_id = str(receipt.get("run_id") or "")
        items.append(_item("code-review", run_id, str(receipt.get("status") or "unknown"), str(receipt.get("reviewer_id") or "review run"), f"brigade work review show {run_id}", created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None, updated_at=receipt.get("completed_at") or receipt.get("started_at"), receipt_path=str(Path(str(receipt.get("path") or "")) / "receipt.json") if receipt.get("path") else None, path=receipt.get("path") if isinstance(receipt.get("path"), str) else None))
    for draft in handoff_cmd.draft_queue_payload(target).get("drafts", [])[:20]:
        if not isinstance(draft, dict):
            continue
        draft_id = str(draft.get("id") or Path(str(draft.get("path") or "handoff")).stem)
        items.append(_item("handoff-draft", draft_id, str(draft.get("status") or "pending"), str(draft.get("title") or draft.get("target_document") or "handoff draft"), f"brigade handoff show {draft_id}", severity=draft.get("severity") if isinstance(draft.get("severity"), str) else None, updated_at=draft.get("modified_at") if isinstance(draft.get("modified_at"), str) else None, path=draft.get("path") if isinstance(draft.get("path"), str) else None))
    for receipt in _iter_json_files(target / ".brigade" / "handoffs" / "ingest-runs", "*.json")[:20]:
        run_id = str(receipt.get("run_id") or Path(str(receipt.get("path") or "run")).stem)
        items.append(_item("handoff-ingest", run_id, str(receipt.get("status") or "completed"), "handoff ingest receipt", f"brigade handoff run-show {run_id}", created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None, updated_at=receipt.get("completed_at") or receipt.get("started_at"), receipt_path=receipt.get("path") if isinstance(receipt.get("path"), str) else None))
    for diff in _report_diffs(target)[:20]:
        diff_id = str(diff.get("diff_id") or Path(str(diff.get("path") or "diff")).parent.name)
        summary = diff.get("summary") if isinstance(diff.get("summary"), dict) else {}
        items.append(
            _item(
                "center-report-diff",
                diff_id,
                str(diff.get("status") or "unknown"),
                f"{summary.get('new_item_count', 0)} new, {summary.get('resolved_item_count', 0)} resolved",
                f"brigade center report diff {diff.get('base_report_id')} {diff.get('compare_report_id')}",
                created_at=diff.get("created_at") if isinstance(diff.get("created_at"), str) else None,
                updated_at=diff.get("created_at") if isinstance(diff.get("created_at"), str) else None,
                receipt_path=diff.get("path") if isinstance(diff.get("path"), str) else None,
            )
        )
    for call in _read_jsonl(tools_cmd.calls_path(target))[:20]:
        call_id = str(call.get("call_id") or call.get("id") or "")
        items.append(_item("tool-call", call_id, str(call.get("status") or "unknown"), str(call.get("tool_id") or "tool call"), f"brigade tools call show {call_id}", severity=call.get("severity") if isinstance(call.get("severity"), str) else None, created_at=call.get("created_at") if isinstance(call.get("created_at"), str) else None, updated_at=call.get("reviewed_at") or call.get("created_at"), receipt_path=str(tools_cmd.calls_path(target))))
    for receipt in _iter_json_files(tools_cmd.runs_path(target), "*/receipt.json")[:20]:
        run_id = str(receipt.get("run_id") or Path(str(receipt.get("path") or "run")).parent.name)
        items.append(_item("tool-run", run_id, str(receipt.get("status") or "unknown"), str(receipt.get("tool_id") or "tool run"), f"brigade tools run show {run_id}", created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None, updated_at=receipt.get("completed_at") or receipt.get("started_at"), receipt_path=receipt.get("path") if isinstance(receipt.get("path"), str) else None))
    for checkpoint in _iter_json_files(tools_cmd.checkpoints_path(target), "*.json")[:20]:
        checkpoint_id = str(checkpoint.get("checkpoint_id") or Path(str(checkpoint.get("path") or "checkpoint")).stem)
        items.append(_item("checkpoint", checkpoint_id, str(checkpoint.get("status") or "waiting"), str(checkpoint.get("reason") or "tool checkpoint"), f"brigade tools checkpoint show {checkpoint_id}", severity=checkpoint.get("severity") if isinstance(checkpoint.get("severity"), str) else None, created_at=checkpoint.get("created_at") if isinstance(checkpoint.get("created_at"), str) else None, updated_at=checkpoint.get("reviewed_at") or checkpoint.get("created_at"), receipt_path=checkpoint.get("path") if isinstance(checkpoint.get("path"), str) else None))
    for pack in tools_cmd._tool_packs(target)[:20]:
        pack_id = str(pack.get("pack_id") or "")
        items.append(_item("tool-pack", pack_id, str(pack.get("status") or "built"), "portable tool pack", f"brigade tools pack show {pack_id}", created_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None, updated_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None, receipt_path=str(Path(str(pack.get("path") or "")) / "tool-pack.json") if pack.get("path") else None, path=pack.get("path") if isinstance(pack.get("path"), str) else None))
    for pack in context_cmd._packs(target)[:20]:
        pack_id = str(pack.get("pack_id") or "")
        items.append(_item("context-pack", pack_id, str(pack.get("status") or "built"), str(pack.get("kind") or "context"), f"brigade context show {pack_id}", created_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None, updated_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None, receipt_path=str(Path(str(pack.get("path") or "")) / "context.json") if pack.get("path") else None, path=pack.get("path") if isinstance(pack.get("path"), str) else None))
    for receipt in _iter_json_files(target / ".brigade" / "context" / "sync-plans", "*/sync-plan.json")[:20]:
        sync_id = str(receipt.get("sync_id") or Path(str(receipt.get("path") or "sync")).parent.name)
        items.append(_item("context-sync", sync_id, str(receipt.get("status") or "planned"), f"{receipt.get('destination_count', 0)} destination(s)", f"brigade context sync plan {receipt.get('pack_id') or 'latest'}", created_at=receipt.get("created_at") if isinstance(receipt.get("created_at"), str) else None, updated_at=receipt.get("created_at") if isinstance(receipt.get("created_at"), str) else None, receipt_path=receipt.get("path") if isinstance(receipt.get("path"), str) else None))
    for replay in _iter_json_files(target / ".brigade" / "learn" / "replays", "*/replay.json")[:20]:
        replay_id = str(replay.get("replay_id") or Path(str(replay.get("path") or "replay")).parent.name)
        items.append(_item("learning-replay", replay_id, str(replay.get("status") or "recorded"), str(replay.get("scenario_id") or "learning replay"), "brigade learn plan", updated_at=replay.get("created_at") if isinstance(replay.get("created_at"), str) else None, receipt_path=replay.get("path") if isinstance(replay.get("path"), str) else None))
    security_latest = target / ".brigade" / "security" / "latest" / "security-report.json"
    security_report = _read_json(security_latest)
    if security_report is not None:
        generated = security_report.get("generated_at") if isinstance(security_report.get("generated_at"), str) else None
        items.append(_item("security-report", "latest", "ready", "security report", "brigade security findings", created_at=generated, updated_at=generated, receipt_path=str(security_latest), path=str(security_latest.parent)))
    for closeout in _iter_json_files(target / ".brigade" / "security" / "closeouts", "*/closeout.json")[:20]:
        closeout_id = str(closeout.get("closeout_id") or Path(str(closeout.get("path") or "closeout")).parent.name)
        items.append(_item("security-closeout", closeout_id, str(closeout.get("status") or "reviewed"), "security closeout", "brigade security closeout", created_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None, updated_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None, receipt_path=closeout.get("path") if isinstance(closeout.get("path"), str) else None))
    for closeout in _iter_json_files(target / ".brigade" / "backups" / "closeouts", "*/closeout.json")[:20]:
        closeout_id = str(closeout.get("closeout_id") or Path(str(closeout.get("path") or "closeout")).parent.name)
        items.append(_item("backup-closeout", closeout_id, str(closeout.get("status") or "reviewed"), "backup closeout", "brigade work backup closeout", created_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None, updated_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None, receipt_path=closeout.get("path") if isinstance(closeout.get("path"), str) else None))
    for closeout in _iter_json_files(target / ".brigade" / "memory-care" / "closeouts", "*/closeout.json")[:20]:
        closeout_id = str(closeout.get("closeout_id") or Path(str(closeout.get("path") or "closeout")).parent.name)
        items.append(_item("memory-care-closeout", closeout_id, str(closeout.get("status") or "reviewed"), "memory-care closeout", "brigade memory care closeout", created_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None, updated_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None, receipt_path=closeout.get("path") if isinstance(closeout.get("path"), str) else None))
    release = release_cmd._latest_release_receipt(target)
    if release:
        run_id = str(release.get("run_id") or "latest")
        items.append(_item("release-readiness", run_id, str(release.get("status") or "unknown"), "release readiness", f"brigade release show {run_id}", created_at=release.get("started_at") if isinstance(release.get("started_at"), str) else None, updated_at=release.get("completed_at") or release.get("created_at") or release.get("started_at"), receipt_path=str(Path(str(release.get("path") or "")) / "receipt.json") if release.get("path") else None, path=release.get("path") if isinstance(release.get("path"), str) else None))
    candidate = release_cmd._latest_candidate(target)
    if candidate:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        items.append(_item("release-candidate", candidate_id, str(candidate.get("status") or "draft"), "release candidate", f"brigade release candidate show {candidate_id}", created_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None, updated_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None, receipt_path=str(Path(str(candidate.get("path") or "")) / "EVIDENCE.json") if candidate.get("path") else None, path=candidate.get("path") if isinstance(candidate.get("path"), str) else None))
    for receipt in release_cmd._read_install_smoke_receipts(target)[:20]:
        receipt_id = str(receipt.get("receipt_id") or "")
        items.append(_item("install-smoke", receipt_id, str(receipt.get("status") or "unknown"), str(receipt.get("matrix_id") or "install smoke"), f"brigade release smoke show {receipt_id}", created_at=receipt.get("created_at") if isinstance(receipt.get("created_at"), str) else None, updated_at=receipt.get("completed_at") or receipt.get("created_at"), receipt_path=str(release_cmd._install_smoke_receipts_path(target))))
    for action in _read_actions(target)[:20]:
        action_id = str(action.get("action_id") or "")
        items.append(
            _item(
                "center-action",
                action_id,
                str(action.get("status") or "pending"),
                str(action.get("safe_summary") or "operator action"),
                f"brigade center actions show {action_id}",
                priority=action.get("priority") if isinstance(action.get("priority"), str) else None,
                severity=action.get("severity") if isinstance(action.get("severity"), str) else None,
                created_at=action.get("created_at") if isinstance(action.get("created_at"), str) else None,
                updated_at=action.get("updated_at") if isinstance(action.get("updated_at"), str) else None,
                receipt_path=str(_actions_path(target)),
            )
        )
    readiness = _latest_readiness(target)
    if readiness:
        readiness_id = str(readiness.get("readiness_id") or "latest")
        items.append(_item("center-readiness", readiness_id, str(readiness.get("status") or "unknown"), "operator readiness closeout", f"brigade center readiness show {readiness_id}", created_at=readiness.get("created_at") if isinstance(readiness.get("created_at"), str) else None, updated_at=readiness.get("completed_at") or readiness.get("created_at"), receipt_path=str(Path(str(readiness.get("path") or "")) / "readiness.json") if readiness.get("path") else None, path=readiness.get("path") if isinstance(readiness.get("path"), str) else None))
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return items


def _reviews(target: Path) -> list[dict[str, Any]]:
    from . import daily_cmd

    items: list[dict[str, Any]] = []
    for item in work_cmd._pending_imports(target):
        import_id = str(item.get("id") or "")
        items.append(
            _item(
                "work-import",
                import_id,
                "pending",
                str(item.get("text") or ""),
                f"brigade work import plan {import_id}",
                priority=item.get("priority") if isinstance(item.get("priority"), str) else None,
                severity=item.get("severity") if isinstance(item.get("severity"), str) else None,
                receipt_path=str(work_cmd._imports_path(target)),
                updated_at=item.get("updated_at") or item.get("created_at"),
            )
        )
    review_health = work_cmd._review_health(target)
    for finding_key in ("top_pending_finding", "top_unresolved_finding"):
        finding = review_health.get(finding_key) if isinstance(review_health.get(finding_key), dict) else None
        if finding:
            finding_id = str(finding.get("id") or finding.get("import_id") or finding_key)
            items.append(_item("code-review", finding_id, "pending", str(finding.get("text") or finding.get("safe_detail") or "review finding"), f"brigade work review finding-show {finding_id}", severity=finding.get("severity") if isinstance(finding.get("severity"), str) else None, updated_at=finding.get("created_at") if isinstance(finding.get("created_at"), str) else None))
    handoffs = handoff_cmd.draft_queue_payload(target)
    for draft in handoffs.get("drafts", [])[:20]:
        if isinstance(draft, dict) and draft.get("status") in {None, "pending", "failed", "invalid"}:
            draft_id = str(draft.get("id") or Path(str(draft.get("path") or "handoff")).stem)
            items.append(_item("handoff-draft", draft_id, str(draft.get("status") or "pending"), str(draft.get("title") or draft.get("target_document") or "handoff draft"), f"brigade handoff show {draft_id}", severity=draft.get("severity") if isinstance(draft.get("severity"), str) else None, updated_at=draft.get("modified_at") if isinstance(draft.get("modified_at"), str) else None, path=draft.get("path") if isinstance(draft.get("path"), str) else None))
    tool_health = tools_cmd.health(target)
    for bucket, command in (
        ("call_queue", "brigade tools call list"),
        ("run_history", "brigade tools run list"),
        ("checkpoints", "brigade tools checkpoint list"),
    ):
        value = tool_health.get(bucket) if isinstance(tool_health.get(bucket), dict) else {}
        top = value.get("top_issue") if isinstance(value.get("top_issue"), dict) else None
        if top:
            items.append(_item("tools", str(top.get("call_id") or top.get("run_id") or top.get("checkpoint_id") or bucket), str(top.get("status") or "warn"), str(top.get("detail") or top.get("issue_type") or bucket), command, severity=top.get("severity") if isinstance(top.get("severity"), str) else None))
    for name, health, command in (
        ("backup", work_cmd._backup_health(target), "brigade work backup status"),
        ("memory-care", memory_cmd.health(target), "brigade memory care status"),
        ("security", security_cmd.health(target), "brigade security findings"),
    ):
        top = health.get("top_issue") or health.get("top_finding")
        if isinstance(top, dict):
            items.append(_item(name, str(top.get("id") or top.get("name") or top.get("issue_type") or name), str(top.get("status") or "warn"), str(top.get("detail") or top.get("title") or top.get("safe_summary") or name), command, severity=top.get("severity") if isinstance(top.get("severity"), str) else None))
    for candidate in learn_cmd.candidates(target):
        items.append(
            _item(
                "learning",
                str(candidate.get("id") or ""),
                str(candidate.get("status") or "pending"),
                str(candidate.get("safe_summary") or ""),
                str(candidate.get("suggested_next_command") or "brigade learn plan"),
                severity=candidate.get("severity") if isinstance(candidate.get("severity"), str) else None,
            )
        )
    learning_health = learn_cmd.health(target)
    replay = learning_health.get("replay") if isinstance(learning_health.get("replay"), dict) else {}
    replay_issue = replay.get("top_issue") if isinstance(replay.get("top_issue"), dict) else None
    if replay_issue:
        items.append(_item("learning", str(replay_issue.get("compare_id") or "learning-replay"), str(replay_issue.get("status") or "warn"), str(replay_issue.get("detail") or "learning replay needs review"), "brigade learn replay compare latest"))
    project_health = projects_cmd.health(target)
    for issue in project_health.get("checks", []):
        if issue.get("status") != "ok":
            items.append(_item("project-consolidation", str(issue.get("project_id") or issue.get("name")), str(issue.get("status")), str(issue.get("detail")), "brigade projects audit"))
    repo_health = repos_cmd.health(target)
    repo_report = repo_health.get("report") if isinstance(repo_health.get("report"), dict) else {}
    repo_actions = repo_health.get("actions") if isinstance(repo_health.get("actions"), dict) else {}
    repo_sweep = repo_health.get("sweep") if isinstance(repo_health.get("sweep"), dict) else {}
    repo_release = repo_health.get("release_train") if isinstance(repo_health.get("release_train"), dict) else {}
    for bucket, command in ((repo_report, "brigade repos report build"), (repo_actions, "brigade repos actions list"), (repo_sweep, "brigade repos sweep run"), (repo_release, "brigade repos release build")):
        top = bucket.get("top_issue") if isinstance(bucket.get("top_issue"), dict) else None
        if top:
            items.append(_item("repo-fleet", str(top.get("name") or "repo-fleet"), str(top.get("status") or "warn"), str(top.get("detail") or "repo fleet issue"), str(top.get("suggested_next_command") or command)))
    context_health = context_cmd.health(target)
    for issue in context_health.get("issues", []):
        items.append(_item("context", str(issue.get("name")), str(issue.get("status")), str(issue.get("detail")), "brigade context plan"))
    candidate = release_cmd._latest_candidate(target)
    if isinstance(candidate, dict) and candidate.get("status") in {"draft", "blocked"}:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        items.append(_item("release-candidate", candidate_id, str(candidate.get("status") or "draft"), "release candidate awaits review", f"brigade release candidate compare {candidate_id}", updated_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None, path=candidate.get("path") if isinstance(candidate.get("path"), str) else None))
    for action in _read_actions(target):
        if action.get("status") not in {"pending", "active", "deferred"}:
            continue
        action_id = str(action.get("action_id") or "")
        items.append(
            _item(
                "center-action",
                action_id,
                str(action.get("status") or "pending"),
                str(action.get("safe_summary") or "operator action"),
                f"brigade center actions show {action_id}",
                priority=action.get("priority") if isinstance(action.get("priority"), str) else None,
                severity=action.get("severity") if isinstance(action.get("severity"), str) else None,
                updated_at=action.get("updated_at") if isinstance(action.get("updated_at"), str) else None,
                receipt_path=str(_actions_path(target)),
            )
        )
    readiness = readiness_health(target)
    top_readiness = readiness.get("top_issue") if isinstance(readiness.get("top_issue"), dict) else None
    if top_readiness:
        items.append(_item("center-readiness", str(top_readiness.get("name") or "readiness"), str(top_readiness.get("status") or "warn"), str(top_readiness.get("detail") or "operator readiness needs review"), "brigade center readiness plan"))
    daily_health = daily_cmd.health(target)
    approvals = daily_health.get("approvals") if isinstance(daily_health.get("approvals"), dict) else {}
    top_approval = approvals.get("top_pending") if isinstance(approvals.get("top_pending"), dict) else None
    if top_approval:
        approval_id = str(top_approval.get("approval_id") or "approval")
        items.append(_item("daily-approval", approval_id, str(top_approval.get("status") or "pending"), str(top_approval.get("safe_summary") or "daily approval pending"), f"brigade daily approvals show {approval_id}"))
    top_daily = daily_health.get("top_issue") if isinstance(daily_health.get("top_issue"), dict) else None
    if top_daily:
        items.append(_item("daily-driver", str(top_daily.get("name") or "daily"), str(top_daily.get("status") or "warn"), str(top_daily.get("detail") or "daily driver needs review"), "brigade daily doctor"))
    phase_health = phases_cmd.health(target)
    top_phase = phase_health.get("top_issue") if isinstance(phase_health.get("top_issue"), dict) else None
    if top_phase:
        items.append(_item("phase-ledger", str(top_phase.get("phase_id") or top_phase.get("name") or "phase-ledger"), str(top_phase.get("status") or "warn"), str(top_phase.get("detail") or "phase execution ledger needs review"), "brigade work phases doctor"))
    latest_phase_session = phase_health.get("latest_session") if isinstance(phase_health.get("latest_session"), dict) else None
    if latest_phase_session and latest_phase_session.get("status") not in {"closed", "archived"}:
        items.append(_item("phase-session", str(latest_phase_session.get("session_id") or "phase-session"), "warn", "active phase execution session needs review", "brigade work phases session next latest"))
    return items


def status_payload(target: Path) -> dict[str, Any]:
    from . import daily_cmd

    target = target.expanduser().resolve()
    active = work_cmd._active_session_info(target)
    pending_tasks = work_cmd._pending_tasks(target)
    pending_imports = work_cmd._pending_imports(target)
    return {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-status"),
        "target": str(target),
        "active_session": active,
        "pending_task_count": len(pending_tasks),
        "pending_import_count": len(pending_imports),
        "scanner_sweeps": work_cmd._scanner_sweep_health(target),
        "code_review": work_cmd._review_health(target),
        "inbox_hygiene": work_cmd._inbox_hygiene_payload(target),
        "chat_surfaces": chat_cmd.health(target),
        "handoff_drafts": handoff_cmd.draft_queue_payload(target),
        "memory_care": memory_cmd.health(target),
        "backup": work_cmd._backup_health(target),
        "tool_catalog": tools_cmd.health(target),
        "learning": learn_cmd.health(target),
        "context": context_cmd.health(target),
        "release_readiness": release_cmd._latest_release_receipt(target),
        "release_candidate": release_cmd._latest_candidate(target),
        "repo_fleet": repos_cmd.health(target),
        "roadmap": roadmap_cmd.health(target),
        "projects": projects_cmd.health(target),
        "security": security_cmd.health(target),
        "operator_readiness": readiness_health(target),
        "operator_report": report_health(target),
        "action_queue": actions_health(target),
        "daily_driver": daily_cmd.health(target),
        "phase_ledger": phases_cmd.health(target),
        "review_queue_count": len(_reviews(target)),
    }


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center status: {payload['target']}")
    print(f"pending_tasks: {payload['pending_task_count']}")
    print(f"pending_imports: {payload['pending_import_count']}")
    print(f"reviews: {payload['review_queue_count']}")
    print(f"actions: {payload['action_queue']['open_count']}")
    print(f"context_packs: {payload['context']['pack_count']}")
    phase_ledger = payload.get("phase_ledger") if isinstance(payload.get("phase_ledger"), dict) else {}
    if phase_ledger:
        print(f"phase_records: {phase_ledger.get('record_count', 0)}")
        print(f"phase_issues: {phase_ledger.get('issue_count', 0)}")
        print(f"phase_actions: {phase_ledger.get('open_action_count', 0)}")
        latest_phase_session = phase_ledger.get("latest_session") if isinstance(phase_ledger.get("latest_session"), dict) else None
        if latest_phase_session:
            print(f"phase_session: {latest_phase_session.get('session_id')} [{latest_phase_session.get('status')}]")
    return 0


def schema(*, target: Path, json_output: bool = False) -> int:
    payload = _center_schema_manifest(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center schema manifest: {payload['target']}")
    print(f"schemas: {payload['schema_count']}")
    print("read_only: true")
    for schema_item in payload["schemas"]:
        print(f"- {schema_item['id']}: {schema_item['command']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def activity(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    target = target.expanduser().resolve()
    items = _activity(target)[:limit]
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("center-activity"), "target": str(target), "activity": items, "activity_count": len(items)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center activity: {target}")
    for item in items:
        print(f"- {item['subsystem']} {item['id']} [{item['status']}] {item['safe_summary']}")
    return 0


def reviews(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    target = target.expanduser().resolve()
    items = _reviews(target)[:limit]
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("center-reviews"), "target": str(target), "reviews": items, "review_count": len(items)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center reviews: {target}")
    for item in items:
        print(f"- {item['subsystem']} {item['id']} [{item['status']}] {item['safe_summary']}")
        print(f"  next: {item['suggested_next_command']}")
    return 0


def templates(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    items = [
        _item("context", "task", "available", "Task context pack template", "brigade context plan --kind task"),
        _item("context", "repo", "available", "Repo context pack template", "brigade context plan --kind repo"),
        _item("context", "release", "available", "Release context pack template", "brigade context plan --kind release"),
        _item("tools", "tool-pack", "available", "Portable tool pack template", "brigade tools pack build"),
        _item("projects", "audit-plan", "available", "Project audit plan template", "brigade projects audit"),
        _item("release", "candidate", "available", "Release candidate checklist template", "brigade release candidate plan"),
        _item("review", "closeout", "available", "Review closeout template", "brigade work review closeout latest"),
    ]
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("center-templates"), "target": str(target), "templates": items, "template_count": len(items)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center templates: {target}")
    for item in items:
        print(f"- {item['subsystem']}:{item['id']} {item['safe_summary']}")
    return 0


def _readiness_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "readiness"


def _readiness_archive_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "readiness-archive"


def _readiness_waivers_path(target: Path) -> Path:
    return _readiness_root(target) / "waivers.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


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


def _readiness_finding(subsystem: str, name: str, severity: str, summary: str, command: str, *, status: str = "warn") -> dict[str, Any]:
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
        findings.append(_readiness_finding("work", "pending_tasks", "warning", f"{status_data['pending_task_count']} pending task(s)", "brigade work tasks"))
    if status_data.get("pending_import_count", 0) > 0:
        findings.append(_readiness_finding("work", "pending_imports", "blocker", f"{status_data['pending_import_count']} pending import(s)", "brigade work inbox", status="blocked"))
    if status_data.get("review_queue_count", 0) > 0:
        findings.append(_readiness_finding("center", "pending_reviews", "warning", f"{status_data['review_queue_count']} pending review item(s)", "brigade center reviews"))
    release = status_data.get("release_readiness") if isinstance(status_data.get("release_readiness"), dict) else None
    if not release:
        findings.append(_readiness_finding("release", "missing_release_readiness", "blocker", "release readiness receipt is missing", "brigade release run", status="blocked"))
    elif release.get("ready") is False or release.get("status") in {"blocked", "failed"}:
        run_id = str(release.get("run_id") or "latest")
        findings.append(_readiness_finding("release", "blocked_release_readiness", "blocker", "release readiness is blocked", f"brigade release show {run_id}", status="blocked"))
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
        detail = _readiness_safe_text(target, str((top or {}).get("detail") or (top or {}).get("safe_summary") or f"{subsystem} has unresolved health issue(s)"))
        findings.append(_readiness_finding(subsystem, str((top or {}).get("name") or "health"), "warning", detail, command))
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
    status_data["review_queue_count"] = len([item for item in _reviews(target) if item.get("subsystem") != "center-readiness"])
    findings = _readiness_findings(target, status_data)
    waivers = _active_readiness_waivers(target)
    for finding in findings:
        waiver = waivers.get(str(finding.get("finding_id")))
        finding["waived"] = bool(waiver)
        if waiver:
            finding["waiver_id"] = waiver.get("waiver_id")
            finding["waiver_reason"] = waiver.get("reason")
    unwaived_blockers = [finding for finding in findings if finding.get("severity") == "blocker" and not finding.get("waived")]
    warnings = [finding for finding in findings if finding.get("severity") != "blocker" and not finding.get("waived")]
    manual_checklist = [
        {"label": "roadmap audit", "command": "brigade roadmap audit --json", "manual_only": True},
        {"label": "command inventory check", "command": "brigade roadmap commands --check", "manual_only": True},
        {"label": "operator center status", "command": "brigade center status --json", "manual_only": True},
        {"label": "release doctor", "command": "brigade release doctor", "manual_only": True},
        {"label": "release candidate compare", "command": "brigade release candidate compare latest", "manual_only": True},
        {"label": "diff check", "command": "git diff --check", "manual_only": True},
        {"label": "full tests", "command": "PYTHONPATH=src python3 -m pytest -q", "manual_only": True},
        {"label": "manual tag placeholder", "command": "manual tag command after explicit operator approval", "manual_only": True, "remote_mutation": True},
        {"label": "manual push placeholder", "command": "manual push command after explicit operator approval", "manual_only": True, "remote_mutation": True},
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
        issues.append({"status": "warn", "name": "center_readiness_missing", "detail": "no local operator readiness closeout found"})
    elif latest.get("ready") is False and latest.get("review_status") != "deferred":
        issues.append({"status": "warn", "name": "center_readiness_blocked", "detail": str(latest.get("readiness_id") or "latest")})
    return {
        "latest": latest,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "checks": issues,
    }


def readiness_plan(*, target: Path, json_output: bool = False) -> int:
    payload = _readiness_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ready"] else 1
    print(f"center readiness plan: {payload['target']}")
    print(f"status: {payload['status']}")
    print(f"blockers: {payload['blocker_count']}")
    print(f"warnings: {payload['warning_count']}")
    for finding in payload["findings"][:20]:
        waived = " waived" if finding.get("waived") else ""
        print(f"- {finding['finding_id']} {finding['severity']}{waived}: {finding['safe_summary']}")
        print(f"  next: {finding['suggested_next_command']}")
    return 0 if payload["ready"] else 1


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
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("center-readiness-list"), "target": str(target), "readiness": receipts, "readiness_count": len(receipts)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center readiness list: {target}")
    for receipt in receipts:
        print(f"- {receipt.get('readiness_id')} [{receipt.get('status')}] {receipt.get('review_status')}")
    return 0


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


def _reports_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "reports"


def _reports_archive_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "reports-archive"


def _report_diffs_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "report-diffs"


def _report_json_path(path: Path) -> Path:
    return path / "CENTER_EVIDENCE.json" if path.is_dir() else path


def _read_report(path: Path) -> dict[str, Any] | None:
    payload = _read_json(_report_json_path(path))
    if payload is not None:
        payload.setdefault("path", str(_report_json_path(path).parent))
    return payload


def _reports(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_reports_root(target)]
    if include_archived:
        roots.append(_reports_archive_root(target))
    reports: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name.endswith("archive") or not child.is_dir():
                continue
            payload = _read_report(child)
            if payload is not None:
                reports.append(payload)
    reports.sort(key=lambda item: str(item.get("created_at") or item.get("report_id") or ""), reverse=True)
    return reports


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
    if report_id == "latest":
        latest = latest_report(target)
        return (latest, None) if latest else (None, "operator report not found: latest")
    matches = [item for item in reports if str(item.get("report_id") or "").startswith(report_id)]
    if not matches:
        return None, f"operator report not found: {report_id}"
    if len(matches) > 1:
        return None, f"operator report id is ambiguous: {report_id}"
    return matches[0], None


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
            "memory_care": status_data.get("memory_care") if "memory_care" in status_data else memory_cmd.health(target),
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
    report_health_data = status_data.get("operator_report") if isinstance(status_data.get("operator_report"), dict) else {}
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
    commands = payload.get("suggested_next_commands") if isinstance(payload.get("suggested_next_commands"), dict) else {}
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
        lines.append(f"- `{item.get('subsystem')}` `{item.get('id')}` [{item.get('status')}] {item.get('safe_summary')}")
        if item.get("suggested_next_command"):
            lines.append(f"  - next: `{item.get('suggested_next_command')}`")
    if not reviews_data:
        lines.append("- none")
    lines.extend(["", "## Activity", ""])
    activity_data = payload.get("activity") if isinstance(payload.get("activity"), list) else []
    for item in activity_data[:25]:
        lines.append(f"- `{item.get('subsystem')}` `{item.get('id')}` [{item.get('status')}] {item.get('safe_summary')}")
    if not activity_data:
        lines.append("- none")
    lines.extend(["", "## Boundaries", "", "- local report only", "- no daemon", "- no web server", "- no remote mutation", "- no automatic promotion"])
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
    sweep_review = summaries.get("scanner_sweep") if isinstance(summaries.get("scanner_sweep"), dict) else status_data.get("scanner_sweeps")
    if isinstance(sweep_review, dict):
        top = (sweep_review.get("review") if isinstance(sweep_review.get("review"), dict) else {}).get("top_pending_import")
        if isinstance(top, dict):
            groups["scanner_sweep_issues"].append(
                _item("scanner-sweep", str(top.get("id") or "pending-import"), "pending", str(top.get("text") or "pending sweep import"), f"brigade work import plan {top.get('id')}")
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
                _item(name.replace("_", "-"), str(top.get("id") or top.get("name") or top.get("issue_type") or name), str(top.get("status") or "warn"), str(top.get("detail") or top.get("title") or name), command, severity=top.get("severity") if isinstance(top.get("severity"), str) else None)
            )
    readiness = release_data.get("readiness") if isinstance(release_data.get("readiness"), dict) else None
    candidate = release_data.get("candidate") if isinstance(release_data.get("candidate"), dict) else None
    if isinstance(readiness, dict) and readiness.get("ready") is False:
        run_id = str(readiness.get("run_id") or "latest")
        groups["release_readiness_candidate_issues"].append(_item("release-readiness", run_id, str(readiness.get("status") or "blocked"), "release readiness is blocked", f"brigade release show {run_id}"))
    if isinstance(candidate, dict) and candidate.get("status") not in {None, "reviewed", "archived"}:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        groups["release_readiness_candidate_issues"].append(_item("release-candidate", candidate_id, str(candidate.get("status") or "draft"), "release candidate awaits review", f"brigade release candidate compare {candidate_id}"))
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
        "<html><head><meta charset=\"utf-8\"><title>"
        + title
        + "</title><style>body{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;line-height:1.45}pre{white-space:pre-wrap;background:#f6f8fa;padding:1rem;border:1px solid #d0d7de}</style></head>"
        "<body><pre>"
        + body
        + "</pre></body></html>\n"
    )


def _write_report_bundle(report_dir: Path, payload: dict[str, Any]) -> None:
    markdown = _report_markdown(payload)
    _write_json(report_dir / "CENTER_EVIDENCE.json", payload)
    (report_dir / "OPERATOR_REPORT.md").write_text(markdown)
    (report_dir / "OPERATOR_REPORT.html").write_text(_report_html(markdown, payload))


def report_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = latest_report(target)
    reports = _reports(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append({"status": "warn", "name": "operator_report_missing", "detail": "no local operator report has been built", "suggested_next_command": "brigade center report build"})
        return {"latest": None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    closeout_status = str(closeout.get("status") or "") if closeout else ""
    if closeout_status not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append({"status": "warn", "name": "operator_report_unclosed", "detail": f"{latest.get('report_id')} has not been closed out", "suggested_next_command": f"brigade center report review {latest.get('report_id')}"})
    created = _parse_time(latest.get("created_at") or latest.get("generated_at"))
    if created is not None:
        age_hours = (_now() - created).total_seconds() / 3600
        if age_hours > REPORT_STALE_HOURS:
            checks.append({"status": "warn", "name": "operator_report_stale", "detail": f"{latest.get('report_id')}={age_hours:.1f}h", "suggested_next_command": "brigade center report build"})
    current_head = _git_value(target, "rev-parse", "HEAD")
    git = latest.get("git") if isinstance(latest.get("git"), dict) else {}
    if git.get("head") and current_head and git.get("head") != current_head:
        checks.append({"status": "warn", "name": "operator_report_head_changed", "detail": f"{latest.get('report_id')} head changed", "suggested_next_command": "brigade center report build"})
    for ref in latest.get("receipt_references") if isinstance(latest.get("receipt_references"), list) else []:
        if isinstance(ref, str) and ref and not Path(ref).exists():
            checks.append({"status": "warn", "name": "operator_report_missing_receipt", "detail": ref, "suggested_next_command": f"brigade center report show {latest.get('report_id')}"})
            break
    latest_activity = [item for item in _activity(target) if item.get("subsystem") != "center-report-diff"]
    report_activity = latest.get("activity") if isinstance(latest.get("activity"), list) else []
    latest_time = _parse_time(latest_activity[0].get("updated_at")) if latest_activity else None
    report_time = _parse_time(report_activity[0].get("updated_at")) if report_activity else created
    if latest_time is not None and report_time is not None and latest_time > report_time:
        checks.append({"status": "warn", "name": "operator_report_newer_activity", "detail": f"{latest.get('report_id')} is older than local activity", "suggested_next_command": "brigade center report build"})
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
        elif latest_diff.get("base_report_id") != base_report.get("report_id") or latest_diff.get("compare_report_id") != compare_report.get("report_id"):
            checks.append(
                {
                    "status": "warn",
                    "name": "operator_report_diff_stale",
                    "detail": f"latest diff does not cover {base_report.get('report_id')} -> {compare_report.get('report_id')}",
                    "suggested_next_command": f"brigade center report diff {base_report.get('report_id')} {compare_report.get('report_id')} --record",
                }
            )
        elif int((latest_diff.get("summary") or {}).get("new_blocker_count") or 0) > 0 or int((latest_diff.get("summary") or {}).get("stale_reference_count") or 0) > 0:
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
    payload.update({"report_id": "planned", "report_root": str(_reports_root(target)), "bundle_files": ["OPERATOR_REPORT.md", "OPERATOR_REPORT.html", "CENTER_EVIDENCE.json"]})
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
    payload = {"schema_version": SCHEMA_VERSION, "schema": _schema("center-report-list"), "target": str(target), "reports_root": str(_reports_root(target)), "reports": reports, "report_count": len(reports)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator reports: {target}")
    print(f"reports_root: {payload['reports_root']}")
    for item in reports:
        print(f"- {item.get('report_id')} reviews={len(item.get('reviews') if isinstance(item.get('reviews'), list) else [])} {item.get('created_at')}")
    return 0


def report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "schema": _schema("center-report-show"), "target": str(target), "report": report}, indent=2, sort_keys=True))
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
    destination = _reports_archive_root(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"error: archived operator report already exists: {destination}", file=sys.stderr)
        return 2
    shutil.move(str(source), str(destination))
    payload = {"schema_version": SCHEMA_VERSION, "target": str(target), "report_id": report.get("report_id"), "status": "archived", "archive_path": str(destination)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived operator report: {report.get('report_id')}")
    print(f"path: {destination}")
    return 0


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
            print(f"- {item.get('subsystem')} {item.get('local_id') or item.get('id')} [{item.get('status')}] {item.get('safe_summary')}")
            if item.get("suggested_next_command"):
                print(f"  next: {item.get('suggested_next_command')}")
    return 0


def _receipt_newer_than_report(receipt: dict[str, Any] | None, report_created: datetime | None) -> bool:
    if receipt is None or report_created is None:
        return False
    stamp = work_cmd._parse_iso_datetime(receipt.get("completed_at") or receipt.get("created_at") or receipt.get("started_at") or receipt.get("generated_at"))
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
    for ref in report.get("receipt_references") if isinstance(report.get("receipt_references"), list) else []:
        if isinstance(ref, str) and ref and not Path(ref).exists():
            stale.append({"report_id": report_id, "path": ref})
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
    payload = _report_diff_payload(target=target, base_report=base_report, compare_report=compare_report, diff_id=diff_id, path=diff_dir)
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
        issues.append({"status": "warn", "name": "operator_report_head_changed", "detail": "current HEAD differs from report HEAD"})
    for ref in report.get("receipt_references") if isinstance(report.get("receipt_references"), list) else []:
        if isinstance(ref, str) and ref and not Path(ref).exists():
            issues.append({"status": "warn", "name": "operator_report_missing_receipt", "detail": ref})
            break
    current_activity = _activity(target)
    report_activity = report.get("activity") if isinstance(report.get("activity"), list) else []
    current_activity_time = _parse_time(current_activity[0].get("updated_at")) if current_activity else None
    report_activity_time = _parse_time(report_activity[0].get("updated_at")) if report_activity else report_created
    if current_activity_time is not None and report_activity_time is not None and current_activity_time > report_activity_time:
        issues.append({"status": "warn", "name": "operator_report_newer_activity", "detail": "newer center activity exists"})
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
    security_generated = work_cmd._parse_iso_datetime((latest_security or {}).get("generated_at") if isinstance(latest_security, dict) else None)
    if report_created and security_generated and security_generated > report_created:
        issues.append({"status": "warn", "name": "newer_security_report", "detail": str((latest_security or {}).get("path"))})
    current_reviews = _reviews(target)
    if _report_queue_changed(report, current_reviews):
        issues.append({"status": "warn", "name": "operator_report_review_queue_changed", "detail": "current review queue differs from report"})
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
    if status not in {"reviewed", "deferred", "superseded", "archived"}:
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
        "report_fingerprint": report.get("report_fingerprint") or _fingerprint_payload({"reviews": report.get("reviews"), "activity": report.get("activity")}),
    }
    closeout_path = report_path / "CLOSEOUT.json"
    payload["path"] = str(closeout_path)
    _write_json(closeout_path, payload)
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


def _action_priority_rank(action: dict[str, Any]) -> tuple[int, int]:
    severity = str(action.get("severity") or "")
    priority = str(action.get("priority") or "")
    status = str(action.get("status") or "")
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(severity, 4)
    priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(priority, 4)
    status_rank = {"active": 0, "pending": 1, "deferred": 2, "done": 3, "archived": 4}.get(status, 5)
    return (status_rank, min(severity_rank, priority_rank))


def _planned_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    plan = _action_plan(report)
    report_id = str(report.get("report_id") or "planned")
    report_fingerprint = str(report.get("report_fingerprint") or _fingerprint_payload({"reviews": report.get("reviews"), "activity": report.get("activity")}))
    reviewed_at = _report_reviewed_at(report)
    created = _now().isoformat()
    actions: list[dict[str, Any]] = []
    seen_source_items: set[str] = set()
    for group, items in plan["groups"].items():
        for item in items:
            if not isinstance(item, dict):
                continue
            source_subsystem = str(item.get("subsystem") or "unknown")
            source_local_id = str(item.get("local_id") or item.get("id") or "unknown")
            source_item_id = f"{source_subsystem}:{source_local_id}"
            if source_item_id in seen_source_items:
                continue
            seen_source_items.add(source_item_id)
            source_fingerprint = _fingerprint_payload(
                {
                    "report_fingerprint": report_fingerprint,
                    "source_item_id": source_item_id,
                }
            )
            action_id = f"act-{source_fingerprint[:16]}"
            actions.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "action_id": action_id,
                    "source_report_id": report_id,
                    "source_report_path": report.get("path"),
                    "source_report_fingerprint": report_fingerprint,
                    "source_group": group,
                    "source_subsystem": source_subsystem,
                    "source_local_id": source_local_id,
                    "status": "pending",
                    "priority": item.get("priority") if isinstance(item.get("priority"), str) else None,
                    "severity": item.get("severity") if isinstance(item.get("severity"), str) else None,
                    "safe_summary": str(item.get("safe_summary") or "operator action"),
                    "suggested_command": str(item.get("suggested_next_command") or ""),
                    "created_at": created,
                    "updated_at": created,
                    "reviewed_at": reviewed_at,
                    "source_fingerprint": source_fingerprint,
                }
            )
    actions.sort(key=lambda action: (_action_priority_rank(action), str(action.get("source_group") or ""), str(action.get("source_local_id") or "")))
    return actions


def _find_action(target: Path, action_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    actions = _read_actions(target)
    matches = [item for item in actions if str(item.get("action_id") or "").startswith(action_id)]
    if not matches:
        return actions, None, f"action not found: {action_id}"
    if len(matches) > 1:
        return actions, None, f"action id is ambiguous: {action_id}"
    return actions, matches[0], None


def _action_counts(actions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(ACTION_STATUSES)}
    for action in actions:
        status = str(action.get("status") or "pending")
        if status not in counts:
            counts[status] = 0
        counts[status] += 1
    return counts


def _action_age_hours(action: dict[str, Any], *, now: datetime, fields: tuple[str, ...]) -> float | None:
    for field in fields:
        stamp = _parse_time(action.get(field))
        if stamp is not None:
            return (now - stamp).total_seconds() / 3600
    return None


def _action_policy_issue(action: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    action_id = str(action.get("action_id") or "")
    status = str(action.get("status") or "pending")
    if status == "pending":
        age = _action_age_hours(action, now=now, fields=("created_at", "updated_at"))
        if age is not None and age > ACTION_PENDING_STALE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_stale_pending",
                "action_id": action_id,
                "detail": f"{action_id} has been pending for {age:.1f}h",
                "suggested_next_command": f"brigade center actions start {action_id}",
                "age_hours": round(age, 2),
            }
    elif status == "active":
        age = _action_age_hours(action, now=now, fields=("started_at", "updated_at", "created_at"))
        if age is not None and age > ACTION_ACTIVE_STALE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_stale_active",
                "action_id": action_id,
                "detail": f"{action_id} has been active for {age:.1f}h",
                "suggested_next_command": f"brigade center actions done {action_id}",
                "age_hours": round(age, 2),
            }
    elif status == "deferred":
        age = _action_age_hours(action, now=now, fields=("deferred_at", "updated_at", "created_at"))
        if age is not None and age > ACTION_DEFERRED_STALE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_deferred_too_long",
                "action_id": action_id,
                "detail": f"{action_id} has been deferred for {age:.1f}h",
                "suggested_next_command": f"brigade center actions show {action_id}",
                "age_hours": round(age, 2),
            }
    elif status == "done":
        age = _action_age_hours(action, now=now, fields=("completed_at", "updated_at", "created_at"))
        if age is not None and age > ACTION_DONE_ARCHIVE_HOURS:
            return {
                "status": "warn",
                "name": "center_action_completed_unarchived",
                "action_id": action_id,
                "detail": f"{action_id} has been completed for {age:.1f}h and should be archived",
                "suggested_next_command": "brigade center actions archive --completed",
                "age_hours": round(age, 2),
            }
    return None


def _action_policy_issues(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = _now()
    issues = [_action_policy_issue(action, now=now) for action in actions]
    return [issue for issue in issues if issue is not None]


def actions_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    open_actions = [action for action in actions if action.get("status") in {"pending", "active", "deferred"}]
    open_actions.sort(key=_action_priority_rank)
    checks: list[dict[str, Any]] = []
    policy_issues = _action_policy_issues(actions)
    if open_actions:
        top = open_actions[0]
        checks.append(
            {
                "status": "warn",
                "name": "center_actions_open",
                "detail": f"{len(open_actions)} open operator action(s)",
                "suggested_next_command": f"brigade center actions show {top.get('action_id')}",
            }
        )
    checks.extend(policy_issues)
    return {
        "actions_path": str(_actions_path(target)),
        "action_count": len(actions),
        "open_count": len(open_actions),
        "counts": _action_counts(actions),
        "policy": {
            "pending_stale_hours": ACTION_PENDING_STALE_HOURS,
            "active_stale_hours": ACTION_ACTIVE_STALE_HOURS,
            "deferred_stale_hours": ACTION_DEFERRED_STALE_HOURS,
            "done_archive_hours": ACTION_DONE_ARCHIVE_HOURS,
        },
        "policy_issues": policy_issues,
        "policy_issue_count": len(policy_issues),
        "top_action": open_actions[0] if open_actions else None,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def actions_doctor(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-doctor"),
        "target": str(target),
        "health": actions_health(target),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center actions doctor: {target}")
    health = payload["health"]
    print(f"actions: {health['action_count']}")
    print(f"open: {health['open_count']}")
    print(f"policy_issues: {health['policy_issue_count']}")
    for issue in health["policy_issues"]:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0


def _action_policy_import_record(issue: dict[str, Any], actions_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    action_id = str(issue.get("action_id") or "")
    action = actions_by_id.get(action_id, {})
    source_key = f"center-action-policy:{issue.get('name')}:{action_id}"
    fingerprint = _fingerprint_payload(
        {
            "source_key": source_key,
            "source_fingerprint": action.get("source_fingerprint"),
            "status": action.get("status"),
            "issue": issue.get("name"),
        }
    )
    return {
        "kind": "task",
        "source": "center-action-policy",
        "text": str(issue.get("detail") or f"Review operator action {action_id}"),
        "type": "task",
        "priority": "high" if issue.get("name") == "center_action_stale_active" else "normal",
        "acceptance": [
            f"Operator action `{action_id}` is reviewed.",
            "The action is started, completed, deferred with a fresh reason, or archived as appropriate.",
            "No suggested command is executed automatically by this import.",
        ],
        "metadata": {
            "source_item_key": source_key,
            "source_fingerprint": fingerprint,
            "issue_type": issue.get("name"),
            "action_id": action_id,
            "action_status": action.get("status"),
            "source_report_id": action.get("source_report_id"),
            "source_subsystem": action.get("source_subsystem"),
            "source_local_id": action.get("source_local_id"),
            "safe_summary": action.get("safe_summary"),
            "suggested_command": action.get("suggested_command"),
            "suggested_next_command": issue.get("suggested_next_command"),
        },
    }


def actions_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    actions_by_id = {str(action.get("action_id") or ""): action for action in actions}
    issues = _action_policy_issues(actions)
    records = [_action_policy_import_record(issue, actions_by_id) for issue in issues]
    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-import-issues"),
        "target": str(target),
        "dry_run": dry_run,
        "issue_count": len(issues),
        "created_count": len(imported),
        "skipped_count": len(skipped),
        "dismissed_count": len(skipped_dismissed),
        "imports_path": str(work_cmd._imports_path(target)),
        "issues": issues,
        "created": imported,
        "skipped": skipped,
        "dismissed": skipped_dismissed,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("center actions import-issues")
    print(f"dry_run: {dry_run}")
    print(f"issues: {len(issues)}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(skipped_dismissed)}")
    return 0


def actions_plan(*, target: Path, report_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actions = _planned_actions(report)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-plan"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": report.get("path"),
        "report_review_status": _report_review_status(report),
        "actions_root": str(_actions_root(target)),
        "actions": actions,
        "action_count": len(actions),
        "write_required": False,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center actions plan: {report.get('report_id')}")
    print(f"actions: {len(actions)}")
    print(f"report_status: {payload['report_review_status'] or 'unreviewed'}")
    print("run: brigade center actions build latest")
    for action in actions[:20]:
        print(f"- {action['action_id']} {action['source_group']} {action['source_local_id']} [{action['status']}] {action['safe_summary']}")
        if action.get("suggested_command"):
            print(f"  next: {action['suggested_command']}")
    return 0


def actions_build(*, target: Path, report_id: str = "latest", allow_unreviewed: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    review_status = _report_review_status(report)
    if review_status not in {"reviewed", "deferred"} and not allow_unreviewed:
        print("error: source report must be closed out as reviewed or deferred, or pass --allow-unreviewed", file=sys.stderr)
        return 2
    planned = _planned_actions(report)
    existing = _read_actions(target)
    existing_fingerprints = {str(item.get("source_fingerprint")) for item in existing}
    existing_fingerprints.update(str(item.get("source_fingerprint")) for item in _read_action_archive(target))
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in planned:
        if str(action.get("source_fingerprint")) in existing_fingerprints:
            skipped.append(action)
            continue
        created.append(action)
        existing.append(action)
        existing_fingerprints.add(str(action.get("source_fingerprint")))
    _write_actions(target, existing)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-build"),
        "target": str(target),
        "report_id": report.get("report_id"),
        "report_path": report.get("path"),
        "report_review_status": review_status,
        "actions_path": str(_actions_path(target)),
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created_actions": created,
        "skipped_actions": skipped,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center actions build: {report.get('report_id')}")
    print(f"created: {len(created)}")
    print(f"skipped: {len(skipped)}")
    print(f"path: {_actions_path(target)}")
    return 0


def actions_list(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    actions.sort(key=lambda action: (_action_priority_rank(action), str(action.get("updated_at") or "")))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-list"),
        "target": str(target),
        "actions_path": str(_actions_path(target)),
        "actions": actions[:limit],
        "action_count": len(actions),
        "counts": _action_counts(actions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center actions: {target}")
    print(f"actions_path: {_actions_path(target)}")
    for action in actions[:limit]:
        print(f"- {action.get('action_id')} [{action.get('status')}] {action.get('source_group')} {action.get('source_local_id')}: {action.get('safe_summary')}")
        if action.get("suggested_command"):
            print(f"  next: {action.get('suggested_command')}")
    return 0


def actions_show(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-show"),
        "target": str(target),
        "action": action,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center action: {action.get('action_id')}")
    print(f"status: {action.get('status')}")
    print(f"source: {action.get('source_group')} {action.get('source_subsystem')}:{action.get('source_local_id')}")
    print(f"summary: {action.get('safe_summary')}")
    if action.get("suggested_command"):
        print(f"next: {action.get('suggested_command')}")
    return 0


def _set_action_status(
    *,
    target: Path,
    action_id: str,
    status: str,
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if status not in ACTION_STATUSES:
        print(f"error: invalid action status: {status}", file=sys.stderr)
        return 2
    actions, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    now = _now().isoformat()
    action["status"] = status
    action["updated_at"] = now
    if status == "active":
        action["started_at"] = now
    elif status == "done":
        action["completed_at"] = now
    elif status == "deferred":
        action["deferred_at"] = now
        action["defer_reason"] = reason or "deferred"
    _write_actions(target, actions)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema(f"center-actions-{status}"),
        "target": str(target),
        "action": action,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center action {status}: {action.get('action_id')}")
    print(f"status: {action.get('status')}")
    return 0


def actions_start(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_action_status(target=target, action_id=action_id, status="active", json_output=json_output)


def actions_done(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_action_status(target=target, action_id=action_id, status="done", json_output=json_output)


def actions_defer(*, target: Path, action_id: str, reason: str, json_output: bool = False) -> int:
    if not reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    return _set_action_status(target=target, action_id=action_id, status="deferred", reason=reason, json_output=json_output)


def actions_archive_completed(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    now = _now().isoformat()
    archived: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for action in actions:
        if action.get("status") == "done":
            archived_action = dict(action)
            archived_action["status"] = "archived"
            archived_action["archived_at"] = now
            archived_action["updated_at"] = now
            archived.append(archived_action)
        else:
            remaining.append(action)
    _write_actions(target, remaining)
    _append_action_archive(target, archived)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _action_schema("center-actions-archive"),
        "target": str(target),
        "archived_count": len(archived),
        "archive_path": str(_actions_archive_path(target)),
        "actions_path": str(_actions_path(target)),
        "archived_actions": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("center actions archive: completed")
    print(f"archived: {len(archived)}")
    print(f"path: {_actions_archive_path(target)}")
    return 0
