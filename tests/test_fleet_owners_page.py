"""Hub HTTP contracts for the ownership map: ``GET /owners`` and ``/deck/owners``."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager

from brigade import fleet_hub, fleet_hub_policy, fleet_policy

TOKEN = "test-admin-token-owners"  # content-guard: allow api-key-assignment
LOOPBACK = "127.0.0.1"  # content-guard: allow loopback-ipv4


@contextmanager
def _hub(tmp_path, **kwargs):
    db = tmp_path / "fleet.db"
    server = fleet_hub.make_server(LOOPBACK, 0, db, TOKEN, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (LOOPBACK, server.server_address[1]), db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, path, *, headers=None, method="GET", body=None):
    conn = http.client.HTTPConnection(*hub, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


def _bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _cookie(hub):
    status, _, body = _request(
        hub,
        "/dashboard/enrollment",
        method="POST",
        headers={**_bearer(), "Content-Type": "application/json"},
        body=b"{}",
    )
    assert status == 201
    code = json.loads(body)["code"]
    status, headers, _ = _request(
        hub,
        "/enroll",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"},
        body=f"code={code}".encode(),
    )
    assert status == 303
    return {"Cookie": headers["Set-Cookie"].split(";")[0]}


def _seed_owners(db, owners):
    conn = fleet_hub.open_db(db)
    try:
        document = fleet_policy.empty_document()
        document["owners"] = owners
        revision = fleet_hub_policy.current_policy(conn)["revision"]
        fleet_hub_policy.save_policy(conn, document, expected_version=revision, actor="admin", reason="seed owners")
    finally:
        conn.close()


def _node_token(db, node_id="node-a-0001"):
    conn = fleet_hub.open_db(db)
    try:
        return fleet_hub.add_node(conn, node_id, "Node A")[1]
    finally:
        conn.close()


def test_owners_json_requires_a_bearer_token(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        status, _, body = _request(hub, "/owners")
        assert status == 401 and json.loads(body) == {"error": "unauthorized"}
        assert _request(hub, "/owners", headers=_bearer("wrong-token"))[0] == 401
        assert _request(hub, "/owners", headers=_cookie(hub))[0] == 401


def test_owners_json_serves_the_v1_shape_to_admin_and_node_tokens(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed_owners(db, {"alpha": {"label": "Alpha", "owns": ["Notes"], "match": {"harnesses": ["claude"]}}})
        for headers in (_bearer(), _bearer(_node_token(db))):
            status, response_headers, body = _request(hub, "/owners", headers=headers)
            assert status == 200
            assert response_headers["Content-Type"].startswith("application/json")
            payload = json.loads(body)
            assert payload["schema"] == "brigade.fleet_owners.v1"
            assert payload["policy_version"] == 2
            assert [owner["name"] for owner in payload["owners"]] == ["alpha"]
            assert payload["owners"][0]["owns"] == ["Notes"]
            assert payload["unassigned"] == [] and payload["overlaps"] == []


def test_owners_json_lists_live_claims_and_reads_without_writing(tmp_path):
    with _hub(tmp_path, allow_admin_writes=True) as (hub, db):
        _seed_owners(db, {"alpha": {"label": "Alpha", "match": {"harnesses": ["claude"]}}})
        claim = {
            "action": "acquire",
            "target": "example/repo",
            "node_id": "node-a-0001",
            "holder": "holder-1",
            "harness": "claude",
        }
        status, _, text = _request(
            hub,
            "/claims",
            method="POST",
            headers={**_bearer(), "Content-Type": "application/json"},
            body=json.dumps(claim),
        )
        assert status == 200, text
        conn = fleet_hub.open_db(db)
        try:
            before = list(conn.iterdump())
        finally:
            conn.close()
        status, _, body = _request(hub, "/owners", headers=_bearer())
        assert status == 200
        holdings = json.loads(body)["owners"][0]["holdings"]
        assert [(item["kind"], item["ref"]) for item in holdings] == [("claim", "example/repo")]
        conn = fleet_hub.open_db(db)
        try:
            assert list(conn.iterdump()) == before
        finally:
            conn.close()
