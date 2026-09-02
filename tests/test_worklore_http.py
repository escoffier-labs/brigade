# Synthetic rejection fixtures retain private paths to exercise redaction.
# content-guard: allow home-path file
import importlib.util
import json
import sqlite3
import subprocess
from base64 import urlsafe_b64encode
from pathlib import Path

from brigade import fleet_hub
from brigade import fleet_hub_http
from brigade import worklore_github_sync as github_sync
from brigade import worklore_http
from brigade import worklore_http as http
from brigade import worklore_store as store

_CLOUD = importlib.util.spec_from_file_location(
    "worklore_http_fleet_cloud_helpers", Path(__file__).with_name("test_fleet_cloud.py")
)
assert _CLOUD is not None and _CLOUD.loader is not None
_cloud = importlib.util.module_from_spec(_CLOUD)
_CLOUD.loader.exec_module(_cloud)
ADMIN_TOKEN = _cloud.ADMIN_TOKEN
_hub = _cloud._hub
_request = _cloud._request


def _req(
    method,
    path,
    *,
    admin=False,
    node=None,
    operator=False,
    operator_authorization_resolved=None,
    body=None,
    headers=None,
):
    return http.Request(
        method=method,
        path=path,
        is_admin=admin,
        node_id=node,
        is_operator=operator,
        operator_authorization_resolved=(
            operator if operator_authorization_resolved is None else operator_authorization_resolved
        ),
        body=body or {},
        headers=headers or {},
    )


def _http_conn(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "db")
    store.ensure_schema(conn)
    return conn


def test_cookie_caller_cannot_read_or_write(tmp_path):
    conn = _http_conn(tmp_path)
    status, body = http.handle(conn, _req("GET", "/work/items"))
    assert status == 401 and body["code"] == "unauthorized"


def test_node_can_read_and_import_but_cannot_transition(tmp_path):
    conn = _http_conn(tmp_path)
    status, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Native", "kind": "fleet"}),
    )
    assert status == 201
    work_id = created["item"]["work_id"]
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}", node="node-a"))
    assert status == 200 and body["item"]["title"] == "Native"
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            node="node-a",
            body={"to_status": "defining"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 403 and body["code"] == "forbidden"


def test_patch_and_transition_require_if_match(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Edit me", "kind": "admin"}),
    )
    work_id = created["item"]["work_id"]
    status, body = http.handle(
        conn,
        _req("PATCH", f"/work/items/{work_id}", admin=True, body={"priority": "high"}),
    )
    assert status == 400 and body["code"] == "if-match-required"
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "defining"},
        ),
    )
    assert status == 400 and body["code"] == "if-match-required"
    status, ok = http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"priority": "high"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 200 and ok["item"]["version"] == 2
    status, ready = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "defining"},
            headers={"If-Match": "2"},
        ),
    )
    assert status == 200 and ready["item"]["status"] == "defining"
    status, conflict = http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"priority": "low"},
            headers={"If-Match": "2"},
        ),
    )
    assert status == 409 and conflict["code"] == "version-conflict"


