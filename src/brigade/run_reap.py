"""Explicit local reaper for runs whose recorded owner process is gone.

Containment. The reaper binds each candidate run directory with a no-follow
open and refuses to proceed when that identity cannot be established
(:func:`_bind_contained_run_dir`), which fails closed on a symlinked or
reparse-pointed run directory and on any platform without a no-follow
directory primitive.

The whole terminalization transaction then runs under a descriptor binding
(:func:`run_dirfd.bound_run_dir`) whose identity is proven equal to that
already-held handle, so the transaction is pinned to the exact inode the
reaper validated. Inside the binding every write, and every read owned by the
five run modules, resolves descriptor-relative through held directory
handles: ``run_journal``'s no-follow opens, stats, reads, and renames; the
``run_checkpoint`` temp/link/unlink publish; ``run_lifecycle``'s snapshot and
enrollment reads; ``run_shadow``'s evidence reads and quarantine renames; and
``localio``'s atomic writers. Those modules also stop normalizing ``run_dir``
with ``Path.resolve()`` under a binding (``run_journal.normalize_run_dir``),
which is what previously let a swapped symlink relocate a whole transaction.
A directory-entry swap or a symlink planted after bind can therefore only
reach the original held run inode, or the operation fails closed with a
bounded error. No write in this transaction can reach a directory outside the
bound run.

Ordinary callers run with no binding and keep the existing pathname behavior;
the binding is authorized lexically against the exact bound run path, so it
never widens to a sibling run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import dirfd, proc, run_checkpoint, run_dirfd, run_journal, run_lifecycle, run_redaction, runguard, runs_cmd

SCHEMA = "brigade.runs-reap.v1"
DEFAULT_OLDER_THAN = "2h"
MAX_UNCOMMITTED_CHANGE_COUNT = 10_000
# Bounded scan for the work-brief surface, matched to doctor's
# ``_RECOVERY_CHECKPOINTS_SCAN_LIMIT`` so a brief on a repo with no orphan
# never parses unbounded run history.
ORPHAN_SCAN_LIMIT = 50
_OLDER_THAN_RE = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_REAPABLE_STATUSES = runs_cmd._NONTERMINAL_STATUSES
_MALFORMED_PUBLIC_RUN_ID = "malformed"
_UNKNOWN_STATUS = "unknown"


class ReapError(ValueError):
    """Bounded CLI / contract error for the local reaper."""


class RetainRunLockError(runguard.RetainRunLockError):
    """Keep the claimed lock and carry the bounded public skip reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _BoundRunDir:
    """A no-follow handle on a run directory plus its (dev, ino) identity.

    ``still_bound`` compares the held handle and a fresh no-follow open of the
    same name against the identity recorded at bind time. This handle stays
    open for the whole candidate, so its inode cannot be recycled: the
    transaction binding taken in :func:`_terminalize` proves it is the same
    inode by comparing identities, and only then does any writer run.
    """

    def __init__(self, path: Path, fd: int, identity: tuple[int, int]) -> None:
        self.path = path
        self._fd = fd
        self.identity = identity

    def still_bound(self) -> bool:
        try:
            held = os.fstat(self._fd)
        except OSError:
            return False
        if (held.st_dev, held.st_ino) != self.identity:
            return False
        return _directory_identity(self.path) == self.identity

    def read_regular_file(self, name: str, *, max_bytes: int) -> bytes | None:
        """Read a direct child that must be a regular file, without hanging on a FIFO.

        Stats through the already-held directory descriptor, refuses anything
        that is not a regular file or that exceeds ``max_bytes``, then opens
        no-follow and non-blocking so a raced FIFO cannot block the reaper.
        """
        try:
            info = dirfd.stat_child(self._fd, name)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > max_bytes:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = dirfd.open_child_file(self._fd, name, flags)
        except OSError:
            return None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size < 0 or opened.st_size > max_bytes:
                return None
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def close(self) -> None:
        fd = self._fd
        self._fd = -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _public_run_id(name: str) -> str:
    if runs_cmd._is_bare_run_id(name):
        return name
    return _MALFORMED_PUBLIC_RUN_ID


