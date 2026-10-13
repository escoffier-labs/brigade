"""Hub-backed per-principal message queue (#1688)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from brigade import fleet_hub

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
    yield f"http://127.0.0.1:{server.server_address[1]}", token_a, token_b
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


def test_nodes_send_list_ack_and_reply(message_hub):
    base, token_a, token_b = message_hub
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
    base, token_a, token_b = message_hub
    sent = _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "S", "body": "B"})[1]
    assert _request(base, token_a, {"action": "ack", "id": sent["message"]["id"]})[0] == 404
    assert _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "x" * 201, "body": "B"})[0] == 400
