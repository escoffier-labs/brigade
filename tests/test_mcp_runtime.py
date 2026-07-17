from __future__ import annotations

import json
import socket
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brigade import cli, mcp_cmd, mcp_runtime
from brigade.mcp_adapters import CanonicalServer


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def _seed_stdio(target: Path, name: str, script: Path, *, env: list[str] | None = None) -> None:
    mcp_cmd.init(target=target, json_output=True)
    mcp_cmd.add(
        target=target,
        name=name,
        command=sys.executable,
        args=[str(script)],
        env=env or [],
        timeout=2,
        targets=["claude"],
        json_output=True,
    )
    assert mcp_cmd.sync(target=target, harness="claude", write=True, json_output=True) == 0


VALID_STDIO = """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            },
        }), flush=True)
    elif request.get("method") == "tools/list":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"tools": [{"name": "echo", "description": "fixture"}]},
        }), flush=True)
"""


NOTIFYING_STDIO = """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request.get("id") == 1:
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "1"},
            },
        }), flush=True)
    elif request.get("method") == "notifications/initialized":
        print(json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "starting"},
        }), flush=True)
    elif request.get("method") == "tools/list":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"tools": [{"name": "echo", "description": "fixture"}]},
        }), flush=True)
"""


def test_verify_valid_stdio_keeps_config_and_runtime_status_separate(tmp_path, capsys):
    script = _script(tmp_path, "valid_mcp.py", VALID_STDIO)
    _seed_stdio(tmp_path, "valid", script)

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 0
    payload = _payload(capsys)

    assert payload["status"] == "completed"
    assert payload["receipt_path"].endswith("/receipt.json")
    assert payload["results"] == [
        {
            "name": "valid",
            "transport": "stdio",
            "config_current": True,
            "runtime_healthy": True,
            "failure_class": None,
            "detail": "",
            "protocol_version": "2024-11-05",
            "tool_count": 1,
        }
    ]


def test_verify_skips_stdio_notification_before_tools_response(tmp_path, capsys):
    script = _script(tmp_path, "notifying_mcp.py", NOTIFYING_STDIO)
    _seed_stdio(tmp_path, "notifying", script)

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 0
    result = _payload(capsys)["results"][0]

    assert result["runtime_healthy"] is True
    assert result["tool_count"] == 1


def test_verify_clean_exit_without_handshake_is_protocol_failure(tmp_path, capsys):
    script = _script(tmp_path, "library_module.py", "")
    _seed_stdio(tmp_path, "library", script)

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["config_current"] is True
    assert result["runtime_healthy"] is False
    assert result["failure_class"] == "protocol_failure"


def test_verify_invalid_executable_is_startup_failure(tmp_path, capsys):
    mcp_cmd.init(target=tmp_path, json_output=True)
    mcp_cmd.add(
        target=tmp_path,
        name="missing",
        command=str(tmp_path / "does-not-exist"),
        timeout=1,
        targets=["claude"],
        json_output=True,
    )
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["config_current"] is True
    assert result["failure_class"] == "startup_failure"


def test_verify_stdio_timeout_is_bounded(tmp_path, capsys):
    script = _script(tmp_path, "sleeping_mcp.py", "import time\ntime.sleep(10)\n")
    _seed_stdio(tmp_path, "sleeping", script)

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", timeout=0.1, json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["runtime_healthy"] is False
    assert result["failure_class"] == "timeout"


class _McpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if request.get("method") == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if request.get("method") == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "http-fixture", "version": "1"},
            }
        else:
            result = {"tools": [{"name": "http-echo"}]}
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "fixture-session")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _SlowMcpHandler(_McpHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        time.sleep(0.1)
        super().do_POST()


class _RedirectMcpHandler(_McpHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(302)
        self.send_header("Location", "/redirected")
        self.send_header("Content-Length", "0")
        self.end_headers()


class _SseMcpHandler(_McpHandler):
    tool_description = ""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        accept = self.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            self.send_response(406)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if request.get("method") == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if request.get("method") == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "sse-fixture", "version": "1"},
            }
        else:
            tool = {"name": "sse-echo"}
            if self.tool_description:
                tool["description"] = self.tool_description
            result = {"tools": [tool]}
        payload = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        body = f"event: message\ndata: {payload}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "sse-session")
        self.end_headers()
        self.wfile.write(body)


