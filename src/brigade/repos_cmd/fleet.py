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
from . import constants


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fingerprint_payload(value: Any) -> str:
    return work_cmd._stable_hash(value)


def _format_default_config() -> str:
    return """# Local-only repository fleet config.
# Keep exact private repository names, owner names, hostnames, and private paths out of committed files.

[[repo]]
id = "current"
label = "current repo"
path = "."
enabled = true
expect_brigade = true
expect_publish_guard = false
"""


def _health_command_from_raw(raw: object, repo_id: str, index: int) -> tuple[constants.SweepCommand | None, str | None]:
    if not isinstance(raw, dict):
        return None, f"repo {repo_id}: health command {index} must be a table"
    label = str(raw.get("label") or f"health-{index}").strip()
    if not label:
        return None, f"repo {repo_id}: health command {index} label is required"
    timeout_raw = raw.get("timeout", 120)
    timeout = int(timeout_raw) if isinstance(timeout_raw, int) and timeout_raw > 0 else 120
    enabled = bool(raw.get("enabled", True))
    if not enabled:
        return None, None
    argv: list[str] | None = None
    command = raw.get("command")
    if isinstance(command, str) and command.strip():
        argv, error = work_cmd._scanner_argv(command)
        if error:
            return None, f"repo {repo_id}: health command {label}: {error}"
    else:
        raw_argv = raw.get("argv")
        if isinstance(raw_argv, list) and all(isinstance(part, str) and part.strip() for part in raw_argv):
            argv = [str(part) for part in raw_argv]
            executable = Path(argv[0]).name
            if executable in work_cmd.SCANNER_HIGH_RISK_COMMANDS:
                return None, f"repo {repo_id}: health command {label}: high-risk scanner command: {executable}"
            if any(work_cmd.SCANNER_SHELL_META_RE.search(part) for part in argv):
                return (
                    None,
                    f"repo {repo_id}: health command {label}: high-risk scanner command contains shell metacharacters",
                )
            if executable != "brigade" and "/" not in argv[0] and shutil.which(argv[0]) is None:
                return None, f"repo {repo_id}: health command {label}: scanner command is not resolvable: {argv[0]}"
        else:
            return None, f"repo {repo_id}: health command {label}: command or argv is required"
    return constants.SweepCommand(label, argv or [], timeout), None


def _health_commands(raw_entry: dict[str, Any], repo_id: str) -> tuple[tuple[constants.SweepCommand, ...], list[str]]:
    raw_commands = raw_entry.get("health_command") or raw_entry.get("health_commands") or []
    if not isinstance(raw_commands, list):
        return (), [f"repo {repo_id}: health_commands must be a list"]
    commands: list[constants.SweepCommand] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_commands, start=1):
        command, error = _health_command_from_raw(raw, repo_id, index)
        if error:
            errors.append(error)
        if command is not None:
            commands.append(command)
    return tuple(commands), errors


def _load_config(target: Path) -> tuple[list[constants.RepoEntry], list[str], bool]:
    path = constants.config_path(target)
    if not path.is_file():
        return [], [f"missing config: {path}"], False
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"invalid config: {exc}"], True
    raw_entries = data.get("repo")
    if not isinstance(raw_entries, list):
        return [], ["missing [[repo]] entries"], True
    entries: list[constants.RepoEntry] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries, start=1):
        if not isinstance(raw, dict):
            errors.append(f"repo {index}: entry must be a table")
            continue
        repo_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or repo_id).strip()
        path_value = str(raw.get("path") or "").strip()
        if not repo_id:
            errors.append(f"repo {index}: id is required")
            continue
        if repo_id in seen:
            errors.append(f"repo {index}: duplicate id {repo_id}")
            continue
        seen.add(repo_id)
        if not path_value:
            errors.append(f"repo {repo_id}: path is required")
            continue
        health_commands, health_errors = _health_commands(raw, repo_id)
        errors.extend(health_errors)
        repo_path = (target / path_value).expanduser().resolve()
        entries.append(
            constants.RepoEntry(
                repo_id=repo_id,
                label=label or repo_id,
                path=repo_path,
                enabled=bool(raw.get("enabled", True)),
                expect_brigade=bool(raw.get("expect_brigade", False)),
                expect_publish_guard=bool(raw.get("expect_publish_guard", False)),
                health_commands=health_commands,
            )
        )
    return entries, errors, True


