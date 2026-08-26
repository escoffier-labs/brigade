"""Fleet-authoritative cloud admission and safe policy contracts."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager

import pytest

from brigade import cli, fleet_client, fleet_command_deck as deck, fleet_hub


NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
ADMIN_TOKEN = "test-admin-token"


def _admit(
    provider: str = "cursor",
    *,
    lease_id: str = "lease-a",
    node: str = NODE_A,
    holder: str = "holder-a",
    **extra: object,
) -> dict[str, object]:
    return {
        "action": "admit",
        "provider": provider,
        "lease_id": lease_id,
        "node_id": node,
        "holder": holder,
        "repo": "repo-a",
        "label": "safe label",
        "prompt_hash": "a" * 64,
        **extra,
    }


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    yield connection
    connection.close()


def test_cloud_config_defaults_and_validates_hosted_pool(tmp_path):
    path = tmp_path / "deck.json"
    path.write_text('{"stations":[{"node_id":"node-a","name":"A","capacity":1}]}')
    config = deck.load_config(path)
    assert config.cloud.global_limit == 4
    assert config.cloud.providers["cursor"].limit == 3
    assert config.cloud.providers["codex"].limit == 2
    assert config.cloud.providers["claude"].limit == 0
    assert config.cloud.providers["jules"].limit == 15
    assert config.cloud.providers["grok-bot"].hosted is False
    path.write_text(
        '{"stations":[{"node_id":"node-a","name":"A","capacity":1}],'
        '"cloud":{"global_limit":5,"providers":{"cursor":{"limit":7,"enabled":false}}}}'
    )
    assert deck.load_config(path).cloud.providers["cursor"].enabled is False


def test_atomic_admission_checks_provider_and_global_caps(tmp_path):
    db = tmp_path / "fleet.db"
    fleet_hub.init_db(db).close()
    defaults = deck.default_cloud_config()
    config = deck.DeckConfig(cloud=deck.CloudConfig(global_limit=5, providers=defaults.providers))
    barrier = threading.Barrier(6)
    outcomes: list[tuple[str, bool]] = []
    lock = threading.Lock()

    def contend(index: int, provider: str) -> None:
        connection = fleet_hub.open_db(db)
        try:
            barrier.wait()
            status, payload = fleet_hub.handle_cloud(
                connection,
                _admit(provider, lease_id=f"lease-{index}", node=NODE_A, holder=f"holder-{index}"),
                caller_node=NODE_A,
                config=config,
            )
            with lock:
                outcomes.append((provider, status == 200 and payload.get("admitted") is True))
        finally:
            connection.close()

    threads = [threading.Thread(target=contend, args=(index, "cursor")) for index in range(4)]
    threads += [threading.Thread(target=contend, args=(index + 4, "codex")) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(granted for _provider, granted in outcomes) == 5
    assert sum(granted for provider, granted in outcomes if provider == "cursor") == 3


def test_cloud_lease_is_idempotent_fenced_expiring_and_sanitized(conn, monkeypatch):
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: clock["now"])
    config = deck.DeckConfig()
    status, first = fleet_hub.handle_cloud(
        conn, _admit(label="hello\x00", ttl_seconds=2), caller_node=NODE_A, config=config
    )
    assert status == 200 and first["admitted"] is True
    status, again = fleet_hub.handle_cloud(
        conn, _admit(label="ignored", ttl_seconds=30), caller_node=NODE_A, config=config
    )
    assert status == 200 and again["lease"]["admitted_at"] == first["lease"]["admitted_at"]
    status, refused = fleet_hub.handle_cloud(
        conn,
        {"action": "renew", "lease_id": "lease-a", "node_id": NODE_A, "holder": "wrong"},
        caller_node=NODE_A,
        config=config,
    )
    assert status == 409 and refused["renewed"] is False
    status, bound = fleet_hub.handle_cloud(
        conn,
        {
            "action": "bind",
            "lease_id": "lease-a",
            "node_id": NODE_A,
            "holder": "holder-a",
            "provider_task_id": "task-1",
        },
        caller_node=NODE_A,
        config=config,
    )
    assert status == 200 and bound["bound"] is True
    safe = fleet_hub.list_cloud_leases(conn, include_all=True)
    assert safe[0]["label"] == "hello"
    assert safe[0]["provider_task_id"] == "task-1"
    assert "holder" not in safe[0] and "prompt_hash" not in safe[0]
    clock["now"] += 3
    assert fleet_hub.list_cloud_leases(conn) == []
    assert fleet_hub.list_cloud_leases(conn, include_all=True)[0]["expired"] is True


@pytest.mark.parametrize(
    ("raw_provider", "canonical"),
    [
        ("cursor-cloud", "cursor"),
        ("codex-cloud", "codex"),
        ("claude-cloud", "claude"),
        ("grokbot-cloud", "grok-bot"),
        ("jules", "jules"),
    ],
)
def test_cloud_provider_aliases_are_stored_and_compared_canonically(conn, raw_provider, canonical):
    config = deck.DeckConfig()
    status, payload = fleet_hub.handle_cloud(
        conn,
        {"action": "policy", "provider": raw_provider, "enabled": True, "limit": 1},
        config=config,
    )
    assert status == 200 and payload["policy"]["provider"] == canonical
    assert conn.execute("SELECT provider FROM cloud_provider_state").fetchone()[0] == canonical

    if canonical == "cursor":
        status, admitted = fleet_hub.handle_cloud(
            conn, _admit(raw_provider, lease_id="canonical-lease"), caller_node=NODE_A, config=config
        )
        assert status == 200 and admitted["lease"]["provider"] == "cursor"
        assert fleet_hub.cloud_snapshot(conn, config)["leases"][0]["provider"] == "cursor"


def test_node_cannot_mutate_policy_or_another_nodes_lease(conn):
    config = deck.DeckConfig()
    with pytest.raises(fleet_hub.FleetHubForbidden):
        fleet_hub.handle_cloud(
            conn, {"action": "policy", "provider": "cursor", "enabled": False}, caller_node=NODE_A, config=config
        )
    fleet_hub.handle_cloud(conn, _admit(), caller_node=NODE_A, config=config)
    with pytest.raises(fleet_hub.FleetHubForbidden):
        fleet_hub.handle_cloud(
            conn,
            _admit(lease_id="lease-b", node=NODE_A, holder="holder-b"),
            caller_node=NODE_B,
            config=config,
        )
    status, policy = fleet_hub.handle_cloud(
        conn,
        {"action": "policy", "provider": "ox-alpha", "enabled": False, "reason": "disabled by policy"},
        caller_node=None,
        config=config,
    )
    assert status == 200 and policy["policy"]["provider"] == "ox-alpha"
    assert fleet_hub.list_model_policy(conn)[0]["provider"] == "ox-alpha"


def test_v4_migration_preserves_existing_rows(tmp_path):
    db = tmp_path / "legacy.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE events (node_id TEXT, run_id TEXT, sequence INTEGER, digest TEXT, repo TEXT, seat TEXT, harness TEXT, state TEXT, ts TEXT, received_at TEXT, PRIMARY KEY (node_id, run_id, sequence, digest))"
    )
    old.execute(
        "INSERT INTO events VALUES ('node-a', 'run-a', 1, 'd', 'repo', 'seat', 'harness', 'run.created', 't', 't')"
    )
    old.execute(
        "CREATE TABLE nodes (node_id TEXT PRIMARY KEY, token_sha256 TEXT UNIQUE, label TEXT, created_at TEXT, revoked_at TEXT)"
    )
    old.execute("PRAGMA user_version=4")
    old.commit()
    old.close()
    migrated = fleet_hub.init_db(db)
    try:
        assert migrated.execute("SELECT run_id FROM events").fetchone()[0] == "run-a"
        assert migrated.execute("SELECT COUNT(*) FROM cloud_leases").fetchone()[0] == 0
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 9
    finally:
        migrated.close()


def test_client_fails_closed_for_configured_hub(monkeypatch):
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "token"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    monkeypatch.setattr(
        fleet_client, "_post_cloud_blocking", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("down"))
    )
    decision = fleet_client.admit_cloud("cursor", repo="repo-a")
    assert decision.granted is False and decision.reason == "hub-unavailable"


def test_client_preserves_request_lease_id_when_granted_projection_omits_it(monkeypatch):
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "token"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "_post_cloud_blocking",
        lambda *args, **kwargs: (200, {"admitted": True, "lease": {"provider": "cursor"}}),
    )
    decision = fleet_client._cloud_op("admit", provider="cursor", lease_id="client-lease")
    assert decision.granted is True
    assert decision.lease is not None
    assert decision.lease["lease_id"] == "client-lease"


def test_cloud_and_policy_cli_views_are_read_only(monkeypatch, capsys):
    monkeypatch.setattr(
        fleet_client, "fetch_cloud", lambda include_all=False: {"leases": [], "policy": {"providers": []}}
    )
    monkeypatch.setattr(fleet_client, "fetch_model_policy", lambda: [{"provider": "ox-alpha", "enabled": False}])
    assert cli.main(["fleet", "cloud", "--json"]) == 0
    assert '"leases": []' in capsys.readouterr().out
    assert cli.main(["fleet", "models", "--json"]) == 0
    assert '"ox-alpha"' in capsys.readouterr().out


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


def test_cloud_http_routes_require_valid_auth_and_keep_dashboard_read_only(tmp_path):
    with _hub(tmp_path) as hub:
        assert _request(hub, "GET", "/cloud")[0] == 401
        assert _request(hub, "GET", "/models", token="unknown-token")[0] == 401
        db = fleet_hub.open_db(hub[2])
        try:
            _node, node_token = fleet_hub.add_node(db, NODE_A, "node-a")
            fleet_hub.revoke_node(db, NODE_A)
        finally:
            db.close()
        assert _request(hub, "GET", "/cloud", token=node_token)[0] == 401
        status, snapshot = _request(hub, "GET", "/cloud", token=ADMIN_TOKEN)
        assert status == 200 and snapshot["leases"] == []
        assert _request(hub, "GET", "/models", token=ADMIN_TOKEN) == (200, {"models": []})
        # Dashboard routes stay GET-only and cannot turn a bearer into a cloud write.
        assert _request(hub, "POST", "/", token=ADMIN_TOKEN, body={})[0] == 404


def test_cloud_http_post_binds_node_token_to_lease_owner(tmp_path):
    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            _node, node_token = fleet_hub.add_node(db, NODE_A, "node-a")
        finally:
            db.close()
        status, payload = _request(hub, "POST", "/cloud", token=node_token, body=_admit())
        assert status == 200 and payload["admitted"] is True
        status, payload = _request(hub, "POST", "/cloud", token=node_token, body=_admit(node=NODE_B))
        assert status == 403 and "holder-a" not in payload["error"]


def test_cloud_http_node_reads_and_admin_model_policy_mutation(tmp_path):
    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            _node, node_token = fleet_hub.add_node(db, NODE_A, "node-a")
        finally:
            db.close()
        status, snapshot = _request(hub, "GET", "/cloud", token=node_token)
        assert status == 200 and snapshot["leases"] == [] and snapshot["policy"]["global_limit"] == 4
        assert _request(hub, "GET", "/models", token=node_token) == (200, {"models": []})
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=node_token,
            body={"action": "set", "provider": "openai", "model": "gpt-5.6-terra", "seat": "coder", "enabled": True},
        )
        assert status == 403 and "admin token" in payload["error"]
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set",
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "seat": "coder",
                "enabled": True,
                "limit": 2,
                "notes": "primary worker",
            },
        )
        assert status == 200 and payload["policy"] == {
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "seat": "coder",
            "enabled": True,
            "limit": 2,
            "notes": "primary worker",
        }
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={"action": "set", "provider": "openai", "model": "gpt-5.6-terra", "seat": "coder", "enabled": False},
        )
        assert status == 200 and payload["policy"]["enabled"] is False
        assert _request(hub, "GET", "/models", token=node_token) == (
            200,
            {
                "models": [
                    {
                        "provider": "openai",
                        "model": "gpt-5.6-terra",
                        "seat": "coder",
                        "enabled": False,
                        "limit": None,
                        "notes": None,
                    }
                ]
            },
        )


@pytest.mark.parametrize(
    "body",
    [
        {
            "action": "set",
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "seat": "coder",
            "enabled": True,
            "token": "nope",
        },
        {"action": "set", "provider": "OpenAI", "model": "gpt-5.6-terra", "seat": "coder", "enabled": True},
        {"action": "set", "provider": "openai", "model": "gpt-5.6-terra", "seat": "coder", "enabled": 1},
        {
            "action": "set",
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "seat": "coder",
            "enabled": True,
            "limit": -1,
        },
    ],
)
def test_model_policy_rejects_unknown_and_unsafe_fields(tmp_path, body):
    with _hub(tmp_path) as hub:
        assert _request(hub, "POST", "/models", token=ADMIN_TOKEN, body=body)[0] == 400


def test_admin_cloud_lease_writes_need_allow_admin_writes(tmp_path):
    with _hub(tmp_path) as hub:
        status, payload = _request(hub, "POST", "/cloud", token=ADMIN_TOKEN, body=_admit())
        assert status == 403 and "--allow-admin-writes" in payload["error"]


def test_model_policy_cli_set_uses_bounded_admin_client(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def _set(provider, model, seat, *, enabled, limit=None, notes=None):
        captured.update(provider=provider, model=model, seat=seat, enabled=enabled, limit=limit, notes=notes)
        return {"provider": provider, "model": model, "seat": seat, "enabled": enabled, "limit": limit, "notes": notes}

    monkeypatch.setattr(fleet_client, "set_model_policy", _set)
    assert (
        cli.main(
            [
                "fleet",
                "models",
                "set",
                "openai",
                "gpt-5.6-terra",
                "coder",
                "--disable",
                "--limit",
                "2",
                "--notes",
                "paused",
                "--json",
            ]
        )
        == 0
    )
    assert captured == {
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "seat": "coder",
        "enabled": False,
        "limit": 2,
        "notes": "paused",
    }
    assert '"enabled": false' in capsys.readouterr().out


def test_two_seats_can_share_one_model(tmp_path):
    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            _node, node_token = fleet_hub.add_node(db, NODE_A, "node-a")
        finally:
            db.close()
        for seat in ("coder", "reviewer"):
            status, _payload = _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "set",
                    "provider": "openai",
                    "model": "gpt-5.6-terra",
                    "seat": seat,
                    "enabled": True,
                    "limit": 1,
                },
            )
            assert status == 200
        status, payload = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        models = [row for row in payload["models"] if row.get("seat") is not None]
        assert {row["seat"] for row in models} == {"coder", "reviewer"}
        assert all(row["provider"] == "openai" and row["model"] == "gpt-5.6-terra" for row in models)


def test_changing_seat_route_replaces_only_that_seat(tmp_path):
    with _hub(tmp_path) as hub:
        for seat, model in (("coder", "gpt-5.6-terra"), ("reviewer", "gpt-5.6-terra")):
            _request(
                hub,
                "POST",
                "/models",
                token=ADMIN_TOKEN,
                body={
                    "action": "set",
                    "provider": "openai",
                    "model": model,
                    "seat": seat,
                    "enabled": True,
                },
            )
        status, _payload = _request(
            hub,
            "POST",
            "/models",
            token=ADMIN_TOKEN,
            body={
                "action": "set",
                "provider": "openai",
                "model": "gpt-5.5",
                "seat": "coder",
                "enabled": True,
            },
        )
        assert status == 200
        status, payload = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        by_seat = {row["seat"]: row for row in payload["models"] if row.get("seat") is not None}
        assert by_seat["coder"]["model"] == "gpt-5.5"
        assert by_seat["reviewer"]["model"] == "gpt-5.6-terra"


def test_set_model_policy_fails_closed_on_oversized_response(monkeypatch):
    class _OversizedResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args, **kwargs):
            return False

        def read(self, size=-1):
            return b"x" * (fleet_client.MAX_CLOUD_RESPONSE_BYTES + 1)

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "http://hub", "admin_token": "admin-token", "node_token": ""},
    )
    monkeypatch.setattr(fleet_client, "_hub_open", lambda _request, timeout: _OversizedResponse())
    with pytest.raises(fleet_client.FleetClientError):
        fleet_client.set_model_policy("openai", "gpt-5.6-terra", "coder", enabled=True)


def test_idempotent_admit_refuses_released_and_expired_leases(conn, monkeypatch):
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: clock["now"])
    config = deck.DeckConfig()
    assert fleet_hub.handle_cloud(conn, _admit(lease_id="released"), caller_node=NODE_A, config=config)[0] == 200
    assert (
        fleet_hub.handle_cloud(
            conn,
            {"action": "release", "lease_id": "released", "node_id": NODE_A, "holder": "holder-a"},
            caller_node=NODE_A,
            config=config,
        )[0]
        == 200
    )
    status, released = fleet_hub.handle_cloud(conn, _admit(lease_id="released"), caller_node=NODE_A, config=config)
    assert status == 409 and released["admitted"] is False
    assert (
        fleet_hub.handle_cloud(
            conn, _admit(lease_id="expired", holder="holder-expired", ttl_seconds=1), caller_node=NODE_A, config=config
        )[0]
        == 200
    )
    clock["now"] += 2
    status, expired = fleet_hub.handle_cloud(
        conn, _admit(lease_id="expired", holder="holder-expired"), caller_node=NODE_A, config=config
    )
    assert status == 409 and expired["admitted"] is False
