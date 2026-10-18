"""Hard ingestion and claim bounds at the authenticated Hub boundary."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from brigade import fleet_hub, fleet_hub_http
from brigade.fleet_hub_claim_cardinality import ClaimCardinalityError, MAX_ACTIVE_CLAIMS_PER_NODE
from tests.test_fleet_claims import _claim, _post

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
        assert caught.value.payload()["reason"] == "claim-cardinality-exceeded"
        assert not conn.in_transaction
        assert _claim_snapshot(conn) == before
        assert fleet_hub.handle_claim(conn, _claim(target="repo-0000"))[0] == 200
        assert fleet_hub.handle_claim(conn, _claim(node="other-node", target="other-repo"))[0] == 200
    finally:
        conn.close()


def test_stale_holder_refusal_precedes_claim_cardinality(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        assert fleet_hub.handle_claim(conn, _claim(target="retired", holder="old"))[0] == 200
        assert fleet_hub.handle_claim(conn, _claim("release", target="retired", holder="old"))[0] == 200
        for i in range(MAX_ACTIVE_CLAIMS_PER_NODE):
            assert fleet_hub.handle_claim(conn, _claim(target=f"repo-{i:04}"))[0] == 200
        status, payload = fleet_hub.handle_claim(conn, _claim(target="retired", holder="old"))
        assert (status, payload["reason"]) == (409, "stale-holder")
    finally:
        conn.close()


def test_http_claim_cardinality_refusal_preserves_existing_claims(hub):
    url, token, db = hub
    with fleet_hub.open_db(db) as conn:
        for i in range(MAX_ACTIVE_CLAIMS_PER_NODE):
            assert fleet_hub.handle_claim(conn, _claim(target=f"repo-{i:04}"))[0] == 200
        before = _claim_snapshot(conn)
    status, payload = _post(url, token, _claim(target="overflow"))
    assert status == 409
    assert payload["granted"] is False
    assert payload["reason"] == "claim-cardinality-exceeded"
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
