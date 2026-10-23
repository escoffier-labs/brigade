"""Sender acknowledgement visibility and body-free unread totals (#1715)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from brigade import cli, fleet_client, fleet_client_messages, fleet_hub_messages as messages
from tests.test_fleet_message_grants import enroll, send, set_grants
from tests.test_fleet_messages import NODE_A, NODE_B, _request
from tests.test_fleet_mcp import call, stations  # noqa: F401

pytest_plugins = ["tests._fleet_message_fixtures"]


@pytest.mark.parametrize("agent", [False, True], ids=["node", "agent"])
def test_count_tracks_ack_and_reply_without_reading_other_inboxes(hub, agent):
    base, token_a, token_b, _ = hub
    recipient = "agent:fixture" if agent else NODE_B
    recipient_token = enroll(base, may_send_to=[NODE_A])["token"] if agent else token_b
    assert _request(base, recipient_token, {"action": "count"}) == (200, {"unread_count": 0})
    first = send(base, token_a, recipient)[1]["message"]
    second = send(base, token_a, recipient)[1]["message"]
    assert _request(base, recipient_token, {"action": "count"}) == (200, {"unread_count": 2})
    assert _request(base, token_a, {"action": "count"}) == (200, {"unread_count": 0})
    assert _request(base, recipient_token, {"action": "ack", "id": first["id"]})[0] == 200
    assert _request(base, recipient_token, {"action": "count"}) == (200, {"unread_count": 1})
    assert _request(base, recipient_token, {"action": "reply", "id": second["id"], "body": "Done"})[0] == 201
    # Replying sends mail but leaves acknowledgement explicit.
    assert _request(base, recipient_token, {"action": "count"}) == (200, {"unread_count": 1})
    assert _request(base, token_a, {"action": "count"}) == (200, {"unread_count": 1})
    assert _request(base, recipient_token, {"action": "ack", "id": second["id"]})[0] == 200
    assert _request(base, recipient_token, {"action": "count"}) == (200, {"unread_count": 0})
    assert _request(base, recipient_token, {"action": "ack", "id": second["id"]})[0] == 200
    assert _request(base, recipient_token, {"action": "count"}) == (200, {"unread_count": 0})
    assert _request(base, recipient_token, {"action": "count", "recipient": NODE_A})[0] == 400


def test_count_is_read_only_body_free_unpaged_and_excludes_expired(hub):
    _, _, _, db = hub
    with sqlite3.connect(db) as conn:
        for index in range(102):
            messages.send(
                conn, {"to": NODE_B, "subject": "Task", "body": "Private", "client_id": str(index)}, sender=NODE_A
            )
        conn.execute(
            "UPDATE fleet_messages SET created_at=? WHERE client_id='0'",
            ((datetime.now(timezone.utc) - timedelta(days=messages.RETENTION_DAYS + 1)).isoformat(),),
        )
        conn.commit()

        def authorize(action, table, column, *_args):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table == "fleet_messages" and column in {"body", "subject"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        assert messages.handle(conn, {"action": "count"}, caller=NODE_B) == (200, {"unread_count": 101})
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 102


def test_agent_sent_ack_visibility_preserves_grants_and_sender_scope(hub):
    base, token_a, _, _ = hub
    principal = enroll(base, may_send_to=[NODE_A])
    outbound = send(base, principal["token"])[1]["message"]
    assert _request(base, principal["token"], {"action": "sent"})[1]["messages"][0]["acked_at"] is None
    acked = _request(base, token_a, {"action": "ack", "id": outbound["id"]})[1]["message"]
    set_grants(base, [])
    assert send(base, principal["token"])[0] == 403
    status, page = _request(base, principal["token"], {"action": "sent", "ids": [outbound["id"]]})
    assert status == 200
    assert page["messages"] == [acked]
    assert acked["acked_at"] is not None and acked["acked_by"] == NODE_A
    # The recipient can read its inbox, but cannot claim sender visibility.
    assert _request(base, token_a, {"action": "sent", "ids": [outbound["id"]]}) == (
        200,
        {"messages": [], "next_cursor": 0},
    )


@pytest.mark.parametrize("action", ["sent", "count"])
@pytest.mark.parametrize("agent", [False, True], ids=["node", "agent"])
def test_revoked_token_is_rejected_at_authentication(hub, action, agent):
    base, token_a, _, db = hub
    principal = enroll(base) if agent else None
    token = principal["token"] if principal else token_a
    with sqlite3.connect(db) as conn:
        table, field, identity = (
            ("fleet_message_principals", "principal_id", principal["principal_id"])
            if principal
            else ("nodes", "node_id", NODE_A)
        )
        conn.execute(
            f"UPDATE {table} SET revoked_at=? WHERE {field}=?", (datetime.now(timezone.utc).isoformat(), identity)
        )
    assert _request(base, token, {"action": action})[0] == 401


@pytest.mark.parametrize("action", ["sent", "count"])
@pytest.mark.parametrize("agent", [False, True], ids=["node", "agent"])
def test_revocation_between_authentication_and_query_is_forbidden(hub, monkeypatch, action, agent):
    base, token_a, _, _ = hub
    principal = enroll(base) if agent else None
    token = principal["token"] if principal else token_a
    original_handle = messages.handle

    def revoke_after_auth(conn, raw, *, caller, **kwargs):
        table, field = ("fleet_message_principals", "principal_id") if agent else ("nodes", "node_id")
        conn.execute(
            f"UPDATE {table} SET revoked_at=? WHERE {field}=?", (datetime.now(timezone.utc).isoformat(), caller)
        )
        conn.commit()
        return original_handle(conn, raw, caller=caller, **kwargs)

    monkeypatch.setattr(messages, "handle", revoke_after_auth)
    status, payload = _request(base, token, {"action": action})
    assert status == 403
    assert payload["code"] == "message_sender_inactive"


def test_mcp_count_only_and_cli_count_use_the_bound_credential(stations, capsys):  # noqa: F811
    a, b = stations
    call(a, "fleet_send", to=NODE_B, body="Unread")
    assert call(b, "fleet_inbox", count_only=True) == {"unread_count": 1}
    assert call(a, "fleet_inbox", count_only=True) == {"unread_count": 0}
    assert len(call(b, "fleet_inbox")["messages"]) == 1
    assert cli.main(["fleet", "msg", "count", "--token-file", b.token_file, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"unread_count": 1}
    assert cli.main(["fleet", "msg", "count", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"unread_count": 0}


@pytest.mark.parametrize("payload", [{}, {"unread_count": True}, {"unread_count": -1}, {"unread_count": "1"}])
def test_client_rejects_invalid_count_payload(stations, monkeypatch, payload):  # noqa: F811
    authority = stations[0]._authority()
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (200, payload))
    with pytest.raises(fleet_client.FleetClientError, match="invalid unread count"):
        fleet_client_messages.unread_count(authority=authority)
