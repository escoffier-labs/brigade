"""Descriptor-bound cassette storage and bounded private-body retention."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from . import dirfd, receipt_schema, run_dirfd

DIRECTORY = "cassettes"
MAX_FILES = 8192
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_RUN_BYTES = 32 * 1024 * 1024
KEEP_RUNS = 50
KEEP_DAYS = 30
MAX_RUN_SCAN_BYTES = 64 * 1024 * 1024
MAX_OUTCOME_BYTES = 64 * 1024 * 1024
MAX_OUTCOME_ROWS = 100_000
MAX_OUTCOME_REFERENCES = 10_000


class CassetteError(RuntimeError):
    """A bounded diagnostic with no worker content or filesystem paths."""


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


@contextmanager
def directory(run_dir: Path, *, create: bool = False, create_root: bool = True) -> Iterator[int]:
    """Walk every component without following links, including on Windows."""
    descriptors: list[int] = []
    try:
        parent = dirfd.open_directory_chain(Path(os.path.abspath(run_dir)), create=create and create_root)
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


@contextmanager
def journal_scope(run_dir: Path) -> Iterator[None]:
    """Bind journal helpers to an existing run opened by a no-follow walk.

    A run deleted mid-flight is never recreated here, so late publication
    cannot resurrect an orphan tree.
    """
    descriptor = dirfd.open_directory_chain(Path(os.path.abspath(run_dir)))
    info = os.fstat(descriptor)
    bound = run_dirfd.BoundRunDir(run_dir, descriptor, (info.st_dev, info.st_ino))
    token = run_dirfd._active.set(bound)
    try:
        yield
    finally:
        run_dirfd._active.reset(token)
        bound.close()


def validated_runs(runs_root: Path) -> list[tuple[float, str, dict[str, Any]]]:
    """Bound the scan and admit identified runs, excluding hidden staging trees."""
    from . import runs_cmd

    parent = dirfd.open_directory_chain(Path(os.path.abspath(runs_root)))
    try:
        names = dirfd.child_names(parent, 10001)
        if len(names) > 10000:
            raise CassetteError("runs inventory exceeds 10000 entries")
        runs = []
        scanned_bytes = 0
        for name in names:
            if name.startswith("."):
                continue
            try:
                child = dirfd.open_child_directory(parent, name)
                try:
                    raw = read(child, "run.json", private=False)
                    modified = os.fstat(child).st_mtime
                finally:
                    os.close(child)
            except (OSError, CassetteError):
                continue
            scanned_bytes += len(raw)
            if scanned_bytes > MAX_RUN_SCAN_BYTES:
                raise CassetteError("runs inventory exceeds byte cap")
            try:
                metadata = json.loads(raw)
            except ValueError:
                continue
            # The artifact writer identifies runs by directory and schema, not run_id.
            if isinstance(metadata, dict) and (
                metadata.get("run_id") == name
                or (
                    "run_id" not in metadata
                    and runs_cmd._is_bare_run_id(name)
                    and metadata.get("schema") == receipt_schema.RUN_RECEIPT_SCHEMA
                    and metadata.get("schema_version") == receipt_schema.RUN_RECEIPT_SCHEMA_VERSION
                )
            ):
                started = metadata.get("started_at")
                # Keep only the bounded sort field rather than every run body.
                summary = {"started_at": started} if isinstance(started, str) and len(started) <= 128 else {}
                runs.append((modified, name, summary))
        return runs
    finally:
        os.close(parent)


@contextmanager
def _outcome_lock(workspace: Path) -> Iterator[None]:
    """Share capture's lock protocol without following links or writing aliases."""
    from . import outcome_cmd

    path = outcome_cmd._records_lock_path(workspace)
    parent = dirfd.open_directory_chain(Path(os.path.abspath(path.parent)), create=True)
    try:
        descriptor = dirfd.open_child_lock_file(parent, path.name)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "r+b") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CassetteError("outcome lock is unsafe")
        outcome_cmd._ensure_lock_sentinel(handle)
        outcome_cmd._acquire_file_lock(handle, wait_seconds=outcome_cmd._LOCK_WAIT_SECONDS)
        try:
            yield
        finally:
            outcome_cmd._release_file_lock(handle)


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
            removed = _remove_owned_temps(parent)
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