def test_invalid_transition_and_missing_item_use_stable_codes(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "X", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "running"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 400 and body["code"] == "invalid-transition"
    status, body = http.handle(conn, _req("GET", "/work/items/wl-missing", admin=True))
    assert status == 404 and body["code"] == "not-found"


def test_attempts_body_is_action_and_optional_run_id(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Try", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/attempts",
            node="node-a",
            body={"action": "failed"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 403 and body["code"] == "attempt-forbidden"


def test_attempt_run_id_uses_canonical_safe_text_validation(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Try", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]
    cases = [
        ("x" * (store.ATTEMPT_RUN_ID_MAX + 1), "field-bound"),
        ("run\x1b[31m", "field-bound"),
        ("/home/example/run", "private-data"),
        (1, "field-bound"),
    ]
    for run_id, code in cases:
        status, body = http.handle(
            conn,
            _req(
                "POST",
                f"/work/items/{work_id}/attempts",
                admin=True,
                body={"action": "started", "run_id": run_id},
                headers={"If-Match": "1"},
            ),
        )
        assert status == 400 and body["code"] == code


def test_attempt_reset_discards_a_run_id_over_http(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Try", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]

    status, _ = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/attempts",
            admin=True,
            body={"action": "reset", "run_id": "run-do-not-persist"},
            headers={"If-Match": "1"},
        ),
    )

    assert status == 200 and store.list_events(conn, work_id)[-1]["run_id"] is None


def test_authenticated_node_attempt_replay_is_idempotent_over_http(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Try", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    status, _ = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/execution",
            node="node-a",
            body={"node_id": "node-a", "run_id": "run-1"},
        ),
    )
    assert status == 201
    request = _req(
        "POST",
        f"/work/items/{work_id}/attempts",
        node="node-a",
        body={"action": "failed", "run_id": "run-1"},
        headers={"If-Match": "1"},
    )

    first_status, first = http.handle(conn, request)
    replay_status, replay = http.handle(conn, request)

    assert (first_status, replay_status) == (200, 200)
    assert replay["item"] == first["item"]
    assert [event["event_type"] for event in store.list_events(conn, work_id)].count("attempt-failed") == 1


def test_worklore_enabled_is_fail_closed_and_reads_fleet_config(tmp_path):
    config = tmp_path / "fleet.toml"
    assert worklore_http.enabled(environ={}, config_path=config) is False
    config.write_text("[fleet.worklore]\nenabled = true\n")
    assert worklore_http.enabled(environ={}, config_path=config) is True
    assert worklore_http.enabled(environ={"BRIGADE_WORKLORE_ENABLED": "0"}, config_path=config) is False
    assert worklore_http.enabled(environ={"BRIGADE_WORKLORE_ENABLED": "1"}, config_path=config) is True


def test_worklore_enabled_uses_brigade_home_config(monkeypatch, tmp_path):
    home = tmp_path / "brigade-home"
    home.mkdir()
    (home / "fleet.toml").write_text("[fleet.worklore]\nenabled = true\n")
    monkeypatch.setenv("BRIGADE_HOME", str(home))
    assert worklore_http.enabled(environ={}) is True


def test_worklore_list_query_validation_and_pagination(tmp_path):
    conn = _http_conn(tmp_path)
    for index in range(3):
        http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": f"Item {index}", "kind": "fleet"}))
    status, body = http.handle(conn, _req("GET", "/work/items?kind=fleet&burn_eligible=false&limit=2", admin=True))
    assert status == 200 and len(body["items"]) == 2 and body["next_cursor"]
    status, body = http.handle(conn, _req("GET", "/work/items?unknown=x", admin=True))
    assert status == 400 and body["code"] == "unknown-field"
    status, body = http.handle(conn, _req("GET", "/work/events?limit=0", admin=True))
    assert status == 400 and body["code"] == "field-bound"
    status, body = http.handle(conn, _req("GET", "/work/events?unknown=x", admin=True))
    assert status == 400 and body["code"] == "unknown-field"
    work_id = store.list_items(conn, kind="fleet")["items"][0]["work_id"]
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}/events?limit=1", admin=True))
    assert status == 200 and len(body["events"]) == 1
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}/events?status=ready", admin=True))
    assert status == 400 and body["code"] == "unknown-field"


def test_worklore_admin_link_routes_call_store_directly(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Links", "kind": "admin"}),
    )
    work_id = created["item"]["work_id"]
    status, added = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/links",
            admin=True,
            body={"link_type": "url", "external_key": "https://example.invalid/runbook"},
        ),
    )
    assert status == 201
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/links",
            admin=True,
            body={"link_type": "fleet-run", "external_key": "node/run"},
        ),
    )
    assert status == 403 and body["code"] == "link-forbidden"
    status, body = http.handle(
        conn,
        _req("DELETE", f"/work/items/{work_id}/links/{added['link']['link_id']}", admin=True),
    )
    assert status == 200 and body == {"ok": True}


def test_link_delete_honors_if_match_and_keeps_the_link_on_conflict(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": "Links", "kind": "admin"}))
    work_id = created["item"]["work_id"]
    _, added = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/links",
            admin=True,
            body={"link_type": "url", "external_key": "https://example.invalid/runbook"},
        ),
    )
    http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"priority": "high"},
            headers={"If-Match": "1"},
        ),
    )

    status, body = http.handle(
        conn,
        _req(
            "DELETE",
            f"/work/items/{work_id}/links/{added['link']['link_id']}",
            admin=True,
            headers={"If-Match": "1"},
        ),
    )

    assert (status, body["code"]) == (409, "version-conflict")
    assert store.list_links(conn, work_id)[0]["link_id"] == added["link"]["link_id"]


def test_worklore_routes_are_absent_when_disabled(tmp_path):
    with _hub(tmp_path) as hub:
        status, body = _request(hub, "GET", "/work/items", token=ADMIN_TOKEN)
        assert status == 404
        assert body["error"] == "not found"


def test_worklore_native_create_to_burn_on_enabled_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as hub:
        status, created = _request(
            hub, "POST", "/work/items", token=ADMIN_TOKEN, body={"title": "NAS restic", "kind": "fleet"}
        )
        assert status == 201
        work_id = created["item"]["work_id"]
        _request(
            hub,
            "PATCH",
            f"/work/items/{work_id}",
            token=ADMIN_TOKEN,
            body={
                "acceptance": ["restic snapshots --latest 1 succeeds"],
                "burn_eligible": True,
                "execution_mode": "agent",
            },
            extra_headers={"If-Match": "1"},
        )
        _request(
            hub,
            "POST",
            f"/work/items/{work_id}/transitions",
            token=ADMIN_TOKEN,
            body={"to_status": "ready"},
            extra_headers={"If-Match": "2"},
        )
        status, queue = _request(hub, "GET", "/work/queue/burn", token=ADMIN_TOKEN)
        assert status == 200
        assert queue["items"][0]["work_id"] == work_id


