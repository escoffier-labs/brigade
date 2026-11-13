"""Explicit, isolated promotion of a registered Codex Cloud diff to a PR."""

from __future__ import annotations

import configparser
from collections import Counter
import hashlib
import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from . import cloud_tracker, codex_cloud, isolated_exec, proc
from .guard.policy import default_policy
from .inbox_lock import InboxLockTimeout, held_file_lock


class LandError(ValueError):
    """A safe named refusal, without provider output or diff content."""


_CREDENTIALS = {
    "git": {"GH_TOKEN", "GITHUB_TOKEN"},
    "gh": {"GH_TOKEN", "GITHUB_TOKEN"},
    # Codex authenticates through the pinned CODEX_HOME login. No provider API key passes through.
    "codex": set(),
    "vale": set(),
    "brigade": {"GH_TOKEN", "GITHUB_TOKEN"},
}


def _environment(tool: str = "git") -> dict[str, str]:
    allowed = {"HOME", "PATH", "LANG"} | _CREDENTIALS[tool]
    return {key: value for key, value in os.environ.items() if key in allowed or re.fullmatch(r"LC_[A-Z_]+", key)}


def _operator_directory(value: str, roots: list[Path]) -> Path:
    try:
        path = Path(value)
        if not path.is_absolute():
            raise LandError("unsafe-git-config")
        path = path.resolve()
        existing = path
        while not existing.exists():
            existing = existing.parent
        if (
            not existing.is_dir()
            or existing.stat().st_uid != os.getuid()
            or any(path.is_relative_to(root) for root in roots)
        ):
            raise LandError("unsafe-git-config")
        return path
    except (OSError, RuntimeError) as exc:
        raise LandError("unsafe-git-config") from exc


def _env_interpreter(executable: Path) -> str | None:
    """Return the interpreter name a ``#!/usr/bin/env NAME`` launcher needs, if any."""
    try:
        with executable.open("rb") as handle:
            first = handle.readline(256)
    except OSError:
        return None
    parts = first.decode("ascii", "replace").strip().split()
    if len(parts) < 2 or parts[0] not in {"#!/usr/bin/env", "#!/bin/env"}:
        return None
    name = next((part for part in parts[1:] if not part.startswith("-")), None)
    return name if name and re.fullmatch(r"[A-Za-z0-9._+-]+", name) else None


