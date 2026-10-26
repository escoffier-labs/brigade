"""Public Hub key directory, opaque statements, and live paging contracts."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from brigade import fleet_hub, fleet_hub_messages as messages, fleet_hub_paging as paging


def binary(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def record(principal="node-b", seed=1, **extra):
    sig, enc = bytes([seed]) * 32, bytes([seed]) * 1216
    return {
        "v": 1,
        "principal": principal,
        "hub": "h_" + binary(bytes(16)),
        "kid": "k1_" + binary(hashlib.sha256(b"brigade-mesh/v1/kid\0" + sig + enc).digest()[:16]),
        "sig_pub": binary(sig),
        "enc_pub": binary(enc),
        "suites": ["bm1"],
        "created_at": f"2026-10-22T12:00:{seed:02d}+00:00",
        "self_sig": binary(bytes(64)),
        **extra,
    }


def identity():
    public = bytes(32)
    return {
        "v": 1,
        "hub_id": "h_" + binary(hashlib.sha256(b"brigade-mesh/v1/hub-id\0" + public).digest()[:16]),
        "hub_sig_pub": binary(public),
        "seq": 3,
        "backend": "full",
        "capabilities": ["messages"],
        "endpoint": "https://hub.invalid",
        "require_sealed": False,
        "max_envelope_bytes": 98304,
        "envelope_versions": [1],
        "suites": ["bm1"],
        "created_at": "2026-10-22T12:00:00Z",
        "sig": binary(bytes(64)),
    }


@pytest.fixture
def hub(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    for name in ("node-a", "node-b", "node-c"):
        fleet_hub.add_node(conn, name)
    yield conn
    conn.close()


def action(conn, name, caller="node-a", **args):
    return messages.handle(conn, {"action": name, **args}, caller=caller)


def publish(conn, value):
    return action(conn, "keys_publish", caller=value["principal"], record=value)


def test_publish_only_authenticated_principal(hub):
    value = record()
    with pytest.raises(messages.MessageForbidden):
        action(hub, "keys_publish", record=value)
    assert publish(hub, value)[0] == 201
    found = action(hub, "keys_get", kid=value["kid"])[1]["key"]
    assert found["record"] == value and found["state"] == "active"
    with pytest.raises(messages.MessageAuthError):
        action(hub, "keys_publish", caller="missing-node", record=record("missing-node"))


def test_replacement_requires_current_prev_kid(hub):
    old = record()
    publish(hub, old)
    for extra in ({}, {"prev_kid": record(seed=9)["kid"], "prev_sig": binary(bytes(64))}, {"prev_kid": old["kid"]}):
        with pytest.raises(messages.MessageConflict) as caught:
            publish(hub, record(seed=2, **extra))
        assert caught.value.code == "principal_key_exists"
    new = record(seed=2, prev_kid=old["kid"], prev_sig=binary(bytes(64)))
    assert publish(hub, new)[0] == 201
    assert action(hub, "keys_get", kid=old["kid"])[1]["key"]["state"] == "retired"
    assert action(hub, "keys_get", kid=old["kid"])[1]["key"]["record"] == old


def test_revoked_kid_cannot_be_republished(hub):
    value = record()
    publish(hub, value)
    fleet_hub.revoke_node(hub, "node-b")
    fleet_hub.add_node(hub, "node-b")
    with pytest.raises(messages.MessageConflict) as caught:
        publish(hub, value)
    assert caught.value.code == "principal_key_revoked"


@pytest.mark.parametrize("mutation", ["publish", "revoke", "rotate", "grants"])
def test_who_continuation_reads_live_keys(hub, mutation):
    messages.add_principal(hub, {"name": "reader", "may_send_to": ["node:*"]})
    value = record()
    if mutation != "publish":
        publish(hub, value)
    first = action(hub, "who", caller="agent:reader", limit=1)[1]
    assert first["nodes"][0]["node_id"] == "node-a"
    assert "sig_pub" not in str(hub.execute("SELECT payload FROM fleet_read_snapshot_rows").fetchall())
    if mutation == "publish":
        publish(hub, value)
    elif mutation == "rotate":
        value = record(seed=2, prev_kid=value["kid"], prev_sig=binary(bytes(64)))
        publish(hub, value)
    elif mutation == "revoke":
        fleet_hub.revoke_node(hub, "node-b")
    else:
        messages.handle_principal_admin(hub, {"action": "grants", "name": "reader", "may_send_to": ["node-c"]})
    page = action(hub, "who", caller="agent:reader", limit=1, cursor=first["next_cursor"])[1]
    if mutation in ("revoke", "grants"):
        assert all(row["node_id"] != "node-b" for row in page["nodes"])
    else:
        assert page["nodes"][0]["keys"][0]["record"] == value


@pytest.mark.parametrize("principal", ["node-b", "agent:peer"])
def test_key_directory_respects_grants(hub, principal):
    enrolled = messages.add_principal(hub, {"name": "peer"}) if principal.startswith("agent:") else None
    reader = messages.add_principal(hub, {"name": "reader", "may_send_to": [principal]})
    messages.add_principal(hub, {"name": "limited", "may_send_to": ["node-c"]})
    old = record(principal)
    publish(hub, old)
    value = record(principal, seed=2, prev_kid=old["kid"], prev_sig=binary(bytes(64)))
    publish(hub, value)
    assert action(hub, "keys_get", caller="agent:reader", kid=old["kid"])[1]["key"]["state"] == "retired"
    first = action(hub, "who", limit=1)[1]
    assert first["next_cursor"]
    if enrolled:
        messages.revoke_principal(
            hub, {"action": "revoke", "name": "peer", "expected_enrollment_id": enrolled["enrollment_id"]}
        )
    else:
        fleet_hub.revoke_node(hub, principal)
    before = hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall()
    for original in (old, value):
        for caller in ("node-a", "agent:reader"):
            status, payload = action(hub, "keys_get", caller=caller, kid=original["kid"])
            assert status == 200
            key = payload["key"]
            assert key["record"] == original and key["state"] == "revoked"
            assert key["principal_id"] == principal
            assert key["compromised_from"] == original["created_at"] and key["revoked_at"]
            stored = next(row for row in before if row[1] == original["kid"])
            assert key["revoked_at"] == stored[6] and key["compromised_from"] == stored[7]
        assert action(hub, "keys_get", caller="agent:limited", kid=original["kid"])[0] == 404
        assert action(hub, "keys_get", caller=None, kid=original["kid"])[0] == 403
    assert hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall() == before
    pages = [action(hub, "who")[1]]
    cursor = first["next_cursor"]
    while cursor:
        page = action(hub, "who", limit=1, cursor=cursor)[1]
        pages.append(page)
        cursor = page["next_cursor"]
        assert len(pages) < 10
    for page in pages:
        assert all(row["node_id"] != principal for row in page["nodes"])
        assert all(row["principal_id"] != principal for row in page["principals"])
    messages.revoke_principal(
        hub, {"action": "revoke", "name": "reader", "expected_enrollment_id": reader["enrollment_id"]}
    )
    fleet_hub.revoke_node(hub, "node-c")
    for caller in ("agent:reader", "node-c"):
        with pytest.raises(messages.MessageAuthError):
            action(hub, "keys_get", caller=caller, kid=value["kid"])
    with pytest.raises(messages.MessageError):
        action(hub, "keys_get", kid="k1_invalid")


@pytest.mark.parametrize("principal", ["node-b", "agent:peer"])
def test_revoke_node_and_principal_keys_atomically(hub, principal):
    enrolled = messages.add_principal(hub, {"name": "peer"}) if principal.startswith("agent:") else None
    old = record(principal)
    publish(hub, old)
    publish(hub, record(principal, seed=2, prev_kid=old["kid"], prev_sig=binary(bytes(64))))

    def revoke():
        if enrolled:
            return messages.revoke_principal(
                hub, {"action": "revoke", "name": "peer", "expected_enrollment_id": enrolled["enrollment_id"]}
            )
        return fleet_hub.revoke_node(hub, principal)

    hub.execute(
        "CREATE TRIGGER reject_key_revoke BEFORE UPDATE ON fleet_message_keys BEGIN SELECT RAISE(ABORT,'key storage failure'); END"
    )
    hub.commit()
    with pytest.raises(sqlite3.IntegrityError, match="key storage failure"):
        revoke()
    table, column = ("fleet_message_principals", "principal_id") if enrolled else ("nodes", "node_id")
    assert hub.execute(f"SELECT revoked_at FROM {table} WHERE {column}=?", (principal,)).fetchone() == (None,)
    assert hub.execute("SELECT state FROM fleet_message_keys ORDER BY created_at").fetchall() == [
        ("retired",),
        ("active",),
    ]
    hub.execute("DROP TRIGGER reject_key_revoke")
    hub.commit()
    revoke()
    rows = hub.execute("SELECT state,created_at,compromised_from,revoked_at FROM fleet_message_keys").fetchall()
    assert all(
        state == "revoked" and created == compromised and revoked for state, created, compromised, revoked in rows
    )
    revoke()
    assert hub.execute("SELECT state,created_at,compromised_from,revoked_at FROM fleet_message_keys").fetchall() == rows


@contextmanager
def serving(path):
    server = fleet_hub.make_server("127.0.0.1", 0, path, "fixture-admin")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request(address, method, path, token=None, value=None):
    conn = http.client.HTTPConnection(*address, timeout=3)
    headers = {"Authorization": "Bearer " + token} if token else {}
    conn.request(method, path, body=json.dumps(value) if value is not None else None, headers=headers)
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


@pytest.mark.parametrize("principal", ["node-reader", "agent:reader"])
@pytest.mark.parametrize("read_action", ["keys_get", "manifests_get"])
def test_http_mesh_read_rejects_replaced_generation_after_auth(hub, monkeypatch, principal, read_action):
    from brigade import fleet_hub_event_bounds as bounds, fleet_hub_identity as mesh

    old_key = record()
    publish(hub, old_key)
    current_key = record(seed=2, prev_kid=old_key["kid"], prev_sig=binary(bytes(64)))
    publish(hub, current_key)
    fleet_hub.revoke_node(hub, "node-b")
    statement = {"seq": 1, "sig": "opaque-fixture"}
    mesh.upload_statement(hub, {"kind": "manifest", "statement": statement})
    if principal.startswith("agent:"):
        enrolled = messages.add_principal(hub, {"name": "reader"})
        old_token = enrolled["token"]
    else:
        _, old_token = fleet_hub.add_node(hub, principal)
    raw = {"action": read_action}
    if read_action == "keys_get":
        raw["kid"] = old_key["kid"]
    authenticated, release_body = threading.Event(), threading.Event()
    read_body = bounds.read_json_body

    def paused_body(handler, **kwargs):
        if handler.path == "/messages" and handler.headers.get("Authorization") == "Bearer " + old_token:
            authenticated.set()
            assert release_body.wait(2), "body-read barrier was not released"
        return read_body(handler, **kwargs)

    path = hub.execute("PRAGMA database_list").fetchone()[2]
    with serving(path) as address:
        if principal.startswith("agent:") and read_action == "keys_get":
            assert request(address, "POST", "/messages", old_token, raw)[0] == 404
        monkeypatch.setattr(bounds, "read_json_body", paused_body)
        with ThreadPoolExecutor(max_workers=1) as executor:
            inflight = executor.submit(request, address, "POST", "/messages", old_token, raw)
            try:
                assert authenticated.wait(2), "request did not reach the post-authentication body barrier"
                if principal.startswith("agent:"):
                    messages.revoke_principal(
                        hub, {"action": "revoke", "name": "reader", "expected_enrollment_id": enrolled["enrollment_id"]}
                    )
                    fresh = messages.add_principal(
                        hub,
                        {
                            "name": "reader",
                            "expected_enrollment_id": enrolled["enrollment_id"],
                            "may_send_to": ["node-b"],
                        },
                    )
                    fresh_token = fresh["token"]
                    assert messages.lookup_principal_token(hub, old_token) is None
                    assert messages.lookup_principal_token(hub, fresh_token) == principal
                    assert messages.get_principal(hub, {"action": "get", "name": "reader"})["may_send_to"] == ["node-b"]
                else:
                    fleet_hub.revoke_node(hub, principal)
                    _, fresh_token = fleet_hub.add_node(hub, principal)
                    assert fleet_hub.lookup_node_token(hub, old_token)[0] is None
                    assert fleet_hub.lookup_node_token(hub, fresh_token)[0] == principal
                before_keys = hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall()
                before_statements = hub.execute("SELECT * FROM fleet_mesh_statements ORDER BY kind").fetchall()
                before_grants = hub.execute(
                    "SELECT * FROM fleet_message_send_grants ORDER BY principal_id,pattern"
                ).fetchall()
            finally:
                release_body.set()
            status, payload = inflight.result(timeout=3)
        fresh_status, fresh_payload = request(address, "POST", "/messages", fresh_token, raw)
        assert fresh_status == 200
        if read_action == "keys_get":
            key = fresh_payload["key"]
            assert key["record"] == old_key and key["state"] == "revoked"
            assert key["principal_id"] == "node-b"
            assert key["compromised_from"] == old_key["created_at"] and key["revoked_at"]
        else:
            assert fresh_payload == {"statements": {"manifest": statement}}
        assert hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall() == before_keys
        assert hub.execute("SELECT * FROM fleet_mesh_statements ORDER BY kind").fetchall() == before_statements
        assert (
            hub.execute("SELECT * FROM fleet_message_send_grants ORDER BY principal_id,pattern").fetchall()
            == before_grants
        )
        assert status == 403, payload
        assert payload["code"] == "message_sender_inactive"
        assert "credential generation changed" in payload["error"]


def enroll_reader(conn, principal):
    if principal.startswith("agent:"):
        enrolled = messages.add_principal(conn, {"name": "reader"})
        return enrolled["token"], enrolled
    return fleet_hub.add_node(conn, principal)[1], None


def replace_reader(conn, principal, enrolled):
    if enrolled is None:
        fleet_hub.revoke_node(conn, principal)
        return fleet_hub.add_node(conn, principal)[1]
    messages.revoke_principal(
        conn, {"action": "revoke", "name": "reader", "expected_enrollment_id": enrolled["enrollment_id"]}
    )
    return messages.add_principal(
        conn,
        {"name": "reader", "expected_enrollment_id": enrolled["enrollment_id"], "may_send_to": ["node-b"]},
    )["token"]


@pytest.mark.parametrize("principal", ["node-reader", "agent:reader"])
@pytest.mark.parametrize("read_action", ["who", "claims"])
def test_http_station_read_rejects_replaced_generation_after_auth(hub, monkeypatch, principal, read_action):
    from brigade import fleet_hub_event_bounds as bounds

    value = record()
    publish(hub, value)
    assert (
        fleet_hub.handle_claim(
            hub,
            {"action": "acquire", "target": "example", "node_id": "node-b", "holder": "fixture"},
            caller_node="node-b",
        )[0]
        == 200
    )
    old_token, enrolled = enroll_reader(hub, principal)
    raw = {"action": read_action}
    authenticated, release_body = threading.Event(), threading.Event()
    read_body = bounds.read_json_body

    def paused_body(handler, **kwargs):
        if handler.path == "/messages" and handler.headers.get("Authorization") == "Bearer " + old_token:
            authenticated.set()
            assert release_body.wait(2), "body-read barrier was not released"
        return read_body(handler, **kwargs)

    path = hub.execute("PRAGMA database_list").fetchone()[2]
    with serving(path) as address:
        if enrolled:
            before = request(address, "POST", "/messages", old_token, raw)[1]
            assert before["nodes" if read_action == "who" else "claims"] == []
        monkeypatch.setattr(bounds, "read_json_body", paused_body)
        with ThreadPoolExecutor(max_workers=1) as executor:
            inflight = executor.submit(request, address, "POST", "/messages", old_token, raw)
            try:
                assert authenticated.wait(2), "request did not reach the post-authentication body barrier"
                fresh_token = replace_reader(hub, principal, enrolled)
                if enrolled:
                    assert messages.lookup_principal_token(hub, old_token) is None
                    assert messages.lookup_principal_token(hub, fresh_token) == principal
                else:
                    assert fleet_hub.lookup_node_token(hub, old_token)[0] is None
                    assert fleet_hub.lookup_node_token(hub, fresh_token)[0] == principal
            finally:
                release_body.set()
            stale_status, stale_payload = inflight.result(timeout=3)
        fresh_status, fresh = request(address, "POST", "/messages", fresh_token, raw)
        assert fresh_status == 200
        if read_action == "who":
            node = next(row for row in fresh["nodes"] if row["node_id"] == "node-b")
            assert node["keys"][0]["record"] == value
        else:
            assert [row["owner_node"] for row in fresh["claims"]] == ["node-b"]
        assert stale_status == 403, stale_payload
        assert stale_payload["code"] == "message_sender_inactive"
        assert "credential generation changed" in stale_payload["error"]


@pytest.mark.parametrize("principal", ["node-reader", "agent:reader"])
@pytest.mark.parametrize("read_action", ["who", "claims"])
def test_station_snapshot_retry_rechecks_generation_after_rollback(hub, monkeypatch, principal, read_action):
    value = record()
    publish(hub, value)
    for target in ("example-a", "example-b"):
        assert (
            fleet_hub.handle_claim(
                hub,
                {"action": "acquire", "target": target, "node_id": "node-b", "holder": "fixture"},
                caller_node="node-b",
            )[0]
            == 200
        )
    old_token, enrolled = enroll_reader(hub, principal)
    if enrolled:
        messages.handle_principal_admin(hub, {"action": "grants", "name": "reader", "may_send_to": ["node:*"]})
    original = messages._as_active_principal
    fresh_tokens = []

    def replace_after_rollback(conn, caller, operation, **kwargs):
        try:
            return original(conn, caller, operation, **kwargs)
        except paging.SnapshotWriteRequired:
            assert not conn.in_transaction
            assert not fresh_tokens, "expected one read-only attempt"
            fresh_tokens.append(replace_reader(conn, principal, enrolled))
            raise

    error = None
    with monkeypatch.context() as patch:
        patch.setattr(messages, "_as_active_principal", replace_after_rollback)
        try:
            messages.handle(
                hub,
                {"action": read_action, "limit": 1},
                caller=principal,
                authenticated_generation=fleet_hub.hash_node_token(old_token),
            )
        except messages.MessageAuthError as caught:
            error = caught
    assert len(fresh_tokens) == 1
    assert hub.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0, (
        "stale bearer created a snapshot"
    )
    assert not hub.in_transaction
    assert error is not None and "credential generation changed" in str(error)
    rows = []
    raw = {"action": read_action, "limit": 1}
    for _ in range(10):
        status, page = messages.handle(
            hub, raw, caller=principal, authenticated_generation=fleet_hub.hash_node_token(fresh_tokens[0])
        )
        assert status == 200
        rows.extend(page["nodes" if read_action == "who" else "claims"])
        if page["next_cursor"] is None:
            break
        raw["cursor"] = page["next_cursor"]
    else:
        pytest.fail("fresh generation pagination did not terminate")
    if read_action == "who":
        expected = ["node-b"] if enrolled else ["node-a", "node-b", "node-c", "node-reader"]
        assert [row["node_id"] for row in rows] == expected
        assert next(row for row in rows if row["node_id"] == "node-b")["keys"][0]["record"] == value
    else:
        assert [row["target"] for row in rows] == ["example-a", "example-b"]
    assert hub.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0


def test_identity_upload_requires_admin_and_matching_hub_id(hub):
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    _, token = fleet_hub.add_node(hub, "http-node")
    value = identity()
    with serving(path) as address:
        assert request(address, "POST", "/mesh/identity", token, value)[0] == 403
        assert (
            request(address, "POST", "/mesh/identity", "fixture-admin", {**value, "hub_id": "h_" + binary(bytes(16))})[
                0
            ]
            == 400
        )
        assert request(address, "POST", "/mesh/identity", "fixture-admin", value)[0] == 200
        assert request(address, "GET", "/mesh/identity") == (200, value)
        migration = {"old_hub_id": value["hub_id"], "new_hub_id": value["hub_id"], "sig": "opaque"}
        assert (
            request(address, "POST", "/mesh/identity", "fixture-admin", {"identity": value, "migration": migration})[0]
            == 200
        )
        assert request(address, "GET", "/mesh/identity") == (200, {"identity": value, "migration": migration})


def test_statement_sequence_and_opaque_roundtrip(hub):
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    _, token = fleet_hub.add_node(hub, "http-node")
    value = {"seq": 7, "sig": "opaque", "future": {"preserve": ["all", "fields"]}}
    with serving(path) as address:
        for kind in ("membership", "manifest", "revocation"):
            upload = {"kind": kind, "statement": value}
            assert request(address, "POST", "/mesh/statements", token, upload)[0] == 403
            assert request(address, "POST", "/mesh/statements", "fixture-admin", upload)[0] == 200
            assert request(address, "POST", "/mesh/statements", "fixture-admin", upload)[0] == 409
            assert (
                request(
                    address, "POST", "/mesh/statements", "fixture-admin", {"kind": kind, "statement": {"seq": True}}
                )[0]
                == 400
            )
        status, payload = request(address, "POST", "/messages", token, {"action": "manifests_get"})
        assert status == 200 and payload["statements"] == {
            kind: value for kind in ("membership", "manifest", "revocation")
        }
        for path in ("/mesh/identity", "/mesh/statements"):
            conn = http.client.HTTPConnection(*address, timeout=3)
            conn.putrequest("POST", path)
            conn.putheader("Content-Length", "999999999")
            conn.putheader("Authorization", "Bearer " + token)
            conn.endheaders()
            assert conn.getresponse().status == 403
            conn.close()


def test_schema_44_upgrade_preserves_cursor(hub):
    first = action(hub, "who", limit=1)[1]
    path = hub.execute("PRAGMA database_list").fetchone()[2]
    hub.execute("PRAGMA user_version=44")
    hub.execute("DROP TABLE fleet_message_keys")
    hub.execute("DROP TABLE fleet_mesh_statements")
    hub.execute("DROP TABLE hub_meta")
    hub.commit()
    for _ in range(2):
        conn = fleet_hub.init_db(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        conn.close()
    conn = fleet_hub.open_db(path)
    try:
        assert action(conn, "who", limit=1, cursor=first["next_cursor"])[1]["nodes"][0]["node_id"] == "node-b"
    finally:
        conn.close()


def test_hub_routes_without_cryptography(tmp_path):
    code = """
