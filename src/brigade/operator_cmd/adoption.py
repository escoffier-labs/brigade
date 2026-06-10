from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..localio import write_json as _write_json
from ..selection import KNOWN_HARNESSES, WRITER_INBOXES
from .guide import _steps
from .surfaces import _operator_surface_inventory


def adoption_plan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    workspace = _workspace_inventory(target)
    surfaces = _operator_surface_inventory()
    issues = _adoption_issues(workspace, surfaces)
    brigade_present = _brigade_operator_config_present(workspace)
    modeled_surface_count = sum(1 for surface in surfaces.values() if int(surface.get("count") or 0) > 0)
    if brigade_present and not issues:
        status = "managed"
    elif brigade_present:
        status = "partial"
    elif modeled_surface_count:
        status = "needs-adoption"
    else:
        status = "unmanaged"
    commands = _adoption_next_commands(issues)
    return {
        "target": str(target),
        "status": status,
        "privacy": {
            "raw_crontab_lines_included": False,
            "raw_openclaw_jobs_included": False,
            "raw_pm2_processes_included": False,
            "external_command_output_redacted_to_counts": True,
        },
        "workspace": workspace,
        "surfaces": surfaces,
        "issue_count": len(issues),
        "issues": issues,
        "suggested_next_commands": commands,
        "boundaries": [
            "Read-only: does not write files.",
            "Does not start services, activate hooks, install schedulers, or mutate remotes.",
            "Does not include raw crontab lines, OpenClaw job names, PM2 process names, command paths, or environment values.",
        ],
    }


