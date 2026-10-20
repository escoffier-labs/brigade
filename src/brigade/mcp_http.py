"""Loopback Streamable HTTP transport for the shared MCP dispatcher.

Phase 1 binds only to loopback, requires a bearer token read from a private
file, rejects non-loopback Origin headers (DNS rebinding), and answers each POST
with one JSON body: no server-sent events and no sessions.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import stat
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .mcp_server import Handlers, dispatch, response

LOOPBACK_HOSTS = ("127.0.0.1", "::1")
ORIGIN_HOSTS = ("127.0.0.1", "::1", "localhost")
MCP_PATH = "/mcp"
MAX_BODY_BYTES = 1024 * 1024


def parse_bind(value: str) -> tuple[str, int]:
    host, sep, port_text = value.rpartition(":")
    host = host.strip("[]")
    if not sep or host not in LOOPBACK_HOSTS:
        raise ValueError(f"--http must bind to 127.0.0.1 or [::1], got {value!r}")
    if not port_text.isdigit() or int(port_text) > 65535:
        raise ValueError(f"--http port must be 0-65535, got {port_text!r}")
    return host, int(port_text)


def read_token(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"bearer file must be a regular file, not a symlink: {path}")
    if info.st_mode & 0o077:
        raise ValueError(f"bearer file must be mode 0600: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"bearer file is empty: {path}")
    return token


def ensure_token(path: Path) -> Path:
    """Create a random bearer token at path (mode 0600) unless one already exists."""
    if path.exists() or path.is_symlink():
        read_token(path)
        return path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")
    return path


def _origin_allowed(origin: str | None) -> bool:
    if origin is None:
        return True
    return urlsplit(origin).hostname in ORIGIN_HOSTS


def make_handler(handlers: Handlers, token: str) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {token}".encode()

    class McpRequestHandler(BaseHTTPRequestHandler):
        server_version = "brigade-mcp"

        def log_message(self, format: str, *args: object) -> None:
            return  # keep request lines out of logs; tool arguments are user content

        def _send(self, status: int, payload: object | None = None) -> None:
            body = b"" if payload is None else json.dumps(payload, sort_keys=True).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _admitted(self) -> bool:
            if urlsplit(self.path).path != MCP_PATH:
                self._send(404, {"error": "not found"})
                return False
            if not _origin_allowed(self.headers.get("Origin")):
                self._send(403, {"error": "origin not allowed"})
                return False
            supplied = (self.headers.get("Authorization") or "").encode()
            if not hmac.compare_digest(supplied, expected):
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if self._admitted():
                self._send(405, {"error": "use POST"})

        do_PUT = do_GET
        do_PATCH = do_GET
        do_DELETE = do_GET

        def do_POST(self) -> None:
            if not self._admitted():
                return
            length_text = self.headers.get("Content-Length") or ""
            if not length_text.isdigit():
                self._send(411, {"error": "content length required"})
                return
            if int(length_text) > MAX_BODY_BYTES:
                self._send(413, {"error": "request too large"})
                return
            raw = self.rfile.read(int(length_text))
            try:
                request = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, response(None, error={"code": -32700, "message": "parse error"}))
                return
            if isinstance(request, list):
                self._send(400, response(None, error={"code": -32600, "message": "batch requests are not supported"}))
                return
            reply = dispatch(request, handlers)
            if reply is None:
                self._send(202)
                return
            self._send(200, reply)

    return McpRequestHandler


class _LoopbackServer(HTTPServer):
    def __init__(self, host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> None:
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, port), handler)


def build_server(handlers: Handlers, *, host: str, port: int, token: str) -> HTTPServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing non-loopback bind: {host}")
    return _LoopbackServer(host, port, make_handler(handlers, token))


def serve_http(handlers: Handlers, *, host: str, port: int, token: str) -> int:
    server = build_server(handlers, host=host, port=port, token=token)
    shown = f"[{host}]" if ":" in host else host
    print(
        f"brigade memory MCP listening on http://{shown}:{server.server_address[1]}{MCP_PATH}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