import sys
sys.modules['cryptography'] = None
sys.modules['brigade.fleet_mesh_crypto'] = None
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory
ns = runpy.run_path('tests/test_fleet_hub_message_keys.py')
from brigade import fleet_hub
with TemporaryDirectory() as directory:
    for index, name in enumerate(('test_publish_only_authenticated_principal', 'test_identity_upload_requires_admin_and_matching_hub_id', 'test_statement_sequence_and_opaque_roundtrip')):
        conn = fleet_hub.init_db(Path(directory) / f'hub-{index}.db')
        for node in ('node-a', 'node-b', 'node-c'):
            fleet_hub.add_node(conn, node)
        ns[name](conn)
        conn.close()
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_live_key_entries_stay_within_page_byte_cap(hub, monkeypatch):
    for index, name in enumerate(("node-a", "node-b", "node-c"), start=1):
        publish(hub, record(name, seed=index))
    # Ground the small fixture in one live decorated row, plus pager reserve.
    row_cost = paging._wire_size(action(hub, "who")[1]["nodes"][0])
    monkeypatch.setattr(paging, "PAGE_MAX_BYTES", 8192 + row_cost)
    assert row_cost <= paging.PAGE_MAX_BYTES - 8192 < 2 * row_cost
    first = action(hub, "who", limit=1)[1]
    args = {"cursor": first["next_cursor"]}
    pages = [first]
    while args["cursor"]:
        page = action(hub, "who", limit=500, **args)[1]
        pages.append(page)
        args["cursor"] = page["next_cursor"]
        assert len(pages) < 10
    assert len(pages) == 3
    assert [row["node_id"] for page in pages for row in page["nodes"]] == ["node-a", "node-b", "node-c"]
    for page in pages:
        assert (
            sum(paging._wire_size(row) for key in ("nodes", "principals", "sessions") for row in page[key])
            <= paging.PAGE_MAX_BYTES - 8192
        )
        assert len(json.dumps(page).encode()) < paging.PAGE_MAX_BYTES


