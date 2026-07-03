# ruff: noqa: F401
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
from . import actions_dispatch, constants, fleet, sweeps


def _release_trains_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "releases"


def _release_trains_archive_root(target: Path) -> Path:
    return _release_trains_root(target) / "archive"


def _train_json_path(path: Path) -> Path:
    return reportstore.bundle_json_path(path, "FLEET_RELEASE_EVIDENCE.json")


def _read_train(path: Path) -> dict[str, Any] | None:
    payload = _read_json(_train_json_path(path))
    if payload is not None:
        payload.pop("path", None)
        payload.setdefault("path_label", _train_json_path(path).parent.name)
    return payload


def _dict_or_empty(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _release_trains(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_release_trains_root(target)]
    if include_archived:
        roots.append(_release_trains_archive_root(target))
    return reportstore.list_bundles(roots, _read_train, id_field="train_id", skip_child=lambda name: name == "archive")


def latest_release_train(target: Path) -> dict[str, Any] | None:
    trains = _release_trains(target)
    return trains[0] if trains else None


def _resolve_release_train(target: Path, train_id: str) -> tuple[dict[str, Any] | None, str | None]:
    trains = [] if train_id == "latest" else _release_trains(target, include_archived=True)
    return reportstore.resolve_bundle(
        trains, train_id, id_field="train_id", label="fleet release train", latest=lambda: latest_release_train(target)
    )


def _repo_git_labels(repo: Path) -> dict[str, Any]:
    tracked_dirty, _ = fleet._dirty_counts(repo) if repo.is_dir() else (0, 0)
    upstream = (
        fleet._git_value(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") if repo.is_dir() else None
    )
    ahead = behind = None
    if upstream:
        counts = fleet._git_value(repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                try:
                    ahead, behind = int(parts[0]), int(parts[1])
                except ValueError:
                    ahead = behind = None
    return {
        "branch": fleet._git_value(repo, "rev-parse", "--abbrev-ref", "HEAD") if repo.is_dir() else None,
        "head_label": fleet._git_value(repo, "rev-parse", "--short", "HEAD") if repo.is_dir() else None,
        "upstream_label": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty_tracked_count": tracked_dirty,
    }


def _fleet_actions_for_repo(target: Path, repo_id: str) -> list[dict[str, Any]]:
    return [action for action in actions_dispatch._read_actions(target) if action.get("repo_id") == repo_id]


def _fleet_imports_for_repo(repo: Path) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for item in work_cmd._read_imports(repo):
        metadata = _dict_or_empty(item.get("metadata"))
        if item.get("source") == "repo-fleet" or metadata.get("fleet_action_id"):
            imports.append(item)
    return imports


def _safe_import_ref(item: dict[str, Any]) -> dict[str, Any]:
    metadata = _dict_or_empty(item.get("metadata"))
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "kind": item.get("kind"),
        "source": item.get("source"),
        "fleet_action_id": metadata.get("fleet_action_id"),
        "source_fingerprint": metadata.get("source_fingerprint"),
    }


def _safe_train_action_ref(action: dict[str, Any]) -> dict[str, Any]:
    dispatch = _dict_or_empty(action.get("dispatch"))
    return {
        "fleet_action_id": action.get("fleet_action_id"),
        "status": action.get("status"),
        "resolution_status": action.get("resolution_status"),
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "source_report_id": action.get("source_report_id"),
        "source_subsystem": action.get("source_subsystem"),
        "source_local_id": action.get("source_local_id"),
        "source_fingerprint": action.get("source_fingerprint"),
        "target_import_id": action.get("target_import_id") or dispatch.get("target_import_id"),
        "target_task_id": action.get("target_task_id"),
        "safe_summary": fleet._safe_text(action.get("safe_summary")),
    }


def _latest_review_closeout_ref(repo: Path, repo_id: str, label: str) -> dict[str, Any] | None:
    try:
        from .. import release_cmd

        closeout = release_cmd._latest_review_closeout(repo)
    except Exception:
        closeout = None
    return fleet._safe_report_ref(closeout, repo_id, label)


def _latest_security_closeout_ref(repo: Path, repo_id: str, label: str) -> dict[str, Any] | None:
    return fleet._safe_report_ref(
        fleet._latest_json_payload(repo / ".brigade" / "security" / "closeouts", "closeout.json"), repo_id, label
    )


def _latest_verification_ref(repo: Path, repo_id: str, label: str) -> dict[str, Any] | None:
    receipt = work_cmd._latest_verify_receipt(repo)
    return fleet._safe_report_ref(receipt, repo_id, label)


def _classify_release_repo(state: dict[str, Any], actions: list[dict[str, Any]], imports: list[dict[str, Any]]) -> str:
    if not state.get("exists"):
        return "blocked"
    if any(action.get("status") == "deferred" for action in actions):
        return "deferred"
    if any(action.get("resolution_status") in {"broken-reference", "stale"} for action in actions):
        return "blocked"
    if any(action.get("status") in {"pending", "active"} and not action.get("dispatch") for action in actions):
        return "needs-dispatch"
    if any(action.get("resolution_status") in {"dispatched", "in-progress"} for action in actions):
        return "in-progress"
    if any(item.get("status") == "pending" for item in imports):
        return "in-progress"
    if int(state.get("dirty_tracked_count") or 0) > 0 or int(state.get("security_issue_count") or 0) > 0:
        return "blocked"
    if state.get("latest_operator_report") is None:
        return "stale-evidence"
    if state.get("latest_release_readiness") is None:
        return "needs-review"
    if state.get("latest_release_candidate") is None:
        return "no-release-candidate"
    candidate = _dict_or_empty(state.get("latest_release_candidate"))
    readiness = _dict_or_empty(state.get("latest_release_readiness"))
    if readiness.get("status") in {"blocked", "failed"}:
        return "blocked"
    if candidate.get("status") not in {"ready", "reviewed"}:
        return "needs-review"
    return "ready"


def _release_repo_payload(target: Path, entry: constants.RepoEntry) -> dict[str, Any]:
    repo = entry.path
    state = fleet._repo_brigade_state(entry)
    actions = _fleet_actions_for_repo(target, entry.repo_id)
    imports = _fleet_imports_for_repo(repo) if repo.is_dir() else []
    latest_sweep = sweeps._safe_sweep_ref(sweeps._latest_sweep_for_repo(target, entry.repo_id))
    fleet_report = sweeps.latest_report(target)
    classification = _classify_release_repo(state, actions, imports)
    verification = _latest_verification_ref(repo, entry.repo_id, entry.label) if repo.is_dir() else None
    review_closeout = _latest_review_closeout_ref(repo, entry.repo_id, entry.label) if repo.is_dir() else None
    security_closeout = _latest_security_closeout_ref(repo, entry.repo_id, entry.label) if repo.is_dir() else None
    pending_fleet_imports = [_safe_import_ref(item) for item in imports if item.get("status") == "pending"]
    warnings = _list_or_empty(state.get("warnings"))
    blockers = _list_or_empty(state.get("blockers"))
    evidence = {
        "latest_fleet_sweep": latest_sweep,
        "latest_fleet_report": fleet._safe_report_ref(fleet_report, entry.repo_id, entry.label),
        "fleet_actions": [_safe_train_action_ref(action) for action in actions],
        "pending_fleet_imports": pending_fleet_imports,
        "latest_operator_report": state.get("latest_operator_report"),
        "latest_work_closeout": state.get("latest_work_closeout"),
        "latest_verification": verification,
        "latest_review_closeout": review_closeout,
        "latest_security_closeout": security_closeout,
        "latest_release_readiness": state.get("latest_release_readiness"),
        "latest_release_candidate": state.get("latest_release_candidate"),
    }
    return {
        "repo_id": entry.repo_id,
        "repo_label": entry.label,
        "enabled": entry.enabled,
        "exists": state.get("exists"),
        "classification": classification,
        "git": _repo_git_labels(repo),
        "dirty_tracked_count": state.get("dirty_tracked_count"),
        "action_count": len(actions),
        "open_action_count": len(
            [action for action in actions if action.get("status") in {"pending", "active", "deferred"}]
        ),
        "pending_fleet_import_count": len(pending_fleet_imports),
        "warning_count": len(warnings),
        "blocker_count": len(blockers),
        "evidence": evidence,
        "suggested_next_command": _release_repo_next_command(entry.repo_id, classification, actions),
        "source_fingerprint": fleet._fingerprint_payload(
            {
                "repo_id": entry.repo_id,
                "classification": classification,
                "evidence": evidence,
                "git": _repo_git_labels(repo),
            }
        ),
    }


def _release_repo_next_command(repo_id: str, classification: str, actions: list[dict[str, Any]]) -> str:
    if classification == "needs-dispatch":
        action = next(
            (item for item in actions if item.get("status") in {"pending", "active"} and not item.get("dispatch")), None
        )
        if action:
            return f"brigade repos actions dispatch plan {action.get('fleet_action_id')}"
    if classification in {"in-progress", "blocked", "stale-evidence"}:
        action = next((item for item in actions if item.get("status") in {"pending", "active", "deferred"}), None)
        if action:
            return f"brigade repos actions reconcile {action.get('fleet_action_id')}"
    if classification == "no-release-candidate":
        return "brigade release candidate plan"
    if classification == "needs-review":
        return "brigade release doctor"
    if classification == "deferred":
        return "brigade repos actions list --target ."
    return f"brigade repos show {repo_id}"


def _release_train_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    entries, errors, config_loaded = fleet._load_config(target)
    repos = [_release_repo_payload(target, entry) for entry in entries if entry.enabled]
    counts: dict[str, int] = {}
    for repo in repos:
        counts[str(repo.get("classification"))] = counts.get(str(repo.get("classification")), 0) + 1
    blockers = [repo for repo in repos if repo.get("classification") in {"blocked"}]
    warnings = [
        repo
        for repo in repos
        if repo.get("classification")
        in {"needs-review", "needs-dispatch", "in-progress", "stale-evidence", "no-release-candidate", "deferred"}
    ]
    payload = {
        "schema_version": 1,
        "target_label": "repo-fleet",
        "config_loaded": config_loaded,
        "config_errors": [fleet._safe_text(error, target, "repo-fleet", "repo fleet") for error in errors],
        "generated_at": _now().isoformat(),
        "repo_count": len(repos),
        "classification_counts": counts,
        "repos": repos,
        "blocker_count": len(blockers) + len(errors),
        "warning_count": len(warnings),
        "blockers": [
            {
                "repo_id": repo.get("repo_id"),
                "classification": repo.get("classification"),
                "detail": f"{repo.get('repo_id')} is blocked",
            }
            for repo in blockers
        ],
        "warnings": [
            {
                "repo_id": repo.get("repo_id"),
                "classification": repo.get("classification"),
                "detail": f"{repo.get('repo_id')} is {repo.get('classification')}",
            }
            for repo in warnings
        ],
        "suggested_next_commands": [
            repo.get("suggested_next_command") for repo in repos if repo.get("suggested_next_command")
        ],
    }
    payload["train_fingerprint"] = fleet._fingerprint_payload(
        {"repos": repos, "counts": counts, "errors": payload["config_errors"]}
    )
    return payload


def _release_train_markdown(train: dict[str, Any]) -> str:
    lines = [
        "# Fleet Release Train",
        "",
        f"- Train: `{train.get('train_id', 'planned')}`",
        f"- Generated: {train.get('generated_at')}",
        f"- Repos: {train.get('repo_count')}",
        f"- Blockers: {train.get('blocker_count')}",
        f"- Warnings: {train.get('warning_count')}",
        "",
        "## Repo Status",
        "",
    ]
    repos = _list_or_empty(train.get("repos"))
    for repo in repos:
        lines.append(f"- `{repo.get('repo_id')}` {repo.get('repo_label')} - {repo.get('classification')}")
        if repo.get("suggested_next_command"):
            lines.append(f"  - next: `{repo.get('suggested_next_command')}`")
    if not repos:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Boundaries",
            "",
            "- local release train only",
            "- no push, tags, releases, uploads, or remote mutation",
            "- manual publish steps only",
        ]
    )
    return "\n".join(lines) + "\n"


