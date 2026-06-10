"""`brigade scrub` - run the content-guard scanner against a target."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .templates import template_root


def scanner_dir() -> Path:
    return Path(os.environ.get("CONTENT_GUARD_DIR", str(Path.home() / "repos" / "content-guard")))


def available() -> bool:
    return scanner_dir().is_dir()


def policy_path(repo_target: Path, policy: str = "public-repo") -> Path:
    return _resolve_policy(repo_target.expanduser().resolve(), scanner_dir(), policy)


def hook_status(target: Path, policy: str = "public-repo") -> dict[str, Any]:
    target = target.expanduser().resolve()
    hook = target / "hooks" / "pre-push"
    hooks_path = _git_config(target, "core.hooksPath")
    local_hooks_path = _git_config(target, "core.hooksPath", local_only=True)
    git_hook = _git_pre_push_hook(target)
    configured_hook = _configured_pre_push_hook(target, hooks_path)
    active_hook = configured_hook if hooks_path else git_hook
    managed_enabled = hooks_path == "hooks" and hook.is_file() and os.access(hook, os.X_OK)
    active_enabled = active_hook is not None and active_hook.is_file() and os.access(active_hook, os.X_OK)
    hook_enabled = managed_enabled or active_enabled
    hook_mode = "not-enabled"
    if hook_enabled and managed_enabled:
        hook_mode = "managed-hooks-path"
    elif hook_enabled and hooks_path:
        hook_mode = "configured-hooks-path"
    elif hook_enabled:
        hook_mode = "git-hooks"
    # A core.hooksPath inherited from global/system git config can point at a
    # personal pre-push that has nothing to do with content-guard. Reporting
    # that as "installed" is a false positive; only trust an inherited hook
    # when it actually runs content-guard.
    hook_inherited = bool(hooks_path) and not local_hooks_path
    if hook_mode == "configured-hooks-path" and hook_inherited and active_hook is not None:
        if not _hook_runs_content_guard(active_hook):
            hook_mode = "external-hooks-path"
            hook_enabled = False
    try:
        resolved_policy = str(policy_path(target, policy))
    except ValueError as exc:
        resolved_policy = str(exc)
    policy_exists = Path(resolved_policy).is_file()
    checks: list[dict[str, str]] = []
    suggestions: list[str] = []
    if not available():
        checks.append({"status": "warn", "name": "content_guard_missing", "detail": f"content-guard not found at {scanner_dir()}"})
        suggestions.append("clone https://github.com/escoffier-labs/content-guard or set CONTENT_GUARD_DIR")
    if not policy_exists:
        checks.append({"status": "warn", "name": "content_guard_policy_missing", "detail": f"policy not found: {resolved_policy}"})
        suggestions.append(f"brigade scrub --target . --policy {policy} --dry-run")
    if hook_mode == "external-hooks-path":
        checks.append(
            {
                "status": "warn",
                "name": "content_guard_hook_unrelated",
                "detail": "the active pre-push comes from a global core.hooksPath outside this repo and does not run content-guard; the repo's hook is not active",
            }
        )
        if hook.is_file():
            suggestions.append("git config core.hooksPath hooks")
    elif not hook_enabled:
        checks.append({"status": "warn", "name": "content_guard_hook_not_enabled", "detail": "no executable pre-push hook found in the active Git hooks path"})
        if hook.is_file():
            suggestions.append("git config core.hooksPath hooks")
        else:
            suggestions.append("brigade init --target . --force")
    if not checks:
        checks.append({"status": "ok", "name": "content_guard_ready", "detail": "content-guard policy and pre-push hook are available"})
    return {
        "available": available(),
        "scanner_dir": str(scanner_dir()),
        "policy": policy,
        "policy_path": resolved_policy,
        "policy_exists": policy_exists,
        "pre_push_hook_path": str(hook),
        "pre_push_hook_exists": hook.is_file(),
        "pre_push_hook_executable": os.access(hook, os.X_OK),
        "configured_pre_push_hook_path": str(configured_hook) if configured_hook else None,
        "configured_pre_push_hook_exists": configured_hook.is_file() if configured_hook else False,
        "configured_pre_push_hook_executable": os.access(configured_hook, os.X_OK) if configured_hook else False,
        "git_pre_push_hook_path": str(git_hook) if git_hook else None,
        "git_pre_push_hook_exists": git_hook.is_file() if git_hook else False,
        "git_pre_push_hook_executable": os.access(git_hook, os.X_OK) if git_hook else False,
        "hooks_path": hooks_path,
        "pre_push_hook_enabled": hook_enabled,
        "pre_push_hook_mode": hook_mode,
        "checks": checks,
        "suggested_commands": list(dict.fromkeys(suggestions)),
        "last_scan": _last_scan_summary(target),
    }


def _hook_runs_content_guard(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return any(marker in text for marker in ("content-guard", "content_guard", "brigade scrub"))


def _git_config(target: Path, key: str, *, local_only: bool = False) -> str | None:
    scope = ["--local"] if local_only else []
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "config", *scope, "--get", key],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value or None


def _git_pre_push_hook(target: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--git-dir"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        return None
    git_dir = Path(value)
    if not git_dir.is_absolute():
        git_dir = target / git_dir
    return git_dir / "hooks" / "pre-push"


def _configured_pre_push_hook(target: Path, hooks_path: str | None) -> Path | None:
    if not hooks_path:
        return None
    path = Path(hooks_path).expanduser()
    if not path.is_absolute():
        path = target / path
    return path / "pre-push"


def _last_scan_summary(target: Path) -> dict[str, Any] | None:
    latest = target / ".brigade" / "security" / "latest" / "security-report.json"
    if not latest.is_file():
        return None
    try:
        payload = json.loads(latest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "path": str(latest.parent),
        "generated_at": payload.get("generated_at"),
        "finding_count": payload.get("finding_count"),
        "policy": payload.get("policy"),
    }


def run_scan(scan_target: Path, *, repo_target: Path | None = None, policy: str = "public-repo") -> dict[str, Any]:
    scan_target = scan_target.expanduser().resolve()
    repo_target = repo_target.expanduser().resolve() if repo_target is not None else scan_target
    scanner = scanner_dir()
    if not scanner.is_dir():
        return {
            "available": False,
            "status": "missing",
            "exit_code": 2,
            "detail": f"content-guard not found at {scanner}",
            "stdout": "",
            "stderr": "clone https://github.com/escoffier-labs/content-guard or set CONTENT_GUARD_DIR",
            "policy": policy,
            "policy_path": None,
            "target": str(scan_target),
        }
    try:
        resolved_policy = policy_path(repo_target, policy)
    except ValueError as exc:
        return {
            "available": True,
            "status": "error",
            "exit_code": 4,
            "detail": str(exc),
            "stdout": "",
            "stderr": str(exc),
            "policy": policy,
            "policy_path": None,
            "target": str(scan_target),
        }
    if not resolved_policy.is_file():
        return {
            "available": True,
            "status": "error",
            "exit_code": 3,
            "detail": f"policy not found: {resolved_policy}",
            "stdout": "",
            "stderr": f"policy not found: {resolved_policy}",
            "policy": policy,
            "policy_path": str(resolved_policy),
            "target": str(scan_target),
        }
    cmd = [
        sys.executable,
        "-m",
        "content_guard",
        "scan",
        str(scan_target),
        "--policy",
        str(resolved_policy),
    ]
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{scanner / 'src'}{os.pathsep}{existing_pp}" if existing_pp else str(scanner / "src")
    result = subprocess.run(cmd, env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return {
        "available": True,
        "status": "ok" if result.returncode == 0 else "blocked",
        "exit_code": result.returncode,
        "detail": "clean" if result.returncode == 0 else "content-guard reported findings",
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "policy": policy,
        "policy_path": str(resolved_policy),
        "target": str(scan_target),
        "argv": cmd,
    }


def run(
    target: Path,
    policy: str = "public-repo",
    dry_run: bool = False,
) -> int:
    target = target.expanduser().resolve()
    scanner_dir = globals()["scanner_dir"]()

    if not scanner_dir.is_dir():
        print(
            f"brigade scrub: content-guard not found at {scanner_dir}",
            file=sys.stderr,
        )
        print(
            "brigade scrub: clone https://github.com/escoffier-labs/content-guard "
            "or set CONTENT_GUARD_DIR",
            file=sys.stderr,
        )
        return 2

    try:
        policy_path = _resolve_policy(target, scanner_dir, policy)
    except ValueError as exc:
        print(f"brigade scrub: {exc}", file=sys.stderr)
        return 4
    if not policy_path.is_file():
        print(f"brigade scrub: policy not found: {policy_path}", file=sys.stderr)
        return 3

    cmd = [sys.executable, "-m", "content_guard", "scan", str(target), "--policy", str(policy_path)]
    if dry_run:
        print("brigade scrub: would run:")
        print(" ", " ".join(cmd))
        print(f"  PYTHONPATH={scanner_dir / 'src'}")
        return 0

    result = run_scan(target, policy=policy)
    if result.get("stdout"):
        print(result["stdout"], end="")
    if result.get("stderr"):
        print(result["stderr"], end="", file=sys.stderr)
    return int(result["exit_code"])


def _resolve_policy(target: Path, scanner_dir: Path, policy: str) -> Path:
    """Resolve a policy name to a JSON path.

    Lookup order:
      1. If `policy` looks like a path (contains `/` or `\\` or ends in `.json`),
         treat it as a literal file path and use it as-is.
      2. Otherwise, treat it as a basename and search the safe lookup chain:
         `<target>/.brigade/policies/<policy>.json`,
         Brigade's packaged policies, then `<scanner_dir>/policies/<policy>.json`.
    """
    looks_like_path = "/" in policy or "\\" in policy or policy.endswith(".json")
    if looks_like_path:
        return Path(policy)

    # Bare name: must be a simple slug, no path segments.
    safe = policy.strip()
    if not safe or any(c in safe for c in ("/", "\\", "..")):
        raise ValueError(f"unsafe policy name: {policy!r}")

    candidates = [
        target / ".brigade" / "policies" / f"{safe}.json",
        template_root() / "policies" / f"{safe}.json",
        scanner_dir / "policies" / f"{safe}.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]  # caller prints "not found" with this path