@pytest.mark.parametrize("principal", ["node-race", "agent:peer"])
def test_key_publish_rechecks_authenticated_generation(hub, principal):
    if principal.startswith("agent:"):
        enrolled = messages.add_principal(hub, {"name": "peer"})
        old_token = enrolled["token"]
        assert messages.lookup_principal_token(hub, old_token) == principal
    else:
        _, old_token = fleet_hub.add_node(hub, principal)
        assert fleet_hub.lookup_node_token(hub, old_token)[0] == principal
    # Capture exactly what HTTP passes after authenticating, before body handling.
    old_generation = fleet_hub.hash_node_token(old_token)
    publish(hub, record(principal))
    if principal.startswith("agent:"):
        messages.revoke_principal(
            hub, {"action": "revoke", "name": "peer", "expected_enrollment_id": enrolled["enrollment_id"]}
        )
        fresh = messages.add_principal(hub, {"name": "peer", "expected_enrollment_id": enrolled["enrollment_id"]})
        fresh_token = fresh["token"]
        assert messages.lookup_principal_token(hub, old_token) is None
        assert messages.lookup_principal_token(hub, fresh_token) == principal
    else:
        fleet_hub.revoke_node(hub, principal)
        _, fresh_token = fleet_hub.add_node(hub, principal)
        assert fleet_hub.lookup_node_token(hub, old_token)[0] is None
        assert fleet_hub.lookup_node_token(hub, fresh_token)[0] == principal

    def authenticated_publish(value, generation):
        return messages.handle(
            hub, {"action": "keys_publish", "record": value}, caller=principal, authenticated_generation=generation
        )

    value = record(principal, seed=2)
    before = hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall()
    with pytest.raises(messages.MessageAuthError, match="credential generation changed"):
        authenticated_publish(value, old_generation)
    assert hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall() == before
    generation = fleet_hub.hash_node_token(fresh_token)
    assert generation != old_generation
    assert authenticated_publish(value, generation)[0] == 201
    replacement = record(principal, seed=3, prev_kid=value["kid"], prev_sig=binary(bytes(64)))
    before = hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall()
    with pytest.raises(messages.MessageAuthError, match="credential generation changed"):
        authenticated_publish(replacement, old_generation)
    assert hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall() == before
    assert authenticated_publish(replacement, generation)[0] == 201
    assert action(hub, "keys_get", kid=value["kid"])[1]["key"]["state"] == "retired"


