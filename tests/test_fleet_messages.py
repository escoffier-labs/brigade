"""Hub-backed per-principal message queue (#1688)."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from io import BytesIO
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from brigade import cli, fleet_client, fleet_client_messages, fleet_hub, fleet_hub_enrollment, fleet_hub_messages
from brigade.cli import fleet_messages as fleet_messages_cli

NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
ADMIN = "admin-token-12345"


@pytest.fixture
def message_hub(tmp_path):
    db = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(db)
    try:
        _a, token_a = fleet_hub.add_node(conn, NODE_A)
        _b, token_b = fleet_hub.add_node(conn, NODE_B)
    finally:
        conn.close()
    server = fleet_hub.make_server("127.0.0.1", 0, db, ADMIN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", token_a, token_b, db
    server.shutdown()
    server.server_close()


def _request(base: str, token: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + "/messages",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(base: str, token: str, path: str) -> str:
    request = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def _post_path(base: str, token: str, path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_nodes_send_list_ack_and_reply(message_hub):
    base, token_a, token_b, _db = message_hub
    status, sent = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_B, "subject": "Review", "body": "Please review", "ref": "issue:1688"},
    )
    assert status == 201
    assert sent["message"]["sender"] == NODE_A

    status, inbox = _request(base, token_b, {"action": "list", "unacked": True, "after": 0})
    assert status == 200
    assert inbox["messages"] == [sent["message"]]
    assert inbox["next_cursor"] == sent["message"]["id"]
    status, acked = _request(base, token_b, {"action": "ack", "id": sent["message"]["id"]})
    assert (status, acked["message"]["acked_by"]) == (200, NODE_B)
    status, reply = _request(base, token_b, {"action": "reply", "id": sent["message"]["id"], "body": "Done"})
    assert status == 201
    assert reply["message"]["recipient"] == NODE_A
    assert reply["message"]["reply_to"] == sent["message"]["id"]


def test_future_message_timestamp_returns_retryable_error_code(message_hub):
    base, token_a, _token_b, _db = message_hub
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    status, payload = _request(
        base,
        token_a,
        {
            "action": "send",
            "to": NODE_B,
            "subject": "Clock skew",
            "body": "Retry after correcting the clock",
            "client_id": "future-clock-retry",
            "enqueued_at": future,
        },
    )

    assert status == 400
    assert payload["code"] == "message_timestamp_future"


def test_ack_requires_matching_response_message_id(monkeypatch):
    monkeypatch.setattr(fleet_client_messages, "_settings", lambda: ("https://hub.invalid", "token", NODE_A))
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (200, {}))

    with pytest.raises(fleet_client.FleetClientError, match="did not confirm the requested message"):
        fleet_client_messages.ack(42)

    for invalid_id in (True, 42.0):
        monkeypatch.setattr(
            fleet_client_messages,
            "_request",
            lambda *_args, invalid_id=invalid_id, **_kwargs: (200, {"message": {"id": invalid_id}}),
        )
        with pytest.raises(fleet_client.FleetClientError, match="did not confirm the requested message"):
            fleet_client_messages.ack(42)
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args, **_kwargs: (200, {"message": {"id": 42}}),
    )
    assert fleet_client_messages.ack(42)["message"]["id"] == 42


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"messages": {}, "next_cursor": 0},
        {"messages": [None], "next_cursor": 0},
        {"messages": [], "next_cursor": -1},
        {"messages": [], "next_cursor": True},
    ],
)
def test_message_list_rejects_invalid_success_payload(monkeypatch, payload):
    monkeypatch.setattr(fleet_client_messages, "_settings", lambda: ("https://hub.invalid", "token", NODE_A))
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (200, payload))

    with pytest.raises(fleet_client.FleetClientError, match="invalid message page"):
        fleet_client_messages.list_messages()


def test_recipient_scope_and_field_bounds(message_hub):
    base, token_a, token_b, _db = message_hub
    sent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "S", "body": "B"})[1]
    assert _request(base, token_a, {"action": "ack", "id": sent["message"]["id"]})[0] == 404
    assert _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "x" * 201, "body": "B"})[0] == 400
    assert _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "S", "body": "\ud800"})[0] == 400
    assert (
        _request(
            base,
            token_a,
            {"action": "send", "to": NODE_B, "subject": "S", "body": "B", "priority": []},
        )[0]
        == 400
    )
    too_large = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_B, "subject": "S", "body": "x" * (fleet_hub_messages.BODY_LIMIT + 1)},
    )
    assert too_large[0] == 400
    expired = _request(
        base,
        token_a,
        {
            "action": "send",
            "to": NODE_B,
            "subject": "Old",
            "body": "Too late",
            "client_id": "expired-request",
            "enqueued_at": (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(),
        },
    )
    assert expired[0] == 400
    assert "expired" in expired[1]["error"]
    out_of_range = _request(
        base,
        token_a,
        {
            "action": "send",
            "to": NODE_B,
            "subject": "Out of range",
            "body": "Invalid timestamp",
            "client_id": "out-of-range-timestamp",
            "enqueued_at": "0001-01-01T00:00:00+14:00",
        },
    )
    assert out_of_range[0] == 400


def test_message_pages_stay_below_client_response_limit(message_hub):
    base, token_a, token_b, _db = message_hub
    body = "\x01" * fleet_hub_messages.BODY_LIMIT
    for index in range(22):
        status, _ = _request(
            base,
            token_a,
            {"action": "send", "to": NODE_B, "subject": f"Large {index}", "body": body, "client_id": f"large-{index}"},
        )
        assert status == 201
    status, first = _request(base, token_b, {"action": "list"})
    assert status == 200
    assert len(json.dumps(first).encode("utf-8")) <= fleet_hub_messages.MAX_LIST_RESPONSE_BYTES
    assert len(first["messages"]) < 22
    status, second = _request(base, token_b, {"action": "list", "after": first["next_cursor"]})
    assert status == 200
    assert len(json.dumps(second).encode("utf-8")) <= fleet_hub_messages.MAX_LIST_RESPONSE_BYTES
    assert len(first["messages"]) + len(second["messages"]) == 22


def test_message_page_serialization_work_is_linear(message_hub, monkeypatch):
    _base, _token_a, _token_b, db = message_hub
    body = "x" * fleet_hub_messages.BODY_LIMIT
    with sqlite3.connect(db) as conn:
        for index in range(100):
            fleet_hub_messages.send(
                conn,
                {
                    "to": NODE_B,
                    "subject": f"Large {index}",
                    "body": body,
                    "client_id": f"linear-page-{index}",
                },
                sender=NODE_A,
            )

    original_dumps = json.dumps
    serialization_work = 0

    def measure_serialization(value, *args, **kwargs):
        nonlocal serialization_work
        serialized = original_dumps(value, *args, **kwargs)
        serialization_work += len(serialized.encode("utf-8"))
        return serialized

    monkeypatch.setattr(fleet_hub_messages.json, "dumps", measure_serialization)
    with sqlite3.connect(db) as conn:
        page = fleet_hub_messages.list_messages(conn, {"action": "list"}, recipient=NODE_B)

    response_size = len(original_dumps(page).encode("utf-8"))
    assert len(page["messages"]) == 100
    assert response_size <= fleet_hub_messages.MAX_LIST_RESPONSE_BYTES
    assert serialization_work <= response_size * 2


def test_retry_deduplication_quota_and_dashboard_unread_count(message_hub, monkeypatch):
    base, token_a, token_b, _db = message_hub
    first_body = {
        "action": "send",
        "to": NODE_B,
        "subject": "Retry-safe",
        "body": "One message",
        "client_id": "retry-key-1",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    first = _request(base, token_a, first_body)
    duplicate = _request(base, token_a, first_body)
    assert first[0] == duplicate[0] == 201
    assert first[1] == duplicate[1]
    mismatch = _request(base, token_a, {**first_body, "body": "Changed payload"})
    assert mismatch[0] == 409

    monkeypatch.setattr(fleet_hub_messages, "UNACKED_QUOTA", 1)
    rejected = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_B, "subject": "Over quota", "body": "Two", "client_id": "retry-key-2"},
    )
    assert rejected[0] == 429
    assert "quota" in rejected[1]["error"]

    page = _get(base, ADMIN, "/view/machines")
    assert "1 unread messages" in page
    assert _request(base, token_b, {"action": "ack", "id": first[1]["message"]["id"]})[0] == 200
    page = _get(base, ADMIN, "/view/machines")
    assert "1 unread messages" not in page
    monkeypatch.setattr(fleet_hub_messages, "SENDER_DAILY_QUOTA", 1)
    sender_limited = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_A, "subject": "Daily limit", "body": "Three", "client_id": "retry-key-3"},
    )
    assert sender_limited[0] == 429
    assert "sender" in sender_limited[1]["error"]


def test_dashboard_cookie_user_sees_unread_message_count(message_hub):
    base, token_a, _token_b, db = message_hub
    assert (
        _request(
            base,
            token_a,
            {"action": "send", "to": NODE_B, "subject": "Dashboard", "body": "Unread"},
        )[0]
        == 201
    )
    with sqlite3.connect(db) as conn:
        code = fleet_hub_enrollment.mint(conn)["code"]
        credential = fleet_hub_enrollment.redeem(conn, code)

    request = urllib.request.Request(
        base + "/view/machines",
        headers={"Cookie": f"{fleet_hub.DASHBOARD_COOKIE}={credential}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        page = response.read().decode("utf-8")
    assert "1 unread messages" in page


def test_revoked_recipient_exact_retry_returns_existing_before_active_check(message_hub):
    base, token_a, _token_b, db = message_hub
    body = {"action": "send", "to": NODE_B, "subject": "Retry", "body": "Accepted", "client_id": "revoked-retry"}
    first = _request(base, token_a, body)
    assert first[0] == 201
    conn = sqlite3.connect(db)
    conn.execute("UPDATE nodes SET revoked_at=? WHERE node_id=?", (datetime.now(timezone.utc).isoformat(), NODE_B))
    conn.commit()
    conn.close()
    retry = _request(base, token_a, body)
    assert retry == first
    rejected = _request(base, token_a, {**body, "client_id": "new-to-revoked"})
    assert rejected[0] == 400


def test_revoked_sender_is_rechecked_inside_message_transaction(message_hub):
    base, _token_a, token_b, db = message_hub
    incoming = _request(
        base,
        token_b,
        {"action": "send", "to": NODE_A, "subject": "Incoming", "body": "Before revoke", "client_id": "inbound"},
    )[1]
    message_id = incoming["message"]["id"]
    conn = sqlite3.connect(db)
    conn.execute("UPDATE nodes SET revoked_at=? WHERE node_id=?", (datetime.now(timezone.utc).isoformat(), NODE_A))
    conn.commit()
    requests = (
        {"action": "send", "to": NODE_B, "subject": "Late", "body": "Revoked", "client_id": "late-send"},
        {"action": "list"},
        {"action": "ack", "id": message_id},
        {"action": "reply", "id": message_id, "body": "Late reply"},
    )
    for request in requests:
        with pytest.raises(fleet_hub_messages.MessageAuthError, match="sender is not an active"):
            fleet_hub_messages.handle(conn, request, caller=NODE_A)
    assert conn.execute("SELECT acked_at FROM fleet_messages WHERE id=?", (message_id,)).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 1
    conn.close()


def test_client_enqueue_time_cannot_bypass_sender_daily_quota(message_hub, monkeypatch):
    base, token_a, _token_b, db = message_hub
    monkeypatch.setattr(fleet_hub_messages, "SENDER_DAILY_QUOTA", 1)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    first = _request(
        base,
        token_a,
        {
            "action": "send",
            "to": NODE_A,
            "subject": "Old timestamp",
            "body": "One",
            "client_id": "old-1",
            "enqueued_at": old,
        },
    )
    assert first[0] == 201
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE fleet_messages SET created_at=? WHERE client_id=?",
        ((datetime.now(timezone.utc) - timedelta(days=89)).isoformat(), "old-1"),
    )
    conn.commit()
    conn.close()
    rejected = _request(
        base,
        token_a,
        {
            "action": "send",
            "to": NODE_A,
            "subject": "Old timestamp",
            "body": "Two",
            "client_id": "old-2",
            "enqueued_at": old,
        },
    )
    assert rejected[0] == 429
    assert "sender" in rejected[1]["error"]


def test_expired_messages_are_pruned_before_read_ack_reply_and_unread_count(message_hub):
    base, token_a, token_b, db = message_hub
    sent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "Soon old", "body": "Expire me"})
    message_id = sent[1]["message"]["id"]
    # Insert an expired timestamp directly to exercise paths after the hub has started.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE fleet_messages SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(days=91)).isoformat(), message_id),
    )
    conn.commit()
    conn.close()
    assert _request(base, token_b, {"action": "list", "unacked": True})[1]["messages"] == []
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM fleet_messages WHERE id=?", (message_id,)).fetchone()[0] == 0
    conn.close()
    assert _request(base, token_b, {"action": "ack", "id": message_id})[0] == 404
    assert _request(base, token_b, {"action": "reply", "id": message_id, "body": "Too late"})[0] == 404
    conn = sqlite3.connect(db)
    status, unread_message = _request(
        base, token_a, {"action": "send", "to": NODE_B, "subject": "Unread", "body": "Expire me too"}
    )
    assert status == 201
    unread_id = unread_message["message"]["id"]
    conn.execute(
        "UPDATE fleet_messages SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(days=91)).isoformat(), unread_id),
    )
    conn.commit()
    assert fleet_hub_messages.unread_counts(conn) == {}
    conn.close()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM fleet_messages WHERE id=?", (unread_id,)).fetchone()[0] == 0
    conn.close()


def test_revoked_node_unread_messages_are_excluded_from_board_counts(message_hub):
    base, token_a, _token_b, db = message_hub
    status, _payload = _request(
        base, token_a, {"action": "send", "to": NODE_B, "subject": "Unread", "body": "Recipient revoked"}
    )
    assert status == 201
    conn = sqlite3.connect(db)
    fleet_hub.revoke_node(conn, NODE_B)
    assert fleet_hub_messages.unread_counts(conn) == {}
    conn.close()


def test_message_schema_indexes_retention_prune_columns(message_hub):
    _base, _token_a, _token_b, db = message_hub
    conn = sqlite3.connect(db)
    message_indexes = {
        tuple(column[2] for column in conn.execute(f'PRAGMA index_info("{row[1]}")'))
        for row in conn.execute("PRAGMA index_list(fleet_messages)")
    }
    send_indexes = {
        tuple(column[2] for column in conn.execute(f'PRAGMA index_info("{row[1]}")'))
        for row in conn.execute("PRAGMA index_list(fleet_message_sends)")
    }
    conn.close()

    assert ("created_at",) in message_indexes
    assert ("reply_to",) in message_indexes
    assert ("received_at",) in send_indexes


def test_expired_idempotent_retry_prunes_before_lookup(message_hub):
    base, token_a, _token_b, db = message_hub
    request = {"action": "send", "to": NODE_B, "subject": "Retry", "body": "Expire", "client_id": "expired-key"}
    assert _request(base, token_a, request)[0] == 201
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE fleet_messages SET created_at=? WHERE client_id=?",
        ((datetime.now(timezone.utc) - timedelta(days=91)).isoformat(), "expired-key"),
    )
    conn.commit()
    conn.close()
    assert _request(base, token_a, request)[0] == 201
    conn = sqlite3.connect(db)
    created_at = conn.execute("SELECT created_at FROM fleet_messages WHERE client_id=?", ("expired-key",)).fetchone()[0]
    assert datetime.fromisoformat(created_at) > datetime.now(timezone.utc) - timedelta(days=1)
    assert conn.execute("SELECT COUNT(*) FROM fleet_messages WHERE client_id=?", ("expired-key",)).fetchone()[0] == 1
    conn.close()


def test_expiry_prune_clears_reply_link_to_deleted_parent(message_hub):
    base, token_a, token_b, db = message_hub
    parent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "Parent", "body": "Old"})[1]
    parent_id = parent["message"]["id"]
    reply_request = {
        "action": "reply",
        "id": parent_id,
        "body": "Recent",
        "client_id": "reply-before-parent-expiry",
    }
    reply = _request(base, token_b, reply_request)[1]
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE fleet_messages SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(days=91)).isoformat(), parent_id),
    )
    conn.commit()
    conn.close()
    _request(base, token_b, {"action": "list"})
    replayed = _request(base, token_b, reply_request)
    assert replayed[0] == 201
    assert replayed[1]["message"]["id"] == reply["message"]["id"]
    inbox = _request(base, token_a, {"action": "list"})[1]
    assert inbox["messages"][0]["id"] == reply["message"]["id"]
    assert inbox["messages"][0]["reply_to"] is None


def test_default_reply_to_max_length_subject_stays_within_limit(message_hub):
    base, token_a, token_b, _db = message_hub
    parent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "S" * 200, "body": "Question"})[1]
    response = _request(base, token_b, {"action": "reply", "id": parent["message"]["id"], "body": "Answer"})
    assert response[0] == 201
    assert len(response[1]["message"]["subject"].encode("utf-8")) <= fleet_hub_messages.SUBJECT_LIMIT


def test_reply_idempotency_key_is_bound_to_original_message(message_hub):
    base, token_a, token_b, _db = message_hub
    first = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "First", "body": "One"})[1]
    second = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "Second", "body": "Two"})[1]
    reply_one = {
        "action": "reply",
        "id": first["message"]["id"],
        "subject": "Same reply",
        "body": "Same body",
        "client_id": "reply-key",
    }
    reply_two = {**reply_one, "id": second["message"]["id"]}
    assert _request(base, token_b, reply_one)[0] == 201
    conflict = _request(base, token_b, reply_two)
    assert conflict[0] == 409


def test_named_external_principal_uses_its_own_revocable_identity(message_hub):
    base, token_a, _token_b, _db = message_hub
    status, enrolled = _post_path(base, ADMIN, "/message-principals", {"action": "add", "name": "miller"})
    assert status == 201
    principal = enrolled["principal"]
    assert principal["principal_id"] == "agent:miller"
    token = principal["token"]

    sent = _request(
        base,
        token_a,
        {"action": "send", "to": "agent:miller", "subject": "Task", "body": "Review this", "client_id": "task-1"},
    )
    assert sent[0] == 201
    page = _get(base, ADMIN, "/view/machines")
    assert 'data-node="agent:miller"' not in page
    inbox = _request(base, token, {"action": "list", "unacked": True})
    assert inbox[0] == 200
    assert inbox[1]["messages"][0]["sender"] == NODE_A
    assert (
        _request(
            base,
            token,
            {"action": "send", "to": NODE_A, "subject": "Unsolicited", "body": "No", "client_id": "out-1"},
        )[0]
        == 403
    )
    replied = _request(base, token, {"action": "reply", "id": inbox[1]["messages"][0]["id"], "body": "Done"})
    assert replied[0] == 201
    assert replied[1]["message"]["sender"] == "agent:miller"

    revoked = _post_path(base, ADMIN, "/message-principals", {"action": "revoke", "name": "miller"})
    assert revoked[0] == 200
    assert _request(base, token, {"action": "list"})[0] == 401
    reissued = _post_path(base, ADMIN, "/message-principals", {"action": "add", "name": "miller"})
    assert reissued[0] == 201
    new_token = reissued[1]["principal"]["token"]
    assert _request(base, token, {"action": "list"})[0] == 401
    assert _request(base, new_token, {"action": "list"})[0] == 200


def test_principal_enrollment_replays_the_same_token_and_revocation_is_idempotent(message_hub):
    base, _token_a, _token_b, db = message_hub
    request = {
        "action": "add",
        "name": "recoverable",
        "request_id": "a" * 32,
        "token": "client-generated-token-for-retry",
    }
    first = _post_path(base, ADMIN, "/message-principals", request)
    replay = _post_path(base, ADMIN, "/message-principals", request)

    assert first[0] == replay[0] == 201
    assert first[1]["principal"] == replay[1]["principal"]
    assert first[1]["principal"]["token"] == request["token"]
    changed = _post_path(base, ADMIN, "/message-principals", {**request, "label": "changed"})
    assert changed[0] == 409

    revoke = {"action": "revoke", "name": "recoverable"}
    assert _post_path(base, ADMIN, "/message-principals", revoke)[0] == 200
    stale_replay = _post_path(base, ADMIN, "/message-principals", request)
    assert stale_replay[0] == 409
    assert stale_replay[1]["code"] == "principal_enrollment_revoked"
    assert _post_path(base, ADMIN, "/message-principals", revoke)[0] == 200
    assert _post_path(base, ADMIN, "/message-principals", {"action": "revoke", "name": "missing"})[0] == 404

    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT token_hash FROM fleet_message_principals WHERE name='recoverable'").fetchone()[0]
    assert stored != request["token"]


@pytest.mark.parametrize("token", ["a" * 31 + "\ud800", "a" * 31 + "\n", "é" * 32])
def test_principal_enrollment_rejects_tokens_unsafe_for_bearer_headers(message_hub, token):
    base, _token_a, _token_b, _db = message_hub
    status, payload = _post_path(
        base,
        ADMIN,
        "/message-principals",
        {"action": "add", "name": "unsafe-token", "request_id": "u" * 32, "token": token},
    )

    assert status == 400
    assert "token is invalid" in payload["error"]


@pytest.mark.parametrize("action", ["list", "ack"])
def test_online_message_operations_reject_redirects(action, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-token", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (302, {"error": "redirected"}))

    with pytest.raises(fleet_client.FleetClientError, match="redirected"):
        if action == "list":
            fleet_client_messages.list_messages()
        else:
            fleet_client_messages.ack(1)


def test_message_post_does_not_follow_same_origin_redirect():
    requests = []

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib handler API
            requests.append(self.command)
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802 - stdlib handler API
            requests.append(self.command)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = fleet_client_messages._request(
            f"http://127.0.0.1:{server.server_address[1]}", "node-token", {"action": "send"}, timeout=1
        )
    finally:
        server.shutdown()
        server.server_close()

    assert status == 302
    assert payload["error"] == "HTTP 302"
    assert requests == ["POST"]


@pytest.mark.parametrize("action", ["add", "revoke"])
def test_principal_management_rejects_redirects(action, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": "admin-token"},
    )

    class Response:
        status = 302

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(fleet_client, "_hub_open", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(fleet_client, "_read_bounded_response", lambda *_args, **_kwargs: b'{"error":"redirected"}')

    with pytest.raises(fleet_client.FleetClientError, match="redirected"):
        fleet_client_messages.manage_principal(action, name="researcher")


def test_principal_add_reuses_pending_request_after_ambiguous_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": "admin-token"},
    )
    requests = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def hub_open(request, **_kwargs):
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    monkeypatch.setattr(
        fleet_client,
        "_read_bounded_response",
        lambda *_args, **_kwargs: json.dumps(
            {
                "principal": {
                    "principal_id": "agent:researcher",
                    "name": "researcher",
                    "token": requests[-1]["token"],
                }
            }
        ).encode(),
    )
    calls = 0

    def run_with_deadline(request, *, timeout):
        nonlocal calls
        calls += 1
        result = request()
        if calls == 1:
            raise TimeoutError("response lost after commit")
        return result

    monkeypatch.setattr(fleet_client, "_run_with_deadline", run_with_deadline)
    with pytest.raises(fleet_client.FleetClientError, match="TimeoutError"):
        fleet_client_messages.manage_principal("add", name="researcher")

    stable_path = fleet_client_messages._principal_pending_path("researcher", "admin-token")
    legacy_path = stable_path.with_name(f"{NODE_A}.{stable_path.name}")
    stable_path.rename(legacy_path)
    # Recover retry state written by the older node-scoped format even after
    # the home node identity changes.
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_B)
    assert fleet_client_messages.manage_principal("add", name="researcher")["principal"]["token"]
    assert requests[0]["request_id"] == requests[1]["request_id"]
    assert requests[0]["token"] == requests[1]["token"]
    assert not stable_path.exists()
    assert fleet_client._read_spool(legacy_path) == []


def test_principal_add_keeps_retry_state_until_response_matches_request(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": "admin-token"},
    )
    requests = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def hub_open(request, **_kwargs):
        requests.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    monkeypatch.setattr(fleet_client, "_read_bounded_response", lambda *_args, **_kwargs: b"{}")

    with pytest.raises(fleet_client.FleetClientError, match="did not confirm the enrolled principal"):
        fleet_client_messages.manage_principal("add", name="researcher")
    path = fleet_client_messages._principal_pending_path("researcher", "admin-token")
    assert fleet_client._read_spool(path)[0]["request_id"] == requests[0]["request_id"]

    monkeypatch.setattr(
        fleet_client,
        "_read_bounded_response",
        lambda *_args, **_kwargs: json.dumps(
            {"principal": {"principal_id": "agent:researcher", "name": "researcher", "token": "é"}}
        ).encode(),
    )
    with pytest.raises(fleet_client.FleetClientError, match="did not confirm the enrolled principal"):
        fleet_client_messages.manage_principal("add", name="researcher")
    assert fleet_client._read_spool(path)[0]["request_id"] == requests[0]["request_id"]

    monkeypatch.setattr(
        fleet_client,
        "_read_bounded_response",
        lambda *_args, **_kwargs: json.dumps(
            {
                "principal": {
                    "principal_id": "agent:researcher",
                    "name": "researcher",
                    "token": requests[-1]["token"],
                }
            }
        ).encode(),
    )
    assert fleet_client_messages.manage_principal("add", name="researcher")["principal"]["token"]
    assert requests[0]["request_id"] == requests[1]["request_id"]
    assert not path.exists()


def test_unacked_recipient_index_supports_count_queries(message_hub):
    _base, _token_a, _token_b, db = message_hub
    with sqlite3.connect(db) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(fleet_messages)")}
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM fleet_messages WHERE recipient=? AND acked_at IS NULL",
                (NODE_A,),
            )
        )
    assert "fleet_messages_unacked_recipient" in indexes
    assert "fleet_messages_unacked_recipient" in plan


def test_schema_v28_migrates_principal_retry_state_and_unacked_index(tmp_path):
    db = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(db)
    conn.execute("ALTER TABLE fleet_message_principals DROP COLUMN enrollment_id")
    conn.execute("DROP INDEX fleet_messages_unacked_recipient")
    conn.execute("PRAGMA user_version=28")
    conn.commit()
    conn.close()

    migrated = fleet_hub.init_db(db)
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 29
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(fleet_message_principals)")}
        indexes = {row[1] for row in migrated.execute("PRAGMA index_list(fleet_messages)")}
        assert "enrollment_id" in columns
        assert "fleet_messages_unacked_recipient" in indexes
        status, payload = fleet_hub_messages.handle_principal_admin(
            migrated,
            {"action": "add", "name": "migrated", "request_id": "m" * 32, "token": "t" * 40},
        )
        assert status == 201
        assert payload["principal"]["token"] == "t" * 40
    finally:
        migrated.close()


def test_offline_send_is_spooled_and_delivered_by_flush(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    attempts = []

    def request(_hub: str, _token: str, body: dict):
        attempts.append(body)
        if len(attempts) == 1:
            return 503, {"error": "temporarily unavailable"}
        if len(attempts) == 2:
            return 429, {"error": "quota full"}
        return 201, {"message": {"client_id": body["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    queued = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Offline",
        body="Queue me",
        reference=None,
        priority="normal",
    )
    assert queued["queued"] is True
    assert fleet_client_messages.flush(NODE_A) == 0
    assert fleet_client_messages._spool_path(NODE_A).exists()
    assert fleet_client_messages.flush(NODE_A) == 1
    assert len({attempt["client_id"] for attempt in attempts}) == 1
    assert not fleet_client_messages._spool_path(NODE_A).exists()


@pytest.mark.parametrize("status", [302, 408, 425, 429, 503])
def test_retryable_send_is_spooled_instead_of_reported_delivered(status, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args, **_kwargs: (status, {"error": "retry later"}),
    )

    result = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Keep for retry",
        body="A retryable response is not delivery",
        reference=None,
        priority="normal",
    )

    assert result["queued"] is True
    queued = fleet_client._read_spool(fleet_client_messages._spool_path(NODE_A, "node-secret"))
    assert len(queued) == 1
    assert queued[0]["subject"] == "Keep for retry"
    assert queued[0]["client_id"] == result["client_id"]


def test_send_spools_success_response_without_matching_client_id(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (201, {}))

    result = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Keep after invalid response",
        body="A 2xx response must confirm delivery",
        reference=None,
        priority="normal",
    )

    path = fleet_client_messages._spool_path(NODE_A, "node-secret")
    queued = fleet_client._read_spool(path)
    assert result["queued"] is True
    assert len(queued) == 1
    assert queued[0]["client_id"] == result["client_id"]


@pytest.mark.parametrize("status", [302, 408, 425, 429, 503])
def test_retryable_spool_flush_keeps_message_queued(status, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(NODE_A, "node-secret")
    message = {
        "action": "send",
        "to": NODE_B,
        "subject": "Keep for retry",
        "body": "A retryable response is not delivery",
        "client_id": "retryable-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args, **_kwargs: (status, {"error": "retry later"}),
    )

    assert fleet_client_messages.flush(NODE_A) == 0
    assert fleet_client._read_spool(path) == [message]


def test_spool_flush_keeps_message_when_success_response_does_not_confirm_client_id(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(NODE_A, "node-secret")
    message = {
        "action": "send",
        "to": NODE_B,
        "client_id": "queued-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (201, {}))

    assert fleet_client_messages.flush(NODE_A) == 0
    assert fleet_client._read_spool(path) == [message]


def test_spool_flush_drops_forbidden_send_and_delivers_next_message(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(NODE_A, "node-secret")
    enqueued_at = datetime.now(timezone.utc).isoformat()
    forbidden = {"action": "send", "client_id": "forbidden", "enqueued_at": enqueued_at}
    reply = {"action": "reply", "client_id": "valid-reply", "enqueued_at": enqueued_at}
    with fleet_client._spool_lock(path) as dir_fd:
        for message in (forbidden, reply):
            assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, **_kwargs):
        attempts.append(message["client_id"])
        return (
            (403, {"error": "external principals may only reply"})
            if len(attempts) == 1
            else (201, {"message": {"client_id": message["client_id"]}})
        )

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    assert fleet_client_messages.flush(NODE_A) == 1
    assert attempts == ["forbidden", "valid-reply"]
    assert not path.exists()


def test_queued_messages_are_not_replayed_with_replaced_node_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    token = "node-token-a"
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": token, "admin_token": ""},
    )
    attempts = []

    def request(_hub: str, request_token: str, message: dict):
        attempts.append((request_token, message.copy()))
        if len(attempts) == 1:
            return 503, {"error": "temporarily unavailable"}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    queued = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Offline under first credential",
        body="Keep the auth scope",
        reference=None,
        priority="normal",
    )
    first_path = fleet_client_messages._spool_path(NODE_A)
    assert queued["queued"] is True
    assert first_path.is_file()

    token = "node-token-b"
    sent = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Online under replacement credential",
        body="Do not replay the older request",
        reference=None,
        priority="normal",
    )

    assert sent["message"]["client_id"] == attempts[-1][1]["client_id"]
    assert attempts[-1][0] == "node-token-b"
    assert all(request_token != "node-token-b" for request_token, _message in attempts[:-1])
    assert first_path.is_file()


def test_flush_reports_legacy_unscoped_message_spool(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    legacy_path = fleet_client.spool_path(NODE_A).with_name(f"{NODE_A}.messages.jsonl")
    legacy_message = {
        "action": "send",
        "to": NODE_B,
        "subject": "Legacy",
        "body": "Keep this queued for operator review",
        "client_id": "legacy-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps(legacy_message) + "\n", encoding="utf-8")
    attempts = []
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda _hub, _token, message: attempts.append(message) or (201, {}),
    )

    assert fleet_client_messages.flush(NODE_A) == 0

    assert attempts == []
    assert fleet_client._read_spool(legacy_path) == [legacy_message]
    assert "no credential scope" in caplog.text


def test_flush_reports_spools_scoped_to_replaced_credentials(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-token-b", "admin_token": ""},
    )
    old_path = fleet_client_messages._spool_path(NODE_A, "node-token-a")
    old_message = {
        "action": "send",
        "to": NODE_B,
        "subject": "Old credential",
        "body": "Keep this queued for its original sender",
        "client_id": "old-credential-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(old_path) as dir_fd:
        assert fleet_client._spool_append_locked(old_path, old_message, dir_fd=dir_fd)
    attempts = []
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda _hub, _token, message: attempts.append(message) or (201, {}),
    )

    assert fleet_client_messages.flush(NODE_A) == 0

    assert attempts == []
    assert fleet_client._read_spool(old_path) == [old_message]
    assert "other credentials" in caplog.text


def test_message_request_uses_hard_wall_clock_deadline(monkeypatch):
    deadlines = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(fleet_client, "_hub_open", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(fleet_client, "_read_bounded_response", lambda *_args, **_kwargs: b"{}")

    def run_with_deadline(_fn, *, timeout):
        deadlines.append(timeout)
        raise TimeoutError("message request hard deadline")

    monkeypatch.setattr(fleet_client, "_run_with_deadline", run_with_deadline)

    with pytest.raises(fleet_client.FleetClientError, match="TimeoutError"):
        fleet_client_messages._request("https://hub.invalid", "node-token", {"action": "list"})

    assert deadlines == [fleet_client.FLUSH_TIMEOUT_SECONDS]


def test_principal_management_uses_hard_wall_clock_deadline(monkeypatch):
    deadlines = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": "admin-token"},
    )
    monkeypatch.setattr(fleet_client, "_hub_open", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(fleet_client, "_read_bounded_response", lambda *_args, **_kwargs: b"{}")

    def run_with_deadline(_fn, *, timeout):
        deadlines.append(timeout)
        raise TimeoutError("principal request hard deadline")

    monkeypatch.setattr(fleet_client, "_run_with_deadline", run_with_deadline)

    with pytest.raises(fleet_client.FleetClientError, match="TimeoutError"):
        fleet_client_messages.manage_principal("add", name="researcher")

    assert deadlines == [fleet_client.FLUSH_TIMEOUT_SECONDS]


def test_spool_flush_obeys_aggregate_wall_clock_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    path = fleet_client_messages._spool_path(NODE_A, "node-token")
    now = datetime.now(timezone.utc).isoformat()
    with fleet_client._spool_lock(path) as dir_fd:
        for index in range(4):
            assert fleet_client._spool_append_locked(
                path,
                {
                    "action": "send",
                    "to": NODE_B,
                    "subject": f"Queued {index}",
                    "body": "bounded flush",
                    "client_id": f"budget-{index}",
                    "enqueued_at": now,
                },
                dir_fd=dir_fd,
            )
    clock = [0.0]
    monkeypatch.setattr(fleet_client_messages, "monotonic", lambda: clock[0], raising=False)
    timeouts = []

    def request(_hub, _token, message, *, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS):
        timeouts.append(timeout)
        clock[0] += timeout
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    delivered = fleet_client_messages._flush_locked(path, "https://hub.invalid", "node-token")

    assert timeouts == [5.0, 5.0, 2.0]
    assert delivered == 3
    assert [message["client_id"] for message in fleet_client._read_spool(path)] == ["budget-3"]


def test_new_send_flushes_older_spooled_messages_first(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    attempts = []
    offline = True

    def request(_hub: str, _token: str, message: dict):
        nonlocal offline
        attempts.append(message)
        if offline:
            return 503, {"error": "temporarily unavailable"}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    older = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Older",
        body="Queued first",
        reference=None,
        priority="normal",
    )
    assert older["queued"] is True

    pending = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Newer",
        body="Sent second",
        reference=None,
        priority="normal",
    )
    assert pending["queued"] is True
    assert [message["subject"] for message in attempts[1:]] == ["Older"]

    offline = False
    newest = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Newest",
        body="Sent last",
        reference=None,
        priority="normal",
    )

    assert newest["message"]["client_id"] == attempts[-1]["client_id"]
    assert [message["subject"] for message in attempts[2:]] == ["Older", "Newer", "Newest"]


@pytest.mark.parametrize("action", ["send", "reply"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_spool_drain_preserves_new_message(tmp_path, monkeypatch, action, interrupt):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    attempts = []

    def request(_hub: str, _token: str, message: dict):
        attempts.append(message.copy())
        if len(attempts) == 1:
            return 503, {"error": "temporarily unavailable"}
        raise interrupt()

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    older = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Older",
        body="Already queued",
        reference=None,
        priority="normal",
    )
    assert older["queued"] is True

    with pytest.raises(interrupt):
        if action == "send":
            fleet_client_messages.send(
                recipient=NODE_B,
                subject="Newer",
                body="Preserve through interrupted backlog drain",
                reference=None,
                priority="normal",
            )
        else:
            fleet_client_messages.reply(
                17,
                body="Preserve through interrupted backlog drain",
                subject="Re: task",
                reference=None,
                priority="normal",
            )

    pending = fleet_client._read_spool(fleet_client_messages._spool_path(NODE_A))
    assert len(pending) == 2
    assert pending[0]["client_id"] == older["client_id"]
    assert pending[1]["action"] == action


def test_reply_flushes_older_spooled_messages_first(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    attempts = []
    offline = True

    def request(_hub: str, _token: str, message: dict):
        attempts.append(message)
        if offline:
            return 503, {"error": "temporarily unavailable"}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    queued = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Older",
        body="Queued first",
        reference=None,
        priority="normal",
    )
    assert queued["queued"] is True

    offline = False
    replied = fleet_client_messages.reply(
        17, body="Reply second", subject="Re: task", reference=None, priority="normal"
    )

    assert replied["message"]["client_id"] == attempts[-1]["client_id"]
    assert [message["action"] for message in attempts[1:]] == ["send", "reply"]


def test_new_send_bounds_opportunistic_spool_drain(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    spool = fleet_client_messages._spool_path(NODE_A)
    now = datetime.now(timezone.utc).isoformat()
    with fleet_client._spool_lock(spool) as dir_fd:
        for index in range(fleet_client_messages.MESSAGE_BATCH_SIZE + 1):
            assert fleet_client._spool_append_locked(
                spool,
                {
                    "action": "send",
                    "to": NODE_B,
                    "subject": f"Queued {index}",
                    "body": "Older request",
                    "client_id": f"queued-{index}",
                    "enqueued_at": now,
                },
                dir_fd=dir_fd,
            )

    attempts = []
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda _hub, _token, message: (
            attempts.append(message) or (201, {"message": {"client_id": message["client_id"]}})
        ),
    )
    result = fleet_client_messages.send(
        recipient=NODE_B,
        subject="New request",
        body="Keep this behind the backlog",
        reference=None,
        priority="normal",
    )

    assert result["queued"] is True
    assert len(attempts) == fleet_client_messages.MESSAGE_BATCH_SIZE
    pending = fleet_client._read_spool(spool)
    assert [message["subject"] for message in pending] == ["Queued 100", "New request"]
    assert fleet_client_messages.flush(NODE_A) == 2
    assert [message["subject"] for message in attempts[-2:]] == ["Queued 100", "New request"]


def test_future_timestamp_rejection_keeps_queued_message_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    attempts = []
    clock_skewed = True

    def request(_hub: str, _token: str, message: dict):
        attempts.append(message.copy())
        if clock_skewed:
            return 400, {"code": "message_timestamp_future", "error": "enqueued_at is too far in the future"}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    queued = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Clock skew",
        body="Retry when the hub clock accepts this timestamp",
        reference=None,
        priority="normal",
    )
    assert queued["queued"] is True
    spool = fleet_client_messages._spool_path(NODE_A)
    assert fleet_client._read_spool(spool) == [attempts[0]]

    clock_skewed = False
    assert fleet_client_messages.flush(NODE_A) == 1
    assert attempts[1] == attempts[0]
    assert fleet_client._read_spool(spool) == []


@pytest.mark.parametrize("action", ["send", "reply"])
@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupted_send_is_spooled_before_reraising(tmp_path, monkeypatch, action, interrupt):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    attempted = []

    def interrupted_request(_hub: str, _token: str, message: dict):
        attempted.append(message.copy())
        raise interrupt()

    monkeypatch.setattr(fleet_client_messages, "_request", interrupted_request)
    with pytest.raises(interrupt):
        if action == "send":
            fleet_client_messages.send(
                recipient=NODE_B,
                subject="Interrupted",
                body="Preserve this request",
                reference=None,
                priority="normal",
            )
        else:
            fleet_client_messages.reply(
                17, body="Preserve this request", subject="Re: interrupted", reference=None, priority="normal"
            )

    spool = fleet_client_messages._spool_path(NODE_A)
    assert fleet_client._read_spool(spool) == attempted


def test_ambiguous_reply_is_spooled_and_retried_with_same_client_id(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    attempts = []

    def request(_hub: str, _token: str, message: dict):
        attempts.append(message)
        if len(attempts) == 1:
            raise fleet_client.FleetClientError("response lost after hub commit")
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    queued = fleet_client_messages.reply(17, body="Done", subject="Re: task", reference=None, priority="normal")

    assert queued["queued"] is True
    assert queued["client_id"] == attempts[0]["client_id"]
    assert fleet_client_messages.flush(NODE_A) == 1
    assert attempts[1] == attempts[0]


@pytest.mark.parametrize(
    ("message_id", "body", "subject", "reference", "priority", "error"),
    [
        (17, "x" * (fleet_hub_messages.BODY_LIMIT + 1), None, None, "normal", "body"),
        (17, "Done", "   ", None, "normal", "subject"),
        (17, "Done", "x" * (fleet_hub_messages.SUBJECT_LIMIT + 1), None, "normal", "subject"),
        (17, "Done", None, "x" * (fleet_hub_messages.REF_LIMIT + 1), "normal", "ref"),
        (17, "Done", None, None, "critical", "priority"),
        (0, "Done", None, None, "normal", "message id"),
        (2**63, "Done", None, None, "normal", "message id"),
    ],
)
def test_invalid_offline_reply_is_not_claimed_queued(
    tmp_path, monkeypatch, message_id, body, subject, reference, priority, error
):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args: (_ for _ in ()).throw(AssertionError("invalid reply reached transport")),
    )

    with pytest.raises(fleet_client.FleetClientError, match=error):
        fleet_client_messages.reply(
            message_id,
            body=body,
            subject=subject,
            reference=reference,
            priority=priority,
        )
    assert not fleet_client_messages._spool_path(NODE_A).exists()


def test_reply_preserves_enqueue_time_and_rejects_expired_delivery(message_hub):
    base, token_a, token_b, _db = message_hub
    sent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "Task", "body": "Review"})[1]
    enqueued_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    delivered = _request(
        base,
        token_b,
        {
            "action": "reply",
            "id": sent["message"]["id"],
            "body": "Done",
            "client_id": "delayed-reply",
            "enqueued_at": enqueued_at,
        },
    )

    assert delivered[0] == 201
    assert delivered[1]["message"]["created_at"] == enqueued_at
    expired = _request(
        base,
        token_b,
        {
            "action": "reply",
            "id": sent["message"]["id"],
            "body": "Too late",
            "client_id": "expired-reply",
            "enqueued_at": (
                datetime.now(timezone.utc) - timedelta(days=fleet_hub_messages.RETENTION_DAYS + 1)
            ).isoformat(),
        },
    )
    assert expired[0] == 400
    assert "expired" in expired[1]["error"]


def test_hub_rejects_ids_outside_sqlite_integer_range(message_hub):
    base, token_a, token_b, _db = message_hub
    sent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "Task", "body": "Review"})[1]
    assert sent["message"]["id"] < 2**63

    status, error = _request(base, token_b, {"action": "reply", "id": 2**63, "body": "Invalid"})

    assert status == 400
    assert "SQLite's range" in error["error"]

    ack_status, ack_error = _request(base, token_b, {"action": "ack", "id": 2**63})
    assert ack_status == 400
    assert "SQLite's range" in ack_error["error"]

    list_status, list_error = _request(base, token_b, {"action": "list", "after": 2**63})
    assert list_status == 400
    assert "SQLite's range" in list_error["error"]


@pytest.mark.parametrize(("field", "value"), [("subject", 0), ("ref", [])])
def test_hub_rejects_falsy_non_string_reply_fields(message_hub, field, value):
    base, token_a, token_b, _db = message_hub
    sent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "Task", "body": "Review"})[1]
    status, error = _request(
        base,
        token_b,
        {"action": "reply", "id": sent["message"]["id"], "body": "Done", field: value},
    )

    assert status == 400
    assert field in error["error"]


def test_client_rejects_ids_outside_sqlite_integer_range_before_transport(monkeypatch):
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args: (_ for _ in ()).throw(AssertionError("invalid ID reached transport")),
    )
    with pytest.raises(fleet_client.FleetClientError, match="SQLite's range"):
        fleet_client_messages.ack(2**63)
    with pytest.raises(fleet_client.FleetClientError, match="SQLite's range"):
        fleet_client_messages.list_messages(after=2**63)


@pytest.mark.parametrize("command", ["ack", "reply"])
def test_msg_ack_and_reply_escape_subject_control_characters(command, tmp_path, monkeypatch, capsys):
    result = {"message": {"id": 7, "recipient": "node-a", "subject": "task\x1b[31mred"}}
    monkeypatch.setattr(fleet_client_messages, "ack", lambda _message_id: result)
    monkeypatch.setattr(fleet_client_messages, "reply", lambda *_args, **_kwargs: result)
    argv = ["fleet", "msg", command, "7"]
    if command == "reply":
        body_file = tmp_path / "reply.txt"
        body_file.write_text("Done", encoding="utf-8")
        argv.extend(("--body-file", str(body_file)))

    assert cli.main(argv) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\\x1b[31m" in output


def test_msg_errors_escape_hub_control_characters(capsys):
    def rejected():
        raise fleet_client.FleetClientError("hub rejected\x1b[31mmessage")

    args = type("Args", (), {"json": False})()
    assert fleet_messages_cli._result(args, rejected) == 1

    output = capsys.readouterr().err
    assert "\x1b" not in output
    assert "\\x1b[31m" in output


def test_spool_rejection_log_escapes_hub_control_characters(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    path = fleet_client_messages._spool_path(NODE_A, "node-token")
    message = {
        "action": "send",
        "to": NODE_B,
        "subject": "Rejected",
        "body": "Keep errors safe in logs",
        "client_id": "unsafe-error",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args, **_kwargs: (400, {"error": "rejected\x1b[31mmessage"}),
    )

    fleet_client_messages._flush_locked(path, "https://hub.invalid", "node-token")

    assert "\x1b" not in caplog.text
    assert "\\x1b[31m" in caplog.text


def test_message_body_file_read_is_bounded(monkeypatch):
    reads = []

    class ReadSpy(BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: ReadSpy(b"x" * (fleet_hub_messages.BODY_LIMIT + 2)))
    with pytest.raises(ValueError, match="message body file exceeds"):
        fleet_messages_cli._read_body(Path("large-message.txt"))
    assert reads == [fleet_hub_messages.BODY_LIMIT + 1]


def test_offline_send_does_not_claim_queued_when_spool_is_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    spool = fleet_client_messages._spool_path(NODE_A)
    spool.parent.mkdir(mode=0o700)
    target = tmp_path / "outside.jsonl"
    target.write_text("keep", encoding="utf-8")
    spool.symlink_to(target)
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args: (_ for _ in ()).throw(fleet_client.FleetClientError("offline")),
    )

    with pytest.raises(fleet_client.FleetClientError, match="could not be persisted"):
        fleet_client_messages.send(
            recipient=NODE_B,
            subject="Unsafe spool",
            body="Do not claim this was queued",
            reference=None,
            priority="normal",
        )
    assert spool.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_offline_send_validates_fields_before_claiming_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args: (503, {"error": "offline"}))
    with pytest.raises(fleet_client.FleetClientError, match="subject"):
        fleet_client_messages.send(
            recipient=NODE_B,
            subject="x" * (fleet_hub_messages.SUBJECT_LIMIT + 1),
            body="Valid body",
            reference=None,
            priority="normal",
        )
    assert not fleet_client_messages._spool_path(NODE_A).exists()


def test_flush_discards_messages_older_than_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    spool = fleet_client_messages._spool_path(NODE_A)
    expired = {
        "action": "send",
        "to": NODE_B,
        "subject": "Expired",
        "body": "Old",
        "client_id": "expired-1",
        "enqueued_at": (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(),
    }
    with fleet_client._spool_lock(spool) as dir_fd:
        assert fleet_client._spool_append_locked(spool, expired, dir_fd=dir_fd)
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args: (_ for _ in ()).throw(AssertionError("expired message was sent")),
    )
    assert fleet_client_messages.flush(NODE_A) == 0
    assert not spool.exists()


def test_message_cli_registers_send_and_external_principal_commands():
    parser = cli._build_parser()
    send = parser.parse_args(["fleet", "msg", "send", "--to", NODE_B, "--subject", "Task", "--body-file", "body.txt"])
    enroll = parser.parse_args(["fleet", "msg", "principals", "add", "miller"])
    assert send.func.__name__ == "_send"
    assert enroll.func.__name__ == "_principal_add"


def test_schema_migrates_existing_version_26_message_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.execute(
        """CREATE TABLE fleet_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            reference TEXT,
            priority TEXT NOT NULL,
            reply_to INTEGER,
            created_at TEXT NOT NULL,
            acked_at TEXT,
            acked_by TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO fleet_messages(sender,recipient,subject,body,priority,created_at) VALUES(?,?,?,?,?,?)",
        (NODE_A, NODE_B, "Legacy", "Keep me", "normal", datetime.now(timezone.utc).isoformat()),
    )
    fleet_hub_messages.ensure_schema(conn)
    client_id, payload_hash = conn.execute("SELECT client_id,payload_hash FROM fleet_messages WHERE id=1").fetchone()
    assert client_id == "legacy-1"
    assert payload_hash == fleet_hub_messages._payload_digest(NODE_B, "Legacy", "Keep me", None, "normal")
    assert fleet_hub.SCHEMA_VERSION == 29
    conn.close()


