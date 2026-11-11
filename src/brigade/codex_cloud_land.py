"""Explicit, isolated promotion of a registered Codex Cloud diff to a PR."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from . import cloud_tracker, codex_cloud, isolated_exec, proc
from .inbox_lock import InboxLockTimeout, held_file_lock


class LandError(ValueError):
    """A safe named refusal, without provider output or diff content."""


def _run(argv: list[str], cwd: Path, reason: str, *, stdin: bytes | None = None) -> str:
    try:
        # Anchor relative search paths before subprocess cwd enters the task tree.
        environment = dict(os.environ)
        environment["PATH"] = os.pathsep.join(str(Path(part or ".").resolve()) for part in os.get_exec_path())
        result = proc.run(argv, cwd=cwd, timeout=240, stdin=stdin, env=environment)
    except (OSError, TimeoutError) as exc:
        raise LandError(reason) from exc
    if result.code or result.output_limit_exceeded or result.decode_failed or result.incomplete_process_group:
        raise LandError(reason)
    return result.stdout


def _check_path(value: str, tree: Path, hooks: tuple[Path, ...]) -> None:
    path = PurePosixPath(value)
    parts = tuple(part.lower() for part in path.parts)
    if not parts or path.is_absolute() or ".." in parts or "\\" in value:
        raise LandError("unsafe-path")
    if parts[:2] == (".github", "workflows") or "codeowners" in parts:
        raise LandError("protected-policy-path")
    # Refuse credential stores and files, including common dotfiles and key formats.
    if any(
        part
        in {
            ".git",
            ".ssh",
            ".aws",
            ".azure",
            ".config",
            ".codex",
            ".claude",
            ".brigade",
            ".gnupg",
            ".kube",
            ".docker",
            ".pki",
        }
        or re.search(r"secret|credential|token|password|service[-_]account", part)
        or part.startswith(".env")
        or part
        in {
            "auth.json",
            "auth.toml",
            "auth.yaml",
            "auth.yml",
            ".netrc",
            ".npmrc",
            ".pypirc",
            "id_rsa",
            "id_ed25519",
            "id_ecdsa",
            "id_dsa",
        }
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        for part in parts
    ):
        raise LandError("credential-path")
    candidate = tree / value
    if any(
        hook == checked or hook in checked.parents or checked in hook.parents
        for hook in hooks
        for checked in (candidate, candidate.resolve())
    ):
        raise LandError("protected-hook-path")
    if parts[0] in {"hooks", ".githooks"}:
        raise LandError("protected-hook-path")


def _diff_paths(diff: str, cwd: Path, hooks: tuple[Path, ...]) -> None:
    raw = _run(["git", "apply", "--numstat", "-z", "-"], cwd, "invalid-diff", stdin=diff.encode())
    records = iter(raw.split("\0"))
    count = 0
    for record in records:
        if not record:
            continue
        columns = record.split("\t", 2)
        if len(columns) != 3:
            raise LandError("invalid-diff")
        paths = [columns[2]] if columns[2] else [next(records, ""), next(records, "")]
        for path in paths:
            _check_path(path, cwd, hooks)
            count += 1
    if not count:
        raise LandError("empty-diff")


def land(target: Path, task_id: str, issue: int) -> dict[str, str]:
    """Apply only in a disposable origin/main worktree, then publish for review."""
    if not re.fullmatch(r"task_[A-Za-z0-9_-]{3,160}", task_id) or issue < 1:
        raise LandError("invalid-task-or-issue")
    target = target.resolve()
    try:
        with held_file_lock(cloud_tracker.registry_path(target).parent / f"land-{task_id}.lock", deadline_seconds=5):
            return _land_locked(target, task_id, issue)
    except InboxLockTimeout as exc:
        raise LandError("landing-busy") from exc


def _record_pr(target: Path, task_id: str, branch: str, url: str) -> dict[str, str]:
    try:
        with cloud_tracker.registry_lock(target):
            registry = cloud_tracker.load_registry(target)
            entry = next(
                row
                for row in registry["entries"]
                if row.get("task_id") == task_id and row.get("provider") == "codex-cloud"
            )
            entry.update(branch=branch, pr_url=url, expected_artifact={"kind": "branch"})
            cloud_tracker.save_registry(target, registry)
    except (OSError, StopIteration) as exc:
        raise LandError("registry-update-failed") from exc
    return {"task_id": task_id, "branch": branch, "pr_url": url}


def _land_locked(target: Path, task_id: str, issue: int) -> dict[str, str]:
    with cloud_tracker.registry_lock(target):
        registry = cloud_tracker.load_registry(target)
        entries = [
            row for row in registry["entries"] if row.get("provider") == "codex-cloud" and row.get("task_id") == task_id
        ]
        if len(entries) != 1:
            raise LandError("task-not-uniquely-registered")
        entry = entries[0]
        if entry.get("pr_url"):
            return {"task_id": task_id, "branch": entry["branch"], "pr_url": entry["pr_url"]}
        previous_branch = entry.get("landing_branch")
        branch = previous_branch or f"codex/issue-{issue}-{uuid4().hex[:12]}"
        entry["landing_branch"] = branch
        cloud_tracker.save_registry(target, registry)
    repo = codex_cloud._repo_from_cwd(target)
    if not repo:
        raise LandError("github-origin-required")
    if previous_branch:
        raw = _run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--json", "url", "--limit", "1"],
            target,
            "pr-recovery-failed",
        )
        try:
            found = json.loads(raw)
            if not isinstance(found, list):
                raise ValueError("invalid PR inventory")
            if found:
                url = found[0]["url"]
                if not isinstance(url, str) or not re.fullmatch(
                    rf"https://github\.com/{re.escape(repo)}/pull/[1-9][0-9]*", url
                ):
                    raise ValueError("invalid PR URL")
                return _record_pr(target, task_id, branch, url)
        except (ValueError, KeyError, TypeError) as exc:
            raise LandError("pr-recovery-failed") from exc
    status = _run(["codex", "cloud", "status", task_id], target, "status-failed")
    if codex_cloud._verified_status(status) not in codex_cloud.TERMINAL_OK:
        raise LandError("task-not-ready")
    _run(["git", "fetch", "origin"], target, "fetch-failed")
    with tempfile.TemporaryDirectory(prefix="brigade-cloud-land-") as temporary:
        root = Path(temporary)
        tree = root / "worktree"
        _run(["git", "worktree", "add", "-b", branch, str(tree), "origin/main"], target, "worktree-failed")
        publication_started = False
        try:
            hook_path = Path(_run(["git", "rev-parse", "--git-path", "hooks"], tree, "hook-config-failed").strip())
            hook_path = hook_path if hook_path.is_absolute() else tree / hook_path
            hooks = (hook_path, hook_path.resolve())
            diff = _run(["codex", "cloud", "diff", task_id], tree, "diff-failed")
            _diff_paths(diff, tree, hooks)
            _run(["git", "apply", "--check", "-"], tree, "apply-conflict", stdin=diff.encode())
            _run(["codex", "cloud", "apply", task_id], tree, "apply-conflict")
            _run(["git", "add", "--all"], tree, "stage-failed")
            paths = _run(
                ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"], tree, "inspect-failed"
            ).split("\0")
            if not any(paths):
                raise LandError("empty-diff")
            for name in filter(None, paths):
                _check_path(name, tree, hooks)
                path = tree / name
                if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
                    raise LandError("unsafe-file-type")
            # Execute the trusted installed guard, never code from the applied tree.
            # A script keeps verification argv shell-free and produces a Brigade receipt.
            check = root / "guard-check"
            check.write_text(
                f"#!{sys.executable} -I\nimport subprocess\nraise SystemExit(subprocess.call("
                + repr(
                    isolated_exec.package_argv(
                        isolated_exec.trusted_cwd() / "__init__.py",
                        "brigade",
                        "guard",
                        "git",
                        "--staged",
                        cwd_after_load=tree,
                    )
                )
                + "))\n",
                encoding="utf-8",
            )
            check.chmod(0o700)
            _run(
                [
                    "brigade",
                    "work",
                    "verify",
                    "run",
                    "--target",
                    str(tree),
                    "--command",
                    str(check),
                    "--capture",
                    "brigade-work",
                ],
                target,
                "guard-failed",
            )
            guarded_tree = _run(["git", "write-tree"], tree, "inspect-failed").strip()
            message = (
                f"fix: address #{issue}\n\nCo-Authored-By: Codex <codex@openai.com>"  # content-guard: allow pii/email
            )
            _run(["git", "commit", "-m", message], tree, "commit-failed")
            if _run(["git", "rev-parse", "HEAD^{tree}"], tree, "inspect-failed").strip() != guarded_tree:
                raise LandError("commit-tree-mismatch")
            body = root / "pr-body.md"
            body.write_text(
                f"Refs #{issue}.\n\nContent came from codex-cloud task {task_id}.\n\n"
                "Applied in an isolated worktree from origin/main. Content guard passed.\n"
                "Conductor review and merge required.\n\n"
                "🤖 Generated with Codex\n",
                encoding="utf-8",
            )
            _run(["vale", str(body)], target, "pr-body-lint-failed")
            publication_started = True
            _run(["git", "push", "-u", "origin", branch], tree, "push-failed")
            url = _run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repo,
                    "--base",
                    "main",
                    "--head",
                    branch,
                    "--title",
                    f"fix: address #{issue}",
                    "--body-file",
                    str(body),
                ],
                tree,
                "pr-create-failed",
            ).strip()
            if not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/[1-9][0-9]*", url):
                raise LandError("invalid-pr-url")
            return _record_pr(target, task_id, branch, url)
        finally:
            _run(["git", "worktree", "remove", "--force", str(tree)], target, "worktree-cleanup-failed")
            if not publication_started:
                _run(["git", "branch", "-D", branch], target, "branch-cleanup-failed")
