"""Hub-backed per-principal message queue (#1688)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from io import BytesIO
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from brigade import fleet_client_message_credentials as credentials
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
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
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
    monkeypatch.setattr(credentials, "message_settings", lambda _path=None: ("https://hub.invalid", "token", NODE_A))
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
        {"messages": [{"id": 7}], "next_cursor": 5},
        {"messages": [{"id": 7}, {"id": 6}], "next_cursor": 6},
        {"messages": [], "next_cursor": 1},
        {"messages": [{"id": True}], "next_cursor": True},
    ],
)
def test_message_list_rejects_invalid_success_payload(monkeypatch, payload):
    monkeypatch.setattr(credentials, "message_settings", lambda _path=None: ("https://hub.invalid", "token", NODE_A))
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (200, payload))

    with pytest.raises(fleet_client.FleetClientError, match="invalid message page"):
        fleet_client_messages.list_messages()


def test_message_list_rejects_id_before_requested_cursor(monkeypatch):
    payload = {"messages": [{"id": 5}], "next_cursor": 5}
    monkeypatch.setattr(credentials, "message_settings", lambda _path=None: ("https://hub.invalid", "token", NODE_A))
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (200, payload))

    with pytest.raises(fleet_client.FleetClientError, match="invalid message page"):
        fleet_client_messages.list_messages(after=5)


def test_message_list_accepts_consistent_cursor(monkeypatch):
    page = {"messages": [{"id": 7}, {"id": 9}], "next_cursor": 9}
    monkeypatch.setattr(credentials, "message_settings", lambda _path=None: ("https://hub.invalid", "token", NODE_A))
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (200, page))

    assert fleet_client_messages.list_messages(after=5) == page


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
    assert rejected[1]["code"] == "message_recipient_quota"
    assert "quota" in rejected[1]["error"]

    page = _get(base, ADMIN, "/view/machines")
    assert "1 unread message</div>" in page
    assert _request(base, token_b, {"action": "ack", "id": first[1]["message"]["id"]})[0] == 200
    page = _get(base, ADMIN, "/view/machines")
    assert "unread message" not in page
    monkeypatch.setattr(fleet_hub_messages, "SENDER_DAILY_QUOTA", 1)
    sender_limited = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_A, "subject": "Daily limit", "body": "Three", "client_id": "retry-key-3"},
    )
    assert sender_limited[0] == 429
    assert sender_limited[1]["code"] == "message_sender_quota"
    assert "sender" in sender_limited[1]["error"]


def test_dashboard_cookie_user_does_not_see_unread_message_count(message_hub):
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
    assert "unread message" not in page


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


def test_http_sender_revocation_race_has_retryable_error_code(message_hub, monkeypatch):
    base, token_a, _token_b, db = message_hub
    handle = fleet_hub_messages.handle

    def revoke_after_caller_auth(conn, raw, *, caller, **kwargs):
        conn.execute(
            "UPDATE nodes SET revoked_at=? WHERE node_id=?",
            (datetime.now(timezone.utc).isoformat(), NODE_A),
        )
        conn.commit()
        return handle(conn, raw, caller=caller, **kwargs)

    monkeypatch.setattr(fleet_hub_messages, "handle", revoke_after_caller_auth)

    status, payload = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_B, "subject": "Race", "body": "Retain", "client_id": "auth-race"},
    )

    assert status == 403
    assert payload["code"] == "message_sender_inactive"
    assert payload["error"] == "sender is not an active hub principal"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0


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
    assert rejected[1]["code"] == "message_sender_quota"
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


def test_schema_backfill_ignores_sends_outside_daily_quota_window(tmp_path, monkeypatch):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    now = datetime.now(timezone.utc)
    old_timestamp = (now - timedelta(days=2)).isoformat()
    recent_timestamp = (now - timedelta(hours=1)).isoformat()
    rows = [
        ("old-send", old_timestamp),
        ("recent-send", recent_timestamp),
    ]
    for client_id, received_at in rows:
        conn.execute(
            "INSERT INTO fleet_messages "
            "(sender,recipient,subject,body,priority,client_id,payload_hash,created_at,received_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (NODE_A, NODE_B, client_id, "body", "normal", client_id, "legacy-hash", received_at, received_at),
        )
    conn.commit()
    monkeypatch.setattr(fleet_hub_messages, "_prune_expired", lambda _conn: None)

    fleet_hub_messages.ensure_schema(conn)

    send_ids = [row[0] for row in conn.execute("SELECT client_id FROM fleet_message_sends ORDER BY client_id")]
    conn.close()
    assert send_ids == ["recent-send"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes are not portable")
def test_hub_message_database_and_wal_files_are_owner_only(tmp_path):
    old_umask = os.umask(0o022)
    try:
        db_dir = tmp_path / "hub-state"
        db = db_dir / "fleet.db"
        conn = fleet_hub.init_db(db)
        conn.execute(
            "INSERT INTO fleet_message_sends(sender,client_id,received_at) VALUES(?,?,?)",
            (NODE_A, "permission-probe", datetime.now(timezone.utc).isoformat()),
        )
        assert stat.S_IMODE(db_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(db.stat().st_mode) == 0o600
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{db}{suffix}")
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
        conn.close()
    finally:
        os.umask(old_umask)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes are not portable")
def test_hub_preserves_existing_shared_database_directory(tmp_path):
    db_dir = tmp_path / "shared-hub-state"
    db_dir.mkdir(mode=0o755)
    os.chmod(db_dir, 0o755)
    db = db_dir / "fleet.db"
    db.write_bytes(b"")
    os.chmod(db, 0o644)

    conn = fleet_hub.init_db(db)
    conn.close()

    assert stat.S_IMODE(db_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


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
    status, enrolled = _post_path(base, ADMIN, "/message-principals", {"action": "add", "name": "dot"})
    assert status == 201
    principal = enrolled["principal"]
    assert principal["principal_id"] == "agent:dot"
    token = principal["token"]

    sent = _request(
        base,
        token_a,
        {"action": "send", "to": "agent:dot", "subject": "Task", "body": "Review this", "client_id": "task-1"},
    )
    assert sent[0] == 201
    page = _get(base, ADMIN, "/view/machines")
    assert 'data-node="agent:dot"' not in page
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
    assert replied[0] == 403
    assert replied[1]["code"] == "message_recipient_not_permitted"

    revoked = _post_path(
        base,
        ADMIN,
        "/message-principals",
        {
            "action": "revoke",
            "name": "dot",
            "expected_enrollment_id": principal["enrollment_id"],
        },
    )
    assert revoked[0] == 200
    assert _request(base, token, {"action": "list"})[0] == 401
    reissued = _post_path(
        base,
        ADMIN,
        "/message-principals",
        {"action": "add", "name": "dot", "expected_enrollment_id": principal["enrollment_id"]},
    )
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
        "expected_enrollment_id": None,
    }
    first = _post_path(base, ADMIN, "/message-principals", request)
    replay = _post_path(base, ADMIN, "/message-principals", request)

    assert first[0] == replay[0] == 201
    assert first[1]["principal"] == replay[1]["principal"]
    assert first[1]["principal"]["token"] == request["token"]
    changed = _post_path(base, ADMIN, "/message-principals", {**request, "label": "changed"})
    assert changed[0] == 409

    revoke = {"action": "revoke", "name": "recoverable", "expected_enrollment_id": request["request_id"]}
    assert _post_path(base, ADMIN, "/message-principals", revoke)[0] == 200
    stale_replay = _post_path(base, ADMIN, "/message-principals", request)
    assert stale_replay[0] == 409
    assert stale_replay[1]["code"] == "principal_enrollment_revoked"
    assert _post_path(base, ADMIN, "/message-principals", revoke)[0] == 200
    assert (
        _post_path(
            base,
            ADMIN,
            "/message-principals",
            {"action": "revoke", "name": "missing", "expected_enrollment_id": None},
        )[0]
        == 404
    )

    with sqlite3.connect(db) as conn:
        stored = conn.execute("SELECT token_hash FROM fleet_message_principals WHERE name='recoverable'").fetchone()[0]
    assert stored != request["token"]


def test_principal_enrollment_and_revoke_are_generation_fenced(message_hub):
    base, _token_a, _token_b, db = message_hub
    enrollment_a = {
        "action": "add",
        "name": "generation-fence",
        "request_id": "a" * 32,
        "token": "old-principal-token-that-is-long-enough",
        "expected_enrollment_id": None,
    }
    first = _post_path(base, ADMIN, "/message-principals", enrollment_a)
    assert first[0] == 201
    generation_a = first[1]["principal"]["enrollment_id"]
    assert (
        _post_path(
            base,
            ADMIN,
            "/message-principals",
            {"action": "revoke", "name": "generation-fence", "expected_enrollment_id": generation_a},
        )[0]
        == 200
    )

    enrollment_b = {
        **enrollment_a,
        "request_id": "b" * 32,
        "token": "new-principal-token-that-is-long-enough",
        "expected_enrollment_id": generation_a,
    }
    second = _post_path(base, ADMIN, "/message-principals", enrollment_b)
    assert second[0] == 201
    generation_b = second[1]["principal"]["enrollment_id"]
    delayed_revoke = _post_path(
        base,
        ADMIN,
        "/message-principals",
        {"action": "revoke", "name": "generation-fence", "expected_enrollment_id": generation_a},
    )
    assert delayed_revoke[0] == 409
    assert delayed_revoke[1]["code"] == "principal_enrollment_changed"

    assert (
        _post_path(
            base,
            ADMIN,
            "/message-principals",
            {"action": "revoke", "name": "generation-fence", "expected_enrollment_id": generation_b},
        )[0]
        == 200
    )
    stale_add = _post_path(base, ADMIN, "/message-principals", enrollment_a)
    assert stale_add[0] == 409
    assert stale_add[1]["code"] == "principal_enrollment_changed"
    with sqlite3.connect(db) as conn:
        current = conn.execute(
            "SELECT enrollment_id,revoked_at FROM fleet_message_principals WHERE name='generation-fence'"
        ).fetchone()
    assert current[0] == generation_b
    assert current[1] is not None


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
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
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
        body = json.loads(request.data)
        if body["action"] == "get":
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, BytesIO(b'{"error":"missing"}'))
        requests.append(body)
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
                    "enrollment_id": requests[-1]["request_id"],
                }
            }
        ).encode(),
    )
    calls = 0

    def run_with_deadline(request, *, timeout):
        nonlocal calls
        result = request()
        if requests and requests[-1]["action"] == "add" and calls == 0:
            calls += 1
            raise TimeoutError("response lost after commit")
        return result

    monkeypatch.setattr(fleet_client, "_run_with_deadline", run_with_deadline)
    with pytest.raises(fleet_client.FleetClientError, match="TimeoutError"):
        fleet_client_messages.manage_principal("add", name="researcher")

    stable_path = fleet_client_messages._principal_pending_path(
        "researcher", "admin-token", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    # Active scoped retry state survives a change in the home node identity.
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_B)
    deferred_request_ids = []
    enrolled = fleet_client_messages.manage_principal(
        "add", name="researcher", _defer_finalize=True, _deferred_request_ids=deferred_request_ids
    )
    assert enrolled["principal"]["token"]
    assert fleet_client._read_spool(stable_path)[0]["request_id"] == requests[-1]["request_id"]
    assert deferred_request_ids == [requests[-1]["request_id"]]
    fleet_client_messages.finalize_principal_add("researcher", request_id=deferred_request_ids[0])
    assert not stable_path.exists()
    assert requests[0]["request_id"] == requests[1]["request_id"]
    assert requests[0]["token"] == requests[1]["token"]
    assert not stable_path.exists()


@pytest.mark.parametrize("interruption", ["before-copy", "after-copy", "after-finalize"])
def test_node_prefixed_principal_retry_cannot_resurrect_after_interruption(tmp_path, monkeypatch, interruption):
    settings = {"hub_url": "https://a.invalid", "node_token": "", "admin_token": "fixture-admin"}
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(settings))
    pending = fleet_client_messages._pending_principal_request(
        "fixture", settings["admin_token"], None, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    active = fleet_client_messages._principal_pending_path(
        "fixture", settings["admin_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    history = active.with_name(f"node-a.{active.name}")
    original = active.read_bytes()
    if interruption == "before-copy":
        active.rename(history)
    else:
        history.write_bytes(original)
        history.chmod(0o600)
        if interruption == "after-finalize":
            fleet_client_messages.finalize_principal_add("fixture", request_id=pending["request_id"])
    active_bytes = active.read_bytes() if active.exists() else None
    enrollments = []

    def principal_request(hub, _admin, body):
        if body["action"] == "get":
            return 404, {"error": "missing"}
        enrollments.append((hub, dict(body)))
        return 201, {
            "principal": {
                "principal_id": "agent:fixture",
                "name": "fixture",
                "token": body["token"],
                "enrollment_id": body["request_id"],
            }
        }

    monkeypatch.setattr(fleet_client_messages, "_principal_admin_request", principal_request)
    # Retrying repeatedly must preserve both sides of an interrupted migration.
    for _ in range(2):
        with pytest.raises(fleet_client.FleetClientError) as error:
            fleet_client_messages.manage_principal("add", name="fixture")
        assert str(error.value) == "node-prefixed principal retry requires reconciliation; left untouched"
        assert history.read_bytes() == original
        assert (active.read_bytes() if active.exists() else None) == active_bytes
        assert enrollments == []

    # The same admin credential at another endpoint gets independent retry state.
    settings["hub_url"] = "https://b.invalid"
    deferred = []
    fleet_client_messages.manage_principal("add", name="fixture", _defer_finalize=True, _deferred_request_ids=deferred)
    assert len(enrollments) == 1
    assert enrollments[0][0] == "https://b.invalid"
    assert deferred == [enrollments[0][1]["request_id"]]
    assert deferred[0] != pending["request_id"]
    other = fleet_client_messages._principal_pending_path(
        "fixture", settings["admin_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    assert other != active
    assert fleet_client._read_spool(other)[0]["request_id"] == deferred[0]
    fleet_client_messages.finalize_principal_add("fixture", request_id=deferred[0])
    assert not other.exists()
    assert history.read_bytes() == original
    assert (active.read_bytes() if active.exists() else None) == active_bytes

    # A normalized return to the original endpoint must still refuse old history.
    settings["hub_url"] = "https://a.invalid/"
    with pytest.raises(fleet_client.FleetClientError) as error:
        fleet_client_messages.manage_principal("add", name="fixture")
    assert str(error.value) == "node-prefixed principal retry requires reconciliation; left untouched"
    assert len(enrollments) == 1
    assert history.read_bytes() == original
    assert (active.read_bytes() if active.exists() else None) == active_bytes


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
        body = json.loads(request.data)
        if body["action"] == "get":
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, BytesIO(b'{"error":"missing"}'))
        requests.append(body)
        return Response()

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    monkeypatch.setattr(fleet_client, "_read_bounded_response", lambda *_args, **_kwargs: b"{}")

    with pytest.raises(fleet_client.FleetClientError, match="did not confirm the enrolled principal"):
        fleet_client_messages.manage_principal("add", name="researcher")
    path = fleet_client_messages._principal_pending_path(
        "researcher", "admin-token", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
                    "enrollment_id": "wrong-generation",
                }
            }
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
                    "enrollment_id": requests[-1]["request_id"],
                }
            }
        ).encode(),
    )
    assert fleet_client_messages.manage_principal("add", name="researcher")["principal"]["token"]
    assert len({request["request_id"] for request in requests}) == 1
    assert not path.exists()


def test_stale_principal_finalizer_preserves_new_enrollment_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": "admin-token"},
    )
    path = fleet_client_messages._principal_pending_path(
        "researcher", "admin-token", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    with fleet_client._spool_lock(path) as dir_fd:
        fleet_client._write_spool_atomic(
            path, [{"name": "researcher", "request_id": "older-generation"}], dir_fd=dir_fd
        )

    with fleet_client._spool_lock(path) as dir_fd:
        fleet_client._write_spool_atomic(
            path, [{"name": "researcher", "request_id": "newer-generation"}], dir_fd=dir_fd
        )

    fleet_client_messages.finalize_principal_add("researcher", request_id="older-generation")

    assert fleet_client._read_spool(path) == [{"name": "researcher", "request_id": "newer-generation"}]


def test_principal_revoke_reuses_generation_after_ambiguous_timeout(tmp_path, monkeypatch):
    current_generation = "a" * 32
    generation_reads = []
    revoke_requests = []
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ADMIN},
    )

    def request(_hub, _admin, body):
        nonlocal current_generation
        if body["action"] == "get":
            generation_reads.append(current_generation)
            return 200, {
                "principal": {
                    "principal_id": "agent:researcher",
                    "name": "researcher",
                    "enrollment_id": current_generation,
                }
            }
        revoke_requests.append(dict(body))
        if len(revoke_requests) == 1:
            raise fleet_client.FleetClientError("timeout after revoke commit")
        if len(revoke_requests) == 2:
            return 409, {"code": "principal_enrollment_changed", "error": "principal generation changed"}
        return 200, {
            "principal": {
                "principal_id": "agent:researcher",
                "name": "researcher",
                "enrollment_id": body["expected_enrollment_id"],
            }
        }

    monkeypatch.setattr(fleet_client_messages, "_principal_admin_request", request)
    with pytest.raises(fleet_client.FleetClientError, match="timeout after revoke commit"):
        fleet_client_messages.manage_principal("revoke", name="researcher")

    current_generation = "b" * 32
    with pytest.raises(fleet_client.FleetClientError, match="principal generation changed"):
        fleet_client_messages.manage_principal("revoke", name="researcher")

    assert [body["expected_enrollment_id"] for body in revoke_requests] == ["a" * 32, "a" * 32]
    assert generation_reads == ["a" * 32]
    assert not fleet_client_messages._principal_revocation_path(
        "researcher", ADMIN, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ).exists()

    fleet_client_messages.manage_principal("revoke", name="researcher")
    assert revoke_requests[-1]["expected_enrollment_id"] == "b" * 32
    assert generation_reads == ["a" * 32, "b" * 32]


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
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
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
    assert fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ).exists()
    assert fleet_client_messages.flush(NODE_A) == 1
    assert len({attempt["client_id"] for attempt in attempts}) == 1
    assert not fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ).exists()


def test_send_refuses_before_post_when_full_spool_cannot_retain_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    queued_for_b = {
        "action": "send",
        "to": NODE_B,
        "subject": "Blocked recipient",
        "body": "Keep the retry key",
        "client_id": "blocked-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, queued_for_b, dir_fd=dir_fd)
    monkeypatch.setattr(fleet_client, "MAX_SPOOL_BYTES", path.stat().st_size + 50)
    attempts = []

    def request(_hub, _token, message):
        attempts.append(message.copy())
        return 429, {"code": "message_recipient_quota", "recipient": NODE_B}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    with pytest.raises(fleet_client.FleetClientError, match="spool is full"):
        fleet_client_messages.send(
            recipient="33333333-3333-4333-8333-333333333333",
            subject="Preserve idempotency",
            body="Do not post without queue capacity",
            reference=None,
            priority="normal",
        )

    assert [message["client_id"] for message in attempts] == ["blocked-message"]
    assert fleet_client._read_spool(path)[0]["client_id"] == "blocked-message"


@pytest.mark.parametrize(
    "failed_mode",
    [
        stat.S_IFREG,
        pytest.param(
            stat.S_IFDIR,
            marks=pytest.mark.skipif(sys.platform == "win32", reason="Spool directory fsync is POSIX-only"),
        ),
    ],
)
def test_send_does_not_post_until_spool_fsync_succeeds(tmp_path, monkeypatch, failed_mode):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    with fleet_client._spool_lock(path):
        pass
    attempts = []
    real_fsync = os.fsync
    failure_injected = False

    def request(_hub, _token, message):
        attempts.append(message.copy())
        return 503, {"error": "response lost after commit"}

    def fail_fsync(fd):
        nonlocal failure_injected
        if stat.S_IFMT(os.fstat(fd).st_mode) == failed_mode and not failure_injected:
            failure_injected = True
            raise OSError("disk full")
        real_fsync(fd)

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    monkeypatch.setattr(fleet_client.os, "fsync", fail_fsync)

    with pytest.raises(fleet_client.FleetClientError, match="could not be persisted"):
        fleet_client_messages.send(
            recipient=NODE_B,
            subject="Keep the idempotency key",
            body="Do not post before durable spooling",
            reference=None,
            priority="normal",
        )

    assert attempts == []
    assert fleet_client._read_spool(path) == []


def test_ambiguous_spool_append_surfaces_client_id_without_posting(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    real_append = fleet_client._spool_append_locked
    attempts = []

    def ambiguous_append(path, message, *, dir_fd=None, evict_oldest=True):
        assert real_append(path, message, dir_fd=dir_fd, evict_oldest=evict_oldest)
        return False

    monkeypatch.setattr(fleet_client, "_spool_append_locked", ambiguous_append)
    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda _hub, _token, message: attempts.append(message.copy()),
    )

    with pytest.raises(fleet_client.FleetClientError, match="persistence is ambiguous.*client_id") as error:
        fleet_client_messages.send(
            recipient=NODE_B,
            subject="Keep the retained ID",
            body="Do not post after an ambiguous append result",
            reference=None,
            priority="normal",
        )

    assert attempts == []
    retained_id = error.value.args[0].split("client_id ", 1)[1].split(";", 1)[0]
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    assert [entry["client_id"] for entry in fleet_client._read_spool(path)] == [retained_id]


def test_message_spool_refuses_overflow_without_evicting_queued_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    messages = [
        {
            "action": "send",
            "to": NODE_B,
            "subject": "queued",
            "body": "body",
            "client_id": f"message-{index}",
            "enqueued_at": "2026-01-01T00:00:00+00:00",
        }
        for index in range(3)
    ]
    encoded_size = len((fleet_client._encode(messages[0]) + "\n").encode("utf-8"))
    monkeypatch.setattr(fleet_client, "MAX_SPOOL_BYTES", encoded_size * 3 - 1)
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, messages[0], dir_fd=dir_fd)
        assert fleet_client._spool_append_locked(path, messages[1], dir_fd=dir_fd)
    original_spool = path.read_bytes()

    with (
        fleet_client._spool_lock(path) as dir_fd,
        pytest.raises(fleet_client.FleetClientError, match="could not be persisted"),
    ):
        fleet_client_messages._queue_locked(path, messages[2], dir_fd=dir_fd)

    assert path.read_bytes() == original_spool
    assert fleet_client._read_spool(path) == messages[:2]


@pytest.mark.parametrize(
    ("status", "response"),
    [
        (401, {"error": "unauthorized"}),
        (403, {"error": "sender is not an active hub principal", "code": "message_sender_inactive"}),
    ],
)
def test_new_send_reports_unauthorized_without_cross_credential_spooling(tmp_path, monkeypatch, status, response):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (status, response))

    with pytest.raises(fleet_client.FleetClientError, match=response["error"]):
        fleet_client_messages.send(
            recipient=NODE_B,
            subject="Retry after credential recovery",
            body="Do not leave an unreachable token-scoped request",
            reference=None,
            priority="normal",
        )

    assert not fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ).exists()


@pytest.mark.parametrize(
    ("status", "response"),
    [
        (401, {"error": "unauthorized"}),
        (403, {"error": "sender is not an active hub principal", "code": "message_sender_inactive"}),
    ],
)
def test_send_surfaces_unauthorized_while_draining_existing_spool(tmp_path, monkeypatch, status, response):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    old = {
        "action": "send",
        "to": NODE_B,
        "subject": "Queued earlier",
        "body": "Retain when auth expires",
        "client_id": "old-token-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, old, dir_fd=dir_fd)
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (status, response))

    with pytest.raises(fleet_client.FleetClientError, match=response["error"]):
        fleet_client_messages.send(
            recipient=NODE_B,
            subject="New message",
            body="Do not report this as queued",
            reference=None,
            priority="normal",
        )

    assert fleet_client._read_spool(path) == [old]
    with pytest.raises(fleet_client.FleetClientError, match=response["error"]):
        fleet_client_messages.flush(NODE_A)
    assert fleet_client._read_spool(path) == [old]


@pytest.mark.parametrize(
    ("status", "response"),
    [
        (401, {"error": "unauthorized"}),
        (403, {"error": "sender is not an active hub principal", "code": "message_sender_inactive"}),
    ],
)
def test_send_surfaces_unauthorized_reply_lookup_while_draining(tmp_path, monkeypatch, status, response):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    blocked = {
        "action": "send",
        "to": NODE_B,
        "subject": "Queued earlier",
        "body": "Recipient quota may clear later",
        "client_id": "blocked-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    reply = {
        "action": "reply",
        "id": 42,
        "text": "Queued reply",
        "client_id": "queued-reply",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, blocked, dir_fd=dir_fd)
        assert fleet_client._spool_append_locked(path, reply, dir_fd=dir_fd)

    def request(_hub, _token, message, **_kwargs):
        if message.get("client_id") == "blocked-message":
            return 429, {"error": "quota", "code": "message_recipient_quota", "recipient": NODE_B}
        return status, response

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    with pytest.raises(fleet_client.FleetClientError, match=response["error"]):
        fleet_client_messages.send(
            recipient=NODE_A,
            subject="New message",
            body="Do not report this as queued",
            reference=None,
            priority="normal",
        )

    pending = fleet_client._read_spool(path)
    assert [item["client_id"] for item in pending] == ["blocked-message", "queued-reply"]
    assert pending[0]["_recipient"] == NODE_B
    assert pending[1] == reply


@pytest.mark.parametrize(
    ("status", "response"),
    [
        (401, {"error": "unauthorized"}),
        (403, {"error": "sender is not an active hub principal", "code": "message_sender_inactive"}),
    ],
)
def test_new_reply_surfaces_unauthorized_parent_lookup(tmp_path, monkeypatch, status, response):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    old = {
        "action": "send",
        "to": NODE_B,
        "subject": "Queued earlier",
        "body": "Recipient quota may clear later",
        "client_id": "blocked-message",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, old, dir_fd=dir_fd)

    def request(_hub, _token, message, **_kwargs):
        if message.get("client_id") == "blocked-message":
            return 429, {"error": "quota", "code": "message_recipient_quota", "recipient": NODE_B}
        return status, response

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    with pytest.raises(fleet_client.FleetClientError, match=response["error"]):
        fleet_client_messages.reply(42, body="Reply text", subject=None, reference=None, priority="normal")

    pending = fleet_client._read_spool(path)
    assert [item["client_id"] for item in pending] == ["blocked-message"]
    assert pending[0]["_recipient"] == NODE_B


def test_spool_flush_keeps_message_during_recipient_revocation(message_hub, tmp_path, monkeypatch):
    base, token_a, _token_b, db = message_hub
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": base, "node_token": token_a, "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": base})
    request = fleet_client_messages._request
    monkeypatch.setattr(fleet_client_messages, "_request", lambda *_args, **_kwargs: (503, {"error": "offline"}))

    queued = fleet_client_messages.send(
        recipient=NODE_B,
        subject="During rotation",
        body="Keep while recipient is re-enrolled",
        reference=None,
        priority="normal",
    )
    path = fleet_client_messages._spool_path(NODE_A, token_a, hub_url=fleet_client.load_fleet_settings()["hub_url"])
    assert queued["queued"] is True

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE nodes SET revoked_at=? WHERE node_id=?", (datetime.now(timezone.utc).isoformat(), NODE_B))
    monkeypatch.setattr(fleet_client_messages, "_request", request)

    assert fleet_client_messages.flush(NODE_A) == 0
    assert fleet_client._read_spool(path)[0]["client_id"] == queued["client_id"]
    status, response = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "x", "body": "x"})
    assert status == 400
    assert response["code"] == "message_recipient_inactive"

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE nodes SET revoked_at=NULL WHERE node_id=?", (NODE_B,))
    assert fleet_client_messages.flush(NODE_A) == 1
    assert not path.exists()


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
    queued = fleet_client._read_spool(
        fleet_client_messages._spool_path(NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"])
    )
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

    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    first_path = fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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


def test_queued_messages_survive_home_node_identity_change(tmp_path, monkeypatch):
    current_node = "unknown"
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: current_node)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    attempts = []

    def request(_hub: str, _token: str, message: dict):
        attempts.append(message["client_id"])
        if len(attempts) == 1:
            return 503, {"error": "temporarily unavailable"}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    queued = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Queued before node identity is known",
        body="Keep the stable sender spool",
        reference=None,
        priority="normal",
    )
    original_path = fleet_client_messages._spool_path(
        current_node,
        fleet_client.load_fleet_settings()["node_token"],
        hub_url=fleet_client.load_fleet_settings()["hub_url"],
    )
    assert queued["queued"] is True
    assert original_path.is_file()

    current_node = NODE_A
    newer = fleet_client_messages.send(
        recipient=NODE_B,
        subject="Newer after node identity is known",
        body="Deliver after the older queued message",
        reference=None,
        priority="normal",
    )

    assert (
        fleet_client_messages._spool_path(
            current_node,
            fleet_client.load_fleet_settings()["node_token"],
            hub_url=fleet_client.load_fleet_settings()["hub_url"],
        )
        == original_path
    )
    assert attempts[:2] == [queued["client_id"], queued["client_id"]]
    assert attempts[2] != queued["client_id"]
    assert newer["message"]["client_id"] == attempts[2]
    assert not original_path.exists()


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


def test_message_flush_preserves_malformed_spool(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    path = fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    path.parent.mkdir(parents=True)
    original = b'{"client_id":\n'
    path.write_bytes(original)

    with pytest.raises(fleet_client.FleetClientError, match="malformed JSON"):
        fleet_client_messages.flush(NODE_A)

    assert path.read_bytes() == original


def test_flush_reports_spools_scoped_to_replaced_credentials(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-token-b", "admin_token": ""},
    )
    old_path = fleet_client_messages._spool_path(
        NODE_A, "node-token-a", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    from brigade.fleet_client_message_credentials import MessageAuthority

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://hub.invalid", node_token="node-token", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    message_authority = MessageAuthority.capture()
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    path = fleet_client_messages._spool_path(NODE_A, "node-token", hub_url="https://hub.invalid")
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

    delivered = fleet_client_messages._flush_locked(path, message_authority)

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


def test_spool_flush_skips_recipient_quota_without_reordering_that_recipient(tmp_path, monkeypatch):
    from brigade.fleet_client_message_credentials import MessageAuthority

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://hub.invalid", node_token="node-token", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    message_authority = MessageAuthority.capture()
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    path = fleet_client_messages._spool_path(NODE_A, "node-token", hub_url="https://hub.invalid")
    now = datetime.now(timezone.utc).isoformat()
    messages = [
        {"action": "send", "to": NODE_B, "client_id": "blocked-1", "enqueued_at": now},
        {"action": "send", "to": NODE_A, "client_id": "deliver-1", "enqueued_at": now},
        {"action": "send", "to": NODE_B, "client_id": "blocked-2", "enqueued_at": now},
    ]
    with fleet_client._spool_lock(path) as dir_fd:
        for message in messages:
            assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, *, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS):
        attempts.append(message["client_id"])
        if message["to"] == NODE_B:
            return 429, {
                "error": "recipient quota exceeded",
                "code": "message_recipient_quota",
                "recipient": NODE_B,
            }
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    assert fleet_client_messages._flush_locked(path, message_authority) == 1

    assert attempts == ["blocked-1", "deliver-1"]
    assert [message["client_id"] for message in fleet_client._read_spool(path)] == ["blocked-1", "blocked-2"]


def test_spool_flush_resolves_reply_before_blocked_recipient_delivery(tmp_path, monkeypatch):
    from brigade.fleet_client_message_credentials import MessageAuthority

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://hub.invalid", node_token="node-token", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    message_authority = MessageAuthority.capture()
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    path = fleet_client_messages._spool_path(NODE_A, "node-token", hub_url="https://hub.invalid")
    now = datetime.now(timezone.utc).isoformat()
    messages = [
        {"action": "send", "to": NODE_B, "client_id": "blocked-send", "enqueued_at": now},
        {"action": "reply", "id": 77, "client_id": "blocked-reply", "enqueued_at": now},
    ]
    with fleet_client._spool_lock(path) as dir_fd:
        for message in messages:
            assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, *, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS):
        attempts.append(message["action"])
        if message["action"] == "send":
            return 429, {
                "error": "recipient quota exceeded",
                "code": "message_recipient_quota",
                "recipient": NODE_B,
            }
        if message["action"] == "list":
            return 200, {"messages": [{"id": 77, "sender": NODE_B}], "next_cursor": 77}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    assert fleet_client_messages._flush_locked(path, message_authority) == 0

    assert attempts == ["send", "list"]
    assert [message["client_id"] for message in fleet_client._read_spool(path)] == ["blocked-send", "blocked-reply"]


def test_spool_reply_recipient_lookup_obeys_drain_deadline_and_batch_limit(tmp_path, monkeypatch):
    from brigade.fleet_client_message_credentials import MessageAuthority

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://hub.invalid", node_token="node-token", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    message_authority = MessageAuthority.capture()
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client_messages, "monotonic", iter((0.0, 0.0, 0.0, 11.0)).__next__)
    path = fleet_client_messages._spool_path(NODE_A, "node-token", hub_url="https://hub.invalid")
    now = datetime.now(timezone.utc).isoformat()
    messages = [
        {"action": "send", "to": NODE_B, "client_id": "blocked-send", "enqueued_at": now},
        {"action": "reply", "id": 77, "client_id": "blocked-reply-1", "enqueued_at": now},
        {"action": "reply", "id": 78, "client_id": "blocked-reply-2", "enqueued_at": now},
    ]
    with fleet_client._spool_lock(path) as dir_fd:
        for message in messages:
            assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, **kwargs):
        attempts.append((message["action"], kwargs.get("timeout")))
        if message["action"] == "send":
            return 429, {
                "error": "recipient quota exceeded",
                "code": "message_recipient_quota",
                "recipient": NODE_B,
            }
        if message["action"] == "list":
            next_cursor = message["after"] + 1
            return 200, {"messages": [{"id": next_cursor, "sender": NODE_B}], "next_cursor": next_cursor}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    assert fleet_client_messages._flush_locked(path, message_authority, max_messages=2) == 0

    assert attempts == [("send", None), ("list", 1.0)]
    assert [message["client_id"] for message in fleet_client._read_spool(path)] == [
        "blocked-send",
        "blocked-reply-1",
        "blocked-reply-2",
    ]


def test_spool_drops_reply_when_parent_is_gone_and_continues_other_recipients(tmp_path, monkeypatch):
    from brigade.fleet_client_message_credentials import MessageAuthority

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://hub.invalid", node_token="node-token", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    message_authority = MessageAuthority.capture()
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    path = fleet_client_messages._spool_path(NODE_A, "node-token", hub_url="https://hub.invalid")
    now = datetime.now(timezone.utc).isoformat()
    messages = [
        {"action": "send", "to": NODE_B, "client_id": "inactive-send", "enqueued_at": now},
        {"action": "reply", "id": 77, "client_id": "orphan-reply", "enqueued_at": now},
        {"action": "send", "to": NODE_A, "client_id": "deliver-send", "enqueued_at": now},
    ]
    with fleet_client._spool_lock(path) as dir_fd:
        for message in messages:
            assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, **_kwargs):
        attempts.append(message["action"])
        if message.get("client_id") == "inactive-send":
            return 400, {
                "error": "recipient is inactive",
                "code": "message_recipient_inactive",
                "recipient": NODE_B,
            }
        if message["action"] == "list":
            return 200, {"messages": [], "next_cursor": 76}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    assert fleet_client_messages._flush_locked(path, message_authority) == 1

    assert attempts == ["send", "list", "send"]
    assert [message["client_id"] for message in fleet_client._read_spool(path)] == ["inactive-send"]


def test_new_send_does_not_probe_unresolved_replies_after_spool_batch_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(fleet_client_messages, "MESSAGE_BATCH_SIZE", 1)
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    now = datetime.now(timezone.utc).isoformat()
    messages = [
        {"action": "send", "to": NODE_B, "client_id": "old-send", "enqueued_at": now},
        {"action": "reply", "id": 77, "client_id": "old-reply", "enqueued_at": now},
    ]
    with fleet_client._spool_lock(path) as dir_fd:
        for message in messages:
            assert fleet_client._spool_append_locked(path, message, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, **_kwargs):
        attempts.append(message["action"])
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    result = fleet_client_messages.send(
        recipient=NODE_A,
        subject="New message",
        body="Keep the batch bounded",
        reference=None,
        priority="normal",
    )

    assert result["queued"] is True
    assert attempts == ["send"]
    assert [message["client_id"] for message in fleet_client._read_spool(path)] == [
        "old-reply",
        result["client_id"],
    ]


@pytest.mark.parametrize(("recipient", "queued"), [(NODE_B, True), (NODE_A, False)])
def test_new_send_only_waits_behind_blocked_messages_for_its_recipient(tmp_path, monkeypatch, recipient, queued):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    old = {
        "action": "send",
        "to": NODE_B,
        "subject": "Blocked recipient",
        "body": "Wait for quota",
        "client_id": "blocked-old",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, old, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, *, timeout=fleet_client.FLUSH_TIMEOUT_SECONDS):
        attempts.append(message["client_id"])
        if message["client_id"] == "blocked-old":
            return 429, {
                "error": "recipient quota exceeded",
                "code": "message_recipient_quota",
                "recipient": NODE_B,
            }
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    result = fleet_client_messages.send(
        recipient=recipient,
        subject="New message",
        body="Keep unrelated recipients moving",
        reference=None,
        priority="normal",
    )

    assert bool(result.get("queued")) is queued
    assert attempts == (["blocked-old"] if queued else ["blocked-old", result["message"]["client_id"]])
    pending = fleet_client._read_spool(path)
    assert pending[0]["client_id"] == "blocked-old"
    assert len(pending) == (2 if queued else 1)


@pytest.mark.parametrize(("reply_recipient", "queued"), [(NODE_B, True), (NODE_A, False)])
def test_new_reply_waits_only_behind_blocked_messages_for_its_recipient(tmp_path, monkeypatch, reply_recipient, queued):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    path = fleet_client_messages._spool_path(
        NODE_A, "node-secret", hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    old = {
        "action": "send",
        "to": NODE_B,
        "subject": "Blocked recipient",
        "body": "Wait for quota",
        "client_id": "blocked-old",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    with fleet_client._spool_lock(path) as dir_fd:
        assert fleet_client._spool_append_locked(path, old, dir_fd=dir_fd)
    attempts = []

    def request(_hub, _token, message, **_kwargs):
        attempts.append(message["action"])
        if message.get("client_id") == "blocked-old":
            return 429, {
                "error": "recipient quota exceeded",
                "code": "message_recipient_quota",
                "recipient": NODE_B,
            }
        if message["action"] == "list":
            return 200, {"messages": [{"id": 77, "sender": reply_recipient}], "next_cursor": 77}
        return 201, {"message": {"client_id": message["client_id"]}}

    monkeypatch.setattr(fleet_client_messages, "_request", request)

    result = fleet_client_messages.reply(77, body="Reply", subject="Re: task", reference=None, priority="normal")

    assert bool(result.get("queued")) is queued
    assert attempts == (["send", "list"] if queued else ["send", "list", "reply"])
    pending = fleet_client._read_spool(path)
    assert pending[0]["client_id"] == "blocked-old"
    assert len(pending) == (2 if queued else 1)


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

    pending = fleet_client._read_spool(
        fleet_client_messages._spool_path(
            NODE_A,
            fleet_client.load_fleet_settings()["node_token"],
            hub_url=fleet_client.load_fleet_settings()["hub_url"],
        )
    )
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


@pytest.mark.skipif(sys.platform == "win32", reason="Locked directory descriptors are POSIX-only; Windows uses paths")
def test_ordered_send_uses_the_locked_directory_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://hub.invalid", node_token="node-secret", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    spool = fleet_client_messages._spool_path(NODE_A, "node-secret", hub_url="https://hub.invalid")
    spool.parent.mkdir(parents=True)
    older = {"action": "send", "client_id": "older", "enqueued_at": datetime.now(timezone.utc).isoformat()}
    newer = {"action": "send", "client_id": "newer"}
    fleet_client._write_spool_atomic(spool, [older])
    attempts = []

    def request(_hub: str, _token: str, message: dict):
        attempts.append(message["client_id"])
        return 201, {"message": {"client_id": message["client_id"]}}

    real_exists = Path.exists
    real_is_symlink = Path.is_symlink

    def reject_path_probe(path):
        if path == spool:
            raise AssertionError("spool checks must use the locked directory descriptor")
        return real_exists(path)

    def reject_symlink_probe(path):
        if path == spool:
            raise AssertionError("spool checks must use the locked directory descriptor")
        return real_is_symlink(path)

    monkeypatch.setattr(Path, "exists", reject_path_probe)
    monkeypatch.setattr(Path, "is_symlink", reject_symlink_probe)
    monkeypatch.setattr(fleet_client_messages, "_request", request)

    fleet_client_messages._send_ordered(newer, credentials.MessageAuthority.capture())

    assert attempts == ["older", "newer"]


def test_new_send_bounds_opportunistic_spool_drain(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    spool = fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    spool = fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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

    spool = fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    ids=["body-too-long", "empty-subject", "subject-too-long", "ref-too-long", "priority", "zero-id", "large-id"],
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
    assert not fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ).exists()


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


@pytest.mark.parametrize("failure_phase", ["operation", "after_output"])
def test_msg_reports_spool_oserrors_without_tracebacks(capsys, failure_phase):
    def operation():
        if failure_phase == "operation":
            raise OSError("message spool is read-only")
        return {}

    def after_output():
        if failure_phase == "after_output":
            raise OSError("principal spool finalization failed")

    args = type("Args", (), {"json": False})()
    assert fleet_messages_cli._result(args, operation, after_output=after_output) == 1

    captured = capsys.readouterr()
    expected_output = "" if failure_phase == "operation" else "{}\n"
    assert captured.out == expected_output
    assert "error:" in captured.err
    assert "OSError" not in captured.err


def test_principal_add_retry_state_finalizes_only_after_successful_output(monkeypatch):
    args = type("Args", (), {"json": True, "name": "researcher", "label": None})()
    original_print = print
    events = []
    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: events.append("output"))
    monkeypatch.setattr(
        fleet_client_messages,
        "manage_principal",
        lambda *_args, **kwargs: (
            kwargs["_deferred_request_ids"].append("enrollment-generation"),
            kwargs["_deferred_paths"].append(Path("fixture-pending.jsonl")),
            events.append(("managed", kwargs.get("_defer_finalize"))),
            {"principal": {"token": "one-time-token"}},
        )[-1],
    )
    monkeypatch.setattr(
        fleet_client_messages,
        "finalize_principal_add",
        lambda _name, *, request_id, pending_path: events.append(("finalized", request_id)),
    )

    assert fleet_messages_cli._principal_add(args) == 0
    assert events == [("managed", True), "output", ("finalized", "enrollment-generation")]

    events.clear()

    def fail_output(*_args, **_kwargs):
        raise BrokenPipeError("consumer closed stdout")

    monkeypatch.setattr("builtins.print", fail_output)
    with pytest.raises(BrokenPipeError, match="consumer closed stdout"):
        fleet_messages_cli._principal_add(args)
    assert events == [("managed", True)]

    class FlushFails:
        def write(self, value):
            events.append(("write", value))
            return len(value)

        def flush(self):
            raise BrokenPipeError("stdout flush failed")

    events.clear()
    monkeypatch.setattr("builtins.print", original_print)
    monkeypatch.setattr(fleet_messages_cli.sys, "stdout", FlushFails())
    with pytest.raises(BrokenPipeError, match="stdout flush failed"):
        fleet_messages_cli._principal_add(args)
    assert events[0] == ("managed", True)
    assert events[1][0] == "write"
    assert all(event != "finalized" for event in events)


def test_spool_rejection_log_escapes_hub_control_characters(tmp_path, monkeypatch, caplog):
    from brigade.fleet_client_message_credentials import MessageAuthority

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: dict(hub_url="https://hub.invalid", node_token="node-token", admin_token=""),
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    message_authority = MessageAuthority.capture()
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    path = fleet_client_messages._spool_path(NODE_A, "node-token", hub_url="https://hub.invalid")
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

    fleet_client_messages._flush_locked(path, message_authority)

    assert "\x1b" not in caplog.text
    assert "rejected\\x1b[31mmessage" not in caplog.text
    assert "hub_rejected" in caplog.text


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
    spool = fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    spool.parent.mkdir(mode=0o700)
    target = tmp_path / "outside.jsonl"
    target.write_text("keep", encoding="utf-8")
    spool.symlink_to(target)
    attempts = []

    def offline(*args):
        attempts.append(args)
        raise fleet_client.FleetClientError("offline")

    monkeypatch.setattr(fleet_client_messages, "_request", offline)
    error = "spool is not a regular file" if sys.platform == "win32" else "could not be persisted"
    with pytest.raises(fleet_client.FleetClientError, match=error):
        fleet_client_messages.send(
            recipient=NODE_B,
            subject="Unsafe spool",
            body="Do not claim this was queued",
            reference=None,
            priority="normal",
        )
    assert attempts == []
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
    assert not fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ).exists()


def test_flush_discards_messages_older_than_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "node-secret", "admin_token": ""},
    )
    spool = fleet_client_messages._spool_path(
        NODE_A, fleet_client.load_fleet_settings()["node_token"], hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
    enroll = parser.parse_args(["fleet", "msg", "principals", "add", "dot"])
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
        if body["action"] == "add":
            payload["principal"]["token"] = body["token"]
            payload["principal"]["enrollment_id"] = body["request_id"]
        elif body["action"] in ("send", "reply"):
            payload = {"message": {"client_id": body["client_id"]}}
        elif body["action"] == "list":
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
        expected_requests = 2 if action in ("add", "revoke") else 1
        assert len(requests) == len(opened) == expected_requests
        token = "node-token" if action in ("send", "reply", "list") else ADMIN
        assert requests[0].get_header("Authorization") == f"Bearer {token}"
    if action in ("send", "reply"):
        assert not fleet_client_messages._spool_path(
            NODE_A, "node-token", hub_url=fleet_client.load_fleet_settings()["hub_url"]
        ).exists()


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
    add_requests = []
    original_open = fleet_client._hub_open
    original_deadline = fleet_client._run_with_deadline
    last_action = None

    def hub_open(request, **kwargs):
        nonlocal last_action
        body = json.loads(request.data)
        last_action = body["action"]
        if body["action"] == "get":
            return original_open(request, **kwargs)
        requests.append(body)
        add_requests.append(body)
        if conflict and len(add_requests) == 2:
            raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, BytesIO(b'{"error":"conflict"}'))
        return original_open(request, **kwargs)

    def lose_first_response(request, *, timeout):
        result = original_deadline(request, timeout=timeout)
        if last_action == "add" and len(add_requests) == 1:
            raise TimeoutError("response lost after commit")
        return result

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lose_first_response)
    with pytest.raises(fleet_client.FleetClientError, match="TimeoutError"):
        fleet_client_messages.manage_principal("add", name="recoverable", label=label)
    path = fleet_client_messages._principal_pending_path(
        "recoverable", ADMIN, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    pending = fleet_client._read_spool(path)
    if conflict:
        with pytest.raises(fleet_client.FleetClientError, match="conflict"):
            fleet_client_messages.manage_principal("add", name="recoverable", label="Changed label")
        assert fleet_client._read_spool(path) == pending
    recovered = fleet_client_messages.manage_principal("add", name="recoverable", label="Changed again")
    assert all(request == add_requests[0] for request in add_requests)
    assert recovered["principal"]["token"] == add_requests[0]["token"]
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
        if body["action"] == "get":
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, BytesIO(b'{"error":"missing"}'))
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
                "enrollment_id": body["request_id"],
            }
        }
        return Response(json.dumps(response).encode())

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    with pytest.raises(fleet_client.FleetClientError, match="invalid label"):
        fleet_client_messages.manage_principal("add", name="researcher", label="")
    path = fleet_client_messages._principal_pending_path(
        "researcher", ADMIN, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
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
        if body["action"] == "get":
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, BytesIO(b'{"error":"missing"}'))
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
                        "enrollment_id": body["request_id"],
                    }
                }
            ).encode()
        )

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    with pytest.raises(fleet_client.FleetClientError, match="run principals add again"):
        fleet_client_messages.manage_principal("add", name="researcher", label="Current label")
    first_request_id = requests[0]["request_id"]
    enrolled = fleet_client_messages.manage_principal("add", name="researcher", label="Current label")

    assert len(requests) == 2
    assert first_request_id != requests[1]["request_id"]
    assert requests[1]["label"] == "Current label"
    assert enrolled["principal"]["token"] == requests[1]["token"]
    assert not fleet_client_messages._principal_pending_path(
        "researcher", ADMIN, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    ).exists()


def test_principal_revoke_clears_local_pending_enrollment(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ADMIN},
    )
    pending_path = fleet_client_messages._principal_pending_path(
        "researcher", ADMIN, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    pending = fleet_client_messages._pending_principal_request(
        "researcher", ADMIN, None, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    generation = pending["request_id"]

    class Response(BytesIO):
        status = 200

    monkeypatch.setattr(
        fleet_client,
        "_hub_open",
        lambda *_args, **_kwargs: Response(
            json.dumps(
                {
                    "principal": {
                        "principal_id": "agent:researcher",
                        "name": "researcher",
                        "enrollment_id": generation,
                    }
                }
            ).encode()
        ),
    )
    fleet_client_messages.manage_principal("revoke", name="researcher")

    assert not pending_path.exists()


def test_principal_revoke_preserves_concurrent_replacement_add(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "brigade_home", lambda: tmp_path)
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://hub.invalid", "node_token": "", "admin_token": ADMIN},
    )
    add_path = fleet_client_messages._principal_pending_path(
        "researcher", ADMIN, hub_url=fleet_client.load_fleet_settings()["hub_url"]
    )
    add_path.parent.mkdir(parents=True)
    replacement = {
        "name": "researcher",
        "request_id": "b" * 32,
        "expected_enrollment_id": "a" * 32,
    }
    fleet_client._write_spool_atomic(add_path, [replacement])

    def principal_request(_hub, _admin, body):
        if body["action"] == "get":
            principal = {
                "principal_id": "agent:researcher",
                "name": "researcher",
                "enrollment_id": "a" * 32,
            }
            return 200, {"principal": principal}
        assert body["expected_enrollment_id"] == "a" * 32
        return 200, {
            "principal": {
                "principal_id": "agent:researcher",
                "name": "researcher",
                "enrollment_id": body["expected_enrollment_id"],
            }
        }

    monkeypatch.setattr(fleet_client_messages, "_principal_admin_request", principal_request)
    fleet_client_messages.manage_principal("revoke", name="researcher")

    assert fleet_client._read_spool(add_path) == [replacement]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"principal": None},
        {"principal": []},
        {"principal": {}},
        {"principal": {"principal_id": "agent:other", "name": "researcher"}},
        {"principal": {"principal_id": "agent:researcher", "name": "other"}},
        {
            "principal": {
                "principal_id": "agent:researcher",
                "name": "researcher",
                "enrollment_id": "b" * 32,
            }
        },
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

    calls = 0

    def hub_open(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        body = json.dumps(
            {
                "principal": {
                    "principal_id": "agent:researcher",
                    "name": "researcher",
                    "enrollment_id": "a" * 32,
                }
            }
            if calls == 1
            else payload
        ).encode()
        return Response(body)

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    with pytest.raises(fleet_client.FleetClientError, match="did not confirm the revoked principal generation"):
        fleet_client_messages.manage_principal("revoke", name="researcher")


def test_message_reads_forward_explicit_timeouts(monkeypatch):
    seen = []

    def request(hub, token, body, *, timeout=None):
        seen.append((body["action"], timeout))
        if body["action"] == "ack":
            return 200, {"message": {"id": body["id"]}}
        return 200, {"messages": [], "next_cursor": body.get("after", 0)}

    monkeypatch.setattr(fleet_client_messages, "_request", request)
    monkeypatch.setattr(
        credentials, "message_settings", lambda token_file=None: ("https://hub.example.test", "token", "node")
    )
    fleet_client_messages.list_messages(after=0, timeout=1.5)
    fleet_client_messages.ack(7, timeout=0.5)
    fleet_client_messages.list_messages(after=0)
    # Omitting the timeout keeps the client's default transport timeout.
    assert seen == [("list", 1.5), ("ack", 0.5), ("list", None)]


@pytest.mark.parametrize("lock_timeout", [None, 0, 0.25])
def test_flush_credential_forwards_optional_lock_timeout(tmp_path, monkeypatch, lock_timeout):
    from contextlib import contextmanager

    path = tmp_path / "messages.jsonl"
    path.touch()
    calls = []
    monkeypatch.setattr(credentials, "message_settings", lambda *args: ("https://hub.example.test", "x" * 64, "node-a"))
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.test"})
    monkeypatch.setattr(fleet_client_messages, "_spool_path", lambda *args, **kwargs: path)
    monkeypatch.setattr(fleet_client_messages, "_warn_inactive_spools", lambda *args: None)
    monkeypatch.setattr(fleet_client_messages, "_flush_locked", lambda *args, **kwargs: 7)

    @contextmanager
    def lock(path, *, timeout=None):
        calls.append(timeout)
        yield None

    monkeypatch.setattr(fleet_client, "_spool_lock", lock)
    assert fleet_client_messages.flush_credential(lock_timeout=lock_timeout) == 7
    assert calls == [lock_timeout]


@pytest.mark.parametrize("lock_timeout", [-1, float("nan"), float("inf"), True])
@pytest.mark.parametrize("credential", [True, False])
def test_flush_rejects_invalid_lock_timeout_before_settings(monkeypatch, lock_timeout, credential):
    def forbidden():
        pytest.fail("settings accessed before timeout validation")

    monkeypatch.setattr(credentials, "message_settings", forbidden)
    monkeypatch.setattr(fleet_client, "load_fleet_config", forbidden)
    with pytest.raises(fleet_client.FleetClientError, match="^invalid fleet spool lock timeout$"):
        if credential:
            fleet_client_messages.flush_credential(lock_timeout=lock_timeout)
        else:
            fleet_client_messages.flush("node-a", lock_timeout=lock_timeout)


def _wait_for_active_waiters(recipient: str, expected: int, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with fleet_hub_messages._WAKE_LOCK:
            if fleet_hub_messages._ACTIVE_WAITERS.get(recipient, 0) == expected:
                return
        time.sleep(0.005)
    with fleet_hub_messages._WAKE_LOCK:
        actual = fleet_hub_messages._ACTIVE_WAITERS.get(recipient, 0)
    raise AssertionError(f"expected {expected} active waiter(s) for {recipient}, found {actual}")


def test_list_wait_wakes_promptly_on_send_to_same_recipient(message_hub):
    base, token_a, token_b, _db = message_hub
    results: list[tuple[int, dict]] = []

    def waiter() -> None:
        results.append(_request(base, token_b, {"action": "list", "after": 0, "unacked": True, "wait_ms": 5000}))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _wait_for_active_waiters(NODE_B, 1)

    send_status, sent = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_B, "subject": "Wake", "body": "Deliver immediately"},
    )
    send_finished = time.monotonic()
    assert send_status == 201
    thread.join(timeout=2.0)
    wake_elapsed = time.monotonic() - send_finished

    assert not thread.is_alive()
    assert wake_elapsed < 2.0
    assert len(results) == 1
    list_status, inbox = results[0]
    assert list_status == 200
    assert inbox["messages"] == [sent["message"]]
    assert inbox["next_cursor"] == sent["message"]["id"]


def test_list_wait_ignores_send_to_different_recipient(message_hub):
    base, _token_a, token_b, _db = message_hub
    results: list[tuple[int, dict, float]] = []

    def waiter() -> None:
        started = time.monotonic()
        status, payload = _request(base, token_b, {"action": "list", "after": 0, "unacked": True, "wait_ms": 250})
        results.append((status, payload, time.monotonic() - started))

    # Establish an epoch even if the short waiter expires during the send.
    fleet_hub_messages._notify_recipient(NODE_B)
    before_epoch = fleet_hub_messages._wake_epoch(NODE_B)
    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _wait_for_active_waiters(NODE_B, 1)

    send_status, _sent = _request(
        base,
        token_b,
        {"action": "send", "to": NODE_A, "subject": "Unrelated", "body": "Do not wake NODE_B"},
    )
    assert send_status == 201
    # HTTP latency can exceed the 250 ms deadline, especially on Windows.
    # A completed timeout is valid, but a notification for NODE_B is not.
    assert fleet_hub_messages._wake_epoch(NODE_B) == before_epoch

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    status, inbox, elapsed = results[0]
    assert status == 200
    assert inbox == {"messages": [], "next_cursor": 0}
    assert elapsed >= 0.2


def test_list_wait_timeout_returns_empty_after_wait_ms(message_hub):
    base, _token_a, token_b, _db = message_hub
    started = time.monotonic()
    status, inbox = _request(base, token_b, {"action": "list", "after": 7, "unacked": True, "wait_ms": 150})
    elapsed = time.monotonic() - started

    assert status == 200
    assert inbox == {"messages": [], "next_cursor": 7}
    assert 0.12 <= elapsed < 1.5


@pytest.mark.parametrize("bad_wait_ms", [True, False, -1, 20001, 1.5, "100", None, [], {}])
def test_list_wait_validation_rejects_bad_values(message_hub, monkeypatch, bad_wait_ms):
    base, _token_a, token_b, _db = message_hub
    status, payload = _request(base, token_b, {"action": "list", "after": 0, "wait_ms": bad_wait_ms})
    assert status == 400
    assert "wait_ms" in payload["error"]

    monkeypatch.setattr(
        fleet_client_messages,
        "_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid wait_ms reached transport")),
    )
    with pytest.raises(fleet_client.FleetClientError, match="wait_ms"):
        fleet_client_messages.list_messages(after=0, wait_ms=bad_wait_ms)


def test_list_wait_closes_db_connection_while_waiting_and_notifies_after_commit(message_hub, monkeypatch):
    base, token_a, token_b, db = message_hub
    lock = threading.Lock()
    opened_connections: list[sqlite3.Connection] = []
    active_count = 0

    class TrackingConnection(sqlite3.Connection):
        def close(self) -> None:
            nonlocal active_count
            if not getattr(self, "_tracked_closed", False):
                self._tracked_closed = True
                with lock:
                    active_count -= 1
            super().close()

    def tracking_open_db(path: Path) -> sqlite3.Connection:
        nonlocal active_count
        resolved = Path(path).expanduser().resolve()
        conn = sqlite3.connect(
            f"{resolved.as_uri()}?mode=rw",
            timeout=10,
            uri=True,
            factory=TrackingConnection,
        )
        conn.execute("PRAGMA busy_timeout=5000")
        with lock:
            active_count += 1
            opened_connections.append(conn)
        return conn

    monkeypatch.setattr(fleet_hub, "open_db", tracking_open_db)
    results: list[tuple[int, dict]] = []

    def waiter() -> None:
        results.append(_request(base, token_b, {"action": "list", "after": 0, "unacked": True, "wait_ms": 3000}))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _wait_for_active_waiters(NODE_B, 1)

    with lock:
        assert len(opened_connections) == 1
        assert active_count == 0
        first_conn = opened_connections[0]
    with pytest.raises(sqlite3.ProgrammingError):
        first_conn.execute("SELECT 1")

    # A failed commit in send() must not notify the waiting recipient.
    class FailingCommitConnection(sqlite3.Connection):
        def commit(self) -> None:
            if getattr(self, "_fail_commit", False):
                raise sqlite3.OperationalError("simulated commit failure")
            super().commit()

    failing_conn = sqlite3.connect(
        f"{Path(db).expanduser().resolve().as_uri()}?mode=rw",
        timeout=10,
        uri=True,
        factory=FailingCommitConnection,
    )
    try:
        failing_conn.execute("PRAGMA busy_timeout=5000")
        notified: list[str] = []
        original_notify = fleet_hub_messages._notify_recipient
        monkeypatch.setattr(fleet_hub_messages, "_notify_recipient", lambda r: notified.append(r))
        try:
            failing_conn._fail_commit = True
            with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
                fleet_hub_messages.send(
                    failing_conn,
                    {"to": NODE_B, "subject": "Rollback", "body": "Commit will fail"},
                    sender=NODE_A,
                )
        finally:
            failing_conn._fail_commit = False
            monkeypatch.setattr(fleet_hub_messages, "_notify_recipient", original_notify)
        assert notified == []
    finally:
        failing_conn.close()

    send_status, sent = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_B, "subject": "After close", "body": "Reopened connection sees me"},
    )
    assert send_status == 201
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert results[0][0] == 200
    assert results[0][1]["messages"] == [sent["message"]]
    with lock:
        assert active_count == 0
        assert len(opened_connections) >= 3


def test_list_wait_enforces_per_caller_waiter_cap(message_hub, monkeypatch):
    base, token_a, token_b, _db = message_hub
    # Keep the source budget larger so this independently tests the caller cap.
    monkeypatch.setattr(fleet_hub_messages, "MAX_WAITERS_PER_SOURCE", 3)
    waiter_results: list[tuple[int, dict]] = []

    def waiter() -> None:
        waiter_results.append(_request(base, token_b, {"action": "list", "after": 0, "unacked": True, "wait_ms": 3000}))

    threads = [threading.Thread(target=waiter, daemon=True) for _ in range(fleet_hub_messages.MAX_WAITERS_PER_CALLER)]
    for thread in threads:
        thread.start()
    _wait_for_active_waiters(NODE_B, fleet_hub_messages.MAX_WAITERS_PER_CALLER)

    started = time.monotonic()
    extra_status, extra_inbox = _request(
        base, token_b, {"action": "list", "after": 9, "unacked": True, "wait_ms": 3000}
    )
    extra_elapsed = time.monotonic() - started

    assert extra_status == 429
    assert extra_inbox["code"] == "message_wait_capped"
    assert isinstance(extra_inbox["error"], str)
    assert extra_elapsed < 2.0

    send_status, sent = _request(
        base,
        token_a,
        {"action": "send", "to": NODE_B, "subject": "Wake capped waiters", "body": "Both wake"},
    )
    assert send_status == 201
    for thread in threads:
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    assert len(waiter_results) == fleet_hub_messages.MAX_WAITERS_PER_CALLER
    for status, inbox in waiter_results:
        assert status == 200
        assert inbox["messages"] == [sent["message"]]
    _wait_for_active_waiters(NODE_B, 0)


def test_client_and_cli_list_wait_timeout_margin_cap_and_old_hub_compat(monkeypatch, capsys):
    seen: list[tuple[dict, float | None]] = []

    def fake_request(_hub: str, _token: str, body: dict, *, timeout: float | None = None):
        seen.append((dict(body), timeout))
        # Simulate an older hub that ignores wait_ms and returns immediately.
        return 200, {"messages": [], "next_cursor": body.get("after", 0)}

    monkeypatch.setattr(fleet_client_messages, "_request", fake_request)
    monkeypatch.setattr(
        credentials,
        "message_settings",
        lambda token_file=None, **_kw: ("https://hub.example.test", "token", NODE_A),
    )

    page = fleet_client_messages.list_messages(after=3, unacked=True, wait_ms=5000)
    assert page == {"messages": [], "next_cursor": 3}
    assert seen[-1][0] == {"action": "list", "after": 3, "unacked": True, "wait_ms": 5000}
    assert seen[-1][1] is not None and seen[-1][1] >= 5.0 + fleet_client.FLUSH_TIMEOUT_SECONDS

    fleet_client_messages.list_messages(after=0, wait_ms=4000, timeout=1.0)
    assert seen[-1][1] is not None and seen[-1][1] >= 4.0 + fleet_client.FLUSH_TIMEOUT_SECONDS

    fleet_client_messages.list_messages(after=0, wait_ms=4000, timeout=15.0)
    assert seen[-1][1] == 15.0

    assert cli.main(["fleet", "msg", "list", "--unacked", "--after", "2", "--wait", "1.5", "--json"]) == 0
    capsys.readouterr()
    assert seen[-1][0] == {"action": "list", "after": 2, "unacked": True, "wait_ms": 1500}

    assert cli.main(["fleet", "msg", "list", "--wait", "20", "--json"]) == 0
    capsys.readouterr()
    assert seen[-1][0] == {"action": "list", "after": 0, "unacked": False, "wait_ms": 20000}

    calls_before = len(seen)
    for excessive_wait in ("20.0001", "45"):
        assert cli.main(["fleet", "msg", "list", "--wait", excessive_wait, "--json"]) == 1
        err_payload = json.loads(capsys.readouterr().out)
        assert err_payload["code"] == "invalid_argument"
        assert "20 seconds" in err_payload["error"]
    assert len(seen) == calls_before

    assert cli.main(["fleet", "msg", "list", "--wait", "-1", "--json"]) == 1
    err_payload = json.loads(capsys.readouterr().out)
    assert err_payload["code"] == "invalid_argument"
    assert "wait" in err_payload["error"]


@pytest.mark.parametrize("cap", ["global", "source"])
def test_list_wait_caps_across_callers(message_hub, monkeypatch, cap):
    base, token_a, token_b, _db = message_hub
    if cap == "global":
        monkeypatch.setattr(fleet_hub_messages, "MAX_WAITERS", 1, raising=False)
    else:
        # The test hub is reached over loopback, which is exempt; treat it as a remote peer.
        monkeypatch.setattr(fleet_hub_messages, "_source_cap_applies", lambda _source_ip: True)
    results = []
    tokens = [token_b] if cap == "global" else [token_b, token_a]
    threads = [
        threading.Thread(
            target=lambda token=token: results.append(_request(base, token, {"action": "list", "wait_ms": 3000})),
            daemon=True,
        )
        for token in tokens
    ]
    try:
        for thread, recipient in zip(threads, [NODE_B, NODE_A], strict=False):
            thread.start()
            _wait_for_active_waiters(recipient, 1)
        status, payload = _request(base, token_a, {"action": "list", "wait_ms": 3000})
        assert status == 429
        assert payload["code"] == "message_wait_capped"
        assert isinstance(payload["error"], str)
        # Ordinary reads still work with every waiter slot occupied.
        assert _request(base, token_a, {"action": "list"}) == (200, {"messages": [], "next_cursor": 0})
    finally:
        for token, recipient in [(token_a, NODE_B), (token_b, NODE_A)]:
            assert (
                _request(base, token, {"action": "send", "to": recipient, "subject": "Release", "body": "Wake"})[0]
                == 201
            )
        for thread in threads:
            thread.join(timeout=4)
            assert not thread.is_alive()
    assert all(status == 200 for status, _payload in results)
    _wait_for_active_waiters(NODE_B, 0)
    _wait_for_active_waiters(NODE_A, 0)
    # Released source/global reservations admit a later wait.
    assert _request(base, token_b, {"action": "list", "after": 100, "wait_ms": 1}) == (
        200,
        {"messages": [], "next_cursor": 100},
    )


def test_source_cap_skips_loopback_proxy_peers():
    assert not fleet_hub_messages._source_cap_applies("127.0.0.1")
    assert not fleet_hub_messages._source_cap_applies("::1")
    assert fleet_hub_messages._source_cap_applies("100.64.0.5")
    assert fleet_hub_messages._source_cap_applies("192.0.2.10")
    assert fleet_hub_messages._source_cap_applies("")


def test_list_wait_loopback_callers_share_no_source_cap(message_hub, monkeypatch):
    base, token_a, token_b, _db = message_hub
    monkeypatch.setattr(fleet_hub_messages, "MAX_WAITERS_PER_SOURCE", 1)
    results = []
    threads = [
        threading.Thread(
            target=lambda token=token: results.append(_request(base, token, {"action": "list", "wait_ms": 3000})),
            daemon=True,
        )
        for token in (token_b, token_a)
    ]
    try:
        for thread, recipient in zip(threads, [NODE_B, NODE_A], strict=True):
            thread.start()
            _wait_for_active_waiters(recipient, 1)
    finally:
        for token, recipient in [(token_a, NODE_B), (token_b, NODE_A)]:
            assert (
                _request(base, token, {"action": "send", "to": recipient, "subject": "Release", "body": "Wake"})[0]
                == 201
            )
        for thread in threads:
            thread.join(timeout=4)
            assert not thread.is_alive()
    assert [status for status, _payload in results] == [200, 200]


def test_wait_capped_client_and_cli_exit_without_retry(message_hub, monkeypatch, capsys):
    base, _token_a, token_b, _db = message_hub
    monkeypatch.setattr(fleet_hub_messages, "MAX_WAITERS_PER_CALLER", 0)
    monkeypatch.setattr(credentials, "message_settings", lambda token_file=None, **_kw: (base, token_b, NODE_B))
    with pytest.raises(fleet_client_messages.FleetMessageHubError) as caught:
        fleet_client_messages.list_messages(wait_ms=100)
    assert caught.value.status == 429
    assert caught.value.code == "message_wait_capped"
    calls = 0
    request = fleet_client_messages._request

    def counted_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        return request(*args, **kwargs)

    monkeypatch.setattr(fleet_client_messages, "_request", counted_request)
    assert cli.main(["fleet", "msg", "list", "--wait", "1", "--json"]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["status"] == 429
    assert payload["code"] == "message_wait_capped"
    assert payload["error"] in output.err
    assert calls == 1


def test_list_wait_epoch_window_returns_message_without_sleep(message_hub, monkeypatch):
    base, token_a, token_b, _db = message_hub
    armed = threading.Event()
    armed.set()
    sent = []

    class SendOnCloseConnection(sqlite3.Connection):
        def close(self):
            super().close()
            if armed.is_set():
                armed.clear()
                sent.append(
                    _request(
                        base, token_a, {"action": "send", "to": NODE_B, "subject": "Epoch", "body": "In the window"}
                    )
                )

    def open_db(path):
        return sqlite3.connect(path, timeout=5, factory=SendOnCloseConnection)

    monkeypatch.setattr(fleet_hub, "open_db", open_db)
    started = time.monotonic()
    status, payload = _request(base, token_b, {"action": "list", "wait_ms": 4000})
    assert time.monotonic() - started < 2
    assert sent[0][0] == 201
    assert status == 200
    assert payload["messages"] == [sent[0][1]["message"]]
    _wait_for_active_waiters(NODE_B, 0)


def test_list_wait_empty_wake_waits_until_deadline(message_hub):
    base, _token_a, token_b, _db = message_hub
    results = []

    def waiter():
        started = time.monotonic()
        result = _request(base, token_b, {"action": "list", "wait_ms": 350})
        results.append((result, time.monotonic() - started))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _wait_for_active_waiters(NODE_B, 1)
    fleet_hub_messages._notify_recipient(NODE_B)
    thread.join(timeout=2)
    assert not thread.is_alive()
    result, elapsed = results[0]
    assert result == (200, {"messages": [], "next_cursor": 0})
    assert 0.3 <= elapsed < 2
    _wait_for_active_waiters(NODE_B, 0)


@pytest.mark.parametrize("failure", ["commit", "rollback"])
def test_active_principal_does_not_swallow_unexpected_programming_error(message_hub, failure):
    _base, _token_a, _token_b, db = message_hub

    class BrokenConnection(sqlite3.Connection):
        def commit(self):
            if failure == "commit":
                raise sqlite3.ProgrammingError("unexpected commit error")
            return super().commit()

        def rollback(self):
            if failure == "rollback":
                raise sqlite3.ProgrammingError("unexpected rollback error")
            return super().rollback()

    conn = sqlite3.connect(db, factory=BrokenConnection)
    try:

        def operation():
            if failure == "rollback":
                raise ValueError("trigger rollback")
            return {}

        with pytest.raises(sqlite3.ProgrammingError, match=f"unexpected {failure} error"):
            fleet_hub_messages._as_active_principal(conn, NODE_B, operation)
    finally:
        conn.close()


@pytest.mark.parametrize("failure", ["revoked", "database"])
def test_list_wait_rechecks_auth_and_releases_reservations_on_error(message_hub, monkeypatch, failure):
    base, _token_a, token_b, db = message_hub
    results = []
    thread = threading.Thread(
        target=lambda: results.append(_request(base, token_b, {"action": "list", "wait_ms": 3000})),
        daemon=True,
    )
    thread.start()
    _wait_for_active_waiters(NODE_B, 1)
    if failure == "revoked":
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE nodes SET revoked_at=? WHERE node_id=?", (datetime.now(timezone.utc).isoformat(), NODE_B)
            )
    else:

        def fail_query(*args, **kwargs):
            raise sqlite3.OperationalError("simulated requery failure")

        monkeypatch.setattr(fleet_hub_messages, "_query_inbox_rows", fail_query)
    fleet_hub_messages._notify_recipient(NODE_B)
    thread.join(timeout=2)
    assert not thread.is_alive()
    status, payload = results[0]
    if failure == "revoked":
        assert status == 403
        assert payload["code"] == "message_sender_inactive"
    else:
        assert status == 500
        assert "simulated requery failure" in payload["error"]
    with fleet_hub_messages._WAKE_LOCK:
        assert fleet_hub_messages._ACTIVE_WAITERS == {}
        assert fleet_hub_messages._SOURCE_WAITERS == {}
        assert fleet_hub_messages._WAKE_CONDITIONS == {}


def test_message_identity_is_authenticated_even_with_empty_inbox(message_hub, tmp_path, monkeypatch, capsys):
    base, token_a, _token_b, db = message_hub
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": token_a})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    with fleet_hub.init_db(db) as conn:
        principal = fleet_hub_messages.add_principal(conn, {"name": "duet", "label": "Duet fixture"})
    token = tmp_path / "message.token"
    token.write_text(principal["token"])
    token.chmod(0o600)
    assert _request(base, principal["token"], {"action": "list"})[1]["messages"] == []
    assert _request(base, principal["token"], {"action": "identity"}) == (200, {"principal": "agent:duet"})
    assert fleet_client_messages.identity(token_file=str(token)) == {"principal": "agent:duet"}
    assert cli.main(["fleet", "msg", "identity", "--token-file", str(token), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"principal": "agent:duet"}
    assert _request(base, token_a, {"action": "identity"}) == (200, {"principal": NODE_A})
    with fleet_hub.init_db(db) as conn:
        conn.execute(
            "UPDATE fleet_message_principals SET revoked_at=? WHERE principal_id=?", ("2026-01-01", "agent:duet")
        )
    with pytest.raises(fleet_client.FleetClientError):
        fleet_client_messages.identity(token_file=str(token))


def _identity_pair(db):
    conn = fleet_hub.open_db(db)
    try:
        caller = fleet_hub_messages.add_principal(conn, {"name": "caller", "may_send_to": ["agent:peer"]})
        peer = fleet_hub_messages.add_principal(conn, {"name": "peer", "may_send_to": ["agent:caller"]})
        return caller, peer
    finally:
        conn.close()


def _denied_identity_peer(principal="agent:peer"):
    return {"principal": principal, "authorized": False, "enrollment_epoch": None, "grant_epoch": None}


def test_identity_peers_exact_schema_and_bounds(message_hub):
    import re

    base, node_token, _, db = message_hub
    caller, peer = _identity_pair(db)
    status, result = _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer", "agent:unknown"]})
    assert status == 200
    assert set(result) == {"schema", "principal", "enrollment_epoch", "grant_epoch", "peers"}
    assert result["schema"] == "brigade.message_authorization.v1"
    assert result["principal"] == "agent:caller"
    assert len(result["peers"]) == 2
    authorized = result["peers"][0]
    assert set(authorized) == {"principal", "authorized", "enrollment_epoch", "grant_epoch"}
    assert authorized["principal"] == "agent:peer" and authorized["authorized"] is True
    assert result["peers"][1] == _denied_identity_peer("agent:unknown")
    epochs = [value[key] for value in (result, authorized) for key in ("enrollment_epoch", "grant_epoch")]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) for value in epochs)
    assert len(set(epochs)) == 4
    assert not set(epochs).intersection(
        {caller["token"], peer["token"], caller["enrollment_id"], peer["enrollment_id"]}
    )
    peers = [f"agent:peer{i}" for i in range(16)]
    assert _request(base, caller["token"], {"action": "identity", "peers": peers}) == (
        200,
        {**result, "peers": [_denied_identity_peer(name) for name in peers]},
    )
    assert _request(base, node_token, {"action": "identity", "peers": ["agent:peer"]}) == (
        403,
        {"error": "identity peers requires an agent principal", "code": "message_identity_peers_unsupported"},
    )
    for token in ("", "unknown-bearer"):
        assert _request(base, token, {"action": "identity", "peers": ["agent:peer"]})[0] == 401


@pytest.mark.parametrize(
    "change",
    [
        {"peers": None},
        {"peers": []},
        {"peers": [f"agent:p{i}" for i in range(17)]},
        {"peers": ["agent:peer", "agent:peer"]},
        {"peers": ["agent:caller"]},
        {"peers": [NODE_A]},
        {"peers": ["*"]},
        {"peers": ["agent:*"]},
        {"peers": ["agent:Bad"]},
        {"peers": ["agent:peer\n"]},
        {"peers": [1]},
        {"peers": "agent:peer"},
        {"peers": ["agent:" + "p" * 64]},
        {"extra": True},
    ],
)
def test_identity_peers_invalid_arguments(message_hub, change):
    base, _, _, db = message_hub
    caller, _ = _identity_pair(db)
    assert _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer"], **change}) == (
        400,
        {"error": "invalid identity arguments", "code": "invalid_argument"},
    )


@pytest.mark.parametrize(
    "denial", ["caller_grants", "peer_grants", "both_grants", "unknown", "revoked", "null", "malformed"]
)
def test_identity_mutual_grants_uniform_denial(message_hub, denial):
    from brigade import fleet_hub_message_grants as grants

    base, _, _, db = message_hub
    caller, peer = _identity_pair(db)
    conn = fleet_hub.open_db(db)
    try:
        if denial in {"caller_grants", "both_grants"}:
            grants.set_grants(conn, {"action": "grants", "name": "caller", "may_send_to": []})
        if denial in {"peer_grants", "both_grants"}:
            grants.set_grants(conn, {"action": "grants", "name": "peer", "may_send_to": []})
        if denial == "unknown":
            conn.execute("DELETE FROM fleet_message_principals WHERE name='peer'")
        if denial == "revoked":
            fleet_hub_messages.revoke_principal(
                conn, {"action": "revoke", "name": "peer", "expected_enrollment_id": peer["enrollment_id"]}
            )
        if denial in {"null", "malformed"}:
            conn.execute(
                "UPDATE fleet_message_principals SET grant_epoch=? WHERE name='peer'",
                (None if denial == "null" else "bad",),
            )
        conn.commit()
        before = conn.total_changes
        status, result = _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer"]})
        assert status == 200
        assert result["peers"] == [_denied_identity_peer()]
        assert conn.total_changes == before
        if denial in {"null", "malformed"}:
            assert conn.execute("SELECT grant_epoch FROM fleet_message_principals WHERE name='peer'").fetchone()[0] == (
                None if denial == "null" else "bad"
            )
    finally:
        conn.close()


@pytest.mark.parametrize("patterns", [["*"], ["agent:*"], ["agent:caller"]])
def test_identity_mutual_wildcard_semantics(message_hub, patterns):
    from brigade import fleet_hub_message_grants as grants

    base, _, _, db = message_hub
    caller, _ = _identity_pair(db)
    conn = fleet_hub.open_db(db)
    try:
        grants.set_grants(conn, {"action": "grants", "name": "peer", "may_send_to": patterns})
        grants.set_grants(conn, {"action": "grants", "name": "caller", "may_send_to": ["agent:*"]})
    finally:
        conn.close()
    assert (
        _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer"]})[1]["peers"][0]["authorized"]
        is True
    )


@pytest.mark.parametrize("generation", [None, "0" * 64])
def test_identity_requires_authenticated_generation(message_hub, generation):
    _, _, _, db = message_hub
    _identity_pair(db)
    conn = fleet_hub.open_db(db)
    try:
        with pytest.raises(fleet_hub_messages.MessageAuthError):
            fleet_hub_messages.handle(
                conn,
                {"action": "identity", "peers": ["agent:peer"]},
                caller="agent:caller",
                authenticated_generation=generation,
            )
        assert not conn.in_transaction
    finally:
        conn.close()


@pytest.mark.parametrize("column,value", [("authorization_enrollment_epoch", None), ("grant_epoch", "bad")])
def test_identity_caller_metadata_fails_closed_without_repair(message_hub, column, value):
    base, _, _, db = message_hub
    caller, _ = _identity_pair(db)
    conn = fleet_hub.open_db(db)
    try:
        conn.execute(f"UPDATE fleet_message_principals SET {column}=? WHERE name='caller'", (value,))
        conn.commit()
        assert _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer"]}) == (
            403,
            {"error": "sender is not an active hub principal", "code": "message_sender_inactive"},
        )
        assert conn.execute(f"SELECT {column} FROM fleet_message_principals WHERE name='caller'").fetchone()[0] == value
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ["revoke", "rotate"])
def test_identity_stale_captured_http_authentication(message_hub, monkeypatch, mutation):
    from concurrent.futures import ThreadPoolExecutor

    base, _, _, db = message_hub
    caller, _ = _identity_pair(db)
    captured, resume = threading.Event(), threading.Event()
    handle = fleet_hub_messages.handle

    def delayed(conn, raw, **kwargs):
        if raw.get("action") == "identity":
            assert kwargs["authenticated_generation"] == fleet_hub.hash_node_token(caller["token"])
            captured.set()
            assert resume.wait(5)
        return handle(conn, raw, **kwargs)

    monkeypatch.setattr(fleet_hub_messages, "handle", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_request, base, caller["token"], {"action": "identity", "peers": ["agent:peer"]})
        try:
            assert captured.wait(5)
            conn = fleet_hub.open_db(db)
            try:
                fleet_hub_messages.revoke_principal(
                    conn, {"action": "revoke", "name": "caller", "expected_enrollment_id": caller["enrollment_id"]}
                )
                if mutation == "rotate":
                    fleet_hub_messages.add_principal(
                        conn,
                        {
                            "name": "caller",
                            "expected_enrollment_id": caller["enrollment_id"],
                            "may_send_to": ["agent:peer"],
                        },
                    )
            finally:
                conn.close()
        finally:
            resume.set()
        assert future.result(timeout=5) == (
            403,
            {"error": "sender is not an active hub principal", "code": "message_sender_inactive"},
        )
    assert _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer"]})[0] == 401


@pytest.fixture
def identity_authority(tmp_path, monkeypatch):
    token = tmp_path / "identity.token"
    token.write_text("t" * 40)
    token.chmod(0o600)
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.invalid", "node_token": ""}
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    return credentials.MessageAuthority.capture(str(token))


def _identity_response():
    return {
        "schema": "brigade.message_authorization.v1",
        "principal": "agent:caller",
        "enrollment_epoch": "a" * 32,
        "grant_epoch": "b" * 32,
        "peers": [
            {"principal": "agent:peer", "authorized": True, "enrollment_epoch": "c" * 32, "grant_epoch": "d" * 32},
            _denied_identity_peer("agent:unknown"),
        ],
    }


def _identity_http_response(monkeypatch, payload, *, status=200):
    calls = []

    def hub_open(request, **kwargs):
        calls.append((request, kwargs))
        response = BytesIO(json.dumps(payload).encode())
        response.status = status
        return response

    monkeypatch.setattr(fleet_client, "_hub_open", hub_open)
    return calls


def test_identity_client_strict_validation_and_scope(identity_authority, monkeypatch):
    payload = _identity_response()
    calls = _identity_http_response(monkeypatch, payload)
    assert (
        fleet_client_messages.identity(
            peers=["agent:peer", "agent:unknown"],
            authority=identity_authority,
            expected_scope=identity_authority.scope,
            timeout=1.5,
        )
        == payload
    )
    assert len(calls) == 1
    request, kwargs = calls[0]
    assert json.loads(request.data) == {"action": "identity", "peers": ["agent:peer", "agent:unknown"]}
    assert request.get_header("Authorization") == "Bearer " + "t" * 40
    assert kwargs["timeout"] == 1.5
    with pytest.raises(credentials.MessageAuthorityError):
        fleet_client_messages.identity(peers=["agent:peer"], authority=identity_authority, expected_scope="other-scope")
    assert len(calls) == 1
    # The opaque bearer cannot establish the caller independently. The relay
    # compares this syntactically valid returned caller to its own configuration.
    payload["principal"] = "agent:another"
    assert (
        fleet_client_messages.identity(peers=["agent:peer", "agent:unknown"], authority=identity_authority)["principal"]
        == "agent:another"
    )
    identity_authority_path = Path(identity_authority.token_file)
    identity_authority_path.write_text("u" * 40)
    with pytest.raises(credentials.MessageAuthorityError):
        fleet_client_messages.identity(peers=["agent:peer"], authority=identity_authority)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "peers",
    [
        [],
        ["agent:p"] * 2,
        [f"agent:p{i}" for i in range(17)],
        [NODE_A],
        ["agent:Bad"],
        ["agent:p\n"],
        ["agent:*"],
        [None],
        "agent:peer",
    ],
)
def test_identity_client_invalid_peers_before_authority(peers, monkeypatch):
    def capture(*args, **kwargs):
        pytest.fail("invalid peers reached credential capture")

    monkeypatch.setattr(credentials.MessageAuthority, "capture", capture)
    with pytest.raises(fleet_client.FleetClientError, match="identity"):
        fleet_client_messages.identity(peers=peers)


@pytest.mark.parametrize(
    "mutation",
    [
        "basic",
        "schema",
        "principal_type",
        "principal_node",
        "self",
        "top_extra",
        "top_missing",
        "caller_null",
        "caller_short",
        "caller_long",
        "caller_newline",
        "records_type",
        "zero",
        "seventeen",
        "reorder",
        "duplicate",
        "omit",
        "substitute",
        "extra",
        "record_extra",
        "record_missing",
        "integer_bool",
        "null_bool",
        "true_null",
        "false_epoch",
        "peer_short",
        "peer_long",
        "peer_newline",
    ],
)
def test_identity_client_rejects_malformed_success(identity_authority, monkeypatch, mutation):
    payload = _identity_response()
    peer = payload["peers"][0]
    if mutation == "basic":
        payload = {"principal": "agent:caller"}
    elif mutation == "schema":
        payload["schema"] = "brigade.message_authorization.v0"
    elif mutation == "principal_type":
        payload["principal"] = 1
    elif mutation == "principal_node":
        payload["principal"] = NODE_A
    elif mutation == "self":
        payload["principal"] = "agent:peer"
    elif mutation == "top_extra":
        payload["extra"] = True
    elif mutation == "top_missing":
        del payload["grant_epoch"]
    elif mutation == "caller_null":
        payload["enrollment_epoch"] = None
    elif mutation == "caller_short":
        payload["grant_epoch"] = "a" * 15
    elif mutation == "caller_long":
        payload["grant_epoch"] = "a" * 129
    elif mutation == "caller_newline":
        payload["enrollment_epoch"] += "\n"
    elif mutation == "records_type":
        payload["peers"] = {}
    elif mutation == "zero":
        payload["peers"] = []
    elif mutation == "seventeen":
        payload["peers"] *= 9
    elif mutation == "reorder":
        payload["peers"].reverse()
    elif mutation == "duplicate":
        payload["peers"][1] = peer.copy()
    elif mutation == "omit":
        payload["peers"].pop()
    elif mutation == "substitute":
        peer["principal"] = "agent:other"
    elif mutation == "extra":
        payload["peers"].append(_denied_identity_peer("agent:other"))
    elif mutation == "record_extra":
        peer["patterns"] = ["*"]
    elif mutation == "record_missing":
        del peer["grant_epoch"]
    elif mutation == "integer_bool":
        peer["authorized"] = 1
    elif mutation == "null_bool":
        peer["authorized"] = None
    elif mutation == "true_null":
        peer["grant_epoch"] = None
    elif mutation == "false_epoch":
        payload["peers"][1]["enrollment_epoch"] = "e" * 32
    elif mutation == "peer_short":
        peer["enrollment_epoch"] = "e" * 15
    elif mutation == "peer_long":
        peer["enrollment_epoch"] = "e" * 129
    elif mutation == "peer_newline":
        peer["grant_epoch"] += "\n"
    calls = _identity_http_response(monkeypatch, payload)
    with pytest.raises(fleet_client.FleetClientError, match="identity"):
        fleet_client_messages.identity(peers=["agent:peer", "agent:unknown"], authority=identity_authority)
    assert len(calls) == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_identity_client_timeout_is_finite(identity_authority, monkeypatch, timeout):
    calls = _identity_http_response(monkeypatch, _identity_response())
    with pytest.raises(fleet_client.FleetClientError):
        fleet_client_messages.identity(peers=["agent:peer"], authority=identity_authority, timeout=timeout)
    assert calls == []


def test_identity_client_typed_failure_and_byte_bound(identity_authority, monkeypatch):
    calls = _identity_http_response(monkeypatch, {"code": "message_sender_inactive"}, status=403)
    with pytest.raises(credentials.FleetMessageHubError) as exc:
        fleet_client_messages.identity(peers=["agent:peer"], authority=identity_authority)
    assert exc.value.status == 403 and exc.value.code == "message_sender_inactive"
    assert len(calls) == 1
    calls = _identity_http_response(monkeypatch, {"padding": "x" * fleet_client_messages.MESSAGE_RESPONSE_LIMIT})
    with pytest.raises(fleet_client.FleetClientError):
        fleet_client_messages.identity(peers=["agent:peer"], authority=identity_authority)
    assert len(calls) == 1


def test_identity_client_real_http_contract(message_hub, tmp_path, monkeypatch):
    base, token_a, _, db = message_hub
    caller, _ = _identity_pair(db)
    token = tmp_path / "caller.token"
    token.write_text(caller["token"])
    token.chmod(0o600)
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": token_a})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    authority = credentials.MessageAuthority.capture(str(token))
    result = fleet_client_messages.identity(
        peers=["agent:peer"], authority=authority, expected_scope=authority.scope, timeout=2
    )
    assert result["principal"] == "agent:caller"
    assert result["peers"][0]["authorized"] is True
    assert fleet_client_messages.identity(authority=authority) == {"principal": "agent:caller"}
    assert fleet_client_messages.identity() == {"principal": NODE_A}


@pytest.mark.parametrize("mutation", ["caller_grants", "peer_grants", "revoke", "reenroll"])
def test_identity_snapshot_coherence(message_hub, monkeypatch, mutation):
    from concurrent.futures import ThreadPoolExecutor

    from brigade import fleet_hub_message_grants as grants

    base, _, _, db = message_hub
    caller, peer = _identity_pair(db)
    before = _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer"]})
    assert before[0] == 200 and before[1]["peers"][0]["authorized"] is True
    reached, resume = threading.Event(), threading.Event()
    grants_for = grants.grants_for

    def paused(conn, principal):
        patterns = grants_for(conn, principal)
        if principal == "agent:caller" and threading.current_thread().name != "MainThread":
            reached.set()
            assert resume.wait(5)
        return patterns

    monkeypatch.setattr(grants, "grants_for", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_request, base, caller["token"], {"action": "identity", "peers": ["agent:peer"]})
        try:
            assert reached.wait(5)
            conn = fleet_hub.open_db(db)
            try:
                if mutation in {"caller_grants", "peer_grants"}:
                    grants.set_grants(conn, {"action": "grants", "name": mutation.split("_")[0], "may_send_to": []})
                else:
                    fleet_hub_messages.revoke_principal(
                        conn, {"action": "revoke", "name": "peer", "expected_enrollment_id": peer["enrollment_id"]}
                    )
                    if mutation == "reenroll":
                        fleet_hub_messages.add_principal(
                            conn,
                            {
                                "name": "peer",
                                "expected_enrollment_id": peer["enrollment_id"],
                                "may_send_to": ["agent:caller"],
                            },
                        )
            finally:
                conn.close()
        finally:
            resume.set()
        assert future.result(timeout=5) == before
    monkeypatch.setattr(grants, "grants_for", grants_for)
    after = _request(base, caller["token"], {"action": "identity", "peers": ["agent:peer"]})
    assert after[0] == 200
    if mutation == "reenroll":
        assert after[1]["peers"][0]["authorized"] is True
        assert after[1]["peers"][0]["enrollment_epoch"] != before[1]["peers"][0]["enrollment_epoch"]
        assert after[1]["peers"][0]["grant_epoch"] != before[1]["peers"][0]["grant_epoch"]
    else:
        assert after[1]["peers"] == [_denied_identity_peer()]


@pytest.mark.parametrize("wire_code", ["message_identity_peers_unsupported", "fixture_unrecognized_code"])
def test_identity_node_client_preserves_bounded_error_without_fallback(message_hub, monkeypatch, wire_code):
    base, token_a, _, _ = message_hub
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": token_a, "admin_token": ADMIN}
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE_A)
    handle = fleet_hub_messages.handle

    def denied(conn, raw, **kwargs):
        try:
            return handle(conn, raw, **kwargs)
        except fleet_hub_messages.MessageForbidden as exc:
            if wire_code == "fixture_unrecognized_code":
                raise fleet_hub_messages.MessageForbidden(str(exc), code=wire_code) from exc
            raise

    monkeypatch.setattr(fleet_hub_messages, "handle", denied)
    request_body = {"action": "identity", "peers": ["agent:peer"]}
    assert _request(base, token_a, request_body) == (
        403,
        {"error": "identity peers requires an agent principal", "code": wire_code},
    )
    hub_open = fleet_client._hub_open
    requests = []

    def observed(request, **kwargs):
        requests.append((request.full_url, json.loads(request.data)))
        return hub_open(request, **kwargs)

    monkeypatch.setattr(fleet_client, "_hub_open", observed)
    with pytest.raises(credentials.FleetMessageHubError) as exc:
        fleet_client_messages.identity(peers=["agent:peer"], timeout=2)
    assert requests == [(base + "/messages", request_body)]
    assert exc.value.status == 403
    assert exc.value.code == (wire_code if wire_code == "message_identity_peers_unsupported" else None)


def test_principal_client_preserves_epoch_failure_code_and_transaction(message_hub, monkeypatch):
    from brigade import fleet_hub_message_grants as grants

    base, token_a, _, db = message_hub
    _identity_pair(db)
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": base, "node_token": token_a, "admin_token": ADMIN}
    )
    conn = fleet_hub.open_db(db)
    try:
        before_principals = conn.execute("SELECT * FROM fleet_message_principals ORDER BY principal_id").fetchall()
        before_grants = conn.execute("SELECT * FROM fleet_message_send_grants ORDER BY principal_id,pattern").fetchall()
        current_epoch = conn.execute("SELECT grant_epoch FROM fleet_message_principals WHERE name='caller'").fetchone()[
            0
        ]
        calls = []

        def collide(size):
            calls.append(size)
            return current_epoch

        monkeypatch.setattr(grants.secrets, "token_urlsafe", collide)
        request_body = {"action": "grants", "name": "caller", "may_send_to": []}
        assert _post_path(base, ADMIN, "/message-principals", request_body) == (
            403,
            {"error": "message authorization unavailable", "code": "message_authorization_unavailable"},
        )
        assert calls == [24] * 4
        calls.clear()
        hub_open = fleet_client._hub_open
        requests = []

        def observed(request, **kwargs):
            requests.append((request.full_url, json.loads(request.data)))
            return hub_open(request, **kwargs)

        monkeypatch.setattr(fleet_client, "_hub_open", observed)
        with pytest.raises(credentials.FleetMessageHubError) as exc:
            fleet_client_messages.manage_principal("grants", name="caller", may_send_to=[])
        assert requests == [(base + "/message-principals", request_body)]
        assert calls == [24] * 4
        assert (
            conn.execute("SELECT * FROM fleet_message_principals ORDER BY principal_id").fetchall() == before_principals
        )
        assert (
            conn.execute("SELECT * FROM fleet_message_send_grants ORDER BY principal_id,pattern").fetchall()
            == before_grants
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        assert exc.value.status == 403
        assert exc.value.code == "message_authorization_unavailable"
    finally:
        conn.close()