def test_nested_key_pages_fit_actual_mcp_transport(tmp_path):
    from brigade import fleet_client_messages as client, fleet_hub_message_keys as keys, mcp_server
    from brigade.fleet_mcp import FleetMcp

    conn = fleet_hub.init_db(tmp_path / "nested-keys.db")
    try:
        nodes = [f"node-{index:04d}" for index in range(500)]
        principals = [f"agent:peer-{index:04d}" for index in range(500)]
        for node in nodes:
            fleet_hub.add_node(conn, node)
        for principal in principals:
            messages.add_principal(conn, {"name": principal.removeprefix("agent:")})
        for index, principal in enumerate(nodes + principals):
            seed = str(index).encode()
            sig, enc = hashlib.sha256(seed).digest(), hashlib.shake_256(seed).digest(1216)
            value = record(
                principal,
                sig_pub=binary(sig),
                enc_pub=binary(enc),
                kid="k1_" + binary(hashlib.sha256(b"brigade-mesh/v1/kid\0" + sig + enc).digest()[:16]),
                created_at="2026-10-22T12:00:00.123456+00:00",
                prev_kid="k1_" + binary(bytes(16)),
                prev_sig=binary(bytes(64)),
                transport={"iroh": "0" * 64},
            )
            keys.publish(conn, {"action": "keys_publish", "record": value}, caller=principal)
        conn.commit()
        station = FleetMcp()
        pages = []

        def fetch(body, **kwargs):
            page = action(
                conn, "who", **{key: value for key, value in body.items() if key != "action"}, caller=nodes[0]
            )[1]
            pages.append(dict(page))
            return page

        with (
            patch.object(station, "_authority", return_value=object()),
            patch.object(client, "_online", side_effect=fetch),
        ):
            arguments = {"limit": 500}
            while True:
                response = mcp_server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": len(pages) + 1,
                        "method": "tools/call",
                        "params": {"name": "fleet_who", "arguments": arguments},
                    },
                    **station.callbacks(),
                )
                assert "error" not in response
                assert len(json.dumps(response).encode()) < 8 * 1024 * 1024
                assert pages[-1]["oversized"] == 0
                cursor = pages[-1]["next_cursor"]
                if cursor is None:
                    break
                arguments["cursor"] = cursor
                assert len(pages) < 10
        assert len(pages) > 1
        assert [row["node_id"] for page in pages for row in page["nodes"]] == nodes
        assert [row["principal_id"] for page in pages for row in page["principals"]] == principals
        assert all(len(row["keys"]) == 1 for page in pages for group in ("nodes", "principals") for row in page[group])
    finally:
        conn.close()


