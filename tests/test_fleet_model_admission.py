from __future__ import annotations

import dataclasses
import http.client
import json
import os
import stat
import threading
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import aboyeur
from brigade import agents
from brigade import cli
from brigade import fleet_client
from brigade import fleet_hub
from brigade import fleet_model_roster
from brigade.roster import Agent, Roster
from tests.run_test_helpers import run_aboyeur_guarded

NODE_A = "11111111-1111-4111-8111-111111111111"
ADMIN_TOKEN = "test-admin-token"
ADMIT_REQUEST_ID = "c833a6f6-02fd-4eb2-92cb-d44d3cd29b66"
SEAT_BODY = {
    "action": "set",
    "provider": "cursor",
    "model": "cursor-grok-4.6-high-fast",
    "seat": "cursor_grok",
    "enabled": True,
    "reasoning": "high",
    "brigade_cli": "cursor-agent",
    "t3_instance_id": "cursor",
    "t3_service_tier": "standard",
}


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


def test_model_policy_model_slugifies_display_names_deterministically():
    assert agents.model_policy_model("antigravity", "Gemini 3.7 Flash (Low)") == "gemini-3.7-flash-low"
    assert agents.model_policy_model("antigravity", "  Gemini   3.7 / Flash ((Low))  ") == "gemini-3.7-flash-low"


def test_model_policy_model_preserves_valid_ids_exactly():
    for model in (
        "cursor-grok-4.6-high-fast",
        "composer-2.5",
        "gpt-5.6-terra",
        "configured",
        "gemini-3.7-flash-low",
    ):
        assert agents.model_policy_model("cursor", model) == model


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


