"""Fleet-authoritative cloud admission and safe policy contracts."""

from __future__ import annotations

import hashlib
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
        assert migrated.execute("SELECT COUNT(*) FROM grokbot_jobs").fetchone()[0] == 0
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
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


def test_admit_cloud_normalizes_prefixed_prompt_hash_on_hub_wire(monkeypatch):
    """cloud_tracker stores sha256:<hex>; the hub accepts bare hex only."""
    bodies: list[dict[str, object]] = []

    def capture_post(_hub, _token, body, *, timeout):
        bodies.append(dict(body))
        return 200, {"admitted": True, "lease": {"provider": "cursor", "lease_id": body["lease_id"]}}

    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "token"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    monkeypatch.setattr(fleet_client, "_post_cloud_blocking", capture_post)

    prompt = "rotate the production credentials"
    prefixed = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
    bare = prefixed.removeprefix("sha256:")

    decision = fleet_client.admit_cloud("cursor", repo="repo-a", prompt_hash=prefixed)
    assert decision.granted is True
    assert bodies[0]["prompt_hash"] == bare
    assert bodies[0]["prompt_hash"] != prefixed

    decision = fleet_client.admit_cloud("cursor", repo="repo-a", prompt_hash=bare)
    assert decision.granted is True
    assert bodies[1]["prompt_hash"] == bare


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


def _models_revision(hub, *, token: str = ADMIN_TOKEN) -> int:
    status, payload = _request(hub, "GET", "/models", token=token)
    assert status == 200
    return int(payload.get("revision", 1))


def _admin_set_model(hub, **fields: object):
    body = {"action": "set", "expected_revision": _models_revision(hub), **fields}
    return _request(hub, "POST", "/models", token=ADMIN_TOKEN, body=body)


def _policy_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()
    return int(row[0]) if row is not None else 1


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
        status, models = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        assert models["models"] == []
        assert models["schema"] == "brigade.fleet_model_roster.v1"
        assert "audience_node_id" not in models
        assert "mac" not in models
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
        status, empty = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        assert empty["models"] == []
        assert empty["schema"] == "brigade.fleet_model_roster.v1"
        assert empty["audience_node_id"] == NODE_A
        assert empty["mac"]["algorithm"] == "hmac-sha256-node-bearer-v1"
        status, payload = _request(
            hub,
            "POST",
            "/models",
            token=node_token,
            body={"action": "set", "provider": "openai", "model": "gpt-5.6-terra", "seat": "coder", "enabled": True},
        )
        assert status == 403 and "admin token" in payload["error"]
        status, payload = _admin_set_model(
            hub,
            provider="openai",
            model="gpt-5.6-terra",
            seat="coder",
            enabled=True,
            limit=2,
            notes="primary worker",
        )
        assert status == 200 and payload["policy"]["provider"] == "openai"
        assert payload["policy"]["model"] == "gpt-5.6-terra"
        assert payload["policy"]["seat"] == "coder"
        assert payload["policy"]["enabled"] is True
        assert payload["policy"]["limit"] == 2
        assert payload["policy"]["notes"] == "primary worker"
        status, payload = _admin_set_model(hub, provider="openai", model="gpt-5.6-terra", seat="coder", enabled=False)
        assert status == 200 and payload["policy"]["enabled"] is False
        status, listed = _request(hub, "GET", "/models", token=node_token)
        assert status == 200
        assert listed["models"] == [
            {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "seat": "coder",
                "enabled": False,
                "limit": None,
                "notes": None,
            }
        ]
        assert listed["schema"] == "brigade.fleet_model_roster.v1"


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
        {
            "action": "set",
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "seat": "coder",
            "enabled": True,
            "lease_id": "lease-only-field-must-not-set-policy",
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
            status, _payload = _admin_set_model(
                hub,
                provider="openai",
                model="gpt-5.6-terra",
                seat=seat,
                enabled=True,
                limit=1,
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
            _admin_set_model(hub, provider="openai", model=model, seat=seat, enabled=True)
        status, _payload = _admin_set_model(hub, provider="openai", model="gpt-5.6-sol", seat="coder", enabled=True)
        assert status == 200
        status, payload = _request(hub, "GET", "/models", token=ADMIN_TOKEN)
        assert status == 200
        by_seat = {row["seat"]: row for row in payload["models"] if row.get("seat") is not None}
        assert by_seat["coder"]["model"] == "gpt-5.6-sol"
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


def test_model_policy_limit_is_an_atomic_fenced_expiring_lease(conn, monkeypatch):
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: clock["now"])
    fleet_hub.set_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": _policy_revision(conn),
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "seat": "coder",
            "enabled": True,
            "limit": 0,
        },
    )
    first = {
        "action": "acquire",
        "lease_id": "model-a",
        "node_id": NODE_A,
        "holder": "fence-a",
        "seat": "coder",
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "ttl_seconds": 2,
    }
    assert fleet_hub.handle_model_policy(conn, first, caller_node=NODE_A) == (
        409,
        {"acquired": False, "error": "model policy capacity is exhausted"},
    )
    fleet_hub.set_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": _policy_revision(conn),
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "seat": "coder",
            "enabled": True,
            "limit": 1,
        },
    )
    assert fleet_hub.handle_model_policy(conn, first, caller_node=NODE_A)[0] == 200
    second = dict(first, lease_id="model-b", holder="fence-b")
    assert fleet_hub.handle_model_policy(conn, second, caller_node=NODE_A) == (
        409,
        {"acquired": False, "error": "model policy capacity is exhausted"},
    )
    assert (
        fleet_hub.handle_model_policy(
            conn, {"action": "release", "lease_id": "model-a", "node_id": NODE_A, "holder": "wrong"}, caller_node=NODE_A
        )[0]
        == 409
    )
    clock["now"] += 3
    assert fleet_hub.handle_model_policy(conn, second, caller_node=NODE_A)[0] == 200


