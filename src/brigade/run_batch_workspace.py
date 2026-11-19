"""Reserve, create and validate one per-task worktree for an explicit batch.

This module is workspace plumbing only. It never launches a worker, appends a
journal fact, or removes a directory, and the batch executor does not call it
yet (issue #2050, slice S4a).

A per-task worktree promises separation between cooperative workers. It is not
a security boundary: a worker with full filesystem access can still reach the
caller checkout by absolute path or through the shared Git common directory.
What is strict here is path handling and reuse validation, because a reused or
misidentified worktree would silently collapse that separation:

* the path is always recomputed from ``(repo_root, run_id, task_id)`` and never
  read back from JSON;
* the path must be a direct child of the canonical worktree cache root, outside
  the caller repository and every registered worktree;
* a reservation is exclusive, so a pre-existing directory or lease refuses
  instead of being adopted;
* validation compares the created directory's device/inode, resolved path, Git
  toplevel and common directory, branch, HEAD and (optionally) pristine status
  against what was reserved.

Every refusal is a :class:`WorkspaceError` carrying one closed code and a short
reason token. No refusal deletes or repairs anything.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import proc, runguard, worktree_lease

# Closed refusal codes. Callers branch on these, never on the reason text.
WORKSPACE_OVERLAP = "workspace-overlap"
WORKSPACE_EXISTS = "workspace-exists"
WORKSPACE_INVALID = "workspace-invalid"
WORKSPACE_UNAVAILABLE = "workspace-unavailable"
WORKSPACE_ERROR_CODES = frozenset({WORKSPACE_OVERLAP, WORKSPACE_EXISTS, WORKSPACE_INVALID, WORKSPACE_UNAVAILABLE})

BRANCH_PREFIX = "brigade/batch/"
SLUG_LIMIT = 40
DIGEST_SUFFIX_LIMIT = 12

_BASE_SHA_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)
_SLUG_CHARS = re.compile(r"[^a-z0-9._-]+", re.ASCII)

_GIT_TIMEOUT = 30.0
_GIT_WORKTREE_TIMEOUT = 120.0


class WorkspaceError(RuntimeError):
    """One bounded workspace refusal; nothing was launched and nothing deleted."""

    def __init__(self, code: str, reason: str):
        if code not in WORKSPACE_ERROR_CODES:
            raise ValueError(f"unknown workspace error code {code!r}")
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


@dataclass(frozen=True)
class Reservation:
    """The exclusive claim on one checkout directory, identified by lstat."""

    path: Path
    dev: int
    ino: int


@dataclass(frozen=True)
class Binding:
    """What one task's worktree is, as reserved and validated."""

    task_id: str
    run_id: str
    base_sha: str
    branch: str
    path: Path
    dev: int
    ino: int


def cache_root() -> Path:
    """Canonical per-user worktree cache root, the only allowed parent."""
    return worktree_lease.brigade_worktree_root().expanduser().resolve()


def slug(value: str) -> str:
    """A bounded, portable path component that can never be ``.`` or ``..``."""
    if not isinstance(value, str):
        return "task"
    candidate = _SLUG_CHARS.sub("-", value.strip().lower()).strip("-.")
    return candidate[:SLUG_LIMIT].strip("-.") or "task"


def task_digest(run_id: str, task_id: str) -> str:
    """The 12-hex suffix that separates equal slugs across runs and tasks."""
    material = f"{run_id}\0{task_id}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(material).hexdigest()[:DIGEST_SUFFIX_LIMIT]


def expected_path(repo_root: Path, run_id: str, task_id: str) -> Path:
    """Recompute the only path this task's worktree may occupy.

    Recomputed on every use from the caller's repository, run identity and task
    identity. A path recorded in JSON is never authoritative.
    """
    repo = Path(repo_root).expanduser().resolve()
    name = f"{slug(repo.name)}-batch-{slug(task_id)}-{task_digest(run_id, task_id)}"
    return cache_root() / name


def branch_name(run_id: str, task_id: str) -> str:
    """The only branch a task worktree may have checked out."""
    return f"{BRANCH_PREFIX}{slug(task_id)}-{task_digest(run_id, task_id)}"


