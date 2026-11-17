"""Run status classification and externally controlled run gates.

One small home for the nonterminal run statuses and the explicit state whose
next transition belongs to a native parent outside this process. Inspection
surfaces keep their behavior while the frozen ``runs_cmd`` module stays small.
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from . import runguard

EXTERNALLY_CONTROLLED_STATUS = runguard.EXTERNALLY_CONTROLLED_STATUS

NONTERMINAL_STATUSES = frozenset(
    {
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
        "artifact-collection",
        "running",
        EXTERNALLY_CONTROLLED_STATUS,
    }
)


def is_terminal(meta: Mapping[str, Any]) -> bool:
    status = meta.get("status")
    if meta.get("finished_at"):
        return True
    return isinstance(status, str) and status not in NONTERMINAL_STATUSES


def is_externally_controlled(meta: Mapping[str, Any]) -> bool:
    """True when an external parent owns the run's next transition."""
    return meta.get("status") == EXTERNALLY_CONTROLLED_STATUS


def refuse_resume(meta: Mapping[str, Any]) -> bool:
    """Print the resume refusal and return True for an externally controlled run.

    Such a run has no live lock holder between verbs by design, so resume must
    never reconstruct a transition here.
    """
    if not is_externally_controlled(meta):
        return False
    print(
        "error: run is externally controlled by a native T3 parent and cannot be resumed",
        file=sys.stderr,
    )
    return True


def refuse_recovery(meta: Mapping[str, Any]) -> bool:
    """Print the recovery refusal and return True for an externally controlled run.

    Recovery would rewrite the run behind the parent that owns it. A dead lock
    from an interrupted verb is reclaimed by that verb's own claim, not here.
    """
    if not is_externally_controlled(meta):
        return False
    print(
        "error: run is externally controlled by a native T3 parent; no local recovery is required",
        file=sys.stderr,
    )
    return True
