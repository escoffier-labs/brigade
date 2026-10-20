"""Loopback Streamable HTTP transport: binding, auth, origin, token files, parity."""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brigade import mcp_http, mcp_server, memory_mcp_core

TOKEN = "test-token-value"


def _handlers() -> mcp_server.Handlers:
    return mcp_server.Handlers(
        server_name="t",
        list_resources=lambda: [],
        read_resource=lambda uri: (None, None),
        list_tools=lambda: [{"name": "echo"}],
        call_tool=lambda name, args: ({"name": name}, False),
    )


@pytest.fixture
def server():
    srv = mcp_http.build_server(_handlers(), host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _post(url: str, body: object, headers: dict[str, str] | None = None, method: str = "POST") -> tuple[int, object]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data if method == "POST" else None, method=method)
    for key, value in {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        **(headers or {}),
    }.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def test_parse_bind_accepts_only_loopback():
    assert mcp_http.parse_bind("127.0.0.1:8790") == ("127.0.0.1", 8790)
    assert mcp_http.parse_bind("[::1]:0") == ("::1", 0)
    for bad in ("0.0.0.0:8790", "192.168.1.5:80", "localhost:8790", "127.0.0.1", "127.0.0.1:99999"):
        with pytest.raises(ValueError):
            mcp_http.parse_bind(bad)


def test_round_trip_and_notification(server):
    status, body = _post(f"{server}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200 and body["result"]["tools"] == [{"name": "echo"}]
    status, body = _post(f"{server}/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202 and body is None


def test_auth_origin_path_method_and_batch(server):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert _post(f"{server}/mcp", ping, {"Authorization": "Bearer wrong"})[0] == 401
    assert _post(f"{server}/mcp", ping, {"Authorization": ""})[0] == 401
    assert _post(f"{server}/mcp", ping, {"Origin": "https://evil.example"})[0] == 403
    assert _post(f"{server}/mcp", ping, {"Origin": "http://localhost:3000"})[0] == 200
    assert _post(f"{server}/other", ping)[0] == 404
    assert _post(f"{server}/mcp", ping, method="GET")[0] == 405
    assert _post(f"{server}/mcp", [ping])[0] == 400


def test_ensure_token_creates_private_file_and_reuses_it(tmp_path: Path):
    path = tmp_path / "home" / "mcp-http.token"
    mcp_http.ensure_token(path)
    assert path.stat().st_mode & 0o777 == 0o600
    first = mcp_http.read_token(path)
    mcp_http.ensure_token(path)
    assert mcp_http.read_token(path) == first and len(first) >= 32


def test_read_token_refuses_loose_modes_and_symlinks(tmp_path: Path):
    loose = tmp_path / "loose"
    loose.write_text("x\n")
    os.chmod(loose, 0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        mcp_http.read_token(loose)
    real = tmp_path / "real"
    real.write_text("x\n")
    os.chmod(real, 0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        mcp_http.read_token(link)


def test_stdio_and_http_answer_identically(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    repo = tmp_path / "repo"
    cards = repo / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "a.md").write_text("---\ntitle: Vlan\n---\ntrunk\n")
    handlers = memory_mcp_core.MemoryTools(target=repo).handlers()
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_cards", "arguments": {"query": "vlan"}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "card://memory/cards/a.md"}},
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    mcp_server.serve_stdio_handlers(handlers)
    stdio_replies = [json.loads(line) for line in out.getvalue().splitlines()]
    srv = mcp_http.build_server(handlers, host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
        http_replies = [_post(url, r)[1] for r in requests]
    finally:
        srv.shutdown()
        srv.server_close()
    assert http_replies == stdio_replies


def _raw(url: str, data: bytes, headers: dict[str, str] | None = None) -> tuple[int, object]:
    request = urllib.request.Request(url, data=data, method="POST")
    for key, value in {"Authorization": f"Bearer {TOKEN}", **(headers or {})}.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def test_host_and_malformed_origin_are_rejected(server):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    port = server.rsplit(":", 1)[1]
    assert _post(f"{server}/mcp", ping, {"Host": f"evil.example:{port}"})[0] == 403
    assert _post(f"{server}/mcp", ping, {"Host": f"localhost:{port}"})[0] == 200
    assert _post(f"{server}/mcp", ping, {"Host": "127.0.0.1"})[0] == 200
    assert _post(f"{server}/mcp", ping, {"Origin": "http://[::1"})[0] == 403
    assert _post(f"{server}/mcp", ping, {"Origin": "ftp://localhost"})[0] == 403
    assert _post(f"{server}/mcp", ping, {"Origin": "http://localhost:notaport"})[0] == 403


def test_parser_failures_return_fixed_400_without_traceback(server, capsys):
    for body in (b"[" * 100000, b"1" * 5000):
        status, reply = _raw(f"{server}/mcp", body)
        assert status == 400
        assert reply["error"] == {"code": -32700, "message": "parse error"}
    assert "Traceback" not in capsys.readouterr().err


def test_slow_client_cannot_hold_the_server():
    srv = mcp_http.build_server(_handlers(), host="127.0.0.1", port=0, token=TOKEN)
    assert srv.RequestHandlerClass.timeout == 10
    srv.RequestHandlerClass.timeout = 0.5
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    slow = socket.create_connection(("127.0.0.1", port))
    try:
        slow.sendall(b"POST /mc")
        time.sleep(0.1)
        started = time.monotonic()
        status, _ = _post(f"http://127.0.0.1:{port}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert status == 200
        assert time.monotonic() - started < 4
    finally:
        slow.close()
        srv.shutdown()
        srv.server_close()


def test_read_token_reads_the_opened_file_only(tmp_path: Path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("real-token\n")
    os.chmod(path, 0o600)
    attacker = tmp_path / "attacker"
    attacker.write_text("attacker-token\n")
    os.chmod(attacker, 0o600)
    real_lstat = Path.lstat

    def swapping_lstat(self: Path) -> os.stat_result:
        result = real_lstat(self)
        if self == path and not stat.S_ISLNK(result.st_mode):
            path.unlink()
            path.symlink_to(attacker)
        return result

    monkeypatch.setattr(Path, "lstat", swapping_lstat)
    try:
        token = mcp_http.read_token(path)
    except ValueError:
        token = None
    assert token != "attacker-token"


def test_read_token_checks_owner_and_bounds_size(tmp_path: Path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("a" * 5000)
    os.chmod(path, 0o600)
    assert len(mcp_http.read_token(path)) == 4096
    monkeypatch.setattr(mcp_http.os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(ValueError, match="owned by the current user"):
        mcp_http.read_token(path)
