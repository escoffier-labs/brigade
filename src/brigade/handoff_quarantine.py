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


def _private_hold(target: Path, token: str) -> int:
    """Persist each new parent before moving away the only source name."""
    parent = dirfd.open_directory_chain(target.resolve())
    try:
        for name in (".brigade", "handoffs", "held", token):
            try:
                dirfd.mkdir_child(parent, name)
            except FileExistsError:
                if name == token:
                    raise  # Never reuse a quarantine destination.
            child = dirfd.open_child_directory(parent, name)
            try:
                dirfd.fsync_directory(parent)
            except BaseException:
                os.close(child)
                raise
            os.close(parent)
            parent = child
        return parent
    except BaseException:
        os.close(parent)
        raise


def hold(target: Path, source: Path, *, dry_run: bool = False) -> None:
    """Stream a stable regular source, publish small metadata, then rename it into a private hold.

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
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ingest.ChangedHandoffError("oversized handoff changed during hashing")
                digest.update(chunk)
                remaining -= len(chunk)
            if _identity(before) != _identity(os.fstat(fd)):
                raise ingest.ChangedHandoffError("oversized handoff changed during hashing")
        finally:
            os.close(fd)
        # A fresh private child gives the rename a unique, empty destination.
        token = uuid4().hex
        held_rel = Path(".brigade/handoffs/held") / token
        destination = _private_hold(target, token)
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
                    "sha256": digest.hexdigest(),
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
            inbox.mkdir(parents=True, exist_ok=True)
            if len(text.encode("utf-8")) > MAX_DRAFT_BYTES:
                raise OSError("oversized handoff metadata exceeds review limit")
            draft = handoff_publish.publish_handoff(inbox, f"{slug}-oversized-{token}.md", text)
            try:
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
            except OSError:
                draft.unlink()
                raise
            dirfd.fsync_directory(destination)
            dirfd.fsync_directory(parent)
        finally:
            os.close(destination)
    finally:
        os.close(parent)