def test_admin_worklore_patch_succeeds_without_allow_admin_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as hub:
        _, created = _request(
            hub, "POST", "/work/items", token=ADMIN_TOKEN, body={"title": "Admin write", "kind": "admin"}
        )
        status, patched = _request(
            hub,
            "PATCH",
            f"/work/items/{created['item']['work_id']}",
            token=ADMIN_TOKEN,
            body={"priority": "high"},
            extra_headers={"If-Match": "1"},
        )
        assert status == 200
        assert patched["item"]["priority"] == "high"


def test_existing_claim_and_dashboard_routes_stay_unchanged(tmp_path):
    with _hub(tmp_path) as hub:
        assert _request(hub, "GET", "/claims", token=ADMIN_TOKEN)[0] == 200
        assert _request(hub, "GET", "/health")[0] == 200


def test_operator_nodes_reads_toml_and_bounded_env(tmp_path):
    config = tmp_path / "fleet.toml"
    assert worklore_http.operator_nodes(environ={}, config_path=config) == ()
    config.write_text('[fleet.worklore]\noperator_nodes = ["alpha", "unknown", "bad id", "alpha", "beta"]\n')
    assert worklore_http.operator_nodes(environ={}, config_path=config) == ("alpha", "beta")
    config.write_text('[fleet.worklore]\noperator_nodes = "alpha"\n')
    assert worklore_http.operator_nodes(environ={}, config_path=config) == ()
    env = {"BRIGADE_WORKLORE_OPERATOR_NODES": "gamma, unknown, n1"}
    config.write_text('[fleet.worklore]\noperator_nodes = ["alpha"]\n')
    assert worklore_http.operator_nodes(environ=env, config_path=config) == ("gamma", "n1")
    assert worklore_http.operator_nodes(environ={"BRIGADE_WORKLORE_OPERATOR_NODES": ""}, config_path=config) == ()
    too_long = "a" * 129
    env = {"BRIGADE_WORKLORE_OPERATOR_NODES": f"alpha,{too_long},beta"}
    assert worklore_http.operator_nodes(environ=env, config_path=config) == ("alpha", "beta")
    many = ",".join(f"n{index}" for index in range(40))
    nodes = worklore_http.operator_nodes(environ={"BRIGADE_WORKLORE_OPERATOR_NODES": many}, config_path=config)
    assert len(nodes) == 32
    assert nodes[0] == "n0" and nodes[-1] == "n31"


def test_operator_authorization_is_snapshotted_once_per_request(tmp_path, monkeypatch):
    conn = _http_conn(tmp_path)
    decisions = []

    def rotating_operator_nodes():
        decisions.append("read")
        return ("ops-node",) if len(decisions) == 1 else ()

    monkeypatch.setattr(http, "operator_nodes", rotating_operator_nodes)
    status, created = http.handle(
        conn,
        _req("POST", "/work/items", node="ops-node", body={"title": "Native", "kind": "fleet"}),
    )

    assert status == 201
    assert decisions == ["read"]
    assert store.list_events(conn, created["item"]["work_id"])[0]["actor_type"] == "operator"


def test_direct_request_can_supply_a_resolved_non_operator_snapshot(tmp_path, monkeypatch):
    conn = _http_conn(tmp_path)
    decisions = []
    monkeypatch.setattr(http, "operator_nodes", lambda: decisions.append("read") or ("ops-node",))

    status, body = http.handle(
        conn,
        _req(
            "POST",
            "/work/items",
            node="node-a",
            operator_authorization_resolved=True,
            body={"title": "No config read", "kind": "fleet"},
        ),
    )

    assert (status, body["code"]) == (403, "forbidden")
    assert decisions == []


def test_fleet_hub_reads_operator_configuration_once_for_a_non_operator_node(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    decisions = []
    monkeypatch.setattr(worklore_http, "operator_nodes", lambda: decisions.append("read") or ())

    with _hub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub[2])
        try:
            _node, node_token = fleet_hub.add_node(conn, "node-a", "node-a")
        finally:
            conn.close()
        status, _body = _request(hub, "GET", "/work/items", token=node_token)

    assert status == 200
    assert decisions == ["read"]