def _release_train_publish_plan(train: dict[str, Any]) -> str:
    lines = ["# Manual Fleet Publish Plan", ""]
    repos = _list_or_empty(train.get("repos"))
    for repo in repos:
        repo_id = repo.get("repo_id")
        lines.extend(
            [
                f"## {repo_id}",
                "",
                f"- Classification: {repo.get('classification')}",
                "- Verify: run the repo's configured verification command label manually.",
                "- Doctor: `brigade release doctor`",
                "- Candidate compare: `brigade release candidate compare latest`",
                "- Manual-only remote steps:",
                "  - create or update tag manually after review",
                "  - push manually after review",
                "  - create release manually after review",
                "",
            ]
        )
    if not repos:
        lines.append("- No repos in train.")
    return "\n".join(lines)


def _write_release_train_bundle(train_dir: Path, train: dict[str, Any]) -> None:
    reportstore.write_bundle(
        train_dir,
        train,
        evidence_name="FLEET_RELEASE_EVIDENCE.json",
        documents={
            "FLEET_RELEASE_TRAIN.md": _release_train_markdown(train),
            "MANUAL_PUBLISH_PLAN.md": _release_train_publish_plan(train),
        },
    )


def release_plan(*, target: Path, json_output: bool = False) -> int:
    payload = _release_train_payload(target)
    payload.update({"train_id": "planned", "status": "planned", "release_train_root_label": ".brigade/repos/releases"})
    rc = 0 if payload["config_loaded"] else 1
    text_lines = [
        "repo fleet release plan",
        f"repos: {payload['repo_count']}",
        f"blockers: {payload['blocker_count']}",
        *[f"- {repo.get('repo_id')} [{repo.get('classification')}]" for repo in payload["repos"]],
    ]
    return emit(payload, json_output, text_lines, rc)


