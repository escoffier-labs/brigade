"""Identity-bound removal of a rescued Git worktree.

Removal is the last step of worktree rescue. ``git worktree remove`` and its
``shutil.rmtree`` fallback act on a path, so a rename that lands a decoy
between rescue's final check and the removal makes them delete the decoy and
leave the rescued tree behind. Here the worktree is renamed to a private name
through a held parent directory descriptor, the renamed entry's identity is
compared with the descriptor rescue held, and only a match is removed. A
mismatch restores the name, keeps everything and reports a fixed reason.

Windows has no descriptor-relative tree removal, so it keeps the path-based
behaviour, as do platforms where ``shutil.rmtree`` cannot avoid following
links.
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from pathlib import Path

from . import dirfd, proc
from .worktree_lease import remove_worktree_directory

# Stable reason codes: a refusal never removes anything and names one of these.
MOVED_REASON = "worktree-moved"
UNRENAMABLE_REASON = "worktree-unrenamable"
UNREMOVABLE_REASON = "worktree-removal-failed"
UNVERIFIED_REASON = "worktree-unverified"
LOCKED_REASON = "worktree-locked"
_PRIVATE_PREFIX = ".brigade-remove-"


def bound_removal_available() -> bool:
    """Whether removal can be bound to a held descriptor on this platform."""
    return dirfd.posix_available() and dirfd.tree_removal_available()


def deliver_handoffs(worktree_path: Path, root: Path) -> list[int] | None:
    """Move pending Memory Handoffs to the main checkout before removal.

    Returns the held worktree descriptors rescue verified, or ``None`` when
    any handoff could not be delivered, or rescue did not prove the path still
    names the tree it rescued. The caller then keeps the worktree so the
    handoff is not deleted with it.
    """
    from . import handoff_worktrees

    held: list[int] = []
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
    if not held and bound_removal_available():
        # Removal on POSIX needs the identity rescue held. Without it there
        # is nothing to compare against, so refuse rather than fall back to
        # a path-based delete.
        print(f"brigade: kept worktree {worktree_path}: {UNVERIFIED_REASON}", file=sys.stderr)
        return None
    return held


def remove_held(root: Path, worktree_path: Path, held: list[int], *, force: bool = False) -> bool:
    """Remove the worktree only while it is still the tree `held` names.

    The tree is renamed to a private name through a held parent descriptor
    first, so the removal acts on the object the descriptor proves rather than
    on whatever the public path holds at that instant. Returns False, after
    restoring the public name, when the identity differs or the removal fails,
    and prints the fixed reason. A locked worktree is refused unless ``force``
    was requested, the same as `git worktree remove` without ``--force``.
    ``force`` also reaches the path-based fallback.
    """
    candidate = worktree_path.expanduser()
    # Resolve ancestors but never the final component: a swap that leaves a
    # symlink at the path must be refused, not followed to its target.
    try:
        parent_path, name = candidate.parent.resolve(), candidate.name
    except OSError:
        _keep(worktree_path, UNRENAMABLE_REASON)
        return False
    if len(held) != 1 or not bound_removal_available():
        # Windows, and any platform whose rmtree cannot avoid links, keeps the
        # path-based order: `git worktree remove`, then the rmtree fallback.
        _remove_by_path(root, worktree_path.expanduser().resolve(), force=force)
        return True
    if not force and _locked(root, worktree_path):
        _keep(worktree_path, LOCKED_REASON)
        return False
    try:
        parent_fd = dirfd.open_directory_chain(parent_path, writable=True)
    except OSError:
        _keep(worktree_path, UNRENAMABLE_REASON)
        return False
    private = f"{_PRIVATE_PREFIX}{uuid.uuid4().hex}"
    try:
        try:
            dirfd.rename_child(parent_fd, name, parent_fd, private)
        except OSError:
            _keep(worktree_path, UNRENAMABLE_REASON)
            return False
        try:
            # Durability first, so nothing slow runs between the identity
            # check and the removal.
            dirfd.fsync_directory(parent_fd)
        except OSError:
            _restore(parent_fd, private, name)
            _keep(worktree_path, UNREMOVABLE_REASON)
            return False
        try:
            moved, verified = dirfd.stat_child(parent_fd, private), os.fstat(held[0])
        except OSError:
            moved, verified = None, None
        if moved is None or verified is None or _identity(moved) != _identity(verified):
            _restore(parent_fd, private, name)
            _keep(worktree_path, MOVED_REASON)
            return False
        try:
            dirfd.remove_tree_child(parent_fd, private)
        except OSError:
            _restore(parent_fd, private, name)
            _keep(worktree_path, UNREMOVABLE_REASON)
            return False
        try:
            dirfd.fsync_directory(parent_fd)
        except OSError:
            pass  # the tree is gone, so only entry durability is lost
    finally:
        os.close(parent_fd)
    _unregister(root, parent_path / name, force=force)
    return True


def _locked(root: Path, worktree_path: Path) -> bool:
    """Whether Git records this worktree as locked.

    Read from `git worktree list` so ``git worktree lock`` reason text and the
    admin dir layout stay Git's business. An unregistered path is not locked,
    because there is no lock to respect.
    """
    result = proc.run(["git", "worktree", "list", "--porcelain"], cwd=root, timeout=120.0)
    if result.code != 0:
        return False
    target = _identity_key(worktree_path)
    for record in result.stdout.split("\n\n"):
        lines = record.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        if _identity_key(Path(lines[0][len("worktree ") :])) != target:
            continue
        return any(line == "locked" or line.startswith("locked ") for line in lines[1:])
    return False


def _identity_key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _close(fds: list[int]) -> None:
    for fd in fds:
        os.close(fd)


def _restore(parent_fd: int, private: str, name: str) -> None:
    """Put a refused decoy back under the public name, never replacing an entry.

    A rename that would replace a new entry at the public name is refused, so
    the decoy stays under the private name for an operator.
    """
    try:
        dirfd.rename_child_noreplace(parent_fd, private, parent_fd, name)
    except OSError:
        pass


def _keep(path: Path, reason: str) -> None:
    print(f"brigade: kept worktree {path}: {reason}", file=sys.stderr)


def _remove_by_path(root: Path, worktree_path: Path, *, force: bool) -> None:
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    result = proc.run(["git", *args], cwd=root, timeout=120.0)
    if result.code != 0:
        remove_worktree_directory(root, worktree_path)


def _unregister(root: Path, worktree_path: Path, *, force: bool) -> None:
    """Remove this one worktree's registration after its directory is gone.

    `git worktree remove` on the vacated path drops exactly this entry. A
    global `git worktree prune` would also drop unrelated entries whose
    directory is merely absent, such as one on an unmounted drive, so it is
    never run here. If Git still refuses, only this worktree's admin dir is
    removed, and only after its `gitdir` entry names this very `.git` file.
    """
    args = ["worktree", "remove", "--force"] + (["--force"] if force else [])
    result = proc.run(["git", *args, str(worktree_path)], cwd=root, timeout=120.0)
    if result.code == 0:
        return
    admin = _own_admin_dir(root, worktree_path)
    if admin is None:
        print(
            f"brigade: worktree registration not cleared for {worktree_path}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return
    shutil.rmtree(admin, ignore_errors=True)


def _own_admin_dir(root: Path, worktree_path: Path) -> Path | None:
    """This worktree's admin dir, only when its backlink names this `.git`."""
    result = proc.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=root,
        timeout=120.0,
    )
    if result.code != 0 or not result.stdout.strip():
        return None
    admin = Path(result.stdout.strip()).resolve() / "worktrees" / worktree_path.name
    try:
        backlink = Path((admin / "gitdir").read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return None
    if backlink != worktree_path / ".git":
        return None
    return admin
