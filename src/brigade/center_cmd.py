"""Read-only local operator center views."""
from __future__ import annotations

import html
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import chat_cmd, context_cmd, handoff_cmd, learn_cmd, memory_cmd, projects_cmd, release_cmd, repos_cmd, roadmap_cmd, security_cmd, tools_cmd, work_cmd

SCHEMA_VERSION = 1
REPORT_STALE_HOURS = 24


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
    project_health = projects_cmd.health(target)
    for issue in project_health.get("checks", []):
        if issue.get("status") != "ok":
            items.append(_item("project-consolidation", str(issue.get("project_id") or issue.get("name")), str(issue.get("status")), str(issue.get("detail")), "brigade projects audit"))
    context_health = context_cmd.health(target)
    for issue in context_health.get("issues", []):
        items.append(_item("context", str(issue.get("name")), str(issue.get("status")), str(issue.get("detail")), "brigade context plan"))
    candidate = release_cmd._latest_candidate(target)
    if isinstance(candidate, dict) and candidate.get("status") in {"draft", "blocked"}:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        items.append(_item("release-candidate", candidate_id, str(candidate.get("status") or "draft"), "release candidate awaits review", f"brigade release candidate compare {candidate_id}", updated_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None, path=candidate.get("path") if isinstance(candidate.get("path"), str) else None))
    return items


def status_payload(target: Path) -> dict[str, Any]:
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
        "operator_report": report_health(target),
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


def _reports_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "reports"


def _reports_archive_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "reports-archive"


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
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append({"status": "warn", "name": "operator_report_missing", "detail": "no local operator report has been built", "suggested_next_command": "brigade center report build"})
        return {"latest": None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
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
    latest_activity = _activity(target)
    report_activity = latest.get("activity") if isinstance(latest.get("activity"), list) else []
    latest_time = _parse_time(latest_activity[0].get("updated_at")) if latest_activity else None
    report_time = _parse_time(report_activity[0].get("updated_at")) if report_activity else created
    if latest_time is not None and report_time is not None and latest_time > report_time:
        checks.append({"status": "warn", "name": "operator_report_newer_activity", "detail": f"{latest.get('report_id')} is older than local activity", "suggested_next_command": "brigade center report build"})
    return {"latest": latest, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


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