def release_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    created = _now()
    train_id = f"{created.strftime('%Y%m%d-%H%M%S')}-fleet-release-{uuid4().hex[:6]}"
    train_dir = _release_trains_root(target) / train_id
    payload = _release_train_payload(target)
    payload.update(
        {
            "train_id": train_id,
            "status": "blocked" if payload["blocker_count"] else "ready",
            "created_at": created.isoformat(),
            "path_label": train_id,
        }
    )
    _write_release_train_bundle(train_dir, payload)
    text_lines = [f"repo fleet release train: {train_id}", f"status: {payload['status']}", f"path_label: {train_id}"]
    return emit(payload, json_output, text_lines, 0)


def release_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    trains = _release_trains(target)[:limit]
    payload = {
        "target_label": "repo-fleet",
        "release_train_root_label": ".brigade/repos/releases",
        "trains": trains,
        "train_count": len(trains),
    }
    text_lines = [
        "repo fleet release trains",
        *[
            f"- {train.get('train_id')} [{train.get('status')}] repos={train.get('repo_count')} {train.get('created_at')}"
            for train in trains
        ],
    ]
    return emit(payload, json_output, text_lines, 0)


def release_show(*, target: Path, train_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload = {"target_label": "repo-fleet", "train": train}
    text_lines = [
        f"repo fleet release train: {train.get('train_id')}",
        f"status: {train.get('status')}",
        f"repos: {train.get('repo_count')}",
        f"path_label: {train.get('path_label')}",
    ]
    return emit(payload, json_output, text_lines, 0)


def release_compare(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    current = _release_train_payload(target)
    issues: list[dict[str, Any]] = []
    old_by_repo = {repo.get("repo_id"): repo for repo in _list_or_empty(train.get("repos")) if isinstance(repo, dict)}
    current_by_repo = {repo.get("repo_id"): repo for repo in current.get("repos", []) if isinstance(repo, dict)}
    for repo_id, old in old_by_repo.items():
        new = current_by_repo.get(repo_id)
        if new is None:
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "train_repo_missing",
                    "repo_id": repo_id,
                    "detail": f"{repo_id} is no longer in release train",
                }
            )
            continue
        old_git = _dict_or_empty(old.get("git"))
        new_git = _dict_or_empty(new.get("git"))
        if (
            old_git.get("head_label")
            and new_git.get("head_label")
            and old_git.get("head_label") != new_git.get("head_label")
        ):
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "train_repo_head_changed",
                    "repo_id": repo_id,
                    "detail": f"{repo_id} HEAD changed",
                }
            )
        old_evidence = _dict_or_empty(old.get("evidence"))
        new_evidence = _dict_or_empty(new.get("evidence"))
        for key, name in (
            ("latest_release_readiness", "newer_release_readiness"),
            ("latest_release_candidate", "newer_release_candidate"),
        ):
            old_id = _dict_or_empty(old_evidence.get(key)).get("id")
            new_id = _dict_or_empty(new_evidence.get(key)).get("id")
            if old_id and not new_id:
                issues.append(
                    {
                        "status": constants.WARN,
                        "name": "train_missing_receipt",
                        "repo_id": repo_id,
                        "detail": f"{repo_id} missing {key}",
                    }
                )
            elif old_id and new_id and old_id != new_id:
                issues.append(
                    {"status": constants.WARN, "name": name, "repo_id": repo_id, "detail": f"{repo_id} has newer {key}"}
                )
        old_actions = _list_or_empty(old_evidence.get("fleet_actions"))
        new_actions = _list_or_empty(new_evidence.get("fleet_actions"))
        if fleet._fingerprint_payload(old_actions) != fleet._fingerprint_payload(new_actions):
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "train_fleet_actions_changed",
                    "repo_id": repo_id,
                    "detail": f"{repo_id} fleet action reconciliation changed",
                }
            )
        if old.get("source_fingerprint") != new.get("source_fingerprint"):
            issues.append(
                {
                    "status": constants.WARN,
                    "name": "train_unresolved_state_changed",
                    "repo_id": repo_id,
                    "detail": f"{repo_id} unresolved release state changed",
                }
            )
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "issue_count": len(issues),
        "issues": issues,
        "suggested_next_commands": [
            "brigade repos release build",
            f"brigade repos release closeout {train.get('train_id')} --status superseded",
        ],
    }
    text_lines = [
        f"repo fleet release compare: {train.get('train_id')}",
        f"issues: {len(issues)}",
        *[f"[{issue.get('status')}] {issue.get('name')}: {issue.get('detail')}" for issue in issues],
    ]
    return emit(payload, json_output, text_lines, 0)