def _python_wrapper(code: str) -> str:
    return f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -c {shlex.quote(code)} "$@"\n'


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
        self.repository = root / "repository"
        self.repository.mkdir(mode=0o700, exist_ok=True)
        environment = _environment()
        common = _caller_git_dir(target)
        roots = [target, common, common.parent, root]
        for gitdir in (common / "worktrees").glob("*/gitdir"):
            roots.append(Path(gitdir.read_text(encoding="utf-8").strip()).parent.resolve())
        home = _operator_directory(environment.get("HOME", str(Path.home())), roots)
        self.home = home
        self.roots = roots
        xdg = _operator_directory(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")), roots)
        pinned = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(xdg),
            "GH_CONFIG_DIR": str(_operator_directory(os.environ.get("GH_CONFIG_DIR", str(xdg / "gh")), roots)),
            "CODEX_HOME": str(_operator_directory(os.environ.get("CODEX_HOME", str(home / ".codex")), roots)),
            "GH_HOST": "github.com",
        }
        environment.update(pinned)
        self.environments = {name: {**_environment(name), **pinned} for name in _CREDENTIALS}
        directories = [
            str(Path(part).resolve())
            for part in os.get_exec_path(environment)
            if part and Path(part).is_absolute() and not any(Path(part).resolve().is_relative_to(p) for p in roots)
        ]
        self.tools = {}
        for name in ("git", "gh", "codex", "brigade", "vale"):
            resolved = shutil.which(name, path=os.pathsep.join(directories))
            if not resolved or any(Path(resolved).resolve().is_relative_to(proposal) for proposal in roots):
                raise LandError("trusted-tool-unavailable")
            self.tools[name] = str(Path(resolved).resolve())
        tool_directories = [str(Path(tool).parent) for tool in self.tools.values()]
        # Hooks invoke tools by name. Pin those names before the tool directories.
        tools_dir = root / "tools"
        tools_dir.mkdir(mode=0o700, exist_ok=True)
        pinned_names = dict(self.tools)
        # A launcher such as `#!/usr/bin/env node` needs its interpreter, which may
        # live outside the tool directories. Pin it from the same trusted search.
        for executable in list(self.tools.values()):
            interpreter = _env_interpreter(Path(executable))
            if interpreter and interpreter not in pinned_names:
                resolved = shutil.which(interpreter, path=os.pathsep.join(directories))
                if not resolved or any(Path(resolved).resolve().is_relative_to(p) for p in roots):
                    raise LandError("trusted-tool-unavailable")
                pinned_names[interpreter] = str(Path(resolved).resolve())
        for name, executable in pinned_names.items():
            if name != "brigade":
                (tools_dir / name).unlink(missing_ok=True)
                (tools_dir / name).symlink_to(executable)
        # Hooks use the same trusted package as landing, in isolated Python mode.
        # The final bootstrap operand is the Git working directory at invocation time.
        brigade = tools_dir / "brigade"
        brigade.write_text(
            _python_wrapper(
                "import os, subprocess, sys\nargv = "
                + repr(
                    isolated_exec.package_argv(
                        isolated_exec.trusted_cwd() / "__init__.py", "brigade", cwd_after_load=target
                    )
                )
                + "\nargv[-1] = os.getcwd()\nraise SystemExit(subprocess.call(argv + sys.argv[1:]))\n"
            ),
            encoding="utf-8",
        )
        brigade.chmod(0o700)
        self.tools["brigade"] = str(brigade)
        environment["PATH"] = os.pathsep.join(dict.fromkeys([str(tools_dir), *tool_directories, "/usr/bin", "/bin"]))
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GH_PROMPT_DISABLED"] = "1"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CEILING_DIRECTORIES"] = str(root)
        environment["BRIGADE_GUARD_BACKEND"] = "python"
        # Receipt indexing is outside landing's guard operation.
        environment["BRIGADE_EVIDENCE_BIN"] = os.devnull
        self.environment = environment

        if not (self.repository / ".git").exists():
            _run(
                [self.tools["git"], "init", "--template=", "--initial-branch=main", "."],
                self.repository,
                "git-init-failed",
                environment={**environment, "GIT_CONFIG_GLOBAL": os.devnull},
            )

        # Read only operator global settings, including its trusted includes.
        def operator(*args: str, optional: bool = False) -> str:
            return _run(
                [self.tools["git"], "config", "--global", "--includes", *args],
                self.repository,
                "unsafe-git-config",
                environment=environment,
                optional=optional,
            ).strip()

        identity = {name: operator("--get", name) for name in ("user.name", "user.email")}
        helper = operator("--get-urlmatch", "credential.helper", "https://github.com")
        try:
            words = shlex.split(helper.removeprefix("!"))
        except ValueError as exc:
            raise LandError("unsafe-git-config") from exc
        if words and words[1:] == ["auth", "git-credential"]:
            configured_gh = Path(self.tools["gh"]) if words[0] == "gh" else Path(words[0])
            try:
                if not configured_gh.is_absolute() or configured_gh.resolve(strict=True) != Path(self.tools["gh"]):
                    raise LandError("unsafe-git-config")
            except (OSError, RuntimeError) as exc:
                raise LandError("unsafe-git-config") from exc
            credential_executable = Path(self.tools["gh"])
            arguments = " auth git-credential"
        elif len(words) == 1 and Path(words[0]).is_absolute() and not helper.startswith("!"):
            try:
                credential_executable = Path(words[0]).resolve(strict=True)
                if any(credential_executable.is_relative_to(protected) for protected in roots):
                    raise LandError("unsafe-git-config")
                for ancestor in (credential_executable, *credential_executable.parents):
                    metadata = ancestor.stat()
                    if metadata.st_uid not in {0, os.getuid()} or metadata.st_mode & 0o022:
                        raise LandError("unsafe-git-config")
            except (OSError, RuntimeError) as exc:
                raise LandError("unsafe-git-config") from exc
            arguments = ""
        else:
            raise LandError("unsafe-git-config")
        try:
            metadata = credential_executable.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not os.access(credential_executable, os.X_OK)
                or metadata.st_uid not in {0, os.getuid()}
            ):
                raise LandError("unsafe-git-config")
        except OSError as exc:
            raise LandError("unsafe-git-config") from exc
        helper = "!" + shlex.quote(str(credential_executable.resolve())) + arguments
        configured = Path(operator("--path", "--get", "core.hooksPath")).expanduser()
        guard = (configured / "pre-push").resolve()
        if (
            not configured.is_absolute()
            or any(guard.is_relative_to(worktree) for worktree in roots)
            or not guard.is_file()
            or not os.access(guard, os.X_OK)
        ):
            raise LandError("trusted-hook-required")
        self.hooks = root / "hooks"
        if self.hooks.exists():
            shutil.rmtree(self.hooks)
        self.hooks.mkdir(mode=0o700)
        shutil.copyfile(guard, self.hooks / "pre-push")
        (self.hooks / "pre-push").chmod(0o700)
        # The hook and metadata scans share trusted, pinned policy bytes.
        try:
            public = json.loads(default_policy("public-repo.json").read_text())
            private = home / ".config" / "content-guard" / "internal.json"
            extra = tools_dir / "private-policy.json"
            extra.unlink(missing_ok=True)
            self.policies = [tools_dir / "public-repo.json"]
            if private.exists():
                private = private.resolve(strict=True)
                if (
                    not private.is_file()
                    or private.stat().st_uid not in {0, os.getuid()}
                    or any(private.is_relative_to(proposal) for proposal in roots)
                ):
                    raise LandError("unsafe-git-config")
                private_policy = json.loads(private.read_text(encoding="utf-8"))
                private_policy["allow_agent_coauthors"] = True
                allowed = private_policy.get("allow_values", [])
                if not isinstance(allowed, list) or not all(isinstance(value, str) for value in allowed):
                    raise LandError("unsafe-git-config")
                public["allow_values"] = [*public.get("allow_values", []), *allowed]
                extra.write_text(json.dumps(private_policy), encoding="utf-8")
                extra.chmod(0o600)
                self.policies.append(extra)
            public["allow_agent_coauthors"] = True
            self.policies[0].write_text(json.dumps(public), encoding="utf-8")
            self.policies[0].chmod(0o600)
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise LandError("unsafe-git-config") from exc
        environment["CONTENT_GUARD_POLICY"] = str(self.policies[0])
        environment["CONTENT_GUARD_EXTRA_POLICY"] = str(extra)
        self.config = [
            *(f"{key}={value}" for key, value in identity.items()),
            f"core.hooksPath={self.hooks}",
            "protocol.allow=never",
            "protocol.https.allow=always",
            "http.sslVerify=true",
            "commit.gpgsign=false",
            "tag.gpgsign=false",
            f"credential.helper={helper}",
        ]
        # Git writes this file, so identity and paths receive Git's own escaping.
        global_config = root / "gitconfig"
        global_config.unlink(missing_ok=True)
        global_config.touch(mode=0o600)
        environment["GIT_CONFIG_GLOBAL"] = str(global_config)
        for setting in self.config:
            key, value = setting.split("=", 1)
            _run(
                [self.tools["git"], "config", "--file", str(global_config), key, value],
                self.repository,
                "unsafe-git-config",
                environment=environment,
            )
        self._assert_config(self.repository)

    def _assert_config(self, cwd: Path) -> None:
        raw = _run(
            [self.tools["git"], "config", "--no-includes", "--null", "--list", "--show-origin"],
            cwd,
            "unsafe-git-config",
            environment=self.env(),
        )
        records = raw.split("\0")
        if records[-1] != "" or len(records) % 2 != 1:
            raise LandError("unsafe-git-config")
        pinned = {key.lower(): value for key, value in (setting.split("=", 1) for setting in self.config)}
        metadata = {
            "core.repositoryformatversion": {"0"},
            "core.filemode": {"true", "false"},
            "core.bare": {"false"},
            "core.logallrefupdates": {"true"},
            "core.ignorecase": {"true", "false"},
            "core.precomposeunicode": {"true", "false"},
        }
        seen = set()
        for record in records[1::2]:
            key, separator, value = record.partition("\n")
            key = key.lower()
            # Only the synthesized settings and Git's repository metadata may exist.
            # --no-includes lists include directives without opening their targets.
            if (
                not separator
                or key in seen
                or (value != pinned[key] if key in pinned else value not in metadata.get(key, set()))
            ):
                raise LandError("unsafe-git-config")
            seen.add(key)
        if not pinned.keys() <= seen:
            raise LandError("unsafe-git-config")

    def env(self, tool: str = "git") -> dict[str, str]:
        environment = self.environments[tool].copy()
        environment["PATH"] = self.environment["PATH"]
        if tool in {"git", "brigade"}:
            environment.update(self.environment)
        return environment

    def git(
        self, args: list[str], cwd: Path, reason: str, *, stdin: bytes | None = None, optional: bool = False
    ) -> str:
        if not cwd.resolve().is_relative_to(self.repository.parent):
            raise LandError("unsafe-git-config")
        self._assert_config(cwd)
        # Proposal file names reach pathspecs, so Git magic such as ":(literal)x" must not apply.
        return _run(
            [self.tools["git"], "--no-pager", *args],
            cwd,
            reason,
            stdin=stdin,
            environment={**self.env(), "GIT_LITERAL_PATHSPECS": "1"},
            optional=optional,
        )

    def tool(self, name: str, args: list[str], cwd: Path, reason: str) -> str:
        if not cwd.resolve().is_relative_to(self.repository.parent):
            raise LandError("unsafe-git-config")
        return _run([self.tools[name], *args], cwd, reason, environment=self.env(name))

    def scan_publication_text(self, *texts: str) -> None:
        for text in texts:
            self.scan_text(text, "title-refused")

    def blocking_findings(self, text: str) -> Counter[tuple[int, str, str]]:
        """Blocking findings in full text under every pinned policy, allows and frontmatter included."""
        found: Counter[tuple[int, str, str]] = Counter()
        for index, policy in enumerate(self.policies):
            argv = isolated_exec.module_argv(
                "brigade.guard",
                "scan",
                "-",
                "--policy",
                str(policy),
                "--no-allow-comments",
                "--scan-frontmatter",
                "--json",
            )
            try:
                result = proc.run(argv, cwd=self.repository, timeout=240, stdin=text.encode("utf-8"), env=self.env())
            except (OSError, TimeoutError) as exc:
                raise LandError("guard-failed") from exc
            if result.code not in (0, 1) or result.output_limit_exceeded or result.decode_failed:
                raise LandError("guard-failed")
            try:
                report = json.loads(result.stdout)
                findings = report["findings"]
                found.update(
                    (index, str(item["rule_id"]), str(item["match"]))
                    for item in findings
                    if item.get("action") == "block"
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise LandError("guard-failed") from exc
            # A scanner backend that could not run never counts as a clean scan.
            if any(str(item.get("rule_id")) in _SCANNER_FAILURES for item in findings):
                raise LandError("guard-failed")
            if result.code == 1 and not any(item.get("action") == "block" for item in findings):
                raise LandError("guard-failed")
        return found

    def scan_text(self, text: str, reason: str) -> None:
        """Refuse text with any blocking finding under every pinned policy, allows ignored."""
        if self.blocking_findings(text):
            raise LandError(reason)


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


_GUARD_DIRECTIVE = "content-guard:"
_SCANNER_FAILURES = frozenset({"opf-error", "opf-unavailable"})
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _added_lines(name: str, tree: Path, git: _LandingGit) -> list[str]:
    """Return the lines a proposal adds to one path, read by hunk line counts.

    Counting from the ``@@`` headers keeps an added line that starts with ``++``
    from being mistaken for a file header.
    """
    diff = git.git(
        [
            "diff",
            "--cached",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--text",
            "HEAD",
            "--",
            name,
        ],
        tree,
        "inspect-failed",
    )
    added: list[str] = []
    old = new = 0
    for line in diff.split("\n"):
        if old or new:
            if line.startswith("\\"):
                continue
            if line.startswith("-") and old:
                old -= 1
            elif line.startswith("+") and new:
                new -= 1
                added.append(line[1:])
            else:
                raise LandError("invalid-diff")
            continue
        if match := _HUNK.match(line):
            old = int(match.group(1) or "1")
            new = int(match.group(2) or "1")
    if old or new:
        raise LandError("invalid-diff")
    return added


def _blob_text(spec: str, name: str, tree: Path, git: _LandingGit) -> str | None:
    """Return one path's text from the index (``:``) or HEAD, or None when absent."""
    listing = (
        ["ls-files", "--cached", "-z", "--", name]
        if spec == ":"
        else ["ls-tree", "-z", "--name-only", "HEAD", "--", name]
    )
    if not git.git(listing, tree, "inspect-failed").strip("\0"):
        return None
    return git.git(["cat-file", "blob", f"{spec}{name}" if spec == ":" else f"HEAD:{name}"], tree, "inspect-failed")


def _refuse_unreviewed_exceptions(names: list[str], tree: Path, git: _LandingGit) -> None:
    """Unreviewed task content never publishes a new guard finding.

    The staged, tip and history scans honor allow directives and skip YAML
    frontmatter, and several rules span lines. A proposal could add, broaden
    or sit beside a reviewed exception, finish a finding on an unchanged line,
    or create one by deleting text. So each changed file is scanned whole,
    before and after, with allow comments ignored and frontmatter included,
    under each pinned policy, and any new blocking finding is refused. Path
    names are published too and are scanned the same way. Added directives
    are refused outright. A needed exception is added by the operator during
    review.
    """
    added = [line for name in names for line in _added_lines(name, tree, git)]
    if any(_GUARD_DIRECTIVE in line for line in [*names, *added]):
        raise LandError("guard-directive-added")
    git.scan_text("\n".join(names) + "\n", "guard-failed")
    for name in names:
        after = _blob_text(":", name, tree, git)
        if after is None:
            continue
        before = _blob_text("HEAD", name, tree, git)
        new = git.blocking_findings(after) - (git.blocking_findings(before) if before is not None else Counter())
        if new:
            raise LandError("guard-failed")


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


def _caller_git_dir(target: Path) -> Path:
    """Read caller metadata as data without evaluating its Git configuration."""
    try:
        marker = target / ".git"
        directory = marker
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir: "):
                raise LandError("github-origin-required")
            directory = target / text[8:]
        directory = directory.resolve()
        if (directory / "commondir").is_file():
            directory = (directory / (directory / "commondir").read_text(encoding="utf-8").strip()).resolve()
        return directory
    except (OSError, UnicodeError) as exc:
        raise LandError("github-origin-required") from exc


def _private_root(saved: str | None, target: Path) -> Path:
    def valid_base(path: Path) -> bool:
        try:
            metadata = path.lstat()
            return (
                path.is_absolute()
                and path.resolve() == path
                and stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o700
            )
        except OSError:
            return False

    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    base = runtime if valid_base(runtime) else target / ".brigade" / "cloud" / "landing"
    if base != runtime:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not valid_base(base):
        raise LandError("unsafe-git-config")
    if not saved:
        return Path(tempfile.mkdtemp(prefix="brigade-cloud-land-", dir=base)).resolve()
    root = Path(saved)
    if root.parent != base or not root.name.startswith("brigade-cloud-land-") or not valid_base(root):
        raise LandError("unsafe-git-config")
    return root


def land(target: Path, task_id: str, issue: int, *, closes: bool = False) -> dict[str, str]:
    """Apply one pinned attempt in isolation, then publish its verified commit for review."""
    if os.name != "posix":
        raise LandError("unsupported-platform")
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
            common = _caller_git_dir(target)
            if (common / "shallow").exists():
                raise LandError("shallow-target")
            try:
                config = configparser.RawConfigParser(strict=False)
                config.read_string((common / "config").read_text(encoding="utf-8"))
                origin = config.get('remote "origin"', "url").strip().strip('"')
            except (OSError, configparser.Error, UnicodeError) as exc:
                raise LandError("github-origin-required") from exc
            match = re.fullmatch(
                r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", origin
            )
            if not match:
                raise LandError("github-origin-required")
            repo = match[1]
            canonical = f"https://github.com/{repo}.git"
            root = _private_root(entry.get("landing_workspace"), target)
            try:
                _update(target, entry, landing_workspace=str(root))
                git = _LandingGit(target, root)
                result = _land_locked(target, entry, issue, closes, git, root, repo, canonical)
            except BaseException:
                # Keep a prepared commit for a retry without borrowing the caller's object store.
                if not entry.get("landing_commit"):
                    shutil.rmtree(root)
                    try:
                        _update(target, entry, landing_workspace=None)
                    except LandError:
                        pass
                raise
            shutil.rmtree(root)
            _update(target, entry, landing_workspace=None)
            return result
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
    checkout = _reconcile_checkouts(git, root, branch, commit)
    remote = git.git(
        ["ls-remote", "--refs", canonical, f"refs/heads/{branch}"], git.repository, "ref-inspection-failed"
    ).split()
    remote_sha = remote[0] if remote else None
    local = git.git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        git.repository,
        "ref-inspection-failed",
        optional=True,
    ).strip()
    if commit:
        if remote_sha and remote_sha != commit:
            raise LandError("landing-ref-mismatch")
        if not local and not remote_sha:
            _update(target, entry, landing_commit=None, landing_state=None)
            commit = None
            checkout = None
        else:
            found = git.tool(
                "gh",
                ["pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--json", "url", "--limit", "1"],
                git.repository,
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
        if remote_sha:
            raise LandError("landing-ref-mismatch")
        # Unrecorded preparation refs belong only to this private repository.
        git.git(["branch", "-D", branch], git.repository, "branch-cleanup-failed")
    body = root / "pr-body.md"
    body.write_text(
        f"{'Closes' if closes else 'Refs'} #{issue}.\n\nCodex Cloud task {entry['task_id']}.\n\n"
        "Applied in an isolated worktree from main. Content guard passed.\n"
        "Conductor review and merge required.\n",
        encoding="utf-8",
    )
    # Vale configuration comes only from the trusted base commit. A retry (landing_commit
    # already recorded) lints against the base recorded when the commit was built and never
    # fetches main again. Only a landing with no recorded base fetches.
    recorded = str(entry.get("landing_base") or "") if commit else ""
    if recorded:
        if not re.fullmatch(r"[0-9a-f]{40}", recorded):
            raise LandError("inspect-failed")
        base = git.git(
            ["rev-parse", "--verify", "--quiet", f"{recorded}^{{commit}}"], git.repository, "inspect-failed"
        ).strip()
        # The commit was built directly on the recorded base, so that base can never be the proposal.
        if git.git(["rev-parse", f"{commit}^"], git.repository, "inspect-failed").strip() != base:
            raise LandError("inspect-failed")
    else:
        base = _fetch_base(git, canonical)
    config = _materialize_vale_config(git, root, base)
    # Vale always gets an explicit --config: the validated base .vale.ini copy, else the
    # operator's own $HOME/.vale.ini when it is a plain file they own, else a synthesized
    # minimal ini that lints nothing. Without --config Vale searches upward from its working
    # directory, which can reach the caller checkout when the private root falls back to a
    # directory under the target. --no-global is deliberately not passed, so the
    # operator-owned global Vale config still merges.
    git.tool("vale", ["--config", str(config), str(body)], git.repository, "pr-body-lint-failed")
    if not commit:
        commit = _prepare(target, entry, issue, git, root, branch, base)
    else:
        git.scan_publication_text(
            entry["landing_title"],
            git.git(["show", "-s", "--format=%B", commit], git.repository, "inspect-failed").strip(),
            body.read_text(encoding="utf-8"),
        )
    # Recheck the entry revision immediately before publication.
    _update(target, entry, landing_commit=commit)
    publish_tree = checkout or root / "publication"
    # The push guard must inspect the preserved commit, never the caller's dirty checkout.
    if not local:
        local = git.git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            git.repository,
            "ref-inspection-failed",
            optional=True,
        ).strip()
    if not local:
        git.git(["fetch", "--no-tags", canonical, f"refs/heads/{branch}"], git.repository, "fetch-failed")
        if git.git(["rev-parse", "FETCH_HEAD"], git.repository, "inspect-failed").strip() != commit:
            raise LandError("landing-ref-mismatch")
    if checkout is None:
        git.git(["worktree", "add", "-B", branch, str(publish_tree), commit], git.repository, "worktree-failed")
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
        git.git(["worktree", "remove", "--force", str(publish_tree)], git.repository, "worktree-cleanup-failed")
    _validate_pr_url(url, repo)
    return _record_pr(target, entry, url)


