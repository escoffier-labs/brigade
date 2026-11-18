"""Quarantine-then-verify for the mutations a local-edit check protects.

A drift check and the removal it guards are separate pathname operations, so a
hand edit or a swapped directory can land between them. This module closes that
window: the destination is first renamed to a fresh unpredictable name in the
same parent, through a held parent descriptor, and only then compared with what
the check saw. A writer that changes the destination after the rename can only
change the quarantined object, which the comparison catches. On a mismatch the
object is renamed back (never over a new entry) and the operation aborts.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat as stat_module
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import dirfd
from . import registry


@dataclass(frozen=True)
class TreeIdentity:
    """What a drift check saw of an installed copy: its directory inode and a digest of every byte."""

    device: int
    inode: int
    digest: str


def tree_digest(dirs: list[tuple[str, ...]], files: dict[tuple[str, ...], bytes]) -> str:
    digest = hashlib.sha256()
    for parts in sorted(dirs):
        digest.update(b"d\0" + "/".join(parts).encode("utf-8") + b"\0")
    for parts in sorted(files):
        digest.update(b"f\0" + "/".join(parts).encode("utf-8") + b"\0")
        digest.update(files[parts] + b"\0")
    return digest.hexdigest()


def _changed_message(dest: Path) -> str:
    return f"installed copy changed after it was checked: {dest}; left untouched, run again"


@dataclass
class TreeQuarantine:
    """One installed copy moved aside and verified against the bytes the drift check saw."""

    parent_fd: int
    closers: list[int]
    leaf: str
    name: str
    dest: Path

    def close(self) -> None:
        for fd in reversed(self.closers):
            with contextlib.suppress(OSError):
                os.close(fd)
        self.closers = []

    def _destination_present(self) -> bool:
        try:
            dirfd.stat_child(self.parent_fd, self.leaf, allow_reparse=True)
        except FileNotFoundError:
            return False
        return True

    def restore(self) -> None:
        """Put the quarantined copy back, never over an entry that appeared since."""
        if self._destination_present():
            raise registry.SkillsStatePathError(
                f"installed copy was moved aside as {self.name} and a new entry now holds {self.dest}; "
                "the moved copy was kept for inspection"
            )
        dirfd.rename_child(self.parent_fd, self.name, self.parent_fd, self.leaf)

    def clear_destination(self) -> None:
        """Drop whatever a failed publish left at the destination, relative to the held parent."""
        try:
            info = dirfd.stat_child(self.parent_fd, self.leaf, allow_reparse=True)
        except FileNotFoundError:
            return
        if stat_module.S_ISDIR(info.st_mode):
            child = dirfd.open_child_directory(self.parent_fd, self.leaf)
            try:
                registry._rm_children_recursive(child)
            finally:
                os.close(child)
            dirfd.rmdir_child(self.parent_fd, self.leaf)
        else:
            dirfd.unlink_child(self.parent_fd, self.leaf, allow_reparse=True)

    def discard(self) -> None:
        """Delete the verified quarantined copy once the new content is published; best effort."""
        try:
            with contextlib.suppress(FileNotFoundError):
                child = dirfd.open_child_directory(self.parent_fd, self.name)
                try:
                    registry._rm_children_recursive(child)
                finally:
                    os.close(child)
                dirfd.rmdir_child(self.parent_fd, self.name)
        except OSError:
            # The new content is already published; a leftover hidden directory is harmless.
            pass
        finally:
            self.close()

    def abort_publish(self) -> None:
        """A publish failed: clear any partial destination and put the original back, best effort."""
        with contextlib.suppress(OSError, registry.SkillsStatePathError):
            self.clear_destination()
        with contextlib.suppress(OSError, registry.SkillsStatePathError):
            self.restore()
        self.close()


def _identity_of_child(parent_fd: int, name: str, display: Path) -> TreeIdentity:
    child = dirfd.open_child_directory(parent_fd, name, writable=False)
    try:
        info = os.fstat(child)
        dirs, files = registry._collect_fd_tree(child, (), display, "skills installed copy")
        return TreeIdentity(info.st_dev, info.st_ino, tree_digest(dirs, files))
    finally:
        os.close(child)


def quarantine_checked_tree(
    dest: Path,
    expected: TreeIdentity | None,
    *,
    anchor: registry._StateRootAnchor | None,
    state_parts: tuple[str, ...] | None,
) -> TreeQuarantine:
    """Move ``dest`` aside, then require it to be exactly what the drift check saw.

    ``state_parts`` names a destination inside the held state root; any other
    destination is reached through its parent directory, opened without
    following a final link. Returns the quarantined copy for the caller to
    ``discard`` after publishing, or ``restore`` when publishing fails.
    """
    if expected is None:
        raise registry.SkillsStatePathError(_changed_message(dest))
    closers: list[int] = []
    try:
        if state_parts:
            assert anchor is not None
            anchor.revalidate()
            parent_fd, opened = registry._anchor_open_chain(anchor, *state_parts[:-1])
            assert parent_fd is not None
            closers = list(opened)
            leaf = state_parts[-1]
        else:
            parent_fd = dirfd.open_directory_nofollow(dest.parent)
            closers = [parent_fd]
            leaf = dest.name
        name = f".{leaf}.{uuid.uuid4().hex}.quarantine"
        try:
            dirfd.rename_child(parent_fd, leaf, parent_fd, name)
        except OSError as exc:
            raise registry.SkillsStatePathError(_changed_message(dest)) from exc
    except BaseException:
        for fd in reversed(closers):
            with contextlib.suppress(OSError):
                os.close(fd)
        raise
    held = TreeQuarantine(parent_fd=parent_fd, closers=closers, leaf=leaf, name=name, dest=dest)
    try:
        actual: TreeIdentity | None = _identity_of_child(parent_fd, name, dest)
    except (OSError, registry.SkillsStatePathError):
        actual = None
    if actual == expected:
        return held
    try:
        held.restore()
    except (OSError, registry.SkillsStatePathError) as exc:
        held.close()
        raise registry.SkillsStatePathError(
            f"installed copy changed after it was checked: {dest}; it was moved aside as {name} and could "
            "not be put back, inspect it before running again"
        ) from exc
    held.close()
    raise registry.SkillsStatePathError(_changed_message(dest))
