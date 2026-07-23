from __future__ import annotations

import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brigade import cli, mcp_cmd, pi_mcp_bridge, pi_mcp_cmd


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def _use_pi_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(pi_mcp_cmd, "_home_dir", lambda: home)


DUAL_TOOL_STDIO = """
import json
import sys

TOOLS = {
    "echo": {"name": "echo", "description": "echo fixture", "inputSchema": {"type": "object"}},
}

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
            "result": {"tools": list(TOOLS.values())},
        }), flush=True)
    elif request.get("method") == "tools/call":
        args = request["params"]["arguments"]
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "content": [{"type": "text", "text": json.dumps(args)}],
                "isError": False,
            },
        }), flush=True)
"""


FAILING_TOOL_STDIO = """
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
            "result": {"tools": [{"name": "boom", "description": "fails"}]},
        }), flush=True)
    elif request.get("method") == "tools/call":
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32000, "message": "tool exploded"},
        }), flush=True)
"""


def _seed_catalog_stdio(target: Path, servers: dict[str, Path]) -> None:
    mcp_cmd.init(target=target, json_output=True)
    for name, script in servers.items():
        mcp_cmd.add(
            target=target,
            name=name,
            command=sys.executable,
            args=[str(script)],
            timeout=2,
            json_output=True,
        )


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
        elif request.get("method") == "tools/call":
            result = {
                "content": [{"type": "text", "text": "ok"}],
                "isError": False,
            }
        else:
            result = {"tools": [{"name": "echo", "description": "http echo"}]}
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", "fixture-session")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _http_mcp_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _McpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_qualify_tool_name_prevents_cross_server_collisions():
    assert pi_mcp_bridge.qualify_tool_name("alpha", "echo") == "alpha__echo"
    assert pi_mcp_bridge.qualify_tool_name("beta", "echo") == "beta__echo"
    assert pi_mcp_bridge.qualify_tool_name("alpha", "echo") != pi_mcp_bridge.qualify_tool_name("beta", "echo")


def test_discover_lists_stdio_and_http_tools_from_catalog(tmp_path, capsys):
    script = _script(tmp_path, "stdio.py", DUAL_TOOL_STDIO)
    _seed_catalog_stdio(tmp_path, {"stdio-a": script, "stdio-b": script})
    with _http_mcp_server() as url:
        mcp_cmd.add(target=tmp_path, name="remote", transport="http", url=url, timeout=2, json_output=True)

        capsys.readouterr()
        assert cli.main(["mcp", "pi-bridge", "discover", "--target", str(tmp_path), "--json"]) == 0
        payload = _payload(capsys)

    names = [tool["qualified_name"] for tool in payload["tools"]]
    assert names == ["remote__echo", "stdio-a__echo", "stdio-b__echo"]
    assert {item["transport"] for item in payload["servers"]} == {"http", "stdio"}


def test_call_failure_preserves_server_and_tool_identity(tmp_path, capsys):
    script = _script(tmp_path, "fail.py", FAILING_TOOL_STDIO)
    _seed_catalog_stdio(tmp_path, {"broken": script})

    capsys.readouterr()
    assert (
        cli.main(
            [
                "mcp",
                "pi-bridge",
                "call",
                "--target",
                str(tmp_path),
                "--tool",
                "broken__boom",
                "--args-json",
                "{}",
                "--json",
            ]
        )
        == 1
    )
    payload = _payload(capsys)
    assert payload["error"] is True
    assert payload["server"] == "broken"
    assert payload["tool"] == "boom"
    assert payload["qualified_name"] == "broken__boom"
    assert "tool exploded" in payload["message"]


def test_call_success_returns_structured_result(tmp_path, capsys):
    script = _script(tmp_path, "ok.py", DUAL_TOOL_STDIO)
    _seed_catalog_stdio(tmp_path, {"local": script})

    capsys.readouterr()
    assert (
        cli.main(
            [
                "mcp",
                "pi-bridge",
                "call",
                "--target",
                str(tmp_path),
                "--tool",
                "local__echo",
                "--args-json",
                '{"value": 1}',
                "--json",
            ]
        )
        == 0
    )
    payload = _payload(capsys)
    assert payload["error"] is False
    assert payload["server"] == "local"
    assert payload["tool"] == "echo"
    assert payload["result"]["content"][0]["text"] == '{"value": 1}'


def test_install_is_idempotent_and_writes_receipt(tmp_path, monkeypatch, capsys):
    _use_pi_home(monkeypatch, tmp_path)
    mcp_cmd.init(target=tmp_path, json_output=True)

    capsys.readouterr()
    assert cli.main(["mcp", "pi-bridge", "install", "--target", str(tmp_path), "--write", "--json"]) == 0
    first = _payload(capsys)
    agent = tmp_path / ".pi" / "agent"
    extension = agent / "extensions" / "brigade-mcp-bridge.js"
    projection = agent / "brigade" / "catalog-projection.json"
    state = agent / "brigade" / "install-state.json"
    assert extension.is_file()
    assert projection.is_file()
    assert state.is_file()
    assert first["ready"] is True
    assert "node:child_process" in extension.read_text()

    capsys.readouterr()
    assert cli.main(["mcp", "pi-bridge", "install", "--target", str(tmp_path), "--write", "--json"]) == 0
    second = _payload(capsys)
    assert second["files_written"] == []
    assert all(item["status"] == "current" for item in second["items"])


def test_uninstall_removes_only_managed_artifacts(tmp_path, monkeypatch, capsys):
    _use_pi_home(monkeypatch, tmp_path)
    mcp_cmd.init(target=tmp_path, json_output=True)
    agent = tmp_path / ".pi" / "agent"
    foreign = agent / "extensions" / "foreign.js"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("export default function () {}\n")

    assert cli.main(["mcp", "pi-bridge", "install", "--target", str(tmp_path), "--write", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["mcp", "pi-bridge", "uninstall", "--write", "--json"]) == 0
    payload = _payload(capsys)
    removed = {item["path"] for item in payload["items"] if item.get("action") == "remove"}
    assert str(agent / "extensions" / "brigade-mcp-bridge.js") in removed
    assert str(agent / "brigade" / "catalog-projection.json") in removed
    assert not (agent / "extensions" / "brigade-mcp-bridge.js").exists()
    assert not (agent / "brigade" / "install-state.json").exists()
    assert foreign.is_file()


def test_uninstall_preserves_user_edited_extension(tmp_path, monkeypatch, capsys):
    _use_pi_home(monkeypatch, tmp_path)
    mcp_cmd.init(target=tmp_path, json_output=True)
    assert cli.main(["mcp", "pi-bridge", "install", "--target", str(tmp_path), "--write", "--json"]) == 0
    capsys.readouterr()

    extension = tmp_path / ".pi" / "agent" / "extensions" / "brigade-mcp-bridge.js"
    extension.write_text(extension.read_text() + "\n// user edit\n")

    assert cli.main(["mcp", "pi-bridge", "uninstall", "--write", "--json"]) == 1
    payload = _payload(capsys)
    assert payload["conflicts"]
    assert extension.is_file()


def test_extension_generation_uses_node_builtins_only(tmp_path, monkeypatch):
    _use_pi_home(monkeypatch, tmp_path)
    mcp_cmd.init(target=tmp_path, json_output=True)
    assert pi_mcp_cmd.install(target=tmp_path, write=True, json_output=True) == 0
    text = (tmp_path / ".pi" / "agent" / "extensions" / "brigade-mcp-bridge.js").read_text()
    assert "node:child_process" in text
    assert "require(" not in text
    assert 'from "typebox"' not in text
    assert "from 'typebox'" not in text