def _contains(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def registered_worktree_roots(repo_root: Path) -> list[Path]:
    """Every worktree root Git has registered for the caller's repository."""
    repo = Path(repo_root).expanduser().resolve()
    try:
        result = proc.run(["git", "worktree", "list", "--porcelain"], cwd=repo, timeout=_GIT_TIMEOUT)
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git-worktree-list") from exc
    if result.code != 0:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git-worktree-list")
    roots: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            roots.append(Path(line[len("worktree ") :]).expanduser().resolve())
    return roots


def check_disjoint(candidate: Path, repo_root: Path, other_paths: Iterable[Path] = ()) -> None:
    """Refuse a candidate that is not an isolated direct child of the cache root.

    Refuses when the candidate equals or sits below the caller repository, a
    registered worktree root, or another task's path, when the caller
    repository sits below the candidate, and when the candidate's parent is not
    exactly the canonical cache root.
    """
    target = Path(candidate).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    root = cache_root()
    if target.parent != root:
        raise WorkspaceError(WORKSPACE_OVERLAP, "outside-cache-root")
    if _contains(repo, target):
        raise WorkspaceError(WORKSPACE_OVERLAP, "repo-root")
    if _contains(target, repo):
        raise WorkspaceError(WORKSPACE_OVERLAP, "repo-root")
    for worktree in registered_worktree_roots(repo):
        if _contains(worktree, target):
            raise WorkspaceError(WORKSPACE_OVERLAP, "registered-worktree")
    for other in other_paths:
        if _contains(Path(other).expanduser().resolve(), target):
            raise WorkspaceError(WORKSPACE_OVERLAP, "other-task")


def reserve(path: Path) -> Reservation:
    """Claim one checkout directory exclusively, without adopting an existing one.

    Copies the run worktree reservation order: the lease directory first, then
    the checkout directory, each created without ``exist_ok``. A pre-existing
    path or lease refuses with ``workspace-exists`` and nothing is deleted or
    changed.
    """
    target = Path(path).expanduser().resolve()
    try:
        runguard.worktree_lock_dir(target).mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise WorkspaceError(WORKSPACE_EXISTS, "reservation") from None
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "reservation") from exc
    try:
        target.mkdir()
    except FileExistsError:
        raise WorkspaceError(WORKSPACE_EXISTS, "path") from None
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "mkdir") from exc
    try:
        info = target.lstat()
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "lstat") from exc
    return Reservation(path=target, dev=info.st_dev, ino=info.st_ino)


def create(
    repo_root: Path,
    base_sha: str,
    reservation: Reservation,
    branch: str,
    *,
    task_id: str,
    run_id: str,
) -> Binding:
    """Create the reserved worktree at the pinned base, then validate it.

    ``task_id`` and ``run_id`` are keyword-only because the binding has to name
    the task and recompute its path; they are the same identities that produced
    the reserved path. Any Git failure raises ``workspace-unavailable`` and
    deletes nothing, so the caller can inspect the reservation.
    """
    repo = Path(repo_root).expanduser().resolve()
    if not isinstance(base_sha, str) or _BASE_SHA_RE.fullmatch(base_sha) is None:
        raise WorkspaceError(WORKSPACE_INVALID, "base-sha")
    if not isinstance(task_id, str) or not task_id:
        raise WorkspaceError(WORKSPACE_INVALID, "task-id")
    if not isinstance(run_id, str) or not run_id:
        raise WorkspaceError(WORKSPACE_INVALID, "run-id")
    if not isinstance(reservation, Reservation):
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "reservation")
    binding = Binding(
        task_id=task_id,
        run_id=run_id,
        base_sha=base_sha,
        branch=branch,
        path=reservation.path,
        dev=reservation.dev,
        ino=reservation.ino,
    )
    if binding.path != expected_path(repo, run_id, task_id):
        raise WorkspaceError(WORKSPACE_OVERLAP, "unexpected-path")
    if binding.branch != branch_name(run_id, task_id):
        raise WorkspaceError(WORKSPACE_INVALID, "branch")
    check_disjoint(binding.path, repo)
    try:
        result = proc.run(
            ["git", "worktree", "add", "-b", binding.branch, str(binding.path), base_sha],
            cwd=repo,
            timeout=_GIT_WORKTREE_TIMEOUT,
        )
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git-worktree-add") from exc
    if result.code != 0:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git-worktree-add")
    validate(binding, repo)
    return binding


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError:
        raise WorkspaceError(WORKSPACE_INVALID, "missing") from None
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "lstat") from exc


