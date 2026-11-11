"""Explicit, isolated promotion of a registered Codex Cloud diff to a PR."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import shlex
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from . import cloud_tracker, codex_cloud, isolated_exec, proc
from .inbox_lock import InboxLockTimeout, held_file_lock


class LandError(ValueError):
    """A safe named refusal, without provider output or diff content."""


def _environment() -> dict[str, str]:
    # Git control variables can redirect even discovery and cleanup commands.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("GIT_", "PYTHON", "LD_", "DYLD_"))
        and key not in {"BASH_ENV", "ENV", "CDPATH", "NODE_OPTIONS", "NODE_PATH"}
    }


def _run(
    argv: list[str],
    cwd: Path,
    reason: str,
    *,
    stdin: bytes | None = None,
    environment: dict[str, str] | None = None,
    optional: bool = False,
) -> str:
    try:
        result = proc.run(argv, cwd=cwd, timeout=240, stdin=stdin, env=environment or _environment())
    except (OSError, TimeoutError) as exc:
        raise LandError(reason + ":network" if reason == "push-failed" else reason) from exc
    if optional and result.code == 1 and not result.stdout and not result.stderr:
        return ""
    if result.code or result.output_limit_exceeded or result.decode_failed or result.incomplete_process_group:
        if reason == "push-failed":
            output = (result.stderr + result.stdout).lower()
            category = "rejected"
            if any(word in output for word in ("pre-push", "content-guard", "hook declined", "guard refused")):
                category = "guard-refused"
            elif any(
                word in output
                for word in ("authentication", "could not read username", "permission denied", "403", "401")
            ):
                category = "auth"
            elif result.code == 124 or any(
                word in output for word in ("resolve host", "timed out", "timeout", "connect", "network", "tls")
            ):
                category = "network"
            raise LandError("push-failed:" + category)
        raise LandError(reason)
    return result.stdout


class _LandingGit:
    """One sanitized Git context, also inherited by the trusted guard's Git calls."""

    def __init__(self, target: Path, root: Path):
        self.target = target
        environment = _environment()
        directories = [
            str(Path(part).resolve())
            for part in os.get_exec_path(environment)
            if part and Path(part).is_absolute() and not Path(part).resolve().is_relative_to(target)
        ]
        self.tools = {}
        for name in ("git", "gh", "codex", "brigade", "vale"):
            resolved = shutil.which(name, path=os.pathsep.join(directories))
            if not resolved or Path(resolved).resolve().is_relative_to(target):
                raise LandError("trusted-tool-unavailable")
            self.tools[name] = str(Path(resolved).resolve())
        # Hooks invoke tools by name. Pin those names before any ambient PATH entry.
        tools_dir = root / "tools"
        tools_dir.mkdir(mode=0o700)
        for name, executable in self.tools.items():
            if name != "brigade":
                (tools_dir / name).symlink_to(executable)
        # Hooks use the same trusted package as landing, in isolated Python mode.
        # The final bootstrap operand is the Git working directory at invocation time.
        brigade = tools_dir / "brigade"
        brigade.write_text(
            f"#!{sys.executable} -I\nimport os, subprocess, sys\nargv = "
            + repr(
                isolated_exec.package_argv(
                    isolated_exec.trusted_cwd() / "__init__.py", "brigade", cwd_after_load=target
                )
            )
            + "\nargv[-1] = os.getcwd()\nraise SystemExit(subprocess.call(argv + sys.argv[1:]))\n",
            encoding="utf-8",
        )
        brigade.chmod(0o700)
        self.tools["brigade"] = str(brigade)
        environment["PATH"] = os.pathsep.join([str(tools_dir), *directories])
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GH_PROMPT_DISABLED"] = "1"
        self.environment = environment
        self.hooks = root / "hooks"
        self.hooks.mkdir(mode=0o700)
        self.config = [
            f"core.hooksPath={self.hooks}",
            "core.fsmonitor=false",
            "protocol.allow=never",
            "protocol.https.allow=always",
            "diff.external=",
            "commit.gpgsign=false",
            "tag.gpgsign=false",
            "gpg.program=",
            "gpg.ssh.program=",
            "gpg.x509.program=",
            "core.editor=",
            "core.pager=",
            "core.sshCommand=",
            "core.askPass=",
            "core.gitProxy=",
            "core.alternateRefsCommand=",
            "sequence.editor=",
            "interactive.diffFilter=",
            "credential.helper=",
            "fetch.recurseSubmodules=false",
            "submodule.recurse=false",
            "core.attributesFile=/dev/null",
            "core.autocrlf=false",
            "apply.whitespace=nowarn",
            "apply.ignoreWhitespace=no",
        ]
        # Discovery commands cannot execute filters or textconv. Disable every configured driver
        # before checkout, apply, diff or publication, including drivers selected by base attributes.
        names = self.git(
            [
                "config",
                "--null",
                "--name-only",
                "--get-regexp",
                r"^(filter\..*|merge\..*\.driver|diff\..*\.(textconv|command)|credential(\..*)?\.helper|pager\..*|difftool\..*\.cmd|mergetool\..*\.cmd|submodule\..*\.update)$",
            ],
            target,
            "git-config-failed",
            optional=True,
        )
        for name in filter(None, names.split("\0")):
            value = (
                "false"
                if name.endswith(".required") or name.startswith("pager.")
                else "none"
                if name.startswith("submodule.")
                else ""
            )
            self.config.append(f"{name}={value}")
        # Empty helper entries reset all prior helpers, including URL-scoped ones.
        # Retain exactly the trusted GitHub credential path after those resets.
        self.config.extend(
            ["credential.helper=", f"credential.helper=!{shlex.quote(self.tools['gh'])} auth git-credential"]
        )
        raw = self.git(
            ["config", "--global", "--path", "--get", "core.hooksPath"], target, "trusted-hook-required"
        ).strip()
        configured = Path(raw).expanduser()
        if not configured.is_absolute():
            raise LandError("trusted-hook-required")
        guard = (configured / "pre-push").resolve()
        common = Path(self.git(["rev-parse", "--git-common-dir"], target, "git-config-failed").strip())
        common = (target / common).resolve() if not common.is_absolute() else common.resolve()
        worktrees = self.git(["worktree", "list", "--porcelain", "-z"], target, "git-config-failed")
        roots = [
            Path(record.removeprefix("worktree ")).resolve()
            for record in worktrees.split("\0")
            if record.startswith("worktree ")
        ]
        if (
            any(guard.is_relative_to(worktree) for worktree in roots)
            or guard.is_relative_to(target)
            or guard.is_relative_to(common)
            or not guard.is_file()
            or not os.access(guard, os.X_OK)
        ):
            raise LandError("trusted-hook-required")
        # Only the operator-installed pre-push guard is retained. No commit or checkout hook runs.
        shutil.copyfile(guard, self.hooks / "pre-push")
        (self.hooks / "pre-push").chmod(0o700)

    def env(self) -> dict[str, str]:
        environment = dict(self.environment)
        environment["GIT_CONFIG_COUNT"] = str(len(self.config))
        for index, value in enumerate(self.config):
            key, setting = value.split("=", 1)
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = setting
        return environment

    def git(
        self, args: list[str], cwd: Path, reason: str, *, stdin: bytes | None = None, optional: bool = False
    ) -> str:
        options = [arg for value in self.config for arg in ("-c", value)]
        return _run(
            [self.tools["git"], *options, "--no-pager", *args],
            cwd,
            reason,
            stdin=stdin,
            environment=self.env(),
            optional=optional,
        )

    def tool(self, name: str, args: list[str], cwd: Path, reason: str) -> str:
        return _run([self.tools[name], *args], cwd, reason, environment=self.env())


