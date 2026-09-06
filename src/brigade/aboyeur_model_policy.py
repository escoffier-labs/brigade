"""Fleet model-policy resolution for :mod:`brigade.aboyeur`."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from . import fleet_model_admission, fleet_model_roster
from .roster import Agent, Roster, is_cli_allowed


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
    return snapshot.get("schema") == fleet_model_roster.ROSTER_SCHEMA


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


_NO_REASONING_SENTINEL = "none"


def _effective_reasoning(aboyeur: Any, cli_ref: str, policy_reasoning: str) -> str | None:
    """Hub reasoning is a required column: ``"none"`` is its placeholder, a blank
    string pins nothing, and only some adapters accept a pin at all. Each case
    leaves the launch spec unpinned; the admission receipt still carries the Hub's
    exact value, so the decision records whether the pin was applied."""
    if not _is_reasoning_pin(policy_reasoning):
        return None
    if not aboyeur.agents.supports_reasoning(cli_ref):
        return None
    return policy_reasoning


def _is_reasoning_pin(policy_reasoning: str) -> bool:
    stripped = policy_reasoning.strip()
    return bool(stripped) and stripped.lower() != _NO_REASONING_SENTINEL


def _reasoning_pin_unsupported(aboyeur: Any, cli_ref: str, policy_reasoning: str) -> bool:
    """True when a real Hub pin was dropped because the bound adapter takes none."""
    return _is_reasoning_pin(policy_reasoning) and not aboyeur.agents.supports_reasoning(cli_ref)


def _cli_from_binding(aboyeur: Any, instance_id: str) -> str | None:
    """Map a Hub Brigade binding onto a known adapter key."""
    if aboyeur.agents.is_known(instance_id):
        return instance_id
    aliases = {
        aboyeur.agents.command_for(name): name
        for name in ("cursor", "antigravity", "continue")
        if aboyeur.agents.is_known(name)
    }
    mapped = aliases.get(instance_id)
    return mapped if mapped is not None and aboyeur.agents.is_known(mapped) else None


def _local_cli_matches_hub_binding(aboyeur: Any, agent: Agent, binding: Mapping[str, Any]) -> bool:
    instance_id = str(binding.get("instance_id") or "")
    resolved = _cli_from_binding(aboyeur, instance_id)
    local = agent.cli or ""
    if not resolved or not local:
        return False
    if local == resolved:
        return True
    return aboyeur.agents.command_for(local) == instance_id


def _hub_seat_row(snapshot: Mapping[str, Any], seat_name: str) -> dict[str, Any] | None:
    """Return the Hub roster row for a seat, or None when the Hub carries no such row."""
    raw_seats = snapshot.get("seats")
    if not isinstance(raw_seats, list):
        return None
    for item in raw_seats:
        if isinstance(item, Mapping) and item.get("seat") == seat_name:
            return dict(item)
    return None


def synthesize_hub_worker(
    aboyeur: Any,
    roster: Roster,
    snapshot: Mapping[str, Any],
    seat_name: str,
) -> tuple[Agent | None, str | None]:
    """Build a hub-derived Agent for a seat absent from the local roster.

    Returns ``(agent, error)``: ``agent`` is set on success with the canonical
    slug as its model (``_resolve_versioned`` then applies
    ``effective_brigade_launch_model`` exactly as the local-seat path does);
    ``error`` names the ``brigade fleet models set ...`` remediation (or the
    ``limits.allow_models`` edit) when the Hub row exists but cannot be used;
    both are None when the Hub carries no such row. Callers only invoke this
    for seats missing locally, so a locally declared seat always wins. The
    native launch id is only ever read from an explicit binding leaf; nothing
    is inferred from the slug.
    """
    row = _hub_seat_row(snapshot, seat_name)
    if row is None:
        return None, None
    revision = snapshot.get("roster_revision", snapshot.get("revision"))
    provider = row.get("provider")
    canonical = row.get("model")
    if not isinstance(provider, str) or not provider or not isinstance(canonical, str) or not canonical:
        return None, (
            f"hub seat {seat_name!r} registry entry is missing exact provider/model "
            f"in roster revision {revision if isinstance(revision, int) else 'N'}; "
            f"repair it with: brigade fleet models set PROVIDER MODEL {seat_name} "
            f"--brigade-cli CLI --expect-revision {revision if isinstance(revision, int) else 'N'}"
        )
    if row.get("enabled") is not True:
        return None, (
            f"hub seat {seat_name!r} is disabled in roster revision "
            f"{revision if isinstance(revision, int) else 'N'}; enable it with:"
            + _set_remediation_detail(row, revision, fallback_seat=seat_name)
        )
    launch_groups = fleet_model_roster.consumer_launch_groups(snapshot, "brigade-run", seat_name)
    binding = fleet_model_admission._binding_for("brigade-run", row, launch_groups=launch_groups)
    raw_cli = binding.get("instance_id") if isinstance(binding, Mapping) else None
    if not isinstance(raw_cli, str) or not raw_cli:
        return None, (
            f"hub seat {seat_name!r} has no brigade-run cli binding "
            f"in roster revision {revision if isinstance(revision, int) else 'N'}; "
            "nothing to launch without an explicit binding leaf (the native id is never "
            "inferred from the slug); bind it with:" + _set_remediation_detail(row, revision, fallback_seat=seat_name)
        )
    resolved_cli = _cli_from_binding(aboyeur, raw_cli)
    if resolved_cli is None:
        return None, (
            f"hub seat {seat_name!r} binds {raw_cli!r}, which is not a supported Brigade harness; rebind it with:"
            + _set_remediation_detail(row, revision, fallback_seat=seat_name)
        )
    if not is_cli_allowed(resolved_cli, roster):
        detail = _set_remediation_detail(row, revision, fallback_seat=seat_name)
        return None, (
            f"hub seat {seat_name!r} uses {resolved_cli!r}, which is not allowed by limits.allow_models "
            f"({', '.join(roster.allow_models)}); add {resolved_cli!r} to limits.allow_models in "
            f"{roster.resolution.path if roster.resolution is not None else 'the local roster'}"
            + (f"; or rebind the Hub row:{detail}" if detail else "")
        )
    reasoning = row.get("reasoning")
    return (
        Agent(
            name=seat_name,
            cli=resolved_cli,
            role=f"hub-derived worker ({provider}/{canonical})",
            model=canonical,
            reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
        ),
        None,
    )


def _set_remediation_detail(
    row: Mapping[str, Any],
    revision: object,
    *,
    fallback_seat: str | None = None,
    brigade_model: str | None = None,
) -> str:
    """Return a remediation block for a versioned roster seat row."""
    provider = row.get("provider")
    model = row.get("model")
    seat = row.get("seat") or fallback_seat
    reasoning = row.get("reasoning")
    if not isinstance(provider, str) or not isinstance(model, str) or not isinstance(seat, str):
        return ""
    return "\n" + fleet_model_admission._set_command_remediation(
        provider=provider,
        model=model,
        seat=seat,
        reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
        brigade_cli=fleet_model_admission._brigade_cli_from_row(row),
        t3_instance_id=fleet_model_admission._t3_instance_id_from_row(row),
        t3_service_tier=fleet_model_admission._t3_service_tier_from_row(row),
        revision=revision,
        brigade_model=brigade_model,
    )


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
    raw_snapshot = dict(snapshot) if snapshot is not None else aboyeur.fleet_client.load_model_policy_snapshot()
    cleaned_override: str | None = None
    if model_override is not None:
        cleaned_override = model_override.strip()
        if worker is None:
            return aboyeur.FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error="--model requires --worker so the overridden seat is unambiguous",
            )
        agent = effective.agents.get(worker)
        if agent is None:
            # Validation-only synthesis: _resolve_versioned performs the real
            # admission below, so error-path rosters never carry an unadmitted
            # hub-derived seat.
            hub_error: str | None = None
            if _is_versioned_snapshot(raw_snapshot):
                hub_agent, hub_error = synthesize_hub_worker(aboyeur, effective, raw_snapshot, worker)
                if hub_agent is not None:
                    agent = hub_agent
            if agent is None:
                return aboyeur.FleetModelPolicyResolution(
                    roster=roster,
                    receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                    error=hub_error or f"unknown worker: {worker}",
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

    if _is_versioned_snapshot(raw_snapshot):
        return _resolve_versioned(
            aboyeur,
            effective,
            raw_snapshot,
            worker=worker,
            model_override=cleaned_override,
        )
    if cleaned_override is not None and worker is not None:
        agent = roster.agents[worker]
        updated_agents = dict(roster.agents)
        updated_agents[worker] = replace(agent, model=cleaned_override)
        effective = replace(roster, agents=updated_agents)

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
    if state == "node-token-required":
        return aboyeur.FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy requires a node token; refusing new dispatch",
        )
    if state == "unsupported-schema":
        return aboyeur.FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy hub returned an unsupported roster schema; refusing new dispatch",
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
    digest = raw_snapshot.get("document_sha256")
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
    authority, authority_error = fleet_model_roster.parse_fleet_policy_authority(raw_snapshot.get("fleet_policy"))
    if authority_error is not None:
        receipt["state"] = "denied"
        receipt["authoritative"] = False
        return aboyeur.FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy authority metadata is malformed; refusing new dispatch",
        )
    if authority is not None:
        receipt["fleet_policy"] = authority
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

    raw_retired = raw_snapshot.get("retired_models")
    retired_rows = (
        [dict(row) for row in raw_retired if isinstance(row, Mapping)] if isinstance(raw_retired, list) else []
    )
    seat_rows = {row.get("seat"): row for row in seats if isinstance(row.get("seat"), str) and row.get("seat")}
    if worker is not None and worker not in effective.agents:
        hub_agent, hub_error = synthesize_hub_worker(aboyeur, effective, raw_snapshot, worker)
        if hub_agent is not None:
            effective = replace(effective, agents={**effective.agents, worker: hub_agent})
        else:
            receipt["decisions"] = []
            receipt["admissions"] = []
            if hub_error is not None:
                return aboyeur.FleetModelPolicyResolution(
                    roster=effective,
                    receipt=receipt,
                    error=hub_error,
                )
            return aboyeur.FleetModelPolicyResolution(
                roster=effective,
                receipt=receipt,
                error=(
                    f"unknown worker: {worker} (absent from the local roster and from Hub roster "
                    f"revision {revision}; see current rows: brigade fleet models list --seat {worker})"
                ),
            )
    kept_agents = dict(effective.agents)
    decisions: list[dict[str, object]] = []
    denied: dict[str, str] = {}
    admissions: list[dict[str, object]] = []
    cleaned_override = model_override.strip() if model_override is not None else None
    for seat, agent in effective.agents.items():
        requested_provider = aboyeur.agents.model_policy_provider(agent.cli or "")
        requested_model = aboyeur.agents.model_policy_model(
            agent.cli or "", cleaned_override if seat == worker and cleaned_override is not None else agent.model
        )
        row = seat_rows.get(seat)
        decision: dict[str, object] = {
            "kind": "fleet-model-policy",
            "seat": seat,
            "requested_provider": requested_provider,
            "requested_model": requested_model,
        }
        if row is None:
            outcome = "omitted"
            detail = "seat is absent from the authoritative registry"
        else:
            policy_provider = row.get("provider")
            policy_model = row.get("model")
            policy_reasoning = row.get("reasoning")
            launch_groups = fleet_model_roster.consumer_launch_groups(raw_snapshot, "brigade-run", seat)
            launch_model = (
                fleet_model_roster.effective_brigade_launch_model(row, launch_groups=launch_groups) or policy_model
            )
            decision["policy_provider"] = policy_provider
            decision["policy_model"] = policy_model
            decision["launch_model"] = launch_model
            decision["policy_enabled"] = row.get("enabled") is True
            binding = fleet_model_admission._binding_for("brigade-run", row, launch_groups=launch_groups)
            policy_floor = None
            for identity in fleet_model_roster.binding_launch_models(row, launch_groups=launch_groups):
                policy_floor = fleet_model_roster.retired_reason(
                    str(policy_provider or ""), identity, retired_rows or None
                )
                if policy_floor is not None:
                    break
            allowed_models = {item for item in (policy_model, launch_model) if isinstance(item, str) and item}
            if policy_floor is not None:
                outcome = "retired"
                detail = policy_floor
            elif not isinstance(policy_provider, str) or not isinstance(policy_model, str):
                outcome = "mismatch"
                detail = "registry entry is missing exact provider/model"
            elif cleaned_override is not None and seat == worker and cleaned_override not in allowed_models:
                outcome = "mismatch"
                detail = f"--model {cleaned_override!r} does not match Hub model {policy_model!r}"
                # Re-running `set` with the canonical slug would only re-pin the
                # slug the adapter already rejects. Name the native binding fix.
                detail += _set_remediation_detail(row, revision, fallback_seat=seat, brigade_model=cleaned_override)
            elif not isinstance(policy_reasoning, str) or not policy_reasoning:
                outcome = "binding-missing"
                detail = "registry entry is missing exact reasoning"
                detail += _set_remediation_detail(row, revision, fallback_seat=seat)
            elif binding is None or not isinstance(binding.get("instance_id"), str) or not binding["instance_id"]:
                outcome = "binding-missing"
                detail = "registry entry is missing consumer binding"
                detail += _set_remediation_detail(row, revision, fallback_seat=seat)
            elif row.get("enabled") is not True:
                outcome = "disabled"
                detail = "registry entry is disabled"
            elif agent.command:
                outcome = "custom-command"
                detail = "custom Agent command is not permitted under Hub provenance"
            else:
                resolved_cli = _cli_from_binding(aboyeur, str(binding["instance_id"]))
                if resolved_cli is None:
                    outcome = "unsupported-binding"
                    detail = "registry binding is not a supported Brigade harness"
                elif aboyeur.agents.model_policy_provider(resolved_cli) != policy_provider:
                    outcome = "inconsistent-binding"
                    detail = (
                        f"registry binding {binding['instance_id']!r} is inconsistent with provider {policy_provider!r}"
                    )
                elif not _local_cli_matches_hub_binding(aboyeur, agent, binding):
                    outcome = "inconsistent-binding"
                    detail = f"local CLI {agent.cli!r} does not match Hub binding {binding['instance_id']!r}"
                else:
                    outcome = "enabled"
                    detail = "exact seat/provider/model/reasoning/binding entry is enabled"
                    effective_reasoning = _effective_reasoning(aboyeur, resolved_cli, policy_reasoning)
                    decision["reasoning_applied"] = effective_reasoning is not None
                    if _reasoning_pin_unsupported(aboyeur, resolved_cli, policy_reasoning):
                        detail = f"{detail}; reasoning pin not supported by {resolved_cli}"
                    keep_env = _local_cli_matches_hub_binding(aboyeur, agent, binding)
                    kept_agents[seat] = replace(
                        agent,
                        cli=resolved_cli,
                        model=launch_model if isinstance(launch_model, str) else policy_model,
                        reasoning=effective_reasoning,
                        command=None,
                        env=agent.env if keep_env else None,
                    )
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
