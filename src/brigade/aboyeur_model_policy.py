"""Fleet model-policy resolution for :mod:`brigade.aboyeur`."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from . import fleet_model_admission, fleet_model_roster
from .roster import Agent, Roster


def _aboyeur():
    """Resolve the parent lazily so its public compatibility API can import us."""
    from . import aboyeur

    return aboyeur


def _parse_expiry(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_versioned_snapshot(snapshot: Mapping[str, Any]) -> bool:
    if snapshot.get("schema") == fleet_model_roster.ROSTER_SCHEMA:
        return True
    return isinstance(snapshot.get("seats"), list) and isinstance(snapshot.get("roster_digest"), str)


def _admission_record(
    *,
    source: str,
    revision: int,
    digest: str,
    seat: str,
    provider: str,
    model: str,
    reasoning: str,
    binding: Mapping[str, Any],
    expires_at: object,
) -> dict[str, object]:
    return {
        "schema": fleet_model_roster.ADMISSION_SCHEMA,
        "source": source,
        "roster_revision": revision,
        "roster_digest": digest,
        "seat": seat,
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "binding": dict(binding),
        "expires_at": expires_at,
    }


def _strip_pruned_fallbacks(agents: dict[str, Agent], pruned: set[str]) -> dict[str, Agent]:
    cleaned: dict[str, Agent] = {}
    for name, agent in agents.items():
        fallback = tuple(item for item in agent.fallback if item not in pruned)
        final = agent.invalid_final_fallback
        if final in pruned:
            agent = replace(agent, invalid_final_fallback=None)
        cleaned[name] = replace(agent, fallback=fallback) if fallback != agent.fallback else agent
    return cleaned


def _permanent_floor(provider: object, model: object) -> str | None:
    if not isinstance(provider, str) or not isinstance(model, str):
        return None
    return fleet_model_roster.retired_reason(provider, model)


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
    if _is_versioned_snapshot(raw_snapshot):
        return _resolve_versioned(
            aboyeur,
            effective,
            raw_snapshot,
            worker=worker,
            model_override=model_override,
        )

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
        floor = _permanent_floor(provider, model)
        if floor is not None:
            outcome = "retired"
            detail = floor
        elif provider in disabled_providers:
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
            policy_floor = _permanent_floor(policy_provider, policy_model)
            if policy_floor is not None:
                outcome = "retired"
                detail = policy_floor
            elif policy_provider != provider or policy_model != model:
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

    kept_agents = _strip_pruned_fallbacks(kept_agents, set(denied) - {effective.orchestrator})
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


def _resolve_versioned(
    aboyeur: Any,
    effective: Roster,
    raw_snapshot: Mapping[str, Any],
    *,
    worker: str | None,
    model_override: str | None,
) -> Any:
    state = raw_snapshot.get("state") if isinstance(raw_snapshot.get("state"), str) else "authoritative"
    raw_source = raw_snapshot.get("source")
    source = raw_source if isinstance(raw_source, str) and raw_source else "hub"
    revision = raw_snapshot.get("roster_revision", raw_snapshot.get("revision"))
    digest = raw_snapshot.get("roster_digest")
    expires_at = raw_snapshot.get("expires_at")
    raw_seats = raw_snapshot.get("seats")
    seats = [dict(row) for row in raw_seats if isinstance(row, Mapping)] if isinstance(raw_seats, list) else []
    models = [
        {
            "seat": row.get("seat"),
            "provider": row.get("provider"),
            "model": row.get("model"),
            "enabled": row.get("enabled") is True,
            "limit": row.get("limit"),
            "notes": row.get("notes"),
        }
        for row in seats
        if isinstance(row.get("seat"), str) and row.get("seat")
    ]
    receipt: dict[str, object] = {
        "schema": "brigade.fleet_model_policy.v1",
        "state": state,
        "authoritative": state == "authoritative",
        "source": source,
        "roster_revision": revision,
        "roster_digest": digest,
        "expires_at": expires_at,
        "models": models,
        "decisions": [],
        "admissions": [],
    }
    if model_override is not None:
        receipt["model_override"] = model_override.strip()
        receipt["model_override_seat"] = worker
    if source == "lkg":
        expiry = _parse_expiry(expires_at)
        if expiry is None or expiry <= datetime.now(timezone.utc):
            receipt["state"] = "denied"
            receipt["authoritative"] = False
            return aboyeur.FleetModelPolicyResolution(
                roster=effective,
                receipt=receipt,
                error="fleet model policy LKG is expired or invalid; refusing new dispatch",
            )
    if state == "unconfigured":
        return aboyeur.FleetModelPolicyResolution(roster=effective, receipt=receipt)
    if state != "authoritative":
        return aboyeur.FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy hub is unavailable; refusing new dispatch",
        )
    if type(revision) is not int or not isinstance(digest, str):
        return aboyeur.FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy snapshot is malformed; refusing new dispatch",
        )

    seat_rows = {row.get("seat"): row for row in seats if isinstance(row.get("seat"), str) and row.get("seat")}
    kept_agents = dict(effective.agents)
    decisions: list[dict[str, object]] = []
    denied: dict[str, str] = {}
    admissions: list[dict[str, object]] = []
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
        floor = _permanent_floor(provider, model)
        if floor is not None:
            outcome = "retired"
            detail = floor
        elif row is None:
            outcome = "omitted"
            detail = "seat is absent from the authoritative registry"
        else:
            policy_provider = row.get("provider")
            policy_model = row.get("model")
            policy_reasoning = row.get("reasoning")
            decision["policy_provider"] = policy_provider
            decision["policy_model"] = policy_model
            decision["policy_enabled"] = row.get("enabled") is True
            binding = fleet_model_admission._binding_for("brigade-run", row)
            policy_floor = _permanent_floor(policy_provider, policy_model)
            if policy_floor is not None:
                outcome = "retired"
                detail = policy_floor
            elif not isinstance(policy_provider, str) or not isinstance(policy_model, str):
                outcome = "mismatch"
                detail = "registry entry is missing exact provider/model"
            elif policy_provider != provider or policy_model != model:
                outcome = "mismatch"
                detail = f"requested {provider}/{model}, registry allows {policy_provider}/{policy_model}"
            elif not isinstance(policy_reasoning, str) or not policy_reasoning:
                outcome = "binding-missing"
                detail = "registry entry is missing exact reasoning"
            elif agent.reasoning is not None and agent.reasoning != policy_reasoning:
                outcome = "mismatch"
                detail = f"requested reasoning {agent.reasoning!r}, registry allows {policy_reasoning!r}"
            elif binding is None or not isinstance(binding.get("instance_id"), str) or not binding["instance_id"]:
                outcome = "binding-missing"
                detail = "registry entry is missing consumer binding"
            elif row.get("enabled") is not True:
                outcome = "disabled"
                detail = "registry entry is disabled"
            else:
                outcome = "enabled"
                detail = "exact seat/provider/model/reasoning/binding entry is enabled"
                admission = _admission_record(
                    source=source,
                    revision=revision,
                    digest=digest,
                    seat=seat,
                    provider=policy_provider,
                    model=policy_model,
                    reasoning=policy_reasoning,
                    binding=binding,
                    expires_at=expires_at,
                )
                decision["model_admission"] = admission
                admissions.append(admission)
        decision["outcome"] = outcome
        decision["detail"] = detail
        decisions.append(decision)
        if outcome != "enabled":
            denied[seat] = detail
            if seat != effective.orchestrator:
                kept_agents.pop(seat, None)

    kept_agents = _strip_pruned_fallbacks(kept_agents, set(denied) - {effective.orchestrator})
    receipt["decisions"] = decisions
    receipt["admissions"] = admissions
    required_seat = worker or effective.orchestrator
    required_admission = next((item for item in admissions if item["seat"] == required_seat), None)
    if required_admission is not None:
        receipt["model_admission"] = required_admission
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
            **({"model_admission": decision["model_admission"]} if "model_admission" in decision else {}),
        }
        for decision in decisions
    )
    return aboyeur.FleetModelPolicyResolution(
        roster=replace(effective, agents=kept_agents, seat_routing=(*effective.seat_routing, *routing)),
        receipt=receipt,
    )
