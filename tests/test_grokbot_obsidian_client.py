"""Fixed Streamable-HTTP native MCP client for Obsidian Operator."""

from __future__ import annotations

import json

import pytest

from brigade.grokbot_obsidian.adapters import NATIVE_MCP_TOOLS
from brigade.grokbot_obsidian.contracts import ObsidianError
from brigade.grokbot_obsidian.native_client import StreamableNativeMcpClient

UPSTREAM_KEY = "k" * 16
UPSTREAM_URL = "https://127.0.0.1:27124/"


def _scripted_fetch(calls: list):
    def fetch(url, *, method="POST", headers=None, data=None, timeout=15):
        payload = json.loads(data.decode("utf-8")) if data else {}
        calls.append({"url": url, "method": method, "headers": dict(headers or {}), "body": payload})
        if payload.get("method") == "initialize":
            return {
                "status": 200,
                "headers": {"mcp-session-id": "session01"},
                "body": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "serverInfo": {"name": "obsidian-local-rest-api", "version": "1"},
                        },
                    }
                ).encode("utf-8"),
            }
        if payload.get("method") == "notifications/initialized":
            return {"status": 200, "headers": {}, "body": b""}
        if payload.get("method") == "tools/call":
            return {
                "status": 200,
                "headers": {},
                "body": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"content": [{"type": "text", "text": "ok"}]},
                    }
                ).encode("utf-8"),
            }
        raise AssertionError(payload.get("method"))

    return fetch


def test_client_performs_initialize_session_handshake_then_fixed_tools_call():
    calls: list[dict[str, object]] = []
    client = StreamableNativeMcpClient(url=UPSTREAM_URL, api_key=UPSTREAM_KEY, fetch=_scripted_fetch(calls))
    result = client.call_tool("search_simple", {"query": "demo"})
    assert result == {"content": [{"type": "text", "text": "ok"}]}
    assert [call["body"]["method"] for call in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert calls[0]["body"]["params"]["clientInfo"]["name"] == "grokbot-obsidian-operator"
    assert "id" not in calls[1]["body"]
    assert calls[2]["body"]["params"] == {"name": "search_simple", "arguments": {"query": "demo"}}
    assert calls[2]["headers"]["Mcp-Session-Id"] == "session01"
    assert calls[2]["headers"]["Authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert isinstance(calls[2]["body"]["id"], int)
    assert 1 <= calls[2]["body"]["id"] <= 1_000_000


def test_client_rejects_unknown_tools_without_calling_fetch():
    calls: list[dict[str, object]] = []
    client = StreamableNativeMcpClient(url=UPSTREAM_URL, api_key=UPSTREAM_KEY, fetch=_scripted_fetch(calls))
    with pytest.raises(ObsidianError) as caught:
        client.call_tool("command_execute", {})
    assert caught.value.code == "protocol_error"
    assert calls == []
    assert "command_execute" not in NATIVE_MCP_TOOLS


def test_client_close_is_idempotent_and_blocks_later_calls():
    calls: list[dict[str, object]] = []
    client = StreamableNativeMcpClient(url=UPSTREAM_URL, api_key=UPSTREAM_KEY, fetch=_scripted_fetch(calls))
    client.call_tool("command_list", {})
    client.close()
    client.close()
    with pytest.raises(ObsidianError) as caught:
        client.call_tool("command_list", {})
    assert caught.value.code == "unavailable"
    assert [call["body"]["method"] for call in calls] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


def test_client_errors_are_neutral_and_omit_secrets():
    def boom(*_args, **_kwargs):
        raise OSError(f"refused {UPSTREAM_URL} key={UPSTREAM_KEY}")

    client = StreamableNativeMcpClient(url=UPSTREAM_URL, api_key=UPSTREAM_KEY, fetch=boom)
    with pytest.raises(ObsidianError) as caught:
        client.call_tool("search_simple", {"query": "demo"})
    assert caught.value.code == "unavailable"
    assert UPSTREAM_KEY not in str(caught.value)
    assert UPSTREAM_URL not in str(caught.value)
    assert "Authorization" not in str(caught.value)
