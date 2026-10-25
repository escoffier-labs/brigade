"""Operator replies cannot outlive their HTTP credential enrollment."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from brigade import fleet_hub, fleet_hub_messages
from tests.test_fleet_messages import NODE_A, NODE_B, _request
from tests.test_fleet_questions import create

pytest_plugins = ["tests._fleet_message_fixtures"]


@pytest.fixture(params=["node", "agent"])
def operator_enrollment(hub, monkeypatch, request):
    from tests._fleet_message_fixtures import _RedactedHub

    principal = None
    if request.param == "node":
        operator, token = NODE_B, hub[2]
    else:
        with fleet_hub.open_db(hub[3]) as conn:
            principal = fleet_hub_messages.add_principal(conn, {"name": "operator", "may_send_to": [NODE_A]})
        operator, token = principal["principal_id"], principal["token"]
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", operator)

    def rotate():
        with fleet_hub.open_db(hub[3]) as conn:
            if principal is None:
                fleet_hub.revoke_node(conn, operator)
                return fleet_hub.add_node(conn, operator)[1]
            expected = {"name": "operator", "expected_enrollment_id": principal["enrollment_id"]}
            fleet_hub_messages.revoke_principal(conn, {"action": "revoke", **expected})
            return fleet_hub_messages.add_principal(conn, {**expected, "may_send_to": [NODE_A]})["token"]

    return _RedactedHub((operator, token, rotate))


def reply_request(action, message_id):
    if action == "reply":
        return {"action": "reply", "id": int(message_id), "body": "Accepted answer"}
    return {
        "action": "send",
        "reply_to": int(message_id),
        "to": NODE_A,
        "subject": "Answer",
        "body": "Accepted answer",
    }


@pytest.mark.parametrize("action", ["reply", "send"])
def test_operator_rotation_after_http_authentication_rejects_inflight_reply(
    hub, operator_enrollment, monkeypatch, action
):
    operator, token, rotate = operator_enrollment
    code, question = create(hub)
    if operator == NODE_B:
        assert code == 403 and "dedicated active agent:" in question["error"]
        with sqlite3.connect(hub[3]) as conn:
            assert conn.execute("SELECT COUNT(*) FROM fleet_questions").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone() == (0,)
        return
    assert code == 201
    raw = reply_request(action, question["message_id"])
    authenticated, resume = threading.Event(), threading.Event()
    original_handle = fleet_hub_messages.handle

    def paused_handle(conn, request, **kwargs):
        # The real HTTP handler has completed _caller and supplied the bearer
        # fingerprint before entering this boundary. Never reconstruct it here.
        if kwargs.get("caller") == operator and request == raw:
            authenticated.set()
            assert resume.wait(3), "credential rotation did not resume the request"
        return original_handle(conn, request, **kwargs)

    monkeypatch.setattr(fleet_hub_messages, "handle", paused_handle)
    results = []
    thread = threading.Thread(target=lambda: results.append(_request(hub[0], token, raw)), daemon=True)
    thread.start()
    try:
        assert authenticated.wait(3), "operator request did not finish authentication"
        replacement = rotate()
    finally:
        resume.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert results[0][0] == 403
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages WHERE reply_to IS NOT NULL").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM fleet_message_sends WHERE sender=?", (operator,)).fetchone() == (0,)
        assert conn.execute(
            "SELECT status,answer,answer_id,source,answered_by,answered_at FROM fleet_questions"
        ).fetchone() == ("pending", None, None, None, None, None)
    assert _request(hub[0], replacement, raw)[0] == 201
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("SELECT status,answer,source,answered_by FROM fleet_questions").fetchone() == (
            "answered",
            "Accepted answer",
            "operator_reply",
            operator,
        )


@pytest.mark.parametrize("action", ["reply", "send"])
def test_trusted_library_reply_without_authenticated_generation_still_works(hub, operator_enrollment, action):
    operator, _token, rotate = operator_enrollment
    code, question = create(hub)
    if operator == NODE_B:
        assert code == 403 and "dedicated active agent:" in question["error"]
        with sqlite3.connect(hub[3]) as conn:
            assert conn.execute("SELECT COUNT(*) FROM fleet_questions").fetchone() == (0,)
            assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone() == (0,)
        return
    assert code == 201
    rotate()
    with fleet_hub.open_db(hub[3]) as conn:
        code, result = fleet_hub_messages.handle(conn, reply_request(action, question["message_id"]), caller=operator)
        assert code == 201 and result["message"]["sender"] == operator
        assert conn.execute("SELECT status,source FROM fleet_questions").fetchone() == ("answered", "operator_reply")
