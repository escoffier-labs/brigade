"""Connector grants narrow discovery and calls on both listener profiles."""

import asyncio
from dataclasses import replace

import pytest

from brigade.dot import contracts, lifecycle
from brigade.dot.adapter import build_tools
from brigade.dot.contracts import DotError
from brigade.dot.profile import DOT, DUET, PROFILES, runtime_profile
from brigade.dot.runtime_config import parse_runtime
from tests.dot_support import requires_local_state
from tests.test_dot_inbox_tools import FakeMessages
from tests.test_dot_lifecycle import config, event_service, request, verifier
from tests.test_dot_runtime import _document, _write_private
from tests.test_duet_profile import _duet_document, _duet_verifier

MESSAGING_CALLS = {
    "message_send": {"to": "operator", "subject": "Update", "body": "Ready"},
    "inbox_reply": {"msg_id": 1, "body": "Ready"},
    "inbox_ack": {"ids": [1]},
}
DISPATCH = {
    "fleet_submit",
    "fleet_followup",
    "fleet_answer",
    "fleet_approve",
    "fleet_interrupt",
    "fleet_report_session",
}
CLASSES = ["read", "messaging", "dispatch", "memory_write"]


@pytest.fixture(params=[(DOT, _document, verifier), (DUET, _duet_document, _duet_verifier)], ids=["dot", "duet"])
def connector(request):
    profile, document, verifier_factory = request.param
    return profile, document(), verifier_factory


def test_registered_tool_classes_match_snapshot():
    read = {
        "dot_t3_capabilities",
        "duet_capabilities",
        "fleet_capacity",
        "fleet_doctor",
        "fleet_status",
        "fleet_wait",
        "fleet_recent",
        "fleet_overview",
        "memory_search",
        "memory_card",
        "inbox_list",
        "message_status",
        "fleet_policy",
        "skill_search",
        "skill_get",
        "fleet_sessions",
        "fleet_work_list",
        "fleet_work_show",
        "fleet_work_next",
        "fleet_work_burn",
        "fleet_models",
        "fleet_cloud",
        "fleet_hub_policy",
        "fleet_digest",
    }
    expected = {
        **dict.fromkeys(read, "read"),
        **dict.fromkeys(MESSAGING_CALLS, "messaging"),
        **dict.fromkeys(DISPATCH, "dispatch"),
        "handoff_write": "memory_write",
    }
    registered = set().union(
        *(runtime_profile(profile, True, messaging_enabled=True).tools for profile in PROFILES.values())
    )
    assert registered == expected.keys()
    assert {name: schema.tool_class.value for name, schema in contracts.ALL_TOOL_SCHEMAS.items()} == expected


@requires_local_state
@pytest.mark.parametrize("name", MESSAGING_CALLS)
def test_denied_messaging_tool_is_hidden_and_has_no_side_effects(connector, name, dot_private_dir, monkeypatch):
    profile, document, make_verifier = connector
    document["tool_grants"] = {"classes": CLASSES, "deny": [name]}
    runtime = parse_runtime(document, schema=profile.runtime_schema)
    token = dot_private_dir / "token"
    _write_private(token, b"fixture-message-token-" * 3)
    runtime = replace(
        runtime,
        paths=replace(runtime.paths, state_dir=dot_private_dir),
        hub=replace(runtime.hub, message_token_file=token),
    )
    messages = FakeMessages(monkeypatch)
    tools, state, _ = build_tools(runtime, profile)
    arguments = MESSAGING_CALLS[name]
    if name == "message_send" and profile is DOT:
        arguments = {**arguments, "to": "ops"}
    with state:
        app = lifecycle.build_app(config(), tools, event_service(state), make_verifier(), profile=profile)

        async def discover_and_call():
            async with app.app.router.lifespan_context(app.app):
                status, _, result = await request(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert status == 200
                assert {tool["name"] for tool in result["result"]["tools"]} == profile.tools - {name}
                return await request(
                    app,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )

        status, headers, result = asyncio.run(discover_and_call())
        assert status == 400
        assert headers[b"content-type"] == b"application/json"
        assert result == {"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "tool_not_permitted"}}
        with pytest.raises(DotError, match=r"^tool_not_permitted:"):
            tools.call(runtime.auth.approved_email, name, arguments)

        assert messages.calls == []
        with state.transaction() as connection:
            assert [row["tool"] for row in connection.execute("SELECT tool FROM call_log")] == ["tools/list"]
            for table in ("operations", "replies", "send_budget", "inbox", "hub_inbox", "hub_state"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("include_dispatch", [True, False])
def test_messaging_class_can_be_removed_independently_of_dispatch(include_dispatch):
    classes = ["read", "memory_write"]
    if include_dispatch:
        classes.append("dispatch")
    document = _document()
    document["tool_grants"] = {"classes": classes, "deny": []}
    runtime = parse_runtime(document)
    profile = runtime_profile(DOT, runtime.dispatch_enabled, tool_grants=runtime.tool_grants)
    assert profile.tools == DOT.tools - MESSAGING_CALLS.keys() - (set() if include_dispatch else DISPATCH)


def test_tool_grants_cannot_enable_dispatch_on_duet():
    document = _duet_document()
    document["tool_grants"] = {"classes": ["read", "dispatch", "memory_write"], "deny": []}
    runtime = parse_runtime(document, schema=DUET.runtime_schema)
    profile = runtime_profile(DUET, runtime.dispatch_enabled, tool_grants=runtime.tool_grants)
    assert profile.tools == DUET.tools - MESSAGING_CALLS.keys()


@pytest.mark.parametrize(
    "grants",
    [
        None,
        [],
        {},
        {"classes": []},
        {"deny": []},
        {"classes": [], "deny": [], "extra": True},
        {"classes": ["unknown"], "deny": []},
        {"classes": [], "deny": ["unknown"]},
        {"classes": "read", "deny": []},
        {"classes": [], "deny": "inbox_ack"},
        {"classes": [True], "deny": []},
        {"classes": [], "deny": [{}]},
        {"classes": ["read", "read"], "deny": []},
        {"classes": [], "deny": ["inbox_ack", "inbox_ack"]},
    ],
)
def test_runtime_rejects_invalid_tool_grants(connector, grants):
    profile, document, _ = connector
    document["tool_grants"] = grants
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(document, schema=profile.runtime_schema)
