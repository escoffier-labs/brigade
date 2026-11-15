"""Bounded metadata review and no-follow holding for oversized handoffs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import dirfd, handoff_publish
from .memory_inbox_io import MAX_DRAFT_BYTES

CHUNK_BYTES = 65536
_STABLE_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(info, name) for name in _STABLE_FIELDS)


def _sample(fd: int, size: int) -> str:
    """Hash at most two 64 KiB reads, independent of the logical file size.

    Domain and size bind the sample without claiming a full-file hash.
    """
    from . import ingest

    digest = hashlib.sha256(b"brigade-handoff-sample-v1\0" + str(size).encode("ascii") + b"\0")
    first = min(CHUNK_BYTES, size)
    last = min(CHUNK_BYTES, size - first)
    for offset, length in ((0, first), (size - last, last)):
        if not length:
            continue
        os.lseek(fd, offset, os.SEEK_SET)
        chunk = os.read(fd, length)
        if len(chunk) != length:
            raise ingest.ChangedHandoffError("oversized handoff changed during sampling")
        digest.update(chunk)
    return digest.hexdigest()


def _verify_held(destination: int, name: str, before: os.stat_result, sample: str) -> None:
    """Refuse a held file that gained a link or whose sampled bytes changed during the move."""
    from . import ingest

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = dirfd.open_child_file(destination, name, flags, share_write=False)
    try:
        held = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if held.st_nlink != 1 or any(getattr(held, field) != getattr(before, field) for field in fields):
            raise ingest.ChangedHandoffError("oversized handoff changed during quarantine")
        if _sample(fd, held.st_size) != sample:
            raise ingest.ChangedHandoffError("oversized handoff changed during quarantine")
    finally:
        os.close(fd)


def _private_hold(target: Path, token: str) -> tuple[int, int]:
    """Return the held parent and fresh child, persisting each new directory."""
    parent = dirfd.open_directory_chain(target.absolute())
    try:
        for name in (".brigade", "handoffs", "held"):
            try:
                dirfd.mkdir_child(parent, name)
            except FileExistsError:
                pass
            child = dirfd.open_child_directory(parent, name)
            try:
                dirfd.fsync_directory(parent)
            except BaseException:
                os.close(child)
                raise
            os.close(parent)
            parent = child
        dirfd.mkdir_child(parent, token)
        try:
            child = dirfd.open_child_directory(parent, token)
            try:
                dirfd.fsync_directory(parent)
            except BaseException:
                os.close(child)
                raise
        except BaseException:
            dirfd.rmdir_child(parent, token)
            raise
        return parent, child
    except BaseException:
        os.close(parent)
        raise


def _publish_metadata(parent: int, name: str, text: str) -> None:
    """Publish and roll back only through the confined inbox descriptor."""
    temporary = f".handoff-{uuid4().hex}.tmp"
    fd = dirfd.open_child_file(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        handoff_publish._write_and_sync(fd, text.encode("utf-8"))
        dirfd.replace_children(parent, temporary, name, replace=False)
    finally:
        try:
            dirfd.unlink_child(parent, temporary)
        except FileNotFoundError:
            pass
    dirfd.fsync_directory(parent)


def hold(target: Path, source: Path, *, dry_run: bool = False) -> None:
    """Sample a stable regular source, publish small metadata, then rename it into a private hold.

    Never parse, snapshot or copy the source. A cross-device rename fails closed.
    The review draft is published first so an interrupted move always retains a source.
    """
    from . import ingest

    if ingest._is_tracked(source, ingest._git_tracked_files(source.parent)):
        raise ingest.TrackedHandoffError("handoff became tracked before quarantine")
    if dry_run:
        return
    parent = dirfd.open_directory_chain(source.absolute().parent)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = dirfd.open_child_file(parent, source.name, flags, share_write=False)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OSError("oversized handoff is not a single-link regular file")
            sample = _sample(fd, before.st_size)
            if _identity(before) != _identity(os.fstat(fd)):
                raise ingest.ChangedHandoffError("oversized handoff changed during sampling")
        finally:
            os.close(fd)
        # A fresh private child gives the rename a unique, empty destination.
        token = uuid4().hex
        held_rel = Path(".brigade/handoffs/held") / token
        held_parent, destination = _private_hold(target, token)
        moved = False
        try:
            info = os.fstat(destination)
            if sys.platform != "win32" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise OSError("handoff hold is not private")
            if ingest._is_tracked(source, ingest._git_tracked_files(source.parent)):
                raise ingest.TrackedHandoffError("handoff became tracked before quarantine")
            if _identity(dirfd.stat_child(parent, source.name)) != _identity(before):
                raise ingest.ChangedHandoffError("oversized handoff changed before quarantine")
            slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            # JSON quoting keeps hostile filenames out of the routing header.
            metadata = json.dumps(
                {
                    "filename": source.name,
                    "size": before.st_size,
                    "dev": before.st_dev,
                    "ino": before.st_ino,
                    "mtime_ns": before.st_mtime_ns,
                    "digest": {
                        "algorithm": "sha256-size-first-last-v1",
                        "chunk_bytes": CHUNK_BYTES,
                        "max_read_bytes": 2 * CHUNK_BYTES,
                        "hex": sample,
                    },
                    "held_path": (held_rel / source.name).as_posix(),
                },
                ensure_ascii=True,
                sort_keys=True,
                indent=2,
            )
            text = (
                f"<!-- routed from oversized-handoff\n     reason: handoff-too-large\n     routed-at: {slug} -->\n\n"
                f"Oversized handoff retained for a person.\n\n```json\n{metadata}\n```\n"
            )
            inbox = target / "memory/handoff-inbox"
            if len(text.encode("utf-8")) > MAX_DRAFT_BYTES:
                raise OSError("oversized handoff metadata exceeds review limit")
            review_parent = dirfd.open_directory_chain(inbox.absolute(), create=True)
            draft_name = f"{slug}-oversized-{token}.md"
            try:
                try:
                    _publish_metadata(review_parent, draft_name, text)
                    if _identity(dirfd.stat_child(parent, source.name)) != _identity(before):
                        raise ingest.ChangedHandoffError("oversized handoff changed before quarantine")
                    if dirfd.posix_available():
                        os.rename(source.name, source.name, src_dir_fd=parent, dst_dir_fd=destination)
                    elif dirfd.nt_available():
                        dirfd._nt_dirfd().replace_children(
                            parent, source.name, source.name, replace=False, destination_parent=destination
                        )
                    else:
                        raise OSError("no-follow handoff move is unavailable")
                    moved = True
                    # A rename may update ctime, so ctime cannot be compared here. A retained
                    # second link and a rewrite that restores mtime are caught by the link
                    # count and by resampling the held bytes.
                    _verify_held(destination, source.name, before, sample)
                except OSError:
                    try:
                        dirfd.unlink_child(review_parent, draft_name)
                    except FileNotFoundError:
                        pass
                    raise
            finally:
                os.close(review_parent)
            dirfd.fsync_directory(destination)
            dirfd.fsync_directory(parent)
        finally:
            os.close(destination)
            try:
                if not moved:
                    dirfd.rmdir_child(held_parent, token)
                    dirfd.fsync_directory(held_parent)
            finally:
                os.close(held_parent)
    finally:
        os.close(parent)
