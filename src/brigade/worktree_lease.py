"""Lease paths and setup cleanup for automatically created Git worktrees."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path

from . import proc


_EVENT_PARENT: ContextVar[tuple[Path, Path] | None] = ContextVar("worktree_event_parent", default=None)


@contextmanager
def attribute_worktree_events(checkout: Path, parent: Path) -> Iterator[None]:
    """Describe an automatic run's parent even before or after Git checkout."""
    token = _EVENT_PARENT.set((checkout.resolve(), parent))
    try:
        yield
    finally:
        _EVENT_PARENT.reset(token)


def worktree_event_parent(workspace: Path) -> Path | None:
    attribution = _EVENT_PARENT.get()
    if attribution is not None and workspace == attribution[0]:
        return attribution[1]
    if reserved_worktree_lock_dir(workspace) is None:
        return None
    from .handoff_worktrees import main_checkout

    return main_checkout(workspace)


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
        return worktree_lock_dir(checkout)
    return None


@contextmanager
def run_lock_parent(checkout: Path, lock: Path) -> Iterator[None]:
    """Create only lease storage when operating on a removed checkout."""
    parent = lock.parent
    temporary = not checkout.exists() and reserved_worktree_lock_dir(checkout.resolve()) == parent
    try:
        parent.mkdir(parents=True, exist_ok=not temporary)
    except FileExistsError:
        if not temporary or not parent.is_dir():
            raise
        temporary = False
    try:
        yield
    finally:
        if temporary:
            with suppress(OSError):
                parent.rmdir()


def workspace_for_lock_path(path: Path) -> Path:
    """Map an ordinary or external lease lock back to its workspace."""
    parent = path.parent.parent.resolve()
    if parent == brigade_worktree_root().resolve() / ".run-locks":
        return parent.parent / path.parent.name
    return parent


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
        remove_worktree_directory(repo, worktree_path)


def remove_worktree_directory(repo: Path, worktree_path: Path) -> None:
    """Remove the directory after Git removal failed and clear its registration."""
    if worktree_path.exists():
        shutil.rmtree(worktree_path)
    result = proc.run(["git", "worktree", "prune", "--expire", "now"], cwd=repo, timeout=120.0)
    if result.code != 0:
        raise OSError(f"failed to prune Git worktree registration: {result.stderr.strip()}")

