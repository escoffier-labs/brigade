"""Read-only local operator center views."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import context_cmd, handoff_cmd, learn_cmd, projects_cmd, release_cmd, repos_cmd, roadmap_cmd, security_cmd, tools_cmd, work_cmd


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _item(subsystem: str, local_id: str, status: str, summary: str, command: str, *, priority: str | None = None, severity: str | None = None, updated_at: str | None = None) -> dict[str, Any]:
    return {
        "subsystem": subsystem,
        "id": local_id,
        "status": status,
        "priority": priority,
        "severity": severity,
        "safe_summary": summary,
        "updated_at": updated_at,
        "suggested_next_command": command,
    }


def _activity(target: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for session in work_cmd._collect_sessions(target / ".brigade" / "work")[0][:20]:
        path, payload = session
        items.append(_item("work-session", str(payload.get("id") or path.name), str(payload.get("status") or "unknown"), str(payload.get("title") or "work session"), f"brigade work show {path.name}", updated_at=payload.get("ended_at") or payload.get("started_at")))
    for receipt in work_cmd._scanner_receipts(target)[:20]:
        run_id = str(receipt.get("run_id") or "")
        items.append(_item("scanner-run", run_id, str(receipt.get("status") or "unknown"), str(receipt.get("scanner_id") or "scanner run"), f"brigade work scanners run-show {run_id}", updated_at=receipt.get("completed_at") or receipt.get("started_at")))
    for sweep in work_cmd._scanner_sweeps(target)[:20]:
        sweep_id = str(sweep.get("sweep_id") or "")
        items.append(_item("scanner-sweep", sweep_id, str(sweep.get("status") or "unknown"), "scanner sweep", f"brigade work sweep-show {sweep_id}", updated_at=sweep.get("completed_at") or sweep.get("started_at")))
    for receipt in work_cmd._review_receipts(target)[:20]:
        run_id = str(receipt.get("run_id") or "")
        items.append(_item("code-review", run_id, str(receipt.get("status") or "unknown"), str(receipt.get("reviewer_id") or "review run"), f"brigade work review show {run_id}", updated_at=receipt.get("completed_at") or receipt.get("started_at")))
    for pack in context_cmd._packs(target)[:20]:
        pack_id = str(pack.get("pack_id") or "")
        items.append(_item("context-pack", pack_id, str(pack.get("status") or "built"), str(pack.get("kind") or "context"), f"brigade context show {pack_id}", updated_at=pack.get("created_at")))
    release = release_cmd._latest_release_receipt(target)
    if release:
        run_id = str(release.get("run_id") or "latest")
        items.append(_item("release-readiness", run_id, str(release.get("status") or "unknown"), "release readiness", f"brigade release show {run_id}", updated_at=release.get("created_at")))
    candidate = release_cmd._latest_candidate(target)
    if candidate:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        items.append(_item("release-candidate", candidate_id, str(candidate.get("status") or "draft"), "release candidate", f"brigade release candidate show {candidate_id}", updated_at=candidate.get("created_at")))
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return items


def _reviews(target: Path) -> list[dict[str, Any]]:
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
                updated_at=item.get("updated_at") or item.get("created_at"),
            )
        )
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
    project_health = projects_cmd.health(target)
    for issue in project_health.get("checks", []):
        if issue.get("status") != "ok":
            items.append(_item("project-consolidation", str(issue.get("project_id") or issue.get("name")), str(issue.get("status")), str(issue.get("detail")), "brigade projects audit"))
    context_health = context_cmd.health(target)
    for issue in context_health.get("issues", []):
        items.append(_item("context", str(issue.get("name")), str(issue.get("status")), str(issue.get("detail")), "brigade context plan"))
    return items


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    active = work_cmd._active_session_info(target)
    pending_tasks = work_cmd._pending_tasks(target)
    pending_imports = work_cmd._pending_imports(target)
    return {
        "target": str(target),
        "active_session": active,
        "pending_task_count": len(pending_tasks),
        "pending_import_count": len(pending_imports),
        "scanner_sweeps": work_cmd._scanner_sweep_health(target),
        "code_review": work_cmd._review_health(target),
        "handoff_drafts": handoff_cmd.draft_queue_payload(target),
        "tool_catalog": tools_cmd.health(target),
        "learning": learn_cmd.health(target),
        "context": context_cmd.health(target),
        "release_readiness": release_cmd._latest_release_receipt(target),
        "release_candidate": release_cmd._latest_candidate(target),
        "repo_fleet": repos_cmd.health(target),
        "roadmap": roadmap_cmd.health(target),
        "projects": projects_cmd.health(target),
        "security": security_cmd.health(target),
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
    print(f"context_packs: {payload['context']['pack_count']}")
    return 0


def activity(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    target = target.expanduser().resolve()
    items = _activity(target)[:limit]
    payload = {"target": str(target), "activity": items, "activity_count": len(items)}
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
    payload = {"target": str(target), "reviews": items, "review_count": len(items)}
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
    payload = {"target": str(target), "templates": items, "template_count": len(items)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"center templates: {target}")
    for item in items:
        print(f"- {item['subsystem']}:{item['id']} {item['safe_summary']}")
    return 0