FEED_NODE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKER_NODE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_WORKER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
OPERATOR_NODE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SCOUT_NODE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OTHER_FEED = "ffffffff-ffff-4fff-8fff-ffffffffffff"
QUEUE_ID = "grokbot-queue-main"
OTHER_QUEUE = "grokbot-queue-other"


def _enroll_actors(conn, *, queue_id: str = QUEUE_ID) -> None:
    from brigade import fleet_hub_grokbot

    for node, kind, role in (
        (FEED_NODE, "feed", None),
        (WORKER_NODE, "implementation-worker", "implementation-worker"),
        (OTHER_WORKER, "implementation-worker", "implementation-worker"),
        (OPERATOR_NODE, "operator", None),
        (SCOUT_NODE, "repository-scout", "repository-scout"),
    ):
        body = {
            "action": "enroll-actor",
            "enroll_node_id": node,
            "queue_owner_node_id": FEED_NODE,
            "queue_id": queue_id,
            "actor_kind": kind,
            "enabled": True,
        }
        if role is not None:
            body["role"] = role
        status, payload = fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=None)
        assert status == 200, payload


def _grokbot_enqueue(
    job_id: str = "grokbot-" + "a" * 24,
    digest: str = "b" * 64,
    *,
    role: str = "implementation-worker",
    artifact_kind: str = "draft-pr",
    operation_id: str = "op-enqueue-1",
) -> dict[str, object]:
    return {
        "action": "enqueue",
        "job_id": job_id,
        "role": role,
        "repository": "example/brigade",
        "label": "safe label",
        "task_digest": digest,
        "idempotency_key_hash": digest,
        "timeout_seconds": 900,
        "artifact_kind": artifact_kind,
        "private_snapshot_id": job_id,
        "operation_id": operation_id,
    }


def _claim_body(job_id: str, *, lease_id: str = "lease-a", revision: int = 1, operation_id: str = "op-claim-1"):
    return {
        "action": "claim",
        "job_id": job_id,
        "lease_id": lease_id,
        "expected_item_revision": revision,
        "lease_seconds": 300,
        "operation_id": operation_id,
    }


def _enroll_actor(conn, node_id: str, *, kind: str, queue_id: str, role: str | None = None) -> None:
    from brigade import fleet_hub_grokbot

    body = {
        "action": "enroll-actor",
        "enroll_node_id": node_id,
        "queue_owner_node_id": node_id if queue_id == OTHER_QUEUE else FEED_NODE,
        "queue_id": queue_id,
        "actor_kind": kind,
        "enabled": True,
    }
    if role is not None:
        body["role"] = role
    status, payload = fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=None)
    assert status == 200, payload


def _holder_body(
    action: str,
    job_id: str,
    *,
    revision: int,
    generation: int,
    lease_id: str = "lease-a",
    operation_id: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "action": action,
        "job_id": job_id,
        "lease_id": lease_id,
        "expected_item_revision": revision,
        "lease_generation": generation,
        "operation_id": operation_id or f"op-{action}-1",
    }
    if action == "renew":
        body["lease_seconds"] = 300
    if action == "complete":
        body["artifact"] = {
            "kind": "draft-pr",
            "ref": "https://github.com/example/brigade/pull/9",
            "private_snapshot_id": job_id,
        }
    return body


def _refuse_cross_actor(conn, body: dict[str, object], caller_node: str) -> None:
    from brigade import fleet_hub_grokbot

    try:
        status, payload = fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=caller_node)
    except fleet_hub.FleetHubForbidden:
        return
    assert status != 200
    assert payload.get("error") in {
        "operation-mismatch",
        "lease-conflict",
        "invalid-state",
        "idempotency-conflict",
        "revision-conflict",
    }