def test_direct_worker_acquires_only_its_invoked_seat_and_releases(monkeypatch, tmp_path):
    acquired: list[tuple[str, str, str]] = []
    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {"state": "authoritative", "models": [_row("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")]},
    )

    def acquire(seat, provider, model):
        acquired.append((seat, provider, model))
        return fleet_client.ModelLeaseDecision(True, "ok", "direct-lease", "direct-holder")

    monkeypatch.setattr(fleet_client, "acquire_model_lease", acquire)
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: (
            released.append((lease_id, holder)) or fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder)
        ),
    )
    monkeypatch.setattr(
        agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(text="implemented", ok=True),
    )

    assert (
        run_aboyeur_guarded(
            "implement it",
            _roster(),
            worker="cursor_grok",
            output_dir=tmp_path / "run",
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert acquired == [("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")]
    assert released == [("direct-lease", "direct-holder")]


def test_direct_worker_runs_with_only_its_authoritative_policy_seat(monkeypatch, tmp_path):
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", fallback=("coder",)),
            "coder": Agent("coder", "codex", "code"),
            "reviewer": Agent("reviewer", "codex", "review"),
            "cursor_grok": Agent("cursor_grok", "cursor", "code", model="cursor-grok-4.6-high-fast"),
        },
    )
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {"state": "authoritative", "models": [_row("cursor_grok", "cursor", "cursor-grok-4.6-high-fast")]},
    )
    monkeypatch.setattr(
        fleet_client,
        "acquire_model_lease",
        lambda *args, **kwargs: fleet_client.ModelLeaseDecision(True, "ok", "direct-lease", "direct-holder"),
    )
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder),
    )
    invocations: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
        invocations.append(cli_ref)
        return agents.AgentResult(text="implemented", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    output_dir = tmp_path / "run"

    assert (
        run_aboyeur_guarded(
            "implement it",
            roster,
            worker="cursor_grok",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    effective = json.loads((output_dir / "roster.json").read_text())
    assert "coder" not in effective["agents"]
    assert "reviewer" not in effective["agents"]
    assert invocations == ["cursor"]


def test_orchestrated_run_leases_only_planner_worker_and_synthesis_seats(monkeypatch, tmp_path):
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan", model="opus-5"),
            "coder": Agent("coder", "codex", "code", model="gpt-5.6-terra"),
            "reviewer": Agent("reviewer", "codex", "review", model="gpt-5.6-luna"),
        },
    )
    acquired: list[str] = []
    released: list[str] = []
    monkeypatch.setattr(
        fleet_client,
        "load_model_policy_snapshot",
        lambda: {
            "state": "authoritative",
            "models": [
                _row("chef", "anthropic", "opus-5"),
                _row("coder", "openai", "gpt-5.6-terra"),
                _row("reviewer", "openai", "gpt-5.6-luna"),
            ],
        },
    )

    def acquire(seat, provider, model):  # noqa: ARG001
        acquired.append(seat)
        return fleet_client.ModelLeaseDecision(True, "ok", f"lease-{seat}-{len(acquired)}", f"holder-{seat}")

    monkeypatch.setattr(fleet_client, "acquire_model_lease", acquire)
    monkeypatch.setattr(
        fleet_client,
        "release_model_lease",
        lambda lease_id, *, holder: (
            released.append(lease_id) or fleet_client.ModelLeaseDecision(True, "ok", lease_id, holder)
        ),
    )

    calls: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):  # noqa: ARG001
        calls.append(cli_ref)
        if len(calls) == 1:
            return agents.AgentResult(text='{"assignments":[{"worker":"coder","task":"implement it"}]}', ok=True)
        if len(calls) == 2:
            return agents.AgentResult(text="implemented", ok=True)
        return agents.AgentResult(text="synthesized", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    assert (
        run_aboyeur_guarded(
            "implement it",
            roster,
            output_dir=tmp_path / "run",
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert acquired == ["chef", "coder", "chef"]
    assert len(released) == 3
    assert "reviewer" not in acquired


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


def _plant_home_identity(home: Path, node_id: str) -> None:
    from brigade import node as node_mod

    identity = node_mod.NodeIdentity(node_id=node_id, hostname="fleet-test", roles=(), platform="test")
    path = node_mod.node_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node_mod._format_node_toml(identity), encoding="utf-8")


@contextmanager
def _hub(tmp_path):
    server = fleet_hub.make_server(
        "127.0.0.1", 0, tmp_path / "fleet.db", ADMIN_TOKEN
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_address[1], tmp_path / "fleet.db"  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, method: str, path: str, *, token: str | None = None, body: dict[str, object] | None = None):
    host, port, _db = hub
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(
        method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers
    )
    response = connection.getresponse()
    result = response.status, json.loads(response.read().decode("utf-8"))
    connection.close()
    return result


def _enroll(hub, node_id: str = NODE_A) -> str:
    db = fleet_hub.open_db(hub[2])
    try:
        _node, node_token = fleet_hub.add_node(db, node_id, "node-a")
    finally:
        db.close()
    return node_token


def _current_revision(hub) -> int:
    status, payload = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
    assert status == 200
    return int(payload["revision"])


def _admin_set(hub, **fields: object):
    body = {"expected_revision": _current_revision(hub), **SEAT_BODY, **fields}
    return _request(hub, "POST", "/models", token=ADMIN_TOKEN, body=body)


def _configure_client(monkeypatch, tmp_path, hub, node_token: str) -> Path:
    home = tmp_path / "brigade-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", f"http://{hub[0]}:{hub[1]}")
    monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", node_token)
    monkeypatch.setenv("BRIGADE_FLEET_TOKEN", ADMIN_TOKEN)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    _plant_home_identity(home, NODE_A)
    return home


def _seed_hub(hub, node_token: str) -> dict[str, object]:
    assert _admin_set(hub)[0] == 200
    assert (
        _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set-default",
                "consumer": "t3-fleet",
                "seat": "cursor_grok",
                "expected_revision": _current_revision(hub),
            },
        )[0]
        == 200
    )
    status, roster = _request(hub, "GET", "/models", token=node_token)
    assert status == 200
    return roster


def _secret_free(value: object, *secrets: str) -> None:
    rendered = json.dumps(value, default=str)
    for secret in secrets:
        if secret:
            assert secret not in rendered