def test_operator_node_can_create_patch_transition_reset_and_link(tmp_path):
    conn = _http_conn(tmp_path)
    status, created = http.handle(
        conn,
        _req(
            "POST",
            "/work/items",
            node="ops-node",
            operator=True,
            body={"title": "Native", "kind": "fleet"},
            headers={"Idempotency-Key": "create-1"},
        ),
    )
    assert status == 201
    work_id = created["item"]["work_id"]
    events = store.list_events(conn, work_id)
    assert [event["event_type"] for event in events] == ["created"]
    assert events[0]["actor_type"] == "operator"
    assert events[0]["actor_id"] == "ops-node"
    assert events[0]["node_id"] == "ops-node"
    status, patched = http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            node="ops-node",
            operator=True,
            body={"priority": "high"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 200 and patched["item"]["priority"] == "high"
    assert store.list_events(conn, work_id)[-1]["actor_type"] == "operator"
    status, moved = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            node="ops-node",
            operator=True,
            body={"to_status": "defining"},
            headers={"If-Match": "2"},
        ),
    )
    assert status == 200 and moved["item"]["status"] == "defining"
    status, reset = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/attempts",
            node="ops-node",
            operator=True,
            body={"action": "reset"},
            headers={"If-Match": "3"},
        ),
    )
    assert status == 200 and reset["item"]["attempt_count"] == 0
    reset_event = store.list_events(conn, work_id)[-1]
    assert reset_event["event_type"] == "attempt-reset"
    assert reset_event["actor_type"] == "operator"
    assert reset_event["node_id"] == "ops-node"
    status, added = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/links",
            node="ops-node",
            operator=True,
            body={"link_type": "url", "external_key": "https://example.invalid/runbook"},
        ),
    )
    assert status == 201
    status, body = http.handle(
        conn,
        _req(
            "DELETE",
            f"/work/items/{work_id}/links/{added['link']['link_id']}",
            node="ops-node",
            operator=True,
        ),
    )
    assert status == 200 and body == {"ok": True}


