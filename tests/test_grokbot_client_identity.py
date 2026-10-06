"""Deployment identity stays fixed across configuration, services, and leases."""

from dataclasses import replace
import json
import shlex

import pytest

from brigade import cli, fleet_client_grokbot, grokbot_jobs, grokbot_mcp, grokbot_ops
from tests.test_grokbot_mcp import _adapter, _spec
from tests.test_grokbot_ops import _setup


ROLE = "implementation-worker"
ALPHA_UUID = "00000000-0000-4000-8000-000000000001"
BETA_UUID = "00000000-0000-4000-8000-000000000002"
CONNECTOR_PACK_IDS = (
    "backup-steward",
    "cerebro-memory",
    "fleet-steward",
    "n8n-operator",
    "obsidian-operator",
    "operations-relay",
    "wazuh-triage",
)


@pytest.mark.parametrize("pack_id", CONNECTOR_PACK_IDS)
def test_legacy_connector_pack_names_remain_exact(tmp_path, pack_id):
    service = f"brigade-grokbot-{pack_id}.service"
    assert grokbot_ops.unit_name(pack_id) == service
    assert grokbot_ops.unit_name(pack_id, None) == service
    assert grokbot_ops.config_path(tmp_path, pack_id) == tmp_path / ".brigade" / "grokbot" / f"{pack_id}.json"
    assert grokbot_ops.config_path(tmp_path, pack_id, None) == tmp_path / ".brigade" / "grokbot" / f"{pack_id}.json"
    assert grokbot_ops.service_result_argv(pack_id) == [
        "systemctl",
        "--user",
        "show",
        service,
        "--property=Result",
        "--value",
    ]


@pytest.mark.parametrize("pack_id", CONNECTOR_PACK_IDS)
def test_named_connector_pack_names_are_refused(tmp_path, pack_id):
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.unit_name(pack_id, "alpha")
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.config_path(tmp_path, pack_id, "alpha")
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.service_result_argv(pack_id, "alpha")


@pytest.mark.parametrize("role", ["operator", "implementation-worker", "repository-scout"])
def test_named_queue_role_service_result_uses_client_unit(role):
    assert grokbot_ops.service_result_argv(role, "alpha") == [
        "systemctl",
        "--user",
        "show",
        f"brigade-grokbot-{role}-alpha.service",
        "--property=Result",
        "--value",
    ]


@pytest.mark.parametrize("client_ids", [("alpha", "beta"), (ALPHA_UUID, BETA_UUID)])
def test_client_configs_and_rendered_services_coexist_and_roundtrip(tmp_path, monkeypatch, client_ids):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    captured = []
    monkeypatch.setattr(grokbot_mcp, "run_listener", captured.append)
    units = tmp_path / "units"
    for client_id, port in zip(client_ids, (8766, 8767), strict=True):
        assert _setup(tmp_path, ROLE, ["--client-id", client_id, "--bind", f"127.0.0.1:{port}"]) == 0
        config = grokbot_ops.load_config(tmp_path, ROLE, client_id)
        assert config["client_id"] == client_id
        path = grokbot_ops.write_unit(config, units, exec_root=tmp_path, python="python")
        assert path.name == f"brigade-grokbot-{ROLE}-{client_id}.service"
        command = next(
            line.removeprefix("ExecStart=") for line in path.read_text().splitlines() if line.startswith("ExecStart=")
        )
        assert cli.main(shlex.split(command)[3:]) == 0
        assert captured[-1].client_id == client_id
        assert captured[-1].bot_id == f"grokbot-{ROLE}-{client_id}"
        assert captured[-1].bind_port == port
    assert len(list(units.glob("*.service"))) == 2
    assert len(list((tmp_path / grokbot_ops.CONFIG_DIR).glob("*.json"))) == 2
    assert not grokbot_ops.config_path(tmp_path, ROLE).exists()


def test_legacy_config_and_listener_identity_remain_exact(tmp_path):
    assert _setup(tmp_path, ROLE) == 0
    config = grokbot_ops.load_config(tmp_path, ROLE)
    assert "client_id" not in config
    assert grokbot_ops.config_path(tmp_path, ROLE).name == f"{ROLE}.json"
    assert grokbot_ops.unit_name(ROLE) == f"brigade-grokbot-{ROLE}.service"
    adapter = _adapter(tmp_path)
    assert adapter.config.bot_id == f"grokbot-{ROLE}"
    assert adapter.health_payload() == {"ok": True, "service": "grokbot-mcp", "role": ROLE}
    assert "--client-id" not in grokbot_ops.render_unit(config, python="python", exec_root=tmp_path)


