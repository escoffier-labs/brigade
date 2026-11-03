"""An agent principal's inbox read: hub authorization and the connector's argument errors.

The reported failure was an agent's inbox_list answered with an opaque HTTP 400
``request_denied``. The hub always lets an active principal list its own inbox, so
the denial came from the connector edge, which refuses arguments the advertised
schema does not bound (for example limit above 50) before the SDK runs.
"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from brigade import fleet_hub, fleet_hub_messages
from brigade.dot import lifecycle as m
from brigade.dot.adapter import argument_problem
from brigade.dot.contracts import TOOLS
from brigade.dot.profile import DUET
from tests.test_dot_lifecycle import config, request, verifier
from tests.test_fleet_messages import _request, message_hub as message_hub


# Hub side: a throwaway principal reads only its own inbox.


def test_agent_principal_reads_its_own_inbox_and_never_another(message_hub):
    base, token_a, _, db = message_hub
    with fleet_hub.init_db(db) as connection:
        alpha = fleet_hub_messages.add_principal(connection, {"name": "alpha", "label": "Fixture"})
        beta = fleet_hub_messages.add_principal(connection, {"name": "beta", "label": "Fixture"})
    for recipient in ("agent:alpha", "agent:beta"):
        status, _ = _request(base, token_a, {"action": "send", "to": recipient, "subject": "s", "body": "fixture"})
        assert status == 201
    for principal, own in ((alpha, "agent:alpha"), (beta, "agent:beta")):
        status, page = _request(base, principal["token"], {"action": "list", "unacked": True, "after": 0})
        assert status == 200
        assert [row["recipient"] for row in page["messages"]] == [own]
    # A caller cannot name another recipient: list has no recipient argument at all.
    status, page = _request(base, alpha["token"], {"action": "list", "recipient": "agent:beta"})
    assert status in (200, 400)
    assert all(row["recipient"] == "agent:alpha" for row in page.get("messages", []))
    with fleet_hub.init_db(db) as connection:
        fleet_hub_messages.revoke_principal(
            connection, {"action": "revoke", "name": "alpha", "expected_enrollment_id": alpha["enrollment_id"]}
        )
    assert _request(base, alpha["token"], {"action": "list", "after": 0})[0] == 401


# Connector edge: the refusal names the argument instead of an opaque request_denied.


def _gate():
    pytest.importorskip("mcp.server", reason="requires the grokbot extra")
    inner, recorder = AsyncMock(), Mock()
    return m._DotGate(inner, config(), verifier(), recorder), inner, recorder


def _call(gate, name, arguments, request_id=7):
    payload = dict(jsonrpc="2.0", id=request_id, method="tools/call", params=dict(name=name, arguments=arguments))
    return asyncio.run(request(gate, payload))


def _body(response):
    status, _, body = response
    return status, body


def test_out_of_range_inbox_limit_gets_an_actionable_invalid_params_error():
    gate, inner, recorder = _gate()
    status, body = _body(_call(gate, "inbox_list", {"limit": 100, "unacked_only": True}))
    assert status == 400
    assert body == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32602, "message": "invalid arguments: limit must be an integer from 1 to 50"},
    }
    inner.assert_not_called()
    label, ok = recorder.call_args.args[:2]
    assert (label, ok) == ("inbox_list", False)
    assert recorder.call_args.kwargs == {"error_code": "invalid_request", "arg_summary": "limit=100 unacked_only=true"}


def test_accepted_inbox_arguments_still_reach_the_sdk():
    gate, inner, recorder = _gate()

    async def answer(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    inner.side_effect = answer
    assert _call(gate, "inbox_list", {"limit": 50, "unacked_only": False})[0] == 200
    inner.assert_called_once()
    recorder.assert_not_called()


def test_unknown_keys_and_tools_are_described_without_echoing_caller_text():
    gate, inner, _ = _gate()
    status, body = _body(_call(gate, "inbox_list", {"limit": 5, "include_PRIVATE_marker": True}, "req-1"))
    assert status == 400 and body["id"] == "req-1"
    message = body["error"]["message"]
    assert message == "invalid arguments: 1 unsupported argument(s); accepted arguments: limit, unacked_only"
    assert "PRIVATE" not in json.dumps(body)
    status, body = _body(_call(gate, "shell_PRIVATE_marker", {}, request_id=True))
    assert status == 400 and body["id"] is None
    assert body["error"]["message"] == "invalid arguments: unknown tool"
    assert "PRIVATE" not in json.dumps(body)
    inner.assert_not_called()


def test_non_call_failures_keep_the_opaque_refusal():
    gate, inner, _ = _gate()
    status, _, body = asyncio.run(request(gate, b"{not json"))
    assert status == 400 and body == {"error": "request_denied"}
    inner.assert_not_called()


def test_argument_problem_covers_each_failure_shape():
    assert argument_problem("inbox_list", {"limit": 0}) == "limit must be an integer from 1 to 50"
    assert argument_problem("inbox_list", {"limit": "10"}) == "limit must be an integer from 1 to 50"
    assert argument_problem("inbox_list", {"unacked_only": "yes"}) == "invalid value for unacked_only"
    assert argument_problem("inbox_list", []) == "arguments must be an object"
    assert argument_problem("inbox_ack", {}) == "missing required argument ids"
    assert argument_problem("fleet_submit", {}, tools=DUET.tools) == "unknown tool"
    assert argument_problem(None, {}) == "unknown tool"
    for name in TOOLS:
        assert isinstance(argument_problem(name, {"unexpected": 1}), str)