def test_ordinary_node_keeps_read_but_cannot_mutate_or_import(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Native", "kind": "fleet"}),
    )
    work_id = created["item"]["work_id"]
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}", node="node-a"))
    assert status == 200
    status, body = http.handle(
        conn,
        _req("POST", "/work/items", node="node-a", body={"title": "Nope", "kind": "fleet"}),
    )
    assert status == 403 and body["code"] == "forbidden"
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/attempts",
            node="node-a",
            body={"action": "reset"},
            headers={"If-Match": "1"},
        ),
    )
    assert status == 403 and body["code"] == "forbidden"
    import_body = {
        "adapter_id": "github:escoffier-labs",
        "source_type": "github",
        "idempotency_key": "imp-1",
        "observations": [
            {
                "external_key": "escoffier-labs/brigade#1",
                "link_type": "github",
                "title": "Imported",
                "source_policy": "eligible",
                "url": "https://github.com/escoffier-labs/brigade/issues/1",
                "external_updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    status, body = http.handle(conn, _req("POST", "/work/imports", node="node-a", body=import_body))
    assert status == 403 and body["code"] == "forbidden"
    assert conn.execute("SELECT COUNT(*) FROM work_sync_cursors").fetchone()[0] == 0
    status, imported = http.handle(
        conn,
        _req("POST", "/work/imports", node="ops-node", operator=True, body=import_body),
    )
    assert status == 200 and imported["created"] == 1


def test_native_create_idempotency_replays_and_conflicts(tmp_path):
    conn = _http_conn(tmp_path)
    body = {"title": "Retry me", "kind": "fleet"}
    first_status, first = http.handle(
        conn,
        _req(
            "POST",
            "/work/items",
            node="ops-node",
            operator=True,
            body=body,
            headers={"Idempotency-Key": "native-1"},
        ),
    )
    assert first_status == 201
    work_id = first["item"]["work_id"]
    second_status, second = http.handle(
        conn,
        _req(
            "POST",
            "/work/items",
            node="ops-node",
            operator=True,
            body=body,
            headers={"Idempotency-Key": "native-1"},
        ),
    )
    assert second_status == 201
    assert second["item"]["work_id"] == work_id
    assert [event["event_type"] for event in store.list_events(conn, work_id)] == ["created"]
    conflict_status, conflict = http.handle(
        conn,
        _req(
            "POST",
            "/work/items",
            node="ops-node",
            operator=True,
            body={"title": "Different", "kind": "fleet"},
            headers={"Idempotency-Key": "native-1"},
        ),
    )
    assert conflict_status == 409 and conflict["code"] == "import-conflict"
    other_status, other = http.handle(
        conn,
        _req(
            "POST",
            "/work/items",
            admin=True,
            body=body,
            headers={"Idempotency-Key": "native-1"},
        ),
    )
    assert other_status == 201
    assert other["item"]["work_id"] != work_id


def test_hub_marks_configured_operator_node_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    monkeypatch.setenv("BRIGADE_WORKLORE_OPERATOR_NODES", "ops-node")
    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            _, operator_token = fleet_hub.add_node(db, "ops-node")
            _, worker_token = fleet_hub.add_node(db, "worker-node")
        finally:
            db.close()
        status, created = _request(
            hub,
            "POST",
            "/work/items",
            token=operator_token,
            body={"title": "From node", "kind": "fleet"},
            extra_headers={"Idempotency-Key": "hub-create-1"},
        )
        assert status == 201
        work_id = created["item"]["work_id"]
        status, shown = _request(hub, "GET", f"/work/items/{work_id}", token=operator_token)
        assert status == 200
        created_event = shown["recent_events"][0]
        assert created_event["actor_type"] == "operator"
        assert created_event["actor_id"] == "ops-node"
        assert created_event["node_id"] == "ops-node"
        status, refused = _request(
            hub,
            "POST",
            "/work/items",
            token=worker_token,
            body={"title": "From worker", "kind": "fleet"},
        )
        assert status == 403 and refused["code"] == "forbidden"
        status, listed = _request(hub, "GET", "/work/items", token=worker_token)
        assert status == 200 and listed["items"][0]["work_id"] == work_id
        status, admin_created = _request(
            hub, "POST", "/work/items", token=ADMIN_TOKEN, body={"title": "Admin still works", "kind": "admin"}
        )
        assert status == 201


def test_worklore_open_db_error_does_not_leak_sqlite_text(tmp_path, monkeypatch):
    marker = "SECRET_MARKER_/tmp/fake-worklore.db"

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError(f"unable to open database file: {marker}")

    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    monkeypatch.setattr(fleet_hub_http, "open_db", _boom)
    with _hub(tmp_path) as hub:
        status, body = _request(hub, "GET", "/work/items", token=ADMIN_TOKEN)
    assert status == 500
    assert body["error"] == "hub database error"
    assert marker not in str(body)


def test_sqlite_failure_maps_to_a_stable_internal_error(tmp_path, monkeypatch):
    conn = _http_conn(tmp_path)

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("no such column: /tmp/secret-worklore.db")

    monkeypatch.setattr(store, "list_items", _boom)
    status, body = http.handle(conn, _req("GET", "/work/items", admin=True))
    assert status == 500
    assert body == {"error": "internal error", "code": "internal-error"}
    assert "secret-worklore" not in str(body)
    assert http._STATUS_BY_CODE["internal-error"] == 500


def test_item_detail_uses_the_documented_recent_events_key(tmp_path):
    conn = _http_conn(tmp_path)
    status, created = http.handle(
        conn, _req("POST", "/work/items", admin=True, body={"title": "Detail", "kind": "fleet"})
    )
    assert status == 201
    work_id = created["item"]["work_id"]
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}", admin=True))
    assert status == 200
    assert set(body) == {"item", "links", "links_next_cursor", "recent_events"}
    assert [event["event_type"] for event in body["recent_events"]] == ["created"]
    assert body["links"] == [] and body["links_next_cursor"] is None


def test_execution_link_returns_201_and_passes_admin_authority(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": "Run", "kind": "fleet"}))
    work_id = created["item"]["work_id"]
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    status, body = http.handle(
        conn,
        _req("POST", f"/work/items/{work_id}/execution", admin=True, body={"node_id": "node-a", "run_id": "run-1"}),
    )
    assert status == 201 and body["link"]["link_type"] == "fleet-run"
    assert store.list_events(conn, work_id)[-1]["actor_type"] == "operator"


def test_node_cannot_link_another_nodes_run_through_the_route(tmp_path):
    conn = _http_conn(tmp_path)
    _, created = http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": "Run", "kind": "fleet"}))
    work_id = created["item"]["work_id"]
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    status, body = http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/execution",
            node="node-b",
            body={"node_id": "node-a", "run_id": "run-1"},
        ),
    )
    assert status == 403 and body["code"] == "execution-mismatch"


def test_operator_can_unlink_a_retired_source_link_but_not_an_eligible_one(tmp_path):
    conn = _http_conn(tmp_path)
    observation = {
        "external_key": "escoffier-labs/brigade#31",
        "link_type": "github",
        "title": "Retire me",
        "source_policy": "eligible",
        "external_updated_at": "2026-01-01T00:00:00+00:00",
    }
    status, imported = http.handle(
        conn,
        _req(
            "POST",
            "/work/imports",
            node="ops-node",
            operator=True,
            body={
                "adapter_id": "github:escoffier-labs",
                "source_type": "github",
                "idempotency_key": "http-1",
                "observations": [observation],
            },
        ),
    )
    assert status == 200
    work_id = imported["items"][0]["work_id"]
    link_id = store.list_links(conn, work_id)[0]["link_id"]
    status, refused = http.handle(conn, _req("DELETE", f"/work/items/{work_id}/links/{link_id}", admin=True))
    assert status == 403 and refused["code"] == "link-forbidden"
    http.handle(
        conn,
        _req(
            "POST",
            "/work/imports",
            node="ops-node",
            operator=True,
            body={
                "adapter_id": "github:escoffier-labs",
                "source_type": "github",
                "idempotency_key": "http-2",
                "observations": [
                    {
                        **observation,
                        "source_policy": "closed",
                        "external_updated_at": "2026-01-02T00:00:00+00:00",
                    }
                ],
            },
        ),
    )
    status, body = http.handle(
        conn, _req("DELETE", f"/work/items/{work_id}/links/{link_id}", node="ops-node", operator=True)
    )
    assert status == 200 and body == {"ok": True}
    assert store.list_links(conn, work_id) == []
    assert store.list_items(conn, source="native")["items"][0]["work_id"] == work_id


# --- numeric field bounds (Daybreak finding 6) -------------------------------

_WIDE = "9" * 5000


def _seeded_item(conn) -> str:
    status, created = http.handle(
        conn,
        _req("POST", "/work/items", admin=True, body={"title": "Native", "kind": "fleet"}),
    )
    assert status == 201
    return created["item"]["work_id"]


def _cursor(kind: str, values: dict[str, str]) -> str:
    payload = json.dumps({"kind": kind, "values": values}, sort_keys=True, separators=(",", ":"))
    return urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def test_oversized_limit_is_a_field_bound_refusal(tmp_path):
    conn = _http_conn(tmp_path)
    work_id = _seeded_item(conn)
    for path in ("/work/items", "/work/events", f"/work/items/{work_id}/events"):
        status, body = http.handle(conn, _req("GET", f"{path}?limit={_WIDE}", admin=True))
        assert (status, body["code"]) == (400, "field-bound"), path
        assert "1 to 100" in body["error"]


def test_ordinary_limits_still_reach_the_store(tmp_path):
    conn = _http_conn(tmp_path)
    _seeded_item(conn)
    status, body = http.handle(conn, _req("GET", "/work/items?limit=1", admin=True))
    assert status == 200 and len(body["items"]) == 1
    status, body = http.handle(conn, _req("GET", "/work/items?limit=0", admin=True))
    assert (status, body["code"]) == (400, "field-bound")
    status, body = http.handle(conn, _req("GET", "/work/items?limit=abc", admin=True))
    assert (status, body["code"]) == (400, "field-bound")


def test_oversized_cursor_is_refused_before_it_is_decoded(tmp_path):
    conn = _http_conn(tmp_path)
    work_id = _seeded_item(conn)
    cases = [
        ("/work/items", _cursor("items", {"created_at": "2026-01-01T00:00:00Z", "work_id": "wl-" + _WIDE})),
        ("/work/events", _cursor("events", {"occurred_at": "2026-01-01T00:00:00Z", "seq": _WIDE})),
        (f"/work/items/{work_id}/events", _cursor("item-events", {"seq": _WIDE})),
    ]
    for path, cursor in cases:
        status, body = http.handle(conn, _req("GET", f"{path}?cursor={cursor}", admin=True))
        assert (status, body["code"]) == (400, "field-bound"), path
        assert body["error"] == "cursor is invalid"


def test_a_cursor_sequence_wider_than_sqlite_is_a_field_bound_refusal(tmp_path):
    """A short cursor can still carry an integer SQLite cannot bind; the overflow
    becomes the ordinary refusal instead of a traceback."""
    conn = _http_conn(tmp_path)
    work_id = _seeded_item(conn)
    cases = [
        ("/work/events", _cursor("events", {"occurred_at": "2026-01-01T00:00:00Z", "seq": "9" * 40})),
        (f"/work/items/{work_id}/events", _cursor("item-events", {"seq": "9" * 40})),
    ]
    for path, cursor in cases:
        status, body = http.handle(conn, _req("GET", f"{path}?cursor={cursor}", admin=True))
        assert (status, body["code"]) == (400, "field-bound"), path
        assert body["error"] == "request field is out of bounds"


def test_paging_cursors_still_round_trip(tmp_path):
    conn = _http_conn(tmp_path)
    _seeded_item(conn)
    _seeded_item(conn)
    status, first = http.handle(conn, _req("GET", "/work/items?limit=1", admin=True))
    assert status == 200 and first["next_cursor"]
    status, second = http.handle(conn, _req("GET", f"/work/items?limit=1&cursor={first['next_cursor']}", admin=True))
    assert status == 200 and len(second["items"]) == 1
    assert second["items"][0]["work_id"] != first["items"][0]["work_id"]


def test_oversized_if_match_is_refused_without_a_traceback(tmp_path):
    conn = _http_conn(tmp_path)
    work_id = _seeded_item(conn)
    routes = [
        ("PATCH", f"/work/items/{work_id}", {"priority": "high"}),
        ("POST", f"/work/items/{work_id}/transitions", {"to_status": "ready"}),
        ("POST", f"/work/items/{work_id}/attempts", {"action": "start"}),
    ]
    for method, path, body in routes:
        status, payload = http.handle(conn, _req(method, path, admin=True, body=body, headers={"If-Match": _WIDE}))
        assert (status, payload["code"]) == (400, "if-match-required"), path


def test_ordinary_if_match_values_still_work(tmp_path):
    conn = _http_conn(tmp_path)
    work_id = _seeded_item(conn)
    status, body = http.handle(
        conn,
        _req("PATCH", f"/work/items/{work_id}", admin=True, body={"priority": "high"}, headers={"If-Match": "1"}),
    )
    assert status == 200 and body["item"]["priority"] == "high"
    status, body = http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"priority": "low"},
            headers={"If-Match": "9999999999"},
        ),
    )
    assert (status, body["code"]) == (409, "version-conflict")