class _StreamingSseMcpHandler(_SseMcpHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if request.get("method") == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if request.get("method") == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "streaming-fixture", "version": "1"},
            }
        else:
            result = {"tools": [{"name": "streaming-echo"}]}
        payload = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        body = f"event: message\ndata: {payload}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "streaming-session")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        time.sleep(1)


class _PrefixedSseMcpHandler(_SseMcpHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if request.get("method") == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if request.get("method") == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "prefixed-fixture", "version": "1"},
            }
        else:
            result = {"tools": [{"name": "prefixed-echo"}]}
        notification = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "starting"},
            }
        )
        response = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        body = (f": keepalive\n\nevent: message\ndata: {notification}\n\nevent: message\ndata: {response}\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "prefixed-session")
        self.end_headers()
        self.wfile.write(body)


class _LongLineSseMcpHandler(_SseMcpHandler):
    tool_description = "x" * 5000


@contextmanager
def _http_mcp_server(handler: type[BaseHTTPRequestHandler] = _McpHandler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _seed_http(target: Path, url: str) -> None:
    mcp_cmd.init(target=target, json_output=True)
    mcp_cmd.add(
        target=target,
        name="remote",
        transport="http",
        url=url,
        timeout=2,
        targets=["claude"],
        json_output=True,
    )
    assert mcp_cmd.sync(target=target, harness="claude", write=True, json_output=True) == 0


def test_verify_valid_http_handshake(tmp_path, capsys):
    with _http_mcp_server() as url:
        _seed_http(tmp_path, url)
        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 0

    result = _payload(capsys)["results"][0]
    assert result["runtime_healthy"] is True
    assert result["protocol_version"] == "2024-11-05"
    assert result["tool_count"] == 1


def test_verify_streamable_http_event_stream_handshake(tmp_path, capsys):
    with _http_mcp_server(_SseMcpHandler) as url:
        _seed_http(tmp_path, url)
        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 0

    result = _payload(capsys)["results"][0]
    assert result["runtime_healthy"] is True
    assert result["protocol_version"] == "2024-11-05"
    assert result["tool_count"] == 1


def test_verify_reads_one_event_without_waiting_for_stream_close(tmp_path, capsys):
    with _http_mcp_server(_StreamingSseMcpHandler) as url:
        _seed_http(tmp_path, url)
        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", timeout=0.5, json_output=True) == 0

    result = _payload(capsys)["results"][0]
    assert result["runtime_healthy"] is True
    assert result["tool_count"] == 1


def test_verify_skips_sse_keepalive_and_notification_before_response(tmp_path, capsys):
    with _http_mcp_server(_PrefixedSseMcpHandler) as url:
        _seed_http(tmp_path, url)
        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 0

    result = _payload(capsys)["results"][0]
    assert result["runtime_healthy"] is True
    assert result["tool_count"] == 1


def test_verify_accepts_sse_data_line_larger_than_io_chunk(tmp_path, capsys):
    with _http_mcp_server(_LongLineSseMcpHandler) as url:
        _seed_http(tmp_path, url)
        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 0

    result = _payload(capsys)["results"][0]
    assert result["runtime_healthy"] is True
    assert result["tool_count"] == 1


def test_verify_unreachable_http_server_is_connection_failure(tmp_path, capsys):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    _seed_http(tmp_path, f"http://127.0.0.1:{port}/mcp")

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["config_current"] is True
    assert result["failure_class"] == "connection_failure"


def test_verify_rejects_http_redirects(tmp_path, capsys):
    with _http_mcp_server(_RedirectMcpHandler) as url:
        _seed_http(tmp_path, url)
        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1

    result = _payload(capsys)["results"][0]
    assert result["failure_class"] == "protocol_failure"
    assert result["detail"] == "redirects are disabled"


def test_verify_rejects_non_http_remote_scheme_without_connecting(tmp_path, capsys):
    _seed_http(tmp_path, "file:///etc/passwd")

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["failure_class"] == "protocol_failure"
    assert result["detail"] == "unsupported URL scheme: file"


def test_verify_redacts_invalid_http_header_value(tmp_path, capsys):
    secret = "do-not-leak\r\nInjected: value"
    with _http_mcp_server() as url:
        _seed_http(tmp_path, url)
        catalog_path = mcp_cmd.canonical_path(tmp_path)
        catalog = json.loads(catalog_path.read_text())
        catalog["servers"]["remote"]["headers"] = {"Authorization": {"literal": secret}}
        catalog_path.write_text(json.dumps(catalog))

        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1

    payload = _payload(capsys)
    receipt = Path(payload["receipt_path"])
    assert payload["results"][0]["failure_class"] == "protocol_failure"
    assert secret not in json.dumps(payload)
    assert secret not in receipt.read_text()


def test_verify_timeout_bounds_the_whole_http_handshake(tmp_path, capsys):
    with _http_mcp_server(_SlowMcpHandler) as url:
        _seed_http(tmp_path, url)
        capsys.readouterr()
        assert mcp_cmd.verify(target=tmp_path, harness="claude", timeout=0.15, json_output=True) == 1

    result = _payload(capsys)["results"][0]
    assert result["runtime_healthy"] is False
    assert result["failure_class"] == "timeout"


def test_verify_name_filter_probes_only_requested_server(tmp_path, capsys):
    good = _script(tmp_path, "good_mcp.py", VALID_STDIO)
    bad = _script(tmp_path, "bad_mcp.py", "")
    mcp_cmd.init(target=tmp_path, json_output=True)
    for name, script in (("good", good), ("bad", bad)):
        mcp_cmd.add(
            target=tmp_path,
            name=name,
            command=sys.executable,
            args=[str(script)],
            targets=["claude"],
            json_output=True,
        )
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, name="good", harness="claude", json_output=True) == 0
    payload = _payload(capsys)

    assert payload["filters"] == {"name": "good", "harness": "claude", "user_scope": False}
    assert [result["name"] for result in payload["results"]] == ["good"]


def test_config_current_requires_every_selected_harness_to_be_current(tmp_path, capsys):
    script = _script(tmp_path, "partial_mcp.py", "")
    mcp_cmd.init(target=tmp_path, json_output=True)
    mcp_cmd.add(
        target=tmp_path,
        name="partial",
        command=sys.executable,
        args=[str(script)],
        targets=["claude", "cursor"],
        json_output=True,
    )
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, name="partial", json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["config_current"] is False
    assert result["runtime_healthy"] is False


def test_verify_rejects_tools_list_without_a_tools_array(tmp_path, capsys):
    source = VALID_STDIO.replace('{"tools": [{"name": "echo", "description": "fixture"}]}', "{}")
    script = _script(tmp_path, "missing_tools_mcp.py", source)
    _seed_stdio(tmp_path, "missing-tools", script)

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["runtime_healthy"] is False
    assert result["failure_class"] == "protocol_failure"


def test_verify_rejects_oversized_unterminated_stdio_output(tmp_path, capsys):
    source = "import sys, time\nsys.stdout.write('x' * 300000)\nsys.stdout.flush()\ntime.sleep(10)\n"
    script = _script(tmp_path, "oversized_mcp.py", source)
    _seed_stdio(tmp_path, "oversized", script)

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", timeout=0.5, json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["runtime_healthy"] is False
    assert result["failure_class"] == "protocol_failure"
    assert result["detail"] == "output exceeded size limit"


def test_verify_rejects_high_risk_stdio_argv_without_spawning(tmp_path, capsys):
    sentinel = tmp_path / "spawned"
    mcp_cmd.init(target=tmp_path, json_output=True)
    assert (
        mcp_cmd.add(
            target=tmp_path,
            name="high-risk",
            command="bash",
            args=["-c", f"touch {sentinel}"],
            timeout=2,
            targets=["claude"],
            json_output=True,
        )
        == 0
    )
    assert mcp_cmd.sync(target=tmp_path, harness="claude", write=True, json_output=True) == 0

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", json_output=True) == 1
    result = _payload(capsys)["results"][0]

    assert result["failure_class"] == "startup_failure"
    assert result["detail"] == "command shape is high risk"
    assert not sentinel.exists()


def test_stdio_probe_cleans_process_after_unexpected_error(tmp_path, monkeypatch):
    script = _script(tmp_path, "clean_exit.py", "")
    server = CanonicalServer(name="cleanup", command=sys.executable, args=(str(script),))
    cleaned = False
    original_cleanup = mcp_runtime._kill_process_group

    def record_cleanup(proc, *, pgid=None):
        nonlocal cleaned
        cleaned = True
        original_cleanup(proc, pgid=pgid)

    def fail_send(proc, message):
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(mcp_runtime, "_kill_process_group", record_cleanup)
    monkeypatch.setattr(mcp_runtime, "_send_json_line", fail_send)

    result = mcp_runtime.probe_server(server, config_current=True, timeout=1)

    assert cleaned is True
    assert result.failure_class == "protocol_failure"
    assert result.detail == "runtime probe failed"


def test_sync_write_verify_produces_receipt_without_secret_values(tmp_path, monkeypatch, capsys):
    secret = "do-not-write-this-value"
    monkeypatch.setenv("FIXTURE_SECRET", secret)
    script = _script(tmp_path, "secret_mcp.py", VALID_STDIO)
    _seed_stdio(tmp_path, "secret", script, env=["TOKEN=ref:FIXTURE_SECRET"])

    capsys.readouterr()
    assert (
        mcp_cmd.sync(
            target=tmp_path,
            name="secret",
            harness="claude",
            write=True,
            verify_runtime=True,
            json_output=True,
        )
        == 0
    )
    payload = _payload(capsys)
    receipt = Path(payload["verification"]["receipt_path"])

    assert receipt.is_file()
    assert ".brigade/mcp/verify-runs/" in receipt.as_posix()
    assert payload["verification"]["results"][0]["runtime_healthy"] is True
    assert secret not in receipt.read_text()


def test_verify_rejects_nonpositive_timeout_without_spawning(tmp_path, capsys):
    script = _script(tmp_path, "timeout_mcp.py", VALID_STDIO)
    _seed_stdio(tmp_path, "timeout", script)

    capsys.readouterr()
    assert mcp_cmd.verify(target=tmp_path, harness="claude", timeout=0, json_output=True) == 2
    payload = _payload(capsys)

    assert payload["errors"] == ["--timeout must be greater than 0 and no more than 300 seconds"]
    assert not (tmp_path / ".brigade/mcp/verify-runs").exists()


def test_sync_verify_requires_write(tmp_path, capsys):
    script = _script(tmp_path, "dry_run_mcp.py", VALID_STDIO)
    _seed_stdio(tmp_path, "dry-run", script)

    capsys.readouterr()
    assert mcp_cmd.sync(target=tmp_path, harness="claude", verify_runtime=True, json_output=True) == 2
    payload = _payload(capsys)

    assert payload["errors"] == ["--verify requires --write"]


def test_sync_verify_timeout_error_names_sync_flag(tmp_path, capsys):
    capsys.readouterr()
    assert (
        mcp_cmd.sync(
            target=tmp_path,
            write=True,
            verify_runtime=True,
            verify_timeout=0,
            json_output=True,
        )
        == 2
    )
    payload = _payload(capsys)

    assert payload["errors"] == ["--verify-timeout must be greater than 0 and no more than 300 seconds"]


def test_mcp_verify_cli_supports_harness_and_name_filters(tmp_path, capsys):
    script = _script(tmp_path, "cli_mcp.py", VALID_STDIO)
    _seed_stdio(tmp_path, "cli-fixture", script)

    capsys.readouterr()
    assert (
        cli.main(
            [
                "mcp",
                "verify",
                "--target",
                str(tmp_path),
                "--harness",
                "claude",
                "--name",
                "cli-fixture",
                "--json",
            ]
        )
        == 0
    )
    assert _payload(capsys)["results"][0]["runtime_healthy"] is True
