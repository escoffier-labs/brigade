"""Read-only worktree fingerprint for the Claude Code work-loop hook.

The fingerprint detects whether a Bash call changed the repository. It reads
``git status``, ``git diff HEAD``, and content hashes of untracked paths; it
never writes objects (``hash-object`` runs without ``-w``).
"""

from __future__ import annotations

import os
import stat
import subprocess
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


def _run_snapshot_git(
    target: Path,
    *git_args: str,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            _snapshot_git_args(target, *git_args),
            check=False,
            input=stdin_text,
            stdin=subprocess.DEVNULL if stdin_text is None else None,
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


def _git_diff_head(target: Path) -> str | None:
    head = _run_snapshot_git(target, "rev-parse", "--verify", "HEAD")
    if head is None:
        return None
    if head.returncode == 0:
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


def _git_untracked_content_signature(target: Path, relative: Path) -> str | None:
    result = _run_snapshot_git(target, "hash-object", "--", relative.as_posix())
    if result is None or result.returncode != 0:
        return None
    digest = result.stdout.strip()
    return digest or None


def _git_untracked_batch_signatures(target: Path, relatives: list[Path]) -> tuple[bool, list[str] | None]:
    """Hash regular files in one ``hash-object --stdin-paths`` call.

    Returns ``(True, digests)`` with digests in input order. Returns
    ``(True, None)`` when git answered but the batch failed or its output count
    does not match; the caller then falls back to one call per file. Returns
    ``(False, None)`` when git could not run or timed out: a per-file fallback
    over the same set would only be slower, so the fingerprint is unavailable.
    """
    if not relatives:
        return True, []
    stdin_text = "".join(f"{relative.as_posix()}\n" for relative in relatives)
    result = _run_snapshot_git(target, "hash-object", "--stdin-paths", stdin_text=stdin_text)
    if result is None:
        return False, None
    if result.returncode != 0:
        return True, None
    digests = [line.strip() for line in result.stdout.splitlines()]
    if len(digests) != len(relatives) or not all(digests):
        return True, None
    return True, digests


def _needs_single_hash(relative: Path) -> bool:
    """True when ``hash-object --stdin-paths`` cannot carry the path verbatim.

    Git reads one path per line, strips a trailing CR, and C-unquotes any line
    that starts with a double quote (``hash_stdin_paths`` in hash-object.c), so
    such a path would name a different file.
    """
    text = relative.as_posix()
    return "\n" in text or "\r" in text or text.startswith('"')


def _nested_repo_signature(path: Path, depth: int) -> str | None:
    if depth >= _NESTED_REPO_MAX_DEPTH:
        return None
    toplevel = _run_snapshot_git(path, "rev-parse", "--show-toplevel")
    if toplevel is None or toplevel.returncode != 0:
        return None
    try:
        # Drop only git's line terminator: a directory name may end in spaces.
        toplevel_text = toplevel.stdout.removesuffix("\n")
        if Path(toplevel_text).resolve() != path.resolve():
            return None
    except OSError:
        return None
    head = _run_snapshot_git(path, "rev-parse", "--verify", "HEAD")
    if head is None:
        return None
    head_text = head.stdout.strip() if head.returncode == 0 else ""
    lines = _git_worktree_fingerprint_lines(path, depth=depth + 1)
    if lines is None:
        return None
    return f"repo:{localio.stable_hash([head_text, *sorted(lines)])}"


def _untracked_special_signature(target: Path, relative: Path, depth: int) -> tuple[bool, str | None]:
    """Sign symlinks and nested repositories without following symlinks.

    Returns ``(False, None)`` for a path to content-hash as a regular file,
    ``(True, signature)`` for a signed special path, and ``(True, None)`` when
    a special path cannot be signed (the fingerprint is then unavailable).
    """
    path = target / relative
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False, None
    if stat.S_ISLNK(mode):
        try:
            link_text = os.readlink(path)
        except OSError:
            return True, None
        return True, f"symlink:{localio.stable_hash(link_text)}"
    if stat.S_ISDIR(mode):
        return True, _nested_repo_signature(path, depth)
    return False, None


def _git_untracked_signature_lines(target: Path, untracked: list[Path], depth: int = 0) -> list[str] | None:
    signatures: dict[Path, str] = {}
    batchable: list[Path] = []
    single: list[Path] = []
    for relative in untracked:
        special, signature = _untracked_special_signature(target, relative, depth)
        if special:
            if signature is None:
                return None
            signatures[relative] = signature
        elif _needs_single_hash(relative):
            single.append(relative)
        else:
            batchable.append(relative)
    usable, digests = _git_untracked_batch_signatures(target, batchable)
    if not usable:
        return None
    if digests is None:
        single = batchable + single
    else:
        signatures.update(zip(batchable, digests, strict=True))
    for relative in single:
        content_signature = _git_untracked_content_signature(target, relative)
        if content_signature is None:
            return None
        signatures[relative] = content_signature
    return [f"untracked\t{relative.as_posix()}\t{signatures[relative]}" for relative in untracked]


def _git_worktree_fingerprint_lines(target: Path, *, depth: int = 0) -> list[str] | None:
    status_lines = _git_worktree_lines(target)
    if status_lines is None:
        return None
    lines = [f"status\t{line}" for line in sorted(status_lines)]
    diff = _git_diff_head(target)
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