def test_grokbot_hub_is_authoritative_for_enqueue_claim_renew_and_replay(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    first = fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=FEED_NODE)
    assert first[0] == 200 and first[1]["idempotent"] is False
    same_op = fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=FEED_NODE)
    assert same_op[0] == 200
    assert same_op[1]["job"]["item_revision"] == first[1]["job"]["item_revision"] == 1
    assert same_op[1]["job"]["sequence"] == first[1]["job"]["sequence"] == 1
    replay = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue(operation_id="op-enqueue-retry"), caller_node=FEED_NODE
    )
    assert replay[0] == 200 and replay[1]["idempotent"] is True
    assert replay[1]["job"]["item_revision"] == first[1]["job"]["item_revision"] == 1
    assert replay[1]["job"]["sequence"] == first[1]["job"]["sequence"] == 1
    job_id = first[1]["job"]["job_id"]
    claimed = fleet_hub_grokbot.handle_grokbot(conn, _claim_body(job_id), caller_node=WORKER_NODE)
    assert claimed[0] == 200 and claimed[1]["job"]["state"] == "claimed"
    assert claimed[1]["lease_generation"] == 1
    lost = fleet_hub_grokbot.handle_grokbot(
        conn, _claim_body(job_id, lease_id="other", operation_id="op-claim-other"), caller_node=WORKER_NODE
    )
    assert lost[0] == 409
    generation = claimed[1]["lease_generation"]
    renewed = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "renew",
            "job_id": job_id,
            "lease_id": "lease-a",
            "expected_item_revision": claimed[1]["job"]["item_revision"],
            "lease_generation": generation,
            "lease_seconds": 300,
            "operation_id": "op-renew-1",
        },
        caller_node=WORKER_NODE,
    )
    assert renewed[0] == 200 and renewed[1]["job"]["item_revision"] == claimed[1]["job"]["item_revision"] + 1
    dumped = json.dumps(first[1]) + json.dumps(claimed[1]) + json.dumps(renewed[1])
    assert "PRIVATE" not in dumped
    assert "holder_token" not in dumped
    assert "lease-a" not in dumped
    events = conn.execute("SELECT state, sequence FROM events WHERE harness='grokbot' ORDER BY sequence").fetchall()
    assert [row[0] for row in events] == ["external.queued", "external.claimed", "external.heartbeat"]
    leases = fleet_hub.list_cloud_leases(conn)
    assert leases[0]["provider"] == "grok-bot"
    assert leases[0]["lease_id"] == job_id
    job_row = conn.execute(
        "SELECT lease_token_digest, lease_generation FROM grokbot_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    assert job_row[0] and job_row[0] != "lease-a"
    assert job_row[1] == 1
    cloud_holder = conn.execute("SELECT holder_token FROM cloud_leases WHERE lease_id=?", (job_id,)).fetchone()[0]
    assert cloud_holder != "lease-a" and len(cloud_holder) == 64


def test_grokbot_hub_cancel_expire_and_complete_release_capacity(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    queued = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue("grokbot-" + "c" * 24, "c" * 64, operation_id="op-enq-c"), caller_node=FEED_NODE
    )[1]["job"]
    canceled = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "cancel",
            "job_id": queued["job_id"],
            "expected_item_revision": 1,
            "operation_id": "op-cancel-c",
        },
        caller_node=OPERATOR_NODE,
    )
    assert canceled[0] == 200 and canceled[1]["job"]["state"] == "canceled"

    live = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue("grokbot-" + "d" * 24, "d" * 64, operation_id="op-enq-d"), caller_node=FEED_NODE
    )[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn, _claim_body(live["job_id"], lease_id="lease-d", operation_id="op-claim-d"), caller_node=WORKER_NODE
    )
    generation = claimed[1]["lease_generation"]
    fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "start",
            "job_id": live["job_id"],
            "lease_id": "lease-d",
            "expected_item_revision": 2,
            "lease_generation": generation,
            "operation_id": "op-start-d",
        },
        caller_node=WORKER_NODE,
    )
    completed = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "complete",
            "job_id": live["job_id"],
            "lease_id": "lease-d",
            "expected_item_revision": 3,
            "lease_generation": generation,
            "operation_id": "op-complete-d",
            "artifact": {
                "kind": "draft-pr",
                "ref": "https://github.com/example/brigade/pull/1",
                "private_snapshot_id": live["job_id"],
            },
        },
        caller_node=WORKER_NODE,
    )
    assert completed[0] == 200 and completed[1]["job"]["state"] == "completed"
    assert completed[1]["job"]["artifact_ref"] == "https://github.com/example/brigade/pull/1"
    assert fleet_hub.list_cloud_leases(conn) == []
    assert "report" not in json.dumps(completed[1])


def test_grokbot_hub_rejects_private_bodies_and_unenrolled_nodes(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub_grokbot.handle_grokbot(
            conn, {**_grokbot_enqueue(), "instructions": "do not store this"}, caller_node=FEED_NODE
        )
    with pytest.raises(fleet_hub.FleetHubForbidden):
        fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=NODE_B)
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub_grokbot.handle_grokbot(conn, {**_grokbot_enqueue(), "node_id": FEED_NODE}, caller_node=FEED_NODE)


def test_grokbot_actor_policy_isolates_node_queue_and_role(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    other_queue = "grokbot-queue-other"
    status, _payload = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "enroll-actor",
            "enroll_node_id": NODE_A,
            "queue_owner_node_id": NODE_A,
            "queue_id": other_queue,
            "actor_kind": "implementation-worker",
            "role": "implementation-worker",
            "enabled": True,
        },
        caller_node=None,
    )
    assert status == 200
    job = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue("grokbot-" + "e" * 24, "e" * 64, operation_id="op-enq-e"), caller_node=FEED_NODE
    )[1]["job"]
    scout = fleet_hub_grokbot.handle_grokbot(
        conn,
        _grokbot_enqueue(
            "grokbot-" + "f" * 24,
            "f" * 64,
            role="repository-scout",
            artifact_kind="report",
            operation_id="op-enq-f",
        ),
        caller_node=FEED_NODE,
    )[1]["job"]
    listed = fleet_hub_grokbot.handle_grokbot(conn, {"action": "list"}, caller_node=WORKER_NODE)
    assert [item["job_id"] for item in listed[1]["jobs"]] == [job["job_id"]]
    with pytest.raises(fleet_hub.FleetHubForbidden):
        fleet_hub_grokbot.handle_grokbot(
            conn, _claim_body(job["job_id"], operation_id="op-wrong-queue"), caller_node=NODE_A
        )
    with pytest.raises(fleet_hub.FleetHubForbidden):
        fleet_hub_grokbot.handle_grokbot(
            conn, _claim_body(scout["job_id"], operation_id="op-wrong-role"), caller_node=WORKER_NODE
        )
    with pytest.raises(fleet_hub.FleetHubForbidden):
        fleet_hub_grokbot.handle_grokbot(conn, {"action": "list"}, caller_node=FEED_NODE)
    with pytest.raises(fleet_hub.FleetHubForbidden):
        fleet_hub_grokbot.handle_grokbot(
            conn, {"action": "claim", **_claim_body(job["job_id"])}, caller_node=OPERATOR_NODE
        )


