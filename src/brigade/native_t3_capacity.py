"""Workspace-scoped governed native T3 occupancy and capacity.

T3 Code keeps no admission ledger and no child concurrency limit, so Brigade
bounds concurrent governed T3 tasks. One unit occupies for every durable launch
claim in every native-enrolled run under the workspace runs root. A unit stays
live until a trusted terminal result proves quiescence and that exact result is
acknowledged. A binding abandoned before any launch claim charged nothing and
occupies nothing.

The scanner never takes a run lock. It reads with bounded, no-follow reads and
marks its answer non-authoritative on any malformed, unreadable, replaced or
unverifiable run, because a claim must fail closed instead of reading an
unreadable run as zero live work.
"""

from __future__ import annotations

import json
import os
import stat
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from . import roster as roster_mod
from . import run_journal, run_lifecycle, runguard, worktree_lease
from . import run_task_journal as journal

MAX_RUN_ENTRIES = 512
MAX_RUN_RECEIPT_BYTES = 262_144
MAX_TOTAL_JOURNAL_BYTES = 67_108_864
CAPACITY_LOCK_NAME = "native-capacity.lock"
CAPACITY_WAIT_SECONDS = 10.0
CAPACITY_POLL_SECONDS = 0.05

CAPACITY_AVAILABLE = "capacity-available"
CAPACITY_DENIED = "capacity-denied"
CAPACITY_UNKNOWN = "capacity-unknown"

HOLD_EVENT = "task.native-held"
HOLD_RESOLVED_EVENT = "task.native-hold-resolved"
LAUNCH_CLAIM = "task.launch-claimed"
_ACCEPT_CONFLICT = "accept-conflict"
_ABANDONED = "abandoned-unknown"
_HELD_REASONS = frozenset({_ACCEPT_CONFLICT, _ABANDONED})
_READ_EVENT_TYPES = frozenset(
    {
        LAUNCH_CLAIM,
        "task.observed",
        "task.acknowledged",
        HOLD_EVENT,
        HOLD_RESOLVED_EVENT,
    }
)


class CapacityError(RuntimeError):
    """A bounded capacity-gate refusal, safe for a fixed diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Occupancy:
    """Live governed units, the per-seat split, and whether the read is trusted."""

    live: int
    seats: tuple[tuple[str, int], ...]
    authoritative: bool
    diagnostic: str | None = None

    def seat_live(self, seat: str) -> int:
        return dict(self.seats).get(seat, 0)


@dataclass(frozen=True)
class CapacityDecision:
    code: str
    free: int | None
    global_free: int | None
    seat_free: int | None

    @property
    def granted(self) -> bool:
        return self.code == CAPACITY_AVAILABLE


class _Unverified(Exception):
    """One run entry could not be read with the scan's trust contract."""


def runs_root(workspace: Path) -> Path:
    """The workspace runs root, derived the same way every native verb does."""
    return runguard.lock_path(workspace).parent / "runs"


def capacity_lock_path(workspace: Path) -> Path:
    return runguard.lock_path(workspace).with_name(CAPACITY_LOCK_NAME)


def _read_bounded_no_follow(path: Path, max_bytes: int) -> bytes:
    """Read one regular file without following links and without unbounded allocation."""
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise _Unverified("replaced")
    if not stat.S_ISREG(info.st_mode):
        raise _Unverified("not-regular")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise _Unverified("replaced")
        if opened.st_size > max_bytes:
            raise _Unverified("oversize")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 62_500)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _Unverified("oversize")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _run_receipt(run_dir: Path) -> Mapping[str, object]:
    try:
        raw = _read_bounded_no_follow(run_dir / "run.json", MAX_RUN_RECEIPT_BYTES)
        receipt = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise _Unverified("receipt") from None
    if not isinstance(receipt, dict):
        raise _Unverified("receipt")
    return receipt


def _journal_events(run_dir: Path) -> tuple[list[run_journal.RunEvent], bool]:
    """Read the lifecycle journal without a lock; report whether it is trusted."""
    try:
        report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError):
        raise _Unverified("journal") from None
    trusted = report.partial_tail is None and not report.chain_errors
    return list(report.events), trusted


def _journal_bytes(run_dir: Path) -> int:
    """Size one lifecycle journal with a no-follow lstat; absent reads as zero."""
    try:
        info = os.lstat(run_lifecycle._journal_path(run_dir))
    except FileNotFoundError:
        return 0
    except OSError:
        raise _Unverified("journal") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _Unverified("journal")
    return int(info.st_size)