def _check_path(value: str, tree: Path, hooks: tuple[Path, ...]) -> None:
    path = PurePosixPath(value)
    parts = tuple(part.lower() for part in path.parts)
    if (
        not parts
        or path.is_absolute()
        or ".." in parts
        or "\\" in value
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
    ):
        raise LandError("unsafe-path")
    if parts[0] == ".github" or "codeowners" in parts or parts[-1] in {".gitattributes", ".gitmodules"}:
        raise LandError("protected-policy-path")
    hook_configs = {
        ".pre-commit-config.yaml",
        ".pre-commit-hooks.yaml",
        "lefthook.yml",
        "lefthook.yaml",
        "lefthook.toml",
        "lefthook.json",
        ".lefthook.yml",
        ".lefthook.yaml",
        ".lefthook.toml",
        ".lefthook.json",
        ".overcommit.yml",
        ".simple-git-hooks.json",
        ".simple-git-hooks.js",
        ".simple-git-hooks.cjs",
        "simple-git-hooks.json",
        ".huskyrc",
        ".huskyrc.json",
        ".huskyrc.js",
        ".huskyrc.cjs",
        "husky.config.js",
        "husky.config.cjs",
        ".lintstagedrc",
        "lint-staged.config.js",
        # These manifests can configure Husky, simple-git-hooks and other hook frameworks.
        "package.json",
        "pyproject.toml",
        ".pre-commit-config.yml",
        ".overcommit.yaml",
    }
    if any(
        part in {"hooks", ".githooks", ".husky", ".lefthook"}
        or part in hook_configs
        or part.startswith("lefthook-local.")
        for part in parts
    ):
        raise LandError("protected-hook-path")
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


