"""n8n-operator urllib client: headers, caps, redirects, and sanitized errors."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from brigade.grokbot_n8n.client import ACTION_PATHS, N8nClient
from brigade.grokbot_n8n.contracts import ACTION_IDS, MAX_COLLECTION, N8nError

TEST_HEADER_VALUE = "fixture-value-no-secret"


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = 200
        self.body = b'{"data":[]}'
        self.headers = {"Content-Type": "application/json"}
        self.redirect_to: str | None = None
        self.delay = 0.0


def _server(recorder: _Recorder) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            recorder.calls.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {key: value for key, value in self.headers.items()},
                    "body": body,
                }
            )
            if recorder.delay:
                import time

                time.sleep(recorder.delay)
            if recorder.redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", recorder.redirect_to)
                self.end_headers()
                return
            self.send_response(recorder.status)
            for key, value in recorder.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(recorder.body)))
            self.end_headers()
            self.wfile.write(recorder.body)

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _client(server: ThreadingHTTPServer, **overrides: object) -> N8nClient:
    host, port = server.server_address[:2]
    kwargs = {
        "base_url": f"http://127.0.0.1:{port}",
        "api_key": TEST_HEADER_VALUE,
        "timeout_seconds": 2,
        "max_response_bytes": 4096,
    }
    kwargs.update(overrides)
    return N8nClient(**kwargs)  # type: ignore[arg-type]


def test_client_sends_api_key_header_and_builds_fixed_paths():
    recorder = _Recorder()
    recorder.body = json.dumps({"data": [{"id": "wf1", "name": "demo"}]}).encode("utf-8")
    server = _server(recorder)
    try:
        client = _client(server)
        client.get_json("/api/v1/workflows")
        headers = {key.casefold(): value for key, value in recorder.calls[0]["headers"].items()}
        assert headers.get("x-n8n-api-key") == TEST_HEADER_VALUE
        assert recorder.calls[0]["path"].startswith("/api/v1/workflows")
        assert "authorization" not in headers
        for action_id, path in ACTION_PATHS.items():
            assert action_id in ACTION_IDS
            assert "{id}" in path
            assert not path.startswith("http")
        assert ACTION_PATHS["deactivate-workflow"] == "/api/v1/workflows/{id}/deactivate"
        assert ACTION_PATHS["archive-workflow"] == "/api/v1/workflows/{id}/archive"
        assert ACTION_PATHS["unarchive-workflow"] == "/api/v1/workflows/{id}/unarchive"
        assert ACTION_PATHS["cancel-execution"] == "/api/v1/executions/{id}/stop"
    finally:
        server.shutdown()
        server.server_close()


def test_client_rejects_redirects_without_forwarding_the_key():
    second = _Recorder()
    second_server = _server(second)
    try:
        host, port = second_server.server_address[:2]
        recorder = _Recorder()
        recorder.redirect_to = f"http://127.0.0.1:{port}/stolen"
        first = _server(recorder)
        try:
            client = _client(first)
            with pytest.raises(N8nError) as caught:
                client.get_json("/api/v1/workflows")
            assert caught.value.code == "protocol_error"
            assert second.calls == []
        finally:
            first.shutdown()
            first.server_close()
    finally:
        second_server.shutdown()
        second_server.server_close()


def test_client_caps_responses_and_sanitizes_timeout_protocol_and_404():
    recorder = _Recorder()
    recorder.body = b"{" + (b"x" * 5000)
    server = _server(recorder)
    try:
        client = _client(server)
        with pytest.raises(N8nError) as caught:
            client.get_json("/api/v1/workflows")
        assert caught.value.code == "protocol_error"
        assert "xxxx" not in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()

    recorder = _Recorder()
    recorder.status = 404
    recorder.body = b'{"message":"workflow /secret/path missing"}'
    server = _server(recorder)
    try:
        with pytest.raises(N8nError) as caught:
            _client(server).get_json("/api/v1/workflows/missing")
        assert caught.value.code == "not_found"
        assert "secret" not in str(caught.value)
        assert "/secret" not in caught.value.public_error()["error"]["message"]
    finally:
        server.shutdown()
        server.server_close()

    recorder = _Recorder()
    recorder.body = b"not-json"
    recorder.headers = {"Content-Type": "text/plain"}
    server = _server(recorder)
    try:
        with pytest.raises(N8nError) as caught:
            _client(server).get_json("/api/v1/workflows")
        assert caught.value.code == "protocol_error"
    finally:
        server.shutdown()
        server.server_close()

    recorder = _Recorder()
    recorder.delay = 1.5
    server = _server(recorder)
    try:
        with pytest.raises(N8nError) as caught:
            _client(server, timeout_seconds=0.2).get_json("/api/v1/workflows")
        assert caught.value.code == "timeout"
    finally:
        server.shutdown()
        server.server_close()


def test_collection_lists_request_fixed_server_side_limit_without_caller_query():
    seen: list[str] = []

    def transport(url: str, **_kwargs: object) -> tuple[int, dict[str, str], bytes]:
        seen.append(url)
        return 200, {"Content-Type": "application/json"}, b'{"data":[]}'

    client = N8nClient(base_url="http://127.0.0.1:5678", api_key=TEST_HEADER_VALUE, transport=transport)
    client.list_workflows()
    client.list_executions()
    assert seen == [
        f"http://127.0.0.1:5678/api/v1/workflows?limit={MAX_COLLECTION + 1}",
        f"http://127.0.0.1:5678/api/v1/executions?limit={MAX_COLLECTION + 1}",
    ]
    assert all("cursor=" not in url for url in seen)
    with pytest.raises(N8nError):
        client.get_json(f"/api/v1/workflows?limit={MAX_COLLECTION + 1}")
    with pytest.raises(N8nError):
        client.get_json("/api/v1/workflows?limit=999")
    assert len(seen) == 2


def test_client_refuses_caller_supplied_url_fragments_and_unknown_actions():
    recorder = _Recorder()
    server = _server(recorder)
    try:
        client = _client(server)
        with pytest.raises(N8nError):
            client.mutate("activate-workflow", "wf1")
        with pytest.raises(N8nError):
            client.request_path("GET", "/api/v1/workflows/{id}", "wf1/../2")
        assert recorder.calls == []
    finally:
        server.shutdown()
        server.server_close()
