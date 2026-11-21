"""Host-neutral observation result families for governed native children.

A host adapter classifies its own status evidence and reports the outcome in
these closed families. Containment is caller-supplied evidence, not proof this
module authenticates. A terminal candidate still needs Brigade persistence and
exact acknowledgement.

The adapter supplies the reference type that identifies the native task and
enforces it in its own subclass. The family vocabulary stays closed here so
every host reports the same fixed reason tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

Family = Literal["refused", "awaiting", "active", "terminal-candidate"]
Containment = Literal["unknown", "active", "quiescent"]
CONTAINMENT = ("unknown", "active", "quiescent")
TERMINAL = ("completed", "failed", "cancelled", "interrupted")
STATUS = ("queued", "running", "waiting", *TERMINAL)
# Same bounded provider-reference alphabet as the existing task journal, with
# no dependency on its persistence or event implementation.
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:%~+/@=-]*", re.ASCII)
REASONS = {
    "refused": ("origin-mismatch", "task-mismatch", "remote-mismatch", "circuit-broken"),
    "awaiting": (
        "origin-unknown",
        "acceptance-uncertain",
        "snapshot-unavailable",
        "malformed-snapshot",
        "missing-evidence",
        "pending-work",
        "wait-timeout",
        "work-state-inconsistent",
        "terminal-status-mismatch",
        "terminal-run-missing",
        "result-transfer-missing",
        "result-transfer-mismatch",
        "containment-unconfirmed",
    ),
    "active": ("native-active",),
    "terminal-candidate": ("terminal-evidence-complete",),
}


def valid_reference(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and _REFERENCE.fullmatch(value) is not None


@dataclass(frozen=True)
class NativeObservation:
    family: Family
    reason: str
    reference: Any
    status: str | None
    terminal_run_id: str | None
    result_transfer_id: str | None
    containment: Containment

    def __post_init__(self) -> None:
        if (
            type(self.family) is not str
            or self.family not in REASONS
            or type(self.reason) is not str
            or self.reason not in REASONS[self.family]
            or (self.status is not None and (type(self.status) is not str or self.status not in STATUS))
            or any(
                value is not None and not valid_reference(value)
                for value in (self.terminal_run_id, self.result_transfer_id)
            )
            or type(self.containment) is not str
            or self.containment not in CONTAINMENT
        ):
            raise ValueError("invalid native observation")


def observation_diagnostic(result: NativeObservation) -> dict[str, object]:
    """Expose only fixed classification tokens, never supplied native values."""
    return {"family": result.family, "reason": result.reason}