# --- import authz and private data (Daybreak findings 2 and 3) ---------------


def _import_body(**overrides):
    body = {
        "adapter_id": "github:escoffier-labs",
        "source_type": "github",
        "idempotency_key": "imp-x",
        "observations": [
            {
                "external_key": "escoffier-labs/brigade#41",
                "link_type": "github",
                "title": "Imported",
                "source_policy": "eligible",
                "url": "https://github.com/escoffier-labs/brigade/issues/41",
                # The hub refuses to project an observation whose source reports no
                # revision, so the ordinary import body carries one.
                "external_updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    body.update(overrides)
    return body


def test_direct_github_404_through_hub_marks_the_link_stale_and_excludes_it_from_burn(tmp_path, monkeypatch):
    conn = _http_conn(tmp_path)
    status, imported = http.handle(conn, _req("POST", "/work/imports", admin=True, body=_import_body()))
    assert status == 200 and imported["created"] == 1
    work_id = imported["items"][0]["work_id"]
    http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"acceptance": ["done"], "burn_eligible": True, "execution_mode": "agent"},
            headers={"If-Match": "1"},
        ),
    )
    http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "ready"},
            headers={"If-Match": "2"},
        ),
    )

    def fake_run(argv, **kwargs):
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 404")

    monkeypatch.setattr(github_sync.shutil, "which", lambda _name: None)
    monkeypatch.setattr(github_sync.subprocess, "run", fake_run)
    tombstone = github_sync.discover_and_reconcile(
        [{"name": "escoffier-labs", "visibility": "public"}],
        label="burn-queue",
        state="all",
        existing_items=store.list_items(conn, source="github")["items"],
    )
    status, reconciled = http.handle(
        conn,
        _req(
            "POST",
            "/work/imports",
            admin=True,
            body=_import_body(idempotency_key="direct-404", observations=tombstone),
        ),
    )
    assert status == 200 and (reconciled["updated"], reconciled["refused"]) == (1, 0)
    assert store.list_links(conn, work_id)[0]["stale_at"] is not None
    queue = store.burn_queue(conn)
    assert queue["items"] == []
    assert queue["exclusions"]["source-policy"] == 1


