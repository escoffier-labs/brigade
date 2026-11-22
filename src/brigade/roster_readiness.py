"""Bounded operation readiness, distinct from structural roster validation.

Health is observation, never admission. External operation observations must be
fresh and bound to the exact request; this module never acquires a lease,
creates a model task, applies a fallback, or changes configuration.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field, replace
from numbers import Real
from pathlib import Path
from typing import Any, Mapping

from . import roster as roster_mod
from . import runguard, seat_health
from .worker_failure import safe_detail

SCHEMA = "brigade.roster_readiness.v1"
OPERATION_TTL_SECONDS = 30.0
SOURCE_BRIGADE_GOVERNANCE = "brigade-governance"
SOURCE_PARENT_REPORTED = "parent-reported"
SOURCE_ORCA_LOCAL_PROBE = "orca-local-probe"
ORCA_CHECKS = ("orca-binary", "orca-version", "orca-retry-request", "orca-agent")
ORCA_QUALIFIERS = {
    "assurance": "local-probe",
    "launch_guaranteed": False,
    "capacity_reserved": False,
    "parent_active": "unverified",
}
# Route t3 never proves that the parent is active, that a launch happened, or
# that capacity is reserved. The evaluator attaches these qualifiers and refuses
# ready when any of them is missing.
T3_QUALIFIERS = {
    "assurance": SOURCE_PARENT_REPORTED,
    "launch_guaranteed": False,
    "capacity_reserved": False,
    "parent_active": "unverified",
}


@dataclass(frozen=True)
class ReadinessRequest:
    workers: tuple[str, ...]
    concurrency: int = 1
    route: str = "brigade"
    isolation: str = "shared"
    capability: str | None = None
    read_only: bool | None = None


@dataclass(frozen=True)
class Observation:
    """An operation-scoped fact from an authoritative integration, not policy.

    ``observed_at`` is Unix time; ``scope`` comes from operation_scope.
    Status is passed, failed, denied, unsupported, or unknown. Capacity is
    required only for a passed capacity observation. ``source`` is required:
    T3 route support may be ``parent-reported``, Orca checks use
    ``orca-local-probe``, and isolation, admission and capacity must be
    ``brigade-governance``. No admission is reserved.
    """

    status: str
    observed_at: float
    scope: str
    detail: str = ""
    capacity: int | None = None
    seat: str | None = None
    reason: str = ""
    source: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if type(self.source) is not str or self.source not in {
            SOURCE_BRIGADE_GOVERNANCE,
            SOURCE_PARENT_REPORTED,
            SOURCE_ORCA_LOCAL_PROBE,
        }:
            raise ValueError("invalid observation source")
        if self.seat is not None and (type(self.seat) is not str or not self.seat):
            raise ValueError("invalid observation seat")
        if self.reason and (type(self.reason) is not str or re.fullmatch(r"[a-z][a-z0-9-]*", self.reason) is None):
            raise ValueError("invalid observation reason")


def _finite_real(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _health_result_valid(result: Any) -> bool:
    return (
        isinstance(result, seat_health.SeatHealthResult)
        and isinstance(result.seat, str)
        and isinstance(result.fingerprint, str)
        and isinstance(result.status, str)
        and result.status in {"healthy", "degraded", "unhealthy"}
        and _finite_real(result.finished_wall_at)
        and (result.cache_age_seconds is None or _finite_real(result.cache_age_seconds))
        and isinstance(result.checks, (tuple, list))
    )


def _health_check_valid(check: Any) -> bool:
    return (
        isinstance(check, seat_health.SeatHealthCheck)
        and isinstance(check.name, str)
        and isinstance(check.status, str)
        and isinstance(check.detail, str)
        and (check.cause_code is None or isinstance(check.cause_code, str))
    )


def operation_scope(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    workspace: Path,
    *,
    t3_context: Mapping[str, Any] | None = None,
) -> str:
    error = _request_error(request)
    if error:
        raise ValueError(error)
    request = replace(request, workers=tuple(sorted(set(request.workers))))
    inputs: dict[str, Any] = {
        "request": asdict(request),
        "workspace": str(workspace.expanduser().resolve()),
        "orchestrator": roster.orchestrator,
        "max_workers": roster.max_workers,
        "allow_models": roster.allow_models,
        "seats": {
            name: {
                "fingerprint": seat_health.seat_fingerprint(roster.agents[name], roster),
                "capabilities": roster.agents[name].effective_capabilities(),
                "role": roster.agents[name].role,
            }
            for name in request.workers
            if name in roster.agents
        },
    }
    if request.route == "t3":
        from .roster_readiness_t3 import scope_projection

        material = (
            dict(t3_context) if t3_context is not None else scope_projection(roster, request, workspace, report=None)
        )
        material.pop("received_at", None)
        inputs["t3"] = material
    elif request.route == "orca":
        from .roster_readiness_orca import target_scope

        inputs["orca"] = target_scope(roster, request)
    return "sha256:" + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


# A fresh degraded health check is not always "unknown". Two checks have no
# subject for some adapters or operations, so readiness counts them as passed
# for this operation while keeping the original detail:
# - version-gates: only an adapter that seat_health reports as having no
#   reviewed gate (``no-reviewed-gate``) has no gate to satisfy. ACPX transport
#   version and antigravity have declared gates, so a generic incomplete probe
#   for those adapters stays unknown.
# - isolation-compatibility: the health check tests read-only enforcement. Only
#   an operation that explicitly declares ``read_only=False`` does not depend on
#   it; a writable ACPX plus shared isolation combination is still blocked per
#   seat. A roster sandbox never decides operation writability.
# Everything else that could apply but was not verified, including missing
# authentication evidence and an adapter without prompt-free model inventory,
# stays unknown. A failed check never becomes passed.
def _not_applicable_reason(check: seat_health.SeatHealthCheck, agent: Any, request: ReadinessRequest) -> str | None:
    if check.status != "degraded":
        return None
    if check.name == "version-gates":
        if check.cause_code != "no-reviewed-gate":
            return None
        if agent.transport == "acpx" or agent.cli == "antigravity":
            return None
        return "adapter declares no reviewed version gate"
    if check.name == "isolation-compatibility":
        if request.read_only is not False or check.cause_code != "probe-incomplete":
            return None
        return "health check tests read-only enforcement; this operation is writable"
    return None


def _request_error(request: ReadinessRequest) -> str | None:
    if (
        not isinstance(request.workers, (tuple, list))
        or not request.workers
        or any(not isinstance(name, str) or not name.strip() for name in request.workers)
    ):
        return "Select at least one named worker with --worker."
    if type(request.concurrency) is not int or request.concurrency < 1:
        return "Concurrency must be a positive integer."
    if not isinstance(request.route, str) or request.route not in {"brigade", "t3", "orca"}:
        return "Route must be brigade, t3, or orca."
    if not isinstance(request.isolation, str) or request.isolation not in {"shared", "worktree"}:
        return "Isolation must be shared or worktree."
    if request.capability is not None and (
        not isinstance(request.capability, str)
        or not re.fullmatch(r"[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+", request.capability)
    ):
        return "Capability must be a dotted capability name, such as research.plan."
    if request.read_only is not None and not isinstance(request.read_only, bool):
        return "Read-only must be true, false, or unset."
    return None


def evaluate_readiness(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    health: Mapping[str, seat_health.SeatHealthResult] | None = None,
    observations: Mapping[str, Observation | tuple[Observation, ...] | list[Observation]] | None = None,
    now: float | None = None,
    hub_discovery_possible: bool = False,
    t3_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate facts supplied by selected-seat health and route integrations.

    ``inspect_readiness`` produces live read-only ``admission``, ``capacity``,
    and ``isolation`` observations for ``route == "brigade"``,
    parent-reported route support plus Brigade governance for ``route == "t3"``,
    and local Orca observations for ``route == "orca"``. Embedding callers may
    still inject explicit observations.
    """
    now = time.time() if now is None else now
    evidence: list[dict[str, Any]] = []
    health = health or {}
    observations = observations or {}

    def add(
        category: str, status: str, reason: str, action: str = "", *, source: str = "roster", **fields: Any
    ) -> None:
        evidence.append(
            {"category": category, "status": status, "reason": reason, "action": action, "source": source, **fields}
        )

    error = _request_error(request)
    if error:
        add("request", "failed", "malformed-request", error, freshness="current")
        # Malformed programmatic values must never reach hashing, selection,
        # capability checks, or providers, and may not be JSON serializable.
        malformed = {
            "schema": SCHEMA,
            "status": "blocked",
            "exit_code": 2,
            "observed_at": now,
            "scope": None,
            "request": None,
            "roster": {
                "source": roster.resolution.source if roster.resolution else "provided",
                "path": str(roster.resolution.path) if roster.resolution else None,
                "discovery": "local-cache-only",
                "local_authoritative": not hub_discovery_possible,
            },
            "selected_seats": [],
            "structural_worker_count": len(roster_mod.workers(roster)),
            "usable_worker_count": 0,
            "configured_limit": roster.max_workers,
            "admitted_capacity": None,
            "limiting_reason": "malformed-request",
            "fallback": {"automatic": False, "admitted": False, "action": None},
            "evidence": evidence,
        }
        if getattr(request, "route", None) == "t3":
            malformed.update(T3_QUALIFIERS)
        elif getattr(request, "route", None) == "orca":
            malformed.update(ORCA_QUALIFIERS)
        return malformed
    request = replace(request, workers=tuple(dict.fromkeys(request.workers)))
    if request.concurrency > roster.max_workers:
        add(
            "configured-limit",
            "failed",
            "configured-capacity",
            "Reduce concurrency to the configured worker limit.",
            freshness="current",
        )

    selected = []
    for name in request.workers:
        agent = roster.agents.get(name)
        selected.append(
            {
                "name": name,
                "role": agent.role if agent else None,
                "selection_role": "orchestrator"
                if name == roster.orchestrator
                else "worker"
                if agent
                else "unresolved"
                if hub_discovery_possible
                else "missing",
                "cli": agent.cli if agent else None,
                "model": agent.model if agent else None,
                "transport": agent.transport if agent else None,
            }
        )
        if agent is None:
            add(
                "selection",
                "unknown" if hub_discovery_possible else "failed",
                "worker-discovery-unverified" if hub_discovery_possible else "worker-missing",
                "The seat is absent from the local roster; configured Hub discovery was not queried."
                if hub_discovery_possible
                else "Select a worker present in the local roster.",
                seat=name,
                freshness="current",
            )
            continue
        if name == roster.orchestrator:
            add(
                "selection",
                "failed",
                "orchestrator-selected",
                "Select a non-orchestrator worker seat.",
                seat=name,
                freshness="current",
            )
            continue
        if agent.cli is None or not roster_mod.is_cli_allowed(agent.cli, roster):
            add(
                "selection",
                "failed",
                "worker-adapter-unavailable",
                "Select a worker with an allowed CLI adapter.",
                seat=name,
                freshness="current",
            )
        if request.capability is not None and not agent.supports(request.capability):
            add(
                "capability",
                "unsupported",
                "capability-unsupported",
                "Select a worker declaring the required capability.",
                seat=name,
                freshness="current",
            )
        if request.route == "brigade" and request.isolation == "shared" and agent.transport == "acpx":
            add(
                "isolation",
                "failed",
                "isolation-incompatible",
                "Writable ACPX workers require worktree isolation; review the requested operation.",
                seat=name,
                freshness="current",
            )

        if request.route == "brigade":
            result = health.get(name)
            if not _health_result_valid(result):
                result = None
            observed_at = None
            if result is not None:
                cache_age = result.cache_age_seconds if result.cache_age_seconds is not None else 0.0
                if (
                    math.isfinite(result.finished_wall_at)
                    and result.finished_wall_at <= now
                    and math.isfinite(cache_age)
                    and cache_age >= 0
                ):
                    retained_at = result.finished_wall_at - cache_age
                    if math.isfinite(retained_at):
                        observed_at = retained_at
            age = None if observed_at is None else now - observed_at
            ttl = (
                seat_health.HEALTHY_CACHE_TTL_SECONDS
                if result and result.status == "healthy"
                else seat_health.UNHEALTHY_CACHE_TTL_SECONDS
            )
            fresh = (
                result is not None
                and result.seat == name
                and age is not None
                and math.isfinite(age)
                and 0 <= age <= ttl
                and result.fingerprint == seat_health.seat_fingerprint(agent, roster)
            )
            by_name = {check.name: check for check in result.checks if _health_check_valid(check)} if result else {}
            # Read-only enforcement is a separate health fact from the requested
            # shared/worktree isolation. Do not confuse the two contracts.
            for check_name in seat_health.CHECK_NAMES:
                check = by_name.get(check_name)
                not_applicable = _not_applicable_reason(check, agent, request) if fresh and check is not None else None
                if not fresh or check is None:
                    status, reason, detail = "unknown", "health-unobserved", ""
                elif not_applicable is not None:
                    status, reason = "passed", "not-applicable"
                    detail = safe_detail(f"not-applicable: {not_applicable}; {check.detail}")[:240]
                else:
                    status = {"passed": "passed", "failed": "failed"}.get(check.status, "unknown")
                    reason = check.cause_code or check.status
                    detail = safe_detail(check.detail)[:240]
                add(
                    check_name,
                    status,
                    reason,
                    "Refresh selected-seat health evidence."
                    if status == "unknown"
                    else "Inspect the selected seat health failure."
                    if status == "failed"
                    else "",
                    source="seat-health",
                    seat=name,
                    freshness="fresh" if fresh else "missing" if result is None else "stale",
                    age_seconds=age if age is not None and math.isfinite(age) else None,
                    ttl_seconds=ttl,
                    observed_at=observed_at,
                    detail=detail,
                )

    scope = operation_scope(roster, request, workspace, t3_context=t3_context)
    admitted_capacities: list[int] = []
    isolation_incompatible = any(item["category"] == "isolation" and item["status"] == "failed" for item in evidence)
    categories = ORCA_CHECKS if request.route == "orca" else ("route-support", "isolation", "admission", "capacity")
    expected_source = SOURCE_ORCA_LOCAL_PROBE if request.route == "orca" else SOURCE_BRIGADE_GOVERNANCE
    for category in categories:
        raw = observations.get(category)
        if raw is None:
            if request.route == "orca":
                skipped = any(item["status"] != "passed" for item in evidence)
                for seat in request.workers if category == "orca-agent" else (None,):
                    add(
                        category,
                        "skipped" if skipped else "unknown",
                        "orca-prerequisite-not-passed" if skipped else "orca-probe-unobserved",
                        "" if skipped else "Obtain fresh Orca route evidence.",
                        source=SOURCE_ORCA_LOCAL_PROBE,
                        freshness="missing",
                        **({"seat": seat} if seat is not None else {}),
                    )
            elif category == "route-support" and request.route == "brigade":
                add(category, "passed", "brigade-route-declared", freshness="current", source=SOURCE_BRIGADE_GOVERNANCE)
            elif category == "isolation" and request.route == "brigade" and request.isolation == "shared":
                add(
                    category,
                    "failed" if isolation_incompatible else "passed",
                    "isolation-incompatible" if isolation_incompatible else "shared-isolation-requested",
                    "Select workers compatible with the requested isolation." if isolation_incompatible else "",
                    freshness="current",
                    source=SOURCE_BRIGADE_GOVERNANCE,
                )
            else:
                reason = "t3-caller-context-unavailable" if category == "route-support" else "observation-missing"
                action = (
                    "The parent session must report its own orchestrator capabilities, because Brigade has no caller "
                    "context. Brigade fallback requires separate admission and an explicit route request. A task that "
                    "`brigade t3 open` refuses as t3-unsupported goes to a separate `brigade run batch` request with "
                    "its own admission."
                    if reason == "t3-caller-context-unavailable"
                    else f"Obtain fresh operation-scoped {category} evidence from the execution route."
                )
                add(category, "unknown", reason, action, freshness="missing", source=SOURCE_BRIGADE_GOVERNANCE)
            continue
        items = tuple(raw) if isinstance(raw, (tuple, list)) else (raw,)
        if not items:
            add(
                category,
                "unknown",
                "observation-malformed",
                "Obtain well-formed evidence for this exact operation.",
                freshness="missing",
                source="unknown",
            )
            continue
        for observation in items:
            if (
                not isinstance(observation, Observation)
                or not isinstance(observation.status, str)
                or not _finite_real(observation.observed_at)
                or not isinstance(observation.scope, str)
                or not isinstance(observation.detail, str)
                or (
                    observation.source != expected_source
                    and not (category == "route-support" and observation.source == SOURCE_PARENT_REPORTED)
                )
            ):
                add(
                    category,
                    "unknown",
                    "observation-malformed",
                    "Obtain well-formed evidence for this exact operation.",
                    freshness="missing",
                    source="unknown",
                )
                continue
            age = now - observation.observed_at
            fresh = math.isfinite(age) and 0 <= age <= OPERATION_TTL_SECONDS and observation.scope == scope
            status = (
                observation.status
                if fresh and observation.status in {"passed", "failed", "denied", "unsupported"}
                else "unknown"
            )
            if not fresh:
                reason = "observation-stale-or-scope-mismatch"
            elif status in {"failed", "denied", "unsupported"}:
                reason = observation.reason or f"{category}-{status}"
            elif status == "unknown":
                reason = observation.reason or "observation-incomplete"
            else:
                reason = observation.reason or "observed"
            isolation_refused = category == "isolation" and status == "passed" and isolation_incompatible
            if isolation_refused:
                status, reason = "failed", "isolation-incompatible"
            if category == "capacity" and status == "passed":
                if type(observation.capacity) is not int or observation.capacity < 0:
                    status, reason = "unknown", "capacity-unobserved"
                else:
                    admitted_capacities.append(observation.capacity)
                    if observation.capacity < request.concurrency:
                        status, reason = "failed", "insufficient-capacity"
            add(
                category,
                status,
                reason,
                "Wait for admitted capacity or reduce concurrency."
                if reason == "insufficient-capacity"
                else "Obtain fresh evidence for this exact operation."
                if status == "unknown"
                else "Resolve the route refusal; no automatic fallback is authorized."
                if status != "passed"
                else "",
                freshness="fresh" if fresh else "stale",
                source=observation.source,
                **({"seat": observation.seat} if observation.seat is not None else {}),
                age_seconds=age if age is not None and math.isfinite(age) else None,
                observed_at=observation.observed_at if math.isfinite(observation.observed_at) else None,
                ttl_seconds=OPERATION_TTL_SECONDS,
                detail=safe_detail(observation.detail)[:240],
                **({"observed_status": observation.status} if isolation_refused else {}),
            )
    admitted_capacity = min(admitted_capacities) if admitted_capacities else None

    blocked = any(item["status"] in {"failed", "denied", "unsupported"} for item in evidence)
    status = (
        "blocked" if blocked else "unverified" if any(item["status"] == "unknown" for item in evidence) else "ready"
    )
    limiting = next((item for item in evidence if item["status"] in {"failed", "denied", "unsupported"}), None)
    if limiting is None:
        limiting = next((item for item in evidence if item["status"] == "unknown"), None)
    report = {
        "schema": SCHEMA,
        "status": status,
        "exit_code": {"ready": 0, "blocked": 2, "unverified": 3}[status],
        "observed_at": now,
        "scope": scope,
        "request": asdict(request),
        "roster": {
            "source": roster.resolution.source if roster.resolution else "provided",
            "path": str(roster.resolution.path) if roster.resolution else None,
            "discovery": "local-cache-only",
            "local_authoritative": not hub_discovery_possible,
        },
        "selected_seats": selected,
        "structural_worker_count": len(roster_mod.workers(roster)),
        "usable_worker_count": sum(
            name != roster.orchestrator
            and name in roster.agents
            and all(item["status"] == "passed" for item in evidence if item.get("seat") == name)
            for name in set(request.workers)
        ),
        "configured_limit": roster.max_workers,
        "admitted_capacity": admitted_capacity,
        "limiting_reason": limiting["reason"] if limiting else None,
        "fallback": {
            "automatic": False,
            "admitted": False,
            "action": "Request route brigade explicitly and obtain its own admission evidence. "
            "A task that `brigade t3 open` refuses as t3-unsupported goes to a separate "
            "`brigade run batch` request with its own admission."
            if request.route == "t3"
            else None,
        },
        "evidence": evidence,
    }
    if request.route == "t3":
        report.update(T3_QUALIFIERS)
        if isinstance(t3_context, Mapping):
            envelope = t3_context.get("envelope")
            if isinstance(envelope, Mapping) and envelope.get("state") == "ok":
                report["t3_reported_at"] = envelope.get("reported_at")
                report["t3_received_at"] = t3_context.get("received_at")
        if report["status"] == "ready" and any(report.get(key) != value for key, value in T3_QUALIFIERS.items()):
            report["status"], report["exit_code"], report["limiting_reason"] = "unverified", 3, "t3-qualifier-missing"
    elif request.route == "orca":
        report.update(ORCA_QUALIFIERS)
    return report


