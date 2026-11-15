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
from dataclasses import asdict, dataclass, replace
from numbers import Real
from pathlib import Path
from typing import Any, Mapping

from . import roster as roster_mod
from . import runguard, seat_health
from .worker_failure import safe_detail

SCHEMA = "brigade.roster_readiness.v1"
OPERATION_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class ReadinessRequest:
    workers: tuple[str, ...]
    concurrency: int = 1
    route: str = "brigade"
    isolation: str = "shared"
    capability: str | None = None


@dataclass(frozen=True)
class Observation:
    """An operation-scoped fact from an authoritative integration, not policy.

    ``observed_at`` is Unix time; ``scope`` comes from operation_scope.
    Status is passed, failed, denied, unsupported, or unknown. Capacity is
    required only for a passed capacity observation. No admission is reserved.
    """

    status: str
    observed_at: float
    scope: str
    detail: str = ""
    capacity: int | None = None


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


def operation_scope(roster: roster_mod.Roster, request: ReadinessRequest, workspace: Path) -> str:
    error = _request_error(request)
    if error:
        raise ValueError(error)
    request = replace(request, workers=tuple(sorted(set(request.workers))))
    inputs = {
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
    return "sha256:" + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def _request_error(request: ReadinessRequest) -> str | None:
    if (
        not isinstance(request.workers, (tuple, list))
        or not request.workers
        or any(not isinstance(name, str) or not name.strip() for name in request.workers)
    ):
        return "Select at least one named worker with --worker."
    if type(request.concurrency) is not int or request.concurrency < 1:
        return "Concurrency must be a positive integer."
    if not isinstance(request.route, str) or request.route not in {"brigade", "t3"}:
        return "Route must be brigade or t3."
    if not isinstance(request.isolation, str) or request.isolation not in {"shared", "worktree"}:
        return "Isolation must be shared or worktree."
    if request.capability is not None and (
        not isinstance(request.capability, str)
        or not re.fullmatch(r"[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+", request.capability)
    ):
        return "Capability must be a dotted capability name, such as research.plan."
    return None


def evaluate_readiness(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    health: Mapping[str, seat_health.SeatHealthResult] | None = None,
    observations: Mapping[str, Observation] | None = None,
    now: float | None = None,
    hub_discovery_possible: bool = False,
) -> dict[str, Any]:
    """Evaluate facts supplied by selected-seat health and route integrations.

    ``inspect_readiness`` produces live read-only ``admission`` and
    ``capacity`` observations for ``route == "brigade"``. Embedding callers may
    still inject explicit observations; T3 governance support remains
    unproduced here.
    """
    now = time.time() if now is None else now
    evidence: list[dict[str, Any]] = []
    health = health or {}
    observations = observations or {}

    def add(category: str, status: str, reason: str, action: str = "", **fields: Any) -> None:
        evidence.append({"category": category, "status": status, "reason": reason, "action": action, **fields})

    error = _request_error(request)
    if error:
        add("request", "failed", "malformed-request", error, freshness="current")
        # Malformed programmatic values must never reach hashing, selection,
        # capability checks, or providers, and may not be JSON serializable.
        return {
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
            status = (
                "unknown"
                if not fresh or check is None
                else {"passed": "passed", "failed": "failed"}.get(check.status, "unknown")
            )
            add(
                check_name,
                status,
                check.cause_code or check.status if fresh and check else "health-unobserved",
                "Refresh selected-seat health evidence."
                if status == "unknown"
                else "Inspect the selected seat health failure."
                if status == "failed"
                else "",
                seat=name,
                freshness="fresh" if fresh else "missing" if result is None else "stale",
                age_seconds=age if age is not None and math.isfinite(age) else None,
                ttl_seconds=ttl,
                observed_at=observed_at,
                detail=safe_detail(check.detail)[:240] if check else "",
            )

    scope = operation_scope(roster, request, workspace)
    admitted_capacity = None
    isolation_incompatible = any(item["category"] == "isolation" and item["status"] == "failed" for item in evidence)
    for category in ("route-support", "isolation", "admission", "capacity"):
        observation = observations.get(category)
        if observation is None:
            if category == "route-support" and request.route == "brigade":
                add(category, "passed", "brigade-route-declared", freshness="current")
            elif category == "isolation" and request.route == "brigade" and request.isolation == "shared":
                add(
                    category,
                    "failed" if isolation_incompatible else "passed",
                    "isolation-incompatible" if isolation_incompatible else "shared-isolation-requested",
                    "Select workers compatible with the requested isolation." if isolation_incompatible else "",
                    freshness="current",
                )
            else:
                reason = "t3-contract-unavailable" if category == "route-support" else "observation-missing"
                action = (
                    "T3 capability and Brigade governance support evidence is unavailable; connect an authoritative readiness integration. "
                    "Brigade fallback requires separate admission and an explicit route request."
                    if reason == "t3-contract-unavailable"
                    else f"Obtain fresh operation-scoped {category} evidence from the execution route."
                )
                add(category, "unknown", reason, action, freshness="missing")
            continue
        if (
            not isinstance(observation, Observation)
            or not isinstance(observation.status, str)
            or not _finite_real(observation.observed_at)
            or not isinstance(observation.scope, str)
            or not isinstance(observation.detail, str)
        ):
            add(
                category,
                "unknown",
                "observation-malformed",
                "Obtain well-formed evidence for this exact operation.",
                freshness="missing",
            )
            continue
        age = now - observation.observed_at
        fresh = math.isfinite(age) and 0 <= age <= OPERATION_TTL_SECONDS and observation.scope == scope
        status = (
            observation.status
            if fresh and observation.status in {"passed", "failed", "denied", "unsupported"}
            else "unknown"
        )
        reason = "observed" if fresh else "observation-stale-or-scope-mismatch"
        if fresh and status in {"failed", "denied", "unsupported"}:
            reason = f"{category}-{status}"
        elif fresh and status == "unknown":
            reason = "observation-incomplete"
        isolation_refused = category == "isolation" and status == "passed" and isolation_incompatible
        if isolation_refused:
            status, reason = "failed", "isolation-incompatible"
        if category == "capacity" and status == "passed":
            if type(observation.capacity) is not int or observation.capacity < 0:
                status, reason = "unknown", "capacity-unobserved"
            else:
                admitted_capacity = observation.capacity
                if admitted_capacity < request.concurrency:
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
            age_seconds=age if age is not None and math.isfinite(age) else None,
            observed_at=observation.observed_at if math.isfinite(observation.observed_at) else None,
            ttl_seconds=OPERATION_TTL_SECONDS,
            detail=safe_detail(observation.detail)[:240],
            **({"observed_status": observation.status} if isolation_refused else {}),
        )

    blocked = any(item["status"] in {"failed", "denied", "unsupported"} for item in evidence)
    status = (
        "blocked" if blocked else "unverified" if any(item["status"] == "unknown" for item in evidence) else "ready"
    )
    limiting = next((item for item in evidence if item["status"] in {"failed", "denied", "unsupported"}), None)
    if limiting is None:
        limiting = next((item for item in evidence if item["status"] == "unknown"), None)
    return {
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
            "action": "Request route brigade explicitly and obtain its own admission evidence."
            if request.route == "t3"
            else None,
        },
        "evidence": evidence,
    }


