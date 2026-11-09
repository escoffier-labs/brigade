"""Atomic publication of a Memory Handoff into an inbox.

A reader that lists ``<inbox>/*.md`` must never see a file that is still being
written, and a writer must never keep a descriptor to a published file. So a
handoff is written to a private temp name that no ingester or rescue lists
(dot-prefixed and not ``.md``), fsynced, then published with a no-replace
rename to its final ``*.md`` name. Once the final name exists the file is
complete and is never written again.

``brigade handoff draft`` is the supported writer. A harness that writes a
handoff directly must follow the same three steps (see
``docs/handoff-promotion.md``).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from . import dirfd

# Not `*.md`, dot-prefixed: ingest (`suffix == ".md"`) and the draft queue (`glob("*.md")`) skip it.
TEMP_PREFIX = ".handoff-"
TEMP_SUFFIX = ".tmp"


class HandoffNameTaken(FileExistsError):
    """The final handoff name already exists. Nothing was replaced."""


def is_temp_name(name: str) -> bool:
    """Whether ``name`` is a private publication temp that readers must ignore."""
    return name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)


def publish_handoff(inbox: Path, name: str, text: str, *, replace: bool = False) -> Path:
    """Write ``text`` to ``inbox/name`` so it appears complete or not at all.

    ``replace=False`` raises ``HandoffNameTaken`` when ``name`` exists and leaves it untouched.
    ``replace=True`` swaps a finished file in atomically. The temp file is removed on any failure.
    """
    data = text.encode("utf-8")
    if dirfd.available():
        return _publish_held(inbox, name, data, replace=replace)
    return _publish_by_path(inbox, name, data, replace=replace)


def _taken(path: Path) -> HandoffNameTaken:
    return HandoffNameTaken(f"handoff draft already exists: {path}")


def _write_and_sync(descriptor: int, data: bytes) -> None:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_held(inbox: Path, name: str, data: bytes, *, replace: bool) -> Path:
    """Descriptor-relative path: the inbox is opened once, so a swapped parent cannot redirect it."""
    parent = dirfd.open_directory_nofollow(inbox)
    temporary = f"{TEMP_PREFIX}{uuid4().hex}{TEMP_SUFFIX}"
    final = inbox / name
    try:
        descriptor = dirfd.open_child_file(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_and_sync(descriptor, data)
            try:
                dirfd.replace_children(parent, temporary, name, replace=replace)
            except FileExistsError as exc:
                raise _taken(final) from exc
        finally:
            try:
                dirfd.unlink_child(parent, temporary)
            except FileNotFoundError:
                pass  # published: the rename already consumed the temp name
        dirfd.fsync_directory(parent)
    finally:
        os.close(parent)
    return final


def _publish_by_path(inbox: Path, name: str, data: bytes, *, replace: bool) -> Path:
    """Fallback where descriptor-relative operations are missing: same steps by pathname."""
    final = inbox / name
    descriptor, temporary = tempfile.mkstemp(dir=inbox, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    try:
        _write_and_sync(descriptor, data)
        if replace:
            os.replace(temporary, final)
        else:
            try:
                # A hard link never follows or replaces the destination, symlinks included.
                os.link(temporary, final)
            except FileExistsError as exc:
                raise _taken(final) from exc
    finally:
        Path(temporary).unlink(missing_ok=True)
    _sync_directory(inbox)
    return final


def _sync_directory(path: Path) -> None:
    if sys.platform == "win32":
        return  # a directory cannot be opened for fsync here
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
