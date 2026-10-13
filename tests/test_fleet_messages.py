"""Hub-backed per-principal message queue (#1688)."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from brigade import cli, fleet_client, fleet_client_messages, fleet_hub, fleet_hub_messages

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


def test_retry_deduplication_quota_and_dashboard_unread_count(message_hub, monkeypatch):
    base, token_a, token_b, _db = message_hub
    first_body = {
        "action": "send",
        "to": NODE_B,
        "subject": "Retry-safe",
        "body": "One message",
        "client_id": "retry-key-1",
        "enqueued_at": "2026-10-13T04:00:00+00:00",
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
    reply = _request(base, token_b, {"action": "reply", "id": parent_id, "body": "Recent"})[1]
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE fleet_messages SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(days=91)).isoformat(), parent_id),
    )
    conn.commit()
    conn.close()
    _request(base, token_b, {"action": "list"})
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


def test_offline_send_is_spooled_and_delivered_by_flush(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "http://hub.invalid", "node_token": "node-secret", "admin_token": ""},
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


def test_new_send_flushes_older_spooled_messages_first(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "http://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub.invalid"})
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

    offline = False
    newer = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Newer",
        body="Sent second",
        reference=None,
        priority="normal",
    )

    assert newer["message"]["client_id"] == attempts[-1]["client_id"]
    assert [message["subject"] for message in attempts[1:]] == ["Older", "Newer"]


def test_ambiguous_reply_is_spooled_and_retried_with_same_client_id(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "http://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub.invalid"})
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
        lambda: {"hub_url": "http://hub.invalid", "node_token": "node-secret", "admin_token": ""},
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


def test_offline_send_does_not_claim_queued_when_spool_is_unsafe(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "http://hub.invalid", "node_token": "node-secret", "admin_token": ""},
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
        lambda: {"hub_url": "http://hub.invalid", "node_token": "node-secret", "admin_token": ""},
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
        lambda: {"hub_url": "http://hub.invalid", "node_token": "node-secret", "admin_token": ""},
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
    assert fleet_hub.SCHEMA_VERSION == 28
    conn.close()