def test_imports_need_a_configured_operator_node(tmp_path):
    conn = _http_conn(tmp_path)
    status, body = http.handle(conn, _req("POST", "/work/imports", node="node-a", body=_import_body()))
    assert status == 403 and body["code"] == "forbidden"
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    status, admin_import = http.handle(conn, _req("POST", "/work/imports", admin=True, body=_import_body()))
    assert status == 200 and admin_import["created"] == 1


def test_an_ordinary_node_cannot_squat_an_adapter_namespace_over_http(tmp_path):
    conn = _http_conn(tmp_path)
    status, body = http.handle(
        conn,
        _req("POST", "/work/imports", node="node-squatter", body=_import_body(observations=[])),
    )
    assert status == 403 and body["code"] == "forbidden"
    status, empty = http.handle(
        conn,
        _req(
            "POST",
            "/work/imports",
            node="ops-squatter",
            operator=True,
            body=_import_body(observations=[], idempotency_key="empty-1"),
        ),
    )
    assert status == 200 and empty == {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "refused": 0,
        "refused_details": [],
        "items": [],
    }
    assert conn.execute("SELECT COUNT(*) FROM work_sync_cursors").fetchone()[0] == 0
    status, imported = http.handle(
        conn,
        _req("POST", "/work/imports", node="ops-node", operator=True, body=_import_body()),
    )
    assert status == 200 and imported["created"] == 1
    assert conn.execute("SELECT owner_node FROM work_sync_cursors").fetchone()[0] == "ops-node"


def test_adapter_prefix_and_private_data_are_bad_requests(tmp_path):
    conn = _http_conn(tmp_path)
    status, body = http.handle(
        conn,
        _req(
            "POST",
            "/work/imports",
            node="ops-node",
            operator=True,
            body=_import_body(adapter_id="brigade:escoffier-labs"),
        ),
    )
    assert status == 400 and body["code"] == "field-bound"
    leaky = dict(_import_body()["observations"][0], title="see /home/operator/notes")
    status, body = http.handle(
        conn,
        _req(
            "POST",
            "/work/imports",
            node="ops-node",
            operator=True,
            body=_import_body(observations=[leaky]),
        ),
    )
    assert status == 400 and body["code"] == "private-data"
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


def _ready(conn, title):
    created = http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": title, "kind": "fleet"}))[1]
    work_id = created["item"]["work_id"]
    http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"acceptance": ["done"], "burn_eligible": True, "execution_mode": "agent"},
            headers={"If-Match": "1"},
        ),
    )
    http.handle(
        conn,
        _req(
            "POST",
            f"/work/items/{work_id}/transitions",
            admin=True,
            body={"to_status": "ready"},
            headers={"If-Match": "2"},
        ),
    )
    return work_id


