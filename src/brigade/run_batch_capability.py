"""Resolve a batch task's capability to one seat, once, at request acceptance.

Capability selection names a kind of work, not a seat. The resolution reads the
roster in roster order, drops every seat the batch's existing per-task checks
would refuse, and keeps the first survivor. The result is persisted with the
request so a retry or resume reuses it instead of resolving again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from . import agents, roster as roster_mod
from .run_batch import BatchError

# A task row's resolved selector, reported without any roster or task text.
Selection = dict[str, Any]


def read_only_enforcement_error(task_id: str, agent: Any, roster: roster_mod.Roster, *, read_only: bool) -> str | None:
    """Refuse a per-task read-only setting the seat cannot hard-enforce.

    The shared-writable layer rule lets a read-only task run beside a writer in
    one checkout, so a soft or prompt-only read-only mode is not enough. A
    read-only roster sandbox likewise cannot carry a writable task.
    """
    if read_only:
        enforcement = agents.read_only_enforcement(agent.cli or "", sandbox=roster.sandbox, transport=agent.transport)
        if enforcement != "hard":
            return (
                f"task {task_id} is read_only but seat {agent.name!r} enforces read-only as {enforcement!r} "
                f"under sandbox {roster.sandbox!r}; use isolation worktree, depends_on, or a hard read-only seat"
            )
    elif roster.sandbox == "read-only":
        return f"task {task_id} is writable but the roster sandbox is read-only"
    return None


def _seat_error(
    task: Mapping[str, Any],
    agent: roster_mod.Agent,
    roster: roster_mod.Roster,
    *,
    read_only: bool,
    snapshot: Mapping[str, Any] | None,
) -> str | None:
    """The batch's existing per-task refusal for one candidate seat, if any."""
    from .aboyeur import artifacts, orchestrator

    error = artifacts._direct_worker_error(agent.name, roster, read_only=read_only)
    if error is None and read_only:
        error = roster_mod.read_only_cloud_mutation_error(agent)
    if error is None:
        error = read_only_enforcement_error(str(task["task_id"]), agent, roster, read_only=read_only)
    if error is None and snapshot is not None:
        # The same admission the batch applies to a named seat, for one candidate.
        error = orchestrator.resolve_fleet_model_policy(roster, worker=agent.name, snapshot=snapshot).error
    return error


def selection(task: Mapping[str, Any]) -> Selection:
    """The bounded selector a result reports for one task."""
    capability = task.get("capability")
    if isinstance(capability, str):
        return {"kind": "capability", "capability": capability}
    return {"kind": "seat", "capability": None}


def has_capability(plan: Mapping[str, Any]) -> bool:
    return any(isinstance(task.get("capability"), str) for task in plan.get("tasks") or ())