def release_closeout(
    *,
    target: Path,
    train_id: str = "latest",
    status: str = "reviewed",
    reason: str | None = None,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    if status not in reportstore.CLOSEOUT_STATUSES:
        print("error: --status must be one of reviewed, deferred, superseded, archived", file=sys.stderr)
        return 2
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_path = _release_trains_root(target) / str(train.get("train_id") or "")
    if not train_path.is_dir():
        print(
            f"error: fleet release train path is missing: {train.get('path_label') or train.get('train_id')}",
            file=sys.stderr,
        )
        return 2
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "status": status,
        "reason": reason or f"fleet release train marked {status}",
        "reviewed_at": _now().isoformat(),
        "train_fingerprint": train.get("train_fingerprint"),
        "blocker_count": train.get("blocker_count"),
        "warning_count": train.get("warning_count"),
    }
    from . import release_ops

    payload["summary"] = {
        key: value
        for key, value in release_ops._release_summary_payload(target, train).items()
        if key
        in {
            "counts",
            "repo_count",
            "ready_count",
            "blocked_count",
            "missing_evidence_count",
            "unresolved_action_count",
            "summary_fingerprint",
        }
    }
    _write_json(train_path / "CLOSEOUT.json", payload)
    train["closeout"] = payload
    train["status"] = status
    _write_json(train_path / "FLEET_RELEASE_EVIDENCE.json", train)
    text_lines = [f"repo fleet release closeout: {train.get('train_id')}", f"status: {status}"]
    return emit(payload, json_output, text_lines, 0)