@pytest.mark.parametrize(
    "client_id", ["", "../alpha", "alpha/beta", "alpha\\beta", ".", "Alpha", "alpha beta", "alpha\n", "a" * 65, 1]
)
def test_invalid_client_identity_is_refused_before_paths_or_config_writes(tmp_path, client_id):
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.config_path(tmp_path, ROLE, client_id)
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.unit_name(ROLE, client_id)
    with pytest.raises(grokbot_mcp.ConfigurationError):
        replace(_adapter(tmp_path).config, client_id=client_id).validate()
    assert not (tmp_path / grokbot_ops.CONFIG_DIR).exists()


def test_config_identity_mismatch_refuses_read_and_overwrite(tmp_path):
    assert _setup(tmp_path, ROLE, ["--client-id", "alpha"]) == 0
    path = grokbot_ops.config_path(tmp_path, ROLE, "alpha")
    payload = json.loads(path.read_text())
    payload["client_id"] = "beta"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(grokbot_mcp.ConfigurationError):
        grokbot_ops.load_config(tmp_path, ROLE, "alpha")
    assert _setup(tmp_path, ROLE, ["--client-id", "alpha"]) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("winner", ["identical", "different", "directory", "malformed", "invalid-utf8"])
def test_named_setup_atomic_publication_race(tmp_path, monkeypatch, winner):
    publish = grokbot_ops._write_text_nofollow_atomic
    publications = []
    winning_bytes = []

    def competing_publish(path, data, **kwargs):
        publications.append(path)
        assert kwargs["replace"] is False
        if winner == "directory":
            path.mkdir(parents=True)
        else:
            if winner == "different":
                payload = json.loads(data)
                payload["bind"] = "127.0.0.1:9876"
                winning_data = json.dumps(payload)
            elif winner == "malformed":
                winning_data = "{"
            else:
                winning_data = data
            publish(path, winning_data, **kwargs)
            if winner == "invalid-utf8":
                path.write_bytes(b"\xff")
            winning_bytes.append(path.read_bytes())
        publish(path, data, **kwargs)

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", competing_publish)
    assert _setup(tmp_path, ROLE, ["--client-id", "alpha"]) == (0 if winner == "identical" else 2)
    path = grokbot_ops.config_path(tmp_path, ROLE, "alpha")
    assert publications == [path]
    if winner == "directory":
        assert path.is_dir()
    else:
        assert path.read_bytes() == winning_bytes[0]
    if winner == "identical":
        assert grokbot_ops.load_config(tmp_path, ROLE, "alpha")["client_id"] == "alpha"
    assert not list(path.parent.glob(".*.tmp"))


def test_named_setup_does_not_recover_other_publication_errors(tmp_path, monkeypatch):
    publish = grokbot_ops._write_text_nofollow_atomic

    def failed_publish(path, data, **kwargs):
        publish(path, data, **kwargs)
        raise PermissionError("publication refused")

    monkeypatch.setattr(grokbot_ops, "_write_text_nofollow_atomic", failed_publish)
    assert _setup(tmp_path, ROLE, ["--client-id", "alpha"]) == 2


def test_distinct_clients_cannot_use_each_others_local_leases(tmp_path):
    clients = {
        name: grokbot_mcp.GrokbotAdapter(replace(_adapter(tmp_path).config, client_id=name))
        for name in ("alpha", "beta")
    }
    for name, adapter in clients.items():
        assert adapter.tool_inventory() == _adapter(tmp_path).tool_inventory()
        job_id = grokbot_jobs.enqueue(tmp_path, _spec(ROLE), name)["job_id"]
        args = {"job_id": job_id, "lease_id": f"lease-{name}"}
        assert adapter.call_tool("grokbot_queue_claim", args)["bot_id"] == f"grokbot-{ROLE}-{name}"
        other = clients["beta" if name == "alpha" else "alpha"]
        with pytest.raises(grokbot_mcp.AdapterError):
            other.call_tool("grokbot_queue_start", args)
        assert adapter.call_tool("grokbot_queue_renew", args)["bot_id"] == adapter.config.bot_id
        assert adapter.call_tool("grokbot_queue_start", args)["state"] == "running"
        assert adapter.call_tool("grokbot_queue_fail", args)["bot_id"] == adapter.config.bot_id
        with pytest.raises(grokbot_mcp.AdapterError):
            adapter.call_tool("grokbot_queue_list", {"client_id": "beta"})


