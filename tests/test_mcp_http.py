"""Tests for the loopback Streamable HTTP transport of the read-only MCP servers."""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from brigade import cli, mcp_server, memory_cmd, skills_cmd

TOKEN = "t" * 40


def _card(target: Path) -> None:
    cards = target / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "a.md").write_text("---\ntitle: Networking\n---\nvlan trunking notes\n")


class _Station:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.port = server.server_address[1]

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        *,
        headers: dict[str, str] | None = None,
        token: str | None = TOKEN,
    ) -> tuple[int, dict[str, str], bytes]:
        merged = dict(headers or {})
        if token is not None and "Authorization" not in merged:
            merged["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=merged)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def rpc(
        self, message: object, *, headers: dict[str, str] | None = None, token: str | None = TOKEN
    ) -> tuple[int, dict[str, str], bytes]:
        merged = {"Content-Type": "application/json", **(headers or {})}
        return self.request("POST", "/mcp", json.dumps(message).encode(), headers=merged, token=token)


def _start(monkeypatch: pytest.MonkeyPatch, run: Any) -> Iterator[_Station]:
    """Run a station's real serve_mcp(http=True) in a thread and capture its server."""
    captured: list[Any] = []
    ready = threading.Event()
    original = mcp_server.make_http_server

    def capture(**kwargs: Any) -> Any:
        server = original(**kwargs)
        captured.append(server)
        ready.set()
        return server

    monkeypatch.setattr(mcp_server, "make_http_server", capture)
    monkeypatch.setenv(mcp_server.HTTP_TOKEN_ENV, TOKEN)
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(run()), daemon=True)
    thread.start()
    assert ready.wait(10), "server did not bind"
    try:
        yield _Station(captured[0])
    finally:
        captured[0].shutdown()
        thread.join(10)
        assert not thread.is_alive()
        assert result == [0]


@pytest.fixture
def memory_station(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Station]:
    _card(tmp_path)
    yield from _start(monkeypatch, lambda: memory_cmd.serve_mcp(target=tmp_path, http=True, port=0))


@pytest.fixture
def skills_station(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Station]:
    yield from _start(monkeypatch, lambda: skills_cmd.serve_mcp(target=tmp_path, http=True, port=0))


def _json(body: bytes) -> Any:
    return json.loads(body.decode())


def test_memory_http_roundtrip(memory_station: _Station) -> None:
    status, headers, body = memory_station.rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
    )
    assert status == 200
    assert headers["content-type"] == "application/json"
    init = _json(body)["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert init["serverInfo"]["name"] == "brigade-memory-readonly"

    status, _, body = memory_station.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    names = {tool["name"] for tool in _json(body)["result"]["tools"]}
    assert names == {"list_cards", "get_card", "search_cards"}

    status, _, body = memory_station.rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_card", "arguments": {"path": "memory/cards/a.md"}},
        }
    )
    result = _json(body)["result"]
    assert status == 200
    assert result["isError"] is False
    assert "vlan trunking notes" in result["content"][0]["text"]


def test_skills_http_lists_read_only_tools(skills_station: _Station) -> None:
    status, _, body = skills_station.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200
    names = {tool["name"] for tool in _json(body)["result"]["tools"]}
    assert {"search_skills", "get_skill", "lint_skill"} <= names
    assert len(names) == 7


def test_missing_or_wrong_token_is_401(memory_station: _Station) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    status, headers, _ = memory_station.rpc(message, token=None)
    assert status == 401
    assert headers["www-authenticate"] == "Bearer"
    assert memory_station.rpc(message, token="x" * 40)[0] == 401
    assert memory_station.rpc(message, token=TOKEN[:-1])[0] == 401
    assert memory_station.rpc(message, headers={"Authorization": f"Basic {TOKEN}"}, token=None)[0] == 401


def test_bad_host_and_origin_are_403_before_auth(memory_station: _Station) -> None:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    # No token on purpose: the rebinding checks must win over the 401.
    assert memory_station.rpc(message, headers={"Host": "evil.example"}, token=None)[0] == 403
    assert memory_station.rpc(message, headers={"Host": "evil.example:80"})[0] == 403
    assert memory_station.rpc(message, headers={"Origin": "https://evil.example"})[0] == 403
    assert memory_station.rpc(message, headers={"Origin": "null"})[0] == 403
    assert memory_station.rpc(message, headers={"Origin": "http://localhost.evil.example"})[0] == 403
    assert memory_station.rpc(message, headers={"Origin": "http://localhost:3000"})[0] == 200
    assert memory_station.rpc(message, headers={"Host": "localhost:9"})[0] == 200
    assert memory_station.rpc(message, headers={"Host": "[::1]:9"})[0] == 200