def purge_inactive(run_dir: Path, *, workspace: Path, private_only: bool = False) -> int:
    """Fence maintenance against publication; caller excludes new run owners.

    CLI callers hold the workspace run lock. Retention runs inside that lock
    for the current run, so other runs in the workspace cannot start meanwhile.
    """
    from . import run_journal, run_lifecycle, runguard

    journal = run_dir / "events" / "lifecycle.jsonl"
    with (
        journal_scope(run_dir),
        run_lifecycle.checkpoint_event_pair(),
        run_journal.journal_mutation(journal) if run_journal.bound_lstat(journal) is not None else nullcontext(),
    ):
        # CLI maintenance owns this run's lock itself. A different live process
        # must never lose its stop marker, publication temp, or private bodies.
        if runguard.has_active_run_owner(workspace, run_dir) and not runguard.is_active_run_owner(workspace, run_dir):
            raise CassetteError("cassette purge requires an inactive run")
        return purge(run_dir, private_only=private_only)


def _remove_owned_temps(parent: int) -> int:
    """Unlink private publication debris before checking final hard-link counts."""
    names = dirfd.child_names(parent, MAX_FILES + 1)
    if len(names) > MAX_FILES:
        raise CassetteError("cassette inventory exceeds file cap")
    removed = 0
    for name in names:
        if not re.fullmatch(r"\.cassette-[0-9a-f]{32}\.tmp", name):
            continue
        try:
            info = dirfd.stat_child(parent, name)
            if not stat.S_ISREG(info.st_mode):
                continue
            if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                continue
            dirfd.unlink_child(parent, name)
            removed += 1
        except FileNotFoundError:
            continue
    if removed:
        dirfd.fsync_directory(parent)
    return removed


def _pinned_runs(workspace: Path) -> set[str] | None:
    """Stream outcome references, conservatively pinning on unreadable rows."""
    path = workspace / "memory" / "outcome" / "records.jsonl"
    try:
        parent = dirfd.open_directory_chain(Path(os.path.abspath(path.parent)))
        try:
            descriptor = dirfd.open_child_file(
                parent, path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            )
        finally:
            os.close(parent)
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_OUTCOME_BYTES:
            return None
        pinned = set()
        rows = total_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while line := handle.readline(8 * 1024 * 1024 + 1):
                rows += 1
                total_bytes += len(line)
                if len(line) > 8 * 1024 * 1024 or total_bytes > MAX_OUTCOME_BYTES or rows > MAX_OUTCOME_ROWS:
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
                    if len(pinned) > MAX_OUTCOME_REFERENCES:
                        return None
        return pinned
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)


def retain_private(workspace: Path, runs_root: Path, *, current: Path) -> int:
    """Keep newest N runs and at most D days, except outcome-pinned bodies."""
    try:
        runs = []
        for modified, name, summary in validated_runs(runs_root):
            started = summary.get("started_at")
            if started is not None:
                try:
                    parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    modified = parsed.timestamp()
                except (ValueError, OverflowError, OSError):
                    # A damaged timestamp affects only this run's age and order.
                    # validated_runs already supplied its directory mtime.
                    pass
            runs.append((modified, name, summary))
    except CassetteError as exc:
        print(f"cassette retention skipped: {exc}", file=sys.stderr)
        return 0
    runs.sort(key=lambda item: (item[0], item[1]), reverse=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    candidates = [
        runs_root / name
        for index, (modified, name, _) in enumerate(runs)
        if runs_root / name != current and (index >= KEEP_RUNS or modified < cutoff)
    ]
    # Still report unreadable evidence when there is currently nothing to prune.
    if not candidates and _pinned_runs(workspace) is not None:
        return 0
    removed = 0
    # Capture fsyncs under this same lock. No newly durable pin can be missed
    # between the snapshot and deletion, including the ledger's first append.
    with _outcome_lock(workspace):
        pinned = _pinned_runs(workspace)
        if pinned is None:
            print("cassette retention skipped: outcome evidence unreadable or scan cap exceeded", file=sys.stderr)
            return 0
        from . import runguard

        for path in candidates:
            if path.name not in pinned and not runguard.has_active_run_owner(workspace, path):
                removed += purge_inactive(path, workspace=workspace, private_only=True)
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