def brigade_route_observations(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    scope: str | None = None,
    now: float | None = None,
) -> dict[str, Observation]:
    """Produce authoritative, strictly read-only operation observations.

    Produces ``admission`` and ``capacity`` for ``route == "brigade"`` and, for
    worktree isolation requests, an ``isolation`` observation. Delegates to
    :func:`brigade.roster_readiness_brigade.brigade_route_observations`.
    """
    from .roster_readiness_brigade import brigade_route_observations as _impl

    return _impl(roster, request, workspace=workspace, scope=scope, now=now)


def inspect_readiness(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    health_probe: seat_health.SeatHealthProbe | None = None,
    hub_discovery_possible: bool = False,
    t3_report: Any | None = None,
) -> dict[str, Any]:
    """Inspect only structurally eligible selected seats, without model smoke."""
    if request.route == "t3":
        if _request_error(request):
            return evaluate_readiness(
                roster, request, workspace=workspace, hub_discovery_possible=hub_discovery_possible
            )
        from .roster_readiness_t3 import t3_route_readiness

        malformed_report = t3_report is not None and getattr(t3_report, "state", None) == "malformed"
        if not malformed_report and any(
            seat not in roster.agents and hub_discovery_possible for seat in request.workers
        ):
            return evaluate_readiness(
                roster, request, workspace=workspace, hub_discovery_possible=hub_discovery_possible
            )
        context, t3_observations = t3_route_readiness(
            roster, request, workspace=workspace, report=t3_report, now=time.time()
        )
        return evaluate_readiness(
            roster,
            request,
            workspace=workspace,
            observations=t3_observations,
            now=time.time(),
            hub_discovery_possible=hub_discovery_possible,
            t3_context=context,
        )
    initial = evaluate_readiness(roster, request, workspace=workspace, hub_discovery_possible=hub_discovery_possible)
    if initial["status"] == "blocked" or any(
        seat["selection_role"] == "unresolved" for seat in initial["selected_seats"]
    ):
        return initial
    request = replace(request, workers=tuple(dict.fromkeys(request.workers)))
    if request.route == "orca":
        from .roster_readiness_orca import orca_route_observations

        return evaluate_readiness(
            roster,
            request,
            workspace=workspace,
            observations=orca_route_observations(roster, request, scope=initial["scope"]),
            hub_discovery_possible=hub_discovery_possible,
        )
    # A separate checkout is not a read-only sandbox. Confirm only Git
    # eligibility here, without creating a worktree or asserting disk capacity.
    if request.isolation == "worktree" and not runguard.is_git_checkout_with_commit(workspace):
        scope = operation_scope(roster, request, workspace)
        return evaluate_readiness(
            roster,
            request,
            workspace=workspace,
            hub_discovery_possible=hub_discovery_possible,
            observations={
                "isolation": Observation(
                    "failed",
                    time.time(),
                    scope,
                    "Worktree isolation requires a Git checkout with a committed HEAD.",
                    source=SOURCE_BRIGADE_GOVERNANCE,
                )
            },
        )
    from .roster_readiness_brigade import brigade_route_readiness

    effective_roster, observations = brigade_route_readiness(roster, request, workspace=workspace)
    selected_roster = replace(
        effective_roster,
        agents={name: replace(effective_roster.agents[name], fallback=()) for name in request.workers},
    )
    probe = health_probe or seat_health.SeatHealthProbe(collect_executable_version=False)
    results = probe.probe_roster(selected_roster, workspace=workspace, allow_model_smoke=False)
    observed_now = max([time.time(), *(obs.observed_at for obs in observations.values())])
    return evaluate_readiness(
        effective_roster,
        request,
        workspace=workspace,
        health={result.seat: result for result in results},
        observations=observations,
        now=observed_now,
        hub_discovery_possible=hub_discovery_possible,
    )