def _string_list(value: object, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return tuple(str(item).strip() for item in value)
    return default


def _load_discovery_roots(target: Path) -> tuple[list[constants.DiscoveryRoot], list[str], bool]:
    path = constants.config_path(target)
    if not path.is_file():
        return [], [f"missing config: {path}"], False
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"invalid config: {exc}"], True
    raw_roots = data.get("discovery_root") or data.get("discovery_roots") or []
    if not isinstance(raw_roots, list):
        return [], ["discovery_root entries must be a list"], True
    roots: list[constants.DiscoveryRoot] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_roots, start=1):
        if not isinstance(raw, dict):
            errors.append(f"discovery_root {index}: entry must be a table")
            continue
        root_id = str(raw.get("id") or "").strip()
        label = str(raw.get("label") or root_id).strip()
        path_value = str(raw.get("path") or "").strip()
        if not root_id:
            errors.append(f"discovery_root {index}: id is required")
            continue
        if root_id in seen:
            errors.append(f"discovery_root {index}: duplicate id {root_id}")
            continue
        seen.add(root_id)
        if not path_value:
            errors.append(f"discovery_root {root_id}: path is required")
            continue
        max_depth_raw = raw.get("max_depth", 2)
        max_depth = max_depth_raw if isinstance(max_depth_raw, int) and max_depth_raw >= 0 else 2
        roots.append(
            constants.DiscoveryRoot(
                root_id=root_id,
                label=label or root_id,
                path=(target / path_value).expanduser().resolve(),
                enabled=bool(raw.get("enabled", True)),
                include=_string_list(raw.get("include"), default=("*",)),
                exclude=_string_list(raw.get("exclude"), default=()),
                max_depth=max_depth,
            )
        )
    return roots, errors, True


def _match_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)


def _discovery_candidate_id(root_id: str, index: int) -> str:
    safe_root = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in root_id.lower()).strip("-") or "root"
    return f"{safe_root}-candidate-{index}"


def _discover_repos(root: constants.DiscoveryRoot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not root.enabled:
        return candidates, [{"root_id": root.root_id, "reason": "disabled"}]
    if not root.path.is_dir():
        return candidates, [{"root_id": root.root_id, "reason": "missing-root", "path_label": f"{root.root_id}:root"}]
    pending: list[tuple[Path, int]] = [(root.path, 0)]
    seen: set[Path] = set()
    while pending:
        current, depth = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        try:
            rel = current.relative_to(root.path)
        except ValueError:
            continue
        rel_label = "." if str(rel) == "." else rel.as_posix()
        if rel_label != "." and _match_any(rel_label, root.exclude):
            skipped.append(
                {
                    "root_id": root.root_id,
                    "path_label": f"{root.root_id}:excluded-{len(skipped) + 1}",
                    "reason": "excluded",
                    "depth": depth,
                }
            )
            continue
        if (current / ".git").exists() and (
            rel_label == "." or _match_any(rel_label, root.include) or _match_any(current.name, root.include)
        ):
            candidate_index = len(candidates) + 1
            path_label = f"{root.root_id}:candidate-{candidate_index}"
            candidates.append(
                {
                    "candidate_id": _discovery_candidate_id(root.root_id, candidate_index),
                    "root_id": root.root_id,
                    "root_label": root.label,
                    "path_label": path_label,
                    "depth": depth,
                    "repo_id_suggestion": _discovery_candidate_id(root.root_id, candidate_index),
                    "label_suggestion": f"{root.label} candidate {candidate_index}",
                    "has_git": True,
                    "would_clone": False,
                    "would_write": False,
                    "source_fingerprint": _fingerprint_payload(
                        {"root_id": root.root_id, "path_label": path_label, "depth": depth}
                    ),
                }
            )
            continue
        if depth >= root.max_depth:
            continue
        try:
            children = sorted(
                child
                for child in current.iterdir()
                if child.is_dir() and not child.is_symlink() and child.name != ".git"
            )
        except OSError:
            skipped.append(
                {
                    "root_id": root.root_id,
                    "path_label": f"{root.root_id}:unreadable-{len(skipped) + 1}",
                    "reason": "unreadable",
                    "depth": depth,
                }
            )
            continue
        pending.extend((child, depth + 1) for child in children)
    return candidates, skipped


def discover_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    roots, errors, config_loaded = _load_discovery_roots(target)
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    root_summaries: list[dict[str, Any]] = []
    for root in roots:
        found, root_skipped = _discover_repos(root)
        candidates.extend(found)
        skipped.extend(root_skipped)
        root_summaries.append(
            {
                "root_id": root.root_id,
                "label": root.label,
                "enabled": root.enabled,
                "root_path_label": f"{root.root_id}:root",
                "include": list(root.include),
                "exclude": list(root.exclude),
                "max_depth": root.max_depth,
                "candidate_count": len(found),
            }
        )
    checks: list[dict[str, Any]] = []
    if errors:
        checks.extend({"status": constants.WARN, "name": "repo_discovery_config", "detail": error} for error in errors)
    if config_loaded and not roots:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_discovery_roots_missing",
                "detail": "no [[discovery_root]] entries configured",
            }
        )
    if not config_loaded:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_discovery_config_missing",
                "detail": "repo discovery uses only explicit configured roots",
            }
        )
    payload = {
        "schema_version": 1,
        "target_label": "repo-fleet",
        "dry_run": True,
        "config_loaded": config_loaded,
        "checks": checks,
        "issue_count": len(checks),
        "roots": root_summaries,
        "root_count": len(root_summaries),
        "candidates": candidates,
        "candidate_count": len(candidates),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "would_clone": False,
        "would_write": False,
        "privacy": {
            "path_redaction": "absolute paths are represented as root-local labels",
            "safe_labels_only": True,
        },
        "suggested_next_commands": ["edit .brigade/repos.toml manually to add reviewed candidates"],
    }
    rc = 0 if config_loaded else 1
    text_lines = [
        "repo discovery plan",
        "dry_run: true",
        f"roots: {payload['root_count']}",
        f"candidates: {payload['candidate_count']}",
        "would_clone: false",
        "would_write: false",
        *[
            f"- {candidate['candidate_id']} {candidate['path_label']} depth={candidate['depth']}"
            for candidate in candidates
        ],
    ]
    return emit(payload, json_output, text_lines, rc)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_value(repo: Path, *args: str) -> str | None:
    result = _git(repo, *args)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _dirty_counts(repo: Path) -> tuple[int, int]:
    result = _git(repo, "status", "--porcelain=v1")
    if result.returncode != 0:
        return 0, 0
    tracked = 0
    untracked = 0
    for line in result.stdout.splitlines():
        if line.startswith("??"):
            untracked += 1
        elif line.strip():
            tracked += 1
    return tracked, untracked