def _diff_paths(diff: bytes, cwd: Path, git: _LandingGit) -> None:
    raw = git.git(["apply", "--numstat", "-z", "-"], cwd, "invalid-diff", stdin=diff)
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
            _check_path(path, cwd, ())
            count += 1
    if not count:
        raise LandError("empty-diff")


def _update(target: Path, entry: dict[str, Any], **updates: Any) -> None:
    """Compare the entry identity and revision before every short registry transition."""
    try:
        with cloud_tracker.registry_lock(target):
            registry = cloud_tracker.load_registry(target)
            matches = [row for row in registry["entries"] if row.get("id") == entry["id"]]
            if len(matches) != 1 or matches[0] != entry:
                raise LandError("registry-conflict")
            matches[0].update(updates)
            matches[0]["landing_revision"] = entry.get("landing_revision", 0) + 1
            cloud_tracker.save_registry(target, registry)
            entry.update(matches[0])
    except OSError as exc:
        raise LandError("registry-update-failed") from exc


def _record_pr(target: Path, entry: dict[str, Any], url: str) -> dict[str, str]:
    _update(
        target,
        entry,
        branch=entry["landing_branch"],
        pr_url=url,
        landing_state="pr-created",
        expected_artifact={"kind": "branch"},
    )
    return {"task_id": entry["task_id"], "branch": entry["landing_branch"], "pr_url": url}


def land(target: Path, task_id: str, issue: int, *, closes: bool = False) -> dict[str, str]:
    """Apply one pinned attempt in isolation, then publish its verified commit for review."""
    if not re.fullmatch(r"task_[A-Za-z0-9_-]{3,160}", task_id) or issue < 1:
        raise LandError("invalid-task-or-issue")
    target = target.resolve()
    try:
        with held_file_lock(cloud_tracker.registry_path(target).parent / f"land-{task_id}.lock", deadline_seconds=5):
            with cloud_tracker.registry_lock(target):
                registry = cloud_tracker.load_registry(target)
                entries = [
                    row
                    for row in registry["entries"]
                    if row.get("provider") == "codex-cloud" and row.get("task_id") == task_id
                ]
                if len(entries) != 1:
                    raise LandError("task-not-uniquely-registered")
                entry = dict(entries[0])
            if entry.get("pr_url"):
                return {"task_id": task_id, "branch": entry["branch"], "pr_url": entry["pr_url"]}
            with tempfile.TemporaryDirectory(prefix="brigade-cloud-land-") as temporary:
                root = Path(temporary)
                git = _LandingGit(target, root)
                if git.git(["rev-parse", "--is-shallow-repository"], target, "inspect-failed").strip() != "false":
                    raise LandError("shallow-target")
                origin = git.git(["config", "--get", "remote.origin.url"], target, "github-origin-required").strip()
                match = re.fullmatch(
                    r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", origin
                )
                if not match:
                    raise LandError("github-origin-required")
                repo = match[1]
                canonical = f"https://github.com/{repo}.git"
                return _land_locked(target, entry, issue, closes, git, root, repo, canonical)
    except InboxLockTimeout as exc:
        raise LandError("landing-busy") from exc