def _scan_units(events: list[run_journal.RunEvent]) -> Counter[str]:
    """Fold one run's launch claims into per-seat live units."""
    claims: dict[str, tuple[str, object]] = {}
    observed: dict[str, tuple[object, object, object]] = {}
    acknowledged: dict[str, object] = {}
    held: dict[str, str] = {}
    for event in events:
        if event.event_type not in _READ_EVENT_TYPES:
            continue
        payload = event.payload
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not journal.task_id(task_id):
            raise _Unverified("payload")
        if event.event_type == LAUNCH_CLAIM:
            seat = payload.get("seat")
            if not isinstance(seat, str) or not journal.token(seat):
                raise _Unverified("payload")
            if task_id in claims:
                raise _Unverified("payload")
            claims[task_id] = (seat, payload.get("attempt_id"))
        elif event.event_type == "task.observed":
            # An observation counts only for the launch claim it belongs to.
            if task_id not in claims or payload.get("attempt_id") != claims[task_id][1]:
                raise _Unverified("payload")
            observed[task_id] = (
                payload.get("status"),
                payload.get("containment"),
                payload.get("result_id"),
            )
        elif event.event_type == "task.acknowledged":
            if task_id not in claims:
                raise _Unverified("payload")
            acknowledged[task_id] = payload.get("result_id")
        elif event.event_type == HOLD_EVENT:
            reason = payload.get("reason")
            if reason not in _HELD_REASONS:
                raise _Unverified("payload")
            held[task_id] = str(reason)
        else:
            reason = payload.get("reason")
            if reason != _ACCEPT_CONFLICT or held.get(task_id) != reason:
                raise _Unverified("payload")
            held.pop(task_id, None)
    units: Counter[str] = Counter()
    for task_id, (seat, _attempt) in claims.items():
        if held.get(task_id) is not None:
            units[seat] += 1
            continue
        status, containment, result_id = observed.get(task_id, (None, None, None))
        freed = (
            status in journal.TERMINAL
            and containment == "quiescent"
            and isinstance(result_id, str)
            and acknowledged.get(task_id) == result_id
        )
        if not freed:
            units[seat] += 1
    return units


def governed_occupancy(workspace: Path) -> Occupancy:
    """Count live governed native units under the workspace runs root."""
    workspace = workspace.expanduser().resolve()
    root = runs_root(workspace)
    try:
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except FileNotFoundError:
        return Occupancy(0, (), True)
    except OSError:
        return Occupancy(0, (), False, "runs-unreadable")
    if len(entries) > MAX_RUN_ENTRIES:
        return Occupancy(0, (), False, "runs-truncated")
    live: Counter[str] = Counter()
    authoritative = True
    total_bytes = 0
    for entry in entries:
        try:
            if entry.is_symlink():
                authoritative = False
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            authoritative = False
            continue
        run_dir = Path(entry.path)
        try:
            receipt = _run_receipt(run_dir)
            if receipt.get("kind") != "native-t3":
                continue
            if receipt.get("lock_workspace") != str(workspace):
                raise _Unverified("workspace")
            total_bytes += _journal_bytes(run_dir)
            if total_bytes > MAX_TOTAL_JOURNAL_BYTES:
                return Occupancy(0, (), False, "runs-truncated")
            events, trusted = _journal_events(run_dir)
            authoritative = authoritative and trusted
            live.update(_scan_units(events))
        except _Unverified:
            authoritative = False
    return Occupancy(
        sum(live.values()),
        tuple(sorted(live.items())),
        authoritative,
        None if authoritative else "run-unverified",
    )


def assess(roster: roster_mod.Roster, seat: str, occupancy: Occupancy) -> CapacityDecision:
    """Apply the global and per-seat limits without inventing per-seat allowances."""
    if not occupancy.authoritative:
        return CapacityDecision(CAPACITY_UNKNOWN, None, None, None)
    global_free = roster.max_workers - occupancy.live
    declaration = roster.t3_targets.get(seat) or {}
    limit = declaration.get("max_live")
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = None
    # Only an explicit positive max_live constrains a seat. limits.max_workers
    # is one workspace-wide allowance, never one allowance per seat.
    seat_free = None if limit is None else limit - occupancy.seat_live(seat)
    free = global_free if seat_free is None else min(global_free, seat_free)
    code = CAPACITY_DENIED if free <= 0 else CAPACITY_AVAILABLE
    return CapacityDecision(code, free, global_free, seat_free)


@contextmanager
def capacity_lock(workspace: Path, *, wait_seconds: float = CAPACITY_WAIT_SECONDS) -> Iterator[Path]:
    """Hold the one workspace-scoped native capacity lock for the claim.

    The lock is crash-safe through the same publish, stale-claim and release
    patterns the run lock uses. It fails closed on a bounded wait timeout or a
    stale lock that is not yet reconcilable. ``claim`` acquires this lock and
    only then the per-invocation run lock, and no path may reverse that order.
    """
    try:
        path = capacity_lock_path(workspace)
    except (runguard.RunGuardError, OSError) as exc:
        raise CapacityError("capacity-lock", "the native capacity lock is unavailable") from exc
    try:
        with worktree_lease.run_lock_parent(workspace, path):
            try:
                ownership = runguard._wait_to_acquire_lock(
                    path, wait_seconds=wait_seconds, poll_interval=CAPACITY_POLL_SECONDS
                )
            except (runguard.RunLockError, runguard.RunGuardError, OSError) as exc:
                raise CapacityError("capacity-lock", "the native capacity lock is unavailable") from exc
            try:
                yield path
            finally:
                runguard._release_lock(path, ownership)
    except CapacityError:
        raise
    except (runguard.RunGuardError, OSError) as exc:
        raise CapacityError("capacity-lock", "the native capacity lock is unavailable") from exc
