"""Identity-bound removal of a rescued Git worktree.

Removal is the last step of worktree rescue. ``git worktree remove`` and
``shutil.rmtree`` act on a path, so a rename that lands a decoy in the window
between the final rescue check and the removal makes them delete the decoy and
leave the rescued tree, or delete a directory recreated at the freed path.
Here the worktree is renamed to a private name through a held parent
descriptor, opened no-follow, its identity compared with the descriptor rescue
held, and its contents removed through that descriptor. The final ``rmdir``
only removes an empty directory, so an exchanged tree survives.

The Git registration is then removed through descriptors too: the worktree's
verified admin dir is emptied through its held descriptor and its entry is
removed from the held ``worktrees`` parent, after the backlink and the entry
identity are checked again. No path-based Git command and no repository-wide
prune runs here, so unrelated registrations are untouched.

This module is used only where descriptor-relative removal and
``/proc/self/fd`` pinning are both available. Windows keeps the pre-PR path
removal, and other platforms refuse so the worktree stays for an operator.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from . import dirfd, proc
from .worktree_lease import remove_worktree_directory

if TYPE_CHECKING:
    from .handoff_worktrees import RemovalHandle

# Stable reason codes: a refusal never removes anything and names one of these.
MOVED_REASON = "worktree-moved"
UNRENAMABLE_REASON = "worktree-unrenamable"
UNREMOVABLE_REASON = "worktree-removal-failed"
UNVERIFIED_REASON = "worktree-unverified"
LOCKED_REASON = "worktree-locked"
REGISTRATION_REASON = "worktree-registration-kept"
_PRIVATE_PREFIX = ".brigade-remove-"


def bound_removal_available() -> bool:
    """Whether removal can be bound to held descriptors on this platform."""
    return dirfd.pin_available()


def path_removal_allowed() -> bool:
    """Whether the pre-PR path removal is kept unchanged, which is Windows."""
    return os.name == "nt"


def remove_by_path(root: Path, worktree_path: Path, *, force: bool = False) -> None:
    """Remove by path, which is the pre-PR behaviour kept for Windows.

    Nothing here is bound to a held descriptor: `git worktree remove` gets the
    caller's path unresolved, and its refusal falls back to an rmtree. Other
    platforms refuse instead, so the worktree stays for an operator.
    """
    if not path_removal_allowed():
        # Without descriptor-relative removal and /proc/self/fd pinning the
        # worktree is kept instead of deleted by path.
        print(f"brigade: kept worktree {worktree_path}: {UNVERIFIED_REASON}", file=sys.stderr)
        return
    if not _deliver_handoffs_by_path(worktree_path, root):
        return
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    result = proc.run(["git", *args], cwd=root, timeout=120.0)
    if result.code != 0:
        remove_worktree_directory(root, worktree_path)


def _deliver_handoffs_by_path(worktree_path: Path, root: Path) -> bool:
    """Move pending Memory Handoffs before a path removal (pre-PR behaviour).

    Returns False when any handoff could not be delivered; the caller then
    keeps the worktree so the handoff is not deleted with it.
    """
    from . import handoff_worktrees

    try:
        # Only a main checkout that owns the same common git dir as `root`,
        # held open from validation through every write.
        held = handoff_worktrees.open_main_checkout_for(worktree_path, repo=root)
        try:
            dest, dest_fd = held if held is not None else (None, None)
            results = handoff_worktrees.rescue_pending_handoffs(worktree_path, dest, dest_fd=dest_fd)
        finally:
            if held is not None:
                os.close(held[1])
    except Exception as exc:  # noqa: BLE001 - callers run this from finally blocks
        print(f"brigade: kept worktree {worktree_path}: handoff rescue error ({type(exc).__name__})", file=sys.stderr)
        return False
    failed = [r for r in results if r.state == "failed"]
    for rescue in failed:
        print(
            f"brigade: handoff not delivered [{rescue.reason}] from {handoff_worktrees.safe_path(rescue.source)}"
            + (f" kept as {handoff_worktrees.safe_path(rescue.dest)}" if rescue.dest else ""),
            file=sys.stderr,
        )
    if failed:
        print(f"brigade: kept worktree {worktree_path} so {len(failed)} pending handoff(s) survive", file=sys.stderr)
    return not failed


def deliver_handoffs(worktree_path: Path, root: Path) -> list[RemovalHandle] | None:
    """Move pending Memory Handoffs to the main checkout before removal.

    Returns the handles rescue verified and held, or ``None`` when any handoff
    could not be delivered, or rescue could not prove the identities removal
    needs. The caller then keeps the worktree and closes any handle it gets.
    """
    from . import handoff_worktrees

    held: list[RemovalHandle] = []
    try:
        # Only a main checkout that owns the same common git dir as `root`,
        # held open from validation through every write.
        record = handoff_worktrees.open_main_checkout_for(worktree_path, repo=root)
        try:
            dest, dest_fd = record if record is not None else (None, None)
            results = handoff_worktrees.rescue_pending_handoffs(
                worktree_path,
                dest,
                dest_fd=dest_fd,
                hold_out=held,
            )
        finally:
            if record is not None:
                os.close(record[1])
    except Exception as exc:  # noqa: BLE001 - callers run this from finally blocks
        print(f"brigade: kept worktree {worktree_path}: handoff rescue error ({type(exc).__name__})", file=sys.stderr)
        _close(held)
        return None
    failed = [r for r in results if r.state == "failed"]
    for rescue in failed:
        print(
            f"brigade: handoff not delivered [{rescue.reason}] from {handoff_worktrees.safe_path(rescue.source)}"
            + (f" kept as {handoff_worktrees.safe_path(rescue.dest)}" if rescue.dest else ""),
            file=sys.stderr,
        )
    if failed:
        print(f"brigade: kept worktree {worktree_path} so {len(failed)} pending handoff(s) survive", file=sys.stderr)
        _close(held)
        return None
    if not held:
        # Removal needs the identities rescue held. Without them there is
        # nothing to compare against, so refuse rather than delete by path.
        print(f"brigade: kept worktree {worktree_path}: {UNVERIFIED_REASON}", file=sys.stderr)
        return None
    return held


def close_hold(handle: RemovalHandle) -> None:
    """Close the descriptors a :class:`RemovalHandle` owns."""
    for fd in (handle.root_fd, handle.admin_fd, handle.worktrees_fd):
        os.close(fd)


def remove_held(root: Path, hold: RemovalHandle, *, force: bool = False) -> bool:
    """Remove the worktree only while it is still the tree `hold` names.

    Every step after the rename acts on a held descriptor, so a rename that
    swaps in another tree cannot redirect a deletion. Returns False and keeps
    what it can when an identity differs or a step fails. ``force`` is needed
    to remove a locked worktree, the same as `git worktree remove`.
    """
    path = hold.path
    locked = _lock_state(root, path)
    if locked is None:
        # The lock cannot be verified, so nothing is deleted even when forced.
        _keep(path, UNVERIFIED_REASON)
        return False
    if locked and not force:
        _keep(path, LOCKED_REASON)
        return False
    try:
        parent_fd = dirfd.open_directory_chain(path.parent, writable=True)
    except OSError:
        _keep(path, UNRENAMABLE_REASON)
        return False
    private = f"{_PRIVATE_PREFIX}{uuid.uuid4().hex}"
    try:
        try:
            dirfd.rename_child(parent_fd, path.name, parent_fd, private)
        except OSError:
            _keep(path, UNRENAMABLE_REASON)
            return False
        try:
            dirfd.fsync_directory(parent_fd)
        except OSError:
            return _restore(parent_fd, private, path=path, reason=UNREMOVABLE_REASON)
        try:
            private_fd = dirfd.open_child_directory(parent_fd, private, writable=False)
        except OSError:
            return _restore(parent_fd, private, path=path, reason=UNRENAMABLE_REASON)
        try:
            try:
                same = _identity(os.fstat(private_fd)) == _identity(os.fstat(hold.root_fd))
            except OSError:
                same = False
            if not same:
                return _restore(parent_fd, private, path=path, reason=MOVED_REASON)
            try:
                dirfd.remove_directory_contents(private_fd)
            except OSError:
                return _restore(parent_fd, private, path=path, reason=UNREMOVABLE_REASON)
        finally:
            os.close(private_fd)
        try:
            current: os.stat_result | None = dirfd.stat_child(parent_fd, private)
            expected: os.stat_result | None = os.fstat(hold.root_fd)
        except OSError:
            current = expected = None
        if current is None or expected is None or _identity(current) != _identity(expected):
            _keep(path, f"{MOVED_REASON} (entry kept as {path.parent / private})")
            return False
        try:
            # Only an empty directory goes, so an exchanged tree survives.
            os.rmdir(private, dir_fd=parent_fd)
        except OSError:
            _keep(path, f"{UNREMOVABLE_REASON} (entry kept as {path.parent / private})")
            return False
        try:
            dirfd.fsync_directory(parent_fd)
        except OSError:
            pass  # the tree is gone, so only entry durability is lost
    finally:
        os.close(parent_fd)
    return _unregister(hold)


def _unregister(hold: RemovalHandle) -> bool:
    """Remove this worktree's registration through the held descriptors.

    The admin dir's `gitdir` entry is re-read through its descriptor and must
    still name this worktree's `.git` file. Its entry in the held `worktrees`
    parent must still be that descriptor. Only then are the contents removed
    and the entry dropped.
    """
    from .handoff_worktrees import admin_backlink

    path = hold.path
    if admin_backlink(hold.admin_fd) != path / ".git":
        _keep(path, REGISTRATION_REASON)
        return False
    try:
        entry = dirfd.stat_child(hold.worktrees_fd, hold.admin_name)
        if _identity(entry) != _identity(os.fstat(hold.admin_fd)):
            raise OSError("admin entry changed")
        dirfd.remove_directory_contents(hold.admin_fd)
        os.rmdir(hold.admin_name, dir_fd=hold.worktrees_fd)
    except OSError:
        _keep(path, REGISTRATION_REASON)
        return False
    return True


def _lock_state(root: Path, worktree_path: Path) -> bool | None:
    """Whether Git records this worktree as locked, or None when unknown.

    Read from `git worktree list` so ``git worktree lock`` reason text and the
    admin dir layout stay Git's business. A failed command, OSError, or
    unparseable output cannot verify the lock and returns None, which the
    caller treats as a refusal rather than as unlocked.
    """
    try:
        result = proc.run(["git", "worktree", "list", "--porcelain"], cwd=root, timeout=120.0)
    except OSError:
        return None
    if result.code != 0:
        return None
    target = _identity_key(worktree_path)
    for record in result.stdout.split("\n\n"):
        lines = record.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        if _identity_key(Path(lines[0][len("worktree ") :])) != target:
            continue
        return any(line == "locked" or line.startswith("locked ") for line in lines[1:])
    return None


def _identity_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _close(handles: list[RemovalHandle]) -> None:
    for handle in handles:
        close_hold(handle)


def _restore(parent_fd: int, private: str, *, path: Path, reason: str) -> bool:
    """Put a refused entry back under the public name, never replacing one.

    Returns False, the removal outcome, and always names where the entry is
    when it could not go back.
    """
    try:
        dirfd.rename_child_noreplace(parent_fd, private, parent_fd, path.name)
    except OSError:
        _keep(path, f"{reason} (entry kept as {path.parent / private})")
        return False
    _keep(path, reason)
    return False


def _keep(path: Path, reason: str) -> None:
    print(f"brigade: kept worktree {path}: {reason}", file=sys.stderr)
