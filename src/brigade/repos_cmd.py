"""Local repository fleet readiness inspection."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .install import apply_gitignore
from .selection import Selection
from . import toml_compat as tomllib, work_cmd

OK = "ok"
WARN = "warn"
FAIL = "fail"
CONFIG_REL_PATH = ".brigade/repos.toml"
REPORT_STALE_HOURS = 24
ACTION_STATUSES = {"pending", "active", "done", "deferred", "archived"}
DISPATCH_STALE_HOURS = 24
RELEASE_TRAIN_STALE_HOURS = 168
RELEASE_EVIDENCE_STEPS = {"verification", "release-doctor", "candidate-compare", "tag", "push", "release", "other"}
RELEASE_EVIDENCE_STATUSES = {"completed", "skipped", "blocked", "deferred"}
REQUIRED_RELEASE_EVIDENCE_STEPS = ("verification", "release-doctor", "candidate-compare", "tag", "push", "release")
RELEASE_WAIVER_SCOPES = {"blocked-repo", "unresolved-action", "missing-evidence", "blocked-evidence"}
RELEASE_WAIVER_STALE_HOURS = 168
RELEASE_BUNDLE_FILES = (
    "FLEET_RELEASE_EVIDENCE.json",
    "FLEET_RELEASE_TRAIN.md",
    "MANUAL_PUBLISH_PLAN.md",
    "CLOSEOUT.json",
    "RELEASE_TRAIN_REPORT.json",
    "RELEASE_TRAIN_REPORT.md",
    "RELEASE_TRAIN_MANIFEST.json",
)


@dataclass(frozen=True)
class RepoEntry:
    repo_id: str
    label: str
    path: Path
    enabled: bool = True
    expect_brigade: bool = False
    expect_publish_guard: bool = False
    health_commands: tuple[SweepCommand, ...] = ()


@dataclass(frozen=True)
class SweepCommand:
    label: str
    argv: list[str]
    timeout: int = 120


@dataclass(frozen=True)
class DiscoveryRoot:
    root_id: str
    label: str
    path: Path
    enabled: bool = True
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    max_depth: int = 2


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return records
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


def _health_command_from_raw(raw: object, repo_id: str, index: int) -> tuple[SweepCommand | None, str | None]:
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
                return None, f"repo {repo_id}: health command {label}: high-risk scanner command contains shell metacharacters"
            if executable != "brigade" and "/" not in argv[0] and shutil.which(argv[0]) is None:
                return None, f"repo {repo_id}: health command {label}: scanner command is not resolvable: {argv[0]}"
        else:
            return None, f"repo {repo_id}: health command {label}: command or argv is required"
    return SweepCommand(label, argv or [], timeout), None


def _health_commands(raw_entry: dict[str, Any], repo_id: str) -> tuple[tuple[SweepCommand, ...], list[str]]:
    raw_commands = raw_entry.get("health_command") or raw_entry.get("health_commands") or []
    if not isinstance(raw_commands, list):
        return (), [f"repo {repo_id}: health_commands must be a list"]
    commands: list[SweepCommand] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_commands, start=1):
        command, error = _health_command_from_raw(raw, repo_id, index)
        if error:
            errors.append(error)
        if command is not None:
            commands.append(command)
    return tuple(commands), errors


def _load_config(target: Path) -> tuple[list[RepoEntry], list[str], bool]:
    path = config_path(target)
    if not path.is_file():
        return [], [f"missing config: {path}"], False
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"invalid config: {exc}"], True
    raw_entries = data.get("repo")
    if not isinstance(raw_entries, list):
        return [], ["missing [[repo]] entries"], True
    entries: list[RepoEntry] = []
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
            RepoEntry(
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


def _load_discovery_roots(target: Path) -> tuple[list[DiscoveryRoot], list[str], bool]:
    path = config_path(target)
    if not path.is_file():
        return [], [f"missing config: {path}"], False
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"invalid config: {exc}"], True
    raw_roots = data.get("discovery_root") or data.get("discovery_roots") or []
    if not isinstance(raw_roots, list):
        return [], ["discovery_root entries must be a list"], True
    roots: list[DiscoveryRoot] = []
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
            DiscoveryRoot(
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


def _discover_repos(root: DiscoveryRoot) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
            skipped.append({"root_id": root.root_id, "path_label": f"{root.root_id}:excluded-{len(skipped) + 1}", "reason": "excluded", "depth": depth})
            continue
        if (current / ".git").exists() and (rel_label == "." or _match_any(rel_label, root.include) or _match_any(current.name, root.include)):
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
                    "source_fingerprint": _fingerprint_payload({"root_id": root.root_id, "path_label": path_label, "depth": depth}),
                }
            )
            continue
        if depth >= root.max_depth:
            continue
        try:
            children = sorted(child for child in current.iterdir() if child.is_dir() and not child.is_symlink() and child.name != ".git")
        except OSError:
            skipped.append({"root_id": root.root_id, "path_label": f"{root.root_id}:unreadable-{len(skipped) + 1}", "reason": "unreadable", "depth": depth})
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
        checks.extend({"status": WARN, "name": "repo_discovery_config", "detail": error} for error in errors)
    if config_loaded and not roots:
        checks.append({"status": WARN, "name": "repo_discovery_roots_missing", "detail": "no [[discovery_root]] entries configured"})
    if not config_loaded:
        checks.append({"status": WARN, "name": "repo_discovery_config_missing", "detail": "repo discovery uses only explicit configured roots"})
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
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if config_loaded else 1
    print("repo discovery plan")
    print("dry_run: true")
    print(f"roots: {payload['root_count']}")
    print(f"candidates: {payload['candidate_count']}")
    print("would_clone: false")
    print("would_write: false")
    for candidate in candidates:
        print(f"- {candidate['candidate_id']} {candidate['path_label']} depth={candidate['depth']}")
    return 0 if config_loaded else 1


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


def _safe_text(value: object, repo_path: Path | None = None, repo_id: str | None = None, label: str | None = None) -> str:
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
        "id": payload.get("report_id") or payload.get("run_id") or payload.get("candidate_id") or payload.get("closeout_id"),
        "status": payload.get("status") if isinstance(payload.get("status"), str) else None,
        "created_at": payload.get("created_at") or payload.get("generated_at") or payload.get("started_at"),
        "fingerprint": payload.get("report_fingerprint") or payload.get("source_fingerprint"),
    }


def _repo_brigade_state(entry: RepoEntry) -> dict[str, Any]:
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
    from . import center_cmd, handoff_cmd, release_cmd, security_cmd

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
        _safe_receipt(str(Path(str(latest_report.get("path"))) / "CENTER_EVIDENCE.json") if isinstance(latest_report, dict) and latest_report.get("path") else None, repo_id, label),
        _safe_receipt(str(Path(str(release_ready.get("path"))) / "receipt.json") if isinstance(release_ready, dict) and release_ready.get("path") else None, repo_id, label),
        _safe_receipt(str(Path(str(release_candidate.get("path"))) / "EVIDENCE.json") if isinstance(release_candidate, dict) and release_candidate.get("path") else None, repo_id, label),
        _safe_receipt(str(work_closeout.get("path")) if isinstance(work_closeout, dict) else None, repo_id, label),
    ]
    warnings: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    if tracked_dirty:
        warnings.append({"name": "repo_dirty_tracked", "detail": f"{repo_id} has dirty tracked files", "count": tracked_dirty})
    if isinstance(latest_report, dict):
        report_created = _parse_time(latest_report.get("created_at") or latest_report.get("generated_at"))
        if report_created and (_now() - report_created).total_seconds() / 3600 > REPORT_STALE_HOURS:
            warnings.append({"name": "repo_operator_report_stale", "detail": f"{repo_id} operator report is stale"})
    else:
        warnings.append({"name": "repo_operator_report_missing", "detail": f"{repo_id} has no operator report"})
    if int(action_health.get("open_count") or 0) > 0:
        warnings.append({"name": "repo_actions_open", "detail": f"{repo_id} has open operator actions", "count": action_health.get("open_count")})
    security_count = int(security_health.get("issue_count") or 0)
    if security_count > 0:
        warnings.append({"name": "repo_security_issues", "detail": f"{repo_id} has security issue(s)", "count": security_count})
    return {
        "repo_id": repo_id,
        "repo_label": label,
        "exists": True,
        "branch": _git_value(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_tracked_count": tracked_dirty,
        "pending_import_count": len(pending_imports),
        "pending_task_count": len(pending_tasks),
        "review_finding_count": int(review_health.get("pending_finding_count") or 0) + int(review_health.get("unresolved_finding_count") or 0),
        "handoff_draft_count": int((handoff_payload.get("counts") if isinstance(handoff_payload.get("counts"), dict) else {}).get("pending") or 0),
        "security_issue_count": security_count,
        "scanner_sweep_status": (sweep_health.get("latest") or {}).get("status") if isinstance(sweep_health.get("latest"), dict) else "missing",
        "latest_operator_report": _safe_report_ref(latest_report, repo_id, label),
        "action_queue": {
            "open_count": action_health.get("open_count"),
            "top_action": _safe_action_ref(action_health.get("top_action") if isinstance(action_health.get("top_action"), dict) else None, repo_id, label, repo),
        },
        "latest_release_readiness": _safe_report_ref(release_ready, repo_id, label),
        "latest_release_candidate": _safe_report_ref(release_candidate, repo_id, label),
        "latest_work_closeout": _safe_report_ref(work_closeout, repo_id, label),
        "receipt_references": [ref for ref in receipt_refs if ref is not None],
        "warnings": warnings,
        "blockers": blockers,
        "suggested_command": _repo_suggested_command(repo_id, pending_imports, action_health),
    }


def _safe_action_ref(action: dict[str, Any] | None, repo_id: str, label: str, repo_path: Path | None = None) -> dict[str, Any] | None:
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


def _repo_summary(entry: RepoEntry) -> dict[str, Any]:
    repo = entry.path
    tracked_dirty, untracked_dirty = _dirty_counts(repo) if repo.is_dir() else (0, 0)
    has_agents = (repo / "AGENTS.md").is_file()
    has_claude = (repo / "CLAUDE.md").is_file() or (repo / ".claude" / "CLAUDE.md").is_file()
    handoff_inboxes = [
        inbox
        for inbox in (".claude/memory-handoffs", ".codex/memory-handoffs")
        if (repo / inbox).is_dir()
    ]
    hooks = repo / ".git" / "hooks"
    publish_guard_hooks = [
        hook.name
        for hook in (hooks / "pre-commit", hooks / "pre-push")
        if hook.is_file()
    ] if hooks.is_dir() else []
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
        "publish_guard_hooks": publish_guard_hooks,
        "has_brigade_config": (repo / ".brigade").is_dir(),
        "latest_release_readiness": _latest_json(repo / ".brigade" / "release" / "runs", "release.json"),
        "latest_release_candidate": _latest_json(repo / ".brigade" / "release" / "candidates", "EVIDENCE.json"),
        "latest_work_closeout": _latest_json(repo / ".brigade" / "work" / "closeouts", "closeout.json"),
        "expect_brigade": entry.expect_brigade,
        "expect_publish_guard": entry.expect_publish_guard,
    }


def _repo_checks(summary: dict[str, Any]) -> list[dict[str, Any]]:
    repo_id = str(summary.get("id") or "unknown")
    checks: list[dict[str, Any]] = []
    if not summary.get("exists"):
        checks.append({"status": WARN, "name": "repo_missing", "detail": f"{repo_id} is not reachable", "repo_id": repo_id})
        return checks
    if not summary.get("has_agents") and summary.get("has_claude"):
        checks.append({"status": WARN, "name": "repo_claude_fallback", "detail": f"{repo_id} relies on CLAUDE guidance fallback", "repo_id": repo_id})
    elif not summary.get("has_agents") and not summary.get("has_claude"):
        checks.append({"status": WARN, "name": "repo_missing_guidance", "detail": f"{repo_id} has no AGENTS or CLAUDE guidance", "repo_id": repo_id})
    if not summary.get("test_hints"):
        checks.append({"status": WARN, "name": "repo_missing_test_hint", "detail": f"{repo_id} has no detected test hint", "repo_id": repo_id})
    if summary.get("expect_brigade") and not summary.get("has_brigade_config"):
        checks.append({"status": WARN, "name": "repo_missing_brigade_config", "detail": f"{repo_id} lacks local Brigade config", "repo_id": repo_id})
    if summary.get("expect_publish_guard") and not summary.get("publish_guard_hooks"):
        checks.append({"status": WARN, "name": "repo_missing_publish_guard", "detail": f"{repo_id} lacks expected publish-guard hooks", "repo_id": repo_id})
    if int(summary.get("dirty_tracked_count", 0) or 0) > 0:
        checks.append({"status": WARN, "name": "repo_dirty_tracked", "detail": f"{repo_id} has dirty tracked files", "repo_id": repo_id})
    if not summary.get("handoff_inboxes"):
        checks.append({"status": WARN, "name": "repo_missing_handoff_inbox", "detail": f"{repo_id} has no local handoff inbox", "repo_id": repo_id})
    return checks


def scan_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    entries, errors, config_loaded = _load_config(target)
    repos = [_repo_summary(entry) for entry in entries if entry.enabled]
    checks: list[dict[str, Any]] = []
    if errors:
        checks.extend({"status": WARN, "name": "repo_fleet_config", "detail": error} for error in errors)
    elif config_loaded:
        checks.append({"status": OK, "name": "repo_fleet_config", "detail": str(config_path(target))})
    for summary in repos:
        repo_checks = _repo_checks(summary)
        if repo_checks:
            checks.extend(repo_checks)
        else:
            checks.append({"status": OK, "name": "repo_ready", "detail": summary["id"], "repo_id": summary["id"]})
    issues = [check for check in checks if check["status"] != OK]
    return {
        "target": str(target),
        "config_path": str(config_path(target)),
        "config_loaded": config_loaded,
        "repos": repos,
        "repo_count": len(repos),
        "checks": checks,
        "issues": issues,
        "issue_count": len(issues),
        "top_issue": issues[0] if issues else None,
    }


def init(*, target: Path, force: bool = False, update_gitignore: bool = True, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if not target.is_dir():
        print(f"error: --target is not a directory: {target}", file=sys.stderr)
        return 2
    path = config_path(target)
    if path.exists() and not force:
        print(f"error: repo fleet config already exists: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_default_config())
    gitignore = "skipped"
    if update_gitignore:
        gitignore = apply_gitignore(target, Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[]))
    payload = {"target": str(target), "config_path": str(path), "gitignore": gitignore, "repo_count": 1}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repos_config: {path}")
    print(f"gitignore: {gitignore}")
    print("next_command: brigade repos scan")
    return 0


def list_repos(*, target: Path, json_output: bool = False) -> int:
    payload = scan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print(f"repos: {payload['target']}")
    print(f"config_path: {payload['config_path']}")
    for repo in payload["repos"]:
        print(f"- {repo['id']} [{repo['branch'] or 'unknown'}] dirty={repo['dirty_tracked_count']}")
    return 0 if payload["config_loaded"] else 1


def show(*, target: Path, repo_id: str, json_output: bool = False) -> int:
    payload = scan_payload(target)
    repo = next((item for item in payload["repos"] if item.get("id") == repo_id), None)
    if repo is None:
        print(f"error: repo not found: {repo_id}", file=sys.stderr)
        return 1
    checks = [check for check in payload["checks"] if check.get("repo_id") == repo_id]
    output = {"target": payload["target"], "repo": repo, "checks": checks}
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"repo: {repo['id']}")
    print(f"label: {repo['label']}")
    print(f"branch: {repo.get('branch') or 'unknown'}")
    print(f"guidance: {repo.get('guidance_source') or 'none'}")
    print(f"tests: {', '.join(repo.get('test_hints') or []) or 'none'}")
    for check in checks:
        if check["status"] != OK:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0


def scan(*, target: Path, json_output: bool = False) -> int:
    payload = scan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print(f"repos scan: {payload['target']}")
    print(f"repos: {payload['repo_count']}")
    print(f"issues: {payload['issue_count']}")
    for repo in payload["repos"]:
        print(f"- {repo['id']} guidance={repo.get('guidance_source') or 'none'} tests={len(repo.get('test_hints') or [])}")
    return 0 if payload["config_loaded"] else 1


def doctor(*, target: Path, json_output: bool = False) -> int:
    payload = health(target)
    scan_issue_count = sum(1 for check in payload["checks"] if isinstance(check, dict) and check.get("status") != OK)
    health_issue_count = int(payload.get("issue_count") or 0)
    checks = [*payload["checks"]]
    for bucket_name in ("report", "actions", "sweep", "release_train"):
        bucket = payload.get(bucket_name) if isinstance(payload.get(bucket_name), dict) else {}
        checks.extend(bucket.get("checks") if isinstance(bucket.get("checks"), list) else [])
    payload = {**payload, "checks": checks, "issue_count": scan_issue_count, "health_issue_count": health_issue_count}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if scan_issue_count == 0 else 1
    print(f"repos doctor: {payload['target']}")
    for check in checks:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0 if scan_issue_count == 0 else 1


def _import_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        repo_id = str(issue.get("repo_id") or "fleet")
        name = str(issue.get("name") or "repo_fleet_issue")
        detail = str(issue.get("detail") or name)
        fingerprint = work_cmd._stable_hash({"repo_id": repo_id, "name": name, "detail": detail})
        records.append(
            {
                "text": f"Resolve repository fleet issue: {detail}",
                "kind": "task",
                "source": "repo-fleet",
                "type": "docs",
                "priority": "normal",
                "template": "docs",
                "acceptance": [
                    "The repo fleet issue is resolved or explicitly deferred.",
                    "No private repository contents or paths are copied into public artifacts.",
                ],
                "metadata": {
                    "repo_id": repo_id,
                    "issue_type": name,
                    "safe_summary": detail,
                    "source_item_key": f"{repo_id}:{name}",
                    "source_fingerprint": fingerprint,
                },
            }
        )
    return records


def import_issues(*, target: Path, json_output: bool = False, dry_run: bool = False) -> int:
    payload = scan_payload(target)
    records = _import_records(payload)
    imported, skipped, dismissed = work_cmd._append_import_records(target.expanduser().resolve(), records, dry_run=dry_run)
    output = {
        "target": payload["target"],
        "created": len(imported),
        "skipped": len(skipped),
        "dismissed": len(dismissed),
        "dry_run": dry_run,
        "issue_count": payload["issue_count"],
    }
    if json_output:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    print(f"repo_fleet_imports: {payload['target']}")
    print(f"created: {len(imported)}")
    print(f"skipped: {len(skipped)}")
    print(f"dismissed: {len(dismissed)}")
    if dry_run:
        print("dry_run: true")
    return 0


def _sweeps_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "sweeps"


def _sweep_json_path(path: Path) -> Path:
    return path / "sweep.json" if path.is_dir() else path


def _read_sweep(path: Path) -> dict[str, Any] | None:
    payload = _read_json(_sweep_json_path(path))
    if payload is not None:
        payload.pop("path", None)
        payload.setdefault("path_label", _sweep_json_path(path).parent.name)
    return payload


def _sweeps(target: Path) -> list[dict[str, Any]]:
    root = _sweeps_root(target)
    sweeps: list[dict[str, Any]] = []
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir():
                payload = _read_sweep(child)
                if payload is not None:
                    sweeps.append(payload)
    sweeps.sort(key=lambda item: str(item.get("started_at") or item.get("sweep_id") or ""), reverse=True)
    return sweeps


def latest_sweep(target: Path) -> dict[str, Any] | None:
    sweeps = _sweeps(target)
    return sweeps[0] if sweeps else None


def _resolve_sweep(target: Path, sweep_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if sweep_id == "latest":
        latest = latest_sweep(target)
        return (latest, None) if latest else (None, "repo fleet sweep not found: latest")
    matches = [item for item in _sweeps(target) if str(item.get("sweep_id") or "").startswith(sweep_id)]
    if not matches:
        return None, f"repo fleet sweep not found: {sweep_id}"
    if len(matches) > 1:
        return None, f"repo fleet sweep id is ambiguous: {sweep_id}"
    return matches[0], None


def _sweep_commands() -> list[SweepCommand]:
    return [
        SweepCommand("center-report-build", [sys.executable, "-m", "brigade", "center", "report", "build", "--json"]),
        SweepCommand("release-plan", [sys.executable, "-m", "brigade", "release", "plan", "--base-ref", "", "--json"]),
        SweepCommand("work-brief", [sys.executable, "-m", "brigade", "work", "brief", "--json"]),
    ]


def _commands_for_entry(entry: RepoEntry) -> list[SweepCommand]:
    return [*_sweep_commands(), *entry.health_commands]


def _latest_sweep_for_repo(target: Path, repo_id: str) -> dict[str, Any] | None:
    for sweep in _sweeps(target):
        for result in sweep.get("repos") if isinstance(sweep.get("repos"), list) else []:
            if isinstance(result, dict) and result.get("repo_id") == repo_id and result.get("status") == "completed":
                return sweep
    return None


def _select_sweep_entries(
    target: Path,
    *,
    repo_ids: list[str] | None = None,
    include_disabled: bool = False,
    stale_only: bool = False,
    force: bool = False,
) -> tuple[list[RepoEntry], list[str], bool]:
    entries, errors, config_loaded = _load_config(target)
    wanted = set(repo_ids or [])
    selected: list[RepoEntry] = []
    for entry in entries:
        if wanted and entry.repo_id not in wanted:
            continue
        if not entry.enabled and not include_disabled:
            continue
        if stale_only and not force and _latest_sweep_for_repo(target, entry.repo_id) is not None:
            continue
        selected.append(entry)
    missing = sorted(wanted - {entry.repo_id for entry in entries})
    errors.extend(f"repo not found: {repo_id}" for repo_id in missing)
    return selected, errors, config_loaded


def _sweep_plan_payload(
    target: Path,
    *,
    repo_ids: list[str] | None = None,
    include_disabled: bool = False,
    stale_only: bool = False,
    force: bool = False,
    all_repos: bool = False,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    selected, errors, config_loaded = _select_sweep_entries(
        target,
        repo_ids=repo_ids,
        include_disabled=include_disabled,
        stale_only=stale_only,
        force=force or all_repos,
    )
    command_labels = sorted({command.label for entry in selected for command in _commands_for_entry(entry)})
    safe_errors = [_safe_text(error, target, "repo-fleet", "repo fleet") for error in errors]
    return {
        "target_label": "repo-fleet",
        "config_path_label": CONFIG_REL_PATH,
        "config_loaded": config_loaded,
        "errors": safe_errors,
        "mode": "all" if all_repos else ("stale-only" if stale_only else "selected"),
        "repos": [
            {
                "repo_id": entry.repo_id,
                "repo_label": entry.label,
                "enabled": entry.enabled,
                "exists": entry.path.is_dir(),
                "stale": _latest_sweep_for_repo(target, entry.repo_id) is None,
                "commands": [{"label": command.label, "timeout": command.timeout} for command in _commands_for_entry(entry)],
            }
            for entry in selected
        ],
        "repo_count": len(selected),
        "command_labels": command_labels,
    }


def sweep_plan(
    *,
    target: Path,
    repo_ids: list[str] | None = None,
    all_repos: bool = False,
    stale_only: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    payload = _sweep_plan_payload(target, repo_ids=repo_ids, include_disabled=include_disabled, stale_only=stale_only, force=force, all_repos=all_repos)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] and not payload["errors"] else 1
    print(f"repo fleet sweep plan: {payload['target_label']}")
    print(f"repos: {payload['repo_count']}")
    for repo in payload["repos"]:
        labels = ",".join(command["label"] for command in repo.get("commands", []))
        print(f"- {repo['repo_id']} {repo['repo_label']} commands={labels}")
    for error in payload["errors"]:
        print(f"[warn] {error}")
    return 0 if payload["config_loaded"] and not payload["errors"] else 1


def _summarize_output(text: str, repo_path: Path, repo_id: str, label: str, limit: int = 240) -> str:
    safe = _safe_text(text.replace("\n", " "), repo_path, repo_id, label).strip()
    return work_cmd._short(safe, limit)


def _run_sweep_command(entry: RepoEntry, command: SweepCommand, sweep_dir: Path) -> dict[str, Any]:
    started = _now()
    command_dir = sweep_dir / "logs" / entry.repo_id / command.label
    command_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    source_path = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(source_path) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        result = subprocess.run(
            command.argv,
            cwd=entry.path,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=command.timeout,
            env=env,
        )
        exit_code = result.returncode
        timed_out = False
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    completed = _now()
    (command_dir / "stdout.log").write_text(stdout)
    (command_dir / "stderr.log").write_text(stderr)
    return {
        "label": command.label,
        "argv_label": command.label,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": round((completed - started).total_seconds(), 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "status": "timeout" if timed_out else ("completed" if exit_code == 0 else "failed"),
        "stdout_summary": _summarize_output(stdout, entry.path, entry.repo_id, entry.label),
        "stderr_summary": _summarize_output(stderr, entry.path, entry.repo_id, entry.label),
        "stdout_log_label": f"{entry.repo_id}/{command.label}/stdout.log",
        "stderr_log_label": f"{entry.repo_id}/{command.label}/stderr.log",
    }


def sweep_run(
    *,
    target: Path,
    repo_ids: list[str] | None = None,
    all_repos: bool = False,
    stale_only: bool = False,
    include_disabled: bool = False,
    force: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    plan = _sweep_plan_payload(target, repo_ids=repo_ids, include_disabled=include_disabled, stale_only=stale_only, force=force, all_repos=all_repos)
    selected, errors, config_loaded = _select_sweep_entries(
        target,
        repo_ids=repo_ids,
        include_disabled=include_disabled,
        stale_only=stale_only,
        force=force or all_repos,
    )
    started = _now()
    sweep_id = f"{started.strftime('%Y%m%d-%H%M%S')}-repo-fleet-sweep-{uuid4().hex[:6]}"
    sweep_dir = _sweeps_root(target) / sweep_id
    repo_results: list[dict[str, Any]] = []
    for entry in selected:
        repo_started = _now()
        if not entry.path.is_dir():
            repo_results.append(
                {
                    "repo_id": entry.repo_id,
                    "repo_label": entry.label,
                    "status": "failed",
                    "started_at": repo_started.isoformat(),
                    "completed_at": _now().isoformat(),
                    "commands": [],
                    "warning_count": 1,
                    "blocker_count": 0,
                    "warnings": [{"name": "repo_missing", "detail": f"{entry.repo_id} is not reachable"}],
                    "receipt_labels": [],
                }
            )
            continue
        command_results = [_run_sweep_command(entry, command, sweep_dir) for command in _commands_for_entry(entry)]
        repo_completed = _now()
        failed = [command for command in command_results if command.get("status") != "completed"]
        state = _repo_brigade_state(entry)
        receipt_labels = []
        latest_report_ref = state.get("latest_operator_report") if isinstance(state.get("latest_operator_report"), dict) else None
        if latest_report_ref:
            receipt_labels.append({"repo_id": entry.repo_id, "repo_label": entry.label, "kind": "operator-report", "id": latest_report_ref.get("id")})
        latest_release = state.get("latest_release_readiness") if isinstance(state.get("latest_release_readiness"), dict) else None
        if latest_release:
            receipt_labels.append({"repo_id": entry.repo_id, "repo_label": entry.label, "kind": "release-readiness", "id": latest_release.get("id")})
        repo_results.append(
            {
                "repo_id": entry.repo_id,
                "repo_label": entry.label,
                "status": "completed" if not failed else "failed",
                "started_at": repo_started.isoformat(),
                "completed_at": repo_completed.isoformat(),
                "duration_seconds": round((repo_completed - repo_started).total_seconds(), 3),
                "commands": command_results,
                "warning_count": len(state.get("warnings") if isinstance(state.get("warnings"), list) else []),
                "blocker_count": len(state.get("blockers") if isinstance(state.get("blockers"), list) else []),
                "warnings": state.get("warnings") if isinstance(state.get("warnings"), list) else [],
                "receipt_labels": receipt_labels,
            }
        )
    completed = _now()
    failed_count = sum(1 for repo in repo_results if repo.get("status") != "completed")
    payload = {
        "sweep_id": sweep_id,
        "target_label": "repo-fleet",
        "path_label": sweep_id,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": round((completed - started).total_seconds(), 3),
        "status": "completed" if failed_count == 0 and not errors and config_loaded else "failed",
        "config_loaded": config_loaded,
        "errors": plan.get("errors", errors),
        "plan": plan,
        "repos": repo_results,
        "repo_count": len(repo_results),
        "failed_count": failed_count,
        "suggested_next_commands": [
            "brigade repos report build",
            "brigade repos report closeout latest",
            "brigade repos actions build latest",
        ],
    }
    _write_json(sweep_dir / "sweep.json", payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["status"] == "completed" else 1
    print(f"repo fleet sweep: {sweep_id}")
    print(f"status: {payload['status']}")
    print(f"repos: {payload['repo_count']}")
    print(f"failed: {failed_count}")
    print(f"path_label: {sweep_id}")
    return 0 if payload["status"] == "completed" else 1


def sweep_runs(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    sweeps = _sweeps(target)[:limit]
    payload = {"target_label": "repo-fleet", "sweeps_root_label": ".brigade/repos/sweeps", "sweeps": sweeps, "sweep_count": len(sweeps)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet sweeps: repo-fleet")
    for sweep in sweeps:
        print(f"- {sweep.get('sweep_id')} [{sweep.get('status')}] repos={sweep.get('repo_count')} {sweep.get('started_at')}")
    return 0


def sweep_show(*, target: Path, sweep_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    sweep, error = _resolve_sweep(target, sweep_id)
    if sweep is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "sweep": sweep}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet sweep: {sweep.get('sweep_id')}")
    print(f"status: {sweep.get('status')}")
    print(f"repos: {sweep.get('repo_count')}")
    print(f"path_label: {sweep.get('path_label')}")
    return 0


def sweep_closeout(*, target: Path, sweep_id: str = "latest", status: str = "reviewed", reason: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if status not in {"reviewed", "deferred", "superseded", "archived"}:
        print("error: --status must be one of reviewed, deferred, superseded, archived", file=sys.stderr)
        return 2
    sweep, error = _resolve_sweep(target, sweep_id)
    if sweep is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    sweep_path = _sweeps_root(target) / str(sweep.get("sweep_id") or "")
    if not sweep_path.is_dir():
        print(f"error: repo fleet sweep path is missing: {sweep.get('sweep_id')}", file=sys.stderr)
        return 2
    payload = {
        "target_label": "repo-fleet",
        "sweep_id": sweep.get("sweep_id"),
        "status": status,
        "reason": reason or f"repo fleet sweep marked {status}",
        "reviewed_at": _now().isoformat(),
        "source_fingerprint": _fingerprint_payload({"sweep_id": sweep.get("sweep_id"), "repos": sweep.get("repos")}),
    }
    closeout_path = sweep_path / "CLOSEOUT.json"
    payload["path_label"] = f"{sweep.get('sweep_id')}:CLOSEOUT.json"
    _write_json(closeout_path, payload)
    sweep["closeout"] = payload
    _write_json(sweep_path / "sweep.json", sweep)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet sweep closeout: {sweep.get('sweep_id')}")
    print(f"status: {status}")
    return 0


def sweep_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = latest_sweep(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append({"status": WARN, "name": "repo_fleet_sweep_missing", "detail": "no repo fleet sweep has been run", "suggested_next_command": "brigade repos sweep run"})
        return {"latest": None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    if not closeout or closeout.get("status") not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append({"status": WARN, "name": "repo_fleet_sweep_unclosed", "detail": f"{latest.get('sweep_id')} has not been closed out", "suggested_next_command": f"brigade repos sweep closeout {latest.get('sweep_id')}"})
    if latest.get("status") != "completed":
        checks.append({"status": WARN, "name": "repo_fleet_sweep_failed", "detail": f"{latest.get('sweep_id')} did not complete", "suggested_next_command": f"brigade repos sweep show {latest.get('sweep_id')}"})
    return {"latest": latest, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


def _reports_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "reports"


def _reports_archive_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "reports-archive"


def _report_json_path(path: Path) -> Path:
    return path / "FLEET_EVIDENCE.json" if path.is_dir() else path


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
            if not child.is_dir():
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
    if report_id == "latest":
        latest = latest_report(target)
        return (latest, None) if latest else (None, "fleet report not found: latest")
    matches = [item for item in _reports(target, include_archived=True) if str(item.get("report_id") or "").startswith(report_id)]
    if not matches:
        return None, f"fleet report not found: {report_id}"
    if len(matches) > 1:
        return None, f"fleet report id is ambiguous: {report_id}"
    return matches[0], None


def _report_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    entries, errors, config_loaded = _load_config(target)
    repo_states = [_repo_brigade_state(entry) for entry in entries if entry.enabled]
    sweep = sweep_health(target)
    blockers = [item for repo in repo_states for item in repo.get("blockers", []) if isinstance(item, dict)]
    warnings = [item for repo in repo_states for item in repo.get("warnings", []) if isinstance(item, dict)]
    receipt_refs = [ref for repo in repo_states for ref in repo.get("receipt_references", []) if isinstance(ref, dict)]
    payload = {
        "schema_version": 1,
        "target": str(target),
        "config_path": str(config_path(target)),
        "config_loaded": config_loaded,
        "config_errors": errors,
        "generated_at": _now().isoformat(),
        "repo_count": len(repo_states),
        "repos": repo_states,
        "blocker_count": len(blockers),
        "warning_count": len(warnings) + len(errors),
        "blockers": blockers,
        "warnings": warnings + [{"name": "repo_fleet_config", "detail": error} for error in errors],
        "receipt_references": receipt_refs,
        "latest_sweep": _safe_sweep_ref(sweep.get("latest") if isinstance(sweep.get("latest"), dict) else None),
        "sweep_health": {"issue_count": sweep.get("issue_count"), "top_issue": sweep.get("top_issue")},
        "suggested_next_commands": [repo.get("suggested_command") for repo in repo_states if repo.get("suggested_command")],
    }
    payload["report_fingerprint"] = _fingerprint_payload(
        {
            "repos": repo_states,
            "warnings": payload["warnings"],
            "blockers": blockers,
            "receipts": receipt_refs,
        }
    )
    return payload


def _safe_sweep_ref(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "sweep_id": payload.get("sweep_id"),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "completed_at": payload.get("completed_at"),
        "repo_count": payload.get("repo_count"),
        "failed_count": payload.get("failed_count"),
    }


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Repo Fleet Report",
        "",
        f"- Report: `{payload.get('report_id', 'planned')}`",
        f"- Generated: {payload.get('generated_at')}",
        f"- Repos: {payload.get('repo_count')}",
        f"- Warnings: {payload.get('warning_count')}",
        f"- Blockers: {payload.get('blocker_count')}",
        "",
        "## Repos",
        "",
    ]
    repos = payload.get("repos") if isinstance(payload.get("repos"), list) else []
    for repo in repos:
        lines.append(f"- `{repo.get('repo_id')}` {repo.get('repo_label')} warnings={len(repo.get('warnings') if isinstance(repo.get('warnings'), list) else [])} blockers={len(repo.get('blockers') if isinstance(repo.get('blockers'), list) else [])}")
        top = repo.get("action_queue") if isinstance(repo.get("action_queue"), dict) else {}
        top_action = top.get("top_action") if isinstance(top.get("top_action"), dict) else None
        if top_action:
            lines.append(f"  - top action: `{top_action.get('action_id')}` {top_action.get('safe_summary')}")
        if repo.get("suggested_command"):
            lines.append(f"  - next: `{repo.get('suggested_command')}`")
    if not repos:
        lines.append("- none")
    lines.extend(["", "## Boundaries", "", "- local report only", "- no cloning", "- no remote mutation", "- no automatic action execution"])
    return "\n".join(lines) + "\n"


def _write_report_bundle(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path / "FLEET_EVIDENCE.json", payload)
    (path / "FLEET_REPORT.md").write_text(_report_markdown(payload))


def report_plan(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _report_payload(target)
    payload.update({"report_id": "planned", "reports_root": str(_reports_root(target)), "bundle_files": ["FLEET_REPORT.md", "FLEET_EVIDENCE.json"]})
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print(f"repo fleet report plan: {target}")
    print(f"repos: {payload['repo_count']}")
    print(f"warnings: {payload['warning_count']}")
    print(f"reports_root: {payload['reports_root']}")
    return 0 if payload["config_loaded"] else 1


def report_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    payload = _report_payload(target)
    created = _now()
    report_id = f"{created.strftime('%Y%m%d-%H%M%S')}-repo-fleet-report-{uuid4().hex[:6]}"
    report_dir = _reports_root(target) / report_id
    payload.update({"report_id": report_id, "created_at": created.isoformat(), "path": str(report_dir), "bundle_files": ["FLEET_REPORT.md", "FLEET_EVIDENCE.json"]})
    _write_report_bundle(report_dir, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print(f"repo fleet report: {report_id}")
    print(f"repos: {payload['repo_count']}")
    print(f"warnings: {payload['warning_count']}")
    print(f"path: {report_dir}")
    return 0 if payload["config_loaded"] else 1


def report_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    reports = _reports(target)[:limit]
    payload = {"target": str(target), "reports_root": str(_reports_root(target)), "reports": reports, "report_count": len(reports)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet reports: {target}")
    for report in reports:
        print(f"- {report.get('report_id')} repos={report.get('repo_count')} warnings={report.get('warning_count')} {report.get('created_at')}")
    return 0


def report_show(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target": str(target), "report": report}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet report: {report.get('report_id')}")
    print(f"repos: {report.get('repo_count')}")
    print(f"warnings: {report.get('warning_count')}")
    print(f"path: {report.get('path')}")
    return 0


def report_archive(*, target: Path, report_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    source = Path(str(report.get("path") or _reports_root(target) / str(report.get("report_id"))))
    if not source.is_dir():
        print(f"error: fleet report path is missing: {source}", file=sys.stderr)
        return 2
    destination = _reports_archive_root(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"error: archived fleet report already exists: {destination}", file=sys.stderr)
        return 2
    shutil.move(str(source), str(destination))
    payload = {"target": str(target), "report_id": report.get("report_id"), "status": "archived", "archive_path": str(destination)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived repo fleet report: {report.get('report_id')}")
    print(f"path: {destination}")
    return 0


def report_closeout(*, target: Path, report_id: str = "latest", status: str = "reviewed", reason: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if status not in {"reviewed", "deferred", "superseded", "archived"}:
        print("error: --status must be one of reviewed, deferred, superseded, archived", file=sys.stderr)
        return 2
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    report_path = Path(str(report.get("path") or ""))
    if not report_path.is_dir():
        print(f"error: fleet report path is missing: {report.get('path')}", file=sys.stderr)
        return 2
    payload = {
        "target": str(target),
        "report_id": report.get("report_id"),
        "status": status,
        "reason": reason or f"repo fleet report marked {status}",
        "reviewed_at": _now().isoformat(),
        "report_fingerprint": report.get("report_fingerprint"),
    }
    closeout_path = report_path / "CLOSEOUT.json"
    payload["path"] = str(closeout_path)
    _write_json(closeout_path, payload)
    report["closeout"] = payload
    _write_json(report_path / "FLEET_EVIDENCE.json", report)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet report closeout: {report.get('report_id')}")
    print(f"status: {status}")
    return 0


def _actions_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "actions"


def _actions_path(target: Path) -> Path:
    return _actions_root(target) / "actions.json"


def _actions_archive_path(target: Path) -> Path:
    return _actions_root(target) / "archive.jsonl"


def _read_actions(target: Path) -> list[dict[str, Any]]:
    payload = _read_json(_actions_path(target))
    actions = payload.get("actions") if isinstance(payload, dict) else None
    return [item for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []


def _write_actions(target: Path, actions: list[dict[str, Any]]) -> None:
    _write_json(_actions_path(target), {"updated_at": _now().isoformat(), "actions": actions})


def _read_action_archive(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_actions_archive_path(target))


def _action_target_entry(target: Path, action: dict[str, Any]) -> tuple[RepoEntry | None, str | None]:
    repo_id = str(action.get("repo_id") or "")
    entries, errors, config_loaded = _load_config(target)
    if not config_loaded:
        return None, "repo fleet config is missing"
    if errors:
        return None, "; ".join(_safe_text(error, target, "repo-fleet", "repo fleet") for error in errors)
    for entry in entries:
        if entry.repo_id == repo_id:
            if not entry.path.is_dir():
                return None, f"target repo is not reachable: {repo_id}"
            return entry, None
    return None, f"repo not found: {repo_id}"


def _action_acceptance(action: dict[str, Any]) -> list[str]:
    summary = str(action.get("safe_summary") or "repo fleet action")
    return [
        "The target repo issue is resolved or explicitly deferred with rationale.",
        "Relevant local verification, review, or closeout evidence is captured in the target repo when applicable.",
        "No private repo names, paths, raw logs, scanner output, secrets, or guidance file contents are copied into public artifacts.",
        f"Fleet action remains traceable from {action.get('fleet_action_id')}: {work_cmd._short(summary, 120)}",
    ]


def _action_task_fields(action: dict[str, Any]) -> tuple[str, str, str]:
    subsystem = str(action.get("source_subsystem") or "")
    if subsystem in {"security", "security-scan"}:
        return "security", "high", "security-follow-up"
    if subsystem in {"code-review", "review-finding"}:
        return "bug", "high", "bugfix"
    if subsystem in {"handoff", "memory-care", "context"}:
        return "docs", "normal", "docs"
    return "docs", "normal", "docs"


def _action_import_record(action: dict[str, Any]) -> dict[str, Any]:
    task_type, priority, template = _action_task_fields(action)
    action_id = str(action.get("fleet_action_id") or "fleet-action")
    source_fingerprint = str(action.get("source_fingerprint") or _fingerprint_payload(action))
    metadata = {
        "fleet_action_id": action_id,
        "source_item_key": action_id,
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "source_report_id": action.get("source_report_id"),
        "source_report_fingerprint": action.get("source_report_fingerprint"),
        "source_sweep_id": action.get("source_sweep_id"),
        "source_subsystem": action.get("source_subsystem"),
        "source_local_id": action.get("source_local_id"),
        "source_fingerprint": source_fingerprint,
        "suggested_command": action.get("suggested_command"),
        "safe_summary": action.get("safe_summary"),
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
    return {
        "text": f"Resolve repo fleet action {action_id}: {work_cmd._short(str(action.get('safe_summary') or action_id), 180)}",
        "kind": "task",
        "source": "repo-fleet",
        "type": task_type,
        "priority": priority,
        "template": template,
        "acceptance": _action_acceptance(action),
        "metadata": metadata,
    }


def _target_imports_for_action(repo_path: Path, action: dict[str, Any]) -> list[dict[str, Any]]:
    action_id = str(action.get("fleet_action_id") or "")
    matches: list[dict[str, Any]] = []
    for item in work_cmd._read_imports(repo_path):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("fleet_action_id") == action_id:
            matches.append(item)
    matches.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("id") or ""), reverse=True)
    return matches


def _supersede_prior_dispatch_imports(repo_path: Path, action: dict[str, Any], current_import_ids: set[str]) -> list[str]:
    source_fingerprint = str(action.get("source_fingerprint") or _fingerprint_payload(action))
    imports = work_cmd._read_imports(repo_path)
    superseded: list[str] = []
    changed = False
    now = _now().isoformat()
    for item in imports:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("fleet_action_id") != action.get("fleet_action_id"):
            continue
        if item.get("id") in current_import_ids:
            continue
        if metadata.get("source_fingerprint") == source_fingerprint:
            continue
        if item.get("status") == "superseded":
            continue
        item["status"] = "superseded"
        item["updated_at"] = now
        item["superseded_at"] = now
        item["superseded_by"] = sorted(current_import_ids)[0] if current_import_ids else None
        superseded.append(str(item.get("id")))
        changed = True
    if changed:
        work_cmd._write_imports(repo_path, imports)
    return superseded


def _dispatch_state(action: dict[str, Any], repo_path: Path | None = None) -> dict[str, Any]:
    dispatch = action.get("dispatch") if isinstance(action.get("dispatch"), dict) else {}
    return {
        "status": action.get("resolution_status") or dispatch.get("status"),
        "target_import_id": dispatch.get("target_import_id") or action.get("target_import_id"),
        "target_task_id": dispatch.get("target_task_id") or action.get("target_task_id"),
        "dispatched_at": dispatch.get("dispatched_at"),
        "reconciled_at": action.get("reconciled_at"),
        "repo_label": action.get("repo_label"),
        "repo_id": action.get("repo_id"),
        "repo_path_label": f"{action.get('repo_id')}:.brigade",
    }


def _actions_for_dispatch(target: Path, *, action_id: str | None = None, all_reviewed: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    actions = _read_actions(target)
    if action_id:
        matches = [action for action in actions if str(action.get("fleet_action_id") or "").startswith(action_id)]
        if not matches:
            return actions, [], f"fleet action not found: {action_id}"
        if len(matches) > 1:
            return actions, [], f"fleet action id is ambiguous: {action_id}"
        return actions, [matches[0]], None
    if all_reviewed:
        selected = [
            action
            for action in actions
            if action.get("reviewed_at") and action.get("status") in {"pending", "active"}
        ]
        return actions, selected, None
    return actions, [], "fleet action id is required unless --all-reviewed is passed"


def _dispatch_plan_for_action(target: Path, action: dict[str, Any], *, include_deferred: bool = False) -> dict[str, Any]:
    entry, error = _action_target_entry(target, action)
    blockers: list[str] = []
    if error:
        blockers.append(error)
    status = str(action.get("status") or "")
    if status == "deferred" and not include_deferred:
        blockers.append("deferred actions require --include-deferred")
    elif status not in {"reviewed", "pending", "active", "deferred"}:
        blockers.append(f"action status is not dispatchable: {status or 'unknown'}")
    record = _action_import_record(action)
    if entry is not None:
        record["text"] = _safe_text(record.get("text"), entry.path, entry.repo_id, entry.label)
        record["acceptance"] = [_safe_text(item, entry.path, entry.repo_id, entry.label) for item in record.get("acceptance", [])]
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        for key in ("safe_summary", "suggested_command"):
            if key in metadata:
                metadata[key] = _safe_text(metadata[key], entry.path, entry.repo_id, entry.label)
    existing_imports = _target_imports_for_action(entry.path, action) if entry is not None else []
    same_fingerprint = []
    changed_fingerprint = []
    dismissed_same_fingerprint = []
    wanted_fingerprint = record["metadata"].get("source_fingerprint")
    for item in existing_imports:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if metadata.get("source_fingerprint") == wanted_fingerprint:
            if item.get("status") == "dismissed":
                dismissed_same_fingerprint.append(item.get("id"))
            else:
                same_fingerprint.append(item.get("id"))
        else:
            changed_fingerprint.append(item.get("id"))
    return {
        "fleet_action_id": action.get("fleet_action_id"),
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "target_repo_label": action.get("repo_label"),
        "target_repo_id": action.get("repo_id"),
        "target_inbox_label": f"{action.get('repo_id')}:.brigade/work/imports/inbox.jsonl",
        "action_status": action.get("status"),
        "dispatchable": not blockers,
        "blockers": blockers,
        "record": record,
        "existing_same_fingerprint_import_ids": same_fingerprint,
        "existing_changed_fingerprint_import_ids": changed_fingerprint,
        "dismissed_same_fingerprint_import_ids": dismissed_same_fingerprint,
        "suggested_next_command": f"brigade work import plan <import-id>",
    }


def actions_dispatch_plan(
    *,
    target: Path,
    action_id: str | None = None,
    all_reviewed: bool = False,
    include_deferred: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    _, selected, error = _actions_for_dispatch(target, action_id=action_id, all_reviewed=all_reviewed)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1 if "not found" in error else 2
    plans = [_dispatch_plan_for_action(target, action, include_deferred=include_deferred) for action in selected]
    payload = {"target": str(target), "plans": plans, "plan_count": len(plans), "blocker_count": sum(len(plan["blockers"]) for plan in plans)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["blocker_count"] == 0 else 2
    print("repo fleet action dispatch plan")
    print(f"actions: {len(plans)}")
    for plan in plans:
        print(f"- {plan.get('fleet_action_id')} {plan.get('repo_id')} dispatchable={plan.get('dispatchable')}")
    return 0 if payload["blocker_count"] == 0 else 2


def actions_dispatch_apply(
    *,
    target: Path,
    action_id: str | None = None,
    all_reviewed: bool = False,
    include_deferred: bool = False,
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    target = target.expanduser().resolve()
    actions, selected, error = _actions_for_dispatch(target, action_id=action_id, all_reviewed=all_reviewed)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1 if "not found" in error else 2
    results: list[dict[str, Any]] = []
    now = _now().isoformat()
    changed = False
    for action in selected:
        plan = _dispatch_plan_for_action(target, action, include_deferred=include_deferred)
        if plan["blockers"]:
            results.append({"fleet_action_id": action.get("fleet_action_id"), "status": "blocked", "blockers": plan["blockers"]})
            continue
        entry, _ = _action_target_entry(target, action)
        assert entry is not None
        imported, skipped, skipped_dismissed = work_cmd._append_import_records(entry.path, [plan["record"]], dry_run=dry_run)
        imported_ids = {str(item.get("id")) for item in imported if item.get("id")}
        skipped_ids = {str(item_id) for item_id in plan.get("existing_same_fingerprint_import_ids", []) if item_id}
        dismissed_ids = {str(item_id) for item_id in plan.get("dismissed_same_fingerprint_import_ids", []) if item_id}
        superseded_ids = [] if dry_run else _supersede_prior_dispatch_imports(entry.path, action, imported_ids)
        target_import_id = next(iter(imported_ids or skipped_ids or dismissed_ids), None)
        status = "dry-run" if dry_run else ("created" if imported else ("dismissed" if skipped_dismissed else "skipped"))
        if not dry_run:
            action["dispatch"] = {
                "status": "dispatched" if imported or skipped else "dismissed" if skipped_dismissed else "dispatched",
                "target_import_id": target_import_id,
                "target_inbox_label": f"{action.get('repo_id')}:.brigade/work/imports/inbox.jsonl",
                "dispatched_at": now,
                "source_fingerprint": action.get("source_fingerprint"),
                "superseded_import_ids": superseded_ids,
                "target_evidence_fingerprint": _fingerprint_payload(_latest_safe_receipts(entry.path, entry.repo_id, entry.label)),
            }
            action["resolution_status"] = "dispatched" if imported or skipped else "dismissed" if skipped_dismissed else "dispatched"
            action["updated_at"] = now
            changed = True
        results.append(
            {
                "fleet_action_id": action.get("fleet_action_id"),
                "repo_id": action.get("repo_id"),
                "repo_label": action.get("repo_label"),
                "status": status,
                "imported_count": len(imported),
                "skipped_count": len(skipped),
                "dismissed_count": len(skipped_dismissed),
                "target_import_id": target_import_id,
                "superseded_import_ids": superseded_ids,
            }
        )
    if changed:
        _write_actions(target, actions)
    payload = {
        "target": str(target),
        "dry_run": dry_run,
        "result_count": len(results),
        "created_count": sum(1 for item in results if item.get("status") == "created"),
        "skipped_count": sum(1 for item in results if item.get("status") == "skipped"),
        "dismissed_count": sum(1 for item in results if item.get("status") == "dismissed"),
        "blocked_count": sum(1 for item in results if item.get("status") == "blocked"),
        "results": results,
    }
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["blocked_count"] == 0 else 2
    print("repo fleet action dispatch apply")
    print(f"results: {len(results)}")
    print(f"created: {payload['created_count']}")
    print(f"blocked: {payload['blocked_count']}")
    return 0 if payload["blocked_count"] == 0 else 2


def _latest_safe_receipts(repo_path: Path, repo_id: str, label: str) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for root, filename, kind in (
        (repo_path / ".brigade" / "center" / "reports", "CENTER_EVIDENCE.json", "operator-report"),
        (repo_path / ".brigade" / "work" / "closeouts", "closeout.json", "work-closeout"),
        (repo_path / ".brigade" / "release" / "runs", "release.json", "release-readiness"),
    ):
        receipt = _safe_receipt(_latest_json(root, filename), repo_id, label)
        if receipt:
            receipt["kind"] = kind
            receipts.append(receipt)
    return receipts


def _action_context_payload(target: Path, action: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    entry, error = _action_target_entry(target, action)
    if error or entry is None:
        return None, error
    guidance = {
        "has_agents": (entry.path / "AGENTS.md").is_file(),
        "has_claude": (entry.path / "CLAUDE.md").is_file() or (entry.path / ".claude" / "CLAUDE.md").is_file(),
        "source_labels": [
            name
            for name in ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md")
            if (entry.path / name).is_file()
        ],
    }
    payload = {
        "kind": "repo-fleet-action",
        "fleet_action_id": action.get("fleet_action_id"),
        "repo_id": action.get("repo_id"),
        "repo_label": action.get("repo_label"),
        "safe_summary": _safe_text(action.get("safe_summary"), entry.path, entry.repo_id, entry.label),
        "suggested_command": _safe_text(action.get("suggested_command"), entry.path, entry.repo_id, entry.label),
        "acceptance": [_safe_text(item, entry.path, entry.repo_id, entry.label) for item in _action_acceptance(action)],
        "guidance_presence": guidance,
        "latest_receipts": _latest_safe_receipts(entry.path, entry.repo_id, entry.label),
        "dispatch": _dispatch_state(action, entry.path),
        "excluded_private_evidence": [
            "raw guidance file contents",
            "raw scanner output",
            "raw local logs",
            "private absolute paths",
            "exact private repo names",
            "owner names and organization names",
            "hostnames and secrets",
        ],
        "source_references": [
            {"label": f"{entry.repo_id}:AGENTS.md", "exists": guidance["has_agents"]},
            {"label": f"{entry.repo_id}:.brigade/work/imports/inbox.jsonl", "exists": work_cmd._imports_path(entry.path).is_file()},
        ],
        "checks": [{"status": OK, "name": "repo_fleet_action_context", "detail": "ready"}],
    }
    return payload, None


def actions_context_plan(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    payload, context_error = _action_context_payload(target, action)
    if context_error or payload is None:
        print(f"error: {context_error}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet action context plan: {payload.get('fleet_action_id')}")
    print(f"repo: {payload.get('repo_id')} {payload.get('repo_label')}")
    print("writes: 0")
    return 0


def actions_context_build(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    entry, entry_error = _action_target_entry(target, action)
    payload, context_error = _action_context_payload(target, action)
    if entry_error or context_error or entry is None or payload is None:
        print(f"error: {entry_error or context_error}", file=sys.stderr)
        return 2
    now = _now()
    pack_id = f"{now.strftime('%Y%m%d-%H%M%S')}-fleet-action-context-{uuid4().hex[:6]}"
    payload.update({"pack_id": pack_id, "status": "built", "created_at": now.isoformat(), "path_label": f"{entry.repo_id}:.brigade/context/packs/{pack_id}"})
    pack_dir = entry.path / ".brigade" / "context" / "packs" / pack_id
    _write_json(pack_dir / "context.json", payload)
    markdown = [
        f"# Fleet Action Context {pack_id}",
        "",
        f"- repo: {payload.get('repo_id')} {payload.get('repo_label')}",
        f"- action: {payload.get('fleet_action_id')}",
        f"- summary: {payload.get('safe_summary')}",
        "",
        "## Acceptance",
        *[f"- {item}" for item in payload["acceptance"]],
        "",
        "## Excluded Private Evidence",
        *[f"- {item}" for item in payload["excluded_private_evidence"]],
        "",
    ]
    (pack_dir / "CONTEXT.md").write_text("\n".join(markdown))
    action["context_pack"] = {"pack_id": pack_id, "path_label": payload["path_label"], "created_at": payload["created_at"]}
    action["updated_at"] = payload["created_at"]
    _write_actions(target, actions)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet action context: {pack_id}")
    print(f"path_label: {payload['path_label']}")
    return 0


def _task_by_id(repo_path: Path, task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    for task in work_cmd._read_task_ledger(repo_path).get("tasks", []):
        if isinstance(task, dict) and task.get("id") == task_id:
            return task
    return None


def _reconcile_one(target: Path, action: dict[str, Any]) -> dict[str, Any]:
    entry, error = _action_target_entry(target, action)
    now = _now().isoformat()
    if error or entry is None:
        action["resolution_status"] = "broken-reference"
        action["reconciled_at"] = now
        action["updated_at"] = now
        return {"fleet_action_id": action.get("fleet_action_id"), "status": "broken-reference", "detail": error or "target repo missing"}
    dispatch = action.get("dispatch") if isinstance(action.get("dispatch"), dict) else {}
    imports = _target_imports_for_action(entry.path, action)
    target_import = None
    target_import_id = dispatch.get("target_import_id")
    if target_import_id:
        target_import = next((item for item in imports if item.get("id") == target_import_id), None)
    if target_import is None and imports:
        target_import = imports[0]
    if target_import is None:
        status = "broken-reference" if dispatch else "stale"
        action["resolution_status"] = status
        action["reconciled_at"] = now
        action["updated_at"] = now
        return {"fleet_action_id": action.get("fleet_action_id"), "status": status, "detail": "target import not found"}
    task = _task_by_id(entry.path, target_import.get("task_id") if isinstance(target_import.get("task_id"), str) else None)
    if target_import.get("status") == "superseded":
        status = "superseded"
    elif target_import.get("status") == "dismissed":
        status = "dismissed"
    elif task and task.get("status") == "done":
        status = "completed"
    elif target_import.get("status") == "promoted":
        status = "in-progress"
    elif target_import.get("status") == "pending":
        created = _parse_time(str(target_import.get("created_at") or ""))
        if created and (_now() - created).total_seconds() / 3600 > DISPATCH_STALE_HOURS:
            status = "stale"
        else:
            status = "dispatched"
    else:
        status = str(target_import.get("status") or "dispatched")
    latest_closeout = _safe_receipt(_latest_json(entry.path / ".brigade" / "work" / "closeouts", "closeout.json"), entry.repo_id, entry.label)
    latest_release = _safe_receipt(_latest_json(entry.path / ".brigade" / "release" / "runs", "release.json"), entry.repo_id, entry.label)
    result = {
        "fleet_action_id": action.get("fleet_action_id"),
        "status": status,
        "target_import_id": target_import.get("id"),
        "target_import_status": target_import.get("status"),
        "target_task_id": target_import.get("task_id"),
        "target_task_status": task.get("status") if isinstance(task, dict) else None,
        "completion": task.get("completion") if isinstance(task, dict) and isinstance(task.get("completion"), dict) else None,
        "closeout": latest_closeout,
        "release": latest_release,
        "reconciled_at": now,
    }
    action["resolution_status"] = status
    action["reconciled_at"] = now
    action["target_import_id"] = target_import.get("id")
    if target_import.get("task_id"):
        action["target_task_id"] = target_import.get("task_id")
    if latest_closeout:
        action["target_closeout"] = latest_closeout
    if latest_release:
        action["target_release"] = latest_release
    if isinstance(task, dict) and isinstance(task.get("completion"), dict):
        action["completion"] = task.get("completion")
    if status == "completed":
        action["status"] = "done"
        action["completed_at"] = now
    action["updated_at"] = now
    return result


def actions_reconcile(*, target: Path, action_id: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    selected = actions
    if action_id:
        selected = [action for action in actions if str(action.get("fleet_action_id") or "").startswith(action_id)]
        if not selected:
            print(f"error: fleet action not found: {action_id}", file=sys.stderr)
            return 1
        if len(selected) > 1:
            print(f"error: fleet action id is ambiguous: {action_id}", file=sys.stderr)
            return 2
    results = [_reconcile_one(target, action) for action in selected]
    _write_actions(target, actions)
    payload = {"target": str(target), "results": results, "result_count": len(results)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet actions reconcile")
    for result in results:
        print(f"- {result.get('fleet_action_id')} [{result.get('status')}]")
    return 0


def _release_trains_root(target: Path) -> Path:
    return target / ".brigade" / "repos" / "releases"


def _release_trains_archive_root(target: Path) -> Path:
    return _release_trains_root(target) / "archive"


def _train_json_path(path: Path) -> Path:
    return path / "FLEET_RELEASE_EVIDENCE.json" if path.is_dir() else path


def _read_train(path: Path) -> dict[str, Any] | None:
    payload = _read_json(_train_json_path(path))
    if payload is not None:
        payload.pop("path", None)
        payload.setdefault("path_label", _train_json_path(path).parent.name)
    return payload


def _release_trains(target: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    roots = [_release_trains_root(target)]
    if include_archived:
        roots.append(_release_trains_archive_root(target))
    trains: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name == "archive" or not child.is_dir():
                continue
            train = _read_train(child)
            if train is not None:
                trains.append(train)
    trains.sort(key=lambda item: str(item.get("created_at") or item.get("train_id") or ""), reverse=True)
    return trains


def latest_release_train(target: Path) -> dict[str, Any] | None:
    trains = _release_trains(target)
    return trains[0] if trains else None


def _resolve_release_train(target: Path, train_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if train_id == "latest":
        latest = latest_release_train(target)
        return (latest, None) if latest else (None, "fleet release train not found: latest")
    matches = [item for item in _release_trains(target, include_archived=True) if str(item.get("train_id") or "").startswith(train_id)]
    if not matches:
        return None, f"fleet release train not found: {train_id}"
    if len(matches) > 1:
        return None, f"fleet release train id is ambiguous: {train_id}"
    return matches[0], None


def _repo_git_labels(repo: Path) -> dict[str, Any]:
    tracked_dirty, _ = _dirty_counts(repo) if repo.is_dir() else (0, 0)
    upstream = _git_value(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") if repo.is_dir() else None
    ahead = behind = None
    if upstream:
        counts = _git_value(repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                try:
                    ahead, behind = int(parts[0]), int(parts[1])
                except ValueError:
                    ahead = behind = None
    return {
        "branch": _git_value(repo, "rev-parse", "--abbrev-ref", "HEAD") if repo.is_dir() else None,
        "head_label": _git_value(repo, "rev-parse", "--short", "HEAD") if repo.is_dir() else None,
        "upstream_label": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty_tracked_count": tracked_dirty,
    }


def _fleet_actions_for_repo(target: Path, repo_id: str) -> list[dict[str, Any]]:
    return [action for action in _read_actions(target) if action.get("repo_id") == repo_id]


def _fleet_imports_for_repo(repo: Path) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for item in work_cmd._read_imports(repo):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if item.get("source") == "repo-fleet" or metadata.get("fleet_action_id"):
            imports.append(item)
    return imports


def _safe_import_ref(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "kind": item.get("kind"),
        "source": item.get("source"),
        "fleet_action_id": metadata.get("fleet_action_id"),
        "source_fingerprint": metadata.get("source_fingerprint"),
    }


def _safe_train_action_ref(action: dict[str, Any]) -> dict[str, Any]:
    dispatch = action.get("dispatch") if isinstance(action.get("dispatch"), dict) else {}
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
        "safe_summary": _safe_text(action.get("safe_summary")),
    }


def _latest_review_closeout_ref(repo: Path, repo_id: str, label: str) -> dict[str, Any] | None:
    try:
        from . import release_cmd

        closeout = release_cmd._latest_review_closeout(repo)
    except Exception:
        closeout = None
    return _safe_report_ref(closeout, repo_id, label)


def _latest_security_closeout_ref(repo: Path, repo_id: str, label: str) -> dict[str, Any] | None:
    return _safe_report_ref(_latest_json_payload(repo / ".brigade" / "security" / "closeouts", "closeout.json"), repo_id, label)


def _latest_verification_ref(repo: Path, repo_id: str, label: str) -> dict[str, Any] | None:
    receipt = work_cmd._latest_verify_receipt(repo)
    return _safe_report_ref(receipt, repo_id, label)


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
    candidate = state.get("latest_release_candidate") if isinstance(state.get("latest_release_candidate"), dict) else {}
    readiness = state.get("latest_release_readiness") if isinstance(state.get("latest_release_readiness"), dict) else {}
    if readiness.get("status") in {"blocked", "failed"}:
        return "blocked"
    if candidate.get("status") not in {"ready", "reviewed"}:
        return "needs-review"
    return "ready"


def _release_repo_payload(target: Path, entry: RepoEntry) -> dict[str, Any]:
    repo = entry.path
    state = _repo_brigade_state(entry)
    actions = _fleet_actions_for_repo(target, entry.repo_id)
    imports = _fleet_imports_for_repo(repo) if repo.is_dir() else []
    latest_sweep = _safe_sweep_ref(_latest_sweep_for_repo(target, entry.repo_id))
    fleet_report = latest_report(target)
    classification = _classify_release_repo(state, actions, imports)
    verification = _latest_verification_ref(repo, entry.repo_id, entry.label) if repo.is_dir() else None
    review_closeout = _latest_review_closeout_ref(repo, entry.repo_id, entry.label) if repo.is_dir() else None
    security_closeout = _latest_security_closeout_ref(repo, entry.repo_id, entry.label) if repo.is_dir() else None
    evidence = {
        "latest_fleet_sweep": latest_sweep,
        "latest_fleet_report": _safe_report_ref(fleet_report, entry.repo_id, entry.label),
        "fleet_actions": [_safe_train_action_ref(action) for action in actions],
        "pending_fleet_imports": [_safe_import_ref(item) for item in imports if item.get("status") == "pending"],
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
        "open_action_count": len([action for action in actions if action.get("status") in {"pending", "active", "deferred"}]),
        "pending_fleet_import_count": len(evidence["pending_fleet_imports"]),
        "warning_count": len(state.get("warnings") if isinstance(state.get("warnings"), list) else []),
        "blocker_count": len(state.get("blockers") if isinstance(state.get("blockers"), list) else []),
        "evidence": evidence,
        "suggested_next_command": _release_repo_next_command(entry.repo_id, classification, actions),
        "source_fingerprint": _fingerprint_payload({"repo_id": entry.repo_id, "classification": classification, "evidence": evidence, "git": _repo_git_labels(repo)}),
    }


def _release_repo_next_command(repo_id: str, classification: str, actions: list[dict[str, Any]]) -> str:
    if classification == "needs-dispatch":
        action = next((item for item in actions if item.get("status") in {"pending", "active"} and not item.get("dispatch")), None)
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
        return f"brigade repos actions list --target ."
    return f"brigade repos show {repo_id}"


def _release_train_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    entries, errors, config_loaded = _load_config(target)
    repos = [_release_repo_payload(target, entry) for entry in entries if entry.enabled]
    counts: dict[str, int] = {}
    for repo in repos:
        counts[str(repo.get("classification"))] = counts.get(str(repo.get("classification")), 0) + 1
    blockers = [repo for repo in repos if repo.get("classification") in {"blocked"}]
    warnings = [repo for repo in repos if repo.get("classification") in {"needs-review", "needs-dispatch", "in-progress", "stale-evidence", "no-release-candidate", "deferred"}]
    payload = {
        "schema_version": 1,
        "target_label": "repo-fleet",
        "config_loaded": config_loaded,
        "config_errors": [_safe_text(error, target, "repo-fleet", "repo fleet") for error in errors],
        "generated_at": _now().isoformat(),
        "repo_count": len(repos),
        "classification_counts": counts,
        "repos": repos,
        "blocker_count": len(blockers) + len(errors),
        "warning_count": len(warnings),
        "blockers": [{"repo_id": repo.get("repo_id"), "classification": repo.get("classification"), "detail": f"{repo.get('repo_id')} is blocked"} for repo in blockers],
        "warnings": [{"repo_id": repo.get("repo_id"), "classification": repo.get("classification"), "detail": f"{repo.get('repo_id')} is {repo.get('classification')}"} for repo in warnings],
        "suggested_next_commands": [repo.get("suggested_next_command") for repo in repos if repo.get("suggested_next_command")],
    }
    payload["train_fingerprint"] = _fingerprint_payload({"repos": repos, "counts": counts, "errors": payload["config_errors"]})
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
    repos = train.get("repos") if isinstance(train.get("repos"), list) else []
    for repo in repos:
        lines.append(f"- `{repo.get('repo_id')}` {repo.get('repo_label')} - {repo.get('classification')}")
        if repo.get("suggested_next_command"):
            lines.append(f"  - next: `{repo.get('suggested_next_command')}`")
    if not repos:
        lines.append("- none")
    lines.extend(["", "## Boundaries", "", "- local release train only", "- no push, tags, releases, uploads, or remote mutation", "- manual publish steps only"])
    return "\n".join(lines) + "\n"


def _release_train_publish_plan(train: dict[str, Any]) -> str:
    lines = ["# Manual Fleet Publish Plan", ""]
    repos = train.get("repos") if isinstance(train.get("repos"), list) else []
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
    _write_json(train_dir / "FLEET_RELEASE_EVIDENCE.json", train)
    (train_dir / "FLEET_RELEASE_TRAIN.md").write_text(_release_train_markdown(train))
    (train_dir / "MANUAL_PUBLISH_PLAN.md").write_text(_release_train_publish_plan(train))


def release_plan(*, target: Path, json_output: bool = False) -> int:
    payload = _release_train_payload(target)
    payload.update({"train_id": "planned", "status": "planned", "release_train_root_label": ".brigade/repos/releases"})
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["config_loaded"] else 1
    print("repo fleet release plan")
    print(f"repos: {payload['repo_count']}")
    print(f"blockers: {payload['blocker_count']}")
    for repo in payload["repos"]:
        print(f"- {repo.get('repo_id')} [{repo.get('classification')}]")
    return 0 if payload["config_loaded"] else 1


def release_build(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    created = _now()
    train_id = f"{created.strftime('%Y%m%d-%H%M%S')}-fleet-release-{uuid4().hex[:6]}"
    train_dir = _release_trains_root(target) / train_id
    payload = _release_train_payload(target)
    payload.update({"train_id": train_id, "status": "blocked" if payload["blocker_count"] else "ready", "created_at": created.isoformat(), "path_label": train_id})
    _write_release_train_bundle(train_dir, payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release train: {train_id}")
    print(f"status: {payload['status']}")
    print(f"path_label: {train_id}")
    return 0


def release_list(*, target: Path, limit: int = 20, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    trains = _release_trains(target)[:limit]
    payload = {"target_label": "repo-fleet", "release_train_root_label": ".brigade/repos/releases", "trains": trains, "train_count": len(trains)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release trains")
    for train in trains:
        print(f"- {train.get('train_id')} [{train.get('status')}] repos={train.get('repo_count')} {train.get('created_at')}")
    return 0


def release_show(*, target: Path, train_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target_label": "repo-fleet", "train": train}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release train: {train.get('train_id')}")
    print(f"status: {train.get('status')}")
    print(f"repos: {train.get('repo_count')}")
    print(f"path_label: {train.get('path_label')}")
    return 0


def release_compare(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    current = _release_train_payload(target)
    issues: list[dict[str, Any]] = []
    old_by_repo = {repo.get("repo_id"): repo for repo in train.get("repos") if isinstance(train.get("repos"), list) for repo in [repo] if isinstance(repo, dict)}
    current_by_repo = {repo.get("repo_id"): repo for repo in current.get("repos", []) if isinstance(repo, dict)}
    for repo_id, old in old_by_repo.items():
        new = current_by_repo.get(repo_id)
        if new is None:
            issues.append({"status": WARN, "name": "train_repo_missing", "repo_id": repo_id, "detail": f"{repo_id} is no longer in release train"})
            continue
        old_git = old.get("git") if isinstance(old.get("git"), dict) else {}
        new_git = new.get("git") if isinstance(new.get("git"), dict) else {}
        if old_git.get("head_label") and new_git.get("head_label") and old_git.get("head_label") != new_git.get("head_label"):
            issues.append({"status": WARN, "name": "train_repo_head_changed", "repo_id": repo_id, "detail": f"{repo_id} HEAD changed"})
        old_evidence = old.get("evidence") if isinstance(old.get("evidence"), dict) else {}
        new_evidence = new.get("evidence") if isinstance(new.get("evidence"), dict) else {}
        for key, name in (
            ("latest_release_readiness", "newer_release_readiness"),
            ("latest_release_candidate", "newer_release_candidate"),
        ):
            old_id = (old_evidence.get(key) or {}).get("id") if isinstance(old_evidence.get(key), dict) else None
            new_id = (new_evidence.get(key) or {}).get("id") if isinstance(new_evidence.get(key), dict) else None
            if old_id and not new_id:
                issues.append({"status": WARN, "name": "train_missing_receipt", "repo_id": repo_id, "detail": f"{repo_id} missing {key}"})
            elif old_id and new_id and old_id != new_id:
                issues.append({"status": WARN, "name": name, "repo_id": repo_id, "detail": f"{repo_id} has newer {key}"})
        old_actions = old_evidence.get("fleet_actions") if isinstance(old_evidence.get("fleet_actions"), list) else []
        new_actions = new_evidence.get("fleet_actions") if isinstance(new_evidence.get("fleet_actions"), list) else []
        if _fingerprint_payload(old_actions) != _fingerprint_payload(new_actions):
            issues.append({"status": WARN, "name": "train_fleet_actions_changed", "repo_id": repo_id, "detail": f"{repo_id} fleet action reconciliation changed"})
        if old.get("source_fingerprint") != new.get("source_fingerprint"):
            issues.append({"status": WARN, "name": "train_unresolved_state_changed", "repo_id": repo_id, "detail": f"{repo_id} unresolved release state changed"})
    payload = {"target_label": "repo-fleet", "train_id": train.get("train_id"), "issue_count": len(issues), "issues": issues, "suggested_next_commands": ["brigade repos release build", f"brigade repos release closeout {train.get('train_id')} --status superseded"]}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release compare: {train.get('train_id')}")
    print(f"issues: {len(issues)}")
    for issue in issues:
        print(f"[{issue.get('status')}] {issue.get('name')}: {issue.get('detail')}")
    return 0


def release_closeout(*, target: Path, train_id: str = "latest", status: str = "reviewed", reason: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    if status not in {"reviewed", "deferred", "superseded", "archived"}:
        print("error: --status must be one of reviewed, deferred, superseded, archived", file=sys.stderr)
        return 2
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_path = _release_trains_root(target) / str(train.get("train_id") or "")
    if not train_path.is_dir():
        print(f"error: fleet release train path is missing: {train.get('path_label') or train.get('train_id')}", file=sys.stderr)
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
    payload["summary"] = {
        key: value
        for key, value in _release_summary_payload(target, train).items()
        if key in {"counts", "repo_count", "ready_count", "blocked_count", "missing_evidence_count", "unresolved_action_count", "summary_fingerprint"}
    }
    _write_json(train_path / "CLOSEOUT.json", payload)
    train["closeout"] = payload
    train["status"] = status
    _write_json(train_path / "FLEET_RELEASE_EVIDENCE.json", train)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release closeout: {train.get('train_id')}")
    print(f"status: {status}")
    return 0


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
    destination = _release_trains_archive_root(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"error: archived fleet release train already exists: {train.get('train_id')}", file=sys.stderr)
        return 2
    shutil.move(str(source), str(destination))
    payload = {"target_label": "repo-fleet", "train_id": train.get("train_id"), "status": "archived", "archive_path_label": train.get("train_id")}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"archived repo fleet release train: {train.get('train_id')}")
    return 0


def _release_actions_path(target: Path) -> Path:
    return _release_trains_root(target) / "actions.json"


def _release_actions_archive_path(target: Path) -> Path:
    return _release_trains_root(target) / "actions-archive.jsonl"


def _read_release_actions(target: Path) -> list[dict[str, Any]]:
    payload = _read_json(_release_actions_path(target))
    actions = payload.get("actions") if isinstance(payload, dict) else None
    return [item for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []


def _write_release_actions(target: Path, actions: list[dict[str, Any]]) -> None:
    _write_json(_release_actions_path(target), {"updated_at": _now().isoformat(), "actions": actions})


def _read_release_action_archive(target: Path) -> list[dict[str, Any]]:
    return _read_jsonl(_release_actions_archive_path(target))


def _append_release_action_archive(target: Path, actions: list[dict[str, Any]]) -> None:
    if not actions:
        return
    path = _release_actions_archive_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for action in actions:
            handle.write(json.dumps(action, sort_keys=True) + "\n")


def _release_action_rank(action: dict[str, Any]) -> tuple[int, int, str]:
    status_rank = {"active": 0, "pending": 1, "deferred": 2, "done": 3, "archived": 4}.get(str(action.get("status") or ""), 5)
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
    closeout = train.get("closeout") if isinstance(train.get("closeout"), dict) else None
    status = closeout.get("status") if isinstance(closeout, dict) else None
    return status if isinstance(status, str) else None


def _planned_release_actions(train: dict[str, Any]) -> list[dict[str, Any]]:
    train_id = str(train.get("train_id") or "planned")
    train_fingerprint = str(train.get("train_fingerprint") or _fingerprint_payload(train))
    closeout = train.get("closeout") if isinstance(train.get("closeout"), dict) else {}
    reviewed_at = closeout.get("reviewed_at") if isinstance(closeout, dict) else None
    created = _now().isoformat()
    actions: list[dict[str, Any]] = []
    for repo in train.get("repos") if isinstance(train.get("repos"), list) else []:
        if not isinstance(repo, dict):
            continue
        classification = str(repo.get("classification") or "")
        if classification == "ready":
            continue
        repo_id = str(repo.get("repo_id") or "unknown")
        repo_label = str(repo.get("repo_label") or repo_id)
        repo_fp = str(repo.get("source_fingerprint") or _fingerprint_payload(repo))
        source_fingerprint = _fingerprint_payload(
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
                "suggested_command": str(repo.get("suggested_next_command") or f"brigade repos release show {train_id}"),
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
        print(f"- {action.get('release_action_id')} {action.get('repo_id')} [{action.get('classification')}] {action.get('safe_summary')}")
    return 0


def release_actions_build(*, target: Path, train_id: str = "latest", allow_unreviewed: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    review_status = _train_closeout_status(train)
    if review_status not in {"reviewed", "deferred"} and not allow_unreviewed:
        print("error: source fleet release train must be closed out as reviewed or deferred, or pass --allow-unreviewed", file=sys.stderr)
        return 2
    existing = _read_release_actions(target)
    existing_fingerprints = {str(action.get("source_fingerprint")) for action in existing}
    existing_fingerprints.update(str(action.get("source_fingerprint")) for action in _read_release_action_archive(target))
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in _planned_release_actions(train):
        if str(action.get("source_fingerprint")) in existing_fingerprints:
            skipped.append(action)
            continue
        existing.append(action)
        created.append(action)
        existing_fingerprints.add(str(action.get("source_fingerprint")))
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
    payload = {"target_label": "repo-fleet", "actions_path_label": ".brigade/repos/releases/actions.json", "actions": actions[:limit], "action_count": len(actions)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release actions")
    for action in actions[:limit]:
        print(f"- {action.get('release_action_id')} {action.get('repo_id')} [{action.get('status')}] {action.get('safe_summary')}")
    return 0


def _find_release_action(target: Path, action_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    actions = _read_release_actions(target)
    matches = [action for action in actions if str(action.get("release_action_id") or "").startswith(action_id)]
    if not matches:
        return actions, None, f"fleet release action not found: {action_id}"
    if len(matches) > 1:
        return actions, None, f"fleet release action id is ambiguous: {action_id}"
    return actions, matches[0], None


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


def _set_release_action_status(*, target: Path, action_id: str, status: str, reason: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions, action, error = _find_release_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    now = _now().isoformat()
    action["status"] = status
    action["updated_at"] = now
    if status == "active":
        action["started_at"] = now
    elif status == "done":
        action["completed_at"] = now
    elif status == "deferred":
        action["deferred_at"] = now
        action["defer_reason"] = reason or "deferred"
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
    return _set_release_action_status(target=target, action_id=action_id, status="deferred", reason=reason, json_output=json_output)


def release_actions_archive_completed(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_release_actions(target)
    now = _now().isoformat()
    archived: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for action in actions:
        if action.get("status") == "done":
            archived_action = dict(action)
            archived_action["status"] = "archived"
            archived_action["archived_at"] = now
            archived_action["updated_at"] = now
            archived.append(archived_action)
        else:
            remaining.append(action)
    _write_release_actions(target, remaining)
    _append_release_action_archive(target, archived)
    payload = {"target_label": "repo-fleet", "archived_count": len(archived), "archive_path_label": ".brigade/repos/releases/actions-archive.jsonl", "archived_actions": archived}
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
        if waiver.get("train_id") == train_id and waiver.get("status") == "active" and not _release_waiver_expired(waiver)
    ]


def _release_waiver_scope_names(waivers: list[dict[str, Any]]) -> set[str]:
    return {str(waiver.get("scope") or "") for waiver in waivers if waiver.get("scope") in RELEASE_WAIVER_SCOPES}


def _find_release_waiver(target: Path, waiver_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
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
        "expires_at": expires_at,
        "created_at": now,
        "updated_at": now,
        "source_fingerprint": _fingerprint_payload({"train_id": train_id_value, "repo_id": repo_id or "all", "scope": scope, "reason": reason, "expires_at": expires_at}),
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


def release_waiver_list(*, target: Path, train_id: str | None = None, limit: int = 50, json_output: bool = False) -> int:
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
    waivers.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("waiver_id") or ""), reverse=True)
    payload = {"target_label": "repo-fleet", "waivers_path_label": ".brigade/repos/releases/waivers.jsonl", "waivers": waivers[:limit], "waiver_count": len(waivers)}
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


def release_waiver_renew(*, target: Path, waiver_id: str, reason: str, expires_at: str | None = None, json_output: bool = False) -> int:
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
    waiver["renewed_at"] = now
    waiver["updated_at"] = now
    if isinstance(train, dict):
        waiver["train_fingerprint"] = train.get("train_fingerprint")
    waiver["source_fingerprint"] = _fingerprint_payload({"train_id": waiver.get("train_id"), "repo_id": waiver.get("repo_id") or "all", "scope": waiver.get("scope"), "reason": reason, "expires_at": expires_at})
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
        train, _ = _resolve_release_train(target, str(waiver.get("train_id") or ""))
        if _release_waiver_expired(waiver):
            issues.append({"status": WARN, "name": "release_waiver_expired", "waiver_id": waiver_id, "train_id": waiver.get("train_id"), "scope": waiver.get("scope"), "detail": f"{waiver_id} is expired", "suggested_next_command": f"brigade repos release waivers renew {waiver_id} --reason \"reviewed again\""})
            continue
        if not waiver.get("expires_at"):
            issues.append({"status": WARN, "name": "release_waiver_missing_expiry", "waiver_id": waiver_id, "train_id": waiver.get("train_id"), "scope": waiver.get("scope"), "detail": f"{waiver_id} has no expiry", "suggested_next_command": f"brigade repos release waivers renew {waiver_id} --reason \"set expiry\" --expires-at <timestamp>"})
        created = _parse_time(waiver.get("renewed_at") or waiver.get("created_at"))
        if created and (now - created).total_seconds() / 3600 > RELEASE_WAIVER_STALE_HOURS:
            issues.append({"status": WARN, "name": "release_waiver_stale_review", "waiver_id": waiver_id, "train_id": waiver.get("train_id"), "scope": waiver.get("scope"), "detail": f"{waiver_id} has not been reviewed recently", "suggested_next_command": f"brigade repos release waivers renew {waiver_id} --reason \"reviewed again\""})
        if isinstance(train, dict) and waiver.get("train_fingerprint") and train.get("train_fingerprint") and waiver.get("train_fingerprint") != train.get("train_fingerprint"):
            issues.append({"status": WARN, "name": "release_waiver_train_changed", "waiver_id": waiver_id, "train_id": waiver.get("train_id"), "scope": waiver.get("scope"), "detail": f"{waiver_id} references an older train fingerprint", "suggested_next_command": f"brigade repos release waivers renew {waiver_id} --reason \"train reviewed again\""})
    return {"target_label": "repo-fleet", "waivers_path_label": ".brigade/repos/releases/waivers.jsonl", "train_id": selected_train_id, "waiver_count": len(waivers), "issues": issues, "issue_count": len(issues), "top_issue": issues[0] if issues else None}


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
        fingerprint = _fingerprint_payload({"waiver_id": waiver_id, "name": issue.get("name"), "scope": issue.get("scope"), "train_id": issue.get("train_id")})
        records.append(
            {
                "text": f"Review fleet release waiver {waiver_id}: {issue.get('name')}",
                "kind": "task",
                "source": "repo-fleet-release-waiver",
                "type": "docs",
                "priority": "high" if issue.get("name") in {"release_waiver_expired", "release_waiver_train_changed"} else "normal",
                "template": "docs",
                "acceptance": [
                    "The release waiver is renewed with current review context or revoked.",
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


def release_waiver_import_issues(*, target: Path, train_id: str | None = None, dry_run: bool = False, json_output: bool = False) -> int:
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
    payload = {"target_label": "repo-fleet", "train_id": train.get("train_id"), "records_path_label": ".brigade/repos/releases/evidence.jsonl", "planned": planned, "planned_count": len(planned)}
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
        "source_fingerprint": _fingerprint_payload({"train_id": train_id_value, "repo_id": repo_id, "step": step, "status": status, "summary": summary or ""}),
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


def release_evidence_list(*, target: Path, train_id: str | None = None, limit: int = 50, json_output: bool = False) -> int:
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
    payload = {"target_label": "repo-fleet", "records_path_label": ".brigade/repos/releases/evidence.jsonl", "records": records[:limit], "record_count": len(records)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet release evidence records")
    for record in records[:limit]:
        print(f"- {record.get('evidence_id')} {record.get('repo_id')} {record.get('step')} [{record.get('status')}]")
    return 0


def release_evidence_show(*, target: Path, evidence_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    matches = [record for record in _read_release_evidence(target) if str(record.get("evidence_id") or "").startswith(evidence_id)]
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


def _release_records_by_repo_step(records: list[dict[str, Any]], train_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    by_step: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("train_id") != train_id:
            continue
        repo_id = str(record.get("repo_id") or "")
        step = str(record.get("step") or "")
        if repo_id and step:
            by_step[(repo_id, step)] = record
    return by_step


def _reconcile_release_action(action: dict[str, Any], records_by_step: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
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
    payload = {"target_label": "repo-fleet", "train_id": train_id_value, "result_count": len(results), "results": results}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release reconcile: {train_id_value}")
    for result in results:
        print(f"- {result.get('release_action_id')} {result.get('repo_id')} [{result.get('resolution_status')}]")
    return 0


def _repo_release_summary(repo: dict[str, Any], train_id: str, records_by_step: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
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
        "ready_count": sum(1 for repo in train.get("repos", []) if isinstance(repo, dict) and repo.get("classification") == "ready"),
        "blocked_count": len(blocked_evidence),
        "missing_evidence_count": len(missing_evidence),
        "unresolved_action_count": len(unresolved_actions),
        "unresolved_actions": [{"release_action_id": action.get("release_action_id"), "repo_id": action.get("repo_id"), "status": action.get("status"), "resolution_status": action.get("resolution_status")} for action in unresolved_actions],
        "suggested_next_commands": ["brigade repos release reconcile latest", "brigade repos release evidence plan latest"],
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
    lines.extend(["", "## Boundaries", "", "- local report only", "- no verification, tag, push, release, upload, or remote mutation"])
    return "\n".join(lines) + "\n"


def release_report(*, target: Path, train_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    train, error = _resolve_release_train(target, train_id)
    if train is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    train_dir = _release_trains_root(target) / str(train.get("train_id") or "")
    if not train_dir.is_dir():
        print(f"error: fleet release train path is missing: {train.get('path_label') or train.get('train_id')}", file=sys.stderr)
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
    payload = {"target_label": "repo-fleet", "train_id": train.get("train_id"), "path_label": str(train.get("train_id") or ""), "report": report}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet release report: {train.get('train_id')}")
    print("path_label: RELEASE_TRAIN_REPORT.md")
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


def release_import_issues(*, target: Path, train_id: str = "latest", dry_run: bool = False, json_output: bool = False) -> int:
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
    payload = {"target_label": "repo-fleet", "train_id": train.get("train_id"), "items": items, "item_count": len(items)}
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
            issues.append({"status": WARN, "name": "fleet_release_train_unclosed", "train_id": train_id, "detail": f"{train_id} has no closeout", "suggested_next_command": f"brigade repos release closeout {train_id}"})
        report_path = _release_trains_root(target) / train_id / "RELEASE_TRAIN_REPORT.json"
        if not report_path.is_file():
            issues.append({"status": WARN, "name": "fleet_release_report_missing", "train_id": train_id, "detail": f"{train_id} has no review report", "suggested_next_command": f"brigade repos release report {train_id}"})
        created = _parse_time(train.get("created_at") or train.get("generated_at"))
        if created and (_now() - created).total_seconds() / 3600 > RELEASE_TRAIN_STALE_HOURS:
            issues.append({"status": WARN, "name": "fleet_release_train_stale", "train_id": train_id, "detail": f"{train_id} is stale", "suggested_next_command": "brigade repos release build"})
    payload = {"target_label": "repo-fleet", "train_count": len(trains), "issues": issues, "issue_count": len(issues), "top_issue": issues[0] if issues else None}
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
        print(f"error: fleet release train path is missing: {train.get('path_label') or train.get('train_id')}", file=sys.stderr)
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
        issues.append({"status": WARN, "name": "release_train_bundle_missing", "detail": f"{train_id} bundle path is missing", "suggested_next_command": "brigade repos release build"})
    else:
        for name in RELEASE_BUNDLE_FILES:
            if not (train_dir / name).is_file():
                command = "brigade repos release manifest"
                if name.startswith("RELEASE_TRAIN_REPORT"):
                    command = f"brigade repos release report {train_id}"
                elif name == "CLOSEOUT.json":
                    command = f"brigade repos release closeout {train_id}"
                issues.append({"status": WARN, "name": "release_train_bundle_file_missing", "path_label": name, "detail": f"{name} is missing", "suggested_next_command": command})
        manifest = _read_json(train_dir / "RELEASE_TRAIN_MANIFEST.json")
        if isinstance(manifest, dict):
            expected = {item["path_label"]: item for item in [_release_bundle_file_entry(train_dir, name) for name in RELEASE_BUNDLE_FILES]}
            stored_files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
            for stored in stored_files:
                if not isinstance(stored, dict):
                    continue
                current = expected.get(str(stored.get("path_label") or ""))
                if current and stored.get("fingerprint") and current.get("fingerprint") and stored.get("fingerprint") != current.get("fingerprint"):
                    issues.append({"status": WARN, "name": "release_train_manifest_stale", "path_label": stored.get("path_label"), "detail": f"{stored.get('path_label')} changed after manifest build", "suggested_next_command": f"brigade repos release manifest {train_id}"})
                    break
    open_actions = [action for action in _read_release_actions(target) if action.get("source_train_id") == train_id and action.get("status") in {"pending", "active", "deferred"}]
    if open_actions:
        issues.append({"status": WARN, "name": "release_train_open_actions", "detail": f"{len(open_actions)} release action(s) remain open", "suggested_next_command": f"brigade repos release actions list --target ."})
    if int(summary.get("missing_evidence_count") or 0) > 0:
        issues.append({"status": WARN, "name": "release_train_missing_evidence", "detail": f"{summary.get('missing_evidence_count')} repo(s) have missing evidence", "suggested_next_command": f"brigade repos release evidence plan {train_id}"})
    if int(summary.get("blocked_count") or 0) > 0:
        issues.append({"status": WARN, "name": "release_train_blocked_evidence", "detail": f"{summary.get('blocked_count')} repo(s) have blocked evidence", "suggested_next_command": f"brigade repos release evidence plan {train_id}"})
    if int(train.get("blocker_count") or 0) > 0:
        issues.append({"status": WARN, "name": "release_train_blocked_repos", "detail": f"{train.get('blocker_count')} repo blocker(s) remain", "suggested_next_command": f"brigade repos release show {train_id}"})
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
        "summary": {key: summary.get(key) for key in ("repo_count", "counts", "unresolved_action_count", "missing_evidence_count", "blocked_count")},
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
            waived.append({"scope": scope, "count": count, "reason": next((item.get("reason") for item in active_waivers if item.get("scope") == scope), None)})
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
        "summary": {key: summary.get(key) for key in ("repo_count", "counts", "unresolved_action_count", "missing_evidence_count", "blocked_count")},
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
        checks.append({"status": WARN, "name": "repo_fleet_release_actions_open", "detail": f"{len(open_actions)} open fleet release action(s)", "suggested_next_command": f"brigade repos release actions show {top.get('release_action_id')}"})
    unreconciled = [action for action in actions if action.get("status") in {"pending", "active", "deferred"} and not action.get("reconciled_at")]
    if unreconciled:
        top = unreconciled[0]
        checks.append({"status": WARN, "name": "repo_fleet_release_action_unreconciled", "detail": f"{len(unreconciled)} fleet release action(s) need reconciliation", "suggested_next_command": f"brigade repos release reconcile {top.get('source_train_id') or 'latest'}"})
    missing = [action for action in actions if action.get("resolution_status") == "missing-evidence"]
    if missing:
        top = missing[0]
        checks.append({"status": WARN, "name": "repo_fleet_release_evidence_missing", "detail": f"{len(missing)} fleet release action(s) are missing manual evidence", "suggested_next_command": f"brigade repos release evidence plan {top.get('source_train_id') or 'latest'}"})
    return {"actions_path_label": ".brigade/repos/releases/actions.json", "action_count": len(actions), "open_count": len(open_actions), "top_action": open_actions[0] if open_actions else None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


def release_train_evidence_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    records = _read_release_evidence(target)
    blocked = [record for record in records if record.get("status") == "blocked"]
    checks: list[dict[str, Any]] = []
    if blocked:
        top = blocked[0]
        checks.append({"status": WARN, "name": "repo_fleet_release_evidence_blocked", "detail": f"{len(blocked)} blocked fleet release evidence record(s)", "suggested_next_command": f"brigade repos release evidence show {top.get('evidence_id')}"})
    return {"records_path_label": ".brigade/repos/releases/evidence.jsonl", "record_count": len(records), "blocked_count": len(blocked), "latest": records[-1] if records else None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


def release_train_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = latest_release_train(target)
    actions = release_train_actions_health(target)
    evidence = release_train_evidence_health(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append({"status": WARN, "name": "repo_fleet_release_train_missing", "detail": "no repo fleet release train has been built", "suggested_next_command": "brigade repos release build"})
        checks.extend(actions.get("checks") if isinstance(actions.get("checks"), list) else [])
        checks.extend(evidence.get("checks") if isinstance(evidence.get("checks"), list) else [])
        return {"latest": None, "actions": actions, "evidence": evidence, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    if latest.get("status") == "blocked" or int(latest.get("blocker_count") or 0) > 0:
        checks.append({"status": WARN, "name": "repo_fleet_release_train_blocked", "detail": f"{latest.get('train_id')} has blocker(s)", "suggested_next_command": f"brigade repos release show {latest.get('train_id')}"})
    if not closeout or closeout.get("status") not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append({"status": WARN, "name": "repo_fleet_release_train_unclosed", "detail": f"{latest.get('train_id')} has not been closed out", "suggested_next_command": f"brigade repos release closeout {latest.get('train_id')}"})
    created = _parse_time(latest.get("created_at") or latest.get("generated_at"))
    if created and (_now() - created).total_seconds() / 3600 > RELEASE_TRAIN_STALE_HOURS:
        checks.append({"status": WARN, "name": "repo_fleet_release_train_stale", "detail": f"{latest.get('train_id')} is stale", "suggested_next_command": "brigade repos release build"})
    checks.extend(actions.get("checks") if isinstance(actions.get("checks"), list) else [])
    checks.extend(evidence.get("checks") if isinstance(evidence.get("checks"), list) else [])
    return {"latest": latest, "actions": actions, "evidence": evidence, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}


def _dispatch_health_checks(target: Path, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for action in actions:
        status = action.get("resolution_status")
        if status in {"broken-reference", "stale"}:
            checks.append(
                {
                    "status": WARN,
                    "name": f"repo_fleet_action_{status}",
                    "detail": f"{action.get('fleet_action_id')} is {status}",
                    "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}",
                }
            )
            continue
        if action.get("dispatch") and status in {None, "dispatched"}:
            dispatch = action.get("dispatch") if isinstance(action.get("dispatch"), dict) else {}
            entry, error = _action_target_entry(target, action)
            if error or entry is None:
                checks.append({"status": WARN, "name": "repo_fleet_action_broken_reference", "detail": f"{action.get('fleet_action_id')} target repo is missing", "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}"})
                continue
            old_evidence = dispatch.get("target_evidence_fingerprint")
            new_evidence = _fingerprint_payload(_latest_safe_receipts(entry.path, entry.repo_id, entry.label))
            if old_evidence and old_evidence != new_evidence:
                checks.append({"status": WARN, "name": "repo_fleet_action_evidence_changed", "detail": f"{action.get('fleet_action_id')} target repo evidence changed after dispatch", "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}"})
            imports = _target_imports_for_action(entry.path, action)
            if not imports:
                checks.append({"status": WARN, "name": "repo_fleet_action_missing_import", "detail": f"{action.get('fleet_action_id')} target import is missing", "suggested_next_command": f"brigade repos actions reconcile {action.get('fleet_action_id')}"})
    return checks



def _append_action_archive(target: Path, actions: list[dict[str, Any]]) -> None:
    if not actions:
        return
    path = _actions_archive_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for action in actions:
            handle.write(json.dumps(action, sort_keys=True) + "\n")


def _report_review_status(report: dict[str, Any]) -> str | None:
    closeout = report.get("closeout") if isinstance(report.get("closeout"), dict) else None
    status = closeout.get("status") if isinstance(closeout, dict) else None
    return status if isinstance(status, str) else None


def _report_reviewed_at(report: dict[str, Any]) -> str | None:
    closeout = report.get("closeout") if isinstance(report.get("closeout"), dict) else None
    reviewed_at = closeout.get("reviewed_at") if isinstance(closeout, dict) else None
    return reviewed_at if isinstance(reviewed_at, str) else None


def _action_rank(action: dict[str, Any]) -> tuple[int, int, str]:
    status_rank = {"active": 0, "pending": 1, "deferred": 2, "done": 3, "archived": 4}.get(str(action.get("status") or ""), 5)
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(str(action.get("severity") or ""), 4)
    priority_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(str(action.get("priority") or ""), 4)
    return (status_rank, min(severity_rank, priority_rank), str(action.get("fleet_action_id") or ""))


def _planned_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    report_id = str(report.get("report_id") or "planned")
    report_fingerprint = str(report.get("report_fingerprint") or _fingerprint_payload(report))
    reviewed_at = _report_reviewed_at(report)
    created = _now().isoformat()
    actions: list[dict[str, Any]] = []
    for repo in report.get("repos") if isinstance(report.get("repos"), list) else []:
        if not isinstance(repo, dict):
            continue
        repo_id = str(repo.get("repo_id") or "unknown")
        repo_label = str(repo.get("repo_label") or repo_id)
        repo_items: list[dict[str, Any]] = []
        for warning in repo.get("warnings") if isinstance(repo.get("warnings"), list) else []:
            if isinstance(warning, dict):
                repo_items.append({"subsystem": "repo-fleet", "local_id": warning.get("name"), "summary": warning.get("detail"), "severity": "medium", "command": repo.get("suggested_command")})
        top_action = (repo.get("action_queue") if isinstance(repo.get("action_queue"), dict) else {}).get("top_action")
        if isinstance(top_action, dict):
            repo_items.insert(0, {"subsystem": "center-action", "local_id": top_action.get("action_id"), "summary": top_action.get("safe_summary"), "priority": "high", "command": top_action.get("suggested_command"), "source_report_id": top_action.get("source_report_id"), "source_fingerprint": top_action.get("source_fingerprint")})
        if int(repo.get("pending_import_count") or 0) > 0:
            repo_items.append({"subsystem": "work-import", "local_id": "pending-imports", "summary": f"{repo_id} has pending imports", "priority": "normal", "command": repo.get("suggested_command")})
        seen: set[str] = set()
        for item in repo_items:
            source_subsystem = str(item.get("subsystem") or "repo-fleet")
            source_local_id = str(item.get("local_id") or source_subsystem)
            source_basis = item.get("source_fingerprint") or _fingerprint_payload({"repo_id": repo_id, "subsystem": source_subsystem, "local_id": source_local_id, "summary": item.get("summary")})
            key = f"{repo_id}:{source_basis}"
            if key in seen:
                continue
            seen.add(key)
            source_fingerprint = _fingerprint_payload({"repo_id": repo_id, "report_fingerprint": report_fingerprint, "source": source_basis})
            actions.append(
                {
                    "fleet_action_id": f"fleet-act-{source_fingerprint[:16]}",
                    "repo_id": repo_id,
                    "repo_label": repo_label,
                    "source_report_id": item.get("source_report_id") or report_id,
                    "source_report_fingerprint": report_fingerprint,
                    "source_subsystem": source_subsystem,
                    "source_local_id": source_local_id,
                    "status": "pending",
                    "priority": item.get("priority") if isinstance(item.get("priority"), str) else None,
                    "severity": item.get("severity") if isinstance(item.get("severity"), str) else None,
                    "safe_summary": str(item.get("summary") or "repo fleet action"),
                    "suggested_command": str(item.get("command") or f"brigade repos show {repo_id}"),
                    "created_at": created,
                    "updated_at": created,
                    "reviewed_at": reviewed_at,
                    "source_fingerprint": source_fingerprint,
                }
            )
    actions.sort(key=_action_rank)
    return actions


def actions_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    open_actions = [action for action in actions if action.get("status") in {"pending", "active", "deferred"}]
    open_actions.sort(key=_action_rank)
    checks: list[dict[str, Any]] = []
    if open_actions:
        top = open_actions[0]
        checks.append({"status": WARN, "name": "repo_fleet_actions_open", "detail": f"{len(open_actions)} open fleet action(s)", "suggested_next_command": f"brigade repos actions show {top.get('fleet_action_id')}"})
    checks.extend(_dispatch_health_checks(target, actions))
    return {
        "actions_path": str(_actions_path(target)),
        "action_count": len(actions),
        "open_count": len(open_actions),
        "top_action": open_actions[0] if open_actions else None,
        "checks": checks,
        "issue_count": len(checks),
        "top_issue": checks[0] if checks else None,
    }


def actions_plan(*, target: Path, report_id: str = "latest", json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    actions = _planned_actions(report)
    payload = {"target": str(target), "report_id": report.get("report_id"), "report_review_status": _report_review_status(report), "actions_root": str(_actions_root(target)), "actions": actions, "action_count": len(actions)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet actions plan: {report.get('report_id')}")
    print(f"actions: {len(actions)}")
    for action in actions[:20]:
        print(f"- {action.get('fleet_action_id')} {action.get('repo_id')} [{action.get('status')}] {action.get('safe_summary')}")
    return 0


def actions_build(*, target: Path, report_id: str = "latest", allow_unreviewed: bool = False, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    report, error = _resolve_report(target, report_id)
    if report is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    review_status = _report_review_status(report)
    if review_status not in {"reviewed", "deferred"} and not allow_unreviewed:
        print("error: source fleet report must be closed out as reviewed or deferred, or pass --allow-unreviewed", file=sys.stderr)
        return 2
    existing = _read_actions(target)
    existing_fingerprints = {str(action.get("source_fingerprint")) for action in existing}
    existing_fingerprints.update(str(action.get("source_fingerprint")) for action in _read_action_archive(target))
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in _planned_actions(report):
        if str(action.get("source_fingerprint")) in existing_fingerprints:
            skipped.append(action)
            continue
        created.append(action)
        existing.append(action)
        existing_fingerprints.add(str(action.get("source_fingerprint")))
    _write_actions(target, existing)
    payload = {"target": str(target), "report_id": report.get("report_id"), "actions_path": str(_actions_path(target)), "created_count": len(created), "skipped_count": len(skipped), "created_actions": created, "skipped_actions": skipped}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet actions build: {report.get('report_id')}")
    print(f"created: {len(created)}")
    print(f"skipped: {len(skipped)}")
    return 0


def actions_list(*, target: Path, limit: int = 50, json_output: bool = False) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    actions.sort(key=_action_rank)
    payload = {"target": str(target), "actions_path": str(_actions_path(target)), "actions": actions[:limit], "action_count": len(actions)}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet actions: {target}")
    for action in actions[:limit]:
        print(f"- {action.get('fleet_action_id')} {action.get('repo_id')} [{action.get('status')}] {action.get('safe_summary')}")
    return 0


def _find_action(target: Path, action_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    actions = _read_actions(target)
    matches = [action for action in actions if str(action.get("fleet_action_id") or "").startswith(action_id)]
    if not matches:
        return actions, None, f"fleet action not found: {action_id}"
    if len(matches) > 1:
        return actions, None, f"fleet action id is ambiguous: {action_id}"
    return actions, matches[0], None


def actions_show(*, target: Path, action_id: str, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    _, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    if json_output:
        print(json.dumps({"target": str(target), "action": action}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet action: {action.get('fleet_action_id')}")
    print(f"status: {action.get('status')}")
    print(f"repo: {action.get('repo_id')} {action.get('repo_label')}")
    print(f"summary: {action.get('safe_summary')}")
    return 0


def _set_action_status(*, target: Path, action_id: str, status: str, reason: str | None = None, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions, action, error = _find_action(target, action_id)
    if action is None:
        print(f"error: {error}", file=sys.stderr)
        return 1 if error and "not found" in error else 2
    now = _now().isoformat()
    action["status"] = status
    action["updated_at"] = now
    if status == "active":
        action["started_at"] = now
    elif status == "done":
        action["completed_at"] = now
    elif status == "deferred":
        action["deferred_at"] = now
        action["defer_reason"] = reason or "deferred"
    _write_actions(target, actions)
    if json_output:
        print(json.dumps({"target": str(target), "action": action}, indent=2, sort_keys=True))
        return 0
    print(f"repo fleet action {status}: {action.get('fleet_action_id')}")
    return 0


def actions_start(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_action_status(target=target, action_id=action_id, status="active", json_output=json_output)


def actions_done(*, target: Path, action_id: str, json_output: bool = False) -> int:
    return _set_action_status(target=target, action_id=action_id, status="done", json_output=json_output)


def actions_defer(*, target: Path, action_id: str, reason: str, json_output: bool = False) -> int:
    if not reason:
        print("error: --reason is required", file=sys.stderr)
        return 2
    return _set_action_status(target=target, action_id=action_id, status="deferred", reason=reason, json_output=json_output)


def actions_archive_completed(*, target: Path, json_output: bool = False) -> int:
    target = target.expanduser().resolve()
    actions = _read_actions(target)
    now = _now().isoformat()
    archived: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for action in actions:
        if action.get("status") == "done":
            archived_action = dict(action)
            archived_action["status"] = "archived"
            archived_action["archived_at"] = now
            archived_action["updated_at"] = now
            archived.append(archived_action)
        else:
            remaining.append(action)
    _write_actions(target, remaining)
    _append_action_archive(target, archived)
    payload = {"target": str(target), "archived_count": len(archived), "archive_path": str(_actions_archive_path(target)), "archived_actions": archived}
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print("repo fleet actions archive: completed")
    print(f"archived: {len(archived)}")
    return 0


def health(target: Path) -> dict[str, Any]:
    payload = scan_payload(target)
    report = report_health(target)
    actions = actions_health(target)
    sweep = sweep_health(target)
    release_train = release_train_health(target)
    issue_count = payload["issue_count"] + int(report.get("issue_count") or 0) + int(actions.get("issue_count") or 0) + int(sweep.get("issue_count") or 0) + int(release_train.get("issue_count") or 0)
    top_issue = payload["top_issue"] or report.get("top_issue") or actions.get("top_issue") or sweep.get("top_issue") or release_train.get("top_issue")
    return {
        "target": payload["target"],
        "config_path": payload["config_path"],
        "repo_count": payload["repo_count"],
        "issue_count": issue_count,
        "top_issue": top_issue,
        "checks": payload["checks"],
        "report": report,
        "actions": actions,
        "sweep": sweep,
        "release_train": release_train,
    }


def report_health(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    latest = latest_report(target)
    checks: list[dict[str, Any]] = []
    if latest is None:
        checks.append({"status": WARN, "name": "repo_fleet_report_missing", "detail": "no local repo fleet report has been built", "suggested_next_command": "brigade repos report build"})
        return {"latest": None, "checks": checks, "issue_count": len(checks), "top_issue": checks[0]}
    closeout = latest.get("closeout") if isinstance(latest.get("closeout"), dict) else None
    if not closeout or closeout.get("status") not in {"reviewed", "deferred", "superseded", "archived"}:
        checks.append({"status": WARN, "name": "repo_fleet_report_unclosed", "detail": f"{latest.get('report_id')} has not been closed out", "suggested_next_command": f"brigade repos report closeout {latest.get('report_id')}"})
    created = _parse_time(latest.get("created_at") or latest.get("generated_at"))
    if created and (_now() - created).total_seconds() / 3600 > REPORT_STALE_HOURS:
        checks.append({"status": WARN, "name": "repo_fleet_report_stale", "detail": f"{latest.get('report_id')} is stale", "suggested_next_command": "brigade repos report build"})
    return {"latest": latest, "checks": checks, "issue_count": len(checks), "top_issue": checks[0] if checks else None}
