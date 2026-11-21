"""Workspace-scoped governed native child occupancy and capacity.

The host keeps no admission ledger and no child concurrency limit, so Brigade
bounds concurrent governed native tasks. One unit occupies for every durable launch
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
import re
import stat
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from . import roster as roster_mod
from . import run_dirfd, run_journal, run_lifecycle, runguard, worktree_lease
from . import run_task_journal as journal
from .native_child_host import NativeChildCapacityError, NativeChildHost
from .native_child_observations import QUIESCENT_RESOURCE_STATES, RESOURCE_EVENT, RESOURCE_STATES

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
_NATIVE_KIND_PREFIX = "native-"
# Native adapters derive run IDs as <host>-<request SHA-256>. Batch has its
# own legacy hash-derived namespace. Detect damaged native kind fields even
# when the caller belongs to a different host.
_HASHED_RUN = re.compile(r"([a-z][a-z0-9-]*)-[0-9a-f]{64}", re.ASCII)
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
        RESOURCE_EVENT,
    }
)


CapacityError = NativeChildCapacityError


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
    """The one workspace-scoped capacity lock, shared by every native host."""
    return runguard.lock_path(workspace).with_name(CAPACITY_LOCK_NAME)


def _run_receipt(bound: run_dirfd.BoundRunDir) -> Mapping[str, object]:
    try:
        raw = bound.read_bytes("run.json", max_bytes=MAX_RUN_RECEIPT_BYTES)
    except OSError:
        raise _Unverified("receipt") from None
    if raw is None:
        raise _Unverified("receipt")
    try:
        receipt = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Unverified("receipt") from None
    if not isinstance(receipt, dict):
        raise _Unverified("receipt")
    return receipt


def _journal_size(run_dir: Path) -> int | None:
    """Size the lifecycle journal under the bound run, ``None`` when absent."""
    try:
        info = run_journal.bound_lstat(run_lifecycle._journal_path(run_dir), strict=True)
    except (OSError, run_journal.RunJournalError):
        raise _Unverified("journal") from None
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _Unverified("journal")
    return int(info.st_size)


def _journal_events(run_dir: Path) -> tuple[list[run_journal.RunEvent], bool]:
    """Read the lifecycle journal without a lock; report whether it is trusted."""
    try:
        report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError):
        raise _Unverified("journal") from None
    trusted = report.partial_tail is None and not report.chain_errors
    return list(report.events), trusted


def _scan_run(host: NativeChildHost, run_dir: Path, name: str, workspace: Path) -> tuple[Counter[str], int, bool]:
    """Scan one run entry and report units, journal bytes and trust."""
    try:
        with run_dirfd.bound_run_dir(run_dir) as bound:
            receipt = _run_receipt(bound)
            kind = receipt.get("kind")
            native = isinstance(kind, str) and kind.startswith(_NATIVE_KIND_PREFIX)
            identity = _HASHED_RUN.fullmatch(name)
            if name.startswith(host.run_prefix) and kind != host.run_kind:
                raise _Unverified("kind")
            if identity is not None and identity[1] != "batch" and kind != _NATIVE_KIND_PREFIX + identity[1]:
                raise _Unverified("kind")
            if not native and receipt.get("lifecycle_journal_requested") is not True:
                return Counter(), 0, True
            size = _journal_size(run_dir)
            if size is None:
                # Enrollment publishes run.json before the first journal fact.
                # A run that requested journaling but has none is unverifiable,
                # never zero live work, so a claim racing an open fails closed.
                raise _Unverified("journal-missing")
            if not native:
                return Counter(), size, True
            if receipt.get("lock_workspace") != str(workspace):
                raise _Unverified("workspace")
            events, trusted = _journal_events(run_dir)
            # The journal is read by path, so a run directory replaced during
            # the scan could hide live claims. Fail closed unless the bound
            # directory is still the one at this path.
            if not bound.still_bound():
                raise _Unverified("run-dir")
            return Counter(seat for seat, _reason in _live_claims(events).values()), size, trusted
    except OSError:
        raise _Unverified("run-dir") from None


def _live_claims(events: Sequence[run_journal.RunEvent]) -> dict[str, tuple[str, str]]:
    """Fold one run's live launch claims into seats and fixed capacity reasons."""
    claims: dict[str, tuple[str, object]] = {}
    observed: dict[str, tuple[object, object, object]] = {}
    acknowledged: dict[str, object] = {}
    held: dict[str, str] = {}
    resources: dict[str, str] = {}
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
        elif event.event_type == RESOURCE_EVENT:
            reason = payload.get("reason")
            if (
                task_id not in claims
                or payload.get("attempt_id") != claims[task_id][1]
                or not isinstance(reason, str)
                or reason not in RESOURCE_STATES
                or (resources.get(task_id) in QUIESCENT_RESOURCE_STATES and reason != resources[task_id])
            ):
                raise _Unverified("payload")
            resources[task_id] = reason
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
    live: dict[str, tuple[str, str]] = {}
    for task_id, (seat, _attempt) in claims.items():
        reason = held.get(task_id)
        if reason is None and resources.get(task_id) not in QUIESCENT_RESOURCE_STATES:
            reason = resources.get(task_id)
        if reason is None:
            status, containment, result_id = observed.get(task_id, (None, None, None))
            terminal = status in journal.TERMINAL and containment == "quiescent" and isinstance(result_id, str)
            if not terminal:
                reason = "awaiting-terminal"
            elif acknowledged.get(task_id) != result_id:
                reason = "awaiting-ack"
        if reason is not None:
            live[task_id] = (seat, reason)
    return live


def capacity_reasons(events: Sequence[run_journal.RunEvent]) -> dict[str, str]:
    try:
        return {task_id: reason for task_id, (_seat, reason) in _live_claims(events).items()}
    except _Unverified:
        raise CapacityError(CAPACITY_UNKNOWN, "live governed native capacity cannot be verified") from None


def governed_occupancy(host: NativeChildHost, workspace: Path) -> Occupancy:
    """Count live governed units across all native hosts under the workspace runs root."""
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
            units, size, trusted = _scan_run(host, run_dir, entry.name, workspace)
        except _Unverified:
            authoritative = False
            continue
        total_bytes += size
        if total_bytes > MAX_TOTAL_JOURNAL_BYTES:
            return Occupancy(0, (), False, "runs-truncated")
        authoritative = authoritative and trusted
        live.update(units)
    return Occupancy(
        sum(live.values()),
        tuple(sorted(live.items())),
        authoritative,
        None if authoritative else "run-unverified",
    )


def assess(host: NativeChildHost, roster: roster_mod.Roster, seat: str, occupancy: Occupancy) -> CapacityDecision:
    """Apply the global and per-seat limits without inventing per-seat allowances."""
    if not occupancy.authoritative:
        return CapacityDecision(CAPACITY_UNKNOWN, None, None, None)
    global_free = roster.max_workers - occupancy.live
    declaration = host.declarations(roster).get(seat) or {}
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
