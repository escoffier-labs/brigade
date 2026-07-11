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
    items.sort(
        key=lambda item: str(
            item.get("completed_at") or item.get("created_at") or item.get("started_at") or item.get("path") or ""
        ),
        reverse=True,
    )
    return items


def _actions_root(target: Path) -> Path:
    return target / ".brigade" / "center" / "actions"


def _actions_path(target: Path) -> Path:
    return _actions_root(target) / "actions.json"


def _actions_archive_path(target: Path) -> Path:
    return _actions_root(target) / "archive.jsonl"


def _read_actions(target: Path) -> list[dict[str, Any]]:
    return actionqueue.read_actions(_actions_path(target))


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
    actionqueue.append_archive(_actions_archive_path(target), actions)


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
                receipt_path=str(Path(str(receipt.get("path") or "")) / "receipt.json")
                if receipt.get("path")
                else None,
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
                receipt_path=str(Path(str(receipt.get("path") or "")) / "receipt.json")
                if receipt.get("path")
                else None,
                path=receipt.get("path") if isinstance(receipt.get("path"), str) else None,
            )
        )
    for sweep in work_cmd._scanner_sweeps(target)[:20]:
        sweep_id = str(sweep.get("sweep_id") or "")
        path = str(Path(str(sweep.get("path") or "")) / "sweep.json") if sweep.get("path") else None
        items.append(
            _item(
                "scanner-sweep",
                sweep_id,
                str(sweep.get("status") or "unknown"),
                "scanner sweep",
                f"brigade work sweep-show {sweep_id}",
                created_at=sweep.get("started_at") if isinstance(sweep.get("started_at"), str) else None,
                updated_at=sweep.get("completed_at") or sweep.get("started_at"),
                receipt_path=path,
                path=sweep.get("path") if isinstance(sweep.get("path"), str) else None,
            )
        )
    for receipt in work_cmd._review_receipts(target)[:20]:
        run_id = str(receipt.get("run_id") or "")
        items.append(
            _item(
                "code-review",
                run_id,
                str(receipt.get("status") or "unknown"),
                str(receipt.get("reviewer_id") or "review run"),
                f"brigade work review show {run_id}",
                created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None,
                updated_at=receipt.get("completed_at") or receipt.get("started_at"),
                receipt_path=str(Path(str(receipt.get("path") or "")) / "receipt.json")
                if receipt.get("path")
                else None,
                path=receipt.get("path") if isinstance(receipt.get("path"), str) else None,
            )
        )
    for draft in handoff_cmd.draft_queue_payload(target).get("drafts", [])[:20]:
        if not isinstance(draft, dict):
            continue
        draft_id = str(draft.get("id") or Path(str(draft.get("path") or "handoff")).stem)
        items.append(
            _item(
                "handoff-draft",
                draft_id,
                str(draft.get("status") or "pending"),
                str(draft.get("title") or draft.get("target_document") or "handoff draft"),
                f"brigade handoff show {draft_id}",
                severity=draft.get("severity") if isinstance(draft.get("severity"), str) else None,
                updated_at=draft.get("modified_at") if isinstance(draft.get("modified_at"), str) else None,
                path=draft.get("path") if isinstance(draft.get("path"), str) else None,
            )
        )
    for receipt in _iter_json_files(target / ".brigade" / "handoffs" / "ingest-runs", "*.json")[:20]:
        run_id = str(receipt.get("run_id") or Path(str(receipt.get("path") or "run")).stem)
        items.append(
            _item(
                "handoff-ingest",
                run_id,
                str(receipt.get("status") or "completed"),
                "handoff ingest receipt",
                f"brigade handoff run-show {run_id}",
                created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None,
                updated_at=receipt.get("completed_at") or receipt.get("started_at"),
                receipt_path=receipt.get("path") if isinstance(receipt.get("path"), str) else None,
            )
        )
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
        items.append(
            _item(
                "tool-call",
                call_id,
                str(call.get("status") or "unknown"),
                str(call.get("tool_id") or "tool call"),
                f"brigade tools call show {call_id}",
                severity=call.get("severity") if isinstance(call.get("severity"), str) else None,
                created_at=call.get("created_at") if isinstance(call.get("created_at"), str) else None,
                updated_at=call.get("reviewed_at") or call.get("created_at"),
                receipt_path=str(tools_cmd.calls_path(target)),
            )
        )
    for receipt in _iter_json_files(tools_cmd.runs_path(target), "*/receipt.json")[:20]:
        run_id = str(receipt.get("run_id") or Path(str(receipt.get("path") or "run")).parent.name)
        items.append(
            _item(
                "tool-run",
                run_id,
                str(receipt.get("status") or "unknown"),
                str(receipt.get("tool_id") or "tool run"),
                f"brigade tools run show {run_id}",
                created_at=receipt.get("started_at") if isinstance(receipt.get("started_at"), str) else None,
                updated_at=receipt.get("completed_at") or receipt.get("started_at"),
                receipt_path=receipt.get("path") if isinstance(receipt.get("path"), str) else None,
            )
        )
    for checkpoint in _iter_json_files(tools_cmd.checkpoints_path(target), "*.json")[:20]:
        checkpoint_id = str(checkpoint.get("checkpoint_id") or Path(str(checkpoint.get("path") or "checkpoint")).stem)
        items.append(
            _item(
                "checkpoint",
                checkpoint_id,
                str(checkpoint.get("status") or "waiting"),
                str(checkpoint.get("reason") or "tool checkpoint"),
                f"brigade tools checkpoint show {checkpoint_id}",
                severity=checkpoint.get("severity") if isinstance(checkpoint.get("severity"), str) else None,
                created_at=checkpoint.get("created_at") if isinstance(checkpoint.get("created_at"), str) else None,
                updated_at=checkpoint.get("reviewed_at") or checkpoint.get("created_at"),
                receipt_path=checkpoint.get("path") if isinstance(checkpoint.get("path"), str) else None,
            )
        )
    for pack in tools_cmd._tool_packs(target)[:20]:
        pack_id = str(pack.get("pack_id") or "")
        items.append(
            _item(
                "tool-pack",
                pack_id,
                str(pack.get("status") or "built"),
                "portable tool pack",
                f"brigade tools pack show {pack_id}",
                created_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None,
                updated_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None,
                receipt_path=str(Path(str(pack.get("path") or "")) / "tool-pack.json") if pack.get("path") else None,
                path=pack.get("path") if isinstance(pack.get("path"), str) else None,
            )
        )
    for pack in context_cmd._packs(target)[:20]:
        pack_id = str(pack.get("pack_id") or "")
        items.append(
            _item(
                "context-pack",
                pack_id,
                str(pack.get("status") or "built"),
                str(pack.get("kind") or "context"),
                f"brigade context show {pack_id}",
                created_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None,
                updated_at=pack.get("created_at") if isinstance(pack.get("created_at"), str) else None,
                receipt_path=str(Path(str(pack.get("path") or "")) / "context.json") if pack.get("path") else None,
                path=pack.get("path") if isinstance(pack.get("path"), str) else None,
            )
        )
    for receipt in _iter_json_files(target / ".brigade" / "context" / "sync-plans", "*/sync-plan.json")[:20]:
        sync_id = str(receipt.get("sync_id") or Path(str(receipt.get("path") or "sync")).parent.name)
        items.append(
            _item(
                "context-sync",
                sync_id,
                str(receipt.get("status") or "planned"),
                f"{receipt.get('destination_count', 0)} destination(s)",
                f"brigade context sync plan {receipt.get('pack_id') or 'latest'}",
                created_at=receipt.get("created_at") if isinstance(receipt.get("created_at"), str) else None,
                updated_at=receipt.get("created_at") if isinstance(receipt.get("created_at"), str) else None,
                receipt_path=receipt.get("path") if isinstance(receipt.get("path"), str) else None,
            )
        )
    for replay in _iter_json_files(target / ".brigade" / "learn" / "replays", "*/replay.json")[:20]:
        replay_id = str(replay.get("replay_id") or Path(str(replay.get("path") or "replay")).parent.name)
        items.append(
            _item(
                "learning-replay",
                replay_id,
                str(replay.get("status") or "recorded"),
                str(replay.get("scenario_id") or "learning replay"),
                "brigade learn plan",
                updated_at=replay.get("created_at") if isinstance(replay.get("created_at"), str) else None,
                receipt_path=replay.get("path") if isinstance(replay.get("path"), str) else None,
            )
        )
    security_latest = target / ".brigade" / "security" / "latest" / "security-report.json"
    security_report = _read_json(security_latest)
    if security_report is not None:
        generated = (
            security_report.get("generated_at") if isinstance(security_report.get("generated_at"), str) else None
        )
        items.append(
            _item(
                "security-report",
                "latest",
                "ready",
                "security report",
                "brigade security findings",
                created_at=generated,
                updated_at=generated,
                receipt_path=str(security_latest),
                path=str(security_latest.parent),
            )
        )
    for closeout in _iter_json_files(target / ".brigade" / "security" / "closeouts", "*/closeout.json")[:20]:
        closeout_id = str(closeout.get("closeout_id") or Path(str(closeout.get("path") or "closeout")).parent.name)
        items.append(
            _item(
                "security-closeout",
                closeout_id,
                str(closeout.get("status") or "reviewed"),
                "security closeout",
                "brigade security closeout",
                created_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None,
                updated_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None,
                receipt_path=closeout.get("path") if isinstance(closeout.get("path"), str) else None,
            )
        )
    for closeout in _iter_json_files(target / ".brigade" / "backups" / "closeouts", "*/closeout.json")[:20]:
        closeout_id = str(closeout.get("closeout_id") or Path(str(closeout.get("path") or "closeout")).parent.name)
        items.append(
            _item(
                "backup-closeout",
                closeout_id,
                str(closeout.get("status") or "reviewed"),
                "backup closeout",
                "brigade work backup closeout",
                created_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None,
                updated_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None,
                receipt_path=closeout.get("path") if isinstance(closeout.get("path"), str) else None,
            )
        )
    for closeout in _iter_json_files(target / ".brigade" / "memory-care" / "closeouts", "*/closeout.json")[:20]:
        closeout_id = str(closeout.get("closeout_id") or Path(str(closeout.get("path") or "closeout")).parent.name)
        items.append(
            _item(
                "memory-care-closeout",
                closeout_id,
                str(closeout.get("status") or "reviewed"),
                "memory-care closeout",
                "brigade memory care closeout",
                created_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None,
                updated_at=closeout.get("created_at") if isinstance(closeout.get("created_at"), str) else None,
                receipt_path=closeout.get("path") if isinstance(closeout.get("path"), str) else None,
            )
        )
    release = release_cmd._latest_release_receipt(target)
    if release:
        run_id = str(release.get("run_id") or "latest")
        items.append(
            _item(
                "release-readiness",
                run_id,
                str(release.get("status") or "unknown"),
                "release readiness",
                f"brigade release show {run_id}",
                created_at=release.get("started_at") if isinstance(release.get("started_at"), str) else None,
                updated_at=release.get("completed_at") or release.get("created_at") or release.get("started_at"),
                receipt_path=str(Path(str(release.get("path") or "")) / "receipt.json")
                if release.get("path")
                else None,
                path=release.get("path") if isinstance(release.get("path"), str) else None,
            )
        )
    candidate = release_cmd._latest_candidate(target)
    if candidate:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        items.append(
            _item(
                "release-candidate",
                candidate_id,
                str(candidate.get("status") or "draft"),
                "release candidate",
                f"brigade release candidate show {candidate_id}",
                created_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None,
                updated_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None,
                receipt_path=str(Path(str(candidate.get("path") or "")) / "EVIDENCE.json")
                if candidate.get("path")
                else None,
                path=candidate.get("path") if isinstance(candidate.get("path"), str) else None,
            )
        )
    for receipt in release_cmd._read_install_smoke_receipts(target)[:20]:
        receipt_id = str(receipt.get("receipt_id") or "")
        items.append(
            _item(
                "install-smoke",
                receipt_id,
                str(receipt.get("status") or "unknown"),
                str(receipt.get("matrix_id") or "install smoke"),
                f"brigade release smoke show {receipt_id}",
                created_at=receipt.get("created_at") if isinstance(receipt.get("created_at"), str) else None,
                updated_at=receipt.get("completed_at") or receipt.get("created_at"),
                receipt_path=str(release_cmd._install_smoke_receipts_path(target)),
            )
        )
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
        items.append(
            _item(
                "center-readiness",
                readiness_id,
                str(readiness.get("status") or "unknown"),
                "operator readiness closeout",
                f"brigade center readiness show {readiness_id}",
                created_at=readiness.get("created_at") if isinstance(readiness.get("created_at"), str) else None,
                updated_at=readiness.get("completed_at") or readiness.get("created_at"),
                receipt_path=str(Path(str(readiness.get("path") or "")) / "readiness.json")
                if readiness.get("path")
                else None,
                path=readiness.get("path") if isinstance(readiness.get("path"), str) else None,
            )
        )
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return items


