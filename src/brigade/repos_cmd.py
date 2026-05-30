"""Local repository fleet readiness inspection."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .install import apply_gitignore
from .selection import Selection
from . import work_cmd

OK = "ok"
WARN = "warn"
FAIL = "fail"
CONFIG_REL_PATH = ".brigade/repos.toml"
REPORT_STALE_HOURS = 24
ACTION_STATUSES = {"pending", "active", "done", "deferred", "archived"}


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
    for bucket_name in ("report", "actions", "sweep"):
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
    issue_count = payload["issue_count"] + int(report.get("issue_count") or 0) + int(actions.get("issue_count") or 0) + int(sweep.get("issue_count") or 0)
    top_issue = payload["top_issue"] or report.get("top_issue") or actions.get("top_issue") or sweep.get("top_issue")
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
