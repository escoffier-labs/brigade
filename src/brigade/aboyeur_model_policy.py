"""Fleet model-policy resolution for :mod:`brigade.aboyeur`."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from .roster import Roster


def _aboyeur():
    """Resolve the parent lazily so its public compatibility API can import us."""
    from . import aboyeur

    return aboyeur


def resolve_fleet_model_policy(
    roster: Roster,
    *,
    worker: str | None = None,
    model_override: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> Any:
    """Resolve one immutable Fleet Hub policy snapshot into an effective roster."""
    aboyeur = _aboyeur()
    effective = roster
    if model_override is not None:
        cleaned_override = model_override.strip()
        if worker is None:
            return aboyeur.FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error="--model requires --worker so the overridden seat is unambiguous",
            )
        agent = roster.agents.get(worker)
        if agent is None:
            return aboyeur.FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error=f"unknown worker: {worker}",
            )
        if not cleaned_override:
            return aboyeur.FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error="--model must be a non-empty model identifier",
            )
        if agent.cli is None or not aboyeur.agents.supports_model_pinning(agent.cli):
            cli = agent.cli or "unconfigured"
            return aboyeur.FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error=f"seat {worker!r} uses {cli!r}, which does not support a per-run model override",
            )
        updated_agents = dict(roster.agents)
        updated_agents[worker] = replace(agent, model=cleaned_override)
        effective = replace(roster, agents=updated_agents)

    raw_snapshot = dict(snapshot) if snapshot is not None else aboyeur.fleet_client.load_model_policy_snapshot()
    state = raw_snapshot.get("state") if isinstance(raw_snapshot.get("state"), str) else "unavailable"
    raw_models = raw_snapshot.get("models")
    models = [dict(row) for row in raw_models if isinstance(row, Mapping)] if isinstance(raw_models, list) else []
    receipt: dict[str, object] = {
        "schema": "brigade.fleet_model_policy.v1",
        "state": state,
        "authoritative": state == "authoritative",
        "models": models,
        "decisions": [],
    }
    if model_override is not None:
        receipt["model_override"] = model_override.strip()
        receipt["model_override_seat"] = worker
    if state == "unconfigured":
        return aboyeur.FleetModelPolicyResolution(roster=effective, receipt=receipt)
    if state == "unavailable":
        return aboyeur.FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy hub is unavailable; refusing new dispatch",
        )
    if state == "auth-failed":
        return aboyeur.FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy could not authenticate this node; refusing new dispatch",
        )
    if state != "authoritative":
        return aboyeur.FleetModelPolicyResolution(roster=effective, receipt=receipt)

    seat_rows = {row.get("seat"): row for row in models if isinstance(row.get("seat"), str) and row.get("seat")}
    disabled_providers = {
        row.get("provider")
        for row in models
        if row.get("seat") is None and row.get("enabled") is False and isinstance(row.get("provider"), str)
    }
    kept_agents = dict(effective.agents)
    decisions: list[dict[str, object]] = []
    denied: dict[str, str] = {}
    for seat, agent in effective.agents.items():
        provider = aboyeur.agents.model_policy_provider(agent.cli or "")
        model = aboyeur.agents.model_policy_model(agent.cli or "", agent.model)
        row = seat_rows.get(seat)
        decision: dict[str, object] = {
            "kind": "fleet-model-policy",
            "seat": seat,
            "requested_provider": provider,
            "requested_model": model,
        }
        if provider in disabled_providers:
            outcome = "provider-disabled"
            detail = f"provider {provider!r} is disabled"
        elif row is None:
            outcome = "omitted"
            detail = "seat is absent from the authoritative registry"
        else:
            policy_provider = row.get("provider")
            policy_model = row.get("model")
            decision["policy_provider"] = policy_provider
            decision["policy_model"] = policy_model
            decision["policy_enabled"] = row.get("enabled") is True
            if policy_provider != provider or policy_model != model:
                outcome = "mismatch"
                detail = f"requested {provider}/{model}, registry allows {policy_provider}/{policy_model}"
            elif row.get("enabled") is not True:
                outcome = "disabled"
                detail = "registry entry is disabled"
            else:
                outcome = "enabled"
                detail = "exact seat/provider/model entry is enabled"
        decision["outcome"] = outcome
        decision["detail"] = detail
        decisions.append(decision)
        if outcome != "enabled":
            denied[seat] = detail
            if seat != effective.orchestrator:
                kept_agents.pop(seat, None)

    receipt["decisions"] = decisions
    required_seat = worker or effective.orchestrator
    if required_seat in denied:
        detail = denied[required_seat]
        return aboyeur.FleetModelPolicyResolution(
            roster=replace(effective, agents=kept_agents),
            receipt=receipt,
            error=f"fleet model policy denied seat {required_seat!r}: {detail}",
        )
    routing = tuple(
        {
            "requested_seat": decision["seat"],
            "effective_seat": decision["seat"] if decision["outcome"] == "enabled" else None,
            "outcome": decision["outcome"],
            "typed_cause": "fleet-model-policy",
            "requested_provider": decision["requested_provider"],
            "requested_model": decision["requested_model"],
        }
        for decision in decisions
    )
    return aboyeur.FleetModelPolicyResolution(
        roster=replace(effective, agents=kept_agents, seat_routing=(*effective.seat_routing, *routing)),
        receipt=receipt,
    )
