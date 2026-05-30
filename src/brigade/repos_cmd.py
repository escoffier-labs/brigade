"""Local repository fleet readiness inspection."""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .install import apply_gitignore
from .selection import Selection
from . import work_cmd

OK = "ok"
WARN = "warn"
FAIL = "fail"
CONFIG_REL_PATH = ".brigade/repos.toml"


@dataclass(frozen=True)
class RepoEntry:
    repo_id: str
    label: str
    path: Path
    enabled: bool = True
    expect_brigade: bool = False
    expect_publish_guard: bool = False


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


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
        repo_path = (target / path_value).expanduser().resolve()
        entries.append(
            RepoEntry(
                repo_id=repo_id,
                label=label or repo_id,
                path=repo_path,
                enabled=bool(raw.get("enabled", True)),
                expect_brigade=bool(raw.get("expect_brigade", False)),
                expect_publish_guard=bool(raw.get("expect_publish_guard", False)),
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
    payload = scan_payload(target)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["issue_count"] == 0 else 1
    print(f"repos doctor: {payload['target']}")
    for check in payload["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    return 0 if payload["issue_count"] == 0 else 1


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


def health(target: Path) -> dict[str, Any]:
    payload = scan_payload(target)
    return {
        "target": payload["target"],
        "config_path": payload["config_path"],
        "repo_count": payload["repo_count"],
        "issue_count": payload["issue_count"],
        "top_issue": payload["top_issue"],
        "checks": payload["checks"],
    }
