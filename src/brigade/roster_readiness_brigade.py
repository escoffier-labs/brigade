"""Authoritative read-only admission and capacity producer for ``route == "brigade"``."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from . import roster as roster_mod
from . import runguard
from .roster_readiness import Observation, ReadinessRequest, operation_scope
from .worker_failure import safe_detail


@dataclass(frozen=True)
class _HubAdmissionOutcome:
    effective_roster: roster_mod.Roster
    scope: str
    admission: Observation
    snapshot: Mapping[str, Any] | None = None
    source: str | None = None
    enrollment: str | None = None
    parsed_policy: Mapping[str, Any] | None = None
    early_capacity_detail: str | None = None


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


def _capacity_or_unknown(
    lock_state: str,
    max_workers: int,
    detail: str,
    *,
    observed_at: float,
    scope: str,
) -> Observation:
    if lock_state in {"live", "skipped", "invalid"}:
        return _local_lock_capacity_observation(
            lock_state,
            max_workers,
            observed_at=observed_at,
            scope=scope,
        )
    return Observation("unknown", observed_at, scope, detail=detail)


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
    # fleet_client_cloud has no separate public snapshot-from-payload builder; mirror load_model_policy_snapshot.
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
    if not claim_decision.granted:
        reason = claim_decision.reason or "hub-unavailable"
        if reason == "auth-failed":
            return (
                Observation(
                    "failed",
                    observed_at,
                    scope,
                    detail=(
                        "hub repo claim rejected this node's credentials (auth-failed); "
                        "enroll the node and set [fleet] node_token_file"
                    ),
                ),
                False,
            )
        return (
            Observation(
                "unknown",
                observed_at,
                scope,
                detail=f"hub repo claim unverified ({reason})",
            ),
            False,
        )
    claim = claim_decision.claim
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


def _local_route_observations(
    roster: roster_mod.Roster,
    workers: tuple[str, ...],
    lock_state: str,
    *,
    observed_at: float,
    scope: str,
) -> dict[str, Observation]:
    from . import agents as agents_mod
    from . import fleet_model_admission, fleet_model_roster, route_receipts

    # enrollment_activation_observed swallows spool read errors and returns False, matching brigade run.
    if fleet_model_admission.enrollment_activation_observed():
        return {
            "admission": Observation(
                "denied",
                observed_at,
                scope,
                detail="hub model admission: active enrollment observed and hub_url is unconfigured",
            ),
            "capacity": _capacity_or_unknown(
                lock_state,
                roster.max_workers,
                "hub capacity unverified: active enrollment observed without configured hub",
                observed_at=observed_at,
                scope=scope,
            ),
        }
    admissible = set(route_receipts.admissible_seats(roster))
    admission_obs: Observation | None = None
    for seat_name in workers:
        if seat_name not in admissible:
            admission_obs = Observation(
                "denied",
                observed_at,
                scope,
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
                scope,
                detail=f"local roster admissible seats: seat {seat_name!r} uses retired model ({retired})",
            )
            break
    if admission_obs is None:
        admission_obs = Observation(
            "passed",
            observed_at,
            scope,
            detail="local roster admissible seats (no fleet hub configured)",
        )
    capacity_obs = _local_lock_capacity_observation(
        lock_state, roster.max_workers, observed_at=observed_at, scope=scope
    )
    return {"admission": admission_obs, "capacity": capacity_obs}


def _hub_admission(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    workers: tuple[str, ...],
    hub_url: str,
    workspace: Path,
    *,
    scope: str | None,
    observed_at: float,
) -> _HubAdmissionOutcome:
    from . import (
        aboyeur_model_policy,
        fleet_client,
        fleet_client_policy,
        fleet_model_admission,
        fleet_policy,
        fleet_session_bootstrap,
        fleet_session_presence,
    )

    def _early_scope() -> str:
        return operation_scope(roster, request, workspace) if scope is None else scope

    try:
        decision = fleet_model_admission.fetch_versioned_roster(
            allow_lkg=True,
            hub_url=hub_url,
            cache_write=False,
        )
    except Exception as exc:
        resolved_scope = _early_scope()
        detail = f"hub model admission unavailable ({safe_detail(str(exc))})"
        return _HubAdmissionOutcome(
            effective_roster=roster,
            scope=resolved_scope,
            admission=Observation("unknown", observed_at, resolved_scope, detail=detail),
            early_capacity_detail=detail,
        )

    if not decision.ok:
        resolved_scope = _early_scope()
        if decision.reason == "enrollment-downgrade":
            early_admission = Observation(
                "denied",
                observed_at,
                resolved_scope,
                detail="hub model admission: enrollment downgrade rejected",
            )
        else:
            early_admission = Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail=f"hub model admission unavailable ({decision.reason})",
            )
        return _HubAdmissionOutcome(
            effective_roster=roster,
            scope=resolved_scope,
            admission=early_admission,
            early_capacity_detail=f"hub capacity unverified ({decision.reason})",
        )

    if not isinstance(decision.payload, Mapping):
        resolved_scope = _early_scope()
        detail = "hub model admission returned a malformed roster payload"
        return _HubAdmissionOutcome(
            effective_roster=roster,
            scope=resolved_scope,
            admission=Observation("unknown", observed_at, resolved_scope, detail=detail),
            early_capacity_detail=detail,
        )

    raw_source = decision.payload.get("source")
    source = (
        raw_source
        if isinstance(raw_source, str) and raw_source
        else (decision.reason if decision.reason in {"hub", "lkg"} else "hub")
    )
    source_label = "hub model admission LKG" if source == "lkg" else "hub model admission"
    snapshot = _hub_snapshot_from_roster_payload(decision.payload, source=source)
    if snapshot.get("state") != "authoritative":
        resolved_scope = _early_scope()
        detail = f"{source_label}: {snapshot.get('error') or snapshot.get('state') or 'malformed policy authority'}"
        return _HubAdmissionOutcome(
            effective_roster=roster,
            scope=resolved_scope,
            admission=Observation("unknown", observed_at, resolved_scope, detail=detail),
            early_capacity_detail=detail,
        )

    try:
        model_policy = aboyeur_model_policy.resolve_fleet_model_policy(roster, snapshot=snapshot)
    except Exception as exc:
        resolved_scope = _early_scope()
        detail = f"{source_label}: malformed model policy resolution ({safe_detail(str(exc))})"
        return _HubAdmissionOutcome(
            effective_roster=roster,
            scope=resolved_scope,
            admission=Observation("unknown", observed_at, resolved_scope, detail=detail),
            early_capacity_detail=detail,
        )

    receipt = model_policy.receipt
    raw_decisions = receipt.get("decisions") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("state") != "authoritative"
        or not isinstance(raw_decisions, list)
        or not raw_decisions
    ):
        resolved_scope = _early_scope()
        detail_part = (
            model_policy.error or (receipt.get("state") if isinstance(receipt, Mapping) else None) or "malformed"
        )
        detail = f"{source_label}: {detail_part}"
        return _HubAdmissionOutcome(
            effective_roster=roster,
            scope=resolved_scope,
            admission=Observation("unknown", observed_at, resolved_scope, detail=detail),
            early_capacity_detail=detail,
        )

    # Preserve declared entries for denied or unselected seats so readiness never turns denials into worker-missing.
    effective_agents = {name: model_policy.roster.agents.get(name, agent) for name, agent in roster.agents.items()}
    effective_roster = replace(model_policy.roster, agents=effective_agents)
    resolved_scope = operation_scope(effective_roster, request, workspace) if scope is None else scope

    decisions_by_seat = {
        str(item["seat"]): item
        for item in raw_decisions
        if isinstance(item, Mapping) and isinstance(item.get("seat"), str)
    }
    enrollment = fleet_session_bootstrap.classify_enrollment(snapshot)
    # An enrolled launch rejects the LKG outright, so a seat denial read from the LKG
    # may be stale and is not the refusal the operator would actually hit.
    stale_lkg = enrollment == "enrolled" and source != "hub"
    admission_obs: Observation | None = None
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
        if seat_decision.get("outcome") != "enabled" and stale_lkg:
            admission_obs = Observation(
                "unknown",
                observed_at,
                resolved_scope,
                detail=(
                    f"{source_label}: seat {seat_name!r} is {seat_decision.get('outcome')} in the LKG, "
                    "and an enrolled launch requires live hub admission"
                ),
            )
            break
        if seat_decision.get("outcome") != "enabled":
            admission_obs = Observation(
                "denied",
                observed_at,
                resolved_scope,
                detail=(
                    f"{source_label}: seat {seat_name!r} denied "
                    f"({seat_decision.get('outcome')}: {seat_decision.get('detail')})"
                ),
            )
            break

    parsed_policy: Mapping[str, Any] | None = None
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
                        raw_doc,
                        fleet_session_bootstrap.CONSUMER_BRIGADE_RUN,
                        repo_identity,
                        _parsed=parsed_policy,
                    )
                    for seat_name in workers:
                        verdict = fleet_policy.admissible_seat(
                            raw_doc, seat_name, resolved_policy, _parsed=parsed_policy
                        )
                        if not verdict.get("admissible"):
                            reasons = ", ".join(str(item) for item in verdict.get("reasons") or ())
                            admission_obs = Observation(
                                "denied",
                                observed_at,
                                resolved_scope,
                                detail=f"{source_label} + fleet policy: seat {seat_name!r} denied ({reasons})",
                            )
                            break
            except (
                fleet_client_policy.FleetPolicyClientError,
                fleet_client.FleetClientError,
                fleet_policy.FleetPolicyError,
                subprocess.SubprocessError,
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
                if admission_obs is None:
                    if source != "hub":
                        admission_obs = Observation(
                            "unknown",
                            observed_at,
                            resolved_scope,
                            detail=f"{source_label} + fleet policy: enrolled launch requires live hub admission",
                        )
                    else:
                        admission_obs = Observation(
                            "passed",
                            observed_at,
                            resolved_scope,
                            detail=f"{source_label} + fleet policy",
                        )
        else:
            admission_obs = Observation("passed", observed_at, resolved_scope, detail=source_label)

    return _HubAdmissionOutcome(
        effective_roster=effective_roster,
        scope=resolved_scope,
        admission=admission_obs,
        snapshot=snapshot,
        source=source,
        enrollment=enrollment,
        parsed_policy=parsed_policy,
    )


def _hub_capacity(
    effective_roster: roster_mod.Roster,
    workers: tuple[str, ...],
    lock_state: str,
    workspace: Path,
    *,
    observed_at: float,
    scope: str,
    snapshot: Mapping[str, Any] | None,
    source: str | None,
    enrollment: str | None,
    parsed_policy: Mapping[str, Any] | None,
    early_capacity_detail: str | None,
) -> Observation:
    if early_capacity_detail is not None:
        return _capacity_or_unknown(
            lock_state,
            effective_roster.max_workers,
            early_capacity_detail,
            observed_at=observed_at,
            scope=scope,
        )

    if lock_state in {"live", "skipped", "invalid"}:
        return _local_lock_capacity_observation(
            lock_state, effective_roster.max_workers, observed_at=observed_at, scope=scope
        )

    if source != "hub":
        return Observation(
            "unknown",
            observed_at,
            scope,
            detail="hub capacity unverified while using hub model admission LKG",
        )

    claim_obs, claim_checked = _brigade_hub_claim_capacity(workspace, lock_state, observed_at=observed_at, scope=scope)
    if claim_obs is not None:
        return claim_obs

    if enrollment == "enrolled":
        if parsed_policy is not None:
            parsed_seats = parsed_policy.get("seats") or {}
            for seat_name in workers:
                seat_cfg = parsed_seats.get(seat_name)
                concurrency = seat_cfg.get("concurrency") if isinstance(seat_cfg, Mapping) else None
                if type(concurrency) is int and concurrency == 0:
                    return Observation(
                        "passed",
                        observed_at,
                        scope,
                        detail=f"fleet policy: seat {seat_name!r} concurrency is 0",
                        capacity=0,
                    )
        return Observation(
            "unknown",
            observed_at,
            scope,
            detail="hub capacity unverified: enrolled fleet policy enforces hub seat leases without a read-only usage endpoint",
        )

    seat_rows_by_name = {
        str(row["seat"]): row
        for row in (snapshot.get("seats") if snapshot is not None else None) or ()
        if isinstance(row, Mapping) and isinstance(row.get("seat"), str)
    }
    zero_seat: str | None = None
    malformed_seat: str | None = None
    positive_seat: tuple[str, int] | None = None
    for seat_name in workers:
        row = seat_rows_by_name.get(seat_name)
        if row is None:
            continue
        limit = row.get("limit")
        if limit is None:
            continue
        if type(limit) is not int or limit < 0:
            if malformed_seat is None:
                malformed_seat = seat_name
        elif limit == 0:
            if zero_seat is None:
                zero_seat = seat_name
        elif positive_seat is None:
            positive_seat = (seat_name, limit)

    if zero_seat is not None:
        return Observation(
            "passed",
            observed_at,
            scope,
            detail=f"hub model admission: seat {zero_seat!r} concurrency limit is 0",
            capacity=0,
        )
    if malformed_seat is not None:
        return Observation(
            "unknown",
            observed_at,
            scope,
            detail=f"hub model admission returned malformed limit for seat {malformed_seat!r}",
        )
    if positive_seat is not None:
        seat_name, limit = positive_seat
        return Observation(
            "unknown",
            observed_at,
            scope,
            detail=(
                f"hub capacity unverified: seat {seat_name!r} enforces concurrency limit {limit} "
                "without a read-only lease usage endpoint"
            ),
        )

    extra_source = (
        "hub repo claim and hub model seat limits (unlimited)" if claim_checked else "hub model seat limits (unlimited)"
    )
    return _local_lock_capacity_observation(
        lock_state,
        effective_roster.max_workers,
        observed_at=observed_at,
        scope=scope,
        extra_source=extra_source,
    )


def brigade_route_readiness(
    roster: roster_mod.Roster,
    request: ReadinessRequest,
    *,
    workspace: Path,
    scope: str | None = None,
    now: float | None = None,
) -> tuple[roster_mod.Roster, dict[str, Observation]]:
    """Return the effective roster and read-only ``admission`` and ``capacity`` observations.

    When ``scope`` is ``None`` it is computed from the effective (Hub-resolved)
    roster. A caller that supplies ``scope`` must compute it from that same
    effective roster, or the observations will not match the evaluated roster.
    """
    from . import fleet_client

    if request.route != "brigade":
        raise ValueError("brigade_route_readiness only supports route == 'brigade'")
    observed_at = time.time() if now is None else now
    workers = tuple(dict.fromkeys(request.workers))
    lock_state = runguard.inspect_run_lock_reconcile(workspace)

    try:
        settings = fleet_client.load_fleet_settings()
    except (fleet_client.FleetClientError, OSError, ValueError) as exc:
        resolved_scope = operation_scope(roster, request, workspace) if scope is None else scope
        detail = f"fleet configuration unavailable ({safe_detail(str(exc))})"
        return roster, {
            "admission": Observation("unknown", observed_at, resolved_scope, detail=detail),
            "capacity": _capacity_or_unknown(
                lock_state, roster.max_workers, detail, observed_at=observed_at, scope=resolved_scope
            ),
        }

    hub_url = str(settings.get("hub_url") or "").strip() if isinstance(settings, Mapping) else ""
    if not hub_url:
        resolved_scope = operation_scope(roster, request, workspace) if scope is None else scope
        return roster, _local_route_observations(
            roster, workers, lock_state, observed_at=observed_at, scope=resolved_scope
        )

    hub_admission = _hub_admission(
        roster,
        request,
        workers,
        hub_url,
        workspace,
        scope=scope,
        observed_at=observed_at,
    )
    capacity_obs = _hub_capacity(
        hub_admission.effective_roster,
        workers,
        lock_state,
        workspace,
        observed_at=observed_at,
        scope=hub_admission.scope,
        snapshot=hub_admission.snapshot,
        source=hub_admission.source,
        enrollment=hub_admission.enrollment,
        parsed_policy=hub_admission.parsed_policy,
        early_capacity_detail=hub_admission.early_capacity_detail,
    )
    return hub_admission.effective_roster, {
        "admission": hub_admission.admission,
        "capacity": capacity_obs,
    }


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
    if request.route != "brigade":
        raise ValueError("brigade_route_observations only supports route == 'brigade'")
    _, observations = brigade_route_readiness(
        roster,
        request,
        workspace=workspace,
        scope=scope,
        now=now,
    )
    return observations