def test_routing_methods_and_targets(memory_station: _Station) -> None:
    status, headers, _ = memory_station.request("GET", "/mcp")
    assert (status, headers["allow"]) == (405, "POST")
    assert memory_station.request("DELETE", "/mcp")[0] == 405
    assert memory_station.request("GET", "/other")[0] == 404
    assert memory_station.request("POST", "/healthz", b"{}")[0] == 405
    assert memory_station.request("GET", "http://evil.example/mcp")[0] == 400
    assert memory_station.request("GET", "//evil.example/mcp")[0] == 400
    # Query strings are ignored for path matching.
    status, _, _ = memory_station.request(
        "POST", "/mcp?x=1", b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', headers={"Content-Type": "x"}
    )
    assert status == 200


def test_healthz_needs_no_token_and_exposes_nothing(memory_station: _Station) -> None:
    status, _, body = memory_station.request("GET", "/healthz", token=None)
    assert status == 200
    assert _json(body) == {"status": "ok"}


def test_body_limits_and_invalid_json(memory_station: _Station) -> None:
    status, _, _ = memory_station.request(
        "POST", "/mcp", headers={"Content-Length": str(mcp_server.HTTP_MAX_BODY_BYTES + 1)}
    )
    assert status == 413
    assert memory_station.request("POST", "/mcp", headers={"Content-Length": "abc"})[0] == 400

    conn = http.client.HTTPConnection("127.0.0.1", memory_station.port, timeout=10)
    try:
        conn.putrequest("POST", "/mcp")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.endheaders()
        assert conn.getresponse().status == 411
    finally:
        conn.close()

    status, _, body = memory_station.request("POST", "/mcp", b"{not json")
    assert status == 400
    assert _json(body) == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}


def test_notifications_get_202_and_batches_answer_in_order(memory_station: _Station) -> None:
    status, _, body = memory_station.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert (status, body) == (202, b"")
    status, _, body = memory_station.rpc(
        [{"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 4, "result": {}}]
    )
    assert (status, body) == (202, b"")

    status, _, body = memory_station.rpc(
        [
            {"jsonrpc": "2.0", "id": "a", "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "b", "method": "nope"},
            7,
        ]
    )
    replies = _json(body)
    assert status == 200
    assert [r["id"] for r in replies] == ["a", "b", None]
    assert "tools" in replies[0]["result"]
    assert replies[1]["error"]["code"] == -32601
    assert replies[2]["error"]["code"] == -32600

    status, _, body = memory_station.rpc([])
    assert status == 400
    assert _json(body)["error"]["code"] == -32600


def test_tool_exception_returns_internal_error_without_details() -> None:
    def boom(name: str, arguments: dict[str, Any]) -> tuple[object, bool]:
        raise RuntimeError("secret internals")

    server = mcp_server.make_http_server(
        server_name="t",
        host="127.0.0.1",
        port=0,
        token=TOKEN,
        list_resources=lambda: [],
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [],
        call_tool=boom,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        station = _Station(server)
        status, _, body = station.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}})
        assert status == 200
        assert _json(body)["error"] == {"code": -32603, "message": "internal error"}
        assert b"secret internals" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def test_logs_never_contain_token_or_bodies(memory_station: _Station, capsys: pytest.CaptureFixture[str]) -> None:
    memory_station.rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_cards", "arguments": {"query": "needle-in-body"}},
        }
    )
    memory_station.request("GET", "/mcp?token=" + TOKEN, token=None)
    err = capsys.readouterr().err
    assert TOKEN not in err
    assert "needle-in-body" not in err
    assert "POST /mcp 200" in err
    assert "GET /mcp 405" in err


# --- protocol version negotiation -------------------------------------------------


def _handle(message: object) -> dict[str, Any] | None:
    return mcp_server.handle_message(
        message,
        server_name="t",
        list_resources=lambda: [],
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [],
        call_tool=lambda name, arguments: ("", False),
    )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("2024-11-05", "2024-11-05"),
        ("2025-03-26", "2025-03-26"),
        ("2025-06-18", "2025-06-18"),
        ("2099-01-01", "2025-06-18"),
        (None, "2025-06-18"),
        (20250326, "2025-06-18"),
    ],
)
def test_initialize_negotiates_protocol_version(requested: object, expected: str) -> None:
    params = {} if requested is None else {"protocolVersion": requested}
    reply = _handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert reply is not None
    assert reply["result"]["protocolVersion"] == expected