@pytest.mark.parametrize("action", ["send", "reply", "list", "add", "revoke"])
@pytest.mark.parametrize("hub_url", ["http://hub.invalid", "http://127.0.0.1:9876", "https://hub.invalid"])
def test_message_transports_guard_bearer_urls_before_request(action, hub_url, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": hub_url, "node_token": "node-token", "admin_token": ADMIN},
    )
    requests = []
    opened = []
    original_request = urllib.request.Request

    def build_request(*args, **kwargs):
        request = original_request(*args, **kwargs)
        requests.append(request)
        return request

    class Response(BytesIO):
        status = 200

    def hub_open(request, **_kwargs):
        opened.append(request)
        body = json.loads(request.data)
        payload = {"principal": {"principal_id": "agent:researcher", "name": "researcher"}}
        if action == "add":
            payload["principal"]["token"] = body["token"]
        elif action in ("send", "reply"):
            payload = {"message": {"client_id": body["client_id"]}}
        elif action == "list":
            payload = {"messages": [], "next_cursor": body["after"]}
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request, "Request", build_request)
    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)

    def invoke():
        if action == "send":
            return fleet_client_messages.send(
                recipient=NODE_B, subject="Transport", body="Check transport", reference=None, priority="normal"
            )
        if action == "reply":
            return fleet_client_messages.reply(
                1, body="Check transport", subject=None, reference=None, priority="normal"
            )
        if action == "list":
            return fleet_client_messages.list_messages()
        return fleet_client_messages.manage_principal(action, name="researcher")

    if hub_url == "http://hub.invalid":
        with pytest.raises(fleet_client.FleetClientError, match="must use https"):
            invoke()
        assert requests == opened == []
    else:
        assert invoke()
        assert len(requests) == len(opened) == 1
        token = "node-token" if action in ("send", "reply", "list") else ADMIN
        assert requests[0].get_header("Authorization") == f"Bearer {token}"
    if action in ("send", "reply"):
        assert not fleet_client_messages._spool_path(NODE_A, "node-token").exists()