def _reconcile_checkouts(git: _LandingGit, root: Path, branch: str, commit: str | None) -> Path | None:
    """Reconcile private checkouts before resuming the next pending landing step."""
    inventory = git.git(["worktree", "list", "--porcelain", "-z"], git.repository, "inspect-failed")
    registered: dict[Path, set[str]] = {}
    for record in inventory.split("\0\0"):
        fields = set(record.split("\0"))
        for field in fields:
            if not field.startswith("worktree "):
                continue
            tree = Path(field[9:])
            # The primary repository owns the object store, not a transient checkout.
            if tree == git.repository or not tree.is_relative_to(root):
                continue
            if any(
                tree.is_relative_to(protected) or protected.is_relative_to(tree)
                for protected in (git.repository, git.hooks, root / "tools")
            ):
                raise LandError("unsafe-git-config")
            relative = tree.relative_to(root)
            if not relative.parts or ".." in relative.parts:
                raise LandError("unsafe-git-config")
            registered[tree] = fields
    # Known step paths may remain after Git has dropped their registration.
    candidates = {*registered, root / "worktree", root / "publication"}
    checkout = None
    for tree in sorted(candidates):
        checkout_fields = registered.get(tree)
        # Remove a symlink component itself before Git can traverse that path.
        component = root
        for part in tree.relative_to(root).parts:
            component = component / part
            if component.is_symlink():
                component.unlink()
                break
        # A recorded commit makes publication the next step. Preparation without
        # a recorded commit is rebuilt, so partially applied state is never reused.
        matching = (
            checkout_fields is not None
            and f"HEAD {commit}" in checkout_fields
            and f"branch refs/heads/{branch}" in checkout_fields
        )
        if checkout is None and commit and matching and tree.is_dir():
            try:
                common = git.git(["rev-parse", "--git-common-dir"], tree, "inspect-failed").strip()
                if (
                    (tree / common).resolve() == (git.repository / ".git").resolve()
                    and git.git(["symbolic-ref", "--quiet", "HEAD"], tree, "inspect-failed", optional=True).strip()
                    == f"refs/heads/{branch}"
                    and git.git(["rev-parse", "HEAD"], tree, "inspect-failed").strip() == commit
                ):
                    checkout = tree
                    continue
            except LandError as exc:
                if str(exc) != "inspect-failed":
                    raise
        if checkout_fields is not None or tree.is_symlink() or tree.exists():
            try:
                git.git(["worktree", "remove", "--force", str(tree)], git.repository, "worktree-cleanup-failed")
            except LandError as exc:
                if str(exc) != "worktree-cleanup-failed" or (checkout_fields is not None and tree.is_dir()):
                    raise
            if tree.is_symlink():
                tree.unlink()
            elif tree.is_dir():
                shutil.rmtree(tree)
            else:
                tree.unlink(missing_ok=True)
    git.git(["worktree", "prune", "--expire", "now"], git.repository, "worktree-cleanup-failed")
    return checkout