def test_stdio_negotiates_protocol_version_and_keeps_error_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        "{bad",
        "[1]",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "nope"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))
    rc = mcp_server.serve_stdio(
        server_name="t",
        list_resources=lambda: [],
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [],
        call_tool=lambda name, arguments: ("", False),
    )
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0
    assert out[0]["result"]["protocolVersion"] == "2025-06-18"
    assert [o["error"]["code"] for o in out[1:]] == [-32700, -32600, -32601]


# --- configuration ----------------------------------------------------------------


def _config(**overrides: Any) -> mcp_server.HttpConfig:
    values: dict[str, Any] = {"host": "127.0.0.1", "port": 8931, "token": TOKEN}
    values.update(overrides)
    return mcp_server.validate_http_config(**values)


def test_config_accepts_loopback_and_normalizes_localhost() -> None:
    assert _config(host="localhost").host == "127.0.0.1"
    assert _config(host="::1").host == "::1"
    assert _config(port=0).port == 0
    assert _config(token=f"  {TOKEN}\n").token == TOKEN


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "192.0.2.5", "example.com", "127.0.0.2"])
def test_config_refuses_non_loopback_hosts(host: str) -> None:
    with pytest.raises(mcp_server.McpHttpConfigError, match="loopback"):
        _config(host=host)


@pytest.mark.parametrize("port", [-1, 65536, "80", 1.5, True, None])
def test_config_refuses_bad_ports(port: object) -> None:
    with pytest.raises(mcp_server.McpHttpConfigError, match="port"):
        _config(port=port)


@pytest.mark.parametrize("token", [None, "", "short", "x" * 31, " " * 40])
def test_config_refuses_missing_or_short_tokens(token: object) -> None:
    with pytest.raises(mcp_server.McpHttpConfigError, match=mcp_server.HTTP_TOKEN_ENV):
        _config(token=token)


def test_cli_fails_before_binding_on_bad_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def never_bind(**kwargs: Any) -> None:
        raise AssertionError("must not bind with an invalid config")

    monkeypatch.setattr(mcp_server, "make_http_server", never_bind)
    monkeypatch.delenv(mcp_server.HTTP_TOKEN_ENV, raising=False)
    assert memory_cmd.serve_mcp(target=tmp_path, http=True, port=0) == 2
    assert mcp_server.HTTP_TOKEN_ENV in capsys.readouterr().err

    monkeypatch.setenv(mcp_server.HTTP_TOKEN_ENV, "short")
    assert skills_cmd.serve_mcp(target=tmp_path, http=True, port=0) == 2
    assert "at least 32" in capsys.readouterr().err

    monkeypatch.setenv(mcp_server.HTTP_TOKEN_ENV, TOKEN)
    assert memory_cmd.serve_mcp(target=tmp_path, http=True, host="0.0.0.0", port=0) == 2
    assert "loopback" in capsys.readouterr().err
    assert skills_cmd.serve_mcp(target=tmp_path, http=True, port=70000) == 2
    assert "invalid port" in capsys.readouterr().err
    assert memory_cmd.serve_mcp(target=tmp_path, port=9) == 2
    assert "require --http" in capsys.readouterr().err


@pytest.mark.parametrize("group", ["memory", "skills"])
def test_stdio_and_http_flags_are_mutually_exclusive(group: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([group, "serve-mcp", "--stdio", "--http"])
    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_startup_line_reports_actual_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    gen = _start(monkeypatch, lambda: memory_cmd.serve_mcp(target=tmp_path, http=True, port=0))
    station = next(gen)
    try:
        expected = f"brigade memory-readonly MCP listening on http://127.0.0.1:{station.port}/mcp"
        err = ""
        for _ in range(100):  # the line prints just after the socket binds
            err += capsys.readouterr().err
            if expected in err:
                break
            time.sleep(0.05)
        assert expected in err
        assert station.port != 0
    finally:
        with pytest.raises(StopIteration):
            next(gen)


def _ipv6_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_available(), reason="IPv6 loopback unavailable")
def test_ipv6_loopback_binding_serves_healthz() -> None:
    server = mcp_server.make_http_server(
        server_name="t",
        host="::1",
        port=0,
        token=TOKEN,
        list_resources=lambda: [],
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [],
        call_tool=lambda name, arguments: ("", False),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert mcp_server.http_endpoint_url(server) == f"http://[::1]:{server.server_address[1]}/mcp"
        conn = http.client.HTTPConnection("::1", server.server_address[1], timeout=10)
        conn.request("GET", "/healthz", headers={"Host": f"[::1]:{server.server_address[1]}"})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def test_protocol_version_header_is_checked_after_initialize(memory_station: _Station) -> None:
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    ok = memory_station.request("POST", "/mcp", payload, headers={"MCP-Protocol-Version": "2025-06-18"})
    assert ok[0] == 200
    status, _, body = memory_station.request("POST", "/mcp", payload, headers={"MCP-Protocol-Version": "1999-01-01"})
    assert status == 400
    assert _json(body) == {"error": "unsupported MCP-Protocol-Version"}


def _start_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Station]:
    _card(tmp_path)
    return _start(monkeypatch, lambda: memory_cmd.serve_mcp(target=tmp_path, http=True, port=0))


