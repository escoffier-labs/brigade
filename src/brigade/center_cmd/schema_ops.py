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


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _path_label(target: Path, value: str) -> str:
    path = Path(value).expanduser()
    try:
        return str(path.resolve().relative_to(target.resolve()))
    except (OSError, ValueError):
        return path.name or "external-path"


def _receipt_reference_exists(target: Path, value: str) -> bool:
    path = Path(value).expanduser()
    if path.exists():
        return True
    processed = path.parent / "processed" / path.name
    if processed.exists():
        return True
    archive_root = target / ".brigade" / "handoffs" / "archive"
    return any(archive_root.glob(f"*/{path.name}"))


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
        _schema_field(
            "receipt_path", "string|null", required=False, description="Local receipt path label when available."
        ),
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
        _schema_field(
            "reviewed_at", "string|null", required=False, description="Source report review timestamp when available."
        ),
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
            _item(
                "context",
                "release",
                "available",
                "Release context pack template",
                "brigade context plan --kind release",
            ),
            _item("tools", "tool-pack", "available", "Portable tool pack template", "brigade tools pack build"),
            _item("projects", "audit-plan", "available", "Project audit plan template", "brigade projects audit"),
            _item(
                "release",
                "candidate",
                "available",
                "Release candidate checklist template",
                "brigade release candidate plan",
            ),
            _item("review", "closeout", "available", "Review closeout template", "brigade work review closeout latest"),
        ],
    }
    status_data = status_payload(target)
    status_required = {
        "schema_version",
        "schema",
        "target",
        "pending_task_count",
        "pending_import_count",
        "review_queue_count",
        "operator_report",
        "action_queue",
    }
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
                item_issues.append(
                    {
                        "output": output_name,
                        "field": ",".join(missing),
                        "local_id": item.get("local_id") or item.get("id"),
                    }
                )
            command = item.get("suggested_next_command")
            if not isinstance(command, str) or not command.startswith("brigade "):
                item_issues.append(
                    {
                        "output": output_name,
                        "field": "suggested_next_command",
                        "local_id": item.get("local_id") or item.get("id"),
                    }
                )
            for key in ("path", "receipt_path"):
                reason = _unsafe_reference(item.get(key))
                if reason:
                    item_issues.append(
                        {
                            "output": output_name,
                            "field": key,
                            "local_id": item.get("local_id") or item.get("id"),
                            "reason": reason,
                        }
                    )
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
    reference_gaps = [issue for issue in item_issues if issue.get("field") in {"path", "receipt_path"}]
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