def _public_status(value: object) -> str:
    if isinstance(value, str) and value in _REAPABLE_STATUSES:
        return value
    return _UNKNOWN_STATUS


def _dirfd_identity_available() -> bool:
    # Same predicate as ``run_dirfd`` / ``dirfd.available()`` so identity
    # cannot be claimed on a platform that cannot bind the transaction.
    # Windows stays fail-closed unless ``dirfd.nt_available()`` is true.
    # Tests keep monkeypatching this name.
    return dirfd.available()


def _open_dir_nofollow(path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if nofollow and directory:
        flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        return os.open(path, flags)
    if sys.platform == "win32":
        from .work_cmd import nt_dirfd

        if nt_dirfd.available():
            return nt_dirfd.open_root_directory(path, writable=False)
    raise OSError("no-follow directory open is unavailable")


def _directory_identity(path: Path) -> tuple[int, int] | None:
    """Return (dev, ino) of a real directory without following a final symlink."""
    if not _dirfd_identity_available():
        return None
    try:
        fd = _open_dir_nofollow(path)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISDIR(st.st_mode):
        return None
    try:
        named = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(named.st_mode):
        return None
    if (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
        return None
    return (st.st_dev, st.st_ino)


def _bind_contained_run_dir(path: Path) -> _BoundRunDir | None:
    """Hold a no-follow directory handle bound to the contained identity.

    Returns None when the identity cannot be established. That includes
    platforms without an equivalent no-follow primitive: reap fails closed
    rather than following a symlink or reparse point.
    """
    if not _dirfd_identity_available():
        return None
    try:
        fd = _open_dir_nofollow(path)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            os.close(fd)
            return None
        named = path.lstat()
        if stat.S_ISLNK(named.st_mode) or (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
            os.close(fd)
            return None
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return _BoundRunDir(path, fd, (st.st_dev, st.st_ino))


def parse_older_than(value: str) -> timedelta:
    raw = value.strip()
    match = _OLDER_THAN_RE.fullmatch(raw)
    if match is None:
        raise ValueError("older-than must look like 2h, 30m, 90s, or 1d")
    amount = int(match.group(1))
    seconds = amount * _UNITS[match.group(2).lower()]
    if seconds <= 0:
        raise ValueError("older-than must be a positive duration")
    return timedelta(seconds=seconds)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_fingerprint(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _porcelain_path(line: str) -> str:
    if len(line) < 4:
        return ""
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return path


def _is_brigade_runtime_path(path: str) -> bool:
    return path == ".brigade" or path.startswith(".brigade/")


def _count_uncommitted_changes(workspace: Path) -> int:
    result = proc.run(["git", "status", "--porcelain=v1"], cwd=workspace)
    if result.code != 0:
        return 0
    count = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        if _is_brigade_runtime_path(_porcelain_path(line)):
            continue
        count += 1
    return min(count, MAX_UNCOMMITTED_CHANGE_COUNT)


def _utc_now(now: datetime | None) -> datetime:
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


def _read_run_bytes(run_dir: Path) -> tuple[bytes | None, dict[str, Any] | None]:
    path = run_dir / "run.json"
    try:
        raw = run_journal.bound_read_bytes(path)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return raw, payload


def _read_bound_run_bytes(bound: _BoundRunDir) -> tuple[bytes | None, dict[str, Any] | None]:
    """Initial snapshot through the already-held run directory descriptor.

    The later CAS read stays on :func:`_read_run_bytes` under
    :func:`_bound_transaction`. This path must not follow or block on a
    FIFO, and it must not ingest an oversized receipt.
    """
    try:
        raw = bound.read_regular_file("run.json", max_bytes=run_redaction.MAX_RUN_JSON_BYTES)
        if raw is None:
            return None, None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return raw, payload


def _skip(run_id: str, reason: str) -> dict[str, str]:
    return {"run_id": _public_run_id(run_id), "reason": reason}


def _classify_child(child: Path, resolved_root: Path) -> str | None:
    if not runs_cmd._is_bare_run_id(child.name):
        return "malformed"
    try:
        if child.is_symlink():
            return "symlink"
        if not child.is_dir():
            return None
        if not runs_cmd._run_dir_is_contained(child, resolved_root):
            return "malformed"
    except OSError:
        return "malformed"
    return None


def _revalidate_metadata(
    meta: dict[str, Any],
    *,
    older_than: timedelta,
    now: datetime,
) -> str | None:
    """CAS-time run.json checks. Does not inspect lock ownership or liveness."""
    status = meta.get("status")
    if status == "orphaned":
        return "already-orphaned"
    if not isinstance(status, str) or status not in _REAPABLE_STATUSES or meta.get("finished_at"):
        return "terminal"
    started = _parse_timestamp(meta.get("started_at"))
    if started is None:
        return "malformed"
    if now - started <= older_than:
        return "too-young"
    return None


def _preflight(
    run_dir: Path,
    meta: dict[str, Any],
    *,
    older_than: timedelta,
    now: datetime,
) -> str | None:
    reason = _revalidate_metadata(meta, older_than=older_than, now=now)
    if reason is not None:
        return reason
    workspace = runguard.resolve_run_lock_workspace(meta, run_dir)
    if workspace is None:
        return "ambiguous"
    state = runguard.run_lock_state(workspace, run_dir)
    if state == "live":
        return "live"
    if state != "stale":
        return "ambiguous"
    return None


class ReapBindingError(RuntimeError):
    """The transaction binding could not be pinned to the validated run inode."""


@contextmanager
def _bound_transaction(bound: _BoundRunDir) -> Iterator[run_dirfd.BoundRunDir]:
    """Bind the whole terminalization transaction to the already-held run inode.

    ``bound`` has held a no-follow descriptor on this run directory since
    before any check ran, so that inode cannot be recycled while we hold it.
    The transaction binding re-opens the name no-follow and is accepted only
    when its identity equals the held one; a swap or symlink landing in
    between yields a different identity (or no binding at all) and raises
    ``ReapBindingError`` before any writer runs.
    """
    # A bound path is authorized lexically. If a writer would normalize this
    # run directory to a different path, the binding would silently not apply,
    # so refuse instead of running the transaction unbound.
    try:
        if Path(bound.path).expanduser().resolve() != bound.path:
            raise ReapBindingError("run directory path is not writer-normalized")
    except OSError as exc:
        raise ReapBindingError("run directory path could not be normalized") from exc
    try:
        with run_dirfd.bound_run_dir(bound.path) as transaction:
            if transaction.identity != bound.identity or not transaction.still_bound():
                raise ReapBindingError("run directory identity changed before the transaction")
            if not bound.still_bound():
                raise ReapBindingError("run directory identity changed before the transaction")
            yield transaction
    except OSError as exc:
        raise ReapBindingError("run directory could not be bound") from exc


def _terminalize(run_dir: Path, meta: dict[str, Any], *, workspace: Path, now: datetime) -> dict[str, Any]:
    """Write the orphaned snapshot through the sanctioned run.json writer.

    ``aboyeur._write_json`` owns the whole transaction: it activates the
    lifecycle journal only when this run durably requested it, publishes the
    recovery checkpoint paired with the ``run.orphaned`` event, appends the
    lifecycle transition, records shadow parity, and atomically replaces
    ``run.json`` (projecting it for authority-requested and authoritative
    runs). The reaper holds the matching run lock for ``run_dir``, which is
    what every one of those steps verifies ownership against.

    A legacy snapshot-only run is never migrated: no journal is manufactured
    here, so ``brigade doctor``'s recovery-checkpoint check keeps omitting it
    as ``no-journal`` instead of failing on a journal with no checkpoint.
    """
    from . import aboyeur, localio, receipt_schema

    last_status = _public_status(meta.get("status"))
    dirty = _count_uncommitted_changes(workspace)
    orphaned_at = now.isoformat()
    updated = dict(meta)
    updated.update(
        {
            "status": "orphaned",
            "orphaned_at": orphaned_at,
            "last_observed_status": last_status,
            "uncommitted_change_count": dirty,
            "finished_at": orphaned_at,
        }
    )
    if updated.get("dry_run") is not True:
        tree_fingerprint = localio.tree_fingerprint(workspace)
        if tree_fingerprint is not None:
            updated["tree_fingerprint"] = tree_fingerprint
    aboyeur._write_json(run_dir / "run.json", receipt_schema.stamp_run_receipt(updated))
    return {
        "run_id": _public_run_id(run_dir.name),
        "last_observed_status": last_status,
        "uncommitted_change_count": dirty,
        "orphaned_at": orphaned_at,
    }


def _reap_one(
    run_dir: Path,
    meta: dict[str, Any],
    *,
    fingerprint: str,
    older_than: timedelta,
    now: datetime,
    bound: _BoundRunDir,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    reason = _preflight(run_dir, meta, older_than=older_than, now=now)
    if reason is not None:
        return None, _skip(run_dir.name, reason)
    workspace = runguard.resolve_run_lock_workspace(meta, run_dir)
    assert workspace is not None
    try:
        with runguard.run_lock(workspace, run_dir=run_dir, wait_seconds=0, stale_action="claim"):
            if not bound.still_bound():
                raise RetainRunLockError("concurrently-changed")
            # Everything below runs inside the transaction binding: the CAS
            # read, the revalidation it gates, and the sanctioned writer all
            # resolve descriptor-relative through the same held run inode.
            try:
                with _bound_transaction(bound):
                    raw, current = _read_run_bytes(run_dir)
                    if raw is None or current is None:
                        raise RetainRunLockError("concurrently-changed")
                    if _run_fingerprint(raw) != fingerprint:
                        stale_reason = _revalidate_metadata(current, older_than=older_than, now=now)
                        if stale_reason in {"terminal", "already-orphaned"}:
                            return None, _skip(run_dir.name, "concurrently-changed")
                        raise RetainRunLockError("concurrently-changed")
                    # Under the reaper's newly claimed live lock, re-check run.json
                    # only. Original-owner liveness was already proven before acquire.
                    reason = _revalidate_metadata(current, older_than=older_than, now=now)
                    if reason is not None:
                        return None, _skip(run_dir.name, reason)
                    try:
                        return _terminalize(run_dir, current, workspace=workspace, now=now), None
                    except (run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError, OSError) as exc:
                        # The sanctioned writer refused this run's transaction (an
                        # unready authority gate, a broken chain, or a raw
                        # write-path OSError from the final run.json replace),
                        # possibly after activating the journal or publishing a
                        # checkpoint. Claim mode already deleted the original dead
                        # owner's lock, so releasing here would leave the run with
                        # no lock and no matching ``.stale`` claim for
                        # ``brigade runs recover``. ``RetainRunLockError`` keeps
                        # this claimed lock in place: it records this run_dir, so
                        # once the reaper process exits the lock reads back as a
                        # dead-owner stale lock that recovery matches. The reason
                        # string stays bounded; the writer's diagnostic may name a
                        # path, so it stays out of the contract.
                        raise runguard.RetainRunLockError("orphan reap write refused") from exc
            except ReapBindingError:
                raise RetainRunLockError("concurrently-changed") from None
    except RetainRunLockError as exc:
        # Caught outside the context so ``run_lock`` has already skipped the
        # release. Public skip reason stays the bounded value on the error.
        return None, _skip(run_dir.name, exc.reason)
    except ReapBindingError:
        # The run directory entry stopped resolving to the inode the reaper
        # validated. Nothing was written; report it like any other CAS loss.
        return None, _skip(run_dir.name, "concurrently-changed")
    except runguard.RetainRunLockError:
        # Caught outside the context so ``run_lock`` has already skipped the
        # release. The rest of the scan still runs; other runs in this
        # workspace now see the retained live lock and skip.
        return None, _skip(run_dir.name, "write-refused")
    except runguard.RunLockError:
        return None, _skip(run_dir.name, "concurrently-changed")


def list_orphaned_runs(
    target: Path,
    *,
    limit: int = 8,
    scan_limit: int = ORPHAN_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Newest orphaned runs, over a bounded window of run directories.

    ``limit`` bounds the rows returned; ``scan_limit`` bounds how many run
    directories are opened at all, so a repo with hundreds of runs and no
    orphan costs the work brief a fixed number of ``run.json`` parses.
    """
    root = target.expanduser().resolve() / ".brigade" / "runs"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    resolved_root = runs_cmd._resolved_runs_root(root)
    scanned = 0
    for child in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        if scanned >= scan_limit:
            break
        if _classify_child(child, resolved_root) is not None:
            continue
        scanned += 1
        _raw, meta = _read_run_bytes(child)
        if meta is None or meta.get("status") != "orphaned":
            continue
        dirty = meta.get("uncommitted_change_count")
        count = dirty if isinstance(dirty, int) and not isinstance(dirty, bool) and dirty >= 0 else 0
        run_id = _public_run_id(child.name)
        rows.append(
            {
                "run_id": run_id,
                "last_observed_status": _public_status(meta.get("last_observed_status")),
                "uncommitted_change_count": min(count, MAX_UNCOMMITTED_CHANGE_COUNT),
                "suggested_command": f"brigade runs show {run_id}",
            }
        )
        if len(rows) >= limit:
            break
    return rows


def reap(
    *,
    cwd: Path,
    runs_dir: Path | None,
    older_than: str = DEFAULT_OLDER_THAN,
    json_output: bool = False,
    now: datetime | None = None,
) -> int:
    try:
        threshold = parse_older_than(older_than)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        print("error: --cwd is not a directory", file=sys.stderr)
        return 2
    # Explicit --runs-dir is canonicalized so writer-normalized candidate
    # paths match the binding. The default workspace root must already be
    # canonical: do not follow or resolve a workspace-controlled
    # ``.brigade/runs`` symlink.
    root = runs_dir.expanduser().resolve() if runs_dir is not None else cwd / ".brigade" / "runs"
    if (runs_dir is None and root.is_symlink()) or not root.is_dir():
        print("error: runs directory not found", file=sys.stderr)
        return 2

    clock = _utc_now(now)
    resolved_root = runs_cmd._resolved_runs_root(root)
    reaped: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        classified = _classify_child(child, resolved_root)
        if classified == "symlink":
            skipped.append(_skip(child.name, "symlink"))
            continue
        if classified == "malformed":
            skipped.append(_skip(child.name, "malformed"))
            continue
        if classified is not None or not child.is_dir():
            continue
        bound = _bind_contained_run_dir(child)
        if bound is None:
            # Fail closed before any mutation. A platform with no no-follow
            # directory primitive can never bind, so it reports ``unbindable``
            # for every candidate rather than reusing the bad-run-id reason.
            reason = "malformed" if _dirfd_identity_available() else "unbindable"
            skipped.append(_skip(child.name, reason))
            continue
        try:
            if not bound.still_bound():
                skipped.append(_skip(child.name, "malformed"))
                continue
            raw, meta = _read_bound_run_bytes(bound)
            if raw is None or meta is None:
                skipped.append(_skip(child.name, "malformed"))
                continue
            won, lost = _reap_one(
                child,
                meta,
                fingerprint=_run_fingerprint(raw),
                older_than=threshold,
                now=clock,
                bound=bound,
            )
            if won is not None:
                reaped.append(won)
            elif lost is not None:
                skipped.append(lost)
        finally:
            bound.close()

    if json_output:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "older_than": older_than,
                    "reaped": reaped,
                    "skipped": skipped,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print(f"reaped: {len(reaped)}")
    for row in reaped:
        print(f"  {row['run_id']} {row['last_observed_status']} -> orphaned dirty={row['uncommitted_change_count']}")
    if skipped:
        print(f"skipped: {len(skipped)}")
        for row in skipped:
            print(f"  {row['run_id']} {row['reason']}")
    return 0