def test_model_admission_decision_is_frozen_and_secret_free():
    from brigade import fleet_model_admission

    decision = fleet_model_admission.ModelAdmissionDecision(
        ok=True,
        exit_code=0,
        reason="admitted",
        payload={"schema": "brigade.model_admission.v1", "source": "hub"},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.ok = False  # type: ignore[misc]
    assert set(dataclasses.asdict(decision)) == {"ok", "exit_code", "reason", "payload"}
    _secret_free(decision, "Bearer", "/tmp/", "prompt")


def test_valid_node_roster_is_cached_after_mac_and_digest_checks(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is True
        assert decision.exit_code == 0
        assert decision.payload["schema"] == fleet_model_roster.ROSTER_SCHEMA
        assert decision.payload["revision"] == roster["revision"]
        assert decision.payload["roster_digest"] == roster["roster_digest"]
        assert decision.payload["audience_node_id"] == NODE_A
        lkg = fleet_model_admission.lkg_path()
        high_water = fleet_model_admission.high_water_path()
        assert lkg.is_file()
        assert high_water.is_file()
        assert lkg.is_relative_to(home)
        assert high_water.is_relative_to(home)
        assert stat.S_IMODE(os.stat(lkg.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(lkg).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(high_water).st_mode) == 0o600
        record = json.loads(lkg.read_text(encoding="utf-8"))
        assert record["highest_revision"] == roster["revision"]
        assert record["roster"]["mac"]["value"] == roster["mac"]["value"]
        assert record["roster"]["roster_digest"] == roster["roster_digest"]
        assert "cached_at" in record
        _secret_free(record, node_token, ADMIN_TOKEN)
        _secret_free(decision, node_token, ADMIN_TOKEN)


def test_admin_roster_read_cannot_seed_lkg(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        monkeypatch.delenv("BRIGADE_FLEET_NODE_TOKEN")
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert not fleet_model_admission.lkg_path().exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file identity and modes")
def test_lkg_and_high_water_reject_unsafe_files(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        lkg = fleet_model_admission.lkg_path()
        high_water = fleet_model_admission.high_water_path()
        victim = tmp_path / "victim.json"
        victim.write_text("do not touch\n", encoding="utf-8")
        lkg.unlink()
        lkg.symlink_to(victim)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert victim.read_text(encoding="utf-8") == "do not touch\n"
        lkg.unlink()
        os.mkfifo(lkg)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert stat.S_ISFIFO(os.lstat(lkg).st_mode)
        lkg.unlink()
        high_water.write_bytes(b"x" * (fleet_model_admission.MODEL_ROSTER_MAX_BYTES + 1))
        os.chmod(high_water, 0o600)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        high_water.unlink()
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        os.chmod(lkg, 0o644)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        os.chmod(lkg, 0o600)
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        _secret_free(decision, node_token, str(lkg), str(home))


@pytest.mark.skipif(os.name != "posix", reason="descriptor path-swap is POSIX-only")
def test_lkg_path_swap_between_validation_and_open_never_writes_decoy(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        lkg = fleet_model_admission.lkg_path()
        real_dir = lkg.parent
        decoy = tmp_path / "decoy-fleet"
        decoy.mkdir()
        os.chmod(decoy, 0o700)
        sentinel = decoy / lkg.name
        sentinel.write_text("do not touch\n", encoding="utf-8")
        real_ensure = fleet_client._ensure_private_dir

        def swap_after_open(path):
            fd = real_ensure(path)
            evacuated = tmp_path / "evacuated-fleet"
            path.rename(evacuated)
            decoy.rename(path)
            swap_after_open.evacuated = evacuated  # type: ignore[attr-defined]
            return fd

        monkeypatch.setattr(fleet_client, "_ensure_private_dir", swap_after_open)
        try:
            decision = fleet_model_admission.fetch_versioned_roster()
            assert decision.ok is True
        finally:
            if real_dir.exists() and decoy.exists() is False:
                real_dir.rename(decoy)
            evacuated = getattr(swap_after_open, "evacuated", None)
            if evacuated is not None and evacuated.exists():
                evacuated.rename(real_dir)
        assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
        assert sorted(p.name for p in decoy.iterdir()) == [sentinel.name]


def test_windows_without_nofollow_fails_closed(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        monkeypatch.setattr(fleet_model_admission, "nofollow_supported", lambda: False)
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert decision.reason == "cache-unsafe-platform"
        assert not fleet_model_admission.lkg_path().exists()


def test_lkg_is_used_only_after_timeout_transport_or_http_5xx(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        cached = json.loads(fleet_model_admission.lkg_path().read_text(encoding="utf-8"))
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("deadline")),
        )
        timeout = fleet_model_admission.fetch_versioned_roster()
        assert timeout.ok is True
        assert timeout.payload["source"] == "lkg"
        assert timeout.payload["revision"] == roster["revision"]
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        transport = fleet_model_admission.fetch_versioned_roster()
        assert transport.ok is True
        assert transport.payload["source"] == "lkg"
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                urllib.error.HTTPError("http://hub.invalid/models", 503, "down", {}, None)
            ),
        )
        server_error = fleet_model_admission.fetch_versioned_roster()
        assert server_error.ok is True
        assert server_error.payload["source"] == "lkg"
        assert json.loads(fleet_model_admission.lkg_path().read_text(encoding="utf-8")) == cached


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (urllib.error.HTTPError("http://hub.invalid/models", 401, "no", {}, None), "auth-failed"),
        (urllib.error.HTTPError("http://hub.invalid/models", 403, "no", {}, None), "auth-failed"),
        (
            urllib.error.HTTPError("http://hub.invalid/models", 409, "conflict", {}, None),
            "revision-conflict",
        ),
    ],
)
def test_authoritative_auth_and_conflict_never_fall_back_to_lkg(tmp_path, monkeypatch, exc, reason):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        before = fleet_model_admission.lkg_path().read_bytes()
        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(exc),
        )
        decision = fleet_model_admission.fetch_versioned_roster()
        assert decision.ok is False
        assert decision.exit_code == 1
        assert decision.reason == reason
        assert fleet_model_admission.lkg_path().read_bytes() == before
        _secret_free(decision, node_token, ADMIN_TOKEN)


def test_malformed_and_integrity_failures_never_rewrite_lkg(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        assert fleet_model_admission.fetch_versioned_roster().ok is True
        before = fleet_model_admission.lkg_path().read_bytes()

        def _return(payload):
            monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda *args, **kwargs: payload)

        _return("{not-json")
        malformed = fleet_model_admission.fetch_versioned_roster()
        assert malformed.ok is False
        assert malformed.exit_code in {1, 2}
        assert fleet_model_admission.lkg_path().read_bytes() == before

        bad = dict(roster)
        bad["roster_digest"] = "sha256:" + ("ab" * 32)
        _return(bad)
        digest = fleet_model_admission.fetch_versioned_roster()
        assert digest.ok is False
        assert digest.reason == "lkg-mac-invalid" or digest.reason == "digest-mismatch"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        bad_mac = dict(roster)
        bad_mac = json.loads(json.dumps(roster))
        bad_mac["mac"] = {"algorithm": roster["mac"]["algorithm"], "value": "ab" * 32}
        _return(bad_mac)
        mac = fleet_model_admission.fetch_versioned_roster()
        assert mac.ok is False
        assert mac.reason == "lkg-mac-invalid"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        drifted = json.loads(json.dumps(roster))
        drifted["audience_node_id"] = "22222222-2222-4222-8222-222222222222"
        drifted["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, drifted),
        }
        _return(drifted)
        audience = fleet_model_admission.fetch_versioned_roster()
        assert audience.ok is False
        assert audience.reason == "audience-mismatch"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        _return(json.loads(json.dumps(roster)))
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", "rotated-node-token")
        rotated = fleet_model_admission.fetch_versioned_roster()
        assert rotated.ok is False
        assert rotated.reason in {"lkg-mac-invalid", "token-rotated"}
        assert fleet_model_admission.lkg_path().read_bytes() == before
        monkeypatch.setenv("BRIGADE_FLEET_NODE_TOKEN", node_token)

        future = json.loads(json.dumps(roster))
        future_issued = datetime.now(timezone.utc) + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS + 30)
        future["issued_at"] = future_issued.strftime("%Y-%m-%dT%H:%M:%SZ")
        future["expires_at"] = (future_issued + timedelta(seconds=900)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, future),
        }
        _return(future)
        skew = fleet_model_admission.fetch_versioned_roster()
        assert skew.ok is False
        assert skew.reason == "future-timestamp"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        expired = json.loads(json.dumps(roster))
        expired["issued_at"] = "2020-01-01T00:00:00Z"
        expired["expires_at"] = "2020-01-01T00:15:00Z"
        expired["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, expired),
        }
        _return(expired)
        expiry = fleet_model_admission.fetch_versioned_roster()
        assert expiry.ok is False
        assert expiry.reason == "lkg-expired"
        assert fleet_model_admission.lkg_path().read_bytes() == before

        rollback = json.loads(json.dumps(roster))
        rollback["revision"] = int(roster["revision"]) - 1
        rollback["roster_digest"] = fleet_model_roster.roster_digest(rollback)
        rollback["mac"] = {
            "algorithm": fleet_model_roster.MAC_ALGORITHM,
            "value": fleet_model_roster.roster_mac(node_token, rollback),
        }
        _return(rollback)
        rolled = fleet_model_admission.fetch_versioned_roster()
        assert rolled.ok is False
        assert rolled.reason == "revision-rollback"
        assert fleet_model_admission.lkg_path().read_bytes() == before
        _secret_free(
            [malformed, digest, mac, audience, rotated, skew, expiry, rolled],
            node_token,
            ADMIN_TOKEN,
            "rotated-node-token",
        )