@pytest.mark.parametrize("label", [None, "Original label"])
@pytest.mark.parametrize("conflict", [False, True])
def test_principal_add_recovers_committed_request_after_label_change_and_conflict(
    message_hub, tmp_path, monkeypatch, label, conflict
):
    base, _token_a, _token_b, db = message_hub
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": base, "node_token": "", "admin_token": ADMIN},
    )
    requests = []
    original_open = fleet_client._hub_open
    original_deadline = fleet_client._run_with_deadline

    def hub_open(request, **kwargs):
        requests.append(json.loads(request.data))
        if conflict and len(requests) == 2:
            raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, BytesIO(b'{"error":"conflict"}'))
        return original_open(request, **kwargs)

    def lose_first_response(request, *, timeout):
        result = original_deadline(request, timeout=timeout)
        if len(requests) == 1:
            raise TimeoutError("response lost after commit")
        return result

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lose_first_response)
    with pytest.raises(fleet_client.FleetClientError, match="TimeoutError"):
        fleet_client_messages.manage_principal("add", name="recoverable", label=label)
    path = fleet_client_messages._principal_pending_path("recoverable", ADMIN)
    pending = fleet_client._read_spool(path)
    if conflict:
        with pytest.raises(fleet_client.FleetClientError, match="conflict"):
            fleet_client_messages.manage_principal("add", name="recoverable", label="Changed label")
        assert fleet_client._read_spool(path) == pending
    recovered = fleet_client_messages.manage_principal("add", name="recoverable", label="Changed again")
    assert all(request == requests[0] for request in requests)
    assert recovered["principal"]["token"] == requests[0]["token"]
    assert recovered["principal"]["label"] == (label or "recoverable")
    assert not path.exists()
    conn = fleet_hub.open_db(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM fleet_message_principals WHERE name='recoverable'").fetchone()[0] == 1
    finally:
        conn.close()


def test_principal_add_clears_pending_request_after_definitive_validation_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ADMIN},
    )
    requests = []

    class Response(BytesIO):
        status = 201

    def hub_open(request, **_kwargs):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 400, "invalid label", {}, BytesIO(b'{"error":"invalid label"}')
            )
        response = {
            "principal": {
                "principal_id": "agent:researcher",
                "name": "researcher",
                "label": body["label"],
                "token": body["token"],
            }
        }
        return Response(json.dumps(response).encode())

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    with pytest.raises(fleet_client.FleetClientError, match="invalid label"):
        fleet_client_messages.manage_principal("add", name="researcher", label="")
    path = fleet_client_messages._principal_pending_path("researcher", ADMIN)
    assert not path.exists()

    enrolled = fleet_client_messages.manage_principal("add", name="researcher", label="Corrected label")
    assert enrolled["principal"]["label"] == "Corrected label"
    assert requests[0]["request_id"] != requests[1]["request_id"]
    assert requests[1]["label"] == "Corrected label"


