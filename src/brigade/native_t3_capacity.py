"""Workspace-scoped governed native T3 occupancy and capacity.

T3 Code keeps no admission ledger and no child concurrency limit, so Brigade
bounds concurrent governed T3 tasks. The scan, the decision and the lock are
host neutral and live in ``native_child_capacity``. This module binds them to
the T3 host description and keeps every name callers import from here.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import native_child_capacity as core
from . import roster as roster_mod
from .native_child_capacity import (
    CAPACITY_AVAILABLE,
    CAPACITY_DENIED,
    CAPACITY_LOCK_NAME,
    CAPACITY_POLL_SECONDS,
    CAPACITY_UNKNOWN,
    CAPACITY_WAIT_SECONDS,
    HOLD_EVENT,
    HOLD_RESOLVED_EVENT,
    LAUNCH_CLAIM,
    MAX_RUN_ENTRIES,
    MAX_RUN_RECEIPT_BYTES,
    MAX_TOTAL_JOURNAL_BYTES,
    CapacityDecision,
    CapacityError,
    Occupancy,
    runs_root,
)
from .native_t3_host import HOST

NATIVE_RUN_PREFIX = HOST.run_prefix

__all__ = [
    "CAPACITY_AVAILABLE",
    "CAPACITY_DENIED",
    "CAPACITY_LOCK_NAME",
    "CAPACITY_POLL_SECONDS",
    "CAPACITY_UNKNOWN",
    "CAPACITY_WAIT_SECONDS",
    "HOLD_EVENT",
    "HOLD_RESOLVED_EVENT",
    "LAUNCH_CLAIM",
    "MAX_RUN_ENTRIES",
    "MAX_RUN_RECEIPT_BYTES",
    "MAX_TOTAL_JOURNAL_BYTES",
    "NATIVE_RUN_PREFIX",
    "CapacityDecision",
    "CapacityError",
    "Occupancy",
    "assess",
    "capacity_lock",
    "capacity_lock_path",
    "governed_occupancy",
    "runs_root",
]


def capacity_lock_path(workspace: Path) -> Path:
    return core.capacity_lock_path(workspace)


def governed_occupancy(workspace: Path) -> Occupancy:
    """Count live governed native units under the workspace runs root."""
    return core.governed_occupancy(HOST, workspace)


def assess(roster: roster_mod.Roster, seat: str, occupancy: Occupancy) -> CapacityDecision:
    """Apply the global and per-seat limits without inventing per-seat allowances."""
    return core.assess(HOST, roster, seat, occupancy)


@contextmanager
def capacity_lock(workspace: Path, *, wait_seconds: float = CAPACITY_WAIT_SECONDS) -> Iterator[Path]:
    """Hold the one workspace-scoped native capacity lock for the claim."""
    with core.capacity_lock(workspace, wait_seconds=wait_seconds) as path:
        yield path
