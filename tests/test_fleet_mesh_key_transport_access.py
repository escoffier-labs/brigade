"""Owner visibility must not grant send or directory permissions."""

import hashlib

import pytest

from brigade import fleet_hub
from brigade import fleet_hub_message_grants as grants
from brigade import fleet_hub_message_keys as keys
from brigade import fleet_hub_messages as messages


def record(principal):
    import base64

    def binary(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    sig, enc = bytes(32), bytes(1216)
    return {
        "v": 1,
        "principal": principal,
        "hub": "h_" + binary(bytes(16)),
        "kid": "k1_" + binary(hashlib.sha256(b"brigade-mesh/v1/kid\0" + sig + enc).digest()[:16]),
        "sig_pub": binary(sig),
        "enc_pub": binary(enc),
        "suites": ["bm1"],
        "created_at": "2026-01-02T00:00:00Z",
        "self_sig": binary(bytes(64)),
    }


@pytest.fixture
def conn(tmp_path):
    db = fleet_hub.init_db(tmp_path / "hub.db")
    fleet_hub.add_node(db, "node-a")
    messages.add_principal(db, {"name": "owner"})
    yield db
    db.close()


@pytest.mark.parametrize("role", ["node", "agent", "teammate"])
@pytest.mark.parametrize("state", ["active", "retired", "revoked"])
def test_owner_reads_public_history_without_self_grant(conn, role, state):
    caller = "node-a" if role == "node" else "agent:owner"
    if role == "teammate":
        conn.execute("UPDATE fleet_message_principals SET role='teammate'")
    value = record(caller)
    keys.publish(conn, {"action": "keys_publish", "record": value}, caller=caller)
    conn.execute("UPDATE fleet_message_keys SET state=?", (state,))
    assert keys.get(conn, {"action": "keys_get", "kid": value["kid"]}, caller=caller)["key"]["state"] == state
    if role != "node":
        assert not grants.permits(conn, ["*"], caller, caller)


@pytest.mark.parametrize("role", ["unknown", "revoked", "unsupported"])
def test_inactive_owner_has_no_exception(conn, role):
    caller = "agent:owner"
    value = record(caller)
    keys.publish(conn, {"action": "keys_publish", "record": value}, caller=caller)
    if role == "unknown":
        conn.execute("DELETE FROM fleet_message_principals")
    elif role == "revoked":
        conn.commit()
        messages.revoke_principal(
            conn,
            {
                "action": "revoke",
                "name": "owner",
                "expected_enrollment_id": conn.execute(
                    "SELECT enrollment_id FROM fleet_message_principals WHERE name='owner'"
                ).fetchone()[0],
            },
        )
    else:
        conn.execute("UPDATE fleet_message_principals SET role='unsupported'")
    assert keys.get(conn, {"action": "keys_get", "kid": value["kid"]}, caller=caller) is None


def test_nonowner_missing_and_generation_fence(conn):
    from brigade.fleet_hub_questions import _generation

    caller = "agent:owner"
    value = record("node-a")
    keys.publish(conn, {"action": "keys_publish", "record": value}, caller="node-a")
    assert keys.get(conn, {"action": "keys_get", "kid": value["kid"]}, caller=caller) is None
    with pytest.raises(messages.MessageError):
        keys.get(conn, {"action": "keys_get", "kid": "invalid"}, caller=caller)
    generation = _generation(conn, caller)
    own = record(caller)
    conn.execute("DELETE FROM fleet_message_keys")
    keys.publish(conn, {"action": "keys_publish", "record": own}, caller=caller)
    conn.commit()
    assert (
        messages.handle(
            conn, {"action": "keys_get", "kid": own["kid"]}, caller=caller, authenticated_generation=generation
        )[0]
        == 200
    )
    messages.revoke_principal(
        conn,
        {
            "action": "revoke",
            "name": "owner",
            "expected_enrollment_id": conn.execute(
                "SELECT enrollment_id FROM fleet_message_principals WHERE name='owner'"
            ).fetchone()[0],
        },
    )
    messages.add_principal(
        conn,
        {
            "name": "owner",
            "expected_enrollment_id": conn.execute(
                "SELECT enrollment_id FROM fleet_message_principals WHERE name='owner'"
            ).fetchone()[0],
        },
    )
    with pytest.raises(messages.MessageAuthError):
        messages.handle(
            conn, {"action": "keys_get", "kid": own["kid"]}, caller=caller, authenticated_generation=generation
        )