def adoption_plan(*, target: Path, json_output: bool = False) -> int:
    payload = adoption_plan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator adoption plan: {payload['target']}")
    print(f"status: {payload['status']}")
    print(f"brigade_root: {'yes' if payload['workspace']['brigade']['root_exists'] else 'no'}")
    print(
        f"guidance_files: {payload['workspace']['guidance']['present_count']} (+{payload['workspace']['guidance']['present_dir_count']} dirs)"
    )
    print(f"handoff_inboxes: {payload['workspace']['harnesses']['handoff_inbox_count']}")
    print(f"shell_crontab_active: {payload['surfaces']['shell_crontab']['count']}")
    print(f"openclaw_cron_jobs: {payload['surfaces']['openclaw_cron']['count']}")
    print(f"pm2_processes: {payload['surfaces']['pm2']['count']}")
    print(f"issues: {payload['issue_count']}")
    for issue in payload["issues"]:
        print(f"- {issue['severity']} {issue['name']}: {issue['detail']}")
    if payload["suggested_next_commands"]:
        print("next:")
        for command in payload["suggested_next_commands"]:
            print(f"- {command}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


def adoption_capture_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    plan = adoption_plan_payload(target)
    captured_at = datetime.now(timezone.utc).isoformat()
    capture = {
        "schema_version": 1,
        "captured_at": captured_at,
        "target": plan["target"],
        "status": plan["status"],
        "privacy": plan["privacy"],
        "workspace": plan["workspace"],
        "surfaces": plan["surfaces"],
        "issue_count": plan["issue_count"],
        "issues": plan["issues"],
        "suggested_next_commands": plan["suggested_next_commands"],
        "source_fingerprint": _adoption_fingerprint(plan),
    }
    return capture


def adoption_capture(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    payload = adoption_capture_payload(target)
    latest_path = _adoption_latest_path(target)
    snapshot_path = _adoption_snapshot_path(target, str(payload["source_fingerprint"]))
    _write_json(latest_path, payload)
    _write_json(snapshot_path, payload)
    result = {
        "target": str(target),
        "status": payload["status"],
        "issue_count": payload["issue_count"],
        "capture_path": str(latest_path),
        "snapshot_path": str(snapshot_path),
        "source_fingerprint": payload["source_fingerprint"],
        "privacy": payload["privacy"],
    }
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"operator adoption capture: {target}")
    print(f"status: {payload['status']}")
    print(f"issues: {payload['issue_count']}")
    print(f"capture_path: {latest_path}")
    print(f"snapshot_path: {snapshot_path}")
    print("privacy: raw scheduler and process details are omitted")
    return 0


def adoption_import_issues(*, target: Path, dry_run: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    plan = _read_latest_adoption_capture(target) or adoption_plan_payload(target)
    records = _adoption_import_records(plan)
    from .. import work_cmd

    imported, skipped, skipped_dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target": str(target),
        "source": "operator-adoption",
        "dry_run": dry_run,
        "status": plan.get("status"),
        "capture_path": str(_adoption_latest_path(target)) if _adoption_latest_path(target).exists() else None,
        "candidate_count": len(records),
        "imported": len(imported),
        "skipped": len(skipped),
        "dismissed": len(skipped_dismissed),
        "imports": imported,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"operator adoption imports: {target}")
    print(f"status: {plan.get('status')}")
    print(f"dry_run: {dry_run}")
    print(f"candidates: {len(records)}")
    print(f"imported: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    if skipped_dismissed:
        print(f"dismissed: {len(skipped_dismissed)}")
    for item in imported:
        print(f"- {item.get('id')}: {item.get('text')}")
    return 0


def _workspace_inventory(target: Path) -> dict[str, Any]:
    guidance_paths = [
        "AGENTS.md",
        "CLAUDE.md",
        "MEMORY.md",
        "TOOLS.md",
        "USER.md",
        "rules",
        ".learnings",
        "memory/cards",
    ]
    guidance_dirs = {"rules", ".learnings", "memory/cards"}
    guidance = [
        {"path": rel, "exists": (target / rel).exists(), "kind": "dir" if rel in guidance_dirs else "file"}
        for rel in guidance_paths
    ]
    harness_rows = []
    for harness in KNOWN_HARNESSES:
        root = target / f".{harness}"
        inbox_rel = WRITER_INBOXES.get(harness)
        inbox_exists = bool(inbox_rel and (target / inbox_rel).is_dir())
        harness_rows.append(
            {
                "id": harness,
                "root_exists": root.exists(),
                "handoff_inbox": inbox_rel,
                "handoff_inbox_exists": inbox_exists,
            }
        )
    local_state_paths = [
        "scripts",
        "logs",
        "backups",
        "cron-fragments",
        "pipeline",
        "memory-handoffs",
    ]
    local_state = [{"path": rel, "exists": (target / rel).exists()} for rel in local_state_paths]
    brigade_root = target / ".brigade"
    return {
        "brigade": {
            "root_exists": brigade_root.exists(),
            "config_exists": (brigade_root / "config.json").exists(),
            "local_config_count": len(
                [path for path in _steps(target, profile="local-operator") if path["path"].exists()]
            ),
        },
        "guidance": {
            "items": guidance,
            "present_count": sum(1 for item in guidance if item["exists"] and item["kind"] == "file"),
            "present_dir_count": sum(1 for item in guidance if item["exists"] and item["kind"] == "dir"),
        },
        "harnesses": {
            "items": harness_rows,
            "root_count": sum(1 for item in harness_rows if item["root_exists"]),
            "handoff_inbox_count": sum(1 for item in harness_rows if item["handoff_inbox_exists"]),
        },
        "local_state": {
            "items": local_state,
            "present_count": sum(1 for item in local_state if item["exists"]),
        },
    }


def _brigade_operator_config_present(workspace: dict[str, Any]) -> bool:
    brigade = workspace.get("brigade") if isinstance(workspace.get("brigade"), dict) else {}
    return bool(brigade.get("config_exists") or int(brigade.get("local_config_count") or 0) > 0)


def _adoption_issues(workspace: dict[str, Any], surfaces: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    brigade_present = _brigade_operator_config_present(workspace)
    surface_count = sum(int(surface.get("count") or 0) for surface in surfaces.values())
    if not brigade_present:
        issues.append(
            {
                "severity": "warn",
                "name": "brigade_operator_config_missing",
                "detail": "target has no Brigade operator config, so Brigade is not the operator control plane for this workspace yet",
                "suggested_next_command": "brigade operator quickstart --target . --depth workspace --harnesses codex --dry-run",
            }
        )
    if surface_count and not brigade_present:
        issues.append(
            {
                "severity": "warn",
                "name": "operator_surfaces_unmodeled",
                "detail": "machine scheduler or process surfaces exist, but Brigade only has an external count-level view",
                "suggested_next_command": "brigade operator adopt plan --target . --json",
            }
        )
    if workspace["harnesses"]["handoff_inbox_count"] and not brigade_present:
        issues.append(
            {
                "severity": "info",
                "name": "handoff_inboxes_unwatched_by_brigade",
                "detail": "handoff inboxes exist; add Brigade handoff source config before relying on Brigade work imports",
                "suggested_next_command": "brigade handoff sources init --target .",
            }
        )
    if workspace["local_state"]["present_count"] and not brigade_present:
        issues.append(
            {
                "severity": "info",
                "name": "local_state_needs_mapping",
                "detail": "local scripts, logs, backups, or pipeline folders exist and should be mapped before replacing current workflows",
                "suggested_next_command": "brigade operator quickstart --target . --depth workspace --dry-run",
            }
        )
    if brigade_present and surface_count:
        issues.append(
            {
                "severity": "info",
                "name": "external_surfaces_present",
                "detail": "external scheduler or process surfaces exist; Brigade is reporting counts but does not manage them",
                "suggested_next_command": "brigade operator surfaces capture --target . --json",
            }
        )
    return issues


def _adoption_next_commands(issues: list[dict[str, Any]]) -> list[str]:
    commands = []
    for issue in issues:
        command = issue.get("suggested_next_command")
        if isinstance(command, str) and command not in commands:
            commands.append(command)
    if not commands:
        commands.append("brigade operator doctor --target . --profile local-operator")
    return commands


def _adoption_dir(target: Path) -> Path:
    return target / ".brigade" / "operator" / "adoption"


def _adoption_latest_path(target: Path) -> Path:
    return _adoption_dir(target) / "latest.json"


def _adoption_snapshot_path(target: Path, fingerprint: str) -> Path:
    return _adoption_dir(target) / "snapshots" / f"{fingerprint}.json"


def _read_latest_adoption_capture(target: Path) -> dict[str, Any] | None:
    path = _adoption_latest_path(target)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _adoption_fingerprint(plan: dict[str, Any]) -> str:
    from .. import work_cmd

    payload = {
        "status": plan.get("status"),
        "workspace": plan.get("workspace"),
        "surfaces": plan.get("surfaces"),
        "issues": plan.get("issues"),
    }
    return work_cmd._stable_hash(payload)


def _adoption_import_records(plan: dict[str, Any]) -> list[dict[str, Any]]:
    from .. import work_cmd

    records: list[dict[str, Any]] = []
    for issue in plan.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        name = str(issue.get("name") or "operator_adoption_issue")
        detail = str(issue.get("detail") or name)
        severity = str(issue.get("severity") or "warn")
        suggested = str(issue.get("suggested_next_command") or "brigade operator adopt plan --target . --json")
        fingerprint = work_cmd._stable_hash(
            {
                "source": "operator-adoption",
                "name": name,
                "detail": detail,
                "status": plan.get("status"),
                "surface_counts": {
                    key: value.get("count")
                    for key, value in (plan.get("surfaces") or {}).items()
                    if isinstance(value, dict)
                },
                "workspace_counts": _adoption_workspace_counts(plan.get("workspace")),
            }
        )
        records.append(
            {
                "text": f"Bridge operator adoption gap: {detail}",
                "kind": "task",
                "source": "operator-adoption",
                "type": "workflow",
                "priority": "high" if severity == "warn" else "normal",
                "template": "vertical-slice",
                "acceptance": [
                    "The adoption gap is resolved, deferred, or documented with an explicit local reason.",
                    "A fresh `brigade operator adopt plan --target . --json` no longer reports this issue unchanged.",
                    "No raw scheduler lines, process names, private paths, hostnames, tokens, or environment values are committed or pasted into public docs.",
                ],
                "metadata": {
                    "issue_type": name,
                    "safe_summary": detail,
                    "severity": severity,
                    "adoption_status": plan.get("status"),
                    "suggested_next_command": suggested,
                    "source_item_key": f"operator-adoption:{name}",
                    "source_fingerprint": fingerprint,
                    "capture_fingerprint": plan.get("source_fingerprint"),
                    "capture_path": str(_adoption_latest_path(Path(str(plan.get("target") or "."))))
                    if plan.get("target")
                    else None,
                },
            }
        )
    return records


def _adoption_workspace_counts(workspace: Any) -> dict[str, Any]:
    if not isinstance(workspace, dict):
        return {}
    return {
        "guidance_present": (workspace.get("guidance") or {}).get("present_count")
        if isinstance(workspace.get("guidance"), dict)
        else None,
        "harness_roots": (workspace.get("harnesses") or {}).get("root_count")
        if isinstance(workspace.get("harnesses"), dict)
        else None,
        "handoff_inboxes": (workspace.get("harnesses") or {}).get("handoff_inbox_count")
        if isinstance(workspace.get("harnesses"), dict)
        else None,
        "local_state": (workspace.get("local_state") or {}).get("present_count")
        if isinstance(workspace.get("local_state"), dict)
        else None,
    }