def test_grokbot_operation_replay_and_revision_fencing(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    queued = fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=FEED_NODE)[1]["job"]
    claim = _claim_body(queued["job_id"])
    first = fleet_hub_grokbot.handle_grokbot(conn, claim, caller_node=WORKER_NODE)
    replay = fleet_hub_grokbot.handle_grokbot(conn, claim, caller_node=WORKER_NODE)
    assert first[0] == replay[0] == 200
    assert replay[1]["job"]["item_revision"] == first[1]["job"]["item_revision"]
    assert conn.execute("SELECT COUNT(*) FROM events WHERE harness='grokbot'").fetchone()[0] == 2
    mismatch = fleet_hub_grokbot.handle_grokbot(conn, dict(claim, lease_seconds=120), caller_node=WORKER_NODE)
    assert mismatch[0] == 409 and mismatch[1]["error"] == "operation-mismatch"
    stale = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "start",
            "job_id": queued["job_id"],
            "lease_id": "lease-a",
            "expected_item_revision": 1,
            "lease_generation": first[1]["lease_generation"],
            "operation_id": "op-start-stale",
        },
        caller_node=WORKER_NODE,
    )
    assert stale[0] == 409 and stale[1]["error"] == "revision-conflict"
    wrong_gen = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "start",
            "job_id": queued["job_id"],
            "lease_id": "lease-a",
            "expected_item_revision": first[1]["job"]["item_revision"],
            "lease_generation": 99,
            "operation_id": "op-start-gen",
        },
        caller_node=WORKER_NODE,
    )
    assert wrong_gen[0] == 409
    wrong_token = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "start",
            "job_id": queued["job_id"],
            "lease_id": "not-the-lease",
            "expected_item_revision": first[1]["job"]["item_revision"],
            "lease_generation": first[1]["lease_generation"],
            "operation_id": "op-start-token",
        },
        caller_node=WORKER_NODE,
    )
    assert wrong_token[0] == 409


def test_grokbot_two_claimer_race_one_winner(tmp_path):
    from brigade import fleet_hub_grokbot

    db = tmp_path / "race.db"
    fleet_hub.init_db(db).close()
    setup = fleet_hub.open_db(db)
    try:
        _enroll_actors(setup)
        job = fleet_hub_grokbot.handle_grokbot(setup, _grokbot_enqueue(), caller_node=FEED_NODE)[1]["job"]
    finally:
        setup.close()
    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def contend(node: str, lease_id: str, operation_id: str) -> None:
        connection = fleet_hub.open_db(db)
        try:
            barrier.wait()
            status, _payload = fleet_hub_grokbot.handle_grokbot(
                connection,
                _claim_body(job["job_id"], lease_id=lease_id, operation_id=operation_id),
                caller_node=node,
            )
            with lock:
                outcomes.append(status)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=contend, args=(WORKER_NODE, "lease-a", "op-race-a")),
        threading.Thread(target=contend, args=(OTHER_WORKER, "lease-b", "op-race-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [200, 409]


def test_grokbot_report_artifact_rejects_local_path_and_hides_private_data(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    queued = fleet_hub_grokbot.handle_grokbot(
        conn,
        _grokbot_enqueue(
            "grokbot-" + "9" * 24,
            "9" * 64,
            role="repository-scout",
            artifact_kind="report",
            operation_id="op-enq-report",
        ),
        caller_node=FEED_NODE,
    )[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn,
        _claim_body(queued["job_id"], lease_id="lease-r", operation_id="op-claim-r"),
        caller_node=SCOUT_NODE,
    )
    generation = claimed[1]["lease_generation"]
    fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "start",
            "job_id": queued["job_id"],
            "lease_id": "lease-r",
            "expected_item_revision": 2,
            "lease_generation": generation,
            "operation_id": "op-start-r",
        },
        caller_node=SCOUT_NODE,
    )
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub_grokbot.handle_grokbot(
            conn,
            {
                "action": "complete",
                "job_id": queued["job_id"],
                "lease_id": "lease-r",
                "expected_item_revision": 3,
                "lease_generation": generation,
                "operation_id": "op-complete-path",
                "artifact": {
                    "kind": "report",
                    "ref": "docs/scout.md",
                    "digest": "a" * 64,
                    "size": 12,
                    "private_snapshot_id": queued["job_id"],
                },
            },
            caller_node=SCOUT_NODE,
        )
    completed = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "complete",
            "job_id": queued["job_id"],
            "lease_id": "lease-r",
            "expected_item_revision": 3,
            "lease_generation": generation,
            "operation_id": "op-complete-r",
            "artifact": {
                "kind": "report",
                "digest": "a" * 64,
                "size": 12,
                "private_snapshot_id": queued["job_id"],
            },
        },
        caller_node=SCOUT_NODE,
    )
    assert completed[0] == 200
    assert "artifact_ref" not in completed[1]["job"]
    assert completed[1]["job"]["artifact_digest"] == "a" * 64
    dumped = json.dumps(completed[1]) + json.dumps(
        conn.execute("SELECT * FROM events WHERE harness='grokbot'").fetchall()
    )
    assert "docs/scout.md" not in dumped
    assert "lease-r" not in dumped


def test_grokbot_http_get_is_removed_and_admin_cannot_list_unscoped(tmp_path):
    from brigade import fleet_hub_grokbot

    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            _node, feed_token = fleet_hub.add_node(db, FEED_NODE, "feed")
            _enroll_actors(db)
            fleet_hub_grokbot.handle_grokbot(db, _grokbot_enqueue(), caller_node=FEED_NODE)
        finally:
            db.close()
        assert _request(hub, "GET", "/grokbot", token=ADMIN_TOKEN)[0] == 404
        assert _request(hub, "GET", "/grokbot", token=feed_token)[0] == 404
        status, payload = _request(hub, "POST", "/grokbot", token=ADMIN_TOKEN, body={"action": "list"})
        assert status == 403
        status, payload = _request(hub, "POST", "/grokbot", token=feed_token, body={"action": "list"})
        assert status == 403


