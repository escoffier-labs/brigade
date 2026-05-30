"""Local project consolidation audit."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import work_cmd

OK = "ok"
WARN = "warn"
DECISIONS = {"bake-in", "integrate", "catalog-only", "move-candidate", "leave-alone"}


def config_path(target: Path) -> Path:
    return target / ".brigade" / "projects.toml"


def _read_config(target: Path) -> tuple[list[dict[str, Any]], list[str], bool]:
    path = config_path(target)
    if not path.is_file():
        return [], [f"missing config: {path}"], False
    try:
        payload = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"invalid config: {exc}"], True
    raw = payload.get("project")
    if not isinstance(raw, list):
        return [], ["missing [[project]] entries"], True
    projects: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            errors.append(f"project {index}: entry must be a table")
            continue
        project_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or project_id).strip()
        decision = str(item.get("decision") or "").strip() or _classify(item)
        if not project_id:
            errors.append(f"project {index}: id is required")
            continue
        if decision not in DECISIONS:
            errors.append(f"{project_id}: decision must be one of: {', '.join(sorted(DECISIONS))}")
            decision = "leave-alone"
        projects.append(
            {
                "id": project_id,
                "label": label or project_id,
                "category": str(item.get("category") or "custom"),
                "decision": decision,
                "reason": str(item.get("reason") or _decision_reason(decision)),
                "recommended_owner_label": str(item.get("recommended_owner_label") or "current owner"),
                "enabled": bool(item.get("enabled", True)),
                "readiness": {
                    "docs": bool(item.get("docs_ready", False)),
                    "license": bool(item.get("license_ready", False)),
                    "security": bool(item.get("security_ready", False)),
                    "release": bool(item.get("release_ready", False)),
                },
            }
        )
    return projects, errors, True


def _classify(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "").casefold()
    if "publish" in category or "memory" in category or "search" in category or "usage" in category:
        return "integrate"
    if "mcp" in category or "prompt" in category or "notification" in category:
        return "catalog-only"
    if "side" in category or "public" in category:
        return "move-candidate"
    if "workflow" in category or "bootstrap" in category:
        return "bake-in"
    return "leave-alone"


def _decision_reason(decision: str) -> str:
    return {
        "bake-in": "Small workflow primitive belongs directly in Brigade.",
        "integrate": "External tool should report through receipts or imports.",
        "catalog-only": "Track in catalog without owning execution.",
        "move-candidate": "Needs reviewed migration or consolidation planning.",
        "leave-alone": "Useful context but not Brigade scope.",
    }.get(decision, "No decision reason.")


def _manual_commands(project: dict[str, Any]) -> list[str]:
    if project["decision"] != "move-candidate":
        return []
    return [
        "# manual only: verify docs, license, security, release readiness",
        "# manual only: plan repository owner or organization move outside Brigade",
    ]


def audit_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    projects, errors, loaded = _read_config(target)
    enabled = [item for item in projects if item.get("enabled")]
    checks: list[dict[str, Any]] = []
    if errors:
        checks.extend({"status": WARN, "name": "projects_config", "detail": error} for error in errors)
    else:
        checks.append({"status": OK, "name": "projects_config", "detail": str(config_path(target))})
    audited: list[dict[str, Any]] = []
    for project in enabled:
        readiness = project["readiness"]
        missing = [key for key, value in readiness.items() if not value and project["decision"] == "move-candidate"]
        if missing:
            checks.append(
                {
                    "status": WARN,
                    "name": "project_readiness_missing",
                    "detail": f"{project['id']} missing {', '.join(missing)} readiness",
                    "project_id": project["id"],
                }
            )
        audited.append({**project, "migration_plan": {"manual_commands": _manual_commands(project), "missing_readiness": missing}})
    issues = [check for check in checks if check["status"] != OK]
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "config_loaded": loaded,
        "projects": audited,
        "project_count": len(audited),
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def audit(*, target: Path, json_output: bool = False) -> int:
    payload = audit_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print(f"projects audit: {payload['target']}")
    print(f"projects: {payload['project_count']}")
    print(f"issues: {payload['issue_count']}")
    for project in payload["projects"]:
        print(f"- {project['id']} decision={project['decision']} owner={project['recommended_owner_label']}")
    for issue in payload["issues"]:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0 if payload["config_loaded"] else 1


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        project_id = str(issue.get("project_id") or "projects")
        name = str(issue.get("name") or "project_issue")
        detail = str(issue.get("detail") or name)
        fingerprint = work_cmd._stable_hash({"project_id": project_id, "name": name, "detail": detail})
        records.append(
            {
                "text": f"Resolve project consolidation issue: {detail}",
                "kind": "task",
                "source": "project-consolidation",
                "type": "workflow",
                "priority": "normal",
                "template": "docs",
                "acceptance": [
                    "The project decision or readiness gap is resolved or explicitly deferred.",
                    "No remote repository mutation is performed by Brigade.",
                ],
                "metadata": {
                    "project_id": project_id,
                    "issue_type": name,
                    "safe_summary": detail,
                    "source_item_key": f"{project_id}:{name}",
                    "source_fingerprint": fingerprint,
                },
            }
        )
    return records


def import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    payload = audit_payload(target)
    imported, skipped, dismissed = work_cmd._append_import_records(target.expanduser().resolve(), _records(payload), dry_run=dry_run)
    output = {"target": payload["target"], "created": len(imported), "skipped": len(skipped), "dismissed": len(dismissed), "dry_run": dry_run}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"project_consolidation_imports: {payload['target']}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    return 0


def health(target: Path) -> dict[str, Any]:
    payload = audit_payload(target)
    return {
        "target": payload["target"],
        "project_count": payload["project_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "checks": payload["checks"],
    }