@pytest.mark.parametrize(
    "created_at",
    [
        "2026-10-23T12:00:00+00:60",
        "2026-10-23T12:00:00-00:60",
        "2026-10-23T12:00:00+24:00",
        "2026-10-23T12:00:00-24:00",
        "2026-10-23T12:00:00+01:99",
        "2026-02-30T12:00:00Z",
    ],
)
def test_malformed_rotation_timestamp_preserves_all_rows(hub, created_at):
    old = record()
    publish(hub, old)
    before = hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall()
    successor = record(seed=2, prev_kid=old["kid"], prev_sig=binary(bytes(64)), created_at=created_at)
    with pytest.raises(messages.MessageError, match="invalid mesh timestamp"):
        publish(hub, successor)
    assert hub.execute("SELECT * FROM fleet_message_keys ORDER BY kid").fetchall() == before


@pytest.mark.parametrize("offset", ["Z", "+00:00", "-00:00", "+23:59", "-23:59", "+01:30"])
def test_valid_rotation_timestamp_offsets(hub, offset):
    old = record()
    publish(hub, old)
    successor = record(
        seed=2,
        prev_kid=old["kid"],
        prev_sig=binary(bytes(64)),
        created_at="2026-10-24T12:00:00" + offset,
    )
    assert publish(hub, successor)[0] == 201
    assert action(hub, "keys_get", kid=successor["kid"])[1]["key"]["record"] == successor
    assert action(hub, "keys_get", kid=old["kid"])[1]["key"]["state"] == "retired"