@pytest.mark.parametrize("authenticated_id", [None, "beta", "alpha"])
def test_hub_client_identity_must_match_authenticated_credential(tmp_path, monkeypatch, authenticated_id):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: True)

    def whoami(**options):
        assert options == {"include_node_id": True}
        return fleet_client_grokbot.GrokbotHubDecision(
            True, "ok", job={"actor_kind": ROLE, "role": ROLE, "node_id": authenticated_id}
        )

    monkeypatch.setattr(
        fleet_client_grokbot,
        "whoami",
        whoami,
    )
    config = replace(_adapter(tmp_path, hub_token="fake-alpha-token").config, client_id="alpha")
    adapter = grokbot_mcp.GrokbotAdapter(config)
    if authenticated_id == "alpha":
        adapter.ensure_hub_actor()
    else:
        with pytest.raises(grokbot_mcp.ConfigurationError):
            adapter.ensure_hub_actor()


@pytest.mark.parametrize("authenticated_id", [None, BETA_UUID, ALPHA_UUID])
def test_uuid_client_identity_must_match_authenticated_credential(tmp_path, monkeypatch, authenticated_id):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: True)

    def whoami(**options):
        assert options == {"include_node_id": True}
        return fleet_client_grokbot.GrokbotHubDecision(
            True, "ok", job={"actor_kind": ROLE, "role": ROLE, "node_id": authenticated_id}
        )

    monkeypatch.setattr(
        fleet_client_grokbot,
        "whoami",
        whoami,
    )
    config = replace(_adapter(tmp_path, hub_token="fake-alpha-token").config, client_id=ALPHA_UUID)
    adapter = grokbot_mcp.GrokbotAdapter(config)
    if authenticated_id == ALPHA_UUID:
        adapter.ensure_hub_actor()
    else:
        with pytest.raises(grokbot_mcp.ConfigurationError):
            adapter.ensure_hub_actor()


def test_canary_refuses_same_role_different_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    assert _setup(tmp_path, ROLE, ["--client-id", "alpha"]) == 0
    monkeypatch.setattr(grokbot_ops, "_request_json", lambda *a, **k: {"ok": True, "role": ROLE, "client_id": "beta"})
    monkeypatch.setattr(grokbot_ops, "_anonymous_health_status", lambda *a: 401)
    monkeypatch.setattr(grokbot_ops, "_tools_list", lambda *a: [{"name": name} for name in grokbot_mcp.WORKER_TOOLS])
    assert grokbot_ops.canary(tmp_path, ROLE, client_id="alpha")["reason"] == "identity"