def test_burn_queue_is_paged_and_rejects_unknown_query_fields(tmp_path):
    conn = _http_conn(tmp_path)
    ready = [_ready(conn, f"Ready {index}") for index in range(3)]
    status, first = http.handle(conn, _req("GET", "/work/queue/burn?limit=2", admin=True))
    assert status == 200
    assert set(first) == {"items", "exclusions", "next_cursor"}
    assert [item["work_id"] for item in first["items"]] == ready[:2]
    assert first["next_cursor"]
    cursor = urlsafe_b64encode(b"x").decode("ascii")
    status, last = http.handle(conn, _req("GET", f"/work/queue/burn?limit=2&cursor={first['next_cursor']}", admin=True))
    assert status == 200
    assert [item["work_id"] for item in last["items"]] == ready[2:]
    assert last["next_cursor"] is None
    status, body = http.handle(conn, _req("GET", "/work/queue/burn?state=ready", admin=True))
    assert status == 400 and body["code"] == "unknown-field"
    status, body = http.handle(conn, _req("GET", "/work/queue/burn?limit=0", admin=True))
    assert status == 400 and body["code"] == "field-bound"
    status, body = http.handle(conn, _req("GET", f"/work/queue/burn?cursor={cursor}", admin=True))
    assert status == 400 and body["code"] == "field-bound"


def test_burn_queue_default_page_is_bounded(tmp_path):
    conn = _http_conn(tmp_path)
    for index in range(store.BURN_PAGE_DEFAULT + 2):
        _ready(conn, f"Ready {index:03d}")
    status, body = http.handle(conn, _req("GET", "/work/queue/burn", admin=True))
    assert status == 200
    assert len(body["items"]) == store.BURN_PAGE_DEFAULT
    assert body["next_cursor"]


def test_item_detail_reads_only_the_recent_events_it_returns(tmp_path):
    conn = _http_conn(tmp_path)
    created = http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": "Chatty", "kind": "fleet"}))[1]
    work_id = created["item"]["work_id"]
    for version in range(1, 25):
        http.handle(
            conn,
            _req(
                "PATCH",
                f"/work/items/{work_id}",
                admin=True,
                body={"scope": f"scope-{version}"},
                headers={"If-Match": str(version)},
            ),
        )
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}", admin=True))
    assert status == 200
    assert len(body["recent_events"]) == store.DETAIL_RECENT_EVENTS
    assert body["recent_events"] == store.list_events(conn, work_id)[-store.DETAIL_RECENT_EVENTS :]


def test_native_create_and_patch_reject_private_data_over_http(tmp_path):
    conn = _http_conn(tmp_path)
    status, body = http.handle(
        conn,
        _req(
            "POST",
            "/work/items",
            admin=True,
            body={"title": "Leaky", "kind": "fleet", "description": "see /home/operator/notes"},
        ),
    )
    assert status == 400 and body["code"] == "private-data"
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
    created = http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": "Clean", "kind": "fleet"}))[1]
    work_id = created["item"]["work_id"]
    status, body = http.handle(
        conn,
        _req(
            "PATCH",
            f"/work/items/{work_id}",
            admin=True,
            body={"acceptance": ["paste " + "ghp_" + "a" * 36]},
            headers={"if-match": "1"},
        ),
    )
    assert status == 400 and body["code"] == "private-data"
    assert http.handle(conn, _req("GET", f"/work/items/{work_id}", admin=True))[1]["item"]["version"] == 1


def test_item_detail_pages_links_and_rejects_unknown_query_keys(tmp_path):
    conn = _http_conn(tmp_path)
    created = http.handle(conn, _req("POST", "/work/items", admin=True, body={"title": "Linked", "kind": "fleet"}))[1]
    work_id = created["item"]["work_id"]
    for number in range(3):
        status, _ = http.handle(
            conn,
            _req(
                "POST",
                f"/work/items/{work_id}/links",
                admin=True,
                body={"link_type": "url", "external_key": f"https://example.com/{number}"},
            ),
        )
        assert status == 201
    seen = []
    cursor = None
    for _ in range(4):
        suffix = "?links_limit=2" + (f"&links_cursor={cursor}" if cursor else "")
        status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}{suffix}", admin=True))
        assert status == 200 and len(body["links"]) <= 2
        seen.extend(link["link_id"] for link in body["links"])
        cursor = body["links_next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert seen == [link["link_id"] for link in store.list_links(conn, work_id)]
    status, body = http.handle(conn, _req("GET", f"/work/items/{work_id}?limit=2", admin=True))
    assert status == 400 and body["code"] == "unknown-field"


def test_a_node_may_not_present_the_reserved_admin_node_id(tmp_path):
    conn = _http_conn(tmp_path)
    for request in (
        _req("GET", "/work/items", node="admin"),
        _req("GET", "/work/items", node="admin", operator=True),
        _req(
            "POST",
            "/work/imports",
            node="admin",
            operator=True,
            body={"adapter_id": "github:x", "source_type": "github", "idempotency_key": "k", "observations": []},
        ),
    ):
        status, body = http.handle(conn, request)
        assert (status, body["code"]) == (403, "forbidden")


def test_the_admin_node_id_is_never_a_configured_operator_node(monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_OPERATOR_NODES", "admin,node-a")
    assert worklore_http.operator_nodes() == ("node-a",)