def test_statement_aggregate_wire_bound_is_atomic(hub):
    from brigade import fleet_client, fleet_client_messages as client, fleet_hub_identity as mesh

    statements = {kind: {"seq": 1, "opaque": "x" * 3_000_000} for kind in ("membership", "manifest")}
    for kind, statement in statements.items():
        mesh.upload_statement(hub, {"kind": kind, "statement": statement})
    # Default HTTP JSON escaping expands these opaque characters on the wire.
    candidate = {"seq": 1, "opaque": "\u2603" * 1000 + '"\\\n'}
    empty_response = {"statements": {**statements, "revocation": candidate}}
    padding = client.MESSAGE_RESPONSE_LIMIT - len(json.dumps(empty_response).encode())
    candidate["opaque"] += "x" * padding
    assert (
        len(json.dumps({"statements": {**statements, "revocation": candidate}}).encode())
        == client.MESSAGE_RESPONSE_LIMIT
    )

    path = hub.execute("PRAGMA database_list").fetchone()[2]
    _, token = fleet_hub.add_node(hub, "http-reader")
    with serving(path) as address:
        for seq in (1, 2):
            # seq=1 adds a kind, seq=2 replaces it. Both must roll back every column.
            before = hub.execute("SELECT * FROM fleet_mesh_statements ORDER BY kind").fetchall()
            overflow = {**candidate, "seq": seq, "opaque": candidate["opaque"] + "x"}
            assert (
                request(
                    address, "POST", "/mesh/statements", "fixture-admin", {"kind": "revocation", "statement": overflow}
                )[0]
                == 400
            )
            assert hub.execute("SELECT * FROM fleet_mesh_statements ORDER BY kind").fetchall() == before
            valid = {**candidate, "seq": seq}
            assert request(
                address, "POST", "/mesh/statements", "fixture-admin", {"kind": "revocation", "statement": valid}
            ) == (200, {"kind": "revocation", "seq": seq})
            connection = http.client.HTTPConnection(*address, timeout=3)
            try:
                connection.request(
                    "POST", "/messages", json.dumps({"action": "manifests_get"}), {"Authorization": "Bearer " + token}
                )
                response = connection.getresponse()
                assert response.status == 200
                body = fleet_client._read_bounded_response(response, limit=client.MESSAGE_RESPONSE_LIMIT)
                assert len(body) == client.MESSAGE_RESPONSE_LIMIT
                assert json.loads(body) == {"statements": {**statements, "revocation": valid}}
            finally:
                connection.close()
