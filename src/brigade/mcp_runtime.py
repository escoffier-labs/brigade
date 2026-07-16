"""Runtime MCP handshake probes for ``brigade mcp verify``.

Config sync proves projected files match the catalog; this module proves a server
can complete initialize + tools/list within bounded time. Probes never persist
raw process output, command arguments, env values, or secret material.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse
from uuid import uuid4

from . import localio
from .mcp_adapters import CanonicalServer
from .tools_cmd import HIGH_RISK_COMMAND_PATTERNS

VERIFY_RUNS_REL = ".brigade/mcp/verify-runs"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
MAX_STREAM_BYTES = 256 * 1024
MAX_HTTP_BODY_BYTES = 256 * 1024
_READLINE_CHUNK_SIZE = 4096
MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class VerifyResult:
    name: str
    transport: str
    config_current: bool
    runtime_healthy: bool
    failure_class: str | None
    detail: str
    protocol_version: str
    tool_count: int

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "config_current": self.config_current,
            "runtime_healthy": self.runtime_healthy,
            "failure_class": self.failure_class,
            "detail": self.detail,
            "protocol_version": self.protocol_version,
            "tool_count": self.tool_count,
        }


def verify_runs_root(target: Path) -> Path:
    return target / VERIFY_RUNS_REL


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_child_env(server: CanonicalServer) -> dict[str, str]:
    env = os.environ.copy()
    for key, spec in server.env.items():
        if "ref" in spec:
            value = os.environ.get(spec["ref"])
            if value is not None:
                env[key] = value
        elif "literal" in spec:
            env[key] = spec["literal"]
    return env


def _resolve_headers(server: CanonicalServer) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, spec in server.headers.items():
        if "ref" in spec:
            value = os.environ.get(spec["ref"])
            if value is not None:
                headers[key] = value
        elif "literal" in spec:
            headers[key] = spec["literal"]
    return headers


def _failure(
    name: str,
    transport: str,
    *,
    config_current: bool,
    failure_class: str,
    detail: str,
) -> VerifyResult:
    return VerifyResult(
        name=name,
        transport=transport,
        config_current=config_current,
        runtime_healthy=False,
        failure_class=failure_class,
        detail=detail,
        protocol_version="",
        tool_count=0,
    )


def _success(
    name: str, transport: str, *, config_current: bool, protocol_version: str, tool_count: int
) -> VerifyResult:
    return VerifyResult(
        name=name,
        transport=transport,
        config_current=config_current,
        runtime_healthy=True,
        failure_class=None,
        detail="",
        protocol_version=protocol_version,
        tool_count=tool_count,
    )


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urlerror.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


def _validate_remote_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported URL scheme: {parsed.scheme or '(none)'}"
    if not parsed.netloc:
        return "URL is missing a host"
    return None


def _read_json_response(body: bytes) -> tuple[dict[str, Any] | None, str | None]:
    if not body:
        return {}, None
    if len(body) > MAX_HTTP_BODY_BYTES:
        return None, "response exceeded size limit"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid JSON response"
    if not isinstance(payload, dict):
        return None, "response was not a JSON object"
    return payload, None


def _decode_sse_event_block(event_block: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = event_block.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid response"
    data_payload: str | None = None
    current_data: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            current_data.append(line[5:].lstrip())
        elif line == "":
            if current_data:
                data_payload = "\n".join(current_data)
                current_data = []
    if current_data:
        data_payload = "\n".join(current_data)
    if data_payload is None:
        return None, "invalid response"
    try:
        payload = json.loads(data_payload)
    except json.JSONDecodeError:
        return None, "invalid JSON response"
    if not isinstance(payload, dict):
        return None, "response was not a JSON object"
    return payload, None


def _parse_sse_json_response(body: bytes) -> tuple[dict[str, Any] | None, str | None]:
    if not body:
        return {}, None
    if len(body) > MAX_HTTP_BODY_BYTES:
        return None, "response exceeded size limit"
    return _decode_sse_event_block(body)


def _set_response_socket_timeout(resp: Any, timeout: float) -> None:
    fp = getattr(resp, "fp", None)
    if fp is None:
        return
    sock = getattr(fp, "_sock", None)
    if sock is None and hasattr(fp, "raw"):
        sock = getattr(fp.raw, "_sock", None)
    if sock is not None:
        sock.settimeout(max(0.0, timeout))


def _read_one_sse_json_response(resp: Any, *, deadline: float) -> tuple[dict[str, Any] | None, str | None]:
    event_lines: list[bytes] = []
    total_bytes = 0

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def read_line() -> tuple[bytes | None, str | None]:
        nonlocal total_bytes
        parts: list[bytes] = []
        while remaining() > 0:
            _set_response_socket_timeout(resp, remaining())
            try:
                chunk = resp.readline(_READLINE_CHUNK_SIZE + 1)
            except (TimeoutError, socket.timeout):
                return None, "timeout"
            if not chunk:
                return (b"".join(parts), None) if parts else (b"", None)
            if total_bytes + len(chunk) > MAX_HTTP_BODY_BYTES:
                return None, "response exceeded size limit"
            total_bytes += len(chunk)
            parts.append(chunk)
            joined = b"".join(parts)
            if joined.endswith(b"\n") or joined.endswith(b"\r\n"):
                return joined, None
        return None, "timeout"

    while remaining() > 0:
        line, read_error = read_line()
        if read_error:
            return None, read_error

        if not line:
            if not event_lines:
                return {}, None
            block = b"\n".join(event_lines)
            if len(block) > MAX_HTTP_BODY_BYTES:
                return None, "response exceeded size limit"
            return _decode_sse_event_block(block)

        stripped = line.rstrip(b"\r\n")
        if stripped == b"":
            block = b"\n".join(event_lines)
            if len(block) > MAX_HTTP_BODY_BYTES:
                return None, "response exceeded size limit"
            return _decode_sse_event_block(block)

        event_lines.append(stripped)

    return None, "timeout"


def _rpc_result(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if payload.get("error"):
        return None, "JSON-RPC error response"
    result = payload.get("result")
    if not isinstance(result, dict):
        return None, "missing JSON-RPC result object"
    return result, None


def _probe_http(server: CanonicalServer, *, config_current: bool, timeout: float) -> VerifyResult:
    name = server.name
    transport = server.transport
    if not server.url:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail="remote server is missing a URL",
        )
    url = server.url
    url_error = _validate_remote_url(url)
    if url_error:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail=url_error,
        )

    opener = urlrequest.build_opener(_NoRedirectHandler())
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    headers.update(_resolve_headers(server))
    session_id: str | None = None
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def post(message: dict[str, Any], *, include_session: bool) -> tuple[dict[str, Any] | None, str | None, str | None]:
        req_timeout = remaining()
        if req_timeout <= 0:
            return None, None, "timeout"
        body = json.dumps(message).encode("utf-8")
        if len(body) > MAX_HTTP_BODY_BYTES:
            return None, None, "request exceeded size limit"
        req_headers = dict(headers)
        if include_session and session_id:
            req_headers["Mcp-Session-Id"] = session_id
        try:
            req = urlrequest.Request(url, data=body, headers=req_headers, method="POST")
        except (ValueError, http.client.HTTPException):
            return None, None, "invalid HTTP request"
        try:
            with opener.open(req, timeout=req_timeout) as resp:
                new_session = resp.headers.get("Mcp-Session-Id")
                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/event-stream" in content_type:
                    payload, parse_error = _read_one_sse_json_response(resp, deadline=deadline)
                else:
                    raw = resp.read(MAX_HTTP_BODY_BYTES + 1)
                    payload, parse_error = _read_json_response(raw)
                return payload, new_session, parse_error
        except urlerror.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return None, None, "redirects are disabled"
            return None, None, "HTTP request failed"
        except urlerror.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return None, None, "timeout"
            return None, None, "connection_failed"
        except (TimeoutError, socket.timeout):
            return None, None, "timeout"
        except (ValueError, http.client.HTTPException):
            return None, None, "invalid HTTP request"

    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "brigade", "version": "1"},
        },
    }
    init_response, new_session, init_error = post(init_payload, include_session=False)
    if init_error == "timeout":
        return _failure(name, transport, config_current=config_current, failure_class="timeout", detail=init_error)
    if init_error == "connection_failed":
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="connection_failure",
            detail="connection failed",
        )
    if init_error or init_response is None:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail=init_error or "initialize failed",
        )
    init_result, result_error = _rpc_result(init_response)
    if result_error or init_result is None:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail=result_error or "initialize failed",
        )
    session_id = new_session
    protocol_version = str(init_result.get("protocolVersion") or "")
    if not protocol_version:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail="missing protocol version",
        )

    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    _, _, initialized_error = post(initialized, include_session=True)
    if initialized_error == "timeout":
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="timeout",
            detail=initialized_error,
        )
    if initialized_error == "connection_failed":
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="connection_failure",
            detail="connection failed",
        )
    if initialized_error:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail=initialized_error,
        )

    tools_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    tools_response, _, tools_error = post(tools_payload, include_session=True)
    if tools_error == "timeout":
        return _failure(name, transport, config_current=config_current, failure_class="timeout", detail=tools_error)
    if tools_error == "connection_failed":
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="connection_failure",
            detail="connection failed",
        )
    if tools_error or tools_response is None:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail=tools_error or "tools/list failed",
        )
    tools_result, tools_result_error = _rpc_result(tools_response)
    if tools_result_error or tools_result is None:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail=tools_result_error or "tools/list failed",
        )
    tools = tools_result.get("tools")
    if not isinstance(tools, list):
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="protocol_failure",
            detail="tools/list result missing tools array",
        )
    return _success(
        name,
        transport,
        config_current=config_current,
        protocol_version=protocol_version,
        tool_count=len(tools),
    )


class _BoundedStreamReader:
    def __init__(self, stream, *, limit: int) -> None:  # type: ignore[no-untyped-def]
        self._stream = stream
        self._limit = limit
        self._buffer = ""
        self._closed = False
        self._overflow = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.readline(_READLINE_CHUNK_SIZE)
                if not chunk:
                    break
                with self._lock:
                    if len(self._buffer) + len(chunk) > self._limit:
                        self._overflow = True
                        break
                    self._buffer += chunk
        finally:
            with self._lock:
                self._closed = True

    def read_line(self, timeout: float) -> tuple[str | None, str | None]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._overflow:
                    return None, "output exceeded size limit"
                if "\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\n", 1)
                    return line.strip(), None
                if self._closed and self._buffer:
                    line = self._buffer.strip()
                    self._buffer = ""
                    return line, None
                if self._closed:
                    return None, None
            threading.Event().wait(0.01)
        return None, "timeout"

    @property
    def overflow(self) -> bool:
        with self._lock:
            return self._overflow


def _kill_process_group(proc: subprocess.Popen[str], *, pgid: int | None = None) -> None:
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _send_json_line(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise OSError("stdin is not available")
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _probe_stdio(server: CanonicalServer, *, config_current: bool, timeout: float) -> VerifyResult:
    name = server.name
    transport = server.transport
    if not server.command:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="startup_failure",
            detail="stdio server is missing a command",
        )

    argv = [server.command, *server.args]
    joined_argv = " ".join(argv)
    if any(pattern.search(joined_argv) for pattern in HIGH_RISK_COMMAND_PATTERNS):
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="startup_failure",
            detail="command shape is high risk",
        )
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_resolve_child_env(server),
            shell=False,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return _failure(
            name,
            transport,
            config_current=config_current,
            failure_class="startup_failure",
            detail="failed to start server process",
        )

    assert proc.stdout is not None
    assert proc.stderr is not None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    stdout_reader = _BoundedStreamReader(proc.stdout, limit=MAX_STREAM_BYTES)
    stderr_reader = _BoundedStreamReader(proc.stderr, limit=MAX_STREAM_BYTES)
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    cleaned = False

    def cleanup() -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        _kill_process_group(proc, pgid=pgid)

    def fail(
        failure_class: str,
        detail: str,
    ) -> VerifyResult:
        return _failure(name, transport, config_current=config_current, failure_class=failure_class, detail=detail)

    try:
        try:
            _send_json_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "brigade", "version": "1"},
                    },
                },
            )
        except OSError:
            return fail("startup_failure", "failed to communicate with server process")

        init_line, init_error = stdout_reader.read_line(remaining())
        if init_error == "timeout":
            return fail("timeout", init_error)
        if stdout_reader.overflow or stderr_reader.overflow:
            return fail("protocol_failure", "output exceeded size limit")
        if not init_line:
            if proc.poll() is not None and proc.returncode not in (None, 0):
                return fail("startup_failure", f"process exited with code {proc.returncode}")
            return fail("protocol_failure", "no initialize response")

        try:
            init_payload = json.loads(init_line)
        except json.JSONDecodeError:
            return fail("protocol_failure", "initialize response was not valid JSON")

        init_result, result_error = _rpc_result(init_payload)
        if result_error or init_result is None:
            return fail("protocol_failure", result_error or "initialize failed")
        protocol_version = str(init_result.get("protocolVersion") or "")
        if not protocol_version:
            return fail("protocol_failure", "missing protocol version")

        try:
            _send_json_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_json_line(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        except OSError:
            return fail("startup_failure", "failed to communicate with server process")

        tools_line, tools_error = stdout_reader.read_line(remaining())
        if tools_error == "timeout":
            return fail("timeout", tools_error)
        if stdout_reader.overflow or stderr_reader.overflow:
            return fail("protocol_failure", "output exceeded size limit")
        if not tools_line:
            if proc.poll() is not None:
                return fail("protocol_failure", "no tools/list response")
            return fail("protocol_failure", "no tools/list response")

        try:
            tools_payload = json.loads(tools_line)
        except json.JSONDecodeError:
            return fail("protocol_failure", "tools/list response was not valid JSON")

        tools_result, tools_result_error = _rpc_result(tools_payload)
        if tools_result_error or tools_result is None:
            return fail("protocol_failure", tools_result_error or "tools/list failed")

        tools = tools_result.get("tools")
        if not isinstance(tools, list):
            return fail("protocol_failure", "tools/list result missing tools array")
        return _success(
            name,
            transport,
            config_current=config_current,
            protocol_version=protocol_version,
            tool_count=len(tools),
        )
    except Exception:
        return fail("protocol_failure", "runtime probe failed")
    finally:
        cleanup()


def probe_server(server: CanonicalServer, *, config_current: bool, timeout: float) -> VerifyResult:
    if server.is_remote:
        return _probe_http(server, config_current=config_current, timeout=timeout)
    return _probe_stdio(server, config_current=config_current, timeout=timeout)


def _server_timeout(server: CanonicalServer, override: float | None) -> float:
    if override is not None:
        return override
    if server.timeout is not None:
        return min(float(server.timeout), MAX_TIMEOUT_SECONDS)
    return DEFAULT_TIMEOUT_SECONDS


def run_verification(
    target: Path,
    servers: dict[str, CanonicalServer],
    *,
    config_current_by_name: dict[str, bool],
    timeout_override: float | None = None,
    filters: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    started = _utc_now()
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-mcp-verify-{uuid4().hex[:6]}"
    run_dir = verify_runs_root(target) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    receipt_path = run_dir / "receipt.json"

    results: list[VerifyResult] = []
    for name in sorted(servers):
        server = servers[name]
        if not server.enabled:
            continue
        result = probe_server(
            server,
            config_current=config_current_by_name.get(name, False),
            timeout=_server_timeout(server, timeout_override),
        )
        results.append(result)

    public_results = [result.to_public_dict() for result in results]
    unhealthy = any(not result.runtime_healthy for result in results)
    completed = _utc_now()
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "target": str(target),
        "status": "completed",
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "duration_seconds": (completed - started).total_seconds(),
        "path": str(run_dir),
        "receipt_path": str(receipt_path),
        "filters": filters,
        "results": public_results,
        "unhealthy_count": sum(1 for result in results if not result.runtime_healthy),
    }
    localio.write_json(receipt_path, receipt)
    payload = {
        "target": str(target),
        "status": "completed",
        "run_id": run_id,
        "receipt_path": str(receipt_path),
        "filters": filters,
        "results": public_results,
    }
    return payload, (1 if unhealthy else 0)