def _test_hints(repo: Path) -> list[str]:
    hints: list[str] = []
    if (repo / "pyproject.toml").is_file() or (repo / "pytest.ini").is_file() or (repo / "tests").is_dir():
        hints.append("PYTHONPATH=src python3 -m pytest -q" if (repo / "src").is_dir() else "python3 -m pytest -q")
    if (repo / "package.json").is_file():
        hints.append("npm test")
    if (repo / "Cargo.toml").is_file():
        hints.append("cargo test")
    if (repo / "go.mod").is_file():
        hints.append("go test ./...")
    return hints


def _latest_json(root: Path, filename: str) -> str | None:
    if not root.is_dir():
        return None
    candidates = sorted(root.glob(f"*/{filename}"), key=lambda path: path.stat().st_mtime, reverse=True)
    return str(candidates[0]) if candidates else None


def _latest_json_payload(root: Path, filename: str) -> dict[str, Any] | None:
    path_value = _latest_json(root, filename)
    if path_value is None:
        return None
    payload = _read_json(Path(path_value))
    if payload is not None:
        payload.setdefault("path", path_value)
    return payload


def _safe_receipt(path: str | None, repo_id: str, label: str) -> dict[str, Any] | None:
    if not path:
        return None
    return {"repo_id": repo_id, "repo_label": label, "path_label": f"{repo_id}:{Path(path).name}"}


def _safe_text(
    value: object, repo_path: Path | None = None, repo_id: str | None = None, label: str | None = None
) -> str:
    text = str(value or "")
    replacements = []
    if repo_path is not None:
        replacements.append(str(repo_path))
    for private in replacements:
        if private:
            text = text.replace(private, str(label or repo_id or "repo"))
    return text


def _safe_report_ref(payload: dict[str, Any] | None, repo_id: str, label: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "repo_id": repo_id,
        "repo_label": label,
        "id": payload.get("report_id")
        or payload.get("run_id")
        or payload.get("candidate_id")
        or payload.get("closeout_id"),
        "status": payload.get("status") if isinstance(payload.get("status"), str) else None,
        "created_at": payload.get("created_at") or payload.get("generated_at") or payload.get("started_at"),
        "fingerprint": payload.get("report_fingerprint") or payload.get("source_fingerprint"),
    }


