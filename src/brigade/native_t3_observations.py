"""Classify supplied native status evidence without native actions or consumption.

Containment is caller-supplied evidence, not proof authenticated by this module.
A terminal candidate still needs Brigade persistence and exact acknowledgement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Literal, cast


Family = Literal["refused", "awaiting", "active", "terminal-candidate"]
Containment = Literal["unknown", "active", "quiescent"]
_CONTAINMENT = ("unknown", "active", "quiescent")
_TERMINAL = ("completed", "failed", "cancelled", "interrupted")
_STATUS = ("queued", "running", "waiting", *_TERMINAL)
_WORK_STATE = ("working", "waiting_for_children", "result_available")
# Same bounded provider-reference alphabet as the existing task journal, with
# no dependency on its persistence or event implementation.
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:%~+/@=-]*", re.ASCII)
_REASONS = {
    "refused": ("origin-mismatch", "task-mismatch", "remote-mismatch"),
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


def _valid_reference(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and _REFERENCE.fullmatch(value) is not None


@dataclass(frozen=True)
class NativeTaskReference:
    origin_environment_id: str
    native_client_request_key: str
    task_id: str | None = None
    remote_environment_id: str | None = None
    remote_thread_id: str | None = None

    def __post_init__(self) -> None:
        required = (self.origin_environment_id, self.native_client_request_key)
        optional = (self.task_id, self.remote_environment_id, self.remote_thread_id)
        if (
            not all(_valid_reference(value) for value in required)
            or any(value is not None and not _valid_reference(value) for value in optional)
            or (self.remote_environment_id is None) != (self.remote_thread_id is None)
            or (self.remote_environment_id is not None and self.task_id is None)
        ):
            raise ValueError("invalid native task reference")


@dataclass(frozen=True)
class NativeObservation:
    family: Family
    reason: str
    reference: NativeTaskReference
    status: str | None
    terminal_run_id: str | None
    result_transfer_id: str | None
    containment: Containment

    def __post_init__(self) -> None:
        if (
            type(self.family) is not str
            or self.family not in _REASONS
            or type(self.reason) is not str
            or self.reason not in _REASONS[self.family]
            or type(self.reference) is not NativeTaskReference
            or (self.status is not None and (type(self.status) is not str or self.status not in _STATUS))
            or any(
                value is not None and not _valid_reference(value)
                for value in (self.terminal_run_id, self.result_transfer_id)
            )
            or type(self.containment) is not str
            or self.containment not in _CONTAINMENT
        ):
            raise ValueError("invalid native observation")


def _well_formed(snapshot: Mapping[str, object]) -> bool:
    """Check lifecycle fields after identity validation and correlation."""
    for key, allowed in (("status", _STATUS), ("workState", _WORK_STATE), ("latestTerminalStatus", _TERMINAL)):
        if key in snapshot:
            value = snapshot[key]
            if key == "latestTerminalStatus" and value is None:
                continue
            if type(value) is not str or value not in allowed:
                return False
    for key in ("latestTerminalRunId", "latestTerminalResultContextTransferId", "resultContextTransferId"):
        if key in snapshot:
            value = snapshot[key]
            if value is None:
                continue
            if not _valid_reference(value):
                return False
    for key in ("hasPendingChildRuns", "hasPendingQuestion", "waitTimedOut"):
        if key in snapshot and type(snapshot[key]) is not bool:
            return False
    approval = snapshot.get("pendingApproval")
    if approval is not None:
        if not isinstance(approval, Mapping):
            return False
        kind = approval.get("kind")
        if (
            type(kind) is not str
            or kind not in ("command", "file-read", "file-change", "mcp-elicitation", "permission")
            or not _valid_reference(approval.get("requestId"))
            or not _valid_reference(approval.get("runId"))
        ):
            return False
    return True


def classify_native_snapshot(
    expected: NativeTaskReference,
    snapshot: Mapping[str, object] | None,
    *,
    observed_origin_environment_id: str | None,
    containment: Containment = "unknown",
) -> NativeObservation:
    """Retain uncertainty unless all required terminal evidence is supplied.

    A missing accepted task handle never adopts identity from a status snapshot.
    Child thread/run identities cannot stand in for task/terminal identities.
    """
    if type(expected) is not NativeTaskReference:
        raise ValueError("invalid native task reference")
    if type(containment) is not str or containment not in _CONTAINMENT:
        raise ValueError("invalid containment")

    def result(
        family: Family,
        reason: str,
        status: str | None = None,
        terminal_run_id: str | None = None,
        result_transfer_id: str | None = None,
    ) -> NativeObservation:
        return NativeObservation(family, reason, expected, status, terminal_run_id, result_transfer_id, containment)

    if observed_origin_environment_id is None:
        return result("awaiting", "origin-unknown")
    if (
        not _valid_reference(observed_origin_environment_id)
        or observed_origin_environment_id != expected.origin_environment_id
    ):
        return result("refused", "origin-mismatch")
    if expected.task_id is None:
        return result("awaiting", "acceptance-uncertain")
    if snapshot is None:
        return result("awaiting", "snapshot-unavailable")
    if not isinstance(snapshot, Mapping):
        return result("awaiting", "malformed-snapshot")
    if "taskId" not in snapshot:
        return result("awaiting", "missing-evidence")
    if not _valid_reference(snapshot["taskId"]):
        return result("awaiting", "malformed-snapshot")
    if snapshot["taskId"] != expected.task_id:
        return result("refused", "task-mismatch")
    remote = snapshot.get("remoteChild")
    if remote is not None and (
        not isinstance(remote, Mapping)
        or not _valid_reference(remote.get("environmentId"))
        or not _valid_reference(remote.get("threadId"))
    ):
        return result("awaiting", "malformed-snapshot")
    observed_remote = (
        (remote.get("environmentId"), remote.get("threadId")) if isinstance(remote, Mapping) else (None, None)
    )
    if observed_remote != (expected.remote_environment_id, expected.remote_thread_id):
        return result("refused", "remote-mismatch")
    if isinstance(remote, Mapping) and type(remote.get("label")) is not str:
        return result("awaiting", "malformed-snapshot")
    if not _well_formed(snapshot):
        return result("awaiting", "malformed-snapshot")

    required = ("status", "workState", "hasPendingChildRuns", "hasPendingQuestion", "pendingApproval", "waitTimedOut")
    if any(key not in snapshot for key in required):
        return result("awaiting", "missing-evidence")
    status = cast(str, snapshot["status"])
    work_state = snapshot["workState"]
    if snapshot["waitTimedOut"]:
        return result("awaiting", "wait-timeout", status)
    if (
        snapshot["hasPendingChildRuns"]
        or snapshot["hasPendingQuestion"]
        or snapshot["pendingApproval"] is not None
        or work_state == "waiting_for_children"
        or status == "waiting"
    ):
        return result("awaiting", "pending-work", status)
    if status in ("queued", "running") and work_state == "working":
        return result("active", "native-active", status)
    if status not in _TERMINAL or work_state != "result_available":
        return result("awaiting", "work-state-inconsistent", status)
    if snapshot.get("latestTerminalStatus") != status:
        return result("awaiting", "terminal-status-mismatch", status)
    terminal_run = cast(str | None, snapshot.get("latestTerminalRunId"))
    if terminal_run is None:
        return result("awaiting", "terminal-run-missing", status)
    latest_transfer = cast(str | None, snapshot.get("latestTerminalResultContextTransferId"))
    transfer = cast(str | None, snapshot.get("resultContextTransferId"))
    if latest_transfer is not None and transfer is not None and latest_transfer != transfer:
        return result("awaiting", "result-transfer-mismatch", status)
    transfer = latest_transfer if latest_transfer is not None else transfer
    if transfer is None:
        return result("awaiting", "result-transfer-missing", status)
    if containment != "quiescent":
        return result("awaiting", "containment-unconfirmed", status)
    return result("terminal-candidate", "terminal-evidence-complete", status, terminal_run, transfer)


def observation_diagnostic(result: NativeObservation) -> dict[str, object]:
    """Expose only fixed classification tokens, never supplied native values."""
    return {"family": result.family, "reason": result.reason}
