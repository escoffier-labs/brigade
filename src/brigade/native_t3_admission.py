"""Seat admission for one governed native T3 claim.

Brigade is the admission authority for governed T3, because T3 Code keeps no
admission ledger. The predicates are host neutral and live in
``native_child_admission``. This module binds them to the T3 host description,
keeps the structural tokens `t3 open` also reports, and keeps every name callers
import from here.

A retained target, a matched declaration or a readable policy grant nothing on
their own. Only a granted claim consumes the predicates, and only under the
capacity and run locks held by ``native_t3_dispatch.claim``.
"""

from __future__ import annotations

from pathlib import Path

from . import native_child_admission as core
from . import native_t3_enrollment as enrollment
from . import native_t3_structural as structural
from . import roster as roster_mod
from .native_child_admission import (
    ADAPTER_NOT_ALLOWED,
    ADMITTED,
    DENIED,
    DRIVER_MISMATCH,
    DRIVER_UNSUPPORTED,
    MODEL_RETIRED,
    POLICY_CONSUMER_MISSING,
    POLICY_DENIED,
    POLICY_UNAVAILABLE,
    SEAT_ORCHESTRATOR,
    SEAT_UNKNOWN,
    TARGET_CHANGED,
    TARGET_INVALID,
    TARGET_MISSING,
    UNAVAILABLE,
    Admission,
    HubAuthority,
)
from .native_t3_contract import NativeTarget
from .native_t3_host import HOST

# The three structural tokens are shared with the closed classifier, so a
# claim can report exactly what `t3 open` refuses.
TARGET_MODE_INVALID = structural.TARGET_MODE_INVALID
TARGET_REMOTE_UNSUPPORTED = structural.TARGET_REMOTE_UNSUPPORTED
STRUCTURAL_REASONS = structural.STRUCTURAL_GAPS

__all__ = [
    "ADAPTER_NOT_ALLOWED",
    "ADMITTED",
    "DENIED",
    "DRIVER_MISMATCH",
    "DRIVER_UNSUPPORTED",
    "MODEL_RETIRED",
    "POLICY_CONSUMER_MISSING",
    "POLICY_DENIED",
    "POLICY_UNAVAILABLE",
    "SEAT_ORCHESTRATOR",
    "SEAT_UNKNOWN",
    "STRUCTURAL_REASONS",
    "TARGET_CHANGED",
    "TARGET_INVALID",
    "TARGET_MISSING",
    "TARGET_MODE_INVALID",
    "TARGET_REMOTE_UNSUPPORTED",
    "UNAVAILABLE",
    "Admission",
    "HubAuthority",
    "evaluate",
    "load_roster",
    "read_hub_authority",
    "roster_digest",
]


def roster_digest(roster: roster_mod.Roster) -> str:
    """Digest the admission-relevant roster declaration, never its text."""
    return core.roster_digest(HOST, roster)


def evaluate(
    roster: roster_mod.Roster,
    seat: str,
    retained: NativeTarget,
    *,
    hub: HubAuthority,
) -> Admission:
    """Decide whether one seat may claim, without side effects."""
    return core.evaluate(HOST, roster, seat, retained, hub=hub)


def load_roster(workspace: Path) -> roster_mod.Roster:
    """Load the roster that currently applies to the workspace."""
    return enrollment._load_roster(workspace, None)


def read_hub_authority(workspace: Path) -> HubAuthority:
    """Read live Hub policy authority, or a fixed reason it is not usable."""
    return core.read_hub_authority(workspace)