def release_archive(*, target: Path, train_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    source = _release_trains_root(target) / str(train.get("train_id") or "")
    if not source.is_dir() or source.parent == _release_trains_archive_root(target):
        print(f"error: fleet release train cannot be archived: {train.get('train_id')}", file=sys.stderr)
        return 2
    _, moved = reportstore.move_bundle(source, _release_trains_archive_root(target))
    if not moved:
        print(f"error: archived fleet release train already exists: {train.get('train_id')}", file=sys.stderr)
        return 2
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "status": "archived",
        "archive_path_label": train.get("train_id"),
    }
    text_lines = [f"archived repo fleet release train: {train.get('train_id')}"]
    return emit(payload, json_output, text_lines, 0)


def _release_actions_path(target: Path) -> Path:
    return _release_trains_root(target) / "actions.json"


def _release_actions_archive_path(target: Path) -> Path:
    return _release_trains_root(target) / "actions-archive.jsonl"


def _read_release_actions(target: Path) -> list[dict[str, Any]]:
    return actionqueue.read_actions(_release_actions_path(target))


def _write_release_actions(target: Path, actions: list[dict[str, Any]]) -> None:
    _write_json(_release_actions_path(target), {"updated_at": _now().isoformat(), "actions": actions})


def _read_release_action_archive(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_release_actions_archive_path(target))


