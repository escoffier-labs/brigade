"""Per-field length bounds at fleet hub event ingestion (#1151)."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from brigade import fleet_client, fleet_hub, run_events
from brigade.fleet_hub_event_bounds import EVENT_FIELD_MAX_CHARS

NODE = "11111111-1111-4111-8111-111111111111"


def _event(seq: int = 1, **overrides: object) -> dict:
    event = {
        "node_id": NODE,
        "run_id": "r1",
        "repo": "repo-a",
        "seat": "worker",
        "harness": "claude",
        "state": "run.created",
        "ts": "2026-08-23T12:00:00+00:00",
        "sequence": seq,
        "digest": f"d{seq}",
        "capability_fingerprint": "f" * 64,
    }
    event.update(overrides)
    return event


def _count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    yield connection
    connection.close()


def test_every_string_event_field_is_bounded():
    expected = set(fleet_hub.EVENT_FIELDS) - {"sequence"}
    expected |= set(fleet_hub.OPTIONAL_STR_FIELDS) | set(fleet_hub.OPTIONAL_EVENT_STR_FIELDS)
    assert set(EVENT_FIELD_MAX_CHARS) == expected


@pytest.mark.parametrize("field", sorted(set(EVENT_FIELD_MAX_CHARS) - {"node_id", "repo_identity"}))
def test_field_at_limit_is_stored_and_one_over_is_rejected(conn, field):
    limit = EVENT_FIELD_MAX_CHARS[field]
    assert fleet_hub.store_events(conn, _event(seq=1, **{field: "a" * limit})) == {"accepted": 1, "duplicate": 0}
    with pytest.raises(fleet_hub.FleetHubUnprocessable, match=f"{field}.*{limit}"):
        fleet_hub.store_events(conn, _event(seq=2, **{field: "a" * (limit + 1)}))
    assert _count(conn) == 1


def test_largest_journal_event_a_client_forwards_is_stored(conn, monkeypatch):
    envelope = run_events.build_event(
        run_id="r" * 2000,
        sequence=1,
        event_type="run.dispatch.requested",
        payload={"seat": "s" * run_events.MAX_PAYLOAD_STR_LEN, "attempt": 1},
        idempotency_key="k1",
        recorded_at="2026-10-12T15:00:00.000000Z",
        previous_digest=None,
    )
    forwarded: list[dict] = []
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub.invalid"})
    monkeypatch.setattr(fleet_client, "report_event", lambda event, **_kw: forwarded.append(event) or True)
    assert fleet_client.report_journal_event(envelope)
    assert fleet_hub.store_events(conn, {**forwarded[0], "node_id": NODE})["accepted"] == 1


def test_node_id_bound_applies_before_caller_match(conn):
    long_node = "n" * (EVENT_FIELD_MAX_CHARS["node_id"] + 1)
    with pytest.raises(fleet_hub.FleetHubUnprocessable, match="node_id"):
        fleet_hub.store_events(conn, _event(node_id=long_node), caller_node=long_node)
    assert _count(conn) == 0


def test_whitespace_padding_counts_toward_the_bound(conn):
    padded = "repo" + " " * EVENT_FIELD_MAX_CHARS["repo"]
    with pytest.raises(fleet_hub.FleetHubUnprocessable, match="repo"):
        fleet_hub.store_events(conn, _event(repo=padded))
    assert _count(conn) == 0


def test_one_oversized_event_rejects_the_whole_batch(conn):
    batch = [_event(seq=1), _event(seq=2, run_id="r" * (EVENT_FIELD_MAX_CHARS["run_id"] + 1))]
    with pytest.raises(fleet_hub.FleetHubUnprocessable):
        fleet_hub.store_events(conn, batch)
    assert _count(conn) == 0


def test_oversized_field_over_http_is_422_and_nothing_is_stored(tmp_path):
    db = tmp_path / "hub" / "fleet.db"
    token = "test-token-12345"
    server = fleet_hub.make_server("127.0.0.1", 0, db, token, allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = _event(seat="s" * (EVENT_FIELD_MAX_CHARS["seat"] + 1))
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/events",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        assert caught.value.code == 422
        assert "seat" in json.loads(caught.value.read())["error"]
    finally:
        server.shutdown()
        server.server_close()
    check = sqlite3.connect(str(db))
    try:
        assert _count(check) == 0
    finally:
        check.close()


@pytest.mark.parametrize(
    "context",
    [
        {"schema": "other"},
        {"schema": "brigade.t3_execution_context.v1", "prompt": "text"},
        {"schema": "brigade.t3_execution_context.v1", "request_id": "x" * 129},
        {"schema": "brigade.t3_execution_context.v1", "initiator": {"label": "x\n"}},
        {"schema": "brigade.t3_execution_context.v1", "initiator": {"kind": "admin"}},
        {"schema": "brigade.t3_execution_context.v1", "policy": {"token": "test"}},
        {"schema": "brigade.t3_execution_context.v1", "issue_url": "https://github.com/a/b/issues/1?token=example"},
        {"schema": "brigade.t3_execution_context.v1", "issue_url": "http://github.com/a/b/pull/1"},
        {"schema": "brigade.t3_execution_context.v1", "thread_id": "Bearer example-credential"},
        {"schema": "brigade.t3_execution_context.v1", "thread_id": "token=example-credential"},
        {"schema": "brigade.t3_execution_context.v1", "thread_id": "/tmp/private-file"},
        {"schema": "brigade.t3_execution_context.v1", "thread_id": "x\u200b"},
        {"schema": "brigade.t3_execution_context.v1", "policy": []},
        {"schema": "brigade.t3_execution_context.v1", "thread_id": 12},
    ],
)
def test_execution_context_is_closed_bounded_and_safe(conn, context):
    with pytest.raises(fleet_hub.FleetHubUnprocessable):
        fleet_hub.store_events(conn, _event(context=context))
    assert _count(conn) == 0


def test_execution_context_total_utf8_bound(conn):
    context = {"schema": "brigade.t3_execution_context.v1"}
    context.update(
        {
            key: "\U0001f600" * 128
            for key in ("request_id", "thread_id", "turn_ref", "controller_id", "origin_node", "target_host", "work_id")
        }
    )
    context["policy"] = {"session_id": "\U0001f600" * 128}
    with pytest.raises(fleet_hub.FleetHubUnprocessable):
        fleet_hub.store_events(conn, _event(context=context))
