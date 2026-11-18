"""Lease paths and setup cleanup for automatically created Git worktrees."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

from . import proc


def brigade_worktree_root() -> Path:
    return Path.home() / ".cache" / "brigade" / "worktrees"


def worktree_lock_dir(checkout: Path) -> Path:
    """Reservation and lease storage for an automatically created checkout.

    Keep the lease out of Git's patch and preserve it through checkout removal
    so the final removal receipt can still be checkpointed under ownership.
    """
    return checkout.parent / ".run-locks" / checkout.name


def reserved_worktree_lock_dir(checkout: Path) -> Path | None:
    if checkout.parent == brigade_worktree_root().resolve():
        reservation = worktree_lock_dir(checkout)
        if reservation.is_dir():
            return reservation
    return None


def workspace_identity_ancestors(start: Path) -> Iterator[Path]:
    """Stop before an automatic checkout whose claim is Git-derived."""
    current = Path(start).expanduser()
    try:
        current = current.resolve()
    except OSError:
        pass
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if reserved_worktree_lock_dir(candidate) is not None:
            break
        yield candidate


def discard_incomplete_worktree(repo: Path, worktree_path: Path) -> None:
    """Discard a reserved checkout that failed setup before any worker ran."""
    result = proc.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo, timeout=120.0)
    if result.code != 0:
        # Git cannot remove the reserved directory if add failed before it
        # registered the checkout. Only the caller that reserved it can clean it.
        shutil.rmtree(worktree_path)
