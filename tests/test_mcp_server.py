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


# --- resultType (MCP 2026-07-28: every successful result MUST carry it) ----------------

_META_KEY = "io.modelcontextprotocol/protocolVersion"


def _handle(method, params=None, *, protocol_version=None, extension=None):
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return mcp_server.handle_message(
        message,
        server_name="t",
        list_resources=lambda: [{"uri": "x://a"}],
        read_resource=lambda uri: ("hello", "text/plain") if uri == "x://a" else (None, None),
        list_tools=lambda: [{"name": "echo"}],
        call_tool=lambda name, arguments: (arguments, False),
        extension=extension,
        protocol_version=protocol_version,
    )


_CALLS = [
    ("ping", {}),
    ("resources/list", {}),
    ("resources/read", {"uri": "x://a"}),
    ("tools/list", {}),
    ("tools/call", {"name": "echo", "arguments": {"v": "hi"}}),
    ("custom/method", {"k": 1}),
    ("server/discover", {}),
]


def _extension():
    return mcp_server.McpExtension(methods={"custom/method": lambda params: {"echo": params}})


def test_stateless_results_carry_result_type_complete_via_meta():
    for method, params in _CALLS:
        reply = _handle(method, {**params, "_meta": {_META_KEY: "2026-07-28"}}, extension=_extension())
        assert reply["result"]["resultType"] == "complete", method


def test_stateless_results_carry_result_type_via_transport_version():
    for method, params in _CALLS:
        reply = _handle(method, params, protocol_version="2026-07-28", extension=_extension())
        assert reply["result"]["resultType"] == "complete", method


def test_versionless_request_keeps_the_legacy_shape():
    assert "resultType" not in _handle("tools/list")["result"]


def test_older_negotiated_versions_omit_result_type():
    for version in ("2024-11-05", "2025-03-26", "2025-06-18"):
        for method, params in _CALLS[:-1]:
            reply = _handle(method, params, protocol_version=version, extension=_extension())
            assert "resultType" not in reply["result"], (version, method)
        meta = _handle("tools/list", {"_meta": {_META_KEY: version}})
        assert "resultType" not in meta["result"]


def test_errors_and_initialize_never_carry_result_type():
    missing = _handle("resources/read", {"uri": "x://nope"}, protocol_version="2026-07-28")
    assert "result" not in missing and "resultType" not in missing["error"]
    init = _handle("initialize", {"protocolVersion": "2026-07-28"})
    assert "resultType" not in init["result"]


def test_extension_supplied_result_type_is_preserved():
    ext = mcp_server.McpExtension(methods={"custom/method": lambda params: {"resultType": "input_required"}})
    reply = _handle("custom/method", {}, protocol_version="2026-07-28", extension=ext)
    assert reply["result"]["resultType"] == "input_required"


def test_stdio_remembers_version_negotiated_by_initialize(monkeypatch, capsys):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"_meta": {_META_KEY: "2026-07-28"}}},
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"))
    mcp_server.serve_stdio(
        server_name="t",
        list_resources=list,
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [],
        call_tool=lambda n, a: ("", False),
    )
    by_id = {r["id"]: r for r in map(json.loads, capsys.readouterr().out.strip().splitlines())}
    assert "resultType" not in by_id[2]["result"]
    assert by_id[3]["result"]["resultType"] == "complete"


def test_stdio_stateless_initialize_applies_to_later_requests_without_meta(monkeypatch, capsys):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"))
    mcp_server.serve_stdio(
        server_name="t",
        list_resources=list,
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [],
        call_tool=lambda n, a: ("", False),
    )
    by_id = {r["id"]: r for r in map(json.loads, capsys.readouterr().out.strip().splitlines())}
    assert "resultType" not in by_id[1]["result"]
    assert by_id[2]["result"]["resultType"] == "complete"
    assert by_id[3]["result"]["resultType"] == "complete"