def test_slow_client_is_dropped_after_the_socket_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "HTTP_SOCKET_TIMEOUT_SECONDS", 0.5)
    stations = _start_memory(tmp_path, monkeypatch)
    station = next(stations)
    try:
        # Unauthenticated peer sends half a request line and stalls.
        with socket.create_connection(("127.0.0.1", station.port), timeout=10) as slow:
            slow.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            started = time.monotonic()
            assert slow.recv(1024) == b""
            assert time.monotonic() - started < 5
        # An authenticated peer that declares a body and never sends it is dropped too.
        with socket.create_connection(("127.0.0.1", station.port), timeout=10) as stalled:
            stalled.sendall(
                f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
                "Content-Length: 100\r\n\r\n{".encode()
            )
            assert stalled.recv(1024) == b""
        assert station.request("GET", "/healthz", token=None)[0] == 200
    finally:
        next(stations, None)


def test_connections_beyond_the_cap_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "HTTP_MAX_CONCURRENT_CONNECTIONS", 1)
    stations = _start_memory(tmp_path, monkeypatch)
    station = next(stations)
    try:
        with socket.create_connection(("127.0.0.1", station.port), timeout=10) as idle:
            idle.sendall(b"GET /healthz HTTP/1.1\r\n")  # holds the only slot
            time.sleep(0.2)
            with socket.create_connection(("127.0.0.1", station.port), timeout=10) as extra:
                # A refused connection reads as EOF on Linux and macOS, and as a reset
                # or abort (WinError 10053/10054) on Windows. Any of them is a refusal.
                try:
                    extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                    refused = extra.recv(1024)
                except ConnectionError:
                    refused = b""
                assert refused == b""
        # The slot frees once the idle connection closes.
        deadline = time.monotonic() + 5
        while True:
            try:
                if station.request("GET", "/healthz", token=None)[0] == 200:
                    break
            except ConnectionError:
                pass  # refused while the previous handler thread finishes
            assert time.monotonic() < deadline, "slot was never released"
            time.sleep(0.05)
    finally:
        next(stations, None)


def test_batch_size_nesting_depth_and_huge_length_are_bounded(memory_station: _Station) -> None:
    batch = [{"jsonrpc": "2.0", "id": i, "method": "tools/list"} for i in range(mcp_server.HTTP_MAX_BATCH_MESSAGES + 1)]
    status, _, body = memory_station.rpc(batch)
    assert status == 400
    assert _json(body)["error"]["code"] == -32600

    status, _, body = memory_station.request("POST", "/mcp", b"[" * 200_000)
    assert status == 400
    assert _json(body)["error"]["code"] == -32700

    status, _, _ = memory_station.request("POST", "/mcp", headers={"Content-Length": "9" * 5000})
    assert status == 413

    # Zero padding is normalized, not fed to int(): a padded valid length still works.
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    padded = "0" * 5000 + str(len(payload))
    assert memory_station.request("POST", "/mcp", payload, headers={"Content-Length": padded})[0] == 200
    assert memory_station.request("POST", "/mcp", headers={"Content-Length": "0" * 5000})[0] == 400


def test_methodless_request_with_id_is_invalid_but_responses_are_ignored(memory_station: _Station) -> None:
    status, _, body = memory_station.rpc({"jsonrpc": "2.0", "id": 7})
    assert status == 200
    assert _json(body) == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32600, "message": "invalid request"}}

    status, _, body = memory_station.rpc({"jsonrpc": "2.0", "id": 8, "result": {}})
    assert (status, body) == (202, b"")


def test_ping_gets_an_empty_result(memory_station: _Station) -> None:
    status, _, body = memory_station.rpc({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert status == 200
    assert _json(body) == {"jsonrpc": "2.0", "id": 9, "result": {}}


def test_notification_with_a_non_string_method_is_an_invalid_request(memory_station: _Station) -> None:
    status, _, body = memory_station.rpc({"jsonrpc": "2.0", "method": 1})
    assert status == 200
    assert _json(body) == {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}


def test_http_requires_jsonrpc_2_0(memory_station: _Station) -> None:
    for message in ({"id": 1, "method": "tools/list"}, {"jsonrpc": "1.0", "id": 2, "method": "tools/list"}):
        status, _, body = memory_station.rpc(message)
        assert status == 200
        assert _json(body) == {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32600, "message": "invalid request"},
        }
