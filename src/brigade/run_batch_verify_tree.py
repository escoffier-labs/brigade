"""Prepare the coordinator's disposable verification checkout and trusted argv."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from time import monotonic

from .work_cmd import verify_required

# Protect the launchers, their repository helpers, test configuration, and
# Python startup files at any depth. Compare names against the pinned base.
_INFRA_DIRS = {"scripts", "verify", ".venv"}
_INFRA_NAMES = {
    ".gitattributes",
    ".gitignore",
    ".coveragerc",
    ".pytest.ini",
    ".ruff.toml",
    "conftest.py",
    "mypy.ini",
    "pyproject.toml",
    "pytest.ini",
    "ruff.toml",
    "setup.cfg",
    "sitecustomize.py",
    "tox.ini",
    "usercustomize.py",
}


def git_environment() -> dict[str, str]:
    return {
        **verify_required._planner_env(),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def git(target: Path, env: dict[str, str], *argv: str, deadline: float | None = None) -> bytes:
    result = subprocess.run(
        ["git", "--no-replace-objects", "--literal-pathspecs", "-c", "core.fsmonitor=false", *argv],
        cwd=target,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=verify_required._remaining(deadline, 30),
        check=True,
    )
    return result.stdout


def fingerprint(target: Path, env: dict[str, str], *, deadline: float) -> tuple[str, str]:
    """Compute the tree artifact collection would stage, leaving the actual index alone."""
    with tempfile.TemporaryDirectory() as temporary:
        index = Path(temporary) / "index"
        git_index = git(target, env, "rev-parse", "--path-format=absolute", "--git-path", "index", deadline=deadline)
        source_index = Path(os.fsdecode(git_index.rstrip(b"\n")))
        head = verify_required._commit_id(git(target, env, "rev-parse", "HEAD", deadline=deadline))
        index_env = {**env, "GIT_INDEX_FILE": str(index)}
        try:
            shutil.copyfile(source_index, index)
        except FileNotFoundError:
            git(target, index_env, "read-tree", head, deadline=deadline)
        git(target, index_env, "add", "-A", deadline=deadline)
        tree = verify_required._commit_id(git(target, index_env, "write-tree", deadline=deadline))
        return tree, head


def repository_runner(executable: str) -> str | None:
    path = Path(executable)
    if path.is_absolute() or ("/" not in executable and "\\" not in executable):
        return None
    name = executable.removeprefix("./")
    if not verify_required._repo_path(name):
        raise ValueError("invalid repository verifier")
    return name


def infrastructure_changed(
    source: Path, env: dict[str, str], base: str, tree: str, planner_files: list[str], runner: str | None
) -> bool:
    protected = {*planner_files, runner}
    changed = git(source, env, "diff-tree", "-r", "--name-only", "-z", "--no-renames", base, tree, "--")
    for raw in changed.split(b"\0"):
        name = os.fsdecode(raw)
        path = PurePosixPath(name.lower())
        if (
            name in protected
            or _INFRA_DIRS.intersection(path.parts)
            or path.name in _INFRA_NAMES
            or path.suffix == ".pth"
        ):
            return True
    return False


def command_environment(scratch: Path, git_env: dict[str, str]) -> dict[str, str]:
    """Fix tool resolution and give the commands a fresh home and temp path."""
    home = scratch / "home"
    home.mkdir()
    temp = scratch / "tmp"
    temp.mkdir()
    tools = Path(sys.executable).absolute().parent
    git_exe = shutil.which("git", path=git_env["PATH"]) if "PATH" in git_env else shutil.which("git")
    path_dirs = [str(tools), os.defpath]
    if git_exe and Path(git_exe).is_absolute():
        path_dirs.append(str(Path(git_exe).parent))
    return {
        **git_env,
        "PATH": os.pathsep.join(path_dirs),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home),
        "TMPDIR": str(temp),
        "TMP": str(temp),
        "TEMP": str(temp),
        "PY": str(tools),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }


def command(
    source: Path, target: Path, base: str, argv: list[str], runner: str | None, env: dict[str, str]
) -> list[str]:
    if runner is not None:
        entry = verify_required._tree_entries(source, base, runner, deadline=monotonic() + 30).get(runner)
        if runner != "scripts/verify-focused" or entry is None or not verify_required._regular(entry[0]):
            raise ValueError("repository verifier must be a regular file at the base")
        scripts = git(source, env, "ls-tree", "-r", "-z", base, "--", "scripts")
        if any(row and not verify_required._regular(row.split(b" ", 1)[0]) for row in scripts.split(b"\0")):
            raise ValueError("repository verifier helpers must be regular files at the base")
        executable = target / runner
    elif argv[0] in {"python", "python3", sys.executable}:
        return [str(Path(sys.executable).absolute()), "-P", "-s", *argv[1:]]
    else:
        resolved = shutil.which(argv[0], path=env["PATH"]) if not Path(argv[0]).is_absolute() else None
        if resolved is None:
            raise ValueError("verifier is unavailable in the coordinator environment")
        executable = Path(resolved).absolute()
    return [str(executable), *argv[1:]]


def materialize(source: Path, target: Path, env: dict[str, str], base: str, head: str, tree: str) -> None:
    """Populate a private repository with its own objects, config, and refs."""
    objects = git(source, env, "rev-parse", "--path-format=absolute", "--git-path", "objects").rstrip(b"\n")
    if not os.path.isabs(objects) or b"\n" in objects or b"\r" in objects:
        raise ValueError("invalid source object directory")
    target.mkdir()
    git(target, env, "init", "--quiet", "--template=", "--object-format=sha1")
    alternates = target / ".git/objects/info/alternates"
    alternates.write_bytes(objects + b"\n")
    git(target, env, "update-ref", "HEAD", head)
    git(target, env, "update-ref", "refs/remotes/origin/main", base)
    git(target, env, "read-tree", tree)
    git(target, env, "checkout-index", "--all")
    # Retain both commit history for the pinned runner and staged candidate blobs.
    git(target, env, "repack", "-a", "-d")
    alternates.unlink()