def test_grokbot_hub_projection_is_deck_only_and_terminal_jobs_release_capacity(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    live = fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=FEED_NODE)[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(conn, _claim_body(live["job_id"]), caller_node=WORKER_NODE)
    snapshot = fleet_hub.cloud_snapshot(conn, deck.DeckConfig())
    assert [job["job_id"] for job in snapshot["grokbot"]["active"]] == [live["job_id"]]
    assert snapshot["grokbot"]["history"] == []
    workers = deck.cloud_workers_from_snapshot(snapshot)
    grok = next(worker for worker in workers if worker.provider == "grok-bot")
    assert grok.used == 1
    generation = claimed[1]["lease_generation"]
    fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "start",
            "job_id": live["job_id"],
            "lease_id": "lease-a",
            "expected_item_revision": 2,
            "lease_generation": generation,
            "operation_id": "op-start-deck",
        },
        caller_node=WORKER_NODE,
    )
    fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "complete",
            "job_id": live["job_id"],
            "lease_id": "lease-a",
            "expected_item_revision": 3,
            "lease_generation": generation,
            "operation_id": "op-complete-deck",
            "artifact": {
                "kind": "draft-pr",
                "ref": "https://github.com/example/brigade/pull/2",
                "private_snapshot_id": live["job_id"],
            },
        },
        caller_node=WORKER_NODE,
    )
    after = fleet_hub.cloud_snapshot(conn, deck.DeckConfig())
    assert after["grokbot"]["active"] == []
    assert after["grokbot"]["history"][0]["state"] == "completed"
    grok_after = next(worker for worker in deck.cloud_workers_from_snapshot(after) if worker.provider == "grok-bot")
    assert grok_after.used == 0
    assert grok_after.leases == ()


def test_grokbot_operation_replay_cannot_cross_queue_or_node(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    _enroll_actor(conn, OTHER_FEED, kind="feed", queue_id=OTHER_QUEUE)
    _enroll_actor(conn, NODE_A, kind="implementation-worker", queue_id=OTHER_QUEUE, role="implementation-worker")
    first = fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=FEED_NODE)
    assert first[0] == 200 and first[1]["idempotent"] is False
    original = fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=FEED_NODE)
    assert original[0] == 200
    assert original[1]["job"]["item_revision"] == first[1]["job"]["item_revision"]
    assert original[1]["job"]["queue_id"] == QUEUE_ID
    _refuse_cross_actor(conn, _grokbot_enqueue(), OTHER_FEED)
    _refuse_cross_actor(conn, _grokbot_enqueue(operation_id="op-enqueue-foreign"), OTHER_FEED)
    job_id = first[1]["job"]["job_id"]
    claimed = fleet_hub_grokbot.handle_grokbot(conn, _claim_body(job_id), caller_node=WORKER_NODE)
    assert claimed[0] == 200
    claim_replay = fleet_hub_grokbot.handle_grokbot(conn, _claim_body(job_id), caller_node=WORKER_NODE)
    assert claim_replay[0] == 200
    assert claim_replay[1]["job"]["item_revision"] == claimed[1]["job"]["item_revision"]
    _refuse_cross_actor(conn, _claim_body(job_id), NODE_A)
    _refuse_cross_actor(conn, _claim_body(job_id, operation_id="op-claim-foreign"), NODE_A)
    started = fleet_hub_grokbot.handle_grokbot(
        conn,
        _holder_body("start", job_id, revision=2, generation=claimed[1]["lease_generation"]),
        caller_node=WORKER_NODE,
    )
    assert started[0] == 200
    start_replay = fleet_hub_grokbot.handle_grokbot(
        conn,
        _holder_body("start", job_id, revision=2, generation=claimed[1]["lease_generation"]),
        caller_node=WORKER_NODE,
    )
    assert start_replay[0] == 200
    assert start_replay[1]["job"]["item_revision"] == started[1]["job"]["item_revision"]
    _refuse_cross_actor(
        conn,
        _holder_body("start", job_id, revision=2, generation=claimed[1]["lease_generation"]),
        NODE_A,
    )


@pytest.mark.parametrize("action", ["claim", "start", "renew", "complete", "fail", "ack-cancel"])
def test_grokbot_same_queue_peer_cannot_use_stolen_holder_lease(conn, action):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    suffix = {"claim": "1", "start": "2", "renew": "3", "complete": "4", "fail": "5", "ack-cancel": "6"}[action]
    queued = fleet_hub_grokbot.handle_grokbot(
        conn,
        _grokbot_enqueue("grokbot-" + suffix * 24, suffix * 64, operation_id=f"op-enq-{action}"),
        caller_node=FEED_NODE,
    )[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn,
        _claim_body(queued["job_id"], lease_id="lease-hold", operation_id=f"op-claim-{action}"),
        caller_node=WORKER_NODE,
    )
    assert claimed[0] == 200
    revision = claimed[1]["job"]["item_revision"]
    generation = claimed[1]["lease_generation"]
    if action in {"complete", "ack-cancel"}:
        started = fleet_hub_grokbot.handle_grokbot(
            conn,
            _holder_body(
                "start",
                queued["job_id"],
                revision=revision,
                generation=generation,
                lease_id="lease-hold",
                operation_id=f"op-start-{action}",
            ),
            caller_node=WORKER_NODE,
        )
        assert started[0] == 200
        revision = started[1]["job"]["item_revision"]
    if action == "ack-cancel":
        canceled = fleet_hub_grokbot.handle_grokbot(
            conn,
            {
                "action": "cancel",
                "job_id": queued["job_id"],
                "expected_item_revision": revision,
                "operation_id": f"op-cancel-{action}",
            },
            caller_node=OPERATOR_NODE,
        )
        assert canceled[0] == 200
        revision = canceled[1]["job"]["item_revision"]
    if action == "claim":
        stolen = _claim_body(queued["job_id"], lease_id="lease-hold", operation_id=f"op-claim-{action}")
        stolen_new = _claim_body(
            queued["job_id"],
            lease_id="lease-hold",
            revision=revision,
            operation_id="op-claim-stolen",
        )
        _refuse_cross_actor(conn, stolen, OTHER_WORKER)
        _refuse_cross_actor(conn, stolen_new, OTHER_WORKER)
        replay = fleet_hub_grokbot.handle_grokbot(conn, stolen, caller_node=WORKER_NODE)
        assert replay[0] == 200
        assert replay[1]["job"]["claimant_node"] == WORKER_NODE
        return
    stolen = _holder_body(
        action,
        queued["job_id"],
        revision=revision,
        generation=generation,
        lease_id="lease-hold",
        operation_id=f"op-{action}-stolen",
    )
    _refuse_cross_actor(conn, stolen, OTHER_WORKER)
    allowed = fleet_hub_grokbot.handle_grokbot(
        conn,
        _holder_body(
            action,
            queued["job_id"],
            revision=revision,
            generation=generation,
            lease_id="lease-hold",
            operation_id=f"op-{action}-owner",
        ),
        caller_node=WORKER_NODE,
    )
    assert allowed[0] == 200


