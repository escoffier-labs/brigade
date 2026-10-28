"""Database-backed teammate authorization and transaction regressions."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from brigade import fleet_hub, fleet_hub_events as events, fleet_hub_message_grants as grants
from brigade import fleet_hub_messages as messages


pytest_plugins = ["tests._fleet_message_fixtures"]


def test_revoked_event_subscriber_retains_authentication_failure(mesh, monkeypatch):
    from tests.test_fleet_mcp_events import SECRET

    conn, _ = mesh
    principal = messages.get_principal(conn, {"action": "get", "name": "peer"})
    messages.revoke_principal(
        conn, {"action": "revoke", "name": "peer", "expected_enrollment_id": principal["enrollment_id"]}
    )
    monkeypatch.setattr(events, "_CONFIG", events.EventsConfig(callback_hosts=frozenset({"callback.invalid"})))
    with pytest.raises(messages.MessageAuthError):
        events.handle(
            conn,
            {
                "action": "events_subscribe",
                "name": events.MESSAGE_CREATED,
                "delivery": {"mode": "webhook", "url": "https://callback.invalid/events", "secret": SECRET},
            },
            caller="agent:peer",
        )


@pytest.mark.parametrize("caller", ["node-a", "agent:peer"])
@pytest.mark.parametrize("owner", ["node-b", "missing-node"])
def test_claim_snapshot_visibility_matches_legacy_reader_for_inactive_owners(mesh, caller, owner):
    from brigade import fleet_hub_paging as paging

    conn, _ = mesh
    if owner == "node-b":
        fleet_hub.revoke_node(conn, owner)
    _grant(conn, "agent:peer", [owner])
    first = paging.page(
        conn,
        {"action": "claims", "limit": 1},
        {"claims": [{"owner_node": owner, "target": f"fixture-{i}"} for i in range(2)]},
        caller=caller,
        limit=1,
        visible=lambda _kind, row: grants.permits(conn, [owner], caller, row["owner_node"]),
        snapshot_write=True,
    )
    assert first["total"] == 2
    conn.execute(f"UPDATE fleet_read_snapshot_rows SET visible={paging._VISIBLE_SQL}")
    continued = messages.handle(conn, {"action": "claims", "cursor": first["next_cursor"], "limit": 1}, caller=caller)[
        1
    ]
    assert continued["total"] == 2
    assert [row["target"] for row in continued["claims"]] == ["fixture-1"]


@pytest.mark.parametrize("principal", ["node-b", "agent:peer"])
def test_replayed_revocation_does_not_duplicate_removal_audit(mesh, principal):
    conn, _ = mesh
    if principal.startswith("agent:"):
        enrolled = messages.get_principal(conn, {"action": "get", "name": "peer"})

        def remove():
            return messages.revoke_principal(
                conn, {"action": "revoke", "name": "peer", "expected_enrollment_id": enrolled["enrollment_id"]}
            )
    else:

        def remove():
            return fleet_hub.revoke_node(conn, principal)

    remove()
    remove()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM fleet_audit_log WHERE action='member.remove' AND subject=?", (principal,)
        ).fetchone()[0]
        == 1
    )


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


@pytest.mark.parametrize("node_id", ["sk-gpu1", "hf_box", "npm_ci", "token.runner"])
def test_existing_public_node_identifiers_never_block_grants_or_revocation(mesh, node_id):
    conn, _ = mesh
    fleet_hub.add_node(conn, node_id)
    _grant(conn, node_id, ["agent:token.bot", "sk-future"])
    assert grants.grants_for(conn, node_id) == ["agent:token.bot", "sk-future"]
    fleet_hub.revoke_node(conn, node_id)
    assert grants.classify_principal(conn, node_id) is None
    assert grants.grants_for(conn, node_id) == []
    assert conn.execute("SELECT COUNT(*) FROM fleet_audit_log WHERE subject=?", (node_id,)).fetchone()[0] == 2


@pytest.mark.parametrize("name", ["token.bot", "secret.helper"])
def test_existing_public_agent_identifiers_never_block_grants_or_revocation(mesh, name):
    conn, _ = mesh
    enrolled = messages.add_principal(conn, {"name": name})
    _grant(conn, "agent:" + name, ["sk-future"])
    messages.revoke_principal(
        conn, {"action": "revoke", "name": name, "expected_enrollment_id": enrolled["enrollment_id"]}
    )
    assert grants.classify_principal(conn, "agent:" + name) is None


@pytest.mark.parametrize("target", ["missing", "peer"])
def test_agent_grant_management_preserves_missing_and_revoked_targets(mesh, target):
    conn, _ = mesh
    if target == "missing":
        assert _grant(conn, "agent:missing", []) is None
    else:
        enrolled = messages.get_principal(conn, {"action": "get", "name": target})
        messages.revoke_principal(
            conn, {"action": "revoke", "name": target, "expected_enrollment_id": enrolled["enrollment_id"]}
        )
        assert _grant(conn, "agent:peer", ["sk-future"])["may_send_to"] == ["sk-future"]
        assert grants.classify_principal(conn, "agent:peer") is None


@pytest.mark.parametrize("principal", ["sk-gpu1", "agent:token.bot"])
def test_known_public_identity_audit_context_does_not_mask_sqlite_failure(mesh, principal):
    conn, _ = mesh
    if principal.startswith("agent:"):
        enrolled = messages.add_principal(conn, {"name": principal[6:]})

        def remove():
            return messages.revoke_principal(
                conn, {"action": "revoke", "name": principal[6:], "expected_enrollment_id": enrolled["enrollment_id"]}
            )
    else:
        fleet_hub.add_node(conn, principal)

        def remove():
            return fleet_hub.revoke_node(conn, principal)

    conn.execute(
        "CREATE TRIGGER reject_known_audit BEFORE INSERT ON fleet_audit_log BEGIN SELECT RAISE(ABORT,'fixture audit storage failure'); END"
    )
    conn.commit()
    for operation in (lambda: _grant(conn, principal, ["hf_future"]), remove):
        with pytest.raises(sqlite3.IntegrityError, match="fixture audit storage failure"):
            operation()
        assert grants.classify_principal(conn, principal) in ("node", "agent")
        assert grants.grants_for(conn, principal) == []
    assert conn.execute("SELECT COUNT(*) FROM fleet_audit_log WHERE subject=?", (principal,)).fetchone()[0] == 0


@pytest.mark.parametrize(
    "principal,state,allowed",
    [
        ("node-a", "active", True),
        ("agent:peer", "agent", True),
        ("agent:peer", "teammate", True),
        ("agent:peer", "unsupported", False),
        ("agent:peer", "revoked", False),
        ("node-a", "revoked", False),
        ("missing-node", "unknown", False),
    ],
)
def test_existing_message_event_subscription_rechecks_supported_role(mesh, monkeypatch, principal, state, allowed):
    from brigade import fleet_hub_events_dispatch as dispatch

    conn, _ = mesh
    if state == "revoked":
        if principal.startswith("agent:"):
            conn.execute("UPDATE fleet_message_principals SET revoked_at=1 WHERE principal_id=?", (principal,))
        else:
            conn.execute("UPDATE nodes SET revoked_at=1 WHERE node_id=?", (principal,))
    elif principal.startswith("agent:"):
        conn.execute("UPDATE fleet_message_principals SET role=? WHERE principal_id=?", (state, principal))
    conn.commit()
    calls = []

    def permits(db, caller, subject):
        calls.append((db, caller, subject))
        return False

    monkeypatch.setattr(events, "permits", permits)
    row = {"principal": principal, "name": events.MESSAGE_CREATED, "subject": principal}
    assert dispatch._still_allowed(conn, row) is (True if allowed else None)
    row["subject"] = "another-inbox"
    assert dispatch._still_allowed(conn, row) is (False if allowed else None)
    assert calls == []
    row["name"] = "claim.created"
    assert dispatch._still_allowed(conn, row) is (False if allowed else None)
    assert calls == ([(conn, principal, "another-inbox")] if allowed else [])
