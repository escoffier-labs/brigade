"""Descriptor-bound cassette storage and bounded private-body retention."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from . import dirfd

DIRECTORY = "cassettes"
MAX_FILES = 8192
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_RUN_BYTES = 32 * 1024 * 1024
KEEP_RUNS = 50
KEEP_DAYS = 30


class CassetteError(RuntimeError):
    """A bounded diagnostic with no worker content or filesystem paths."""


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


@contextmanager
def directory(run_dir: Path, *, create: bool = False) -> Iterator[int]:
    """Walk every component without following links, including on Windows."""
    descriptors: list[int] = []
    try:
        parent = dirfd.open_directory_chain(Path(os.path.abspath(run_dir)), create=create)
        descriptors.append(parent)
        for name in ("events", DIRECTORY):
            try:
                child = dirfd.open_child_directory(parent, name)
            except FileNotFoundError:
                if not create:
                    raise
                dirfd.mkdir_child(parent, name)
                child = dirfd.open_child_directory(parent, name)
            descriptors.append(child)
            if create and os.name == "posix":
                os.fchmod(child, 0o700)
            parent = child
        yield parent
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read(
    parent: int,
    name: str,
    *,
    limit: int = MAX_ENTRY_BYTES,
    private: bool = True,
    offset: int = 0,
    identity: tuple[int, int] | None = None,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = dirfd.open_child_file(parent, name, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 <= offset <= info.st_size
            or info.st_size - offset > limit
            or (identity is not None and (info.st_dev, info.st_ino) != identity)
        ):
            raise CassetteError("cassette file is unsafe or oversized")
        if private and os.name == "posix" and info.st_mode & 0o077:
            raise CassetteError("cassette file permissions are unsafe")
        os.lseek(descriptor, offset, os.SEEK_SET)
        data = bytearray()
        while chunk := os.read(descriptor, min(65536, limit + 1 - len(data))):
            data.extend(chunk)
            if len(data) > limit:
                raise CassetteError("cassette file exceeds byte cap")
        return bytes(data)
    finally:
        os.close(descriptor)


def publish(parent: int, data: bytes) -> str:
    """Private temp, fsync, no-replace publication, and checked collisions."""
    digest = hashlib.sha256(data).hexdigest()
    name = f"{digest}.json"
    temporary = f".cassette-{uuid4().hex}.tmp"
    descriptor = dirfd.open_child_file(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        try:
            dirfd.replace_children(parent, temporary, name, replace=False)
        except FileExistsError:
            if read(parent, name) != data:
                raise CassetteError("cassette content address collision") from None
    finally:
        try:
            dirfd.unlink_child(parent, temporary)
        except FileNotFoundError:
            pass
        dirfd.fsync_directory(parent)
    return digest


def inventory(parent: int, *, private_only: bool = False) -> list[tuple[str, os.stat_result]]:
    result = []
    names = dirfd.child_names(parent, MAX_FILES + 1)
    if len(names) > MAX_FILES:
        raise CassetteError("cassette inventory exceeds file cap")
    for name in names:
        if private_only and (name in {".stopped", ".retained"} or name.startswith(".cassette-")):
            continue
        try:
            info = dirfd.stat_child(parent, name)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            if private_only:
                continue
            raise CassetteError("cassette inventory contains unsafe entries")
        result.append((name, info))
    return result


def purge(run_dir: Path, *, private_only: bool = False) -> int:
    """Remove bodies or the entire cassette inventory, never the journal."""
    try:
        with directory(run_dir) as parent:
            removed = 0
            for name, _ in inventory(parent, private_only=private_only):
                try:
                    if private_only:
                        obj = json.loads(read(parent, name))
                        if not isinstance(obj, dict) or obj.get("privacy_class") != "private":
                            continue
                    dirfd.unlink_child(parent, name)
                except FileNotFoundError:
                    # Retention may concurrently delete another inventoried body.
                    # Keep deleting the remaining entries rather than returning early.
                    continue
                except (ValueError, CassetteError):
                    if private_only:
                        continue
                    raise
                removed += 1
            dirfd.fsync_directory(parent)
            return removed
    except FileNotFoundError:
        return 0


def _pinned_runs(workspace: Path) -> set[str] | None:
    """Stream outcome references, conservatively pinning on unreadable rows."""
    path = workspace / "memory" / "outcome" / "records.jsonl"
    try:
        descriptor = dirfd.open_file_nofollow(path, os.O_RDONLY)
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        pinned = set()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while line := handle.readline(8 * 1024 * 1024 + 1):
                if len(line) > 8 * 1024 * 1024:
                    return None
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    return None
                reference = row.get("evidence_ref")
                if isinstance(reference, str):
                    # Outcome capture stores the run.json path, including custom runs roots.
                    source = Path(reference)
                    pinned.add(source.parent.name if source.name == "run.json" else source.name)
        return pinned
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)


def retain_private(workspace: Path, runs_root: Path, *, current: Path) -> int:
    """Keep newest N runs and at most D days, except outcome-pinned bodies."""
    pinned = _pinned_runs(workspace)
    if pinned is None:
        print("cassette retention skipped: outcome evidence unreadable or row exceeds 8 MiB", file=sys.stderr)
        return 0
    parent = dirfd.open_directory_chain(Path(os.path.abspath(runs_root)))
    try:
        runs = []
        names = dirfd.child_names(parent, 10001)
        if len(names) > 10000:
            print("cassette retention skipped: runs inventory exceeds 10000 entries", file=sys.stderr)
            return 0
        for name in names:
            try:
                info = dirfd.stat_child(parent, name)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                runs.append((info.st_mtime, name))
    finally:
        os.close(parent)
    runs.sort(reverse=True)
    removed = 0
    cutoff = time.time() - KEEP_DAYS * 86400
    for index, (modified, name) in enumerate(runs):
        path = runs_root / name
        if path == current or name in pinned:
            continue
        if index >= KEEP_RUNS or modified < cutoff:
            removed += purge(path, private_only=True)
    return removed


def retain_private_once(workspace: Path, current: Path) -> None:
    """Claim one scan per run, isolating maintenance failures from recording."""
    try:
        with directory(current) as parent:
            try:
                descriptor = dirfd.open_child_file(parent, ".retained", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            dirfd.fsync_directory(parent)
        retain_private(workspace, current.parent, current=current)
    except Exception:
        print("cassette retention skipped: storage unavailable", file=sys.stderr)
