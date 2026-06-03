"""`brigade scrub` - run the content-guard scanner against a target."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


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
    try:
        resolved_policy = str(policy_path(target, policy))
    except ValueError as exc:
        resolved_policy = str(exc)
    return {
        "available": available(),
        "scanner_dir": str(scanner_dir()),
        "policy": policy,
        "policy_path": resolved_policy,
        "policy_exists": Path(resolved_policy).is_file(),
        "pre_push_hook_path": str(hook),
        "pre_push_hook_exists": hook.is_file(),
        "pre_push_hook_executable": os.access(hook, os.X_OK),
        "hooks_path": hooks_path,
        "pre_push_hook_enabled": hooks_path == "hooks",
        "last_scan": _last_scan_summary(target),
    }


def _git_config(target: Path, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "config", "--get", key],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value or None


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
            "stderr": "clone https://github.com/solomonneas/content-guard or set CONTENT_GUARD_DIR",
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
            "brigade scrub: clone https://github.com/solomonneas/content-guard "
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
         `<target>/.brigade/policies/<policy>.json`, then
         `<scanner_dir>/policies/<policy>.json`.
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
        scanner_dir / "policies" / f"{safe}.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]  # caller prints "not found" with this path