def test_grokbot_cloud_projection_is_admin_only_on_node_get(tmp_path):
    from brigade import fleet_hub_grokbot

    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            _node, stranger_token = fleet_hub.add_node(db, NODE_B, "stranger")
            _enroll_actors(db)
            queued = fleet_hub_grokbot.handle_grokbot(db, _grokbot_enqueue(), caller_node=FEED_NODE)[1]["job"]
            claimed = fleet_hub_grokbot.handle_grokbot(db, _claim_body(queued["job_id"]), caller_node=WORKER_NODE)
            assert claimed[0] == 200
        finally:
            db.close()
        job_id = queued["job_id"]
        status, node_snapshot = _request(hub, "GET", "/cloud", token=stranger_token)
        assert status == 200
        assert "grokbot" not in node_snapshot
        assert job_id not in json.dumps(node_snapshot)
        assert all(lease.get("provider") != "grok-bot" for lease in node_snapshot.get("leases", []))
        status, admin_snapshot = _request(hub, "GET", "/cloud", token=ADMIN_TOKEN)
        assert status == 200
        assert [job["job_id"] for job in admin_snapshot["grokbot"]["active"]] == [job_id]
        assert "lease-a" not in json.dumps(admin_snapshot)
        assert any(lease.get("provider") == "grok-bot" for lease in admin_snapshot.get("leases", []))


def test_ordinary_node_cannot_squat_grokbot_cloud_namespace(tmp_path):
    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            _node, node_token = fleet_hub.add_node(db, NODE_A, "node-a")
        finally:
            db.close()
        for provider in ("grok-bot", "grokbot-cloud"):
            status, payload = _request(hub, "POST", "/cloud", token=node_token, body=_admit(provider=provider))
            assert status in {403, 409}
            assert payload.get("admitted") is not True
            assert "holder-a" not in json.dumps(payload)


def test_grokbot_noop_expire_is_not_stored_for_replay(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    queued = fleet_hub_grokbot.handle_grokbot(conn, _grokbot_enqueue(), caller_node=FEED_NODE)[1]["job"]
    first = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "expire",
            "job_id": queued["job_id"],
            "expected_item_revision": queued["item_revision"],
            "operation_id": f"expire:{queued['job_id']}:{queued['item_revision']}",
        },
        caller_node=OPERATOR_NODE,
    )
    assert first[0] == 200 and first[1]["expired"] is False
    stored = conn.execute(
        "SELECT result_json FROM grokbot_operations WHERE job_id=? AND operation_id=?",
        (queued["job_id"], f"expire:{queued['job_id']}:{queued['item_revision']}"),
    ).fetchone()
    assert stored is None
    conn.execute(
        "UPDATE grokbot_jobs SET queued_at=? WHERE job_id=?",
        ("2020-01-01T00:00:00Z", queued["job_id"]),
    )
    conn.commit()
    second = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "expire",
            "job_id": queued["job_id"],
            "expected_item_revision": queued["item_revision"],
            "operation_id": f"expire:{queued['job_id']}:{queued['item_revision']}",
        },
        caller_node=OPERATOR_NODE,
    )
    assert second[0] == 200 and second[1]["expired"] is True
    assert second[1]["job"]["state"] == "expired"
    replay = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "expire",
            "job_id": queued["job_id"],
            "expected_item_revision": queued["item_revision"],
            "operation_id": f"expire:{queued['job_id']}:{queued['item_revision']}",
        },
        caller_node=OPERATOR_NODE,
    )
    assert replay[0] == 200 and replay[1]["expired"] is True


def test_stale_sweep_commits_before_conflicting_mutation(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    queued = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue("grokbot-" + "e" * 24, "e" * 64, operation_id="op-enq-stale"), caller_node=FEED_NODE
    )[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn,
        _claim_body(queued["job_id"], lease_id="lease-stale", operation_id="op-claim-stale"),
        caller_node=WORKER_NODE,
    )
    assert claimed[0] == 200
    assert fleet_hub.list_cloud_leases(conn)
    conn.execute(
        "UPDATE grokbot_jobs SET lease_expires_at=? WHERE job_id=?",
        ("2020-01-01T00:00:00Z", queued["job_id"]),
    )
    conn.commit()
    conflict = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "start",
            "job_id": queued["job_id"],
            "lease_id": "lease-stale",
            "expected_item_revision": 99,
            "lease_generation": claimed[1]["lease_generation"],
            "operation_id": "op-start-stale-conflict",
        },
        caller_node=WORKER_NODE,
    )
    assert conflict[0] == 409
    current = fleet_hub_grokbot.handle_grokbot(
        conn, {"action": "status", "job_id": queued["job_id"]}, caller_node=OPERATOR_NODE
    )
    assert current[0] == 200
    assert current[1]["job"]["state"] == "expired"
    assert fleet_hub.list_cloud_leases(conn) == []