def _land_locked(
    target: Path,
    entry: dict[str, Any],
    issue: int,
    closes: bool,
    git: _LandingGit,
    root: Path,
    repo: str,
    canonical: str,
) -> dict[str, str]:
    branch = entry.get("landing_branch") or f"codex/issue-{issue}-{uuid4().hex[:12]}"
    if not re.fullmatch(r"codex/issue-[1-9][0-9]*-[a-f0-9]{12}", branch):
        raise LandError("invalid-landing-branch")
    _update(target, entry, landing_branch=branch)
    commit = entry.get("landing_commit")
    remote = git.git(
        ["ls-remote", "--refs", canonical, f"refs/heads/{branch}"], target, "ref-inspection-failed"
    ).split()
    remote_sha = remote[0] if remote else None
    local = git.git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], target, "ref-inspection-failed", optional=True
    ).strip()
    if commit:
        if (local and local != commit) or (remote_sha and remote_sha != commit):
            raise LandError("landing-ref-mismatch")
        if not local and not remote_sha:
            _update(target, entry, landing_commit=None, landing_state=None)
            commit = None
        else:
            found = git.tool(
                "gh",
                ["pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--json", "url", "--limit", "1"],
                target,
                "pr-recovery-failed",
            )
            try:
                inventory = json.loads(found)
                if not isinstance(inventory, list):
                    raise ValueError("invalid inventory")
                if inventory:
                    url = inventory[0]["url"]
                    _validate_pr_url(url, repo)
                    if remote_sha != commit:
                        raise LandError("landing-ref-mismatch")
                    return _record_pr(target, entry, url)
            except (ValueError, KeyError, TypeError) as exc:
                raise LandError("pr-recovery-failed") from exc
    elif local or remote_sha:
        # An interrupted preparation may have left the base branch behind.
        if remote_sha or not entry.get("landing_base") or local != entry["landing_base"]:
            raise LandError("landing-ref-mismatch")
        git.git(["branch", "-D", branch], target, "branch-cleanup-failed")
    body = root / "pr-body.md"
    body.write_text(
        f"{'Closes' if closes else 'Refs'} #{issue}.\n\nCodex Cloud task {entry['task_id']}.\n\n"
        "Applied in an isolated worktree from main. Content guard passed.\n"
        "Conductor review and merge required.\n",
        encoding="utf-8",
    )
    git.tool("vale", [str(body)], target, "pr-body-lint-failed")
    if not commit:
        commit = _prepare(target, entry, issue, git, root, branch, canonical)
    # Recheck the entry revision immediately before publication.
    _update(target, entry, landing_commit=commit)
    publish_tree = root / "publication"
    # The push guard must inspect the preserved commit, never the caller's dirty checkout.
    if not local:
        local = git.git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], target, "ref-inspection-failed", optional=True
        ).strip()
    if local:
        git.git(["worktree", "add", str(publish_tree), branch], target, "worktree-failed")
    else:
        git.git(["fetch", "--no-tags", canonical, f"refs/heads/{branch}"], target, "fetch-failed")
        if git.git(["rev-parse", "FETCH_HEAD"], target, "inspect-failed").strip() != commit:
            raise LandError("landing-ref-mismatch")
        git.git(["worktree", "add", "--detach", str(publish_tree), commit], target, "worktree-failed")
    try:
        if git.git(["rev-parse", "HEAD^{tree}"], publish_tree, "inspect-failed").strip() != entry["landing_tree"]:
            raise LandError("commit-tree-mismatch")
        if not remote_sha:
            git.git(["push", canonical, f"{commit}:refs/heads/{branch}"], publish_tree, "push-failed")
        _update(target, entry, landing_state="branch-pushed", landing_commit=commit)
        url = git.tool(
            "gh",
            [
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                entry["landing_title"],
                "--body-file",
                str(body),
            ],
            publish_tree,
            "pr-create-failed",
        ).strip()
    finally:
        git.git(["worktree", "remove", "--force", str(publish_tree)], target, "worktree-cleanup-failed")
    _validate_pr_url(url, repo)
    return _record_pr(target, entry, url)