def _local_lock_capacity_observation(
    lock_state: str,
    max_workers: int,
    *,
    observed_at: float,
    scope: str,
    extra_source: str | None = None,
) -> Observation:
    if lock_state == "live":
        return Observation(
            "passed",
            observed_at,
            scope,
            detail="local run lock (live owner holds workspace)",
            capacity=0,
        )
    if lock_state == "skipped":
        return Observation(
            "failed",
            observed_at,
            scope,
            detail="local run lock (unrecovered active run requires brigade runs recover)",
        )
    if lock_state not in {"absent", "unreconciled"}:
        return Observation(
            "unknown",
            observed_at,
            scope,
            detail=f"local run lock state is {lock_state}",
        )
    lock_desc = "local run lock (absent)" if lock_state == "absent" else "local run lock (reconcileable dead owner)"
    detail = f"{lock_desc}, {extra_source}" if extra_source else f"{lock_desc} and roster max_workers"
    return Observation("passed", observed_at, scope, detail=detail, capacity=max_workers)


def _hub_snapshot_from_roster_payload(payload: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    from . import fleet_client_cloud, fleet_model_roster

    schema = payload.get("schema")
    revision = payload.get("revision", payload.get("roster_revision"))
    digest = payload.get("document_sha256")
    raw_seats = payload.get("seats")
    if (
        schema != fleet_model_roster.ROSTER_SCHEMA
        or type(revision) is not int
        or not isinstance(digest, str)
        or not digest
        or not isinstance(raw_seats, list)
    ):
        return {
            "schema": schema,
            "state": "malformed-roster",
            "source": source,
            "models": [],
            "error": "malformed roster payload",
        }
    seats = [dict(item) for item in raw_seats if isinstance(item, Mapping)]
    models = [safe for item in seats if (safe := fleet_client_cloud._safe_model_policy(item)) is not None]
    authority, authority_error = fleet_model_roster.parse_fleet_policy_authority(payload.get("fleet_policy"))
    if authority_error is not None:
        return {
            "schema": payload.get("schema"),
            "state": "malformed-policy",
            "source": source,
            "models": [],
            "error": authority_error,
        }
    snapshot: dict[str, Any] = {
        "schema": payload.get("schema"),
        "state": "authoritative",
        "source": source,
        "roster_revision": revision,
        "revision": revision,
        "document_sha256": payload.get("document_sha256"),
        "roster_digest": payload.get("document_sha256"),
        "expires_at": payload.get("expires_at"),
        "seats": seats,
        "consumer_defaults": payload.get("consumer_defaults"),
        "retired_models": payload.get("retired_models"),
        "models": models,
    }
    if authority is not None:
        snapshot["fleet_policy"] = authority
    launch_bindings = payload.get("consumer_launch_bindings")
    if isinstance(launch_bindings, Mapping):
        snapshot["consumer_launch_bindings"] = dict(launch_bindings)
    return snapshot


def _brigade_hub_claim_capacity(
    workspace: Path,
    lock_state: str,
    *,
    observed_at: float,
    scope: str,
) -> tuple[Observation | None, bool]:
    """Read-only cross-machine repo claim check matching ``brigade run``.

    Returns ``(blocking_or_unknown_observation, claim_checked)``.
    """
    from . import fleet_client, fleet_home_guard

    if fleet_home_guard.absolute_brigade_home() is None:
        return None, False
    try:
        claim_target = fleet_client.resolve_claim_target(workspace)
        claim_decision = fleet_client.inspect_claim(claim_target)
    except fleet_home_guard.RelativeBrigadeHomeError:
        return None, False
    except (fleet_client.FleetClientError, OSError, ValueError) as exc:
        return (
            Observation(
                "unknown",
                observed_at,
                scope,
                detail=f"hub repo claim unverified ({safe_detail(str(exc))})",
            ),
            False,
        )
    if not getattr(claim_decision, "granted", False):
        reason = getattr(claim_decision, "reason", None) or "hub-unavailable"
        return (
            Observation(
                "unknown",
                observed_at,
                scope,
                detail=f"hub repo claim unverified ({reason})",
            ),
            False,
        )
    claim = getattr(claim_decision, "claim", None)
    if claim is None:
        return None, True
    if not isinstance(claim, Mapping):
        return (
            Observation(
                "unknown",
                observed_at,
                scope,
                detail="hub repo claim returned a malformed payload",
            ),
            False,
        )
    can_supersede = False
    if lock_state == "unreconciled":
        try:
            local_node = fleet_client.resolve_node_id()
            dead_owner = runguard.read_lock_owner(runguard.lock_path(workspace))
        except (fleet_client.FleetClientError, runguard.RunGuardError, OSError, RuntimeError):
            local_node = None
            dead_owner = None
        dead_token = dead_owner.get("owner_token") if isinstance(dead_owner, Mapping) else None
        claim_lock_token = claim.get("lock_owner_token")
        can_supersede = (
            bool(local_node)
            and claim.get("owner_node") == local_node
            and isinstance(dead_token, str)
            and bool(dead_token)
            and claim_lock_token == dead_token
        )
    if can_supersede:
        return None, True
    owner = claim.get("owner_node") or "another node"
    return (
        Observation(
            "passed",
            observed_at,
            scope,
            detail=f"hub repo claim held by {owner}",
            capacity=0,
        ),
        True,
    )


def brigade_route_observations(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    scope: str | None = None,
    now: float | None = None,
) -> dict[str, Observation]:
    """Produce authoritative, strictly read-only ``admission`` and ``capacity``
    observations for ``route == "brigade"``.

    Never acquires a lease or claim, never writes LKG/audit caches, never launches
    a task, and never sends model inference.
    """
    from . import (
        aboyeur_model_policy,
        agents as agents_mod,
        fleet_client,
        fleet_client_policy,
        fleet_model_admission,
        fleet_model_roster,
        fleet_policy,
        fleet_session_bootstrap,
        fleet_session_presence,
        route_receipts,
    )

    if request.route != "brigade":
        raise ValueError("brigade_route_observations only supports route == 'brigade'")
    resolved_scope = operation_scope(roster, request, workspace) if scope is None else scope
    observed_at = time.time() if now is None else now
    workers = tuple(dict.fromkeys(request.workers))
    lock_state = runguard.inspect_run_lock_reconcile(workspace)

    try:
        settings = fleet_client.load_fleet_settings()
    except (fleet_client.FleetClientError, OSError, ValueError) as exc:
        detail = f"fleet configuration unavailable ({safe_detail(str(exc))})"
        return {
            "admission": Observation("unknown", observed_at, resolved_scope, detail=detail),
            "capacity": _local_lock_capacity_observation(
                lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
            )
            if lock_state in {"live", "skipped", "invalid"}
            else Observation("unknown", observed_at, resolved_scope, detail=detail),
        }

    hub_url = str(settings.get("hub_url") or "").strip() if isinstance(settings, Mapping) else ""
    if not hub_url:
        if fleet_model_admission.enrollment_activation_observed():
            return {
                "admission": Observation(
                    "denied",
                    observed_at,
                    resolved_scope,
                    detail="hub model admission: active enrollment observed and hub_url is unconfigured",
                ),
                "capacity": _local_lock_capacity_observation(
                    lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
                )
                if lock_state in {"live", "skipped", "invalid"}
                else Observation(
                    "unknown",
                    observed_at,
                    resolved_scope,
                    detail="hub capacity unverified: active enrollment observed without configured hub",
                ),
            }
        admissible = set(route_receipts.admissible_seats(roster))
        admission_obs: Observation | None = None
        for seat_name in workers:
            if seat_name not in admissible:
                admission_obs = Observation(
                    "denied",
                    observed_at,
                    resolved_scope,
                    detail=f"local roster admissible seats: seat {seat_name!r} is not an admissible worker",
                )
                break
            agent = roster.agents[seat_name]
            provider = agents_mod.model_policy_provider(agent.cli or "")
            model = agents_mod.model_policy_model(agent.cli or "", agent.model)
            retired = fleet_model_roster.retired_reason(provider, model)
            if retired is not None:
                admission_obs = Observation(
                    "denied",
                    observed_at,
                    resolved_scope,
                    detail=f"local roster admissible seats: seat {seat_name!r} uses retired model ({retired})",
                )
                break
        if admission_obs is None:
            admission_obs = Observation(
                "passed",
                observed_at,
                resolved_scope,
                detail="local roster admissible seats (no fleet hub configured)",
            )
        capacity_obs = _local_lock_capacity_observation(
            lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
        )
        return {"admission": admission_obs, "capacity": capacity_obs}

    # Hub is configured: query versioned model roster read-only (never write LKG cache).
    try:
        decision = fleet_model_admission.fetch_versioned_roster(
            allow_lkg=True,
            hub_url=hub_url,
            cache_write=False,
        )
    except Exception as exc:
        detail = f"hub model admission unavailable ({safe_detail(str(exc))})"
        return {
            "admission": Observation("unknown", observed_at, resolved_scope, detail=detail),
            "capacity": _local_lock_capacity_observation(
                lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
            )
            if lock_state in {"live", "skipped", "invalid"}
            else Observation("unknown", observed_at, resolved_scope, detail=detail),
        }

    if not decision.ok:
        if decision.reason == "enrollment-downgrade":
            admission_obs = Observation(
                "denied",
                observed_at,
                resolved_scope,
                detail="hub model admission: enrollment downgrade rejected",
            )
        else:
            admission_obs = Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail=f"hub model admission unavailable ({decision.reason})",
            )
        capacity_obs = (
            _local_lock_capacity_observation(
                lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
            )
            if lock_state in {"live", "skipped", "invalid"}
            else Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail=f"hub capacity unverified ({decision.reason})",
            )
        )
        return {"admission": admission_obs, "capacity": capacity_obs}

    if not isinstance(decision.payload, Mapping):
        detail = "hub model admission returned a malformed roster payload"
        return {
            "admission": Observation("unknown", observed_at, resolved_scope, detail=detail),
            "capacity": _local_lock_capacity_observation(
                lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
            )
            if lock_state in {"live", "skipped", "invalid"}
            else Observation("unknown", observed_at, resolved_scope, detail=detail),
        }

    raw_source = decision.payload.get("source")
    source = (
        raw_source
        if isinstance(raw_source, str) and raw_source
        else (decision.reason if decision.reason in {"hub", "lkg"} else "hub")
    )
    source_label = "hub model admission LKG" if source == "lkg" else "hub model admission"
    snapshot = _hub_snapshot_from_roster_payload(decision.payload, source=source)
    if snapshot.get("state") != "authoritative":
        detail = f"{source_label}: {snapshot.get('error') or snapshot.get('state') or 'malformed policy authority'}"
        return {
            "admission": Observation("unknown", observed_at, resolved_scope, detail=detail),
            "capacity": _local_lock_capacity_observation(
                lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
            )
            if lock_state in {"live", "skipped", "invalid"}
            else Observation("unknown", observed_at, resolved_scope, detail=detail),
        }

    try:
        model_policy = aboyeur_model_policy.resolve_fleet_model_policy(roster, snapshot=snapshot)
    except Exception as exc:
        detail = f"{source_label}: malformed model policy resolution ({safe_detail(str(exc))})"
        return {
            "admission": Observation("unknown", observed_at, resolved_scope, detail=detail),
            "capacity": _local_lock_capacity_observation(
                lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
            )
            if lock_state in {"live", "skipped", "invalid"}
            else Observation("unknown", observed_at, resolved_scope, detail=detail),
        }

    receipt = model_policy.receipt
    raw_decisions = receipt.get("decisions") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("state") != "authoritative"
        or not isinstance(raw_decisions, list)
        or not raw_decisions
    ):
        detail = model_policy.error or (receipt.get("state") if isinstance(receipt, Mapping) else None) or "malformed"
        return {
            "admission": Observation("unknown", observed_at, resolved_scope, detail=f"{source_label}: {detail}"),
            "capacity": _local_lock_capacity_observation(
                lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
            )
            if lock_state in {"live", "skipped", "invalid"}
            else Observation("unknown", observed_at, resolved_scope, detail=f"{source_label}: {detail}"),
        }

    decisions_by_seat = {
        str(item["seat"]): item
        for item in raw_decisions
        if isinstance(item, Mapping) and isinstance(item.get("seat"), str)
    }
    admission_obs = None
    for seat_name in workers:
        seat_decision = decisions_by_seat.get(seat_name)
        if seat_decision is None:
            admission_obs = Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail=f"{source_label}: missing decision for seat {seat_name!r}",
            )
            break
        if seat_decision.get("outcome") != "enabled":
            admission_obs = Observation(
                "denied",
                observed_at,
                resolved_scope,
                detail=f"{source_label}: seat {seat_name!r} denied ({seat_decision.get('outcome')}: {seat_decision.get('detail')})",
            )
            break

    enrollment = fleet_session_bootstrap.classify_enrollment(snapshot)
    policy_doc: Mapping[str, Any] | None = None
    if admission_obs is None:
        if enrollment == "denied":
            admission_obs = Observation(
                "denied",
                observed_at,
                resolved_scope,
                detail=f"{source_label}: active fleet policy authority required",
            )
        elif enrollment == "enrolled":
            try:
                policy_payload = fleet_client_policy.show_policy()
                raw_doc = policy_payload.get("document") if isinstance(policy_payload, Mapping) else None
                if not isinstance(raw_doc, Mapping):
                    raise fleet_policy.FleetPolicyError("fleet policy response is missing document")
                parsed_policy = fleet_policy.parse_document(raw_doc)
                policy_doc = raw_doc
            except (
                fleet_client_policy.FleetPolicyClientError,
                fleet_client.FleetClientError,
                fleet_policy.FleetPolicyError,
                OSError,
                ValueError,
            ) as exc:
                admission_obs = Observation(
                    "unknown",
                    observed_at,
                    resolved_scope,
                    detail=f"{source_label} + fleet policy unavailable ({safe_detail(str(exc))})",
                )
            else:
                if fleet_session_bootstrap.CONSUMER_BRIGADE_RUN not in (parsed_policy.get("consumers") or {}):
                    admission_obs = Observation(
                        "denied",
                        observed_at,
                        resolved_scope,
                        detail=f"{source_label} + fleet policy: consumer brigade-run is not enrolled (enrollment-required)",
                    )
                else:
                    repo_identity = fleet_session_presence.repository_identity(workspace).value
                    resolved_policy = fleet_policy.resolve_policy(
                        policy_doc,
                        fleet_session_bootstrap.CONSUMER_BRIGADE_RUN,
                        repo_identity,
                    )
                    for seat_name in workers:
                        verdict = fleet_policy.admissible_seat(policy_doc, seat_name, resolved_policy)
                        if not verdict.get("admissible"):
                            reasons = ", ".join(str(item) for item in verdict.get("reasons") or ())
                            admission_obs = Observation(
                                "denied",
                                observed_at,
                                resolved_scope,
                                detail=f"{source_label} + fleet policy: seat {seat_name!r} denied ({reasons})",
                            )
                            break
                    if admission_obs is None:
                        admission_obs = Observation(
                            "passed",
                            observed_at,
                            resolved_scope,
                            detail=f"{source_label} + fleet policy",
                        )
        else:
            admission_obs = Observation("passed", observed_at, resolved_scope, detail=source_label)

    # Evaluate capacity without acquiring leases or claims.
    if lock_state in {"live", "skipped", "invalid"}:
        capacity_obs = _local_lock_capacity_observation(
            lock_state, roster.max_workers, observed_at=observed_at, scope=resolved_scope
        )
        return {"admission": admission_obs, "capacity": capacity_obs}

    if source != "hub":
        return {
            "admission": admission_obs,
            "capacity": Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail="hub capacity unverified while using hub model admission LKG",
            ),
        }

    claim_obs, claim_checked = _brigade_hub_claim_capacity(
        workspace, lock_state, observed_at=observed_at, scope=resolved_scope
    )
    if claim_obs is not None:
        return {"admission": admission_obs, "capacity": claim_obs}

    if enrollment == "enrolled":
        if policy_doc is not None:
            parsed_seats = fleet_policy.parse_document(policy_doc).get("seats") or {}
            for seat_name in workers:
                seat_cfg = parsed_seats.get(seat_name)
                if isinstance(seat_cfg, Mapping) and seat_cfg.get("concurrency") == 0:
                    return {
                        "admission": admission_obs,
                        "capacity": Observation(
                            "passed",
                            observed_at,
                            resolved_scope,
                            detail=f"fleet policy: seat {seat_name!r} concurrency is 0",
                            capacity=0,
                        ),
                    }
        return {
            "admission": admission_obs,
            "capacity": Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail="hub capacity unverified: enrolled fleet policy enforces hub seat leases without a read-only usage endpoint",
            ),
        }

    seat_rows_by_name = {
        str(row["seat"]): row
        for row in snapshot.get("seats") or ()
        if isinstance(row, Mapping) and isinstance(row.get("seat"), str)
    }
    for seat_name in workers:
        row = seat_rows_by_name.get(seat_name)
        if row is None:
            continue
        limit = row.get("limit")
        if limit is None:
            continue
        if type(limit) is not int or limit < 0:
            return {
                "admission": admission_obs,
                "capacity": Observation(
                    "unknown",
                    observed_at,
                    resolved_scope,
                    detail=f"hub model admission returned malformed limit for seat {seat_name!r}",
                ),
            }
        if limit == 0:
            return {
                "admission": admission_obs,
                "capacity": Observation(
                    "passed",
                    observed_at,
                    resolved_scope,
                    detail=f"hub model admission: seat {seat_name!r} concurrency limit is 0",
                    capacity=0,
                ),
            }
        return {
            "admission": admission_obs,
            "capacity": Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail=f"hub capacity unverified: seat {seat_name!r} enforces concurrency limit {limit} without a read-only lease usage endpoint",
            ),
        }

    extra_source = (
        "hub repo claim and hub model seat limits (unlimited)" if claim_checked else "hub model seat limits (unlimited)"
    )
    capacity_obs = _local_lock_capacity_observation(
        lock_state,
        roster.max_workers,
        observed_at=observed_at,
        scope=resolved_scope,
        extra_source=extra_source,
    )
    return {"admission": admission_obs, "capacity": capacity_obs}