def _append_release_action_archive(target: Path, actions: list[dict[str, Any]]) -> None:
    actionqueue.append_archive(_release_actions_archive_path(target), actions)


def _release_action_rank(action: dict[str, Any]) -> tuple[int, int, str]:
    status_rank = {"active": 0, "pending": 1, "deferred": 2, "done": 3, "archived": 4}.get(
        str(action.get("status") or ""), 5
    )
    class_rank = {
        "blocked": 0,
        "needs-dispatch": 1,
        "in-progress": 2,
        "needs-review": 3,
        "stale-evidence": 4,
        "no-release-candidate": 5,
        "deferred": 6,
    }.get(str(action.get("classification") or ""), 7)
    return (status_rank, class_rank, str(action.get("release_action_id") or ""))


def _train_closeout_status(train: dict[str, Any]) -> str | None:
    closeout = _dict_or_empty(train.get("closeout")) or None
    status = closeout.get("status") if isinstance(closeout, dict) else None
    return status if isinstance(status, str) else None


def _planned_release_actions(train: dict[str, Any]) -> list[dict[str, Any]]:
    train_id = str(train.get("train_id") or "planned")
    train_fingerprint = str(train.get("train_fingerprint") or fleet._fingerprint_payload(train))
    closeout = _dict_or_empty(train.get("closeout"))
    reviewed_at = closeout.get("reviewed_at") if isinstance(closeout, dict) else None
    created = _now().isoformat()
    actions: list[dict[str, Any]] = []
    for repo in _list_or_empty(train.get("repos")):
        if not isinstance(repo, dict):
            continue
        classification = str(repo.get("classification") or "")
        if classification == "ready":
            continue
        repo_id = str(repo.get("repo_id") or "unknown")
        repo_label = str(repo.get("repo_label") or repo_id)
        repo_fp = str(repo.get("source_fingerprint") or fleet._fingerprint_payload(repo))
        source_fingerprint = fleet._fingerprint_payload(
            {
                "train_id": train_id,
                "train_fingerprint": train_fingerprint,
                "repo_id": repo_id,
                "classification": classification,
                "repo_fingerprint": repo_fp,
            }
        )
        actions.append(
            {
                "release_action_id": f"train-act-{source_fingerprint[:16]}",
                "source_train_id": train_id,
                "source_train_fingerprint": train_fingerprint,
                "repo_id": repo_id,
                "repo_label": repo_label,
                "classification": classification,
                "status": "pending",
                "priority": "high" if classification in {"blocked", "needs-dispatch"} else "normal",
                "safe_summary": f"{repo_id} is {classification} for the fleet release train",
                "suggested_command": str(
                    repo.get("suggested_next_command") or f"brigade repos release show {train_id}"
                ),
                "created_at": created,
                "updated_at": created,
                "reviewed_at": reviewed_at,
                "source_fingerprint": source_fingerprint,
            }
        )
    actions.sort(key=_release_action_rank)
    return actions


