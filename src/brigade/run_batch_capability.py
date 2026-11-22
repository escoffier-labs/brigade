"""Resolve a batch task's capability to one seat, once, at request acceptance.

Capability selection names a kind of work, not a seat. The resolution reads the
roster in roster order, drops every seat the batch's existing per-task checks
would refuse, and keeps the first survivor. The result is persisted with the
request so a retry or resume reuses it instead of resolving again.

The persisted block sits outside the request fingerprint, so it is read closed:
every row must name a capability task of this request, carry that task's
capability, and agree with the request's recorded seat, and the whole block must
match its digest. A retry then re-checks the recorded seat against the live
roster and refuses rather than resolving again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from . import agents, roster as roster_mod
from .run_batch import BatchError
from .run_task_journal import token as _token

# A task row's resolved selector, reported without any roster or task text.
Selection = dict[str, Any]
# The closed field set of one persisted resolution.
_RESOLUTION_FIELDS = ("task_id", "capability", "seat", "candidates")


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


def _admission(snapshot: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The Fleet Hub snapshot that participates in admission, if any.

    Fleet model-policy admission applies only to an authoritative snapshot; an
    unconfigured or unavailable hub is the existing run admission's refusal to
    report, not a reason to call the capability itself unavailable.
    """
    return snapshot if isinstance(snapshot, Mapping) and snapshot.get("state") == "authoritative" else None


def _request_capabilities(plan: Mapping[str, Any]) -> dict[str, str]:
    """The capability task IDs of this request and the tag each one names."""
    capabilities: dict[str, str] = {}
    for task in plan.get("tasks") or ():
        if isinstance(task, Mapping) and isinstance(task.get("capability"), str):
            capabilities[str(task["task_id"])] = task["capability"]
    return capabilities


def _mismatch(detail: str) -> BatchError:
    return BatchError("capability-marker-mismatch", detail)


def recorded_resolutions(plan: Mapping[str, Any], marker: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the persisted capability resolutions closed, in task order.

    The block is outside the request fingerprint, so an edited or pre-created
    marker could otherwise bind a capability task to any roster seat. Every row
    must be exactly the documented fields, name a capability task of this
    request, carry that task's capability, count its candidates, and agree with
    the seat the request row records. Anything else refuses closed.
    """
    capabilities = _request_capabilities(plan)
    rows = marker.get("capability_resolutions")
    if not isinstance(rows, list):
        raise _mismatch("request reservation has no readable capability resolutions; refusing launch")
    marker_tasks = marker.get("tasks")
    seats = (
        {
            str(row["task_id"]): row.get("seat")
            for row in marker_tasks
            if isinstance(row, Mapping) and isinstance(row.get("task_id"), str)
        }
        if isinstance(marker_tasks, list)
        else {}
    )
    recorded: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != set(_RESOLUTION_FIELDS):
            raise _mismatch("a capability resolution is not a closed row; refusing launch")
        task_id = row["task_id"]
        if not isinstance(task_id, str) or task_id not in capabilities:
            raise _mismatch("a capability resolution names no capability task of this request; refusing launch")
        if task_id in recorded:
            raise _mismatch(f"task {task_id} has more than one recorded capability resolution; refusing launch")
        capability = row["capability"]
        seat = row["seat"]
        candidates = row["candidates"]
        if capability != capabilities[task_id]:
            raise _mismatch(f"task {task_id} records a different capability than the request; refusing launch")
        if not isinstance(seat, str) or not _token(seat):
            raise _mismatch(f"task {task_id} records no valid capability seat; refusing launch")
        if type(candidates) is not int or candidates < 0:
            raise _mismatch(f"task {task_id} capability resolution has no candidate count; refusing launch")
        if seats.get(task_id) != seat:
            raise _mismatch(f"task {task_id} records a different seat than its resolution; refusing launch")
        recorded[task_id] = {field: row[field] for field in _RESOLUTION_FIELDS}
    return [recorded[task_id] for task_id in sorted(recorded)]


def resolution_digest(resolutions: Iterable[Mapping[str, Any]], fingerprint: object) -> str:
    """Hash the canonical resolutions together with the request fingerprint."""
    canonical = [{field: row[field] for field in _RESOLUTION_FIELDS} for row in resolutions]
    canonical.sort(key=lambda row: row["task_id"])
    material = {
        "fingerprint": fingerprint if isinstance(fingerprint, str) else "",
        "resolutions": canonical,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


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
    admission = _admission(snapshot)
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
    return {
        **plan,
        "tasks": tasks,
        "capability_resolutions": resolutions,
        "resolution_digest": resolution_digest(resolutions, plan.get("fingerprint")),
    }


def reuse(
    plan: dict[str, Any],
    marker: Mapping[str, Any],
    roster: roster_mod.Roster,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a retried capability task to the seat its accepted request recorded.

    The journal binds the resolved seat, so a resolution is never recomputed and
    a recorded seat the roster no longer declares refuses the batch. Choosing a
    different seat here would silently change the accepted assignment. The
    recorded seat is re-checked as if it had just been chosen: a seat that
    stopped declaring the capability, or that the batch's per-task checks would
    now refuse, refuses closed instead of resolving again.
    """
    if not has_capability(plan):
        return plan
    resolutions = recorded_resolutions(plan, marker)
    fingerprint = marker.get("fingerprint")
    digest = resolution_digest(resolutions, fingerprint)
    if marker.get("resolution_digest") != digest:
        raise _mismatch("request reservation capability resolutions do not match its digest; refusing launch")
    admission = _admission(snapshot)
    recorded = {row["task_id"]: row for row in resolutions}
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
        agent = roster.agents[seat]
        if not agent.supports(capability):
            raise BatchError(
                "capability-seat-invalid",
                f"task {task['task_id']} capability {capability!r} is recorded on seat {seat!r}, "
                "which no longer declares it; refusing to resolve another seat",
            )
        error = _seat_error(task, agent, roster, read_only=bool(task.get("read_only", False)), snapshot=admission)
        if error is not None:
            raise BatchError(
                "capability-seat-invalid",
                f"task {task['task_id']} recorded seat {seat!r} is no longer eligible: {error}",
            )
        tasks.append({**task, "seat": seat})
    return {**plan, "tasks": tasks, "capability_resolutions": resolutions, "resolution_digest": digest}


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