def _repo_brigade_state(entry: constants.RepoEntry) -> dict[str, Any]:
    repo = entry.path
    repo_id = entry.repo_id
    label = entry.label
    tracked_dirty, _ = _dirty_counts(repo) if repo.is_dir() else (0, 0)
    if not repo.is_dir():
        return {
            "repo_id": repo_id,
            "repo_label": label,
            "exists": False,
            "dirty_tracked_count": 0,
            "pending_import_count": 0,
            "pending_task_count": 0,
            "review_finding_count": 0,
            "handoff_draft_count": 0,
            "security_issue_count": 0,
            "scanner_sweep_status": "missing",
            "latest_operator_report": None,
            "action_queue": {"open_count": 0, "top_action": None},
            "latest_release_readiness": None,
            "latest_release_candidate": None,
            "latest_work_closeout": None,
            "receipt_references": [],
            "warnings": [{"name": "repo_missing", "detail": f"{repo_id} is not reachable"}],
            "blockers": [],
            "suggested_command": f"brigade repos show {repo_id}",
        }
    from .. import center_cmd, handoff_cmd, release_cmd, security_cmd

    latest_report = center_cmd.latest_report(repo)
    action_health = center_cmd.actions_health(repo)
    release_ready = release_cmd._latest_release_receipt(repo)
    release_candidate = release_cmd._latest_candidate(repo)
    work_closeout = _latest_json_payload(repo / ".brigade" / "work" / "closeouts", "closeout.json")
    review_health = work_cmd._review_health(repo)
    handoff_payload = handoff_cmd.draft_queue_payload(repo)
    security_health = security_cmd.health(repo)
    sweep_health = work_cmd._scanner_sweep_health(repo)
    pending_imports = work_cmd._pending_imports(repo)
    pending_tasks = work_cmd._pending_tasks(repo)
    receipt_refs = [
        _safe_receipt(
            str(Path(str(latest_report.get("path"))) / "CENTER_EVIDENCE.json")
            if isinstance(latest_report, dict) and latest_report.get("path")
            else None,
            repo_id,
            label,
        ),
        _safe_receipt(
            str(Path(str(release_ready.get("path"))) / "receipt.json")
            if isinstance(release_ready, dict) and release_ready.get("path")
            else None,
            repo_id,
            label,
        ),
        _safe_receipt(
            str(Path(str(release_candidate.get("path"))) / "EVIDENCE.json")
            if isinstance(release_candidate, dict) and release_candidate.get("path")
            else None,
            repo_id,
            label,
        ),
        _safe_receipt(str(work_closeout.get("path")) if isinstance(work_closeout, dict) else None, repo_id, label),
    ]
    warnings: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    if tracked_dirty:
        warnings.append(
            {"name": "repo_dirty_tracked", "detail": f"{repo_id} has dirty tracked files", "count": tracked_dirty}
        )
    if isinstance(latest_report, dict):
        report_created = _parse_time(latest_report.get("created_at") or latest_report.get("generated_at"))
        if report_created and (_now() - report_created).total_seconds() / 3600 > constants.REPORT_STALE_HOURS:
            warnings.append({"name": "repo_operator_report_stale", "detail": f"{repo_id} operator report is stale"})
    else:
        warnings.append({"name": "repo_operator_report_missing", "detail": f"{repo_id} has no operator report"})
    if int(action_health.get("open_count") or 0) > 0:
        warnings.append(
            {
                "name": "repo_actions_open",
                "detail": f"{repo_id} has open operator actions",
                "count": action_health.get("open_count"),
            }
        )
    security_count = int(security_health.get("issue_count") or 0)
    if security_count > 0:
        warnings.append(
            {"name": "repo_security_issues", "detail": f"{repo_id} has security issue(s)", "count": security_count}
        )
    return {
        "repo_id": repo_id,
        "repo_label": label,
        "exists": True,
        "branch": _git_value(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_tracked_count": tracked_dirty,
        "pending_import_count": len(pending_imports),
        "pending_task_count": len(pending_tasks),
        "review_finding_count": int(review_health.get("pending_finding_count") or 0)
        + int(review_health.get("unresolved_finding_count") or 0),
        "handoff_draft_count": int(
            (handoff_payload.get("counts") if isinstance(handoff_payload.get("counts"), dict) else {}).get("pending")
            or 0
        ),
        "security_issue_count": security_count,
        "scanner_sweep_status": (sweep_health.get("latest") or {}).get("status")
        if isinstance(sweep_health.get("latest"), dict)
        else "missing",
        "latest_operator_report": _safe_report_ref(latest_report, repo_id, label),
        "action_queue": {
            "open_count": action_health.get("open_count"),
            "top_action": _safe_action_ref(
                action_health.get("top_action") if isinstance(action_health.get("top_action"), dict) else None,
                repo_id,
                label,
                repo,
            ),
        },
        "latest_release_readiness": _safe_report_ref(release_ready, repo_id, label),
        "latest_release_candidate": _safe_report_ref(release_candidate, repo_id, label),
        "latest_work_closeout": _safe_report_ref(work_closeout, repo_id, label),
        "receipt_references": [ref for ref in receipt_refs if ref is not None],
        "warnings": warnings,
        "blockers": blockers,
        "suggested_command": _repo_suggested_command(repo_id, pending_imports, action_health),
    }


def _safe_action_ref(
    action: dict[str, Any] | None, repo_id: str, label: str, repo_path: Path | None = None
) -> dict[str, Any] | None:
    if not isinstance(action, dict):
        return None
    return {
        "repo_id": repo_id,
        "repo_label": label,
        "action_id": action.get("action_id"),
        "status": action.get("status"),
        "source_report_id": action.get("source_report_id"),
        "source_group": action.get("source_group"),
        "source_subsystem": action.get("source_subsystem"),
        "source_local_id": action.get("source_local_id"),
        "safe_summary": _safe_text(action.get("safe_summary"), repo_path, repo_id, label),
        "suggested_command": action.get("suggested_command"),
        "source_fingerprint": action.get("source_fingerprint"),
    }


def _repo_suggested_command(repo_id: str, pending_imports: list[dict[str, Any]], action_health: dict[str, Any]) -> str:
    top_action = action_health.get("top_action") if isinstance(action_health.get("top_action"), dict) else None
    if top_action:
        return f"brigade repos actions show {top_action.get('action_id')}"
    if pending_imports:
        import_id = pending_imports[0].get("id")
        return f"brigade work import plan {import_id}"
    return f"brigade repos show {repo_id}"


def _handoff_backlog(repo: Path) -> tuple[int, int | None]:
    """Total pending handoffs across a repo's inboxes and the oldest age in days.

    Surfaces the silent pile-up where handoffs are written but never ingested,
    e.g. a repo that is outside the canonical ingester's coverage.
    """
    if not repo.is_dir():
        return 0, None
    from .. import handoff_cmd

    pending = 0
    oldest_seconds: int | None = None
    try:
        health = handoff_cmd.inspect(repo)
    except (ValueError, OSError):
        return 0, None
    for inbox in health.inboxes:
        pending += inbox.pending
        age = inbox.oldest_pending_age_seconds
        if age is not None and (oldest_seconds is None or age > oldest_seconds):
            oldest_seconds = age
    oldest_days = oldest_seconds // (24 * 60 * 60) if oldest_seconds is not None else None
    return pending, oldest_days


def _repo_summary(entry: constants.RepoEntry) -> dict[str, Any]:
    repo = entry.path
    tracked_dirty, untracked_dirty = _dirty_counts(repo) if repo.is_dir() else (0, 0)
    has_agents = (repo / "AGENTS.md").is_file()
    has_claude = (repo / "CLAUDE.md").is_file() or (repo / ".claude" / "CLAUDE.md").is_file()
    handoff_inboxes = [inbox for inbox in WRITER_INBOXES.values() if (repo / inbox).is_dir()]
    handoff_pending, handoff_backlog_oldest_days = _handoff_backlog(repo)
    hooks = repo / ".git" / "hooks"
    publish_guard_hooks = (
        [hook.name for hook in (hooks / "pre-commit", hooks / "pre-push") if hook.is_file()] if hooks.is_dir() else []
    )
    return {
        "id": entry.repo_id,
        "label": entry.label,
        "path_label": entry.repo_id,
        "enabled": entry.enabled,
        "exists": repo.is_dir(),
        "branch": _git_value(repo, "rev-parse", "--abbrev-ref", "HEAD") if repo.is_dir() else None,
        "dirty_tracked_count": tracked_dirty,
        "dirty_untracked_count": untracked_dirty,
        "has_agents": has_agents,
        "has_claude": has_claude,
        "guidance_source": "AGENTS.md" if has_agents else ("CLAUDE.md" if has_claude else None),
        "has_roadmap": (repo / "ROADMAP.md").is_file(),
        "has_readme": (repo / "README.md").is_file(),
        "has_changelog": (repo / "CHANGELOG.md").is_file(),
        "test_hints": _test_hints(repo) if repo.is_dir() else [],
        "handoff_inboxes": handoff_inboxes,
        "handoff_pending": handoff_pending,
        "handoff_backlog_oldest_days": handoff_backlog_oldest_days,
        "publish_guard_hooks": publish_guard_hooks,
        "has_brigade_config": (repo / ".brigade").is_dir(),
        "latest_release_readiness": _latest_json(repo / ".brigade" / "release" / "runs", "release.json"),
        "latest_release_candidate": _latest_json(repo / ".brigade" / "release" / "candidates", "EVIDENCE.json"),
        "latest_work_closeout": _latest_json(repo / ".brigade" / "work" / "closeouts", "closeout.json"),
        "expect_brigade": entry.expect_brigade,
        "expect_publish_guard": entry.expect_publish_guard,
    }


def _repo_summaries(entries: list[constants.RepoEntry]) -> list[dict[str, Any]]:
    """Summarize every enabled repo, in config order.

    Each summary is independent and IO-bound (git calls plus file stats), so a
    multi-repo fleet runs them on a small thread pool; executor.map preserves the
    config order the rest of the scan depends on. A single repo stays serial.
    """
    enabled = [entry for entry in entries if entry.enabled]
    if len(enabled) <= 1:
        return [_repo_summary(entry) for entry in enabled]
    with ThreadPoolExecutor(max_workers=min(8, len(enabled))) as executor:
        return list(executor.map(_repo_summary, enabled))


def _repo_checks(summary: dict[str, Any]) -> list[dict[str, Any]]:
    repo_id = str(summary.get("id") or "unknown")
    checks: list[dict[str, Any]] = []
    if not summary.get("exists"):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_missing",
                "detail": f"{repo_id} is not reachable",
                "repo_id": repo_id,
            }
        )
        return checks
    if not summary.get("has_agents") and summary.get("has_claude"):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_claude_fallback",
                "detail": f"{repo_id} relies on CLAUDE guidance fallback",
                "repo_id": repo_id,
            }
        )
    elif not summary.get("has_agents") and not summary.get("has_claude"):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_missing_guidance",
                "detail": f"{repo_id} has no AGENTS or CLAUDE guidance",
                "repo_id": repo_id,
            }
        )
    if not summary.get("test_hints"):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_missing_test_hint",
                "detail": f"{repo_id} has no detected test hint",
                "repo_id": repo_id,
            }
        )
    if summary.get("expect_brigade") and not summary.get("has_brigade_config"):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_missing_brigade_config",
                "detail": f"{repo_id} lacks local Brigade config",
                "repo_id": repo_id,
            }
        )
    if summary.get("expect_publish_guard") and not summary.get("publish_guard_hooks"):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_missing_publish_guard",
                "detail": f"{repo_id} lacks expected publish-guard hooks",
                "repo_id": repo_id,
            }
        )
    if int(summary.get("dirty_tracked_count", 0) or 0) > 0:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_dirty_tracked",
                "detail": f"{repo_id} has dirty tracked files",
                "repo_id": repo_id,
            }
        )
    if not summary.get("handoff_inboxes"):
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_missing_handoff_inbox",
                "detail": f"{repo_id} has no local handoff inbox",
                "repo_id": repo_id,
            }
        )
    pending = int(summary.get("handoff_pending", 0) or 0)
    oldest_days = summary.get("handoff_backlog_oldest_days")
    if pending and isinstance(oldest_days, int) and oldest_days >= constants.BACKLOG_STALE_DAYS:
        checks.append(
            {
                "status": constants.WARN,
                "name": "repo_handoff_backlog",
                "detail": f"{repo_id} has {pending} un-ingested handoff(s), oldest {oldest_days}d old; ingester is not reaching it",
                "repo_id": repo_id,
            }
        )
    return checks