def test_admit_hub_success_lkg_success_policy_denial_and_revision_conflict(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        admitted = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id=ADMIT_REQUEST_ID,
            phase="controller",
        )
        assert admitted.ok is True
        assert admitted.exit_code == 0
        assert admitted.payload == {
            "schema": "brigade.model_admission.v1",
            "state": "authoritative",
            "source": "hub",
            "roster_revision": roster["revision"],
            "roster_digest": roster["roster_digest"],
            "seat": "cursor_grok",
            "provider": "cursor",
            "model": "cursor-grok-4.6-high-fast",
            "reasoning": "high",
            "binding": {"instance_id": "cursor", "service_tier": "standard"},
            "expires_at": admitted.payload["expires_at"],
        }
        _secret_free(admitted, node_token, ADMIN_TOKEN, str(tmp_path), "prompt")
        assert not fleet_model_admission.audit_spool_path().exists()

        monkeypatch.setattr(
            fleet_client,
            "_run_with_deadline",
            lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
        )
        lkg = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="55555555-5555-4555-8555-555555555555",
            phase="controller",
        )
        assert lkg.ok is True
        assert lkg.exit_code == 0
        assert lkg.payload["source"] == "lkg"
        assert lkg.payload["schema"] == "brigade.model_admission.v1"
        spool = fleet_model_admission.audit_spool_path()
        assert spool.is_file()
        assert stat.S_IMODE(os.stat(spool).st_mode) == 0o600
        rows = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines() if line]
        assert len(rows) == 1
        assert rows[0]["source"] == "lkg"
        assert rows[0]["decision"] == "admitted"
        _secret_free(rows[0], node_token, ADMIN_TOKEN, str(tmp_path), "prompt")
        first_spool = spool.read_bytes()
        lkg_again = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="55555555-5555-4555-8555-555555555555",
            phase="controller",
        )
        assert lkg_again.payload == lkg.payload
        assert spool.read_bytes() == first_spool

        monkeypatch.undo()
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        denied = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="66666666-6666-4666-8666-666666666666",
            phase="controller",
            seat="missing-seat",
        )
        assert denied.ok is False
        assert denied.exit_code == 3
        assert denied.reason in {"seat-missing", "binding-missing"}
        _secret_free(denied, node_token, ADMIN_TOKEN, str(tmp_path))

        conflict = fleet_model_admission.admit_model(
            consumer="t3-fleet",
            request_id="77777777-7777-4777-8777-777777777777",
            phase="controller",
            expect_revision=1,
        )
        assert conflict.ok is False
        assert conflict.exit_code == 4
        assert conflict.reason == "roster_revision_conflict"
        _secret_free(conflict, node_token, ADMIN_TOKEN)


