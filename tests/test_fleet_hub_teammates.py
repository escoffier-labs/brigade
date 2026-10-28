"""Database-backed teammate authorization and transaction regressions."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from brigade import fleet_hub, fleet_hub_events as events, fleet_hub_message_grants as grants
from brigade import fleet_hub_messages as messages


pytest_plugins = ["tests._fleet_message_fixtures"]


def test_teammate_directory_exposes_public_key_fingerprint_without_key_material(mesh):
    from brigade.fleet_mesh_crypto import decode_binary, fingerprint
    from tests.test_fleet_hub_message_keys import publish, record

    conn, _ = mesh
    value = record("node-a", seed=1)
    publish(conn, value)
    page = messages.handle(conn, {"action": "who"}, caller="agent:guest")[1]
    node = next(row for row in page["nodes"] if row["node_id"] == "node-a")
    assert node["keys"] == [{"kid": value["kid"], "fingerprint": fingerprint(decode_binary(value["sig_pub"], 32))}]


@pytest.fixture
def mesh(tmp_path):
    path = tmp_path / "mesh.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "node-a")
    fleet_hub.add_node(conn, "node-b")
    messages.add_principal(conn, {"name": "peer", "may_send_to": ["agent:guest"]})
    messages.add_principal(conn, {"name": "guest", "may_send_to": ["agent:peer", "node-a"]})
    # Role assignment is server metadata, never an enrollment API extension.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fleet_message_principals)")}
    if "role" in columns:
        conn.execute("UPDATE fleet_message_principals SET role='teammate',person='Example' WHERE name='guest'")
        conn.commit()
    yield conn, path
    conn.close()


def _send(conn, sender, recipient):
    return messages.handle(conn, {"action": "send", "to": recipient, "subject": "Task", "body": "Work"}, caller=sender)


def _grant(conn, principal, patterns):
    target = {"name": principal[6:]} if principal.startswith("agent:") else {"node_id": principal}
    return grants.set_grants(conn, {"action": "grants", **target, "may_send_to": patterns})


def test_existing_principals_migrate_to_agent_and_unknown_callers_fail_closed(mesh):
    conn, _ = mesh
    assert grants.classify_principal(conn, "agent:peer") == "agent"
    assert grants.classify_principal(conn, "agent:guest") == "teammate"
    assert grants.classify_principal(conn, "node-a") == "node"
    for principal in ("unknown", "alien:peer", "missing", "agent:absent"):
        assert not grants.permits(conn, ["*"], principal, "node-a")
    fleet_hub.revoke_node(conn, "node-b")
    assert not grants.permits(conn, ["*"], "node-b", "node-a")


@pytest.mark.parametrize("peer", ["node-a", "agent:peer"])
@pytest.mark.parametrize("removed", ["peer", "guest"])
def test_reciprocal_grants_removing_either_side_blocks_both_directions(mesh, peer, removed):
    conn, _ = mesh
    _grant(conn, peer, ["agent:guest"])
    assert _send(conn, peer, "agent:guest")[0] == 201
    assert _send(conn, "agent:guest", peer)[0] == 201
    _grant(conn, peer if removed == "peer" else "agent:guest", [])
    for sender, recipient in ((peer, "agent:guest"), ("agent:guest", peer)):
        with pytest.raises(messages.MessageForbidden) as exc:
            _send(conn, sender, recipient)
        assert exc.value.code == "message_recipient_not_permitted"
    assert _send(conn, "node-a", "node-b")[0] == 201


@pytest.mark.parametrize(
    "sender,recipient,removed", [("node-a", "agent:guest", "agent:guest"), ("agent:guest", "node-a", "node-a")]
)
def test_waiting_send_rechecks_grants_after_obtaining_write_lock(mesh, sender, recipient, removed):
    conn, path = mesh
    _grant(conn, "node-a", ["agent:guest"])
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM fleet_message_send_grants WHERE principal_id=?", (removed,))
    waiting = threading.Event()

    def attempt():
        local = fleet_hub.open_db(path)
        local.set_trace_callback(lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None)
        try:
            with pytest.raises(messages.MessageForbidden) as exc:
                _send(local, sender, recipient)
            return exc.value.code
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(attempt)
        assert waiting.wait(5)
        conn.commit()
        assert future.result(timeout=10) == "message_recipient_not_permitted"


def test_node_grants_and_revocation_are_audited_and_atomic(mesh):
    conn, _ = mesh
    _grant(conn, "node-a", ["agent:guest"])
    assert conn.execute("SELECT action FROM fleet_audit_log ORDER BY id DESC LIMIT 1").fetchone() == ("grants.change",)
    conn.execute(
        "CREATE TRIGGER fail_audit BEFORE INSERT ON fleet_audit_log BEGIN SELECT RAISE(ABORT,'fixture audit failure'); END"
    )
    conn.commit()
    for operation in (lambda: _grant(conn, "node-a", []), lambda: fleet_hub.revoke_node(conn, "node-a")):
        with pytest.raises(sqlite3.IntegrityError, match="fixture audit failure"):
            operation()
        assert grants.grants_for(conn, "node-a") == ["agent:guest"]
        assert conn.execute("SELECT revoked_at FROM nodes WHERE node_id='node-a'").fetchone() == (None,)
    conn.execute("DROP TRIGGER fail_audit")
    conn.commit()
    fleet_hub.revoke_node(conn, "node-a")
    assert grants.grants_for(conn, "node-a") == []
    with pytest.raises(messages.MessageError):
        _grant(conn, "node-a", ["*"])
    with pytest.raises(messages.MessageError):
        _grant(conn, "absent", ["*"])


def test_teammate_claims_and_non_message_events_are_forbidden(mesh, monkeypatch):
    conn, _ = mesh
    with pytest.raises(messages.MessageForbidden) as exc:
        messages.handle(conn, {"action": "claims", "cursor": "invalid"}, caller="agent:guest")
    assert exc.value.code == "principal_role_forbidden"
    monkeypatch.setattr(events, "_CONFIG", events.EventsConfig(callback_hosts=frozenset({"callback.invalid"})))
    assert not events.permits(conn, "agent:guest", "node-a")
    for name in (events.CLAIM_EXPIRED, events.RUN_FAILED):
        status, result = events.handle(
            conn, {"action": "events_subscribe", "name": name, "arguments": {"node_id": "*"}}, caller="agent:guest"
        )
        assert status == 403 and result["code"] == "principal_role_forbidden"


def test_teammate_directory_is_reduced_and_one_way_key_reads_remain_allowed(mesh):
    from tests.test_fleet_hub_message_keys import publish, record

    conn, _ = mesh
    value = record("node-a", seed=1)
    publish(conn, value)
    # No reciprocal node grant: directory/key reads are deliberately one way.
    page = messages.handle(conn, {"action": "who", "limit": 1}, caller="agent:guest")[1]
    assert page["sessions"] == []
    assert {row["node_id"] for row in page["nodes"]} == {"node-a"}
    assert all(set(row) <= {"node_id", "principal_id", "label", "keys"} for row in page["nodes"] + page["principals"])
    assert messages.handle(conn, {"action": "keys_get", "kid": value["kid"]}, caller="agent:guest")[0] == 200


def test_principal_revocation_audit_failure_rolls_back(mesh):
    conn, _ = mesh
    principal = messages.get_principal(conn, {"action": "get", "name": "guest"})
    conn.execute(
        "CREATE TRIGGER fail_audit BEFORE INSERT ON fleet_audit_log BEGIN SELECT RAISE(ABORT,'fixture audit failure'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="fixture audit failure"):
        messages.revoke_principal(
            conn, {"action": "revoke", "name": "guest", "expected_enrollment_id": principal["enrollment_id"]}
        )
    assert conn.execute("SELECT revoked_at FROM fleet_message_principals WHERE name='guest'").fetchone() == (None,)


def test_audit_get_authenticates_admin_before_database_access(hub, monkeypatch):
    import json
    import urllib.error
    import urllib.request
    from brigade import fleet_hub_http
    from tests.test_fleet_messages import ADMIN

    base, node_token, _, _ = hub
    original = fleet_hub_http.open_db
    opened = []

    def observed(*args, **kwargs):
        opened.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(fleet_hub_http, "open_db", observed)
    for token, expected in ((None, 401), (node_token, 403), ("fixture-principal", 403), (ADMIN, 200)):
        opened.clear()
        request = urllib.request.Request(
            base + "/audit?limit=1", headers={} if token is None else {"Authorization": "Bearer " + token}
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            assert response.code == expected
            payload = json.load(response)
        assert bool(opened) == (expected == 200)
        if expected == 200:
            assert isinstance(payload["audit"], list)


def test_cli_node_grants_target_does_not_accept_a_credential(monkeypatch, capsys):
    from brigade import cli, fleet_client_messages

    calls = []

    def grant(name, patterns, *, node_id=None):
        calls.append((name, patterns, node_id))
        return {"principal": {"principal_id": node_id}}

    monkeypatch.setattr(fleet_client_messages, "set_principal_grants", grant)
    assert cli.main(["fleet", "msg", "principals", "grants", "--node", "node-a", "--clear", "--json"]) == 0
    assert calls == [(None, [], "node-a")]
    assert "node-a" in capsys.readouterr().out


@pytest.mark.parametrize("role", ["unsupported", "", "node"])
def test_unsupported_agent_roles_fail_closed(mesh, role):
    conn, _ = mesh
    conn.execute("UPDATE fleet_message_principals SET role=? WHERE name='guest'", (role,))
    conn.commit()
    assert grants.classify_principal(conn, "agent:guest") is None
    assert not grants.permits(conn, ["*"], "agent:guest", "node-a")
    with pytest.raises(messages.MessageAuthError):
        _send(conn, "agent:guest", "node-a")
