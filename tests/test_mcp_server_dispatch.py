"""Shared MCP JSON-RPC dispatch used by the stdio and HTTP transports."""

from __future__ import annotations

from brigade import mcp_server


def _handlers() -> mcp_server.Handlers:
    return mcp_server.Handlers(
        server_name="test-server",
        list_resources=lambda: [{"uri": "card://a.md"}],
        read_resource=lambda uri: ("body", "text/markdown") if uri == "card://a.md" else (None, None),
        list_tools=lambda: [{"name": "echo"}],
        call_tool=lambda name, args: ({"name": name, "args": args}, False),
    )


def test_initialize_negotiates_protocol_version():
    h = _handlers()
    legacy = mcp_server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, h)
    assert legacy["result"]["protocolVersion"] == "2024-11-05"
    assert legacy["result"]["serverInfo"]["name"] == "test-server"
    echoed = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, h
    )
    assert echoed["result"]["protocolVersion"] == "2025-03-26"
    newest = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}, h
    )
    assert newest["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_get_no_reply_and_ping_answers():
    h = _handlers()
    assert mcp_server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, h) is None
    assert mcp_server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "ping"}, h)["result"] == {}


def test_tools_call_and_errors():
    h = _handlers()
    reply = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": {"x": 1}}}, h
    )
    assert reply["result"]["isError"] is False
    assert '"x": 1' in reply["result"]["content"][0]["text"]
    missing = mcp_server.dispatch(
        {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "card://nope.md"}}, h
    )
    assert missing["error"]["code"] == -32004
    assert mcp_server.dispatch([1, 2], h)["error"]["code"] == -32600
    assert mcp_server.dispatch({"jsonrpc": "2.0", "id": 7, "method": "bogus"}, h)["error"]["code"] == -32601
