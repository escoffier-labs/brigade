from __future__ import annotations

import json
import urllib.error

from brigade import aboyeur
from brigade import cli
from brigade import fleet_client
from brigade.roster import Agent, Roster
from tests.run_test_helpers import run_aboyeur_guarded


def _roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "cursor_grok": Agent("cursor_grok", "cursor", "code", model="cursor-grok-4.6-high-fast"),
            "cursor_composer": Agent("cursor_composer", "cursor", "code", model="composer-2.5"),
            "cursor_spare": Agent("cursor_spare", "cursor", "code", model="kimi-k2.7"),
        },
    )


def _row(seat: str, provider: str, model: str, *, enabled: bool = True) -> dict[str, object]:
    return {
        "seat": seat,
        "provider": provider,
        "model": model,
        "enabled": enabled,
        "limit": None,
        "notes": None,
    }


def test_authoritative_policy_keeps_distinct_cursor_pins_and_filters_unlisted_seats():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        snapshot={
            "state": "authoritative",
            "models": [
                _row("chef", "anthropic", "opus-5"),
                _row("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
                _row("cursor_composer", "cursor", "composer-2.5"),
            ],
        },
    )

    assert resolution.error is None
    assert set(resolution.roster.agents) == {"chef", "cursor_grok", "cursor_composer"}
    assert resolution.receipt["state"] == "authoritative"
    decisions = {item["seat"]: item for item in resolution.receipt["decisions"]}
    assert decisions["cursor_grok"]["outcome"] == "enabled"
    assert decisions["cursor_composer"]["outcome"] == "enabled"
    assert decisions["cursor_spare"]["outcome"] == "omitted"


def test_direct_worker_model_override_requires_exact_enabled_registry_entry():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        model_override="composer-2.5",
        snapshot={
            "state": "authoritative",
            "models": [
                _row("cursor_grok", "cursor", "composer-2.5"),
                _row("cursor_composer", "cursor", "composer-2.5"),
            ],
        },
    )

    assert resolution.error is None
    assert resolution.roster.agents["cursor_grok"].model == "composer-2.5"
    decision = next(item for item in resolution.receipt["decisions"] if item["seat"] == "cursor_grok")
    assert decision["requested_model"] == "composer-2.5"
    assert decision["outcome"] == "enabled"


def test_direct_worker_model_override_fails_closed_when_registry_model_differs():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        model_override="composer-2.5",
        snapshot={
            "state": "authoritative",
            "models": [_row("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")],
        },
    )

    assert resolution.error == (
        "fleet model policy denied seat 'cursor_grok': requested cursor/composer-2.5, "
        "registry allows cursor/cursor-grok-4.6-high-fast"
    )


def test_unconfigured_hub_preserves_local_roster_behavior():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        snapshot={"state": "unconfigured", "models": []},
    )

    assert resolution.error is None
    assert resolution.roster == _roster()
    assert resolution.receipt["authoritative"] is False
    assert resolution.receipt["state"] == "unconfigured"


def test_configured_unavailable_hub_denies_new_dispatch():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        snapshot={"state": "unavailable", "models": []},
    )

    assert resolution.error == "fleet model policy hub is unavailable; refusing new dispatch"


def test_hub_auth_failure_denies_new_dispatch():
    resolution = aboyeur.resolve_fleet_model_policy(
        _roster(),
        worker="cursor_grok",
        snapshot={"state": "auth-failed", "models": []},
    )

    assert resolution.error == "fleet model policy could not authenticate this node; refusing new dispatch"


def test_model_policy_snapshot_distinguishes_unconfigured_outage_and_auth(monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "", "token": ""},
    )
    assert fleet_client.load_model_policy_snapshot() == {"state": "unconfigured", "models": []}

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "https://hub.invalid", "token": "node-token"},
    )
    monkeypatch.setattr(
        fleet_client,
        "_run_with_deadline",
        lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    assert fleet_client.load_model_policy_snapshot() == {"state": "unavailable", "models": []}

    monkeypatch.setattr(
        fleet_client,
        "_run_with_deadline",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            urllib.error.HTTPError("https://hub.invalid/models", 401, "no", {}, None)
        ),
    )
    assert fleet_client.load_model_policy_snapshot() == {"state": "auth-failed", "models": []}


def test_run_writes_effective_policy_and_override_to_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {
            "state": "authoritative",
            "models": [_row("cursor_grok", "cursor", "composer-2.5")],
        },
    )
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            model_override="composer-2.5",
            dry_run=True,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    policy = json.loads((output_dir / "model-policy.json").read_text())
    run = json.loads((output_dir / "run.json").read_text())
    roster = json.loads((output_dir / "roster.json").read_text())
    assert policy["state"] == "authoritative"
    assert policy["model_override"] == "composer-2.5"
    assert roster["agents"]["cursor_grok"]["model"] == "composer-2.5"
    assert any(
        decision.get("typed_cause") == "fleet-model-policy" and decision.get("requested_model") == "composer-2.5"
        for decision in run["seat_routing"]
    )


def test_run_cli_requires_worker_for_model_override(tmp_path, capsys):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(
        'orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "plan"\nmodel = "gpt-5.6-terra"\n'
    )

    rc = cli.main(
        [
            "run",
            "inspect",
            "--roster",
            str(roster_path),
            "--cwd",
            str(tmp_path),
            "--model",
            "gpt-5.6-luna",
        ]
    )

    assert rc == 2
    assert "--model requires --worker" in capsys.readouterr().err


def test_run_terminalizes_configured_hub_outage_as_model_policy_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {"state": "unavailable", "models": []},
    )
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "inspect",
            _roster(),
            worker="cursor_grok",
            dry_run=True,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )

    run = json.loads((output_dir / "run.json").read_text())
    assert run["status"] == "failed"
    assert run["failure"] == {
        "phase": "preflight",
        "kind": "fleet-model-policy",
        "detail": "fleet model policy hub is unavailable; refusing new dispatch",
        "seat": "cursor_grok",
    }
