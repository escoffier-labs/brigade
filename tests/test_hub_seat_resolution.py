"""Hub-only seat resolution: `brigade run --worker <seat>` from the Hub roster row.

When the local roster does not declare a seat but a versioned Hub roster does,
the seat synthesizes from the Hub row (cli via the consumer projection first,
then the row; model stays the canonical slug until dispatch applies the native
launch id). Refusals name the `brigade fleet models set ...` remediation or
the allow_models edit, never a generic unknown worker. No network: every Hub
snapshot here is a fixture passed explicitly (or a mocked snapshot read).
"""

from __future__ import annotations

import json

import pytest

from brigade import aboyeur, aboyeur_model_policy, agents, fleet_client, fleet_model_roster
from brigade.aboyeur.planning import parse_plan
from brigade.cli import run as run_cli
from brigade.roster import Agent, Roster

SEAT = "muse13"
PROVIDER = "opencode"
CANONICAL = "opencode-muse-spark-1.3-contributor-free"
NATIVE = "opencode/muse-spark-1.3-contributor-free"
DIGEST = "sha256:" + ("ab" * 32)


def _row(**overrides) -> dict[str, object]:
    row: dict[str, object] = {
        "seat": SEAT,
        "provider": PROVIDER,
        "model": CANONICAL,
        "reasoning": "high",
        "enabled": True,
        "bindings": {
            "brigade": {"cli": "opencode"},
            "t3_fleet": {"instance_id": "opencode", "service_tier": None},
        },
    }
    row.update(overrides)
    return row


def _snapshot(*, seats=None, native: str | None = NATIVE, **overrides) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "state": "authoritative",
        "source": "hub",
        "revision": 27,
        "roster_revision": 27,
        "document_sha256": DIGEST,
        "expires_at": "2099-08-30T14:15:00Z",
        "seats": [_row()] if seats is None else seats,
        "consumer_defaults": {"brigade-run": SEAT},
        "retired_models": [],
    }
    if native is not None:
        snapshot["consumer_launch_bindings"] = {
            "brigade-run": {
                SEAT: {
                    "brigade": {"cli": "opencode", "model": native},
                    "t3_fleet": {},
                    "native": {},
                }
            }
        }
    snapshot.update(overrides)
    return snapshot


def _roster_without_seat(**kwargs) -> Roster:
    return Roster(
        orchestrator="chef",
        agents={"chef": Agent("chef", "claude", "plan", model="opus-5")},
        **kwargs,
    )


def test_hub_only_seat_resolves_and_admits_canonical_slug():
    resolution = aboyeur.resolve_fleet_model_policy(_roster_without_seat(), worker=SEAT, snapshot=_snapshot())
    assert resolution.error is None
    agent = resolution.roster.agents[SEAT]
    assert agent.cli == "opencode"
    assert agent.model == NATIVE
    assert "hub-derived" in agent.role
    admission = resolution.receipt["model_admission"]
    assert admission["seat"] == SEAT
    assert admission["model"] == CANONICAL
    assert admission["binding"]["model"] == NATIVE
    assert admission["binding"]["instance_id"] == "opencode"
    routing = next(item for item in resolution.roster.seat_routing if item["requested_seat"] == SEAT)
    assert routing["outcome"] == "enabled"


def test_hub_only_seat_dispatch_argv_uses_native_launch_id():
    resolution = aboyeur.resolve_fleet_model_policy(_roster_without_seat(), worker=SEAT, snapshot=_snapshot())
    assert resolution.error is None
    agent = resolution.roster.agents[SEAT]
    assert agents.build_argv("opencode", "do work", model=agent.model) == [
        "opencode",
        "run",
        "-m",
        NATIVE,
        "do work",
    ]
    assert "models list" not in json.dumps(resolution.receipt)


def test_receipt_models_rows_pass_through_unchanged():
    snapshot = _snapshot()
    resolution = aboyeur.resolve_fleet_model_policy(_roster_without_seat(), worker=SEAT, snapshot=snapshot)
    assert resolution.error is None
    assert resolution.receipt["models"] == [
        {
            "seat": SEAT,
            "provider": PROVIDER,
            "model": CANONICAL,
            "enabled": True,
            "limit": None,
            "notes": None,
        }
    ]


def test_hub_only_seat_outside_allow_models_refused_with_remediation():
    roster = _roster_without_seat(allow_models=("claude",))
    resolution = aboyeur.resolve_fleet_model_policy(roster, worker=SEAT, snapshot=_snapshot())
    assert resolution.error is not None
    assert "limits.allow_models" in resolution.error
    assert "'opencode'" in resolution.error
    assert "unknown worker" not in resolution.error


def test_disabled_hub_row_refused_with_set_remediation():
    snapshot = _snapshot(seats=[_row(enabled=False)])
    resolution = aboyeur.resolve_fleet_model_policy(_roster_without_seat(), worker=SEAT, snapshot=snapshot)
    assert resolution.error is not None
    assert "disabled" in resolution.error
    assert f"brigade fleet models set {PROVIDER} {CANONICAL} {SEAT}" in resolution.error
    assert SEAT not in resolution.roster.agents


def test_missing_binding_cli_refused_with_set_remediation():
    row = _row()
    row["bindings"] = {"brigade": {}, "t3_fleet": {"instance_id": "", "service_tier": None}}
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster_without_seat(), worker=SEAT, snapshot=_snapshot(seats=[row], native=None)
    )
    assert resolution.error is not None
    assert "--brigade-cli" in resolution.error
    assert f"brigade fleet models set {PROVIDER} {CANONICAL} {SEAT}" in resolution.error