def test_principal_add_retries_after_revoked_pending_enrollment(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ADMIN},
    )
    requests = []

    class Response(BytesIO):
        status = 201

    def hub_open(request, **_kwargs):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                409,
                "revoked enrollment",
                {},
                BytesIO(b'{"error":"revoked enrollment","code":"principal_enrollment_revoked"}'),
            )
        return Response(
            json.dumps(
                {
                    "principal": {
                        "principal_id": "agent:researcher",
                        "name": "researcher",
                        "label": body["label"],
                        "token": body["token"],
                    }
                }
            ).encode()
        )

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    enrolled = fleet_client_messages.manage_principal("add", name="researcher", label="Current label")

    assert len(requests) == 2
    assert requests[0]["request_id"] != requests[1]["request_id"]
    assert requests[1]["label"] == "Current label"
    assert enrolled["principal"]["token"] == requests[1]["token"]
    assert not fleet_client_messages._principal_pending_path("researcher", ADMIN).exists()


def test_principal_revoke_clears_local_pending_enrollment(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ADMIN},
    )
    pending_path = fleet_client_messages._principal_pending_path("researcher", ADMIN)
    fleet_client_messages._pending_principal_request("researcher", ADMIN, None)

    class Response(BytesIO):
        status = 200

    monkeypatch.setattr(
        fleet_client,
        "_hub_open",
        lambda *_args, **_kwargs: Response(b'{"principal":{"principal_id":"agent:researcher","name":"researcher"}}'),
    )
    fleet_client_messages.manage_principal("revoke", name="researcher")

    assert not pending_path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"principal": None},
        {"principal": []},
        {"principal": {}},
        {"principal": {"principal_id": "agent:other", "name": "researcher"}},
        {"principal": {"principal_id": "agent:researcher", "name": "other"}},
    ],
)
def test_principal_revoke_requires_matching_success_identity(payload, monkeypatch):
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ADMIN},
    )

    class Response(BytesIO):
        status = 200

    monkeypatch.setattr(fleet_client, "_hub_open", lambda *_args, **_kwargs: Response(json.dumps(payload).encode()))
    with pytest.raises(fleet_client.FleetClientError, match="did not confirm the revoked principal"):
        fleet_client_messages.manage_principal("revoke", name="researcher")
