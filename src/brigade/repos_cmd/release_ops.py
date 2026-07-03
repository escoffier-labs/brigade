# ruff: noqa: F401,F403,F405
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import actionqueue, config as brigade_config, reportstore, toml_compat as tomllib, work_cmd
from ..budgets import HANDOFF_BACKLOG_STALE_DAYS
from ..install import apply_gitignore
from ..localio import (
    read_json_dict as _read_json,
    read_jsonl_dicts as _read_jsonl,
    utc_now as _now,
    write_json as _write_json,
)
from ..render import emit
from ..selection import Selection, WRITER_INBOXES
from .constants import *
from .fleet import *
from .sweeps import *
from .actions_dispatch import *
from .release_train import *


def release_actions_archive_completed(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_release_actions(target)
    archived, remaining = actionqueue.split_archived_completed(actions, now=_now().isoformat())
    _write_release_actions(target, remaining)
    _append_release_action_archive(target, archived)
    payload = {
        "target_label": "repo-fleet",
        "archived_count": len(archived),
        "archive_path_label": ".brigade/repos/releases/actions-archive.jsonl",
        "archived_actions": archived,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release actions archive: completed")
    print(f"archived: {len(archived)}")
    return 0


def _release_evidence_path(target: Path) -> Path:
    return _release_trains_root(target) / "evidence.jsonl"


def _read_release_evidence(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_release_evidence_path(target))


def _write_release_evidence(target: Path, records: list[dict[str, Any]]) -> None:
    path = _release_evidence_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _release_waivers_path(target: Path) -> Path:
    return _release_trains_root(target) / "waivers.jsonl"


def _read_release_waivers(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_release_waivers_path(target))


def _write_release_waivers(target: Path, waivers: list[dict[str, Any]]) -> None:
    path = _release_waivers_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for waiver in waivers:
            handle.write(json.dumps(waiver, sort_keys=True) + "\n")


def _release_waiver_expired(waiver: dict[str, Any]) -> bool:
    expires_at = _parse_time(waiver.get("expires_at"))
    return bool(expires_at and expires_at < _now())


def _active_release_waivers(target: Path, train_id: str) -> list[dict[str, Any]]:
    return [
        waiver
        for waiver in _read_release_waivers(target)
        if waiver.get("train_id") == train_id
        and waiver.get("status") == "active"
        and not _release_waiver_expired(waiver)
    ]


def _release_waiver_scope_names(waivers: list[dict[str, Any]]) -> set[str]:
    return {str(waiver.get("scope") or "") for waiver in waivers if waiver.get("scope") in RELEASE_WAIVER_SCOPES}


def _find_release_waiver(
    target: Path, waiver_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    waivers = _read_release_waivers(target)
    matches = [waiver for waiver in waivers if str(waiver.get("waiver_id") or "").startswith(waiver_id)]
    if not matches:
        return waivers, None, f"fleet release waiver not found: {waiver_id}"
    if len(matches) > 1:
        return waivers, None, f"fleet release waiver id is ambiguous: {waiver_id}"
    return waivers, matches[0], None


def release_waiver_record(
    *,
    target: Path,
    train_id: str = "latest",
    scope: str,
    reason: str,
    repo_id: str | None = None,
    expires_at: str | None = None,
    owner_label: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if scope not in RELEASE_WAIVER_SCOPES:
        print(f"error: --scope must be one of {', '.join(sorted(RELEASE_WAIVER_SCOPES))}", file=sys.stderr)
        return 2
    if not reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    if expires_at and _parse_time(expires_at) is None:
        print("error: --expires-at must be an ISO timestamp", file=sys.stderr)
        return 2
    train_repos = train.get("repos") if isinstance(train.get("repos"), list) else []
    repos = [repo for repo in train_repos if isinstance(repo, dict)]
    if repo_id and not any(repo.get("repo_id") == repo_id for repo in repos):
        print(f"error: repo is not in fleet release train: {repo_id}", file=sys.stderr)
        return 2
    now = _now().isoformat()
    train_id_value = str(train.get("train_id") or "")
    waiver_id = f"train-waiver-{_fingerprint_payload({'train_id': train_id_value, 'repo_id': repo_id or 'all', 'scope': scope})[:16]}"
    waivers = _read_release_waivers(target)
    waiver = {
        "waiver_id": waiver_id,
        "train_id": train_id_value,
        "train_fingerprint": train.get("train_fingerprint"),
        "repo_id": repo_id,
        "scope": scope,
        "status": "active",
        "reason": _safe_text(reason),
        "owner_label": _safe_text(owner_label or ""),
        "expires_at": expires_at,
        "created_at": now,
        "updated_at": now,
        "source_fingerprint": _fingerprint_payload(
            {
                "train_id": train_id_value,
                "repo_id": repo_id or "all",
                "scope": scope,
                "reason": reason,
                "expires_at": expires_at,
                "owner_label": owner_label or "",
            }
        ),
    }
    replaced = False
    for index, existing in enumerate(waivers):
        if existing.get("waiver_id") == waiver_id:
            waiver["created_at"] = existing.get("created_at") or now
            waivers[index] = waiver
            replaced = True
            break
    if not replaced:
        waivers.append(waiver)
    _write_release_waivers(target, waivers)
    payload = {"target_label": "repo-fleet", "created": not replaced, "updated": replaced, "waiver": waiver}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release waiver: {waiver_id}")
    print(f"scope: {scope}")
    return 0


def release_waiver_list(
    *, target: Path, train_id: str | None = None, limit: int = 50, json_output: bool = False
) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    waivers = _read_release_waivers(target)
    if train_id:
        if train_id == "latest":
            latest = latest_release_train(target)
            train_id = str(latest.get("train_id")) if isinstance(latest, dict) else train_id
        waivers = [waiver for waiver in waivers if str(waiver.get("train_id") or "").startswith(train_id or "")]
    waivers.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("waiver_id") or ""),
        reverse=True,
    )
    payload = {
        "target_label": "repo-fleet",
        "waivers_path_label": ".brigade/repos/releases/waivers.jsonl",
        "waivers": waivers[:limit],
        "waiver_count": len(waivers),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release waivers")
    for waiver in waivers[:limit]:
        print(f"- {waiver.get('waiver_id')} {waiver.get('scope')} [{waiver.get('status')}]")
    return 0


def release_waiver_show(*, target: Path, waiver_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _, waiver, error = _find_release_waiver(target, waiver_id)
    if waiver is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "waiver": waiver}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release waiver: {waiver.get('waiver_id')}")
    print(f"scope: {waiver.get('scope')}")
    print(f"status: {waiver.get('status')}")
    return 0


def release_waiver_revoke(*, target: Path, waiver_id: str, reason: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    waivers, waiver, error = _find_release_waiver(target, waiver_id)
    if waiver is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    now = _now().isoformat()
    waiver["status"] = "revoked"
    waiver["revoked_at"] = now
    waiver["updated_at"] = now
    waiver["revoke_reason"] = _safe_text(reason)
    _write_release_waivers(target, waivers)
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "waiver": waiver}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release waiver revoked: {waiver.get('waiver_id')}")
    return 0


def release_waiver_renew(
    *,
    target: Path,
    waiver_id: str,
    reason: str,
    expires_at: str | None = None,
    owner_label: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if not reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    if expires_at and _parse_time(expires_at) is None:
        print("error: --expires-at must be an ISO timestamp", file=sys.stderr)
        return 2
    waivers, waiver, error = _find_release_waiver(target, waiver_id)
    if waiver is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train, _ = _resolve_release_train(target, str(waiver.get("train_id") or ""))
    now = _now().isoformat()
    waiver["status"] = "active"
    waiver["reason"] = _safe_text(reason)
    waiver["expires_at"] = expires_at
    if owner_label is not None:
        waiver["owner_label"] = _safe_text(owner_label)
    waiver["renewed_at"] = now
    waiver["updated_at"] = now
    if isinstance(train, dict):
        waiver["train_fingerprint"] = train.get("train_fingerprint")
    waiver["source_fingerprint"] = _fingerprint_payload(
        {
            "train_id": waiver.get("train_id"),
            "repo_id": waiver.get("repo_id") or "all",
            "scope": waiver.get("scope"),
            "reason": reason,
            "expires_at": expires_at,
            "owner_label": waiver.get("owner_label") or "",
        }
    )
    _write_release_waivers(target, waivers)
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "waiver": waiver}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release waiver renewed: {waiver.get('waiver_id')}")
    return 0


def _release_waiver_health_payload(target: Path, train_id: str | None = None) -> dict[str, Any]:
    target = target.expanduser().resolve()
    selected_train_id = train_id
    if train_id == "latest":
        latest = latest_release_train(target)
        selected_train_id = str(latest.get("train_id")) if isinstance(latest, dict) else train_id
    waivers = _read_release_waivers(target)
    if selected_train_id:
        waivers = [waiver for waiver in waivers if str(waiver.get("train_id") or "").startswith(selected_train_id)]
    issues: list[dict[str, Any]] = []
    now = _now()
    for waiver in waivers:
        waiver_id = str(waiver.get("waiver_id") or "")
        status = str(waiver.get("status") or "")
        if status != "active":
            continue
        scope = str(waiver.get("scope") or "")
        train, _ = _resolve_release_train(target, str(waiver.get("train_id") or ""))
        repos = train.get("repos") if isinstance(train, dict) and isinstance(train.get("repos"), list) else []
        repo_ids = {str(repo.get("repo_id") or "") for repo in repos if isinstance(repo, dict)}
        if scope not in RELEASE_WAIVER_SCOPES:
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_invalid_scope",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} has an invalid waiver scope",
                    "suggested_next_command": f'brigade repos release waivers revoke {waiver_id} --reason "invalid scope"',
                }
            )
        repo_id = str(waiver.get("repo_id") or "")
        if repo_id and repo_ids and repo_id not in repo_ids:
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_repo_missing",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "repo_id": repo_id,
                    "detail": f"{waiver_id} references a repo outside the train",
                    "suggested_next_command": f'brigade repos release waivers revoke {waiver_id} --reason "repo no longer in train"',
                }
            )
        reason = str(waiver.get("reason") or "").strip()
        if len(reason) < RELEASE_WAIVER_REASON_MIN_LENGTH:
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_reason_too_short",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} reason is too short for review",
                    "suggested_next_command": f'brigade repos release waivers renew {waiver_id} --reason "reviewed with current train context"',
                }
            )
        if reason.lower() in RELEASE_WAIVER_GENERIC_REASONS:
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_reason_generic",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} reason is too generic",
                    "suggested_next_command": f'brigade repos release waivers renew {waiver_id} --reason "reviewed with current train context"',
                }
            )
        if not str(waiver.get("owner_label") or "").strip():
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_missing_owner",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} has no review owner label",
                    "suggested_next_command": f'brigade repos release waivers renew {waiver_id} --reason "reviewed with current train context" --owner-label <label>',
                }
            )
        if _release_waiver_expired(waiver):
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_expired",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} is expired",
                    "suggested_next_command": f'brigade repos release waivers renew {waiver_id} --reason "reviewed again"',
                }
            )
            continue
        if not waiver.get("expires_at"):
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_missing_expiry",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} has no expiry",
                    "suggested_next_command": f'brigade repos release waivers renew {waiver_id} --reason "set expiry" --expires-at <timestamp>',
                }
            )
        created = _parse_time(waiver.get("renewed_at") or waiver.get("created_at"))
        if created and (now - created).total_seconds() / 3600 > RELEASE_WAIVER_STALE_HOURS:
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_stale_review",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} has not been reviewed recently",
                    "suggested_next_command": f'brigade repos release waivers renew {waiver_id} --reason "reviewed again"',
                }
            )
        if (
            isinstance(train, dict)
            and waiver.get("train_fingerprint")
            and train.get("train_fingerprint")
            and waiver.get("train_fingerprint") != train.get("train_fingerprint")
        ):
            issues.append(
                {
                    "status": WARN,
                    "name": "release_waiver_train_changed",
                    "waiver_id": waiver_id,
                    "train_id": waiver.get("train_id"),
                    "scope": waiver.get("scope"),
                    "detail": f"{waiver_id} references an older train fingerprint",
                    "suggested_next_command": f'brigade repos release waivers renew {waiver_id} --reason "train reviewed again"',
                }
            )
    return {
        "target_label": "repo-fleet",
        "waivers_path_label": ".brigade/repos/releases/waivers.jsonl",
        "train_id": selected_train_id,
        "waiver_count": len(waivers),
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def _release_waiver_templates_payload() -> dict[str, Any]:
    templates = []
    for scope in sorted(RELEASE_WAIVER_SCOPES):
        templates.append(
            {
                "scope": scope,
                "requires_owner_label": True,
                "requires_expiry": True,
                "recommended_expiry_hours": RELEASE_WAIVER_STALE_HOURS,
                "reason_hint": "Describe the reviewed risk, current train context, and why manual publish may proceed.",
                "suggested_command": f'brigade repos release waivers record latest --scope {scope} --reason "reviewed risk and mitigation" --expires-at <timestamp> --owner-label <label>',
            }
        )
    return {
        "target_label": "repo-fleet",
        "template_count": len(templates),
        "templates": templates,
        "policy": {
            "reason_min_length": RELEASE_WAIVER_REASON_MIN_LENGTH,
            "generic_reasons": sorted(RELEASE_WAIVER_GENERIC_REASONS),
            "stale_review_hours": RELEASE_WAIVER_STALE_HOURS,
        },
    }


def release_waiver_templates(*, json_output: bool = False) -> int:
    payload = _release_waiver_templates_payload()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release waiver templates")
    for template in payload["templates"]:
        print(f"- {template['scope']}: owner and expiry required")
    return 0


def release_waiver_doctor(*, target: Path, train_id: str | None = None, json_output: bool = False) -> int:
    payload = _release_waiver_health_payload(target, train_id)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release waiver doctor")
    print(f"issues: {payload['issue_count']}")
    for issue in payload["issues"]:
        print(f"[{issue.get('status')}] {issue.get('name')}: {issue.get('detail')}")
    return 0


def _release_waiver_import_records(health: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue in health.get("issues") if isinstance(health.get("issues"), list) else []:
        if not isinstance(issue, dict):
            continue
        waiver_id = str(issue.get("waiver_id") or "unknown")
        fingerprint = _fingerprint_payload(
            {
                "waiver_id": waiver_id,
                "name": issue.get("name"),
                "scope": issue.get("scope"),
                "train_id": issue.get("train_id"),
            }
        )
        records.append(
            {
                "text": f"Review fleet release waiver {waiver_id}: {issue.get('name')}",
                "kind": "task",
                "source": "repo-fleet-release-waiver",
                "type": "docs",
                "priority": "high"
                if issue.get("name")
                in {
                    "release_waiver_expired",
                    "release_waiver_train_changed",
                    "release_waiver_invalid_scope",
                    "release_waiver_repo_missing",
                }
                else "normal",
                "template": "docs",
                "acceptance": [
                    "The release waiver is renewed with current review context, owner label, and expiry or revoked.",
                    "The fleet release ready gate and audit output reflect the current waiver state.",
                    "No verification, publish, tag, push, or release command is executed by Brigade.",
                ],
                "metadata": {
                    "train_id": issue.get("train_id"),
                    "waiver_id": waiver_id,
                    "scope": issue.get("scope"),
                    "issue_type": issue.get("name"),
                    "safe_summary": issue.get("detail"),
                    "source_item_key": f"{issue.get('train_id')}:{waiver_id}:{issue.get('name')}",
                    "source_fingerprint": fingerprint,
                },
            }
        )
    return records


def release_waiver_import_issues(
    *, target: Path, train_id: str | None = None, dry_run: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    health = _release_waiver_health_payload(target, train_id)
    records = _release_waiver_import_records(health)
    imported, skipped, dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target_label": "repo-fleet",
        "train_id": health.get("train_id"),
        "dry_run": dry_run,
        "issue_count": len(records),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release waiver imports")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    return 0


def _evidence_for_train(records: list[dict[str, Any]], train_id: str) -> list[dict[str, Any]]:
    return [record for record in records if record.get("train_id") == train_id]


def release_evidence_plan(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    records = _evidence_for_train(_read_release_evidence(target), str(train.get("train_id") or ""))
    by_repo_step = {(record.get("repo_id"), record.get("step")): record for record in records}
    planned: list[dict[str, Any]] = []
    for repo in train.get("repos") if isinstance(train.get("repos"), list) else []:
        if not isinstance(repo, dict):
            continue
        repo_id = str(repo.get("repo_id") or "unknown")
        for step in ("verification", "release-doctor", "candidate-compare", "tag", "push", "release"):
            existing = by_repo_step.get((repo_id, step))
            planned.append(
                {
                    "repo_id": repo_id,
                    "repo_label": repo.get("repo_label"),
                    "step": step,
                    "status": existing.get("status") if isinstance(existing, dict) else "missing",
                    "suggested_record_command": f"brigade repos release evidence record {train.get('train_id')} --repo {repo_id} --step {step} --status completed",
                }
            )
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "records_path_label": ".brigade/repos/releases/evidence.jsonl",
        "planned": planned,
        "planned_count": len(planned),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release evidence plan: {train.get('train_id')}")
    print(f"records: {len(records)}")
    for item in planned[:20]:
        print(f"- {item.get('repo_id')} {item.get('step')} [{item.get('status')}]")
    return 0


def release_evidence_record(
    *,
    target: Path,
    train_id: str = "latest",
    repo_id: str,
    step: str,
    status: str,
    summary: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if step not in RELEASE_EVIDENCE_STEPS:
        print(f"error: --step must be one of {', '.join(sorted(RELEASE_EVIDENCE_STEPS))}", file=sys.stderr)
        return 2
    if status not in RELEASE_EVIDENCE_STATUSES:
        print(f"error: --status must be one of {', '.join(sorted(RELEASE_EVIDENCE_STATUSES))}", file=sys.stderr)
        return 2
    train_repos = train.get("repos") if isinstance(train.get("repos"), list) else []
    repos = [repo for repo in train_repos if isinstance(repo, dict)]
    repo = next((item for item in repos if item.get("repo_id") == repo_id), None)
    if repo is None:
        print(f"error: repo is not in fleet release train: {repo_id}", file=sys.stderr)
        return 2
    now = _now().isoformat()
    records = _read_release_evidence(target)
    train_id_value = str(train.get("train_id") or "")
    record_id = f"train-ev-{_fingerprint_payload({'train': train_id_value, 'repo_id': repo_id, 'step': step})[:16]}"
    record = {
        "evidence_id": record_id,
        "train_id": train_id_value,
        "train_fingerprint": train.get("train_fingerprint"),
        "repo_id": repo_id,
        "repo_label": repo.get("repo_label"),
        "step": step,
        "status": status,
        "safe_summary": _safe_text(summary or f"{step} marked {status} for {repo_id}"),
        "recorded_at": now,
        "source_fingerprint": _fingerprint_payload(
            {"train_id": train_id_value, "repo_id": repo_id, "step": step, "status": status, "summary": summary or ""}
        ),
    }
    replaced = False
    for index, existing in enumerate(records):
        if existing.get("evidence_id") == record_id:
            record["created_at"] = existing.get("created_at") or existing.get("recorded_at") or now
            records[index] = record
            replaced = True
            break
    if not replaced:
        record["created_at"] = now
        records.append(record)
    _write_release_evidence(target, records)
    payload = {"target_label": "repo-fleet", "created": not replaced, "updated": replaced, "record": record}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release evidence: {record_id}")
    print(f"status: {status}")
    return 0


def release_evidence_list(
    *, target: Path, train_id: str | None = None, limit: int = 50, json_output: bool = False
) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    records = _read_release_evidence(target)
    if train_id:
        if train_id == "latest":
            latest = latest_release_train(target)
            train_id = str(latest.get("train_id")) if isinstance(latest, dict) else train_id
        records = [record for record in records if str(record.get("train_id") or "").startswith(train_id or "")]
    records.sort(key=lambda item: str(item.get("recorded_at") or item.get("evidence_id") or ""), reverse=True)
    payload = {
        "target_label": "repo-fleet",
        "records_path_label": ".brigade/repos/releases/evidence.jsonl",
        "records": records[:limit],
        "record_count": len(records),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release evidence records")
    for record in records[:limit]:
        print(f"- {record.get('evidence_id')} {record.get('repo_id')} {record.get('step')} [{record.get('status')}]")
    return 0


def release_evidence_show(*, target: Path, evidence_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    matches = [
        record
        for record in _read_release_evidence(target)
        if str(record.get("evidence_id") or "").startswith(evidence_id)
    ]
    if not matches:
        print(f"error: fleet release evidence not found: {evidence_id}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"error: fleet release evidence id is ambiguous: {evidence_id}", file=sys.stderr)
        return 2
    record = matches[0]
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "record": record}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release evidence: {record.get('evidence_id')}")
    print(f"repo: {record.get('repo_id')} {record.get('repo_label')}")
    print(f"step: {record.get('step')}")
    print(f"status: {record.get('status')}")
    return 0


def _release_records_by_repo_step(
    records: list[dict[str, Any]], train_id: str
) -> dict[tuple[str, str], dict[str, Any]]:
    by_step: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("train_id") != train_id:
            continue
        repo_id = str(record.get("repo_id") or "")
        step = str(record.get("step") or "")
        if repo_id and step:
            by_step[(repo_id, step)] = record
    return by_step


def _reconcile_release_action(
    action: dict[str, Any], records_by_step: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    repo_id = str(action.get("repo_id") or "")
    evidence: list[dict[str, Any]] = []
    missing_steps: list[str] = []
    blocked_steps: list[str] = []
    for step in REQUIRED_RELEASE_EVIDENCE_STEPS:
        record = records_by_step.get((repo_id, step))
        if not isinstance(record, dict):
            missing_steps.append(step)
            continue
        status = str(record.get("status") or "")
        evidence.append({"evidence_id": record.get("evidence_id"), "step": step, "status": status})
        if status == "blocked":
            blocked_steps.append(step)
    if blocked_steps:
        resolution = "blocked-evidence"
    elif missing_steps:
        resolution = "missing-evidence"
    else:
        resolution = "evidence-complete"
    now = _now().isoformat()
    action["resolution_status"] = resolution
    action["manual_evidence"] = evidence
    action["missing_evidence_steps"] = missing_steps
    action["blocked_evidence_steps"] = blocked_steps
    action["reconciled_at"] = now
    action["updated_at"] = now
    if resolution == "evidence-complete":
        action["status"] = "done"
        action.setdefault("completed_at", now)
    elif action.get("status") == "done":
        action["status"] = "active"
    return {
        "release_action_id": action.get("release_action_id"),
        "repo_id": repo_id,
        "status": action.get("status"),
        "resolution_status": resolution,
        "missing_evidence_steps": missing_steps,
        "blocked_evidence_steps": blocked_steps,
        "manual_evidence": evidence,
    }


def release_reconcile(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_id_value = str(train.get("train_id") or "")
    actions = _read_release_actions(target)
    selected = [action for action in actions if action.get("source_train_id") == train_id_value]
    records_by_step = _release_records_by_repo_step(_read_release_evidence(target), train_id_value)
    results = [_reconcile_release_action(action, records_by_step) for action in selected]
    _write_release_actions(target, actions)
    payload = {
        "target_label": "repo-fleet",
        "train_id": train_id_value,
        "result_count": len(results),
        "results": results,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release reconcile: {train_id_value}")
    for result in results:
        print(f"- {result.get('release_action_id')} {result.get('repo_id')} [{result.get('resolution_status')}]")
    return 0


def _repo_release_summary(
    repo: dict[str, Any], train_id: str, records_by_step: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    repo_id = str(repo.get("repo_id") or "unknown")
    steps: list[dict[str, Any]] = []
    missing: list[str] = []
    blocked: list[str] = []
    deferred: list[str] = []
    skipped: list[str] = []
    completed: list[str] = []
    for step in REQUIRED_RELEASE_EVIDENCE_STEPS:
        record = records_by_step.get((repo_id, step))
        if not isinstance(record, dict):
            missing.append(step)
            steps.append({"step": step, "status": "missing", "evidence_id": None})
            continue
        status = str(record.get("status") or "missing")
        steps.append({"step": step, "status": status, "evidence_id": record.get("evidence_id")})
        if status == "blocked":
            blocked.append(step)
        elif status == "deferred":
            deferred.append(step)
        elif status == "skipped":
            skipped.append(step)
        elif status == "completed":
            completed.append(step)
    if blocked:
        evidence_status = "blocked-evidence"
    elif missing:
        evidence_status = "missing-evidence"
    elif deferred:
        evidence_status = "deferred"
    elif skipped and not completed:
        evidence_status = "skipped"
    else:
        evidence_status = "manually-completed"
    return {
        "repo_id": repo_id,
        "repo_label": repo.get("repo_label"),
        "classification": repo.get("classification"),
        "evidence_status": evidence_status,
        "steps": steps,
        "missing_evidence_steps": missing,
        "blocked_evidence_steps": blocked,
        "deferred_evidence_steps": deferred,
        "skipped_evidence_steps": skipped,
        "completed_evidence_steps": completed,
        "suggested_next_command": f"brigade repos release evidence plan {train_id}",
    }


def _release_summary_payload(target: Path, train: dict[str, Any]) -> dict[str, Any]:
    train_id = str(train.get("train_id") or "")
    records_by_step = _release_records_by_repo_step(_read_release_evidence(target), train_id)
    actions = [action for action in _read_release_actions(target) if action.get("source_train_id") == train_id]
    train_repos = train.get("repos") if isinstance(train.get("repos"), list) else []
    repos = [_repo_release_summary(repo, train_id, records_by_step) for repo in train_repos if isinstance(repo, dict)]
    counts: dict[str, int] = {}
    for repo in repos:
        status = str(repo.get("evidence_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    unresolved_actions = [action for action in actions if action.get("status") in {"pending", "active", "deferred"}]
    blocked_evidence = [repo for repo in repos if repo.get("evidence_status") == "blocked-evidence"]
    missing_evidence = [repo for repo in repos if repo.get("evidence_status") == "missing-evidence"]
    return {
        "target_label": "repo-fleet",
        "train_id": train_id,
        "generated_at": _now().isoformat(),
        "repo_count": len(repos),
        "repos": repos,
        "counts": counts,
        "ready_count": sum(
            1 for repo in train.get("repos", []) if isinstance(repo, dict) and repo.get("classification") == "ready"
        ),
        "blocked_count": len(blocked_evidence),
        "missing_evidence_count": len(missing_evidence),
        "unresolved_action_count": len(unresolved_actions),
        "unresolved_actions": [
            {
                "release_action_id": action.get("release_action_id"),
                "repo_id": action.get("repo_id"),
                "status": action.get("status"),
                "resolution_status": action.get("resolution_status"),
            }
            for action in unresolved_actions
        ],
        "suggested_next_commands": [
            "brigade repos release reconcile latest",
            "brigade repos release evidence plan latest",
        ],
        "summary_fingerprint": _fingerprint_payload({"train": train_id, "repos": repos, "actions": unresolved_actions}),
    }


def release_summary(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = _release_summary_payload(target, train)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release summary: {payload['train_id']}")
    print(f"repos: {payload['repo_count']}")
    print(f"unresolved_actions: {payload['unresolved_action_count']}")
    for repo in payload["repos"]:
        print(f"- {repo.get('repo_id')} [{repo.get('evidence_status')}]")
    return 0


def _release_report_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Fleet Release Train Review Report",
        "",
        f"- Train: `{summary.get('train_id')}`",
        f"- Generated: {summary.get('generated_at')}",
        f"- Repos: {summary.get('repo_count')}",
        f"- Unresolved actions: {summary.get('unresolved_action_count')}",
        f"- Missing evidence: {summary.get('missing_evidence_count')}",
        f"- Blocked evidence: {summary.get('blocked_count')}",
        "",
        "## Repo Evidence",
        "",
    ]
    repos = summary.get("repos") if isinstance(summary.get("repos"), list) else []
    for repo in repos:
        lines.append(f"- `{repo.get('repo_id')}` {repo.get('repo_label')} - {repo.get('evidence_status')}")
        missing = repo.get("missing_evidence_steps") if isinstance(repo.get("missing_evidence_steps"), list) else []
        blocked = repo.get("blocked_evidence_steps") if isinstance(repo.get("blocked_evidence_steps"), list) else []
        if missing:
            lines.append(f"  - missing: {', '.join(str(step) for step in missing)}")
        if blocked:
            lines.append(f"  - blocked: {', '.join(str(step) for step in blocked)}")
    if not repos:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- local report only",
            "- no verification, tag, push, release, upload, or remote mutation",
        ]
    )
    return "\n".join(lines) + "\n"


def release_report(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_dir = _release_trains_root(target) / str(train.get("train_id") or "")
    if not train_dir.is_dir():
        print(
            f"error: fleet release train path is missing: {train.get('path_label') or train.get('train_id')}",
            file=sys.stderr,
        )
        return 2
    summary = _release_summary_payload(target, train)
    report = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "generated_at": summary.get("generated_at"),
        "summary": summary,
        "report_fingerprint": _fingerprint_payload(summary),
        "bundle_files": ["RELEASE_TRAIN_REPORT.md", "RELEASE_TRAIN_REPORT.json"],
    }
    _write_json(train_dir / "RELEASE_TRAIN_REPORT.json", report)
    (train_dir / "RELEASE_TRAIN_REPORT.md").write_text(_release_report_markdown(summary))
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "path_label": str(train.get("train_id") or ""),
        "report": report,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release report: {train.get('train_id')}")
    print("path_label: RELEASE_TRAIN_REPORT.md")
    return 0


def _waivers_for_repo(waivers: list[dict[str, Any]], repo_id: str) -> list[dict[str, Any]]:
    return [
        {
            "waiver_id": waiver.get("waiver_id"),
            "scope": waiver.get("scope"),
            "status": waiver.get("status"),
            "repo_id": waiver.get("repo_id"),
            "expires_at": waiver.get("expires_at"),
            "owner_label": waiver.get("owner_label"),
            "reason": waiver.get("reason"),
        }
        for waiver in waivers
        if waiver.get("repo_id") in {None, "", repo_id}
    ]


def _release_matrix_payload(target: Path, train: dict[str, Any]) -> dict[str, Any]:
    train_id = str(train.get("train_id") or "")
    summary = _release_summary_payload(target, train)
    actions = [action for action in _read_release_actions(target) if action.get("source_train_id") == train_id]
    waivers = _active_release_waivers(target, train_id)
    rows: list[dict[str, Any]] = []
    summary_by_repo = {repo.get("repo_id"): repo for repo in summary.get("repos") if isinstance(repo, dict)}
    for repo in train.get("repos") if isinstance(train.get("repos"), list) else []:
        if not isinstance(repo, dict):
            continue
        repo_id = str(repo.get("repo_id") or "")
        repo_summary = summary_by_repo.get(repo_id, {})
        repo_actions = [action for action in actions if action.get("repo_id") == repo_id]
        unresolved_actions = [
            action for action in repo_actions if action.get("status") in {"pending", "active", "deferred"}
        ]
        repo_waivers = _waivers_for_repo(waivers, repo_id)
        waived_scopes = _release_waiver_scope_names(repo_waivers)
        blockers: list[str] = []
        if repo.get("classification") == "blocked" and "blocked-repo" not in waived_scopes:
            blockers.append("blocked-repo")
        if unresolved_actions and "unresolved-action" not in waived_scopes:
            blockers.append("unresolved-action")
        if repo_summary.get("missing_evidence_steps") and "missing-evidence" not in waived_scopes:
            blockers.append("missing-evidence")
        if repo_summary.get("blocked_evidence_steps") and "blocked-evidence" not in waived_scopes:
            blockers.append("blocked-evidence")
        rows.append(
            {
                "repo_id": repo_id,
                "repo_label": repo.get("repo_label"),
                "classification": repo.get("classification"),
                "evidence_status": repo_summary.get("evidence_status"),
                "evidence_steps": repo_summary.get("steps") if isinstance(repo_summary.get("steps"), list) else [],
                "unresolved_action_count": len(unresolved_actions),
                "unresolved_actions": [
                    {
                        "release_action_id": action.get("release_action_id"),
                        "status": action.get("status"),
                        "resolution_status": action.get("resolution_status"),
                    }
                    for action in unresolved_actions
                ],
                "active_waivers": repo_waivers,
                "waived_scopes": sorted(waived_scopes),
                "blockers": blockers,
                "ready": not blockers,
                "suggested_next_command": repo_summary.get("suggested_next_command")
                or repo.get("suggested_next_command"),
            }
        )
    blocker_rows = [row for row in rows if row.get("blockers")]
    payload = {
        "target_label": "repo-fleet",
        "train_id": train_id,
        "generated_at": _now().isoformat(),
        "repo_count": len(rows),
        "rows": rows,
        "ready_count": sum(1 for row in rows if row.get("ready")),
        "blocked_count": len(blocker_rows),
        "waiver_count": len(waivers),
        "evidence_steps": list(REQUIRED_RELEASE_EVIDENCE_STEPS),
        "summary": {
            key: summary.get(key)
            for key in ("counts", "missing_evidence_count", "blocked_count", "unresolved_action_count")
        },
        "matrix_fingerprint": _fingerprint_payload({"train_id": train_id, "rows": rows, "waivers": waivers}),
        "suggested_next_commands": ["brigade repos release ready latest", "brigade repos release checklist latest"],
    }
    return payload


def _release_matrix_markdown(matrix: dict[str, Any]) -> str:
    lines = [
        "# Fleet Release Matrix",
        "",
        f"- Train: `{matrix.get('train_id')}`",
        f"- Generated: {matrix.get('generated_at')}",
        f"- Repos: {matrix.get('repo_count')}",
        f"- Ready: {matrix.get('ready_count')}",
        f"- Blocked: {matrix.get('blocked_count')}",
        f"- Active waivers: {matrix.get('waiver_count')}",
        "",
        "| Repo | Classification | Evidence | Actions | Waivers | Ready |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in matrix.get("rows") if isinstance(matrix.get("rows"), list) else []:
        waivers = ", ".join(str(scope) for scope in row.get("waived_scopes") or []) or "none"
        lines.append(
            f"| `{row.get('repo_id')}` | {row.get('classification')} | {row.get('evidence_status')} | {row.get('unresolved_action_count')} | {waivers} | {str(bool(row.get('ready'))).lower()} |"
        )
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- local matrix only",
            "- no verification, tag, push, release, upload, or remote mutation",
        ]
    )
    return "\n".join(lines) + "\n"


def release_matrix(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_dir = _release_trains_root(target) / str(train.get("train_id") or "")
    if not train_dir.is_dir():
        print(
            f"error: fleet release train path is missing: {train.get('path_label') or train.get('train_id')}",
            file=sys.stderr,
        )
        return 2
    matrix = _release_matrix_payload(target, train)
    report = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "generated_at": matrix.get("generated_at"),
        "matrix": matrix,
        "matrix_fingerprint": matrix.get("matrix_fingerprint"),
        "bundle_files": ["RELEASE_TRAIN_MATRIX.md", "RELEASE_TRAIN_MATRIX.json"],
    }
    _write_json(train_dir / "RELEASE_TRAIN_MATRIX.json", report)
    (train_dir / "RELEASE_TRAIN_MATRIX.md").write_text(_release_matrix_markdown(matrix))
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "path_label": "RELEASE_TRAIN_MATRIX.md",
        "report": report,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release matrix: {train.get('train_id')}")
    print("path_label: RELEASE_TRAIN_MATRIX.md")
    return 0


def _release_import_records(summary: dict[str, Any]) -> list[dict[str, Any]]:
    train_id = str(summary.get("train_id") or "latest")
    records: list[dict[str, Any]] = []
    for repo in summary.get("repos") if isinstance(summary.get("repos"), list) else []:
        if not isinstance(repo, dict):
            continue
        repo_id = str(repo.get("repo_id") or "unknown")
        status = str(repo.get("evidence_status") or "")
        if status not in {"missing-evidence", "blocked-evidence"}:
            continue
        fingerprint = _fingerprint_payload(
            {
                "train_id": train_id,
                "repo_id": repo_id,
                "status": status,
                "missing": repo.get("missing_evidence_steps"),
                "blocked": repo.get("blocked_evidence_steps"),
            }
        )
        records.append(
            {
                "text": f"Resolve fleet release train evidence for {repo_id}: {status}",
                "kind": "task",
                "source": "repo-fleet-release",
                "type": "docs",
                "priority": "high" if status == "blocked-evidence" else "normal",
                "template": "docs",
                "acceptance": [
                    "Required manual release evidence is recorded or explicitly deferred.",
                    "No remote publish, tag, push, or release action is performed by Brigade.",
                    "Fleet release train summary no longer reports the same unresolved evidence.",
                ],
                "metadata": {
                    "train_id": train_id,
                    "repo_id": repo_id,
                    "issue_type": status,
                    "safe_summary": f"{repo_id} release train evidence is {status}",
                    "missing_evidence_steps": repo.get("missing_evidence_steps"),
                    "blocked_evidence_steps": repo.get("blocked_evidence_steps"),
                    "source_item_key": f"{train_id}:{repo_id}:{status}",
                    "source_fingerprint": fingerprint,
                },
            }
        )
    return records


def release_import_issues(
    *, target: Path, train_id: str = "latest", dry_run: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    summary = _release_summary_payload(target, train)
    records = _release_import_records(summary)
    imported, skipped, dismissed = work_cmd._append_import_records(target, records, dry_run=dry_run)
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "dry_run": dry_run,
        "issue_count": len(records),
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release imports: {train.get('train_id')}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    return 0


def release_checklist(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    summary = _release_summary_payload(target, train)
    items = [
        {
            "repo_id": repo.get("repo_id"),
            "repo_label": repo.get("repo_label"),
            "step": step.get("step"),
            "status": step.get("status"),
            "evidence_id": step.get("evidence_id"),
            "suggested_next_command": f"brigade repos release evidence record {train.get('train_id')} --repo {repo.get('repo_id')} --step {step.get('step')} --status completed",
        }
        for repo in summary.get("repos", [])
        if isinstance(repo, dict)
        for step in repo.get("steps", [])
        if isinstance(step, dict)
    ]
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "items": items,
        "item_count": len(items),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release checklist: {train.get('train_id')}")
    for item in items:
        print(f"- {item.get('repo_id')} {item.get('step')} [{item.get('status')}]")
    return 0


def release_hygiene(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    trains = _release_trains(target)
    issues: list[dict[str, Any]] = []
    for train in trains:
        train_id = str(train.get("train_id") or "")
        closeout = train.get("closeout") if isinstance(train.get("closeout"), dict) else None
        if not closeout:
            issues.append(
                {
                    "status": WARN,
                    "name": "fleet_release_train_unclosed",
                    "train_id": train_id,
                    "detail": f"{train_id} has no closeout",
                    "suggested_next_command": f"brigade repos release closeout {train_id}",
                }
            )
        report_path = _release_trains_root(target) / train_id / "RELEASE_TRAIN_REPORT.json"
        if not report_path.is_file():
            issues.append(
                {
                    "status": WARN,
                    "name": "fleet_release_report_missing",
                    "train_id": train_id,
                    "detail": f"{train_id} has no review report",
                    "suggested_next_command": f"brigade repos release report {train_id}",
                }
            )
        created = _parse_time(train.get("created_at") or train.get("generated_at"))
        if created and (_now() - created).total_seconds() / 3600 > RELEASE_TRAIN_STALE_HOURS:
            issues.append(
                {
                    "status": WARN,
                    "name": "fleet_release_train_stale",
                    "train_id": train_id,
                    "detail": f"{train_id} is stale",
                    "suggested_next_command": "brigade repos release build",
                }
            )
    payload = {
        "target_label": "repo-fleet",
        "train_count": len(trains),
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release hygiene")
    print(f"issues: {len(issues)}")
    for issue in issues[:20]:
        print(f"[{issue.get('status')}] {issue.get('name')}: {issue.get('detail')}")
    return 0


def _release_train_dir(target: Path, train: dict[str, Any]) -> Path:
    train_id = str(train.get("train_id") or "")
    path = _release_trains_root(target) / train_id
    if path.is_dir():
        return path
    return _release_trains_archive_root(target) / train_id


def _release_bundle_file_entry(train_dir: Path, name: str) -> dict[str, Any]:
    path = train_dir / name
    entry: dict[str, Any] = {"path_label": name, "exists": path.is_file()}
    if path.is_file() and name != "RELEASE_TRAIN_MANIFEST.json":
        try:
            entry["fingerprint"] = _fingerprint_payload(path.read_text())
        except OSError:
            entry["fingerprint"] = None
    return entry


def release_manifest(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_dir = _release_train_dir(target, train)
    if not train_dir.is_dir():
        print(
            f"error: fleet release train path is missing: {train.get('path_label') or train.get('train_id')}",
            file=sys.stderr,
        )
        return 2
    files = [_release_bundle_file_entry(train_dir, name) for name in RELEASE_BUNDLE_FILES]
    manifest = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "generated_at": _now().isoformat(),
        "bundle_path_label": str(train.get("path_label") or train.get("train_id")),
        "files": files,
        "file_count": len(files),
        "missing_count": len([item for item in files if not item.get("exists")]),
    }
    manifest["manifest_fingerprint"] = _fingerprint_payload({"train_id": manifest["train_id"], "files": files})
    _write_json(train_dir / "RELEASE_TRAIN_MANIFEST.json", manifest)
    files = [_release_bundle_file_entry(train_dir, name) for name in RELEASE_BUNDLE_FILES]
    manifest["files"] = files
    manifest["missing_count"] = len([item for item in files if not item.get("exists")])
    manifest["manifest_fingerprint"] = _fingerprint_payload({"train_id": manifest["train_id"], "files": files})
    _write_json(train_dir / "RELEASE_TRAIN_MANIFEST.json", manifest)
    payload = {"target_label": "repo-fleet", "train_id": train.get("train_id"), "manifest": manifest}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release manifest: {train.get('train_id')}")
    print(f"missing: {manifest['missing_count']}")
    return 0


def _release_audit_payload(target: Path, train: dict[str, Any]) -> dict[str, Any]:
    train_id = str(train.get("train_id") or "")
    train_dir = _release_train_dir(target, train)
    summary = _release_summary_payload(target, train)
    issues: list[dict[str, Any]] = []
    if not train_dir.is_dir():
        issues.append(
            {
                "status": WARN,
                "name": "release_train_bundle_missing",
                "detail": f"{train_id} bundle path is missing",
                "suggested_next_command": "brigade repos release build",
            }
        )
    else:
        for name in RELEASE_BUNDLE_FILES:
            if not (train_dir / name).is_file():
                command = "brigade repos release manifest"
                if name.startswith("RELEASE_TRAIN_REPORT"):
                    command = f"brigade repos release report {train_id}"
                elif name == "CLOSEOUT.json":
                    command = f"brigade repos release closeout {train_id}"
                issues.append(
                    {
                        "status": WARN,
                        "name": "release_train_bundle_file_missing",
                        "path_label": name,
                        "detail": f"{name} is missing",
                        "suggested_next_command": command,
                    }
                )
        manifest = _read_json(train_dir / "RELEASE_TRAIN_MANIFEST.json")
        if isinstance(manifest, dict):
            expected = {
                item["path_label"]: item
                for item in [_release_bundle_file_entry(train_dir, name) for name in RELEASE_BUNDLE_FILES]
            }
            stored_files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
            for stored in stored_files:
                if not isinstance(stored, dict):
                    continue
                current = expected.get(str(stored.get("path_label") or ""))
                if (
                    current
                    and stored.get("fingerprint")
                    and current.get("fingerprint")
                    and stored.get("fingerprint") != current.get("fingerprint")
                ):
                    issues.append(
                        {
                            "status": WARN,
                            "name": "release_train_manifest_stale",
                            "path_label": stored.get("path_label"),
                            "detail": f"{stored.get('path_label')} changed after manifest build",
                            "suggested_next_command": f"brigade repos release manifest {train_id}",
                        }
                    )
                    break
    open_actions = [
        action
        for action in _read_release_actions(target)
        if action.get("source_train_id") == train_id and action.get("status") in {"pending", "active", "deferred"}
    ]
    if open_actions:
        issues.append(
            {
                "status": WARN,
                "name": "release_train_open_actions",
                "detail": f"{len(open_actions)} release action(s) remain open",
                "suggested_next_command": "brigade repos release actions list --target .",
            }
        )
    if int(summary.get("missing_evidence_count") or 0) > 0:
        issues.append(
            {
                "status": WARN,
                "name": "release_train_missing_evidence",
                "detail": f"{summary.get('missing_evidence_count')} repo(s) have missing evidence",
                "suggested_next_command": f"brigade repos release evidence plan {train_id}",
            }
        )
    if int(summary.get("blocked_count") or 0) > 0:
        issues.append(
            {
                "status": WARN,
                "name": "release_train_blocked_evidence",
                "detail": f"{summary.get('blocked_count')} repo(s) have blocked evidence",
                "suggested_next_command": f"brigade repos release evidence plan {train_id}",
            }
        )
    if int(train.get("blocker_count") or 0) > 0:
        issues.append(
            {
                "status": WARN,
                "name": "release_train_blocked_repos",
                "detail": f"{train.get('blocker_count')} repo blocker(s) remain",
                "suggested_next_command": f"brigade repos release show {train_id}",
            }
        )
    waiver_health = _release_waiver_health_payload(target, train_id)
    for issue in waiver_health.get("issues") if isinstance(waiver_health.get("issues"), list) else []:
        if isinstance(issue, dict):
            issues.append(dict(issue))
    return {
        "target_label": "repo-fleet",
        "train_id": train_id,
        "generated_at": _now().isoformat(),
        "issue_count": len(issues),
        "issues": issues,
        "waiver_issue_count": waiver_health.get("issue_count"),
        "summary": {
            key: summary.get(key)
            for key in ("repo_count", "counts", "unresolved_action_count", "missing_evidence_count", "blocked_count")
        },
    }


def release_audit(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = _release_audit_payload(target, train)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release audit: {train.get('train_id')}")
    print(f"issues: {payload['issue_count']}")
    for issue in payload["issues"]:
        print(f"[{issue.get('status')}] {issue.get('name')}: {issue.get('detail')}")
    return 0


def release_activity(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_id_value = str(train.get("train_id") or "")
    train_dir = _release_train_dir(target, train)
    events: list[dict[str, Any]] = [
        {
            "subsystem": "repo-fleet-release",
            "event_type": "train",
            "local_id": train_id_value,
            "status": train.get("status"),
            "created_at": train.get("created_at") or train.get("generated_at"),
            "safe_summary": f"fleet release train {train.get('status')}",
            "suggested_next_command": f"brigade repos release show {train_id_value}",
        }
    ]
    closeout = train.get("closeout") if isinstance(train.get("closeout"), dict) else None
    if closeout:
        events.append(
            {
                "subsystem": "repo-fleet-release",
                "event_type": "closeout",
                "local_id": train_id_value,
                "status": closeout.get("status"),
                "created_at": closeout.get("reviewed_at"),
                "safe_summary": str(closeout.get("reason") or "release train closeout"),
                "suggested_next_command": f"brigade repos release closeout {train_id_value}",
            }
        )
    for action in _read_release_actions(target):
        if action.get("source_train_id") == train_id_value:
            events.append(
                {
                    "subsystem": "repo-fleet-release",
                    "event_type": "action",
                    "local_id": action.get("release_action_id"),
                    "repo_id": action.get("repo_id"),
                    "status": action.get("status"),
                    "created_at": action.get("updated_at") or action.get("created_at"),
                    "safe_summary": action.get("safe_summary"),
                    "suggested_next_command": f"brigade repos release actions show {action.get('release_action_id')}",
                }
            )
    for record in _evidence_for_train(_read_release_evidence(target), train_id_value):
        events.append(
            {
                "subsystem": "repo-fleet-release",
                "event_type": "evidence",
                "local_id": record.get("evidence_id"),
                "repo_id": record.get("repo_id"),
                "status": record.get("status"),
                "created_at": record.get("recorded_at") or record.get("created_at"),
                "safe_summary": record.get("safe_summary"),
                "suggested_next_command": f"brigade repos release evidence show {record.get('evidence_id')}",
            }
        )
    for waiver in [item for item in _read_release_waivers(target) if item.get("train_id") == train_id_value]:
        events.append(
            {
                "subsystem": "repo-fleet-release",
                "event_type": "waiver",
                "local_id": waiver.get("waiver_id"),
                "repo_id": waiver.get("repo_id"),
                "status": waiver.get("status"),
                "created_at": waiver.get("updated_at") or waiver.get("created_at"),
                "safe_summary": waiver.get("reason"),
                "suggested_next_command": f"brigade repos release waivers show {waiver.get('waiver_id')}",
            }
        )
    if train_dir.is_dir():
        for name, event_type, command in (
            ("RELEASE_TRAIN_REPORT.json", "report", f"brigade repos release report {train_id_value}"),
            ("RELEASE_TRAIN_MANIFEST.json", "manifest", f"brigade repos release manifest {train_id_value}"),
        ):
            path = train_dir / name
            if path.is_file():
                events.append(
                    {
                        "subsystem": "repo-fleet-release",
                        "event_type": event_type,
                        "local_id": train_id_value,
                        "status": "present",
                        "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                        "safe_summary": f"{name} present",
                        "suggested_next_command": command,
                    }
                )
    events.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    payload = {"target_label": "repo-fleet", "train_id": train_id_value, "events": events, "event_count": len(events)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release activity: {train_id_value}")
    for event in events:
        print(f"- {event.get('event_type')} {event.get('local_id')} [{event.get('status')}]")
    return 0


def release_ready(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    summary = _release_summary_payload(target, train)
    blockers: list[str] = []
    waived: list[dict[str, Any]] = []
    active_waivers = _active_release_waivers(target, str(train.get("train_id") or ""))
    waiver_health = _release_waiver_health_payload(target, str(train.get("train_id") or ""))
    waived_scopes = _release_waiver_scope_names(active_waivers)
    checks = [
        ("blocked-repo", int(train.get("blocker_count") or 0), "train has blocked repos"),
        ("unresolved-action", int(summary.get("unresolved_action_count") or 0), "train has unresolved actions"),
        ("missing-evidence", int(summary.get("missing_evidence_count") or 0), "train has missing manual evidence"),
        ("blocked-evidence", int(summary.get("blocked_count") or 0), "train has blocked manual evidence"),
    ]
    for scope, count, message in checks:
        if count <= 0:
            continue
        if scope in waived_scopes:
            waiver = next((item for item in active_waivers if item.get("scope") == scope), {})
            waived.append(
                {
                    "scope": scope,
                    "count": count,
                    "reason": waiver.get("reason"),
                    "owner_label": waiver.get("owner_label"),
                    "expires_at": waiver.get("expires_at"),
                    "waiver_id": waiver.get("waiver_id"),
                }
            )
        else:
            blockers.append(message)
    ready = not blockers
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "ready": ready,
        "blockers": blockers,
        "waived": waived,
        "waiver_count": len(active_waivers),
        "waiver_issues": waiver_health.get("issues"),
        "waiver_issue_count": waiver_health.get("issue_count"),
        "waiver_policy": {
            "reason_min_length": RELEASE_WAIVER_REASON_MIN_LENGTH,
            "requires_owner_label": True,
            "requires_expiry": True,
            "template_command": "brigade repos release waivers templates",
        },
        "summary": {
            key: summary.get(key)
            for key in ("repo_count", "counts", "unresolved_action_count", "missing_evidence_count", "blocked_count")
        },
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if ready else 1
    print(f"repo fleet release ready: {train.get('train_id')}")
    print(f"ready: {str(ready).lower()}")
    for blocker in blockers:
        print(f"- {blocker}")
    return 0 if ready else 1


def release_train_actions_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    actions = _read_release_actions(target)
    open_actions = [action for action in actions if action.get("status") in {"pending", "active", "deferred"}]
    open_actions.sort(key=_release_action_rank)
    checks: list[dict[str, Any]] = []
    if open_actions:
        top = open_actions[0]
        checks.append(
            {
                "status": WARN,
                "name": "repo_fleet_release_actions_open",
                "detail": f"{len(open_actions)} open fleet release action(s)",
                "suggested_next_command": f"brigade repos release actions show {top.get('release_action_id')}",
            }
        )
    unreconciled = [
        action
        for action in actions
        if action.get("status") in {"pending", "active", "deferred"} and not action.get("reconciled_at")
    ]
    if unreconciled:
        top = unreconciled[0]
        checks.append(
            {
                "status": WARN,
                "name": "repo_fleet_release_action_unreconciled",
                "detail": f"{len(unreconciled)} fleet release action(s) need reconciliation",
                "suggested_next_command": f"brigade repos release reconcile {top.get('source_train_id') or 'latest'}",
            }
        )
    missing = [action for action in actions if action.get("resolution_status") == "missing-evidence"]
    if missing:
        top = missing[0]
        checks.append(
            {
                "status": WARN,
                "name": "repo_fleet_release_evidence_missing",
                "detail": f"{len(missing)} fleet release action(s) are missing manual evidence",
                "suggested_next_command": f"brigade repos release evidence plan {top.get('source_train_id') or 'latest'}",
            }
        )
    return {
        "actions_path_label": ".brigade/repos/releases/actions.json",
        "action_count": len(actions),
        "open_count": len(open_actions),
        "top_action": open_actions[0] if open_actions else None,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def release_train_evidence_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    records = _read_release_evidence(target)
    blocked = [record for record in records if record.get("status") == "blocked"]
    checks: list[dict[str, Any]] = []
    if blocked:
        top = blocked[0]
        checks.append(
            {
                "status": WARN,
                "name": "repo_fleet_release_evidence_blocked",
                "detail": f"{len(blocked)} blocked fleet release evidence record(s)",
                "suggested_next_command": f"brigade repos release evidence show {top.get('evidence_id')}",
            }
        )
    return {
        "records_path_label": ".brigade/repos/releases/evidence.jsonl",
        "record_count": len(records),
        "blocked_count": len(blocked),
        "latest": records[-1] if records else None,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


__all__ = tuple(name for name in globals() if not name.startswith("__"))