def test_hub_unavailable_refused_honestly_without_guessing():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster_without_seat(), worker=SEAT, snapshot={"state": "unavailable", "models": []}
    )
    assert resolution.error is not None
    assert "unavailable" in resolution.error
    assert SEAT not in resolution.roster.agents


def test_expired_lkg_refused_honestly_without_guessing():
    snapshot = _snapshot(source="lkg", expires_at="2000-01-01T00:00:00Z")
    resolution = aboyeur.resolve_fleet_model_policy(_roster_without_seat(), worker=SEAT, snapshot=snapshot)
    assert resolution.error is not None
    assert "LKG" in resolution.error
    assert SEAT not in resolution.roster.agents


def test_absent_hub_row_keeps_unknown_worker_with_list_pointer():
    snapshot = _snapshot(seats=[])
    resolution = aboyeur.resolve_fleet_model_policy(_roster_without_seat(), worker="ghost", snapshot=snapshot)
    assert resolution.error is not None
    assert "unknown worker: ghost" in resolution.error
    assert "brigade fleet models list --seat ghost" in resolution.error


def test_locally_declared_seat_wins_over_hub_row():
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            SEAT: Agent(SEAT, "opencode", "code", model=CANONICAL),
        },
    )
    resolution = aboyeur.resolve_fleet_model_policy(roster, worker=SEAT, snapshot=_snapshot())
    assert resolution.error is None
    agent = resolution.roster.agents[SEAT]
    assert agent.role == "code"
    assert "hub-derived" not in agent.role


def test_local_hub_disagreement_keeps_existing_mismatch_behavior():
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            SEAT: Agent(SEAT, "opencode", "code", model=CANONICAL),
        },
    )
    row = _row()
    row["bindings"] = {"brigade": {"cli": "codex"}, "t3_fleet": {"instance_id": "", "service_tier": None}}
    resolution = aboyeur.resolve_fleet_model_policy(roster, worker=SEAT, snapshot=_snapshot(seats=[row], native=None))
    assert resolution.error is not None
    assert "inconsistent with provider" in resolution.error


def test_model_override_on_hub_only_seat_admits_bound_native():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster_without_seat(), worker=SEAT, model_override=NATIVE, snapshot=_snapshot()
    )
    assert resolution.error is None
    assert resolution.roster.agents[SEAT].model == NATIVE


def test_model_override_on_hub_only_seat_denial_names_native_fix():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster_without_seat(), worker=SEAT, model_override="other-model", snapshot=_snapshot(native=None)
    )
    assert resolution.error is not None
    assert "does not match Hub model" in resolution.error
    assert "--brigade-model other-model" in resolution.error


def test_native_id_never_inferred_from_slug():
    row = _row()
    assert fleet_model_roster.effective_brigade_launch_model(row) is None
    agent, error = aboyeur_model_policy.synthesize_hub_worker(
        aboyeur, _roster_without_seat(), _snapshot(native=None), SEAT
    )
    assert error is None
    assert agent is not None
    assert agent.model == CANONICAL


def test_direct_worker_error_accepts_hub_only_seat(monkeypatch):
    from brigade import roster as roster_mod

    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda **kwargs: _snapshot())
    roster = _roster_without_seat()
    assert run_cli._direct_worker_error(SEAT, roster, roster_mod) is None


def test_direct_worker_error_refuses_when_hub_unavailable(monkeypatch):
    from brigade import roster as roster_mod

    monkeypatch.setattr(
        fleet_client, "load_model_policy_snapshot", lambda **kwargs: {"state": "unavailable", "models": []}
    )
    roster = _roster_without_seat()
    error = run_cli._direct_worker_error(SEAT, roster, roster_mod)
    assert error is not None
    assert "unavailable" in error
    assert "unknown worker" not in error


def test_direct_worker_error_keeps_unknown_worker_without_hub(monkeypatch):
    from brigade import roster as roster_mod

    monkeypatch.setattr(
        fleet_client, "load_model_policy_snapshot", lambda **kwargs: {"state": "unconfigured", "models": []}
    )
    roster = _roster_without_seat()
    assert run_cli._direct_worker_error("ghost", roster, roster_mod) == "unknown worker: ghost"


def test_direct_worker_error_surfaces_hub_refusal(monkeypatch):
    from brigade import roster as roster_mod

    monkeypatch.setattr(
        fleet_client, "load_model_policy_snapshot", lambda **kwargs: _snapshot(seats=[_row(enabled=False)])
    )
    roster = _roster_without_seat()
    error = run_cli._direct_worker_error(SEAT, roster, roster_mod)
    assert error is not None
    assert f"brigade fleet models set {PROVIDER} {CANONICAL} {SEAT}" in error


def test_assignment_naming_hub_seat_validates_on_effective_roster():
    resolution = aboyeur.resolve_fleet_model_policy(_roster_without_seat(), worker=SEAT, snapshot=_snapshot())
    assert resolution.error is None
    assignments = parse_plan(
        json.dumps({"assignments": [{"stage": 1, "worker": SEAT, "task": "do work"}]}),
        resolution.roster,
    )
    assert [item.worker for item in assignments] == [SEAT]
    with pytest.raises(ValueError, match="unknown worker"):
        parse_plan(
            json.dumps({"assignments": [{"stage": 1, "worker": "ghost", "task": "do work"}]}),
            resolution.roster,
        )
