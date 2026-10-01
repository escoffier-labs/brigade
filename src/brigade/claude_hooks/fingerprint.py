"""Read-only worktree fingerprint for the Claude Code work-loop hook.

The fingerprint detects whether a Bash call changed the repository. It reads
``git status``, ``git diff HEAD``, and ``lstat`` signatures of untracked
paths; it never reads untracked file content or writes git objects.
"""

from __future__ import annotations

import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import localio

_SNAPSHOT_IGNORE_DIRS = {
    ".brigade",
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
_SNAPSHOT_GIT_TIMEOUT_SECONDS = 3
# An untracked directory git reports as one entry is a nested repository. Its
# signature comes from its own fingerprint, so writes inside it are seen. The
# guard bounds recursion through repos nested inside nested repos; past it the
# fingerprint is unavailable (fail closed), never a constant marker.
_NESTED_REPO_MAX_DEPTH = 4
_NESTED_REPO_MAX_WORKERS = 8


def _snapshot_ignore_relative(path: Path) -> bool:
    parts = path.parts
    if not parts:
        return True
    if parts[0] in _SNAPSHOT_IGNORE_DIRS:
        return True
    return any(part in _SNAPSHOT_IGNORE_DIRS for part in parts)


def _porcelain_path(line: str) -> str:
    path = line[3:]
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return bytes(path[1:-1], "utf-8").decode("unicode_escape")
    return path


def _snapshot_git_args(target: Path, *git_args: str) -> list[str]:
    return ["git", "-C", str(target), *git_args]


def _snapshot_pathspec_excludes() -> list[str]:
    excludes: list[str] = []
    for name in sorted(_SNAPSHOT_IGNORE_DIRS):
        excludes.append(f":(exclude){name}")
        excludes.append(f":(exclude){name}/**")
    return excludes


def _run_snapshot_git(target: Path, *git_args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            _snapshot_git_args(target, *git_args),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_SNAPSHOT_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_worktree_lines(target: Path) -> list[str] | None:
    result = _run_snapshot_git(target, "status", "--porcelain", "--untracked-files=all", "--no-renames")
    if result is None or result.returncode != 0:
        return None
    lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
    filtered = [line for line in lines if not _snapshot_ignore_relative(Path(_porcelain_path(line)))]
    return filtered


def _git_diff_output(target: Path, *diff_args: str) -> str | None:
    result = _run_snapshot_git(
        target,
        "diff",
        *diff_args,
        "--no-renames",
        "--",
        ".",
        *_snapshot_pathspec_excludes(),
    )
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _git_diff_head(target: Path, has_head: bool | None = None) -> str | None:
    if has_head is None:
        head = _run_snapshot_git(target, "rev-parse", "--verify", "HEAD")
        if head is None:
            return None
        has_head = head.returncode == 0
    if has_head:
        return _git_diff_output(target, "HEAD")
    # Unborn branch: no HEAD to diff against. ``--cached`` compares the index
    # with the empty tree and the plain diff covers unstaged edits, so staged
    # content rewrites still change the fingerprint.
    staged = _git_diff_output(target, "--cached")
    unstaged = _git_diff_output(target)
    if staged is None or unstaged is None:
        return None
    return f"unborn-staged\n{staged}unborn-unstaged\n{unstaged}"


def _confirmed_git_worktree(target: Path) -> bool | None:
    result = _run_snapshot_git(target, "rev-parse", "--is-inside-work-tree")
    if result is None:
        return None
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "true"


def _nested_repo_signature(path: Path, depth: int) -> str | None:
    if depth >= _NESTED_REPO_MAX_DEPTH:
        return None
    # One call answers both questions: stdout is the toplevel, then the HEAD
    # oid when HEAD resolves. ``--verify -q`` exits 1 with only the toplevel
    # printed on an unborn branch; any other failure is not a usable repo.
    probe = _run_snapshot_git(path, "rev-parse", "--show-toplevel", "--verify", "-q", "HEAD")
    if probe is None or probe.returncode not in (0, 1):
        return None
    # Drop only git's line terminator: a directory name may end in spaces.
    output = probe.stdout.removesuffix("\n")
    toplevel_text, _, head_text = output.rpartition("\n") if probe.returncode == 0 else (output, "", "")
    try:
        if not toplevel_text or Path(toplevel_text).resolve() != path.resolve():
            return None
    except OSError:
        return None
    lines = _git_worktree_fingerprint_lines(path, depth=depth + 1, has_head=probe.returncode == 0)
    if lines is None:
        return None
    return f"repo:{localio.stable_hash([head_text, *sorted(lines)])}"


def _git_untracked_signature_lines(target: Path, untracked: list[Path], depth: int = 0) -> list[str] | None:
    """Sign untracked paths from ``lstat`` without reading or following them.

    A regular file signs from stat fields: a content write always moves
    ``st_ctime_ns``, which ordinary tools cannot restore, so writes are seen
    even when size and mtime are put back. A symlink signs from its link text
    and a directory (a nested repository) from its own fingerprint. Any path
    that cannot be signed makes the fingerprint unavailable.
    """
    signatures: list[str | None] = []
    nested: list[int] = []
    for relative in untracked:
        path = target / relative
        try:
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                signatures.append(f"symlink:{localio.stable_hash(os.readlink(path))}")
                continue
        except OSError:
            return None
        if stat.S_ISDIR(info.st_mode):
            nested.append(len(signatures))
            signatures.append(None)
        else:
            fields = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_mode)
            signatures.append("stat:" + ":".join(map(str, fields)))
    if nested:
        # Nested repos cost a few git subprocesses each, which release the GIL.
        with ThreadPoolExecutor(max_workers=min(_NESTED_REPO_MAX_WORKERS, len(nested))) as pool:
            results = pool.map(lambda index: _nested_repo_signature(target / untracked[index], depth), nested)
            for index, signature in zip(nested, results, strict=True):
                signatures[index] = signature
    if any(signature is None for signature in signatures):
        return None
    return [f"untracked\t{relative.as_posix()}\t{signatures[index]}" for index, relative in enumerate(untracked)]


def _git_worktree_fingerprint_lines(target: Path, *, depth: int = 0, has_head: bool | None = None) -> list[str] | None:
    status_lines = _git_worktree_lines(target)
    if status_lines is None:
        return None
    lines = [f"status\t{line}" for line in sorted(status_lines)]
    diff = _git_diff_head(target, has_head)
    if diff is None:
        return None
    lines.append(f"diff\t{localio.stable_hash(diff)}")
    untracked: list[Path] = []
    for line in status_lines:
        if not line.startswith("??"):
            continue
        relative = Path(_porcelain_path(line))
        if _snapshot_ignore_relative(relative):
            continue
        untracked.append(relative)
    untracked_lines = _git_untracked_signature_lines(target, untracked, depth)
    if untracked_lines is None:
        return None
    lines.extend(untracked_lines)
    return lines


def _directory_worktree_lines(target: Path) -> list[str] | None:
    entries: list[str] = []
    try:
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(target)
            except ValueError:
                continue
            if _snapshot_ignore_relative(relative):
                continue
            stat_result = path.stat()
            entries.append(f"{relative.as_posix()}\t{stat_result.st_mtime_ns}\t{stat_result.st_size}")
    except OSError:
        return None
    return entries


def repo_worktree_fingerprint(target: Path) -> str | None:
    git_worktree = _confirmed_git_worktree(target)
    if git_worktree is True:
        lines = _git_worktree_fingerprint_lines(target)
    elif git_worktree is False:
        lines = _directory_worktree_lines(target)
    else:
        return None
    if lines is None:
        return None
    return localio.stable_hash(sorted(lines))