def test_fleet_models_cli_admit_doctor_reconcile_retire_and_default(tmp_path, monkeypatch, capsys):
    from brigade import fleet_model_admission

    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        roster = _seed_hub(hub, node_token)
        home = _configure_client(monkeypatch, tmp_path, hub, node_token)
        (home / ".brigade").mkdir(parents=True, exist_ok=True)
        (home / ".brigade" / "roster.toml").write_text(
            'orchestrator = "chef"\n\n[agents.cursor_grok]\ncli = "cursor"\nrole = "code"\nmodel = "composer-2.5"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(home)
        admit_rc = cli.main(
            [
                "fleet",
                "models",
                "admit",
                "--consumer",
                "t3-fleet",
                "--request-id",
                ADMIT_REQUEST_ID,
                "--phase",
                "controller",
                "--json",
            ]
        )
        admit_out = capsys.readouterr()
        assert admit_rc == 0
        admitted = json.loads(admit_out.out)
        assert admitted["schema"] == "brigade.model_admission.v1"
        assert admitted["source"] == "hub"
        assert admitted["seat"] == "cursor_grok"
        assert json.dumps(admitted, indent=2, sort_keys=True) + "\n" == admit_out.out
        _secret_free(admitted, node_token, ADMIN_TOKEN, str(home))

        doctor_rc = cli.main(["fleet", "models", "doctor", "--consumer", "t3-fleet", "--json"])
        doctor_out = capsys.readouterr()
        assert doctor_rc == 0
        doctor = json.loads(doctor_out.out)
        assert doctor["consumer"] == "t3-fleet"
        assert doctor["hub"] == "reachable"
        assert doctor["roster_revision"] == roster["revision"]
        assert doctor["roster_digest"] == roster["roster_digest"]
        assert doctor["cache_valid"] is True
        assert doctor["consumer_default"] == "cursor_grok"
        assert doctor["provider"] == "cursor"
        assert doctor["model"] == "cursor-grok-4.6-high-fast"
        assert doctor["reasoning"] == "high"
        assert doctor["binding_present"] is True
        assert doctor["retired"] is False
        assert "local_roster_drift" in doctor
        assert "audit_spool" in doctor
        assert json.dumps(doctor, indent=2, sort_keys=True) + "\n" == doctor_out.out

        reconcile_rc = cli.main(["fleet", "models", "reconcile", "--consumer", "t3-fleet", "--json"])
        reconcile_out = capsys.readouterr()
        assert reconcile_rc == 0
        reconcile = json.loads(reconcile_out.out)
        assert reconcile["read_only"] is True
        assert "local_roster" in reconcile
        assert "project_default_model" in reconcile
        assert "project_default_drift" in reconcile
        assert "findings" in reconcile
        assert json.dumps(reconcile, indent=2, sort_keys=True) + "\n" == reconcile_out.out
        status, after = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        assert after["revision"] == roster["revision"]

        retire_rc = cli.main(
            [
                "fleet",
                "models",
                "retire",
                "openai",
                "gpt-5.4",
                "--permanent",
                "--expect-revision",
                str(roster["revision"]),
            ]
        )
        assert retire_rc == 3
        default_rc = cli.main(
            [
                "fleet",
                "models",
                "default",
                "set",
                "t3-fleet",
                "cursor_grok",
                "--expect-revision",
                str(roster["revision"]),
            ]
        )
        assert default_rc == 0
        stale_default = cli.main(
            [
                "fleet",
                "models",
                "default",
                "set",
                "t3-fleet",
                "cursor_grok",
                "--expect-revision",
                str(roster["revision"]),
            ]
        )
        assert stale_default == 4

        monkeypatch.setattr(
            fleet_model_admission,
            "admit_model",
            lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(
                False, 1, "hub-unavailable", {"reason": "hub-unavailable"}
            ),
        )
        assert (
            cli.main(
                [
                    "fleet",
                    "models",
                    "admit",
                    "--consumer",
                    "t3-fleet",
                    "--request-id",
                    "88888888-8888-4888-8888-888888888888",
                    "--phase",
                    "controller",
                    "--json",
                ]
            )
            == 1
        )
        monkeypatch.setattr(
            fleet_model_admission,
            "admit_model",
            lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(
                False, 2, "unsupported-schema", {"reason": "unsupported-schema"}
            ),
        )
        assert (
            cli.main(
                [
                    "fleet",
                    "models",
                    "admit",
                    "--consumer",
                    "t3-fleet",
                    "--request-id",
                    "88888888-8888-4888-8888-888888888888",
                    "--phase",
                    "not-a-phase",
                    "--json",
                ]
            )
            == 2
        )
        monkeypatch.setattr(
            fleet_model_admission,
            "admit_model",
            lambda **kwargs: fleet_model_admission.ModelAdmissionDecision(
                False, 3, "retired-model", {"reason": "retired-model"}
            ),
        )
        assert (
            cli.main(
                [
                    "fleet",
                    "models",
                    "admit",
                    "--consumer",
                    "t3-fleet",
                    "--request-id",
                    "88888888-8888-4888-8888-888888888888",
                    "--phase",
                    "controller",
                    "--json",
                ]
            )
            == 3
        )


def test_models_set_fetches_admin_revision_and_sends_reasoning_bindings(tmp_path, monkeypatch):
    with _hub(tmp_path) as hub:
        node_token = _enroll(hub)
        _configure_client(monkeypatch, tmp_path, hub, node_token)
        policy = fleet_client.set_model_policy(
            "cursor",
            "cursor-grok-4.6-high-fast",
            "cursor_grok",
            enabled=True,
            limit=8,
            notes="primary",
        )
        assert policy["seat"] == "cursor_grok"
        assert policy["provider"] == "cursor"
        assert policy["model"] == "cursor-grok-4.6-high-fast"
        status, roster = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        seat = next(item for item in roster["seats"] if item["seat"] == "cursor_grok")
        assert seat["reasoning"] == "none"
        assert seat["bindings"]["brigade_cli"] == ""
        assert seat["bindings"]["t3_instance_id"] == ""


def test_model_roster_read_cap_is_one_mib_without_raising_cloud_cap(tmp_path, monkeypatch):
    from brigade import fleet_model_admission

    assert fleet_client.MAX_CLOUD_RESPONSE_BYTES == 64 * 1024
    assert fleet_model_admission.MODEL_ROSTER_MAX_BYTES == 1024 * 1024
    padding = "x" * (fleet_client.MAX_CLOUD_RESPONSE_BYTES + 1)
    body = json.dumps({"schema": "brigade.fleet_model_roster.v1", "padding": padding}).encode("utf-8")
    assert len(body) > fleet_client.MAX_CLOUD_RESPONSE_BYTES
    assert len(body) < fleet_model_admission.MODEL_ROSTER_MAX_BYTES

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            return body[:size] if size != -1 else body

    monkeypatch.setattr(fleet_client, "_hub_open", lambda request, timeout: _Response())
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": "http://127.0.0.1:9", "token": "node-token"},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    decision = fleet_model_admission.fetch_versioned_roster(allow_lkg=False)
    assert "exceeded the size limit" not in decision.reason
    with pytest.raises(fleet_client.FleetClientError, match="size limit"):
        fleet_client.fetch_cloud()