def _validate_pr_url(url: str, repo: str) -> None:
    if not isinstance(url, str) or not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/[1-9][0-9]*", url):
        raise LandError("invalid-pr-url")


def _prepare(
    target: Path, entry: dict[str, Any], issue: int, git: _LandingGit, root: Path, branch: str, canonical: str
) -> str:
    status = git.tool("codex", ["cloud", "status", entry["task_id"]], target, "status-failed")
    if codex_cloud._verified_status(status) not in codex_cloud.TERMINAL_OK:
        raise LandError("task-not-ready")
    title_match = re.match(r"\s*\[[A-Z_]+\]\s*([^\r\n]*)", status)
    title = " ".join(title_match[1].split()) if title_match else ""
    title = "".join(c for c in title if ord(c) >= 32 and not 127 <= ord(c) <= 159)[:200]
    if not re.match(r"^(fix|feat)(\([^)]+\))?:\s", title):
        title = "fix: " + re.sub(r"^[a-z]+(?:\([^)]+\))?:\s*", "", title) if title else f"fix: address #{issue}"
    git.git(["fetch", "--no-tags", canonical, "refs/heads/main"], target, "fetch-failed")
    base = git.git(["rev-parse", "FETCH_HEAD"], target, "inspect-failed").strip()
    _update(target, entry, landing_base=base, landing_title=title, landing_attempt=1)
    tree = root / "worktree"
    git.git(["worktree", "add", "-b", branch, str(tree), base], target, "worktree-failed")
    prepared = False
    try:
        diff = git.tool("codex", ["cloud", "diff", "--attempt", "1", entry["task_id"]], target, "diff-failed").encode(
            "utf-8"
        )
        _diff_paths(diff, tree, git)
        git.git(["apply", "--index", "-"], tree, "apply-conflict", stdin=diff)
        expected_tree = git.git(["write-tree"], tree, "inspect-failed").strip()
        paths = git.git(["diff", "--cached", "--name-only", "--no-renames", "-z"], tree, "inspect-failed").split("\0")
        if not any(paths):
            raise LandError("empty-diff")
        for name in filter(None, paths):
            _check_path(name, tree, ())
            path = tree / name
            if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
                raise LandError("unsafe-file-type")
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
        # Receipt output never enters the task tree, including tracked memory symlinks.
        receipts = root / "verification"
        receipts.mkdir(mode=0o700)
        git.tool(
            "brigade",
            ["work", "verify", "run", "--target", str(receipts), "--command", str(check), "--capture", "brigade-work"],
            target,
            "guard-failed",
        )
        if git.git(["write-tree"], tree, "inspect-failed").strip() != expected_tree:
            raise LandError("guard-tree-mismatch")
        message = f"{title}\n\nCo-Authored-By: Codex <codex@openai.com>"  # content-guard: allow pii/email
        git.git(["commit", "--no-gpg-sign", "-m", message], tree, "commit-failed")
        if git.git(["rev-parse", "HEAD^{tree}"], tree, "inspect-failed").strip() != expected_tree:
            raise LandError("commit-tree-mismatch")
        commit = git.git(["rev-parse", "HEAD"], tree, "inspect-failed").strip()
        _update(
            target,
            entry,
            landing_state="prepared",
            landing_commit=commit,
            landing_diff_sha256=hashlib.sha256(diff).hexdigest(),
            landing_tree=expected_tree,
        )
        prepared = True
        return commit
    finally:
        git.git(["worktree", "remove", "--force", str(tree)], target, "worktree-cleanup-failed")
        if not prepared:
            git.git(["branch", "-D", branch], target, "branch-cleanup-failed")
            # A concurrent entry replacement must never be overwritten during cleanup.
            try:
                _update(target, entry, landing_branch=None, landing_commit=None, landing_state=None)
            except LandError:
                pass