def _reviews(target: Path) -> list[dict[str, Any]]:
    from .. import daily_cmd

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
            items.append(
                _item(
                    "code-review",
                    finding_id,
                    "pending",
                    str(finding.get("text") or finding.get("safe_detail") or "review finding"),
                    f"brigade work review finding-show {finding_id}",
                    severity=finding.get("severity") if isinstance(finding.get("severity"), str) else None,
                    updated_at=finding.get("created_at") if isinstance(finding.get("created_at"), str) else None,
                )
            )
    handoffs = handoff_cmd.draft_queue_payload(target)
    for draft in handoffs.get("drafts", [])[:20]:
        if isinstance(draft, dict) and draft.get("status") in {None, "pending", "failed", "invalid"}:
            draft_id = str(draft.get("id") or Path(str(draft.get("path") or "handoff")).stem)
            items.append(
                _item(
                    "handoff-draft",
                    draft_id,
                    str(draft.get("status") or "pending"),
                    str(draft.get("title") or draft.get("target_document") or "handoff draft"),
                    f"brigade handoff show {draft_id}",
                    severity=draft.get("severity") if isinstance(draft.get("severity"), str) else None,
                    updated_at=draft.get("modified_at") if isinstance(draft.get("modified_at"), str) else None,
                    path=draft.get("path") if isinstance(draft.get("path"), str) else None,
                )
            )
    research_handoffs = research_cmd.health(target)
    for issue in research_handoffs.get("runs", [])[:20]:
        if isinstance(issue, dict) and issue.get("status") != "exported":
            run_id = str(issue.get("run_id") or "")
            items.append(
                _item(
                    "research-handoff",
                    run_id,
                    str(issue.get("status") or "warn"),
                    str(issue.get("question") or "research handoff export"),
                    str(issue.get("suggested_next_command") or f"brigade research show {run_id}"),
                    severity="medium",
                )
            )
    tool_health = tools_cmd.health(target)
    for bucket, command in (
        ("call_queue", "brigade tools call list"),
        ("run_history", "brigade tools run list"),
        ("checkpoints", "brigade tools checkpoint list"),
    ):
        value = tool_health.get(bucket) if isinstance(tool_health.get(bucket), dict) else {}
        top = value.get("top_issue") if isinstance(value.get("top_issue"), dict) else None
        if top:
            items.append(
                _item(
                    "tools",
                    str(top.get("call_id") or top.get("run_id") or top.get("checkpoint_id") or bucket),
                    str(top.get("status") or "warn"),
                    str(top.get("detail") or top.get("issue_type") or bucket),
                    command,
                    severity=top.get("severity") if isinstance(top.get("severity"), str) else None,
                )
            )
    for name, health, command in (
        ("backup", work_cmd._backup_health(target), "brigade work backup status"),
        ("memory-care", memory_cmd.health(target), "brigade memory care status"),
        ("security", security_cmd.health(target), "brigade security findings"),
    ):
        top = health.get("top_issue") or health.get("top_finding")
        if isinstance(top, dict):
            items.append(
                _item(
                    name,
                    str(top.get("id") or top.get("name") or top.get("issue_type") or name),
                    str(top.get("status") or "warn"),
                    str(top.get("detail") or top.get("title") or top.get("safe_summary") or name),
                    command,
                    severity=top.get("severity") if isinstance(top.get("severity"), str) else None,
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
    learning_health = learn_cmd.health(target)
    replay = learning_health.get("replay") if isinstance(learning_health.get("replay"), dict) else {}
    replay_issue = replay.get("top_issue") if isinstance(replay.get("top_issue"), dict) else None
    if replay_issue:
        items.append(
            _item(
                "learning",
                str(replay_issue.get("compare_id") or "learning-replay"),
                str(replay_issue.get("status") or "warn"),
                str(replay_issue.get("detail") or "learning replay needs review"),
                "brigade learn replay compare latest",
            )
        )
    project_health = projects_cmd.health(target)
    for issue in project_health.get("checks", []):
        if issue.get("status") != "ok":
            items.append(
                _item(
                    "project-consolidation",
                    str(issue.get("project_id") or issue.get("name")),
                    str(issue.get("status")),
                    str(issue.get("detail")),
                    "brigade projects audit",
                )
            )
    repo_health = repos_cmd.health(target)
    repo_report = repo_health.get("report") if isinstance(repo_health.get("report"), dict) else {}
    repo_actions = repo_health.get("actions") if isinstance(repo_health.get("actions"), dict) else {}
    repo_sweep = repo_health.get("sweep") if isinstance(repo_health.get("sweep"), dict) else {}
    repo_release = repo_health.get("release_train") if isinstance(repo_health.get("release_train"), dict) else {}
    for bucket, command in (
        (repo_report, "brigade repos report build"),
        (repo_actions, "brigade repos actions list"),
        (repo_sweep, "brigade repos sweep run"),
        (repo_release, "brigade repos release build"),
    ):
        top = bucket.get("top_issue") if isinstance(bucket.get("top_issue"), dict) else None
        if top:
            items.append(
                _item(
                    "repo-fleet",
                    str(top.get("name") or "repo-fleet"),
                    str(top.get("status") or "warn"),
                    str(top.get("detail") or "repo fleet issue"),
                    str(top.get("suggested_next_command") or command),
                )
            )
    context_health = context_cmd.health(target)
    for issue in context_health.get("issues", []):
        items.append(
            _item(
                "context",
                str(issue.get("name")),
                str(issue.get("status")),
                str(issue.get("detail")),
                "brigade context plan",
            )
        )
    candidate = release_cmd._latest_candidate(target)
    if isinstance(candidate, dict) and candidate.get("status") in {"draft", "blocked"}:
        candidate_id = str(candidate.get("candidate_id") or "latest")
        items.append(
            _item(
                "release-candidate",
                candidate_id,
                str(candidate.get("status") or "draft"),
                "release candidate awaits review",
                f"brigade release candidate compare {candidate_id}",
                updated_at=candidate.get("created_at") if isinstance(candidate.get("created_at"), str) else None,
                path=candidate.get("path") if isinstance(candidate.get("path"), str) else None,
            )
        )
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
        items.append(
            _item(
                "center-readiness",
                str(top_readiness.get("name") or "readiness"),
                str(top_readiness.get("status") or "warn"),
                str(top_readiness.get("detail") or "operator readiness needs review"),
                "brigade center readiness plan",
            )
        )
    daily_health = daily_cmd.health(target)
    approvals = daily_health.get("approvals") if isinstance(daily_health.get("approvals"), dict) else {}
    top_approval = approvals.get("top_pending") if isinstance(approvals.get("top_pending"), dict) else None
    if top_approval:
        approval_id = str(top_approval.get("approval_id") or "approval")
        items.append(
            _item(
                "daily-approval",
                approval_id,
                str(top_approval.get("status") or "pending"),
                str(top_approval.get("safe_summary") or "daily approval pending"),
                f"brigade daily approvals show {approval_id}",
            )
        )
    top_daily = daily_health.get("top_issue") if isinstance(daily_health.get("top_issue"), dict) else None
    if top_daily:
        items.append(
            _item(
                "daily-driver",
                str(top_daily.get("name") or "daily"),
                str(top_daily.get("status") or "warn"),
                str(top_daily.get("detail") or "daily driver needs review"),
                "brigade daily doctor",
            )
        )
    phase_health = phases_cmd.health(target)
    top_phase = phase_health.get("top_issue") if isinstance(phase_health.get("top_issue"), dict) else None
    if top_phase:
        items.append(
            _item(
                "phase-ledger",
                str(top_phase.get("phase_id") or top_phase.get("name") or "phase-ledger"),
                str(top_phase.get("status") or "warn"),
                str(top_phase.get("detail") or "phase execution ledger needs review"),
                "brigade work phases doctor",
            )
        )
    latest_phase_session = (
        phase_health.get("latest_session") if isinstance(phase_health.get("latest_session"), dict) else None
    )
    if latest_phase_session and latest_phase_session.get("status") not in {"closed", "archived"}:
        items.append(
            _item(
                "phase-session",
                str(latest_phase_session.get("session_id") or "phase-session"),
                "warn",
                "active phase execution session needs review",
                "brigade work phases session next latest",
            )
        )
    latest_checkpoint = (
        phase_health.get("latest_session_checkpoint")
        if isinstance(phase_health.get("latest_session_checkpoint"), dict)
        else None
    )
    latest_checkpoint_compare = (
        phase_health.get("latest_session_checkpoint_compare")
        if isinstance(phase_health.get("latest_session_checkpoint_compare"), dict)
        else None
    )
    if latest_checkpoint and latest_checkpoint.get("status") == "blocked":
        checkpoint_id = str(latest_checkpoint.get("checkpoint_id") or "phase-session-checkpoint")
        items.append(
            _item(
                "phase-session-checkpoint",
                checkpoint_id,
                "blocked",
                str(latest_checkpoint.get("summary") or "phase session checkpoint is blocked"),
                f"brigade work phases session checkpoints show {checkpoint_id}",
                severity="high",
                updated_at=latest_checkpoint.get("created_at")
                if isinstance(latest_checkpoint.get("created_at"), str)
                else None,
                receipt_path=latest_checkpoint.get("path") if isinstance(latest_checkpoint.get("path"), str) else None,
            )
        )
    if latest_checkpoint_compare and int(latest_checkpoint_compare.get("issue_count") or 0) > 0:
        checkpoint_id = str(latest_checkpoint_compare.get("checkpoint_id") or "latest")
        top_checkpoint = (
            latest_checkpoint_compare.get("top_issue")
            if isinstance(latest_checkpoint_compare.get("top_issue"), dict)
            else {}
        )
        items.append(
            _item(
                "phase-session-checkpoint",
                checkpoint_id,
                "warn",
                str(top_checkpoint.get("detail") or "phase session checkpoint compare needs review"),
                str(
                    latest_checkpoint_compare.get("suggested_next_command")
                    or "brigade work phases session checkpoints compare latest"
                ),
                severity="medium",
            )
        )
    return items


def status_payload(target: Path) -> dict[str, Any]:
    from .. import daily_cmd

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
        "research_handoffs": research_cmd.health(target),
        "context": context_cmd.health(target),
        "release_readiness": release_cmd._latest_release_receipt(target),
        "release_candidate": release_cmd._latest_candidate(target),
        "repo_fleet": repos_cmd.health(target),
        "pantry": pantry_cmd.status_payload(target),
        "notifications": notifications_cmd.health(target),
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
        return emit(payload, json_output, [], 0)
    text_lines = [
        f"center status: {payload['target']}",
        f"pending_tasks: {payload['pending_task_count']}",
        f"pending_imports: {payload['pending_import_count']}",
        f"reviews: {payload['review_queue_count']}",
        f"actions: {payload['action_queue']['open_count']}",
        f"context_packs: {payload['context']['pack_count']}",
    ]
    pantry = payload.get("pantry") if isinstance(payload.get("pantry"), dict) else {}
    if pantry:
        text_lines.append(f"pantry: {pantry.get('summary')}")
    notifications = payload.get("notifications") if isinstance(payload.get("notifications"), dict) else {}
    if notifications:
        text_lines.append(f"notifications: {notifications.get('status')} configured={notifications.get('configured')}")
    phase_ledger = payload.get("phase_ledger") if isinstance(payload.get("phase_ledger"), dict) else {}
    if phase_ledger:
        text_lines.append(f"phase_records: {phase_ledger.get('record_count', 0)}")
        text_lines.append(f"phase_issues: {phase_ledger.get('issue_count', 0)}")
        text_lines.append(f"phase_actions: {phase_ledger.get('open_action_count', 0)}")
        latest_phase_session = (
            phase_ledger.get("latest_session") if isinstance(phase_ledger.get("latest_session"), dict) else None
        )
        if latest_phase_session:
            text_lines.append(
                f"phase_session: {latest_phase_session.get('session_id')} [{latest_phase_session.get('status')}]"
            )
    return emit(payload, json_output, text_lines, 0)


def schema(*, target: Path, json_output: bool = False) -> int:
    payload = _center_schema_manifest(target)
    if json_output:
        return emit(payload, json_output, [], 0)
    text_lines = [
        f"center schema manifest: {payload['target']}",
        f"schemas: {payload['schema_count']}",
        "read_only: true",
    ]
    for schema_item in payload["schemas"]:
        text_lines.append(f"- {schema_item['id']}: {schema_item['command']}")
    for check in payload["checks"]:
        text_lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    return emit(payload, json_output, text_lines, 0)


def activity(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    target = target.expanduser().resolve()
    items = _activity(target)[:limit]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-activity"),
        "target": str(target),
        "activity": items,
        "activity_count": len(items),
    }
    if json_output:
        return emit(payload, json_output, [], 0)
    text_lines = [f"center activity: {target}"]
    for item in items:
        text_lines.append(f"- {item['subsystem']} {item['id']} [{item['status']}] {item['safe_summary']}")
    return emit(payload, json_output, text_lines, 0)


def reviews(*, target: Path, json_output: bool = False, limit: int = 50) -> int:
    target = target.expanduser().resolve()
    items = _reviews(target)[:limit]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-reviews"),
        "target": str(target),
        "reviews": items,
        "review_count": len(items),
    }
    if json_output:
        return emit(payload, json_output, [], 0)
    text_lines = [f"center reviews: {target}"]
    for item in items:
        text_lines.append(f"- {item['subsystem']} {item['id']} [{item['status']}] {item['safe_summary']}")
        text_lines.append(f"  next: {item['suggested_next_command']}")
    return emit(payload, json_output, text_lines, 0)


def templates(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    items = [
        _item("context", "task", "available", "Task context pack template", "brigade context plan --kind task"),
        _item("context", "repo", "available", "Repo context pack template", "brigade context plan --kind repo"),
        _item(
            "context", "release", "available", "Release context pack template", "brigade context plan --kind release"
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
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "schema": _schema("center-templates"),
        "target": str(target),
        "templates": items,
        "template_count": len(items),
    }
    if json_output:
        return emit(payload, json_output, [], 0)
    text_lines = [f"center templates: {target}"]
    for item in items:
        text_lines.append(f"- {item['subsystem']}:{item['id']} {item['safe_summary']}")
    return emit(payload, json_output, text_lines, 0)
