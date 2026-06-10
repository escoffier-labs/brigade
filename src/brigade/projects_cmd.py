"""Local project consolidation audit."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import toml_compat as tomllib
from . import work_cmd
from .localio import read_json_dict as _read_json, utc_now as _now, write_json as _write_json

OK = "ok"
WARN = "warn"
DECISIONS = {"bake-in", "integrate", "catalog-only", "move-candidate", "leave-alone"}
PROJECT_CLOSEOUT_STATUSES = {"reviewed", "deferred", "superseded", "archived"}
QUIETING_PROJECT_CLOSEOUT_STATUSES = {"reviewed", "deferred", "archived"}


def config_path(target: Path) -> Path:
    return target / ".brigade" / "projects.toml"


def readiness_root(target: Path) -> Path:
    return target / ".brigade" / "projects" / "readiness"


def closeout_root(target: Path) -> Path:
    return target / ".brigade" / "projects" / "closeouts"


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    rendered = str(value).strip()
    return [rendered] if rendered else []


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
                "migration_blockers": _as_list(item.get("migration_blockers")),
                "readiness": {
                    "docs": bool(item.get("docs_ready", False)),
                    "license": bool(item.get("license_ready", False)),
                    "security": bool(item.get("security_ready", False)),
                    "release": bool(item.get("release_ready", False)),
                    "ownership": bool(item.get("ownership_ready", False)),
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


def _required_readiness(decision: str) -> list[str]:
    return {
        "bake-in": ["docs", "security"],
        "integrate": ["docs", "security", "release"],
        "catalog-only": ["docs", "security"],
        "move-candidate": ["docs", "license", "security", "release", "ownership"],
        "leave-alone": [],
    }.get(decision, [])


def _project_readiness_record(project: dict[str, Any]) -> dict[str, Any]:
    readiness = project.get("readiness") if isinstance(project.get("readiness"), dict) else {}
    required = _required_readiness(str(project.get("decision") or "leave-alone"))
    missing = [key for key in required if not readiness.get(key)]
    blockers = _as_list(project.get("migration_blockers"))
    issue_names = [f"missing_{key}_readiness" for key in missing]
    issue_names.extend("migration_blocker" for _ in blockers)
    status = "ready" if not missing and not blockers else "blocked"
    if project.get("enabled") is False:
        status = "disabled"
    fingerprint = work_cmd._stable_hash(
        {
            "project_id": project.get("id"),
            "decision": project.get("decision"),
            "readiness": {key: bool(readiness.get(key)) for key in sorted(readiness)},
            "missing": missing,
            "migration_blockers": blockers,
        }
    )
    return {
        "project_id": project.get("id"),
        "safe_label": project.get("label"),
        "category": project.get("category"),
        "decision": project.get("decision"),
        "recommended_owner_label": project.get("recommended_owner_label"),
        "status": status,
        "required_readiness": required,
        "readiness": {key: bool(readiness.get(key)) for key in ["docs", "license", "security", "release", "ownership"]},
        "missing_readiness": missing,
        "migration_blocker_count": len(blockers),
        "migration_blockers": blockers,
        "issue_types": issue_names,
        "manual_commands": _manual_commands(project),
        "source_fingerprint": fingerprint,
    }


def readiness_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    projects, errors, loaded = _read_config(target)
    records = [_project_readiness_record(project) for project in projects if project.get("enabled")]
    issues: list[dict[str, Any]] = [{"status": WARN, "name": "projects_config", "detail": error} for error in errors]
    for record in records:
        if record["status"] == "blocked":
            detail = f"{record['project_id']} has {len(record['issue_types'])} readiness issue(s)"
            issues.append(
                {
                    "status": WARN,
                    "name": "project_readiness_blocked",
                    "detail": detail,
                    "project_id": record["project_id"],
                    "source_fingerprint": record["source_fingerprint"],
                }
            )
    fingerprint = work_cmd._stable_hash(
        {
            "config_loaded": loaded,
            "errors": errors,
            "projects": [
                {
                    "project_id": record["project_id"],
                    "decision": record["decision"],
                    "status": record["status"],
                    "source_fingerprint": record["source_fingerprint"],
                }
                for record in records
            ],
        }
    )
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "config_loaded": loaded,
        "generated_at": _now().isoformat(),
        "receipt_fingerprint": fingerprint,
        "projects": records,
        "project_count": len(records),
        "ready_count": sum(1 for record in records if record["status"] == "ready"),
        "blocked_count": sum(1 for record in records if record["status"] == "blocked"),
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
        "remote_mutation": False,
        "manual_only": True,
    }


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
        readiness_record = _project_readiness_record(project)
        missing = readiness_record["missing_readiness"]
        if missing:
            checks.append(
                {
                    "status": WARN,
                    "name": "project_readiness_missing",
                    "detail": f"{project['id']} missing {', '.join(missing)} readiness",
                    "project_id": project["id"],
                }
            )
        if readiness_record["migration_blockers"]:
            checks.append(
                {
                    "status": WARN,
                    "name": "project_migration_blocked",
                    "detail": f"{project['id']} has {readiness_record['migration_blocker_count']} migration blocker(s)",
                    "project_id": project["id"],
                }
            )
        audited.append(
            {
                **project,
                "migration_plan": {"manual_commands": _manual_commands(project), "missing_readiness": missing},
                "readiness_status": readiness_record["status"],
            }
        )
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


def readiness_plan(*, target: Path, json_output: bool = False) -> int:
    payload = readiness_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print(f"projects readiness plan: {payload['target']}")
    print(f"projects: {payload['project_count']}")
    print(f"ready: {payload['ready_count']}")
    print(f"blocked: {payload['blocked_count']}")
    print("remote_mutation: false")
    for project in payload["projects"]:
        print(f"- {project['project_id']} decision={project['decision']} status={project['status']}")
    for issue in payload["issues"]:
        print(f"[{issue['status']}] {issue['name']}: {issue['detail']}")
    return 0 if payload["config_loaded"] else 1


def readiness_record(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = readiness_payload(target)
    readiness_id = f"{_now().strftime('%Y%m%d-%H%M%S-%f')}-project-readiness"
    payload.update({"readiness_id": readiness_id, "created_at": _now().isoformat(), "status": "recorded"})
    root = readiness_root(target) / readiness_id
    _write_json(root / "readiness.json", payload)
    payload["path"] = str(root)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print(f"project_readiness: {readiness_id}")
    print(f"path: {root}")
    print(f"blocked: {payload['blocked_count']}")
    print("remote_mutation: false")
    return 0 if payload["config_loaded"] else 1


def _readiness_receipts(target: Path) -> list[dict[str, Any]]:
    root = readiness_root(target.expanduser().resolve())
    receipts: list[dict[str, Any]] = []
    if not root.is_dir():
        return receipts
    for path in sorted(root.glob("*/readiness.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("readiness_id", path.parent.name)
        payload["path"] = str(path.parent)
        receipts.append(payload)
    return sorted(
        receipts, key=lambda item: str(item.get("created_at") or item.get("readiness_id") or ""), reverse=True
    )


def _project_closeouts(target: Path) -> list[dict[str, Any]]:
    root = closeout_root(target.expanduser().resolve())
    receipts: list[dict[str, Any]] = []
    if not root.is_dir():
        return receipts
    for path in sorted(root.glob("*/closeout.json")):
        payload = _read_json(path)
        if payload is None:
            continue
        payload.setdefault("closeout_id", path.parent.name)
        payload["path"] = str(path.parent)
        receipts.append(payload)
    return sorted(receipts, key=lambda item: str(item.get("created_at") or item.get("closeout_id") or ""), reverse=True)


def _latest_closeout_by_project(target: Path) -> dict[str, dict[str, Any]]:
    closeouts = _project_closeouts(target)
    latest: dict[str, dict[str, Any]] = {}
    for closeout in closeouts:
        status = str(closeout.get("status") or "")
        for item in closeout.get("projects", []):
            if not isinstance(item, dict):
                continue
            project_id = str(item.get("project_id") or "")
            if project_id and project_id not in latest:
                latest[project_id] = {
                    **item,
                    "closeout_id": closeout.get("closeout_id"),
                    "closeout_status": status,
                    "created_at": closeout.get("created_at"),
                    "path": closeout.get("path"),
                }
    return latest


def _project_issue_summary(target: Path) -> dict[str, Any]:
    readiness = readiness_payload(target)
    closeout_by_project = _latest_closeout_by_project(target)
    active: list[dict[str, Any]] = []
    quieted: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for record in readiness.get("projects", []):
        if not isinstance(record, dict) or record.get("status") != "blocked":
            continue
        project_id = str(record.get("project_id") or "")
        closeout = closeout_by_project.get(project_id)
        issue = {
            "status": WARN,
            "name": "project_readiness_blocked",
            "detail": f"{project_id} has {len(record.get('issue_types') or [])} readiness issue(s)",
            "project_id": project_id,
            "source_fingerprint": record.get("source_fingerprint"),
            "readiness_status": record.get("status"),
            "decision": record.get("decision"),
        }
        if closeout and closeout.get("source_fingerprint") != record.get("source_fingerprint"):
            changed_issue = {
                **issue,
                "name": "project_closeout_changed",
                "detail": f"{project_id} changed since project closeout {closeout.get('closeout_id')}",
                "closeout_id": closeout.get("closeout_id"),
            }
            changed.append(changed_issue)
            active.append(changed_issue)
            continue
        if closeout and closeout.get("closeout_status") in QUIETING_PROJECT_CLOSEOUT_STATUSES:
            quieted.append(
                {
                    **issue,
                    "closeout_id": closeout.get("closeout_id"),
                    "closeout_status": closeout.get("closeout_status"),
                }
            )
            continue
        active.append(issue)
    config_issues = [
        issue
        for issue in readiness.get("issues", [])
        if isinstance(issue, dict) and issue.get("name") == "projects_config"
    ]
    active = config_issues + active
    return {
        "readiness": readiness,
        "issues": active,
        "issue_count": len(active),
        "top_issue": active[0] if active else None,
        "quieted": quieted,
        "quieted_count": len(quieted),
        "changed_fingerprint_issues": changed,
        "changed_fingerprint_count": len(changed),
        "latest_closeout": _project_closeouts(target)[0] if _project_closeouts(target) else None,
    }


def _find_readiness(target: Path, readiness_id: str) -> tuple[dict[str, Any] | None, str | None]:
    receipts = _readiness_receipts(target)
    if readiness_id == "latest":
        if not receipts:
            return None, "project readiness receipt not found: latest"
        return receipts[0], None
    matches = [item for item in receipts if str(item.get("readiness_id") or "").startswith(readiness_id)]
    if not matches:
        return None, f"project readiness receipt not found: {readiness_id}"
    if len(matches) > 1:
        return None, f"project readiness receipt id is ambiguous: {readiness_id}"
    return matches[0], None


def readiness_list(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipts = _readiness_receipts(target)
    payload = {"target": str(target), "receipts": receipts, "receipt_count": len(receipts)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"project_readiness_receipts: {target}")
    print(f"receipts: {len(receipts)}")
    for receipt in receipts:
        print(f"- {receipt.get('readiness_id')} blocked={receipt.get('blocked_count')}")
    return 0


def readiness_show(*, target: Path, readiness_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipt, error = _find_readiness(target, readiness_id)
    if receipt is None:
        print(f"error: {error}", file=sys.stderr)
        return 1
    payload = {"target": str(target), "receipt": receipt}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"project_readiness: {receipt.get('readiness_id')}")
    print(f"status: {receipt.get('status')}")
    print(f"projects: {receipt.get('project_count')}")
    print(f"blocked: {receipt.get('blocked_count')}")
    return 0


def closeout(
    *, target: Path, status: str, reason: str, project_id: str | None = None, json_output: bool = False
) -> int:
    if status not in PROJECT_CLOSEOUT_STATUSES:
        print(f"error: status must be one of: {', '.join(sorted(PROJECT_CLOSEOUT_STATUSES))}", file=sys.stderr)
        return 1
    target = target.expanduser().resolve()
    readiness = readiness_payload(target)
    blocked = [
        project
        for project in readiness.get("projects", [])
        if isinstance(project, dict) and project.get("status") == "blocked"
    ]
    if project_id:
        blocked = [project for project in blocked if project.get("project_id") == project_id]
    if project_id and not blocked:
        print(f"error: blocked project not found: {project_id}", file=sys.stderr)
        return 1
    closeout_id = f"{_now().strftime('%Y%m%d-%H%M%S-%f')}-project-closeout"
    payload = {
        "target": str(target),
        "closeout_id": closeout_id,
        "created_at": _now().isoformat(),
        "status": status,
        "reason": reason,
        "project_count": len(blocked),
        "projects": [
            {
                "project_id": project.get("project_id"),
                "safe_label": project.get("safe_label"),
                "decision": project.get("decision"),
                "readiness_status": project.get("status"),
                "missing_readiness": project.get("missing_readiness"),
                "migration_blocker_count": project.get("migration_blocker_count"),
                "source_fingerprint": project.get("source_fingerprint"),
            }
            for project in blocked
        ],
        "quieting_status": status in QUIETING_PROJECT_CLOSEOUT_STATUSES,
        "manual_only": True,
        "remote_mutation": False,
        "receipt_fingerprint": work_cmd._stable_hash(
            {
                "status": status,
                "reason": reason,
                "projects": [
                    {"project_id": project.get("project_id"), "source_fingerprint": project.get("source_fingerprint")}
                    for project in blocked
                ],
            }
        ),
    }
    root = closeout_root(target) / closeout_id
    _write_json(root / "closeout.json", payload)
    payload["path"] = str(root)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"project_closeout: {closeout_id}")
    print(f"status: {status}")
    print(f"projects: {len(blocked)}")
    print(f"path: {root}")
    print("remote_mutation: false")
    return 0


def closeouts(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipts = _project_closeouts(target)
    payload = {"target": str(target), "closeouts": receipts, "closeout_count": len(receipts)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"project_closeouts: {target}")
    print(f"closeouts: {len(receipts)}")
    for receipt in receipts:
        print(f"- {receipt.get('closeout_id')} status={receipt.get('status')} projects={receipt.get('project_count')}")
    return 0


def closeout_show(*, target: Path, closeout_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    receipts = _project_closeouts(target)
    if closeout_id == "latest":
        matches = receipts[:1]
    else:
        matches = [receipt for receipt in receipts if str(receipt.get("closeout_id") or "").startswith(closeout_id)]
    if not matches:
        print(f"error: project closeout not found: {closeout_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: project closeout id is ambiguous: {closeout_id}", file=sys.stderr)
        return 1
    payload = {"target": str(target), "closeout": matches[0]}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    receipt = matches[0]
    print(f"project_closeout: {receipt.get('closeout_id')}")
    print(f"status: {receipt.get('status')}")
    print(f"projects: {receipt.get('project_count')}")
    return 0


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    readiness_by_project = {
        str(project.get("project_id")): project
        for project in readiness_payload(Path(payload["target"])).get("projects", [])
        if isinstance(project, dict)
    }
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        project_id = str(issue.get("project_id") or "projects")
        name = str(issue.get("name") or "project_issue")
        detail = str(issue.get("detail") or name)
        readiness = readiness_by_project.get(project_id, {})
        fingerprint = str(
            readiness.get("source_fingerprint")
            or work_cmd._stable_hash({"project_id": project_id, "name": name, "detail": detail})
        )
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
                    "decision": readiness.get("decision"),
                    "readiness_status": readiness.get("status"),
                    "source_item_key": f"{project_id}:{name}",
                    "source_fingerprint": fingerprint,
                },
            }
        )
    return records


def import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    summary = _project_issue_summary(target)
    readiness = summary["readiness"]
    payload = {
        **readiness,
        "issues": summary["issues"],
        "issue_count": summary["issue_count"],
        "top_issue": summary["top_issue"],
    }
    imported, skipped, dismissed = work_cmd._append_import_records(target, _records(payload), dry_run=dry_run)
    output = {
        "target": payload["target"],
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
        "dry_run": dry_run,
    }
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
    readiness = readiness_payload(target)
    latest = _readiness_receipts(target)
    issues = _project_issue_summary(target)
    return {
        "target": payload["target"],
        "project_count": payload["project_count"],
        "issue_count": issues["issue_count"],
        "top_issue": issues["top_issue"],
        "checks": issues["issues"] or [check for check in payload["checks"] if check.get("status") == OK],
        "readiness": {
            "ready_count": readiness["ready_count"],
            "blocked_count": readiness["blocked_count"],
            "receipt_fingerprint": readiness["receipt_fingerprint"],
            "latest": latest[0] if latest else None,
        },
        "closeout": {
            "latest": issues["latest_closeout"],
            "quieted_count": issues["quieted_count"],
            "changed_fingerprint_count": issues["changed_fingerprint_count"],
            "quieted": issues["quieted"],
        },
    }
