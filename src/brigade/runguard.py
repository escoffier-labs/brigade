"""Git worktree guardrails for `brigade run`."""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import proc


class RunGuardError(RuntimeError):
    """Base error for run guard failures."""


class DirtyWorktreeError(RunGuardError):
    def __init__(self, paths: list[str]):
        self.paths = paths
        shown = ", ".join(paths[:8])
        if len(paths) > 8:
            shown += f", ... ({len(paths)} total)"
        super().__init__(
            f"dirty worktree: {shown}. Commit, stash, clean the tree, or pass --allow-dirty to run anyway."
        )


class RunLockError(RunGuardError):
    pass


@dataclass(frozen=True)
class PatchSummary:
    path: Path
    changed: bool
    tracked_count: int
    untracked_count: int


def _git(cwd: Path, *args: str, timeout: float = 30.0) -> proc.Result:
    return proc.run(["git", *args], cwd=cwd, timeout=timeout)


def is_git_worktree(cwd: Path) -> bool:
    result = _git(cwd, "rev-parse", "--is-inside-work-tree")
    return result.code == 0 and result.stdout.strip() == "true"


def git_root(cwd: Path) -> Path:
    result = _git(cwd, "rev-parse", "--show-toplevel")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RunGuardError(f"not a git worktree: {cwd}{suffix}")
    return Path(result.stdout.strip()).resolve()


def dirty_paths(cwd: Path) -> list[str]:
    result = _git(cwd, "status", "--porcelain=v1")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise RunGuardError(f"not a git worktree: {cwd}{suffix}")
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return sorted(paths)


def require_clean_worktree(cwd: Path) -> list[str]:
    # Brigade's own state (run lock, run artifacts) is not user work in progress.
    paths = [path for path in dirty_paths(cwd) if not path.startswith(".brigade/")]
    if paths:
        raise DirtyWorktreeError(paths)
    return paths


def lock_path(cwd: Path) -> Path:
    base = git_root(cwd) if is_git_worktree(cwd) else cwd.resolve()
    return base / ".brigade" / "run.lock"


def _lock_is_stale(path: Path) -> bool:
    try:
        pid = int((path / "pid").read_text().strip())
    except (FileNotFoundError, ValueError):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _acquire_lock(path: Path) -> None:
    for _ in range(2):
        try:
            path.mkdir()
        except FileExistsError:
            if _lock_is_stale(path):
                shutil.rmtree(path, ignore_errors=True)
                continue
            raise RunLockError(
                f"another brigade run appears active: {path}. Remove the lock only if no run is active."
            ) from None
        (path / "pid").write_text(f"{os.getpid()}\n")
        return
    raise RunLockError(f"could not acquire run lock: {path}")


@contextlib.contextmanager
def run_lock(cwd: Path, *, wait_seconds: float = 0.0, poll_interval: float = 0.1):
    if wait_seconds < 0:
        raise ValueError("run lock wait_seconds must be non-negative")
    if poll_interval <= 0:
        raise ValueError("run lock poll_interval must be positive")
    path = lock_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            _acquire_lock(path)
            break
        except RunLockError as exc:
            if wait_seconds == 0:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunLockError(f"timed out after {wait_seconds:g}s waiting for run lock: {path}") from exc
            time.sleep(min(poll_interval, remaining))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def create_detached_worktree(repo: Path, worktree_path: Path) -> Path:
    root = git_root(repo)
    worktree_path = worktree_path.expanduser().resolve()
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = _git(root, "worktree", "add", "--detach", str(worktree_path), "HEAD", timeout=120.0)
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to create detached worktree: {detail}")
    return worktree_path


def remove_worktree(repo: Path, worktree_path: Path) -> None:
    if not worktree_path.exists():
        return
    root = git_root(repo)
    result = _git(root, "worktree", "remove", "--force", str(worktree_path), timeout=120.0)
    if result.code != 0:
        shutil.rmtree(worktree_path, ignore_errors=True)


def _tracked_diff(cwd: Path) -> tuple[str, int]:
    result = _git(cwd, "diff", "--binary", "HEAD")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to collect tracked diff: {detail}")
    count = 0
    names = _git(cwd, "diff", "--name-only", "HEAD")
    if names.code == 0:
        count = len([line for line in names.stdout.splitlines() if line.strip()])
    return result.stdout, count


def _untracked_files(cwd: Path) -> list[str]:
    result = _git(cwd, "ls-files", "--others", "--exclude-standard")
    if result.code != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to list untracked files: {detail}")
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def _untracked_diff(cwd: Path, relpath: str) -> str:
    result = proc.run(["git", "diff", "--no-index", "--binary", "--", "/dev/null", relpath], cwd=cwd)
    if result.code not in {0, 1}:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RunGuardError(f"failed to collect untracked diff for {relpath}: {detail}")
    return result.stdout


def collect_changes_patch(cwd: Path, patch_path: Path) -> PatchSummary:
    patch_path = patch_path.expanduser().resolve()
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    tracked_patch, tracked_count = _tracked_diff(cwd)
    untracked = _untracked_files(cwd)
    pieces = [tracked_patch] if tracked_patch else []
    pieces.extend(_untracked_diff(cwd, path) for path in untracked)
    # Diff bytes are newline-sensitive: a hunk whose last line is a blank
    # context line ends in " \n", and trimming it corrupts the patch
    # (issue #124). Keep each piece verbatim, only guaranteeing the single
    # trailing newline git already emits.
    content = "".join(piece if piece.endswith("\n") else piece + "\n" for piece in pieces if piece)
    patch_path.write_text(content)
    return PatchSummary(
        path=patch_path,
        changed=bool(content),
        tracked_count=tracked_count,
        untracked_count=len(untracked),
    )


def verify_changes_patch(cwd: Path, patch_path: Path) -> bool:
    """Check the written patch is one git can actually apply.

    The worktree still holds the changes the patch describes, so a valid
    patch must reverse-apply cleanly against it. An empty patch is valid.
    """
    patch_path = patch_path.expanduser().resolve()
    try:
        if patch_path.stat().st_size == 0:
            return True
    except FileNotFoundError:
        return False
    result = proc.run(["git", "apply", "--check", "--reverse", str(patch_path)], cwd=cwd)
    return result.code == 0
