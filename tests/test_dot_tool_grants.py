"""Connector grants narrow discovery and calls on both listener profiles."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json

import pytest

from brigade.dot import contracts, lifecycle
from brigade.dot.access_auth import AccessVerifier
from brigade.dot.adapter import build_tools
from brigade.dot.contracts import DotError
from brigade.dot.profile import DOT, DUET, PROFILES, runtime_profile
from brigade.dot.runtime_config import parse_runtime
from brigade.dot.events import IDENTITY
from tests.dot_support import requires_local_state
from tests.test_dot_adapter import VALID_CALLS
from tests.test_dot_inbox_tools import FakeMessages
from tests.test_dot_lifecycle import config, event_service, request, verifier
from tests.test_dot_runtime import _document, _write_private
from tests.test_duet_profile import ISSUE_TOOLS, _duet_document, _duet_verifier
from tests.test_duet_dispatch import DISPATCH as DUET_DISPATCH, document as duet_dispatch_document

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


@contextmanager
def listener(connector, dot_private_dir, monkeypatch, *, authenticated=True):
    profile, document, make_verifier = connector
    runtime = parse_runtime(document, schema=profile.runtime_schema)
    token = dot_private_dir / "token"
    _write_private(token, b"fixture-message-token-" * 3)
    runtime = replace(
        runtime,
        paths=replace(runtime.paths, state_dir=dot_private_dir, workspace=dot_private_dir.parent),
        hub=replace(runtime.hub, message_token_file=token),
    )
    messages = FakeMessages(monkeypatch)
    tools, state, _ = build_tools(runtime, profile)
    auth = make_verifier() if authenticated else AccessVerifier(runtime.auth)
    with state:
        app = lifecycle.build_app(config(), tools, event_service(state), auth, profile=profile)
        yield app, tools, state, messages


async def rpc(app, method, *, request_id=1, params=None):
    return await request(app, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})


async def list_tools(app):
    status, _, response = await rpc(app, "tools/list")
    assert status == 200
    return {tool["name"] for tool in response["result"]["tools"]}


async def call_tool(app, name, arguments, *, request_id=2):
    return await rpc(app, "tools/call", request_id=request_id, params={"name": name, "arguments": arguments})


def assert_rpc_denied(response, *, request_id=2):
    status, headers, result = response
    assert status == 400
    assert headers[b"content-type"] == b"application/json"
    assert result == {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32602, "message": "tool_not_permitted"},
    }


def tool_result(response):
    status, _, result = response
    assert status == 200
    return json.loads(result["result"]["content"][0]["text"])


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
    profile, document, _ = connector
    document["tool_grants"] = {"classes": CLASSES, "deny": [name]}
    arguments = MESSAGING_CALLS[name]
    if name == "message_send" and profile is DOT:
        arguments = {**arguments, "to": "ops"}
    with listener(connector, dot_private_dir, monkeypatch) as (app, tools, state, messages):

        async def discover_and_call():
            async with app.app.router.lifespan_context(app.app):
                assert await list_tools(app) == profile.tools - {name}
                return await call_tool(app, name, arguments)

        assert_rpc_denied(asyncio.run(discover_and_call()))
        with pytest.raises(DotError, match=r"^tool_not_permitted:"):
            tools.call(tools.runtime.auth.approved_email, name, arguments)

        assert messages.calls == []
        with state.transaction() as connection:
            rows = connection.execute(
                "SELECT tool, ok, error_code, arg_summary, request_id FROM call_log ORDER BY id"
            ).fetchall()
            assert [tuple(row) for row in rows] == [
                ("tools/list", 1, None, None, None),
                (name, 0, "tool_not_permitted", None, None),
            ]
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


@requires_local_state
@pytest.mark.parametrize("dispatch_granted", [True, False])
def test_duet_listener_dispatch_bundle_requires_dispatch_grant(dispatch_granted, dot_private_dir, monkeypatch):
    document = duet_dispatch_document()
    document["tool_grants"] = {"classes": ["read", "dispatch"] if dispatch_granted else ["read"], "deny": []}
    duet_read = ISSUE_TOOLS - MESSAGING_CALLS.keys() - {"handoff_write"}
    with listener((DUET, document, _duet_verifier), dot_private_dir, monkeypatch) as (app, tools, _, _):

        async def scenario():
            async with app.app.router.lifespan_context(app.app):
                assert await list_tools(app) == duet_read | (DUET_DISPATCH if dispatch_granted else set())
                if not dispatch_granted:
                    assert_rpc_denied(await call_tool(app, "fleet_capacity", {}))

        asyncio.run(scenario())
        if not dispatch_granted:
            with pytest.raises(DotError, match=r"^tool_not_permitted:"):
                tools.call(tools.runtime.auth.approved_email, "fleet_capacity", {})


@requires_local_state
@pytest.mark.parametrize("authenticated", [False, True], ids=["auth-first", "grant-before-arguments"])
def test_listener_checks_identity_then_grants_then_arguments(connector, authenticated, dot_private_dir, monkeypatch):
    _, document, _ = connector
    document["tool_grants"] = {"classes": ["read"], "deny": []}
    with listener(connector, dot_private_dir, monkeypatch, authenticated=authenticated) as (app, _, state, _):

        async def scenario():
            async with app.app.router.lifespan_context(app.app):
                return await call_tool(app, "message_send", {"invalid": "value"})

        response = asyncio.run(scenario())
        if authenticated:
            assert_rpc_denied(response)
        else:
            assert response[0] == 401
            assert response[2] == {"error": "request_denied"}
            with state.transaction() as connection:
                assert connection.execute("SELECT COUNT(*) FROM call_log").fetchone()[0] == 0


@requires_local_state
@pytest.mark.parametrize("name", ["fleet_submit", "handoff_write"])
def test_dot_listener_denies_tools_when_their_class_is_omitted(name, dot_private_dir, monkeypatch):
    document = _document()
    missing = "dispatch" if name == "fleet_submit" else "memory_write"
    document["tool_grants"] = {"classes": [kind for kind in CLASSES if kind != missing], "deny": []}
    with listener((DOT, document, verifier), dot_private_dir, monkeypatch) as (app, _, state, _):

        async def scenario():
            async with app.app.router.lifespan_context(app.app):
                assert name not in await list_tools(app)
                assert_rpc_denied(await call_tool(app, name, VALID_CALLS[name]))

        asyncio.run(scenario())
        assert state.operations(10) == []


@requires_local_state
def test_no_grants_keeps_default_listener_tools_and_allows_calls(connector, dot_private_dir, monkeypatch):
    profile, _, _ = connector
    name = "dot_t3_capabilities" if profile is DOT else "duet_capabilities"
    # Dot's independent tools/list snapshot is also exercised by test_dot_lifecycle.
    expected = contracts.TOOLS if profile is DOT else ISSUE_TOOLS
    with listener(connector, dot_private_dir, monkeypatch) as (app, _, _, _):

        async def scenario():
            async with app.app.router.lifespan_context(app.app):
                assert await list_tools(app) == expected
                assert set(tool_result(await call_tool(app, name, {}))["tools"]) == expected

        asyncio.run(scenario())


@requires_local_state
@pytest.mark.parametrize("enabled", [False, True], ids=["messaging-disabled", "hourly-budget"])
def test_allowed_messaging_still_uses_duet_switch_and_hourly_budget(enabled, dot_private_dir, monkeypatch):
    document = _duet_document()
    document["tool_grants"] = {"classes": ["messaging"], "deny": []}
    document["messaging_enabled"] = enabled
    with listener((DUET, document, _duet_verifier), dot_private_dir, monkeypatch) as (app, _, state, messages):
        if enabled:
            for _ in range(19):
                assert state.reserve_send(20, 3600)

        async def scenario():
            async with app.app.router.lifespan_context(app.app):
                arguments = MESSAGING_CALLS["message_send"]
                first = tool_result(await call_tool(app, "message_send", arguments))
                if not enabled:
                    return first
                assert "id" in first
                return tool_result(await call_tool(app, "message_send", arguments, request_id=3))

        result = asyncio.run(scenario())
        assert result["error"] == ("send_limit_reached" if enabled else "messaging_disabled")
        assert [call["action"] for call in messages.calls] == (["send"] if enabled else [])
        with state.transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM send_budget").fetchone()[0] == (20 if enabled else 0)


@requires_local_state
def test_adapter_error_payload_includes_grant_denial_message(dot_private_dir, monkeypatch):
    document = _document()
    document["tool_grants"] = {"classes": ["read"], "deny": []}
    with listener((DOT, document, verifier), dot_private_dir, monkeypatch) as (_, tools, _, _):
        # Use an unfiltered SDK registration to exercise the adapter's error conversion.
        server = lifecycle._server(config(), tools, DOT)
        token = IDENTITY.set(tools.runtime.auth.approved_email)
        try:
            response = asyncio.run(server.call_tool("inbox_ack", MESSAGING_CALLS["inbox_ack"]))
        finally:
            IDENTITY.reset(token)
    result = json.loads(response.model_dump()["content"][0]["text"])
    assert result["error"] == "tool_not_permitted"
    assert result["message"] == "This tool is outside the connector's configured grants."


@pytest.mark.parametrize(
    "profile, make_document, name",
    [
        (DOT, _document, "duet_capabilities"),
        (DUET, _duet_document, "dot_t3_capabilities"),
        (DUET, _duet_document, "fleet_approve"),
    ],
)
def test_runtime_rejects_deny_names_from_other_profiles(profile, make_document, name):
    document = make_document()
    document["tool_grants"] = {"classes": ["read"], "deny": [name]}
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(document, schema=profile.runtime_schema)


@pytest.mark.parametrize(
    "grants",
    [
        None,
        [],
        {},
        {"classes": []},
        {"classes": [], "deny": []},
        {"deny": []},
        {"classes": ["read"], "deny": [], "extra": True},
        {"classes": ["unknown"], "deny": []},
        {"classes": ["read"], "deny": ["unknown"]},
        {"classes": "read", "deny": []},
        {"classes": ["read"], "deny": "inbox_ack"},
        {"classes": [True], "deny": []},
        {"classes": ["read"], "deny": [{}]},
        {"classes": ["read", "read"], "deny": []},
        {"classes": ["read"], "deny": ["inbox_ack", "inbox_ack"]},
    ],
)
def test_runtime_rejects_invalid_tool_grants(connector, grants):
    profile, document, _ = connector
    document["tool_grants"] = grants
    with pytest.raises(DotError, match="^invalid_config$"):
        parse_runtime(document, schema=profile.runtime_schema)