def scan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    entries, errors, config_loaded = _load_config(target)
    repos = _repo_summaries(entries)
    checks: list[dict[str, Any]] = []
    if errors:
        checks.extend({"status": constants.WARN, "name": "repo_fleet_config", "detail": error} for error in errors)
    elif config_loaded:
        checks.append(
            {"status": constants.OK, "name": "repo_fleet_config", "detail": str(constants.config_path(target))}
        )
    for summary in repos:
        repo_checks = _repo_checks(summary)
        if repo_checks:
            checks.extend(repo_checks)
        else:
            checks.append(
                {"status": constants.OK, "name": "repo_ready", "detail": summary["id"], "repo_id": summary["id"]}
            )
    issues = [check for check in checks if check["status"] != constants.OK]
    return {
        "target": str(target),
        "config_path": str(constants.config_path(target)),
        "config_loaded": config_loaded,
        "repos": repos,
        "repo_count": len(repos),
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def ingest_fleet(
    *,
    target: Path,
    apply: bool = False,
    promote_cards: bool = True,
    route_documents: bool = True,
    json_output: bool = False,
) -> int:
    """Ingest every fleet repo's handoffs into the canonical owner (`target`).

    The fleet model: many writer repos drop handoffs into their own inboxes; one
    owner holds canonical memory. This sweeps each registered, reachable repo,
    routing its handoffs into `target`'s memory and archiving the processed
    handoffs back in the source repo. Defaults to a dry run; pass apply=True to
    write. Closes the gap where each repo had to be ingested by hand.
    """
    from .. import ingest as ingest_mod

    target = target.expanduser().resolve()
    entries, errors, config_loaded = _load_config(target)
    if not config_loaded:
        print(f"error: no repo fleet config at {constants.config_path(target)}", file=sys.stderr)
        return 2

    dry_run = not apply
    stats = ingest_mod.IngestStats()
    per_repo: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.enabled:
            continue
        if not entry.path.is_dir():
            skipped.append({"repo_id": entry.repo_id, "reason": "not reachable"})
            continue
        before = (stats.processed, stats.promoted, stats.routed, stats.inboxed, stats.skipped)
        rc = ingest_mod.ingest_into(
            source=entry.path,
            owner=target,
            stats=stats,
            dry_run=dry_run,
            promote_cards=promote_cards,
            route_documents=route_documents,
        )
        if rc == 2:
            skipped.append({"repo_id": entry.repo_id, "reason": "no handoff inbox"})
            continue
        per_repo.append(
            {
                "repo_id": entry.repo_id,
                "processed": stats.processed - before[0],
                "promoted": stats.promoted - before[1],
                "routed": stats.routed - before[2],
                "inboxed": stats.inboxed - before[3],
                "skipped": stats.skipped - before[4],
            }
        )

    payload = {
        "target": str(target),
        "owner": str(target),
        "dry_run": dry_run,
        "config_loaded": config_loaded,
        "config_errors": errors,
        "repos_ingested": per_repo,
        "skipped": skipped,
        "totals": {
            "processed": stats.processed,
            "promoted": stats.promoted,
            "routed": stats.routed,
            "inboxed": stats.inboxed,
            "skipped": stats.skipped,
        },
    }
    mode = "DRY-RUN (pass --apply to write)" if dry_run else "applied"
    totals = payload["totals"]
    text_lines = [
        f"repos ingest [{mode}] -> owner {target}",
        *[
            f"- {repo['repo_id']}: processed={repo['processed']} promoted={repo['promoted']} "
            f"routed={repo['routed']} inboxed={repo['inboxed']}"
            for repo in per_repo
        ],
        *[f"- {item['repo_id']}: skipped ({item['reason']})" for item in skipped],
        f"totals: processed={totals['processed']} promoted={totals['promoted']} "
        f"routed={totals['routed']} inboxed={totals['inboxed']} skipped={totals['skipped']}",
    ]
    return emit(payload, json_output, text_lines, 0)


def _capture_json_command(func, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    output = StringIO()
    with redirect_stdout(output):
        rc = func(**kwargs, json_output=True)
    try:
        payload = json.loads(output.getvalue() or "{}")
    except json.JSONDecodeError:
        payload = {
            "valid": False,
            "errors": [f"{getattr(func, '__name__', 'command')} returned invalid JSON"],
            "output": output.getvalue().strip().splitlines(),
        }
        rc = 1
    return rc, payload


def _rearm_selection(repo_path: Path) -> tuple[Selection | None, str | None]:
    config = brigade_config.config_path(repo_path)
    if not config.is_file():
        return None, constants.UNWIRED_REARM_REASON
    try:
        loaded = brigade_config.load_config(repo_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"invalid .brigade/config.json: {exc}"
    if loaded is None:
        return None, constants.UNWIRED_REARM_REASON
    return loaded.selection, None


def _rearm_reason(*, has_dogfood: bool, has_mcp: bool) -> str | None:
    missing = []
    if not has_dogfood:
        missing.append(".brigade/dogfood.toml")
    if not has_mcp:
        missing.append(".brigade/mcp.json")
    if not missing:
        return None
    return f"missing {', '.join(missing)}"


def rearm(*, target: Path, apply: bool = False, json_output: bool = False) -> int:
    """Plan or apply the operator quickstart loop across configured fleet repos."""
    from .. import operator_cmd

    target = target.expanduser().resolve()
    entries, errors, config_loaded = _load_config(target)
    if not config_loaded:
        print(f"error: no repo fleet config at {constants.config_path(target)}", file=sys.stderr)
        return 2

    dry_run = not apply
    repos: list[dict[str, Any]] = []
    totals = {"armed": 0, "dormant": 0, "unwired": 0, "applied": 0, "failed": 0, "skipped": 0}
    for entry in entries:
        if not entry.enabled:
            continue
        brigade_dir = entry.path / ".brigade"
        has_config = (brigade_dir / "config.json").is_file()
        has_dogfood = (brigade_dir / "dogfood.toml").is_file()
        has_mcp = (brigade_dir / "mcp.json").is_file()
        selection, selection_error = _rearm_selection(entry.path)
        harnesses = list(selection.harnesses) if selection is not None else []
        row: dict[str, Any] = {
            "repo_id": entry.repo_id,
            "label": entry.label,
            "status": "unwired",
            "action": "skip",
            "reason": selection_error,
            "has_config": has_config,
            "has_dogfood": has_dogfood,
            "has_mcp": has_mcp,
            "harnesses": harnesses,
        }
        if selection is None:
            totals["unwired"] += 1
            totals["skipped"] += 1
            repos.append(row)
            continue

        reason = _rearm_reason(has_dogfood=has_dogfood, has_mcp=has_mcp)
        if reason is None:
            row.update({"status": "armed", "action": "none", "reason": None})
            totals["armed"] += 1
            repos.append(row)
            continue

        row.update({"status": "dormant", "action": "plan quickstart", "reason": reason})
        totals["dormant"] += 1
        if apply:
            rc, quickstart_payload = _capture_json_command(
                operator_cmd.quickstart,
                target=entry.path,
                depth=selection.depth,
                harnesses=",".join(selection.harnesses) or "none",
                owner=selection.owner,
                dry_run=False,
                force=False,
                full=False,
            )
            row["quickstart"] = quickstart_payload
            if rc == 0:
                row["action"] = "applied quickstart"
                totals["applied"] += 1
            else:
                row["action"] = "quickstart failed"
                row["quickstart_return_code"] = rc
                totals["failed"] += 1
        repos.append(row)

    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "config_loaded": config_loaded,
        "config_errors": errors,
        "repos": repos,
        "totals": totals,
    }
    mode = "DRY-RUN (pass --apply to write)" if dry_run else "applied"
    text_lines = [
        f"repos rearm [{mode}]: {target}",
        *[
            f"- {repo['repo_id']}: status={repo['status']} action={repo['action']}"
            + (f" reason={repo['reason']}" if repo.get("reason") else "")
            for repo in repos
        ],
        f"totals: armed={totals['armed']} dormant={totals['dormant']} "
        f"unwired={totals['unwired']} applied={totals['applied']} failed={totals['failed']}",
    ]
    return emit(payload, json_output, text_lines, 1 if totals["failed"] else 0)


def init(*, target: Path, force: bool = False, update_gitignore: bool = True, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = constants.config_path(target)
    if path.exists() and not force:
        print(f"error: repo fleet config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_default_config())
    gitignore = "skipped"
    if update_gitignore:
        gitignore = apply_gitignore(target, Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[]))
    payload = {"target": str(target), "config_path": str(path), "gitignore": gitignore, "repo_count": 1}
    text_lines = [f"repos_config: {path}", f"gitignore: {gitignore}", "next_command: brigade repos scan"]
    return emit(payload, json_output, text_lines, 0)


def list_repos(*, target: Path, json_output: bool = False) -> int:
    payload = scan_payload(target)
    rc = 0 if payload["config_loaded"] else 1
    text_lines = [
        f"repos: {payload['target']}",
        f"config_path: {payload['config_path']}",
        *[
            f"- {repo['id']} [{repo['branch'] or 'unknown'}] dirty={repo['dirty_tracked_count']}"
            for repo in payload["repos"]
        ],
    ]
    return emit(payload, json_output, text_lines, rc)


def show(*, target: Path, repo_id: str, json_output: bool = False) -> int:
    payload = scan_payload(target)
    repo = next((item for item in payload["repos"] if item.get("id") == repo_id), None)
    if repo is None:
        print(f"error: repo not found: {repo_id}", file=sys.stderr)
        return 1
    checks = [check for check in payload["checks"] if check.get("repo_id") == repo_id]
    output = {"target": payload["target"], "repo": repo, "checks": checks}
    text_lines = [
        f"repo: {repo['id']}",
        f"label: {repo['label']}",
        f"branch: {repo.get('branch') or 'unknown'}",
        f"guidance: {repo.get('guidance_source') or 'none'}",
        f"tests: {', '.join(repo.get('test_hints') or []) or 'none'}",
        *[
            f"[{check['status']}] {check['name']}: {check['detail']}"
            for check in checks
            if check["status"] != constants.OK
        ],
    ]
    return emit(output, json_output, text_lines, 0)


def scan(*, target: Path, json_output: bool = False) -> int:
    payload = scan_payload(target)
    rc = 0 if payload["config_loaded"] else 1
    text_lines = [
        f"repos scan: {payload['target']}",
        f"repos: {payload['repo_count']}",
        f"issues: {payload['issue_count']}",
        *[
            f"- {repo['id']} guidance={repo.get('guidance_source') or 'none'} tests={len(repo.get('test_hints') or [])}"
            for repo in payload["repos"]
        ],
    ]
    return emit(payload, json_output, text_lines, rc)


def deep_payload(target: Path) -> dict[str, Any]:
    """Run the operator checkup in each enabled repo and aggregate a fleet verdict.

    Each repo's checkup runs the read-only first-run doctors in-process. The loop
    is intentionally serial: the checkup captures stdout via redirect_stdout, which
    is process-global and not safe to run from multiple threads at once.
    """
    from .. import operator_cmd

    target = target.expanduser().resolve()
    entries, errors, _config_loaded = _load_config(target)
    repos: list[dict[str, Any]] = []
    blocking = 0
    for entry in entries:
        if not entry.enabled:
            continue
        if not entry.path.is_dir():
            repos.append({"id": entry.repo_id, "ready": False, "blocking_surface_count": None, "error": "missing"})
            blocking += 1
            continue
        checkup = operator_cmd.checkup_payload(entry.path)
        ready = bool(checkup.get("ready"))
        if not ready:
            blocking += 1
        repos.append(
            {
                "id": entry.repo_id,
                "ready": ready,
                "blocking_surface_count": checkup.get("blocking_surface_count"),
                "surfaces": [{"name": s.get("name"), "ready": s.get("ready")} for s in checkup.get("surfaces", [])],
            }
        )
    return {
        "target": str(target),
        "deep": True,
        "ready": blocking == 0 and bool(repos),
        "repo_count": len(repos),
        "blocking_repo_count": blocking,
        "repos": repos,
        "errors": errors,
    }


def doctor(*, target: Path, json_output: bool = False, deep: bool = False) -> int:
    if deep:
        payload = deep_payload(target)
        rc = 0 if payload["ready"] else 1
        repo_lines = []
        for repo in payload["repos"]:
            mark = "ok" if repo["ready"] else "fail"
            if repo.get("error") == "missing":
                repo_lines.append(f"  [{mark}] {repo['id']}: repo path is missing")
            else:
                repo_lines.append(f"  [{mark}] {repo['id']}: {repo['blocking_surface_count']} blocking surface(s)")
        text_lines = [
            f"repos doctor --deep: {payload['target']}",
            *[f"[constants.WARN] repo_fleet_config: {error}" for error in payload["errors"]],
            *repo_lines,
            f"ready: {'yes' if payload['ready'] else 'no'}",
            f"repos: {payload['repo_count']}, blocking: {payload['blocking_repo_count']}",
        ]
        return emit(payload, json_output, text_lines, rc)

    from . import fleet_health

    payload = fleet_health.health(target)
    scan_issue_count = sum(
        1 for check in payload["checks"] if isinstance(check, dict) and check.get("status") != constants.OK
    )
    health_issue_count = int(payload.get("issue_count") or 0)
    checks = [*payload["checks"]]
    for bucket_name in ("report", "actions", "sweep", "health_commands", "release_train"):
        bucket = payload.get(bucket_name) if isinstance(payload.get(bucket_name), dict) else {}
        checks.extend(bucket.get("checks") if isinstance(bucket.get("checks"), list) else [])
    payload = {**payload, "checks": checks, "issue_count": scan_issue_count, "health_issue_count": health_issue_count}
    rc = 0 if scan_issue_count == 0 else 1
    text_lines = [
        f"repos doctor: {payload['target']}",
        *[f"[{check['status']}] {check['name']}: {check['detail']}" for check in checks],
    ]
    return emit(payload, json_output, text_lines, rc)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