def resolve(
    plan: dict[str, Any],
    roster: roster_mod.Roster,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the plan with every capability task bound to its first eligible seat.

    Candidates are roster seats declaring the capability, in roster order. A
    candidate is dropped when the batch's existing per-task checks would refuse
    it: the direct-worker rule, read-only enforcement or a mutating cloud seat,
    and Fleet Hub model-policy admission when a policy snapshot applies. The
    refusal names the task and the tag, never the task text.
    """
    if not has_capability(plan):
        return plan
    # Fleet model-policy admission applies only to an authoritative snapshot;
    # an unconfigured or unavailable hub is the existing run admission's refusal
    # to report, not a reason to call the capability itself unavailable.
    admission = snapshot if isinstance(snapshot, Mapping) and snapshot.get("state") == "authoritative" else None
    tasks = []
    resolutions = []
    for task in plan.get("tasks") or ():
        capability = task.get("capability")
        if not isinstance(capability, str):
            tasks.append(task)
            continue
        read_only = bool(task.get("read_only", False))
        candidates = roster.find_capability(capability)
        chosen = next(
            (
                agent
                for agent in candidates
                if _seat_error(task, agent, roster, read_only=read_only, snapshot=admission) is None
            ),
            None,
        )
        if chosen is None:
            raise BatchError(
                "capability-unavailable",
                f"task {task['task_id']} capability {capability!r} has no eligible seat",
            )
        tasks.append({**task, "seat": chosen.name})
        resolutions.append(
            {
                "task_id": task["task_id"],
                "capability": capability,
                "seat": chosen.name,
                "candidates": len(candidates),
            }
        )
    return {**plan, "tasks": tasks, "capability_resolutions": resolutions}


def reuse(
    plan: dict[str, Any],
    marker: Mapping[str, Any] | None,
    roster: roster_mod.Roster,
) -> dict[str, Any]:
    """Bind a retried capability task to the seat its accepted request recorded.

    The journal binds the resolved seat, so a resolution is never recomputed and
    a recorded seat the roster no longer declares refuses the batch. Choosing a
    different seat here would silently change the accepted assignment.
    """
    if not has_capability(plan):
        return plan
    recorded = {
        row.get("task_id"): row
        for row in (marker or {}).get("capability_resolutions") or ()
        if isinstance(row, Mapping) and isinstance(row.get("seat"), str)
    }
    tasks = []
    for task in plan.get("tasks") or ():
        capability = task.get("capability")
        if not isinstance(capability, str):
            tasks.append(task)
            continue
        row = recorded.get(task.get("task_id"))
        if row is None or row.get("capability") != capability:
            raise BatchError(
                "request-unknown",
                f"task {task['task_id']} has no recorded capability resolution; refusing launch",
            )
        seat = row["seat"]
        if seat not in roster.agents:
            raise BatchError(
                "capability-seat-missing",
                f"task {task['task_id']} capability {capability!r} resolved to seat {seat!r}, "
                "which the roster no longer declares",
            )
        tasks.append({**task, "seat": seat})
    return {**plan, "tasks": tasks, "capability_resolutions": list(recorded.values())}


def remaining_capacity(roster: roster_mod.Roster) -> dict[str, int]:
    """Per-seat remaining Hub capacity, or nothing when the fleet is not enrolled.

    This is a scheduling hint for wave construction, never an admission: only an
    enrolled fleet with an answering Hub contributes, and the launch-time lease
    remains the final check. Any refusal, malformed payload, or unknown seat is
    simply absent from the answer.
    """
    from . import fleet_client, fleet_client_policy, fleet_policy, fleet_session_bootstrap

    try:
        snapshot = fleet_client.load_model_policy_snapshot()
        enrollment = fleet_session_bootstrap.classify_enrollment(
            snapshot, lock_timeout=fleet_client.CLOUD_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 - capacity is advisory; launch admission decides
        return {}
    if enrollment != "enrolled":
        return {}
    try:
        document = (fleet_client_policy.show_policy() or {}).get("document")
        if not isinstance(document, Mapping):
            return {}
        limits = fleet_policy.parse_document(document).get("seats") or {}
        usage = fleet_client_policy.read_seat_usage()
    except Exception:  # noqa: BLE001 - capacity is advisory; launch admission decides
        return {}
    seats = usage.get("seats") if isinstance(usage, Mapping) else None
    if not isinstance(seats, Mapping) or not isinstance(limits, Mapping):
        return {}
    remaining: dict[str, int] = {}
    for name in roster.agents:
        limit = limits.get(name)
        limit = limit.get("concurrency") if isinstance(limit, Mapping) else None
        occupied = seats.get(name)
        occupied = occupied.get("occupied") if isinstance(occupied, Mapping) else None
        if type(limit) is not int or type(occupied) is not int or occupied < 0:
            continue
        # A zero remaining capacity adds no scheduling cap: it could only build
        # waves that never admit the task, so the launch-time lease refuses it.
        if limit - occupied > 0:
            remaining[name] = limit - occupied
    return remaining


def build_waves(
    assignments: Iterable[Any],
    max_workers: int,
    capacity: Mapping[str, int] | None = None,
) -> list[Any]:
    """Group assignments into ordered waves, bounded by workers and seat capacity.

    Order inside a layer is preserved. A task that would exceed either bound on
    the current wave opens the next one, so it stays in the same dependency
    layer and only its stage moves.
    """
    capacity = capacity or {}
    members = list(assignments)
    waves: list[Any] = []
    stage = 0
    for layer in sorted({assignment.stage for assignment in members}):
        current: list[Any] = []
        seats: dict[str, int] = {}
        for assignment in (a for a in members if a.stage == layer):
            limit = capacity.get(assignment.worker)
            if current and (
                len(current) >= max_workers or (limit is not None and seats.get(assignment.worker, 0) >= limit)
            ):
                waves.extend(replace(closed, stage=stage) for closed in current)
                stage += 1
                current, seats = [], {}
            current.append(assignment)
            seats[assignment.worker] = seats.get(assignment.worker, 0) + 1
        if current:
            waves.extend(replace(closed, stage=stage) for closed in current)
            stage += 1
    return waves