def _validate_pr_url(url: str, repo: str) -> None:
    if not isinstance(url, str) or not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/[1-9][0-9]*", url):
        raise LandError("invalid-pr-url")


def _fetch_base(git: _LandingGit, canonical: str) -> str:
    git.git(["fetch", "--no-tags", canonical, "refs/heads/main"], git.repository, "fetch-failed")
    return git.git(["rev-parse", "FETCH_HEAD"], git.repository, "inspect-failed").strip()


_VALE_FILE_LIMIT = 4 * 1024 * 1024
_VALE_TOTAL_LIMIT = 64 * 1024 * 1024
_VALE_FILE_COUNT = 2000
_VALE_PATH_KEYS = {"stylespath", "transform", "vocab"}


def _git_blob_bytes(git: _LandingGit, sha: str) -> bytes:
    """Read one blob as raw bytes, bounded by _VALE_FILE_LIMIT."""
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise LandError("unsafe-vale-config")
    size = git.git(["cat-file", "-s", sha], git.repository, "inspect-failed").strip()
    if not size.isdigit() or int(size) > _VALE_FILE_LIMIT:
        raise LandError("unsafe-vale-config")
    git._assert_config(git.repository)
    # Raw bytes are needed, which proc.run cannot return. The size is bounded before and after.
    try:
        result = subprocess.run(  # noqa: S603 - pinned git binary, fixed argv
            [git.tools["git"], "--no-pager", "cat-file", "blob", sha],
            cwd=git.repository,
            env=git.env(),
            capture_output=True,
            timeout=240,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LandError("inspect-failed") from exc
    if result.returncode or len(result.stdout) > _VALE_FILE_LIMIT:
        raise LandError("unsafe-vale-config")
    return result.stdout


_VALE_KEY = re.compile(r"[A-Za-z][A-Za-z0-9._-]*\s*[=:]\s*(.*)")
_VALE_SECTION = re.compile(r"\[[^\]`\"]+\]")
_VALE_PATH_VALUE = re.compile(r"[A-Za-z0-9._/, -]+")


def _check_vale_ini(text: str) -> PurePosixPath | None:
    """Accept only a strict subset of INI that go-ini reads the way this check does.

    Everything else is refused, because each form go-ini reads differently from a
    hand-written parser is a way to point Vale outside the materialized directory.
    Returns the StylesPath, if any.
    """
    # A BOM or control character is read differently by go-ini than by this check.
    if re.search("[\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise LandError("unsafe-vale-config")
    styles: PurePosixPath | None = None
    seen: set[tuple[str, str]] = set()
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        # go-ini joins a line ending in a backslash with the next one.
        if line.endswith("\\"):
            raise LandError("unsafe-vale-config")
        if line.startswith("["):
            if not _VALE_SECTION.fullmatch(line) or ".." in line:
                raise LandError("unsafe-vale-config")
            # go-ini treats [DEFAULT] as the root section.
            section = "" if line.lower() == "[default]" else line.lower()
            continue
        found = _VALE_KEY.fullmatch(line)
        if not found:
            raise LandError("unsafe-vale-config")
        key = re.split(r"\s*[=:]", line, maxsplit=1)[0].lower()
        value = found[1].strip()
        if key in {"include", "includes", "import", "imports"} or (section, key) in seen:
            raise LandError("unsafe-vale-config")
        seen.add((section, key))
        if "`" in value or '"""' in value or re.search(r"\s[;#]", value):
            raise LandError("unsafe-vale-config")
        if key in _VALE_PATH_KEYS:
            # Path values are a closed character set, checked before any splitting.
            if not _VALE_PATH_VALUE.fullmatch(value):
                raise LandError("unsafe-vale-config")
            for part in re.split(r"[,\s]+", value):
                if part.startswith("/") or ".." in part.split("/"):
                    raise LandError("unsafe-vale-config")
        elif any(".." in part.split("/") for part in re.split(r"[,\s]+", value.strip("\"'"))):
            raise LandError("unsafe-vale-config")
        if key == "stylespath":
            # Vale reads StylesPath only from the root section.
            if section:
                raise LandError("unsafe-vale-config")
            styles = PurePosixPath(value)
            if styles.is_absolute() or ".." in styles.parts or not [p for p in styles.parts if p != "."]:
                raise LandError("unsafe-vale-config")
            styles = PurePosixPath(*[p for p in styles.parts if p != "."])
    return styles


def _operator_vale_config(git: _LandingGit) -> Path | None:
    """The operator's own $HOME/.vale.ini, when it is a plain file the operator owns."""
    path = git.home / ".vale.ini"
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            return None
        if any(path.resolve().is_relative_to(root) for root in git.roots):
            return None
    except OSError:
        return None
    return path


def _materialize_vale_config(git: _LandingGit, root: Path, base: str) -> Path:
    """Write the Vale config for the PR body lint into a fresh private directory.

    With a base .vale.ini this is a validated copy plus its StylesPath subtree, read as bytes
    from the base tree. Without one it is the operator's $HOME/.vale.ini when usable, else a
    synthesized minimal ini. Nothing is read from the proposal, the caller's checkout or a
    package download.
    """
    listing = git.git(["ls-tree", "-z", base, "--", ".vale.ini"], git.repository, "inspect-failed")
    text = ""
    styles: PurePosixPath | None = None
    if listing:
        if not listing.startswith("100644 blob ") and not listing.startswith("100755 blob "):
            raise LandError("unsafe-vale-config")
        text = git.git(["show", f"{base}:.vale.ini"], git.repository, "inspect-failed")
        styles = _check_vale_ini(text)
    elif operator := _operator_vale_config(git):
        # No base config: lint with the operator's own file, as Vale's upward search did before.
        return operator
    directory = root / "vale-config"
    # A retry reuses the private root, so rebuild the directory from the base.
    try:
        if directory.is_symlink():
            raise LandError("unsafe-vale-config")
        if directory.exists():
            shutil.rmtree(directory)
        if directory.exists() or directory.is_symlink():
            raise LandError("unsafe-vale-config")
        directory.mkdir(mode=0o700)
        ini = directory / ".vale.ini"
        # With neither a base nor an operator config this lints nothing: Vale reports 0 files.
        ini.write_text(text if listing else "MinAlertLevel = suggestion\n", encoding="utf-8")
        if styles is not None:
            _copy_vale_styles(git, base, directory, styles)
    except OSError as exc:
        raise LandError("unsafe-vale-config") from exc
    return ini


def _copy_vale_styles(git: _LandingGit, base: str, directory: Path, styles: PurePosixPath) -> None:
    # Every component of StylesPath must be a plain directory in the base tree.
    for index in range(1, len(styles.parts) + 1):
        prefix = "/".join(styles.parts[:index])
        entry = git.git(["ls-tree", "-z", base, "--", prefix], git.repository, "inspect-failed")
        if not entry.startswith("040000 tree "):
            raise LandError("unsafe-vale-config")
    destination = (directory / Path(*styles.parts)).resolve()
    if not destination.is_relative_to(directory.resolve()):
        raise LandError("unsafe-vale-config")
    tree = git.git(["ls-tree", "-r", "-z", base, "--", "/".join(styles.parts)], git.repository, "inspect-failed")
    records = list(filter(None, tree.split("\0")))
    if len(records) > _VALE_FILE_COUNT:
        raise LandError("unsafe-vale-config")
    total = 0
    for record in records:
        meta, _, name = record.partition("\t")
        fields = meta.split()
        if len(fields) != 3 or fields[0] not in {"100644", "100755"} or fields[1] != "blob":
            raise LandError("unsafe-vale-config")
        if ".." in PurePosixPath(name).parts:
            raise LandError("unsafe-vale-config")
        out = directory / Path(*PurePosixPath(name).parts)
        if not out.resolve().is_relative_to(directory.resolve()):
            raise LandError("unsafe-vale-config")
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = _git_blob_bytes(git, fields[2])
        total += len(data)
        if total > _VALE_TOTAL_LIMIT:
            raise LandError("unsafe-vale-config")
        out.write_bytes(data)


def _prepare(
    target: Path, entry: dict[str, Any], issue: int, git: _LandingGit, root: Path, branch: str, base: str
) -> str:
    status = git.tool("codex", ["cloud", "status", entry["task_id"]], git.repository, "status-failed")
    if codex_cloud._verified_status(status) not in codex_cloud.TERMINAL_OK:
        raise LandError("task-not-ready")
    title_match = re.match(r"\s*\[[A-Z_]+\]\s*([^\r\n]*)", status)
    title = " ".join(title_match[1].split()) if title_match else ""
    title = "".join(c for c in title if ord(c) >= 32 and not 127 <= ord(c) <= 159)[:200]
    if not re.match(r"^(fix|feat)(\([^)]+\))?:\s", title):
        title = "fix: " + re.sub(r"^[a-z]+(?:\([^)]+\))?:\s*", "", title) if title else f"fix: address #{issue}"
    message = f"{title}\n\nCo-Authored-By: Codex <codex@openai.com>"  # content-guard: allow pii/email
    git.scan_publication_text(title, message, (root / "pr-body.md").read_text(encoding="utf-8"))
    _update(target, entry, landing_base=base, landing_title=title, landing_attempt=1)
    tree = root / "worktree"
    git.git(["worktree", "add", "-b", branch, str(tree), base], git.repository, "worktree-failed")
    prepared = False
    try:
        diff = git.tool(
            "codex", ["cloud", "diff", "--attempt", "1", entry["task_id"]], git.repository, "diff-failed"
        ).encode("utf-8")
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
        _refuse_unreviewed_exceptions(list(filter(None, paths)), tree, git)
        check = root / "guard-check"
        # Scan staged content with each pinned policy, as the pre-push hook and the
        # publication-text scan do, so operator allow_values apply consistently.
        scans = [
            isolated_exec.package_argv(
                isolated_exec.trusted_cwd() / "__init__.py",
                "brigade",
                "guard",
                "git",
                "--staged",
                "--policy",
                str(policy),
                cwd_after_load=tree,
            )
            for policy in git.policies
        ]
        check.write_text(
            _python_wrapper(
                "import subprocess\nfor argv in "
                + repr(scans)
                + ":\n    code = subprocess.call(argv)\n    if code:\n        raise SystemExit(code)\n"
            ),
            encoding="utf-8",
        )
        check.chmod(0o700)
        # Receipt output never enters the task tree, including tracked memory symlinks.
        receipts = root / "verification"
        receipts.mkdir(mode=0o700)
        git.tool(
            "brigade",
            ["work", "verify", "run", "--target", str(receipts), "--command", str(check), "--capture", "brigade-work"],
            git.repository,
            "guard-failed",
        )
        if git.git(["write-tree"], tree, "inspect-failed").strip() != expected_tree:
            raise LandError("guard-tree-mismatch")
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
        git.git(["worktree", "remove", "--force", str(tree)], git.repository, "worktree-cleanup-failed")
        if not prepared:
            git.git(["branch", "-D", branch], git.repository, "branch-cleanup-failed")
            # A concurrent entry replacement must never be overwritten during cleanup.
            try:
                _update(target, entry, landing_branch=None, landing_commit=None, landing_state=None)
            except LandError:
                pass