def _git_line(checkout: Path, *args: str) -> str:
    try:
        result = proc.run(["git", *args], cwd=checkout, timeout=_GIT_TIMEOUT)
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git") from exc
    if result.code != 0:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git")
    return result.stdout.strip()


def _git_state(checkout: Path, *args: str) -> str | None:
    """One observed checkout state, or ``None`` when Git cannot answer.

    A clean Git refusal (a detached ``HEAD``, an unborn branch) is a validation
    mismatch, not an unavailable workspace, so callers compare ``None`` and
    refuse with their own reason token.
    """
    try:
        result = proc.run(["git", *args], cwd=checkout, timeout=_GIT_TIMEOUT)
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git") from exc
    if result.code != 0:
        return None
    return result.stdout.strip()


def _common_dir(checkout: Path) -> Path:
    reported = Path(_git_line(checkout, "rev-parse", "--git-common-dir"))
    if not reported.is_absolute():
        reported = checkout / reported
    try:
        return reported.resolve()
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "git-common-dir") from exc


def validate(binding: Binding, repo_root: Path, *, pristine: bool = True) -> None:
    """Refuse a worktree that is not exactly the reserved, isolated checkout.

    Checks, in order: a real directory (never a symlink), the reserved
    device/inode, the recomputed expected path, the Git toplevel equal to the
    path itself, the same Git common directory as the caller repository, the
    bound branch, ``HEAD`` equal to the pinned base, and — when ``pristine`` —
    no tracked, untracked, or ignored files. The first mismatch raises
    ``workspace-invalid`` with a reason token and deletes nothing.
    """
    repo = Path(repo_root).expanduser().resolve()
    if not isinstance(binding, Binding):
        raise WorkspaceError(WORKSPACE_INVALID, "binding")
    info = _lstat(binding.path)
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceError(WORKSPACE_INVALID, "symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError(WORKSPACE_INVALID, "not-a-directory")
    if (info.st_dev, info.st_ino) != (binding.dev, binding.ino):
        raise WorkspaceError(WORKSPACE_INVALID, "identity-changed")
    try:
        resolved = binding.path.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(WORKSPACE_UNAVAILABLE, "resolve") from exc
    if resolved != expected_path(repo, binding.run_id, binding.task_id):
        raise WorkspaceError(WORKSPACE_INVALID, "unexpected-path")
    reported_toplevel = Path(_git_line(binding.path, "rev-parse", "--show-toplevel"))
    if not reported_toplevel.is_absolute():
        reported_toplevel = binding.path / reported_toplevel
    if reported_toplevel.resolve() != binding.path:
        raise WorkspaceError(WORKSPACE_INVALID, "foreign-toplevel")
    if _common_dir(binding.path) != _common_dir(repo):
        raise WorkspaceError(WORKSPACE_INVALID, "foreign-common-dir")
    if _git_state(binding.path, "symbolic-ref", "--short", "HEAD") != binding.branch:
        raise WorkspaceError(WORKSPACE_INVALID, "branch")
    if _git_state(binding.path, "rev-parse", "HEAD") != binding.base_sha:
        raise WorkspaceError(WORKSPACE_INVALID, "head")
    status = _git_state(binding.path, "status", "--porcelain", "--ignored")
    if pristine and status:
        raise WorkspaceError(WORKSPACE_INVALID, "dirty")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def binding_fact(binding: Binding) -> dict[str, str]:
    """The digest-only journal payload for one validated binding.

    The path, branch and device/inode never enter the journal as text; only
    their SHA-256 digests and the already-public 40-hex base revision do.
    """
    return {
        "task_id": binding.task_id,
        "base_sha": binding.base_sha,
        "branch_digest": _digest(binding.branch),
        "path_digest": _digest(str(binding.path)),
        "dev_ino_digest": _digest(f"{binding.dev}:{binding.ino}"),
    }