def test_grokbot_report_can_complete_from_claimed(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    queued = fleet_hub_grokbot.handle_grokbot(
        conn,
        _grokbot_enqueue(
            "grokbot-" + "8" * 24,
            "8" * 64,
            role="repository-scout",
            artifact_kind="report",
            operation_id="op-enq-claimed-report",
        ),
        caller_node=FEED_NODE,
    )[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn, _claim_body(queued["job_id"], lease_id="lease-cr", operation_id="op-claim-cr"), caller_node=SCOUT_NODE
    )
    completed = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "complete",
            "job_id": queued["job_id"],
            "lease_id": "lease-cr",
            "expected_item_revision": claimed[1]["job"]["item_revision"],
            "lease_generation": claimed[1]["lease_generation"],
            "operation_id": "op-complete-cr",
            "artifact": {
                "kind": "report",
                "digest": "a" * 64,
                "size": 12,
                "private_snapshot_id": queued["job_id"],
            },
        },
        caller_node=SCOUT_NODE,
    )
    assert completed[0] == 200
    assert completed[1]["job"]["state"] == "completed"


def test_grokbot_refuses_unscoped_null_queue_rows(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    job_id = "grokbot-" + "0" * 24
    conn.execute(
        "INSERT INTO grokbot_jobs ("
        "job_id, role, repository, label, task_digest, idempotency_key_hash, state, item_revision, "
        "sequence, created_at, updated_at, queued_at, timeout_seconds, artifact_kind, private_snapshot_id, "
        "owner_node, queue_id"
        ") VALUES (?, 'implementation-worker', 'example/brigade', 'legacy', ?, ?, 'queued', 1, 1, "
        "'2026-08-23T12:00:00Z', '2026-08-23T12:00:00Z', '2026-08-23T12:00:00Z', 900, 'draft-pr', ?, ?, NULL)",
        (job_id, "0" * 64, "1" * 64, job_id, FEED_NODE),
    )
    conn.commit()
    before = conn.execute(
        "SELECT job_id, state, item_revision, queue_id, owner_node FROM grokbot_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    assert before == (job_id, "queued", 1, None, FEED_NODE)

    migration_error = "missing queue scope must be reconciled by an admin"
    with pytest.raises(fleet_hub.FleetHubError, match=migration_error):
        fleet_hub_grokbot.handle_grokbot(conn, {"action": "list"}, caller_node=OPERATOR_NODE)
    with pytest.raises(fleet_hub.FleetHubError, match=migration_error):
        fleet_hub_grokbot.handle_grokbot(
            conn,
            _claim_body(job_id, operation_id="op-claim-unscoped"),
            caller_node=WORKER_NODE,
        )

    after = conn.execute(
        "SELECT job_id, state, item_revision, queue_id, owner_node FROM grokbot_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    assert after == before
    assert conn.execute("SELECT COUNT(*) FROM grokbot_jobs WHERE queue_id IS NOT NULL").fetchone()[0] == 0


def test_grokbot_ack_cancel_emits_external_cancel_acknowledged(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    job_id = "grokbot-" + "7" * 24
    queued = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue(job_id, "7" * 64, operation_id="op-enq-ack"), caller_node=FEED_NODE
    )[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn, _claim_body(job_id, lease_id="lease-ack", operation_id="op-claim-ack"), caller_node=WORKER_NODE
    )
    generation = claimed[1]["lease_generation"]
    started = fleet_hub_grokbot.handle_grokbot(
        conn,
        _holder_body(
            "start",
            job_id,
            revision=claimed[1]["job"]["item_revision"],
            generation=generation,
            lease_id="lease-ack",
            operation_id="op-start-ack",
        ),
        caller_node=WORKER_NODE,
    )
    canceled = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "cancel",
            "job_id": job_id,
            "expected_item_revision": started[1]["job"]["item_revision"],
            "operation_id": "op-cancel-ack",
        },
        caller_node=OPERATOR_NODE,
    )
    assert canceled[0] == 200
    acknowledged = fleet_hub_grokbot.handle_grokbot(
        conn,
        _holder_body(
            "ack-cancel",
            job_id,
            revision=canceled[1]["job"]["item_revision"],
            generation=generation,
            lease_id="lease-ack",
            operation_id="op-ack-cancel",
        ),
        caller_node=WORKER_NODE,
    )
    assert acknowledged[0] == 200
    assert acknowledged[1]["job"]["state"] == "canceled"
    states = [
        row[0]
        for row in conn.execute("SELECT state FROM events WHERE run_id=? ORDER BY sequence", (job_id,)).fetchall()
    ]
    assert states[-1] == "external.cancel-acknowledged"
    assert "external.canceled" not in states
    assert queued["job_id"] == job_id


def test_grokbot_lifecycle_retry_records_one_sanitized_event_per_revision(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    job_id = "grokbot-" + "8" * 24
    secret_task = "implement the secret task and paste the report body"
    enqueue = _grokbot_enqueue(job_id, "8" * 64, operation_id="op-enq-retry")
    enqueue["label"] = secret_task
    queued = fleet_hub_grokbot.handle_grokbot(conn, enqueue, caller_node=FEED_NODE)[1]["job"]
    assert queued["label"] == secret_task
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn, _claim_body(job_id, lease_id="lease-retry", operation_id="op-claim-retry"), caller_node=WORKER_NODE
    )
    assert claimed[0] == 200
    job = fleet_hub_grokbot._require_job(conn, job_id)
    before = conn.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (job_id,)).fetchone()[0]
    fleet_hub_grokbot._record_event(conn, job, "claimed")
    mutated = dict(job)
    mutated["artifact_ref"] = "https://github.com/example/brigade/pull/99"
    mutated["claimant_worker"] = "stolen-worker"
    mutated["label"] = secret_task
    fleet_hub_grokbot._record_event(conn, mutated, "claimed")
    rows = conn.execute("SELECT * FROM events WHERE run_id=?", (job_id,)).fetchall()
    assert len(rows) == before
    dumped = json.dumps(rows)
    assert secret_task not in dumped
    assert "report body" not in dumped
    payload_path = json.dumps(fleet_hub_grokbot._job_payload(mutated))
    assert secret_task in payload_path
    first = fleet_hub.store_events(
        conn,
        {
            "node_id": NODE_A,
            "run_id": "generic-run",
            "sequence": 1,
            "digest": "digest-a",
            "state": "run.started",
            "ts": "2026-08-30T00:00:00+00:00",
        },
    )
    second = fleet_hub.store_events(
        conn,
        {
            "node_id": NODE_A,
            "run_id": "generic-run",
            "sequence": 1,
            "digest": "digest-b",
            "state": "run.started",
            "ts": "2026-08-30T00:00:01+00:00",
        },
    )
    assert first["accepted"] == 1 and second["accepted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE run_id='generic-run'").fetchone()[0] == 2