@pytest.mark.parametrize(
    "field,value",
    [
        (None, None),
        ("role", "repository-scout"),
        ("role", None),
        ("client_id", "beta"),
        ("client_id", None),
        ("bot_id", "grokbot-implementation-worker-beta"),
        ("bot_id", None),
    ],
)
def test_named_doctor_endpoint_requires_matching_identity(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    assert _setup(tmp_path, ROLE, ["--client-id", "alpha"]) == 0
    health = {
        "ok": True,
        "service": "grokbot-mcp",
        "role": ROLE,
        "client_id": "alpha",
        "bot_id": "grokbot-implementation-worker-alpha",
    }
    if field is not None:
        if value is None:
            del health[field]
        else:
            health[field] = value
    monkeypatch.setattr(grokbot_ops, "_request_json", lambda *a, **k: health)
    monkeypatch.setattr(grokbot_mcp, "load_hub_token", lambda **k: None)
    checks = grokbot_ops.doctor(tmp_path, ROLE, client_id="alpha")
    assert {check["check"]: check["status"] for check in checks}["endpoint"] == ("ok" if field is None else "fail")


def test_legacy_doctor_endpoint_does_not_require_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GROKBOT_BEARER", "not-a-real-token")
    assert _setup(tmp_path, ROLE) == 0
    monkeypatch.setattr(grokbot_ops, "_request_json", lambda *a, **k: {"ok": True, "service": "grokbot-mcp"})
    checks = grokbot_ops.doctor(tmp_path, ROLE)
    assert {check["check"]: check["status"] for check in checks}["endpoint"] == "ok"


@pytest.mark.parametrize("command", ["doctor", "canary", "install-service"])
def test_cli_resolves_only_selected_client_config(tmp_path, command):
    assert _setup(tmp_path, ROLE, ["--client-id", "alpha"]) == 0
    assert (
        cli.main(
            ["run", "cloud", "grokbot", command, "--target", str(tmp_path), "--instance", ROLE, "--client-id", "beta"]
        )
        != 0
    )


def test_hub_whoami_projects_only_authenticated_node_identity(tmp_path, monkeypatch):
    from brigade import fleet_hub, fleet_hub_grokbot

    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        for name in ("alpha", "beta"):
            status, _ = fleet_hub_grokbot.handle_grokbot(
                conn,
                {
                    "action": "enroll-actor",
                    "enroll_node_id": name,
                    "queue_owner_node_id": "alpha",
                    "queue_id": "alpha",
                    "actor_kind": ROLE,
                    "role": ROLE,
                    "enabled": True,
                },
            )
            assert status == 200
        monkeypatch.setattr(
            fleet_client_grokbot, "load_fleet_config", lambda: {"hub_url": "https://hub.example", "token": "fake-token"}
        )
        for name in ("alpha", "beta"):
            requests = []

            def post(hub, token, body, timeout, name=name, requests=requests):
                requests.append(body)
                return fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=name)

            assert fleet_hub_grokbot.handle_grokbot(conn, {"action": "whoami"}, caller_node=name) == (
                200,
                {"actor_kind": ROLE, "role": ROLE},
            )
            assert fleet_hub_grokbot.handle_grokbot(
                conn, {"action": "whoami", "include_node_id": True}, caller_node=name
            ) == (200, {"actor_kind": ROLE, "role": ROLE, "node_id": name})

            monkeypatch.setattr(
                fleet_client_grokbot,
                "_post_grokbot_blocking",
                post,
            )
            monkeypatch.setattr(fleet_client_grokbot._client, "_run_with_deadline", lambda fn, timeout: fn())
            with fleet_client_grokbot.listener_identity("fake-token"):
                assert fleet_client_grokbot.whoami().job == {"actor_kind": ROLE, "role": ROLE}
                assert requests[-1] == {"action": "whoami"}
                assert fleet_client_grokbot.whoami(include_node_id=True, node_id="spoofed").job == {
                    "actor_kind": ROLE,
                    "role": ROLE,
                    "node_id": name,
                }
                assert requests[-1] == {"action": "whoami", "include_node_id": True}
            assert fleet_hub_grokbot.handle_grokbot(
                conn, {"action": "whoami", "include_node_id": False}, caller_node=name
            ) == (200, {"actor_kind": ROLE, "role": ROLE})
        with pytest.raises(fleet_hub.FleetHubError):
            fleet_hub_grokbot.handle_grokbot(
                conn, {"action": "whoami", "include_node_id": True, "node_id": "alpha"}, caller_node="beta"
            )
    finally:
        conn.close()


@pytest.mark.parametrize("option", [None, 0, 1, "true", [], {}])
def test_whoami_identity_option_refuses_non_booleans(tmp_path, monkeypatch, option):
    from brigade import fleet_hub, fleet_hub_grokbot

    def unexpected_transport(*args, **kwargs):
        pytest.fail("malformed identity option must be refused before transport")

    monkeypatch.setattr(fleet_client_grokbot, "_post_grokbot_blocking", unexpected_transport)
    monkeypatch.setattr(
        fleet_client_grokbot, "load_fleet_config", lambda: {"hub_url": "https://hub.example", "token": "fake-token"}
    )
    monkeypatch.setattr(fleet_client_grokbot._client, "_run_with_deadline", lambda fn, timeout: fn())
    with fleet_client_grokbot.listener_identity("fake-token"):
        decision = fleet_client_grokbot.whoami(include_node_id=option)
    assert not decision.granted
    assert decision.reason == "invalid-request"
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        with pytest.raises(fleet_hub.FleetHubError, match="include_node_id.*boolean"):
            fleet_hub_grokbot.handle_grokbot(conn, {"action": "whoami", "include_node_id": option}, caller_node="alpha")
    finally:
        conn.close()


def test_identity_option_is_accepted_only_for_read_only_whoami(tmp_path):
    from brigade import fleet_hub, fleet_hub_grokbot

    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        for action in fleet_hub_grokbot.ACTIONS - {"whoami"}:
            with pytest.raises(fleet_hub.FleetHubError, match="include_node_id.*not accepted"):
                fleet_hub_grokbot.handle_grokbot(conn, {"action": action, "include_node_id": True}, caller_node="alpha")
    finally:
        conn.close()


