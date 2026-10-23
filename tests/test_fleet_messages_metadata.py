"""Message metadata, digest compatibility, and sender acknowledgement views."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub_messages as messages
from tests.test_fleet_messages import NODE_A, NODE_B, _request
from tests.test_fleet_message_grants import enroll, send

pytest_plugins = ["tests._fleet_message_fixtures"]


def test_node_label_and_agent_label_rejection(hub):
    base, token_a, _, _ = hub
    principal = enroll(base, may_send_to=[NODE_A])
    status, result = send(base, token_a, NODE_B, sender_label="fixture-relay")
    assert status == 201
    assert result["message"]["sender_label"] == "fixture-relay"
    assert send(base, principal["token"], sender_label="spoof")[0] == 400


@pytest.mark.parametrize(
    "field,value",
    [
        ("sender_label", "bad/name"),
        ("sender_label", "a" * 65),
        ("sender_label", ""),
        ("thread", "bad thread"),
        ("thread", "a" * 129),
        ("thread", ""),
        ("kind", "other"),
        ("kind", 1),
    ],
)
def test_metadata_validation(hub, field, value):
    base, token_a, _, _ = hub
    assert send(base, token_a, NODE_B, **{field: value})[0] == 400


def test_metadata_roundtrip_and_reply_thread(hub):
    base, token_a, token_b, _ = hub
    status, result = send(base, token_a, NODE_B, thread="issue:42/#done", kind="question", sender_label="relay")
    assert status == 201
    message = result["message"]
    assert (message["thread"], message["kind"]) == ("issue:42/#done", "question")
    reply = _request(base, token_b, {"action": "reply", "id": message["id"], "body": "Done"})[1]["message"]
    assert reply["thread"] == message["thread"]
    assert reply["kind"] == "message"
    assert send(base, token_a, NODE_B)[1]["message"]["kind"] == "message"


def test_legacy_digest_is_byte_identical():
    assert (
        messages._payload_digest("node-b", "Task", "Work", None, "normal")
        == "638c8180a379a181f5a8159cd653a1c88fdc305785c009c52a752a5a5bd91eeb"
    )


@pytest.mark.parametrize(
    "field,first,second",
    [("sender_label", "relay", "other"), ("thread", "issue:1", "issue:2"), ("kind", "question", "completion")],
)
def test_metadata_idempotency(hub, field, first, second):
    base, token_a, _, _ = hub
    accepted = send(base, token_a, NODE_B, client_id="metadata", **{field: first})
    assert accepted[0] == 201
    assert send(base, token_a, NODE_B, client_id="metadata", **{field: first}) == accepted
    assert send(base, token_a, NODE_B, client_id="metadata", **{field: second})[0] == 409


def test_explicit_default_kind_has_legacy_digest(hub):
    base, token_a, _, _ = hub
    accepted = send(base, token_a, NODE_B, client_id="default-kind")
    assert send(base, token_a, NODE_B, client_id="default-kind", kind="message") == accepted


def test_sent_is_sender_scoped_and_reports_ack(hub):
    base, token_a, token_b, _ = hub
    first = send(base, token_a, NODE_B)[1]["message"]
    second = send(base, token_a, NODE_B)[1]["message"]
    foreign = send(base, token_b, NODE_A)[1]["message"]
    _request(base, token_b, {"action": "ack", "id": first["id"]})
    status, page = _request(base, token_a, {"action": "sent", "ids": [first["id"], foreign["id"]]})
    assert status == 200
    assert [row["id"] for row in page["messages"]] == [first["id"]]
    assert page["messages"][0]["acked_at"] is not None
    assert page["messages"][0]["acked_by"] == NODE_B
    assert _request(base, token_a, {"action": "sent", "after": first["id"]})[1] == {
        "messages": [second],
        "next_cursor": second["id"],
    }
    assert _request(base, token_a, {"action": "sent", "after": second["id"]})[1] == {
        "messages": [],
        "next_cursor": second["id"],
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"after": 0, "ids": [1]},
        {"ids": []},
        {"ids": [True]},
        {"ids": [0]},
        {"ids": [2**63]},
        {"ids": list(range(1, 102))},
        {"ids": "1"},
        {"after": True},
    ],
)
def test_sent_invalid_filters(hub, fields):
    base, token_a, _, _ = hub
    assert _request(base, token_a, {"action": "sent", **fields})[0] == 400


def test_sent_byte_cap_and_agent_scope(hub, monkeypatch):
    base, _, _, _ = hub
    principal = enroll(base, may_send_to=[NODE_A])
    first = send(base, principal["token"])[1]["message"]
    send(base, principal["token"])
    monkeypatch.setattr(messages, "MAX_LIST_RESPONSE_BYTES", 750)
    status, page = _request(base, principal["token"], {"action": "sent", "after": 0})
    assert status == 200
    assert [row["id"] for row in page["messages"]] == [first["id"]]
    assert page["next_cursor"] == first["id"]


def test_expired_parent_reply_retry_keeps_inherited_thread(message_hub):
    base, token_a, token_b, db = message_hub
    parent = _request(
        base, token_a, {"action": "send", "to": NODE_B, "subject": "Parent", "body": "Old", "thread": "issue:7"}
    )[1]["message"]
    reply_request = {"action": "reply", "id": parent["id"], "body": "Recent", "client_id": "thread-retry"}
    reply = _request(base, token_b, reply_request)[1]["message"]
    assert reply["thread"] == "issue:7"
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE fleet_messages SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(days=91)).isoformat(), parent["id"]),
    )
    conn.commit()
    conn.close()
    _request(base, token_b, {"action": "list"})
    replayed = _request(base, token_b, reply_request)
    assert replayed[0] == 201
    assert replayed[1]["message"]["id"] == reply["id"]
    assert replayed[1]["message"]["thread"] == "issue:7"


@pytest.mark.parametrize("action", ["send", "reply"])
@pytest.mark.parametrize("replay", [False, True])
def test_senders_cannot_choose_reserved_operator_question_kind(hub, action, replay):
    base, token_a, token_b, db = hub
    original = send(base, token_a, NODE_B)[1]["message"]
    request = {"action": action, "body": "Cannot impersonate question", "client_id": "reserved-kind"}
    if action == "send":
        request.update(to=NODE_A, subject="Question")
    else:
        request["id"] = original["id"]
    if replay:
        assert _request(base, token_b, request)[0] == 201
    code, result = _request(base, token_b, {**request, "kind": "operator_question"})
    assert code == 400 and "operator_question is reserved" in result["error"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages WHERE kind='operator_question'").fetchone() == (0,)
    if replay:
        assert _request(base, token_b, request)[0] == 201
