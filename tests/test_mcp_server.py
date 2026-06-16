"""Tests for the shared MCP stdio harness."""

from __future__ import annotations

import io
import json

from brigade import mcp_server


def test_response_envelope():
    ok = mcp_server.response(1, result={"x": 1})
    assert ok == {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}
    err = mcp_server.response(2, error={"code": -1, "message": "boom"})
    assert err["error"]["code"] == -1 and "result" not in err


def test_serve_stdio_dispatches_all_methods(monkeypatch, capsys):
    requests = (
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "x://a"}}),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "x://missing"}}),
                json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {"name": "echo", "arguments": {"v": "hi"}},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 7, "method": "bogus"}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/ping"}),  # no id -> ignored
                "not json",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(requests))
    rc = mcp_server.serve_stdio(
        server_name="test-server",
        list_resources=lambda: [{"uri": "x://a", "name": "A"}],
        read_resource=lambda uri: ("hello", "text/plain") if uri == "x://a" else (None, None),
        list_tools=lambda: [{"name": "echo"}],
        call_tool=lambda name, arguments: (arguments, False),
    )
    responses = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    by_id = {r["id"]: r for r in responses if r.get("id") is not None}
    assert rc == 0
    assert by_id[1]["result"]["serverInfo"]["name"] == "test-server"
    assert by_id[2]["result"]["resources"][0]["uri"] == "x://a"
    assert by_id[3]["result"]["contents"][0]["text"] == "hello"
    assert by_id[4]["error"]["code"] == -32004
    assert by_id[5]["result"]["tools"][0]["name"] == "echo"
    assert "hi" in by_id[6]["result"]["content"][0]["text"]
    assert by_id[7]["error"]["code"] == -32601
    assert any(r.get("error", {}).get("code") == -32700 for r in responses)  # the non-JSON line
