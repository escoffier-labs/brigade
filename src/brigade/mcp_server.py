"""Shared zero-dependency MCP server harness (stdio and loopback HTTP).

Several stations expose their local data over a read-only MCP server
(skills, memory cards). The JSON-RPC envelope and the request handling are identical;
only the resources and tools differ. Each station supplies four callbacks and
this module owns the protocol, so a new read-only MCP surface is a few lines.

Two transports share one protocol function, ``handle_message``:

* ``serve_stdio`` reads line-delimited JSON-RPC from stdin (the default).
* ``serve_http`` is a stateless Streamable HTTP endpoint bound to loopback only
  (``POST /mcp`` plus ``GET /healthz``), guarded by a bearer token read from
  ``BRIGADE_MCP_HTTP_TOKEN``. It has no sessions and no SSE stream.

Protocol version ``2026-07-28`` is stateless: there is no handshake, clients may
probe with ``server/discover``, and HTTP requests carry ``Mcp-Method`` and
``Mcp-Name`` headers that must match the body. Older versions keep working and
need none of that. A station can add capabilities and JSON-RPC methods (for
example MCP Events) through ``McpExtension`` without touching this module.
"""

from __future__ import annotations

import hmac
import json
import os
import signal
import socket
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from . import __version__

ResourceLister = Callable[[], list[dict[str, Any]]]
ResourceReader = Callable[[str], "tuple[str | None, str | None]"]
ToolLister = Callable[[], list[dict[str, Any]]]
ToolCaller = Callable[[str, dict[str, Any]], "tuple[object, bool]"]

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2026-07-28")
# Negotiation stays conservative: a client that offers an unknown version is told
# 2025-06-18, and 2026-07-28 is used only when the client asks for it.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
STATELESS_PROTOCOL_VERSION = "2026-07-28"
HEADER_MISMATCH_CODE = -32020

HTTP_TOKEN_ENV = "BRIGADE_MCP_HTTP_TOKEN"
HTTP_DEFAULT_HOST = "127.0.0.1"
HTTP_MIN_TOKEN_LENGTH = 32
HTTP_MAX_BODY_BYTES = 1024 * 1024
# Per-connection socket deadline: a peer that trickles headers or a declared body
# is dropped instead of holding a handler thread open.
HTTP_SOCKET_TIMEOUT_SECONDS = 10.0
# Connections served at once; extra connections are closed immediately.
HTTP_MAX_CONCURRENT_CONNECTIONS = 32
# Messages per JSON-RPC batch, so one 1 MiB body cannot fan out unbounded work.
HTTP_MAX_BATCH_MESSAGES = 64
HTTP_MCP_PATH = "/mcp"
HTTP_HEALTH_PATH = "/healthz"
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


class McpRpcError(Exception):
    """Raised by an extension method to return a JSON-RPC error with optional structured data."""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


ExtensionMethod = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class McpExtension:
    """Capabilities and JSON-RPC methods a station adds on top of resources and tools."""

    capabilities: Mapping[str, Any] = field(default_factory=dict)
    methods: Mapping[str, ExtensionMethod] = field(default_factory=dict)


