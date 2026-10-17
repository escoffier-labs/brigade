"""Hard ingestion and read bounds at the authenticated Hub boundary."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from brigade import fleet_client, fleet_hub, fleet_hub_http
from brigade.fleet_hub_claim_cardinality import ClaimCardinalityError, MAX_ACTIVE_CLAIMS_PER_NODE
from tests.test_fleet_claims import _claim, _get, _post

from tests.test_fleet_sync import _event


@pytest.fixture()
def hub(tmp_path):
    """Keep this three-value transport fixture local to this test module."""
    db = tmp_path / "hub" / "fleet.db"
    token = "test-token-12345"
    server = fleet_hub.make_server("localhost", 0, db, token, allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}", token, db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _raw_post(url, token, raw):
    request = urllib.request.Request(
        url + "/events", data=raw, headers={"Authorization": f"Bearer {token}"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_body_byte_boundary(hub, monkeypatch):
    url, token, db = hub
    monkeypatch.setattr(fleet_hub_http, "MAX_BODY_BYTES", 1024)
    raw = json.dumps(_event()).encode().ljust(1024)
    assert _raw_post(url, token, raw)[0] == 200
    status, payload = _raw_post(url, token, raw + b" ")
    assert status == 413
    assert "body" in payload["error"]
    with fleet_hub.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize("extra", [False, True], ids=["at-limit", "over-limit"])
def test_array_boundary_rejects_whole_batch(hub, extra):
    url, token, db = hub
    events = [_event(run_id=f"run-{i}") for i in range(1000 + extra)]
    status, _ = _post(url, token, events, path="/events")
    assert status == (400 if extra else 200)
    with fleet_hub.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == (0 if extra else 1000)


@pytest.mark.parametrize("extra", [False, True], ids=["at-limit", "over-limit"])
def test_nested_json_boundary(hub, extra):
    url, token, db = hub
    value = 0
    for _ in range(31 + extra):
        value = [value]
    event = {**_event(), "ignored": value}
    status, _ = _post(url, token, event, path="/events")
    assert status == (400 if extra else 200)
    with fleet_hub.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == (0 if extra else 1)


def test_ignored_array_is_bounded_before_writes(hub):
    url, token, db = hub
    status, _ = _post(url, token, {**_event(), "ignored": [0] * 1001}, path="/events")
    assert status == 400
    with fleet_hub.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def _claim_snapshot(conn):
    return (
        conn.execute("SELECT * FROM claims ORDER BY target").fetchall(),
        conn.execute("SELECT * FROM claim_fences ORDER BY target").fetchall(),
    )


def test_claim_cardinality_preserves_existing_claims(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        for i in range(MAX_ACTIVE_CLAIMS_PER_NODE):
            assert fleet_hub.handle_claim(conn, _claim(target=f"repo-{i:04}"))[0] == 200
        before = _claim_snapshot(conn)
        with pytest.raises(ClaimCardinalityError, match="active claims per node exceed limit") as caught:
            fleet_hub.handle_claim(conn, _claim(target="overflow"))
        assert caught.value.payload()["code"] == "claim_cardinality_exceeded"
        assert not conn.in_transaction
        assert _claim_snapshot(conn) == before
        assert fleet_hub.handle_claim(conn, _claim(target="repo-0000"))[0] == 200
        assert fleet_hub.handle_claim(conn, _claim(node="other-node", target="other-repo"))[0] == 200
    finally:
        conn.close()


def test_status_pages_and_client_follow_cursor(hub, monkeypatch):
    url, token, _db = hub
    assert _post(url, token, [_event(run_id=f"run-{i:04}") for i in range(201)], path="/events")[0] == 200
    status, first = _get(url, "/status?all=1", token)
    assert status == 200
    assert len(first["runs"]) == 200
    assert first["truncated"] is True
    _, second = _get(url, "/status?all=1&cursor=" + first["next_cursor"], token)
    assert len(second["runs"]) == 1
    assert second["truncated"] is False
    assert second["next_cursor"] is None
    assert {r["run_id"] for r in first["runs"]}.isdisjoint(r["run_id"] for r in second["runs"])
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": url, "token": token})
    assert len(fleet_client.fetch_status(include_all=True)) == 201


@pytest.mark.parametrize("path,key", [("/claims", "claims"), ("/nodes", "nodes"), ("/sessions", "sessions")])
def test_list_page_limit_is_validated(hub, path, key):
    url, token, _db = hub
    assert _get(url, path + "?limit=200", token)[0] == 200
    status, payload = _get(url, path + "?limit=201", token)
    assert status == 400
    assert "limit" in payload["error"]
    assert _get(url, path + "?cursor=invalid", token)[0] == 400


@pytest.mark.parametrize("extra", [False, True], ids=["at-limit", "over-limit"])
def test_aggregate_projection_refuses_incomplete_read(hub, extra):
    url, token, db = hub
    with fleet_hub.open_db(db) as conn:
        conn.executemany(
            "INSERT INTO model_policy (seat, provider, model, enabled, updated_at) VALUES (?, 'example', 'model', 1, '2026-01-01')",
            [(f"seat-{i}",) for i in range(1000 + extra)],
        )
    status, payload = _get(url, "/models", token)
    assert status == (413 if extra else 200)
    if extra:
        assert payload["truncated"] is True
        assert payload["code"] == "result_limit_exceeded"
        assert "mac" not in payload


@pytest.mark.parametrize("path,key", [("/claims", "claims"), ("/nodes", "nodes"), ("/sessions", "sessions")])
def test_core_lists_page_without_loss(hub, monkeypatch, path, key):
    from brigade import fleet_hub_sessions

    url, token, db = hub
    with fleet_hub.open_db(db) as conn:
        for i in range(201):
            if key == "claims":
                assert fleet_hub.handle_claim(conn, _claim(target=f"repo-{i:04}"))[0] == 200
            elif key == "nodes":
                conn.execute(
                    "INSERT INTO nodes (node_id, token_sha256, created_at) VALUES (?, ?, '2026-01-01')",
                    (f"node-{i:04}", f"fake-digest-{i}"),
                )
            else:
                fleet_hub_sessions.handle_session(
                    conn,
                    {
                        "action": "upsert",
                        "node_id": "example-node",
                        "harness": "codex",
                        "session_id": f"session-{i:04}",
                        "repo_identity": "repo:example",
                        "identity_scope": "fleet",
                        "repo_label": "example",
                        "checkout_path": "/example",
                        "dirty_paths": [],
                        "dirty_truncated": False,
                    },
                    caller_node=None,
                )
    _, first = _get(url, path, token)
    assert len(first[key]) == 200
    assert first["truncated"] is True
    _, last = _get(url, path + "?cursor=" + first["next_cursor"], token)
    assert len(last[key]) == 1
    assert last["next_cursor"] is None
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": url, "token": token})
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": url, "admin_token": token})
    fetch = {
        "claims": fleet_client.fetch_claims,
        "nodes": fleet_client.fetch_nodes,
        "sessions": fleet_client.fetch_sessions,
    }[key]
    assert len(fetch()) == 201


def test_page_offset_boundary(hub):
    url, token, _db = hub
    assert _get(url, "/nodes?cursor=1000000", token)[0] == 200
    assert _get(url, "/nodes?cursor=1000001", token)[0] == 400


def test_client_page_budget_boundary(monkeypatch):
    from brigade import fleet_client_pagination

    monkeypatch.setattr(fleet_client_pagination, "MAX_LIST_PAGES", 2)
    pages = iter([{"runs": [], "next_cursor": "1"}, {"runs": [{"run_id": "last"}]}])
    assert fleet_client_pagination.collect_pages(lambda _: next(pages), "/status", "runs") == [{"run_id": "last"}]
    counter = iter([1, 2, 3])
    with pytest.raises(fleet_client.FleetClientError, match="page limit"):
        fleet_client_pagination.collect_pages(
            lambda _: {"runs": [], "next_cursor": str(next(counter))}, "/status", "runs"
        )


@pytest.mark.parametrize("path", ["/dashboard/sessions", "/deck/work"], ids=["json", "html"])
def test_caught_read_limit_keeps_truncation_metadata(hub, monkeypatch, path):
    from brigade import fleet_hub_read_bounds

    url, token, _db = hub
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")

    def oversized_projection(conn, **_kwargs):
        return conn.execute(
            "WITH RECURSIVE rows(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM rows WHERE value < ?) "
            "SELECT value FROM rows",
            (fleet_hub_read_bounds.MAX_PROJECTION_ROWS + 1,),
        ).fetchall()

    if path == "/dashboard/sessions":
        monkeypatch.setattr(fleet_hub_http.enrollment, "list_sessions", oversized_projection)
    else:
        monkeypatch.setattr(fleet_hub_http.fleet_work_page, "load_page", oversized_projection)
    request = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {token}"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    response = caught.value
    assert response.code == 413
    raw = response.read()
    if path == "/dashboard/sessions":
        payload = json.loads(raw)
        assert payload["truncated"] is True
        assert payload["code"] == "result_limit_exceeded"
        assert "complete projection exceeds" in payload["error"]
    else:
        assert response.headers["X-Brigade-Truncated"] == "true"
        assert "Results truncated" in raw.decode()


@pytest.mark.parametrize("damage", ["table", "row", "expired-row", "off-page-row"])
def test_claim_page_preserves_missing_fence_refusal(hub, damage):
    url, token, db = hub
    with fleet_hub.open_db(db) as conn:
        for target in ("repo-0", "repo-1", "repo-2"):
            assert fleet_hub.handle_claim(conn, _claim(target=target))[0] == 200
        if damage == "table":
            conn.execute("DROP TABLE claim_fences")
        else:
            target = "repo-2" if damage == "off-page-row" else "repo-1"
            conn.execute("DELETE FROM claim_fences WHERE target = ?", (target,))
        if damage == "expired-row":
            conn.execute("UPDATE claims SET expires_at = 0 WHERE target = 'repo-1'")
        before = conn.execute("SELECT * FROM claims ORDER BY target").fetchall()
    status, payload = _get(url, "/claims?limit=1", token)
    assert status == 503
    assert payload == {
        "error": "claim generation fence storage is missing or corrupt"
        if damage == "table"
        else "claim generation fence is missing or corrupt"
    }
    with fleet_hub.open_db(db) as conn:
        assert conn.execute("SELECT * FROM claims ORDER BY target").fetchall() == before


def test_http_claim_cardinality_refusal_preserves_existing_claims(hub):
    url, token, db = hub
    with fleet_hub.open_db(db) as conn:
        for i in range(MAX_ACTIVE_CLAIMS_PER_NODE):
            assert fleet_hub.handle_claim(conn, _claim(target=f"repo-{i:04}"))[0] == 200
        before = _claim_snapshot(conn)
    status, payload = _post(url, token, _claim(target="overflow"))
    assert status == 429
    assert payload["granted"] is False
    assert payload["code"] == "claim_cardinality_exceeded"
    assert payload["limit"] == MAX_ACTIVE_CLAIMS_PER_NODE
    assert "active claims per node exceed limit" in payload["error"]
    with fleet_hub.open_db(db) as conn:
        assert _claim_snapshot(conn) == before
    assert _post(url, token, _claim(target="repo-0000"))[0] == 200


def test_json_null_gets_a_clear_400_without_writes(hub):
    url, token, db = hub
    status, payload = _raw_post(url, token, b"null")
    assert status == 400
    assert "body" in payload["error"]
    with fleet_hub.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize("extra", [False, True], ids=["at-limit", "over-limit"])
def test_machine_stream_keeps_distinct_node_budget(hub, extra):
    from brigade.fleet_hub_read_bounds import MAX_PROJECTION_ROWS

    url, token, _db = hub
    events = [
        _event(node=f"node-{i:04}", run_id=f"run-{i:04}", state="run.completed")
        for i in range(MAX_PROJECTION_ROWS + extra)
    ]
    for start in range(0, len(events), 1000):
        assert _post(url, token, events[start : start + 1000], path="/events")[0] == 200
    request = urllib.request.Request(url + "/view/machines?all=1", headers={"Authorization": f"Bearer {token}"})
    if extra:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 413
        payload = json.loads(caught.value.read())
        assert payload["truncated"] is True
        assert payload["code"] == "result_limit_exceeded"
    else:
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200
            page = response.read().decode()
        assert page.count("<tr data-bucket=") == 200