def release_actions_plan(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actions = _planned_release_actions(train)
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "train_closeout_status": _train_closeout_status(train),
        "actions_root_label": ".brigade/repos/releases",
        "actions": actions,
        "action_count": len(actions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release actions plan: {train.get('train_id')}")
    print(f"actions: {len(actions)}")
    for action in actions[:20]:
        print(
            f"- {action.get('release_action_id')} {action.get('repo_id')} [{action.get('classification')}] {action.get('safe_summary')}"
        )
    return 0


def release_actions_build(
    *, target: Path, train_id: str = "latest", allow_unreviewed: bool = False, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    review_status = _train_closeout_status(train)
    if review_status not in {"reviewed", "deferred"} and not allow_unreviewed:
        print(
            "error: source fleet release train must be closed out as reviewed or deferred, or pass --allow-unreviewed",
            file=sys.stderr,
        )
        return 2
    existing = _read_release_actions(target)
    created, skipped = actionqueue.merge_planned(
        existing, _read_release_action_archive(target), _planned_release_actions(train)
    )
    _write_release_actions(target, existing)
    payload = {
        "target_label": "repo-fleet",
        "train_id": train.get("train_id"),
        "actions_path_label": ".brigade/repos/releases/actions.json",
        "created_count": len(created),
        "skipped_count": len(skipped),
        "created_actions": created,
        "skipped_actions": skipped,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release actions build: {train.get('train_id')}")
    print(f"created: {len(created)}")
    print(f"skipped: {len(skipped)}")
    return 0


def release_actions_list(*, target: Path, limit: int = 50, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    actions = _read_release_actions(target)
    actions.sort(key=_release_action_rank)
    payload = {
        "target_label": "repo-fleet",
        "actions_path_label": ".brigade/repos/releases/actions.json",
        "actions": actions[:limit],
        "action_count": len(actions),
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release actions")
    for action in actions[:limit]:
        print(
            f"- {action.get('release_action_id')} {action.get('repo_id')} [{action.get('status')}] {action.get('safe_summary')}"
        )
    return 0


def _find_release_action(
    target: Path, action_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    actions = _read_release_actions(target)
    action, error = actionqueue.find_action(
        actions, action_id, id_field="release_action_id", label="fleet release action"
    )
    return actions, action, error


def release_actions_show(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _, action, error = _find_release_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "action": action}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release action: {action.get('release_action_id')}")
    print(f"status: {action.get('status')}")
    print(f"repo: {action.get('repo_id')} {action.get('repo_label')}")
    print(f"summary: {action.get('safe_summary')}")
    return 0


def _set_release_action_status(
    *, target: Path, action_id: str, status: str, reason: str | None = None, json_output: bool = False
) -> int:
    target = target.expanduser().resolve()
    actions, action, error = _find_release_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actionqueue.stamp_status(action, status, now=_now().isoformat(), reason=reason)
    _write_release_actions(target, actions)
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "action": action}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release action {status}: {action.get('release_action_id')}")
    return 0


def release_actions_start(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_release_action_status(target=target, action_id=action_id, status="active", json_output=json_output)


def release_actions_done(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_release_action_status(target=target, action_id=action_id, status="done", json_output=json_output)


def release_actions_defer(*, target: Path, action_id: str, reason: str, json_output: bool = False) -> int:
    if not reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    return _set_release_action_status(
        target=target, action_id=action_id, status="deferred", reason=reason, json_output=json_output
    )


__all__ = tuple(name for name in globals() if not name.startswith("__"))