def test_node_cannot_ingest_reserved_grokbot_harness(conn):
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    poison = {
        "node_id": NODE_A,
        "run_id": "poison-run",
        "sequence": 1,
        "digest": "digest-poison",
        "repo": "example/brigade",
        "seat": "implementation-worker",
        "harness": "grokbot",
        "state": "external.queued",
        "ts": "2026-08-30T00:00:00+00:00",
    }
    with pytest.raises(fleet_hub.FleetHubError, match="grokbot"):
        fleet_hub.store_events(conn, poison, caller_node=NODE_A)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM events WHERE harness='grokbot'").fetchone()[0] == 0


def test_grokbot_event_rejects_item_revision_sequence_mismatch(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    job_id = "grokbot-" + "9" * 24
    queued = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue(job_id, "9" * 64, operation_id="op-enq-mismatch"), caller_node=FEED_NODE
    )[1]["job"]
    job = fleet_hub_grokbot._require_job(conn, job_id)
    mismatched = dict(job)
    mismatched["sequence"] = int(job["sequence"]) + 1
    before = conn.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (job_id,)).fetchone()[0]
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub_grokbot._record_event(conn, mismatched, "queued")
    assert conn.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (job_id,)).fetchone()[0] == before
    assert queued["item_revision"] == queued["sequence"] == 1


def test_grokbot_enqueue_rejects_local_repository_path(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    job_id = "grokbot-" + "a" * 24
    body = _grokbot_enqueue(job_id, "a" * 64, operation_id="op-enq-local")
    body["repository"] = "/tmp/local-brigade"
    before_jobs = conn.execute("SELECT COUNT(*) FROM grokbot_jobs").fetchone()[0]
    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(fleet_hub.FleetHubError, match="repository"):
        fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=FEED_NODE)
    assert conn.execute("SELECT COUNT(*) FROM grokbot_jobs").fetchone()[0] == before_jobs
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
    assert conn.execute("SELECT COUNT(*) FROM grokbot_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (job_id,)).fetchone()[0] == 0


def test_external_expired_is_terminal_and_releases_hub_capacity(conn):
    from brigade import fleet_hub_grokbot

    _enroll_actors(conn)
    job_id = "grokbot-" + "e" * 24
    queued = fleet_hub_grokbot.handle_grokbot(
        conn, _grokbot_enqueue(job_id, "e" * 64, operation_id="op-enq-exp"), caller_node=FEED_NODE
    )[1]["job"]
    claimed = fleet_hub_grokbot.handle_grokbot(
        conn, _claim_body(job_id, lease_id="lease-exp", operation_id="op-claim-exp"), caller_node=WORKER_NODE
    )
    assert claimed[0] == 200
    assert fleet_hub.list_cloud_leases(conn)
    conn.execute(
        "UPDATE grokbot_jobs SET lease_expires_at=?, queued_at=? WHERE job_id=?",
        ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", job_id),
    )
    conn.commit()
    expired = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "expire",
            "job_id": job_id,
            "expected_item_revision": claimed[1]["job"]["item_revision"],
            "operation_id": "op-expire-exp",
        },
        caller_node=OPERATOR_NODE,
    )
    assert expired[0] == 200 and expired[1]["expired"] is True
    assert expired[1]["job"]["state"] == "expired"
    states = [row[0] for row in conn.execute("SELECT state FROM events WHERE run_id=?", (job_id,)).fetchall()]
    assert "external.expired" in states
    assert "external.expired" in fleet_hub.TERMINAL_STATES
    assert deck.is_terminal_state("external.expired") is True
    snapshot = fleet_hub.cloud_snapshot(conn, deck.DeckConfig())
    assert snapshot["grokbot"]["active"] == []
    assert snapshot["grokbot"]["history"][0]["state"] == "expired"
    grok = next(worker for worker in deck.cloud_workers_from_snapshot(snapshot) if worker.provider == "grok-bot")
    assert grok.used == 0
    assert grok.leases == ()
    assert fleet_hub.list_cloud_leases(conn) == []
    live_states = {row["state"] for row in fleet_hub.latest_status(conn, include_all=False) if row["run_id"] == job_id}
    assert "external.expired" not in live_states
    history_states = {
        row["state"] for row in fleet_hub.latest_status(conn, include_all=True) if row["run_id"] == job_id
    }
    assert "external.expired" in history_states
    assert queued["job_id"] == job_id