@pytest.mark.parametrize("response", [(200, {"actor_kind": ROLE, "role": ROLE}), (400, {"error": "unsupported"})])
def test_older_hub_fails_closed_for_identity_opt_in(tmp_path, monkeypatch, response):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: True)
    monkeypatch.setattr(
        fleet_client_grokbot, "load_fleet_config", lambda: {"hub_url": "https://hub.example", "token": "fake-token"}
    )
    requests = []

    def post(hub, token, body, timeout):
        requests.append(body)
        return response

    monkeypatch.setattr(fleet_client_grokbot, "_post_grokbot_blocking", post)
    monkeypatch.setattr(fleet_client_grokbot._client, "_run_with_deadline", lambda fn, timeout: fn())
    with fleet_client_grokbot.listener_identity("fake-token"):
        decision = fleet_client_grokbot.whoami(include_node_id=True)
    assert not decision.granted
    adapter = grokbot_mcp.GrokbotAdapter(replace(_adapter(tmp_path, hub_token="fake-token").config, client_id="alpha"))
    with pytest.raises(grokbot_mcp.ConfigurationError):
        adapter.ensure_hub_actor()
    assert requests == [{"action": "whoami", "include_node_id": True}] * 2


def test_unnamed_hub_listener_uses_exact_legacy_whoami(tmp_path, monkeypatch):
    monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: True)
    monkeypatch.setattr(
        fleet_client_grokbot, "load_fleet_config", lambda: {"hub_url": "https://hub.example", "token": "fake-token"}
    )

    def post(hub, token, body, timeout):
        assert body == {"action": "whoami"}
        return 200, {"actor_kind": ROLE, "role": ROLE, "node_id": "unsolicited"}

    monkeypatch.setattr(fleet_client_grokbot, "_post_grokbot_blocking", post)
    monkeypatch.setattr(fleet_client_grokbot._client, "_run_with_deadline", lambda fn, timeout: fn())
    with fleet_client_grokbot.listener_identity("fake-token"):
        assert fleet_client_grokbot.whoami().job == {"actor_kind": ROLE, "role": ROLE}
    adapter = _adapter(tmp_path, hub_token="fake-token")
    adapter.ensure_hub_actor()


def test_distinct_clients_keep_credential_attribution_through_hub_lifecycle(tmp_path, monkeypatch):
    from brigade import fleet_hub, fleet_hub_grokbot

    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        for name, kind in (("alpha-feed", "feed"), ("alpha", ROLE), ("beta", ROLE)):
            body = {
                "action": "enroll-actor",
                "enroll_node_id": name,
                "queue_owner_node_id": "alpha",
                "queue_id": "alpha",
                "actor_kind": kind,
                "enabled": True,
            }
            if kind == ROLE:
                body["role"] = ROLE
            assert fleet_hub_grokbot.handle_grokbot(conn, body)[0] == 200
        monkeypatch.setattr(grokbot_jobs, "hub_authority", lambda target: True)
        monkeypatch.setattr(
            fleet_client_grokbot, "load_fleet_config", lambda: {"hub_url": "https://hub.example", "token": "fake-token"}
        )
        monkeypatch.setattr(
            fleet_client_grokbot,
            "_post_grokbot_blocking",
            lambda hub, token, body, timeout: fleet_hub_grokbot.handle_grokbot(
                conn, body, caller_node=token.removeprefix("fake-")
            ),
        )
        monkeypatch.setattr(fleet_client_grokbot._client, "_run_with_deadline", lambda fn, timeout: fn())
        for name in ("alpha", "beta"):
            with fleet_client_grokbot.listener_identity("fake-alpha-feed"):
                job_id = grokbot_jobs.enqueue(tmp_path, _spec(ROLE), name)["job_id"]
            adapter = grokbot_mcp.GrokbotAdapter(
                replace(_adapter(tmp_path, hub_token=f"fake-{name}").config, client_id=name)
            )
            args = {"job_id": job_id, "lease_id": f"lease-{name}"}
            assert adapter.call_tool("grokbot_queue_claim", args)["bot_id"] == name
            other_name = "beta" if name == "alpha" else "alpha"
            other = grokbot_mcp.GrokbotAdapter(
                replace(_adapter(tmp_path, hub_token=f"fake-{other_name}").config, client_id=other_name)
            )
            with pytest.raises(grokbot_mcp.AdapterError):
                other.call_tool("grokbot_queue_start", args)
            for tool in ("renew", "start", "fail"):
                result = adapter.call_tool(f"grokbot_queue_{tool}", args)
                assert result["bot_id"] == name
            assert result["state"] == "failed"
    finally:
        conn.close()