def response(
    request_id: object, *, result: object | None = None, error: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    return payload


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def negotiate_protocol_version(requested: object) -> str:
    """Echo a supported client protocol version, otherwise offer the newest one."""
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return DEFAULT_PROTOCOL_VERSION


def handle_message(
    message: object,
    *,
    server_name: str,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
    extension: McpExtension | None = None,
) -> dict[str, Any] | None:
    """Handle one decoded JSON-RPC message and return its response.

    Returns None for notifications (no response is sent). read_resource returns
    (text, mime_type) or (None, None) when the uri is not served. call_tool
    returns (payload, is_error); a non-string payload is rendered as indented
    JSON.
    """
    if not isinstance(message, dict):
        return response(None, error={"code": -32600, "message": "invalid request"})
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if request_id is None and method.startswith("notifications/"):
        return None
    if not isinstance(message.get("method"), str):
        return response(request_id, error={"code": -32600, "message": "invalid request"})
    capabilities: dict[str, Any] = {"resources": {}, "tools": {}, **(extension.capabilities if extension else {})}
    if method == "initialize":
        return response(
            request_id,
            result={
                "protocolVersion": negotiate_protocol_version(params.get("protocolVersion")),
                "capabilities": capabilities,
                "serverInfo": {"name": server_name, "version": __version__},
            },
        )
    if method == "server/discover":
        # The stateless protocol has no handshake; this is the optional capability probe.
        return response(
            request_id,
            result={
                "resultType": "complete",
                "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "capabilities": capabilities,
                "serverInfo": {"name": server_name, "version": __version__},
            },
        )
    if method == "ping":
        return response(request_id, result={})
    if method == "resources/list":
        return response(request_id, result={"resources": list_resources()})
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        text, mime_type = read_resource(uri)
        if text is None:
            return response(request_id, error={"code": -32004, "message": f"resource not found: {uri}"})
        return response(request_id, result={"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]})
    if method == "tools/list":
        return response(request_id, result={"tools": list_tools()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        payload, failed = call_tool(name, arguments)
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
        return response(request_id, result={"content": [{"type": "text", "text": text}], "isError": failed})
    if extension is not None and method in extension.methods:
        # Stateless clients carry client identity in params._meta; the extension never needs it.
        try:
            return response(
                request_id, result=extension.methods[method]({k: v for k, v in params.items() if k != "_meta"})
            )
        except McpRpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return response(request_id, error=error)
    return response(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def serve_stdio(
    *,
    server_name: str,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
    extension: McpExtension | None = None,
) -> int:
    """Run a read-only MCP server over stdin/stdout until EOF."""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _emit(response(None, error={"code": -32700, "message": "parse error"}))
            continue
        reply = handle_message(
            request,
            server_name=server_name,
            list_resources=list_resources,
            read_resource=read_resource,
            list_tools=list_tools,
            call_tool=call_tool,
            extension=extension,
        )
        if reply is not None:
            _emit(reply)
    return 0


# ---------------------------------------------------------------------------
# Loopback Streamable HTTP transport
# ---------------------------------------------------------------------------


class McpHttpConfigError(ValueError):
    """Raised when the HTTP transport configuration is unsafe or invalid."""


@dataclass(frozen=True)
class HttpConfig:
    host: str
    port: int
    token: str


def validate_http_config(*, host: object, port: object, token: object) -> HttpConfig:
    """Validate host, port, and token before anything binds a socket."""
    if not isinstance(host, str):
        raise McpHttpConfigError("host must be 127.0.0.1, ::1, or localhost")
    host_value = host.strip().lower()
    if host_value == "localhost":
        host_value = "127.0.0.1"
    if host_value not in ("127.0.0.1", "::1"):
        raise McpHttpConfigError(
            f"refusing to bind host {host!r}: the HTTP transport serves loopback only (127.0.0.1, ::1, or localhost)"
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise McpHttpConfigError(f"invalid port {port!r}: expected an integer from 0 to 65535 (0 picks a free port)")
    if not isinstance(token, str) or len(token.strip()) < HTTP_MIN_TOKEN_LENGTH:
        raise McpHttpConfigError(
            f"{HTTP_TOKEN_ENV} must be set to a token of at least {HTTP_MIN_TOKEN_LENGTH} characters "
            '(for example: export BRIGADE_MCP_HTTP_TOKEN="$(openssl rand -hex 32)")'
        )
    return HttpConfig(host=host_value, port=port, token=token.strip())


def _authority_host(value: str) -> str | None:
    """Return the lowercase host of a Host header value, or None when malformed."""
    value = value.strip().lower()
    if not value or any(ch in value for ch in "/@?#\\ "):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None
        host, rest = value[1:end], value[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        return host
    host, sep, port = value.partition(":")
    if sep and not port.isdigit():
        return None
    return host


def _host_header_allowed(value: str | None, bound_host: str) -> bool:
    if value is None:
        return False
    host = _authority_host(value)
    return host is not None and (host in _LOOPBACK_HOSTNAMES or host == bound_host)


def _origin_allowed(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
        hostname = parts.hostname
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return False
    return (hostname or "").lower() in _LOOPBACK_HOSTNAMES


def _make_handler(
    *,
    server_name: str,
    token: bytes,
    bound_host: str,
    callbacks: dict[str, Any],
) -> type[BaseHTTPRequestHandler]:
    class McpHttpHandler(BaseHTTPRequestHandler):
        server_version = "brigade-mcp"
        timeout = HTTP_SOCKET_TIMEOUT_SECONDS
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Never log request lines with bodies or headers; only method/path/status.
            return

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            path = urlsplit(self.path).path if self.path.startswith("/") else "-"
            print(f"{self.command} {path} {code}", file=sys.stderr, flush=True)

        def _send(
            self, status: int, body: bytes = b"", *, headers: Mapping[str, str] | None = None, json_body: bool = True
        ) -> None:
            self.send_response(status)
            if body and json_body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # The client went away mid-response; nothing left to tell it.
                    self.close_connection = True

        def _send_json(self, status: int, payload: object, *, headers: Mapping[str, str] | None = None) -> None:
            self._send(status, json.dumps(payload, sort_keys=True).encode("utf-8"), headers=headers)

        def _error(self, status: int, message: str, *, headers: Mapping[str, str] | None = None) -> None:
            self._send_json(status, {"error": message}, headers=headers)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization") or ""
            scheme, _, credential = header.partition(" ")
            if scheme.lower() != "bearer":
                return False
            return hmac.compare_digest(credential.strip().encode("utf-8"), token)

        def _dispatch(self) -> None:
            # http.server rewrites a leading "//" in self.path, so judge the raw request line.
            request_line = self.requestline.split(" ")
            target = request_line[1] if len(request_line) == 3 else self.path
            if not target.startswith("/") or target.startswith("//"):
                self._error(400, "bad request target")
                return
            path = urlsplit(target).path
            if not _host_header_allowed(self.headers.get("Host"), bound_host):
                self._error(403, "forbidden host")
                return
            origin = self.headers.get("Origin")
            if origin is not None and not _origin_allowed(origin):
                self._error(403, "forbidden origin")
                return
            if path == HTTP_HEALTH_PATH:
                if self.command != "GET":
                    self._error(405, "method not allowed", headers={"Allow": "GET"})
                    return
                self._send_json(200, {"status": "ok"})
                return
            if path != HTTP_MCP_PATH:
                self._error(404, "not found")
                return
            if self.command != "POST":
                self._error(405, "method not allowed", headers={"Allow": "POST"})
                return
            if not self._authorized():
                self._error(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
                return
            # Streamable HTTP: a request naming an unsupported protocol version gets a 400.
            # Clients omit the header before initialize, so absence is accepted.
            protocol_version = self.headers.get("MCP-Protocol-Version")
            if protocol_version is not None and protocol_version.strip() not in SUPPORTED_PROTOCOL_VERSIONS:
                self._error(400, "unsupported MCP-Protocol-Version")
                return
            self._handle_mcp_post(protocol_version.strip() if protocol_version is not None else None)

        def _read_body(self) -> bytes | None:
            if self.headers.get("Transfer-Encoding") is not None:
                self._error(411, "chunked uploads are not supported; send Content-Length")
                return None
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._error(411, "Content-Length required")
                return None
            raw_length = raw_length.strip()
            if not raw_length.isascii() or not raw_length.isdigit():
                self._error(400, "malformed Content-Length")
                return None
            # Drop zero padding and compare digit counts before int(): int() raises on
            # digit strings past the interpreter's conversion limit.
            significant = raw_length.lstrip("0") or "0"
            if len(significant) > len(str(HTTP_MAX_BODY_BYTES)) or int(significant) > HTTP_MAX_BODY_BYTES:
                self._error(413, f"request body must be at most {HTTP_MAX_BODY_BYTES} bytes")
                return None
            try:
                return self.rfile.read(int(significant))
            except TimeoutError:
                self.close_connection = True
                return None

        def _routing_error(self, decoded: object, protocol_version: str | None) -> tuple[int, str] | None:
            """Enforce the version-gated routing contract; returns (JSON-RPC code, message) on a violation."""
            stateless = protocol_version == STATELESS_PROTOCOL_VERSION
            members = decoded if isinstance(decoded, list) else [decoded]
            if not stateless:
                # Event methods exist only in the stateless contract; an older version must not reach them.
                if any(
                    isinstance(m, dict) and isinstance(m.get("method"), str) and m["method"].startswith("events/")
                    for m in members
                ):
                    return -32600, f"events methods require MCP-Protocol-Version {STATELESS_PROTOCOL_VERSION}"
                return None
            if isinstance(decoded, list):
                # Headers can describe one request only, so a batch would bypass routing validation.
                return -32600, f"batch requests are not supported in {STATELESS_PROTOCOL_VERSION}"
            if not isinstance(decoded, dict) or not isinstance(decoded.get("method"), str):
                return None
            method = self.headers.get("Mcp-Method")
            if method is None:
                # Notifications carry no id and are not routed.
                return None if "id" not in decoded else (HEADER_MISMATCH_CODE, "Mcp-Method header required")
            if method != decoded["method"]:
                return HEADER_MISMATCH_CODE, "Mcp-Method or Mcp-Name header mismatch"
            if decoded["method"] == "tools/call":
                params = decoded.get("params")
                tool = params.get("name") if isinstance(params, dict) else None
                if not isinstance(tool, str) or self.headers.get("Mcp-Name") != tool:
                    return HEADER_MISMATCH_CODE, "Mcp-Method or Mcp-Name header mismatch"
            return None

        def _handle_mcp_post(self, protocol_version: str | None = None) -> None:
            body = self._read_body()
            if body is None:
                return
            try:
                decoded = json.loads(body.decode("utf-8"))
            except (ValueError, RecursionError):
                # ValueError covers UnicodeDecodeError and JSONDecodeError;
                # RecursionError is pathologically deep nesting.
                self._send_json(400, response(None, error={"code": -32700, "message": "parse error"}))
                return
            violation = self._routing_error(decoded, protocol_version)
            if violation is not None:
                code, message = violation
                self._send_json(
                    400,
                    response(
                        decoded.get("id") if isinstance(decoded, dict) else None,
                        error={"code": code, "message": message},
                    ),
                )
                return
            is_batch = isinstance(decoded, list)
            messages = decoded if is_batch else [decoded]
            if not messages or len(messages) > HTTP_MAX_BATCH_MESSAGES:
                self._send_json(400, response(None, error={"code": -32600, "message": "invalid request"}))
                return
            replies: list[dict[str, Any]] = []
            for message in messages:
                # Notifications (method, no id) and client responses (result or
                # error, no method) never get a reply. A methodless object with an
                # id that is not a response falls through to an invalid-request error.
                if isinstance(message, dict) and message.get("jsonrpc") != "2.0":
                    # Enforced over HTTP only; stdio keeps accepting legacy clients
                    # that omit the version field.
                    replies.append(response(message.get("id"), error={"code": -32600, "message": "invalid request"}))
                    continue
                if isinstance(message, dict):
                    is_notification = "id" not in message and isinstance(message.get("method"), str)
                    is_response = "method" not in message and ("result" in message or "error" in message)
                    if is_notification or is_response:
                        continue
                try:
                    reply = handle_message(message, server_name=server_name, **callbacks)
                except Exception:  # noqa: BLE001 - never leak internals or kill the handler thread
                    request_id = message.get("id") if isinstance(message, dict) else None
                    reply = response(request_id, error={"code": -32603, "message": "internal error"})
                if reply is not None:
                    replies.append(reply)
            if not replies:
                self._send(202)
                return
            self._send_json(200, replies if is_batch else replies[0])

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch  # noqa: N815

    return McpHttpHandler


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], *, ipv6: bool):
        if ipv6:
            self.address_family = socket.AF_INET6
        self._slots = threading.BoundedSemaphore(HTTP_MAX_CONCURRENT_CONNECTIONS)
        super().__init__(server_address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Refuse, rather than queue, connections beyond the cap so idle peers
        # cannot pin an unbounded number of threads.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a full traceback with local source paths.
        exc = sys.exc_info()[1]
        print(f"MCP HTTP connection error: {type(exc).__name__}", file=sys.stderr, flush=True)


def make_http_server(
    *,
    server_name: str,
    host: str,
    port: int,
    token: str,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
    extension: McpExtension | None = None,
) -> ThreadingHTTPServer:
    """Validate the config and return a bound, not yet serving, HTTP server."""
    config = validate_http_config(host=host, port=port, token=token)
    handler = _make_handler(
        server_name=server_name,
        token=config.token.encode("utf-8"),
        bound_host=config.host,
        callbacks={
            "list_resources": list_resources,
            "read_resource": read_resource,
            "list_tools": list_tools,
            "call_tool": call_tool,
            "extension": extension,
        },
    )
    return _LoopbackHTTPServer((config.host, config.port), handler, ipv6=config.host == "::1")


def http_endpoint_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    shown = f"[{host}]" if ":" in str(host) else str(host)
    return f"http://{shown}:{port}{HTTP_MCP_PATH}"


def serve_http(
    *,
    server_name: str,
    host: str,
    port: int,
    token: str,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
    extension: McpExtension | None = None,
) -> int:
    """Serve the read-only MCP endpoint on loopback until interrupted."""
    server = make_http_server(
        server_name=server_name,
        host=host,
        port=port,
        token=token,
        list_resources=list_resources,
        read_resource=read_resource,
        list_tools=list_tools,
        call_tool=call_tool,
        extension=extension,
    )
    label = server_name[len("brigade-") :] if server_name.startswith("brigade-") else server_name
    print(f"brigade {label} MCP listening on {http_endpoint_url(server)}", file=sys.stderr, flush=True)

    def _stop(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    return 0


def run_http_from_env(
    *,
    server_name: str,
    host: str | None,
    port: int | None,
    default_port: int,
    environ: Mapping[str, str] | None = None,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
    extension: McpExtension | None = None,
) -> int:
    """Validate host, port, and the env token, then serve; exit 2 on bad config."""
    env = os.environ if environ is None else environ
    try:
        config = validate_http_config(
            host=HTTP_DEFAULT_HOST if host is None else host,
            port=default_port if port is None else port,
            token=env.get(HTTP_TOKEN_ENV, ""),
        )
        return serve_http(
            server_name=server_name,
            host=config.host,
            port=config.port,
            token=config.token,
            list_resources=list_resources,
            read_resource=read_resource,
            list_tools=list_tools,
            call_tool=call_tool,
            extension=extension,
        )
    except McpHttpConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot bind MCP HTTP server: {exc}", file=sys.stderr)
        return 2
