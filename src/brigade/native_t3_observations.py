"""Classify supplied native status evidence without native actions or consumption.

Containment is caller-supplied evidence, not proof authenticated by this module.
A terminal candidate still needs Brigade persistence and exact acknowledgement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from . import native_child_observations as core
from .native_child_observations import (
    CONTAINMENT,
    STATUS,
    TERMINAL,
    Containment,
    Family,
    observation_diagnostic,
    valid_reference,
)


_WORK_STATE = ("working", "waiting_for_children", "result_available")
_CONTAINMENT = CONTAINMENT
_TERMINAL = TERMINAL
_STATUS = STATUS
_valid_reference = valid_reference


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
class NativeObservation(core.NativeObservation):
    """The core observation, with this adapter's reference type enforced."""

    def __post_init__(self) -> None:
        if type(self.reference) is not NativeTaskReference:
            raise ValueError("invalid native observation")
        super().__post_init__()


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
    The accepted handle is T3's task node identity for the retained client
    request, not Brigade's batch task id. Child thread and run identities cannot
    stand in for the task handle, and the snapshot task id must equal it exactly.
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


__all__ = [
    "Containment",
    "Family",
    "NativeObservation",
    "NativeTaskReference",
    "classify_native_snapshot",
    "observation_diagnostic",
]
