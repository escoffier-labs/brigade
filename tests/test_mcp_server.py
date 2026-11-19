"""Tests for the shared MCP stdio harness."""

from __future__ import annotations

import io
import json
import queue
import threading
import time

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


# --- cancellation (notifications/cancelled) ---------------------------------------

REQUEST_CANCELLED = -32800


class _StdioFeed:
    """A stdin replacement a test can append to while serve_stdio is running."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()

    def send(self, message: object) -> None:
        self._lines.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self._lines.put(None)

    def __iter__(self) -> _StdioFeed:
        return self

    def __next__(self) -> str:
        line = self._lines.get()
        if line is None:
            raise StopIteration
        return line


def _callbacks(call_tool):
    return {
        "server_name": "t",
        "list_resources": list,
        "read_resource": lambda uri: (None, None),
        "list_tools": lambda: [{"name": "block"}],
        "call_tool": call_tool,
    }


def _cancelled(request_id: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}}


def _wait_for(predicate, *, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def test_cancelled_request_is_answered_with_request_cancelled():
    registry = mcp_server.CancellationRegistry()
    started = threading.Event()
    seen: list[object] = []

    def call_tool(name, arguments):
        seen.append(mcp_server.current_cancellation())
        started.set()
        mcp_server.current_cancellation().wait(20)
        return {"done": True}, False

    replies: list[object] = []
    request = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "block", "arguments": {}}}
    worker = threading.Thread(
        target=lambda: replies.append(mcp_server.handle_message(request, **_callbacks(call_tool), registry=registry)),
        daemon=True,
    )
    worker.start()
    assert started.wait(20)
    assert mcp_server.handle_message(_cancelled(7), **_callbacks(call_tool), registry=registry) is None
    worker.join(20)
    assert not worker.is_alive()
    assert seen and seen[0] is not None
    assert replies[0]["error"]["code"] == REQUEST_CANCELLED


def test_cancellation_for_unknown_or_finished_ids_is_ignored():
    registry = mcp_server.CancellationRegistry()
    callbacks = _callbacks(lambda name, arguments: ("done", False))
    # Unknown id: no reply, no crash, and no request is left behind.
    assert mcp_server.handle_message(_cancelled("missing"), **callbacks, registry=registry) is None
    finished = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, **callbacks, registry=registry
    )
    assert finished["result"]["isError"] is False
    assert mcp_server.handle_message(_cancelled(1), **callbacks, registry=registry) is None
    later = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call"}, **callbacks, registry=registry
    )
    assert "error" not in later


def test_stdio_cancellation_reaches_a_running_tool_call(monkeypatch):
    replies: dict[object, dict] = {}
    monkeypatch.setattr(mcp_server, "_emit", lambda payload: replies.__setitem__(payload.get("id"), payload))
    feed = _StdioFeed()
    monkeypatch.setattr("sys.stdin", feed)
    started = threading.Event()

    def call_tool(name, arguments):
        started.set()
        mcp_server.current_cancellation().wait(20)
        return {"done": True}, False

    result: list[int] = []
    server = threading.Thread(
        target=lambda: result.append(mcp_server.serve_stdio(**_callbacks(call_tool))), daemon=True
    )
    server.start()
    try:
        feed.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "block", "arguments": {}}})
        assert started.wait(20)
        feed.send(_cancelled(1))
        _wait_for(lambda: 1 in replies or not server.is_alive(), what="the cancelled call's reply")
        assert replies[1]["error"]["code"] == REQUEST_CANCELLED
    finally:
        feed.close()
        server.join(20)
    assert not server.is_alive()
    assert result == [0]


def test_id_less_requests_are_never_executed():
    calls: list[str] = []
    extension = mcp_server.McpExtension(
        methods={"events/subscribe": lambda params: calls.append("subscribe") or {"ok": True}}
    )
    callbacks = {**_callbacks(lambda name, arguments: (calls.append(name), ("ok", False))[1]), "extension": extension}
    for registry in (None, mcp_server.CancellationRegistry()):
        for message in (
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "block", "arguments": {}}},
            {"jsonrpc": "2.0", "method": "events/subscribe", "params": {}},
            {"jsonrpc": "2.0", "method": "initialize", "params": {}},
        ):
            assert mcp_server.handle_message(message, **callbacks, registry=registry) is None, message
    assert calls == []


def test_null_id_requests_are_answered_as_before():
    calls: list[str] = []
    callbacks = _callbacks(lambda name, arguments: (calls.append(name), ("ok", False))[1])
    for registry in (None, mcp_server.CancellationRegistry()):
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "block", "arguments": {}}},
            **callbacks,
            registry=registry,
        )
        assert reply is not None and reply["id"] is None and reply["result"]["isError"] is False
        reply = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": None, "method": "notifications/cancelled", "params": {"requestId": 1}},
            **callbacks,
            registry=registry,
        )
        assert reply is not None and reply["error"]["code"] == -32601
    assert calls == ["block", "block"]


def test_stdio_deeply_nested_json_is_a_parse_error_and_the_session_continues(monkeypatch):
    replies: list[dict] = []
    monkeypatch.setattr(mcp_server, "_emit", replies.append)
    feed = _StdioFeed()
    monkeypatch.setattr("sys.stdin", feed)
    result: list[int] = []
    callbacks = _callbacks(lambda name, arguments: ("ok", False))
    server = threading.Thread(target=lambda: result.append(mcp_server.serve_stdio(**callbacks)), daemon=True)
    server.start()
    try:
        feed._lines.put("[" * 100_000 + "]" * 100_000 + "\n")
        feed.send({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        _wait_for(lambda: any(r.get("id") == 7 for r in replies) or not server.is_alive(), what="the reply after it")
        assert replies[0]["error"]["code"] == -32700
        assert any(r.get("id") == 7 and "result" in r for r in replies)
    finally:
        feed.close()
        server.join(20)
    assert result == [0]


def test_stdio_cancel_with_an_id_does_not_take_a_running_requests_token(monkeypatch):
    registry = mcp_server.CancellationRegistry()
    running = registry.begin(1)
    assert running is not None
    queued: "queue.Queue[object]" = queue.Queue()
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "notifications/cancelled", "params": {"requestId": 1}}) + "\n",
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) + "\n",
    ]
    mcp_server._read_stdio(iter(lines), queued, registry)
    # The id-less cancellation still reaches the running request.
    assert running.cancelled


def test_cancellation_notification_with_an_id_is_a_request():
    registry = mcp_server.CancellationRegistry()
    running = registry.begin(1)
    callbacks = _callbacks(lambda name, arguments: ("ok", False))
    assert running is not None
    # An id makes this a request, not a notification: it cancels nothing and gets
    # the normal method-not-found error for its own id.
    reply = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "notifications/cancelled", "params": {"requestId": 1}},
        **callbacks,
        registry=registry,
    )
    assert reply is not None and reply["id"] == 2 and reply["error"]["code"] == -32601
    assert not running.cancelled
    # The same message without an id is a notification and still cancels.
    assert (
        mcp_server.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
            **callbacks,
            registry=registry,
        )
        is None
    )
    assert running.cancelled


def test_stdio_cancellation_with_an_id_gets_method_not_found(monkeypatch):
    replies: dict[object, dict] = {}
    monkeypatch.setattr(mcp_server, "_emit", lambda payload: replies.__setitem__(payload.get("id"), payload))
    feed = _StdioFeed()
    monkeypatch.setattr("sys.stdin", feed)
    result: list[int] = []
    callbacks = _callbacks(lambda name, arguments: ("ok", False))
    server = threading.Thread(target=lambda: result.append(mcp_server.serve_stdio(**callbacks)), daemon=True)
    server.start()
    try:
        feed.send({"jsonrpc": "2.0", "id": 2, "method": "notifications/cancelled", "params": {"requestId": 99}})
        _wait_for(lambda: 2 in replies or not server.is_alive(), what="the rejected cancellation's reply")
        assert replies[2]["error"]["code"] == -32601
    finally:
        feed.close()
        server.join(20)
    assert not server.is_alive()
    assert result == [0]