def inspect_readiness(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    health_probe: seat_health.SeatHealthProbe | None = None,
    hub_discovery_possible: bool = False,
) -> dict[str, Any]:
    """Inspect only structurally eligible selected seats, without model smoke."""
    initial = evaluate_readiness(roster, request, workspace=workspace, hub_discovery_possible=hub_discovery_possible)
    if (
        initial["status"] == "blocked"
        or request.route != "brigade"
        or any(seat["selection_role"] == "unresolved" for seat in initial["selected_seats"])
    ):
        return initial
    request = replace(request, workers=tuple(dict.fromkeys(request.workers)))
    selected_roster = replace(
        roster, agents={name: replace(roster.agents[name], fallback=()) for name in request.workers}
    )
    # A separate checkout is not a read-only sandbox. Confirm only Git
    # eligibility here, without creating a worktree or asserting disk capacity.
    if request.isolation == "worktree" and not runguard.is_git_worktree(workspace):
        scope = operation_scope(roster, request, workspace)
        return evaluate_readiness(
            roster,
            request,
            workspace=workspace,
            hub_discovery_possible=hub_discovery_possible,
            observations={
                "isolation": Observation("failed", time.time(), scope, "Worktree isolation requires a Git checkout.")
            },
        )
    probe = health_probe or seat_health.SeatHealthProbe(collect_executable_version=False)
    results = probe.probe_roster(selected_roster, workspace=workspace, allow_model_smoke=False)
    scope = operation_scope(roster, request, workspace)
    observations = brigade_route_observations(roster, request, workspace=workspace, scope=scope)
    observed_now = max([time.time(), *(obs.observed_at for obs in observations.values())])
    return evaluate_readiness(
        roster,
        request,
        workspace=workspace,
        health={result.seat: result for result in results},
        observations=observations,
        now=observed_now,
        hub_discovery_possible=hub_discovery_possible,
    )
