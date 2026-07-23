"""Brigade MCP bridge for Pi: tool discovery and calls against the canonical catalog.

Pi has no native MCP client. This module owns MCP initialization, tool discovery,
tool calls, per-call timeouts, and process cleanup for stdio and HTTP transports
defined in ``.brigade/mcp.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from . import mcp_cmd
from .mcp_adapters import CanonicalServer
from .mcp_runtime import (
    MCP_PROTOCOL_VERSION,
    MAX_HTTP_BODY_BYTES,
    MAX_STREAM_BYTES,
    _BoundedStreamReader,
    _kill_process_group,
    _read_json_response,
    _read_one_sse_json_response,
    _resolve_child_env,
    _resolve_headers,
    _rpc_result,
    _send_json_line,
    _validate_remote_url,
)
from .tools_cmd import HIGH_RISK_COMMAND_PATTERNS

QUALIFIED_SEPARATOR = "__"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 300.0
_READLINE_CHUNK_SIZE = 4096


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise urlerror.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


@dataclass(frozen=True)
class QualifiedTool:
    server: str
    name: str
    qualified_name: str
    description: str
    input_schema: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def qualify_tool_name(server_name: str, tool_name: str) -> str:
    return f"{server_name}{QUALIFIED_SEPARATOR}{tool_name}"


def split_qualified_name(qualified_name: str) -> tuple[str, str]:
    if QUALIFIED_SEPARATOR not in qualified_name:
        raise ValueError(f"qualified tool name must contain {QUALIFIED_SEPARATOR!r}: {qualified_name!r}")
    server, tool = qualified_name.split(QUALIFIED_SEPARATOR, 1)
    if not server or not tool:
        raise ValueError(f"qualified tool name is invalid: {qualified_name!r}")
    return server, tool


def _normalize_timeout(timeout: float | None, server: CanonicalServer) -> float:
    if timeout is not None:
        return min(max(float(timeout), 0.001), MAX_TIMEOUT_SECONDS)
    if server.timeout is not None:
        return min(float(server.timeout), MAX_TIMEOUT_SECONDS)
    return DEFAULT_TIMEOUT_SECONDS


def _tool_error(
    server: CanonicalServer,
    tool_name: str,
    *,
    message: str,
    failure_class: str,
) -> dict[str, Any]:
    return {
        "error": True,
        "server": server.name,
        "tool": tool_name,
        "qualified_name": qualify_tool_name(server.name, tool_name),
        "failure_class": failure_class,
        "message": message,
    }


def _normalize_input_schema(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _normalize_tools(server: CanonicalServer, tools: object) -> tuple[list[QualifiedTool], str | None]:
    if not isinstance(tools, list):
        return [], "tools/list result missing tools array"
    qualified: list[QualifiedTool] = []
    seen: set[str] = set()
    for item in tools:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        qualified_name = qualify_tool_name(server.name, name)
        if qualified_name in seen:
            continue
        seen.add(qualified_name)
        qualified.append(
            QualifiedTool(
                server=server.name,
                name=name,
                qualified_name=qualified_name,
                description=str(item.get("description") or ""),
                input_schema=_normalize_input_schema(item.get("inputSchema")),
            )
        )
    return qualified, None


def _stdio_messages_for_list() -> list[dict[str, Any]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "brigade-pi-bridge", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]


def _stdio_messages_for_call(tool_name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "brigade-pi-bridge", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    ]


def _read_stdio_responses(
    reader: _BoundedStreamReader,
    proc: subprocess.Popen[str],
    *,
    timeout: float,
    expected_ids: set[object],
) -> tuple[dict[object, dict[str, Any]], str | None]:
    deadline = time.monotonic() + timeout
    responses: dict[object, dict[str, Any]] = {}

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    while remaining() > 0 and len(responses) < len(expected_ids):
        line, read_error = reader.read_line(remaining())
        if read_error == "timeout":
            return responses, "timeout"
        if reader.overflow:
            return responses, "output exceeded size limit"
        if not line:
            if proc.poll() is not None:
                break
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return responses, "invalid JSON response"
        if not isinstance(payload, dict):
            return responses, "response was not a JSON object"
        request_id = payload.get("id")
        if request_id in expected_ids and ("result" in payload or "error" in payload):
            responses[request_id] = payload
    missing = expected_ids - set(responses)
    if missing:
        first_missing = next(iter(missing))
        return responses, f"missing JSON-RPC response for id={first_missing}"
    return responses, None


def _exchange_stdio(
    server: CanonicalServer,
    messages: list[dict[str, Any]],
    *,
    timeout: float,
) -> tuple[dict[object, dict[str, Any]], str | None]:
    if not server.command:
        return {}, "stdio server is missing a command"
    argv = [server.command, *server.args]
    joined_argv = " ".join(argv)
    if any(pattern.search(joined_argv) for pattern in HIGH_RISK_COMMAND_PATTERNS):
        return {}, "command shape is high risk"
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
        return {}, "failed to start server process"
    assert proc.stdout is not None
    assert proc.stderr is not None
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    reader = _BoundedStreamReader(proc.stdout, limit=MAX_STREAM_BYTES)
    stderr_reader = _BoundedStreamReader(proc.stderr, limit=MAX_STREAM_BYTES)
    expected_ids = {message["id"] for message in messages if "id" in message}
    try:
        for message in messages:
            _send_json_line(proc, message)
    except OSError:
        return {}, "failed to communicate with server process"
    try:
        responses, error = _read_stdio_responses(reader, proc, timeout=timeout, expected_ids=expected_ids)
        if stderr_reader.overflow:
            return responses, error or "output exceeded size limit"
        return responses, error
    finally:
        _kill_process_group(proc, pgid=pgid)


def _exchange_http(
    server: CanonicalServer,
    messages: list[dict[str, Any]],
    *,
    timeout: float,
) -> tuple[dict[object, dict[str, Any]], str | None]:
    if not server.url:
        return {}, "remote server is missing a URL"
    url = server.url
    url_error = _validate_remote_url(url)
    if url_error:
        return {}, url_error
    opener = urlrequest.build_opener(_NoRedirectHandler())
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    headers.update(_resolve_headers(server))
    session_holder: dict[str, str | None] = {"id": None}
    deadline = time.monotonic() + timeout
    responses: dict[object, dict[str, Any]] = {}

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    def post(message: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        req_timeout = remaining()
        if req_timeout <= 0:
            return None, "timeout"
        body = json.dumps(message).encode("utf-8")
        if len(body) > MAX_HTTP_BODY_BYTES:
            return None, "request exceeded size limit"
        req_headers = dict(headers)
        if session_holder["id"]:
            req_headers["Mcp-Session-Id"] = session_holder["id"]
        try:
            req = urlrequest.Request(url, data=body, headers=req_headers, method="POST")
            with opener.open(req, timeout=req_timeout) as resp:
                session_holder["id"] = resp.headers.get("Mcp-Session-Id") or session_holder["id"]
                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/event-stream" in content_type:
                    payload, parse_error = _read_one_sse_json_response(
                        resp,
                        deadline=deadline,
                        expected_id=message.get("id"),
                    )
                else:
                    raw = resp.read(MAX_HTTP_BODY_BYTES + 1)
                    payload, parse_error = _read_json_response(raw)
                return payload, parse_error
        except urlerror.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return None, "redirects are disabled"
            return None, "HTTP request failed"
        except urlerror.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, OSError)):
                return None, "timeout"
            return None, "connection failed"
        except (TimeoutError, OSError):
            return None, "timeout"
        except ValueError:
            return None, "invalid HTTP request"

    for message in messages:
        if message.get("method", "").startswith("notifications/"):
            _, notify_error = post(message)
            if notify_error in {"timeout", "connection failed"}:
                return responses, notify_error
            continue
        payload, post_error = post(message)
        if post_error:
            return responses, post_error
        if payload is None:
            return responses, "missing JSON-RPC response"
        request_id = message.get("id")
        if request_id is not None:
            responses[request_id] = payload
    return responses, None


def _exchange(
    server: CanonicalServer,
    messages: list[dict[str, Any]],
    *,
    timeout: float,
) -> tuple[dict[object, dict[str, Any]], str | None]:
    if server.is_remote:
        return _exchange_http(server, messages, timeout=timeout)
    return _exchange_stdio(server, messages, timeout=timeout)


def qualified_name_errors(servers: dict[str, CanonicalServer]) -> list[str]:
    ambiguous = [name for name in sorted(servers) if QUALIFIED_SEPARATOR in name]
    return [
        f"MCP server name {name!r} contains reserved separator {QUALIFIED_SEPARATOR!r}: "
        "the Pi bridge cannot qualify its tools unambiguously"
        for name in ambiguous
    ]


def enabled_servers(target: Path) -> tuple[dict[str, CanonicalServer], list[str]]:
    servers, errors, _warnings = mcp_cmd.load_canonical(target)
    if errors:
        return {}, errors
    enabled = {name: server for name, server in servers.items() if server.enabled}
    errors = qualified_name_errors(enabled)
    if errors:
        return {}, errors
    return enabled, []


def list_server_tools(
    server: CanonicalServer,
    *,
    timeout: float | None = None,
) -> tuple[list[QualifiedTool], dict[str, Any] | None]:
    effective_timeout = _normalize_timeout(timeout, server)
    responses, error = _exchange(server, _stdio_messages_for_list(), timeout=effective_timeout)
    if error:
        return [], _tool_error(server, "", message=error, failure_class="protocol_failure")
    init_payload = responses.get(1)
    init_result, init_error = _rpc_result(init_payload or {})
    if init_error or init_result is None:
        return [], _tool_error(
            server,
            "",
            message=init_error or "initialize failed",
            failure_class="protocol_failure",
        )
    tools_payload = responses.get(2)
    tools_result, tools_error = _rpc_result(tools_payload or {})
    if tools_error or tools_result is None:
        return [], _tool_error(
            server,
            "",
            message=tools_error or "tools/list failed",
            failure_class="protocol_failure",
        )
    tools, normalize_error = _normalize_tools(server, tools_result.get("tools"))
    if normalize_error:
        return [], _tool_error(server, "", message=normalize_error, failure_class="protocol_failure")
    return tools, None


def call_server_tool(
    server: CanonicalServer,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    effective_timeout = _normalize_timeout(timeout, server)
    responses, error = _exchange(
        server,
        _stdio_messages_for_call(tool_name, arguments),
        timeout=effective_timeout,
    )
    if error:
        return None, _tool_error(server, tool_name, message=error, failure_class="protocol_failure")
    call_payload = responses.get(3)
    if not call_payload:
        return None, _tool_error(
            server,
            tool_name,
            message="missing tools/call response",
            failure_class="protocol_failure",
        )
    if call_payload.get("error"):
        error_obj = call_payload.get("error")
        message = error_obj.get("message") if isinstance(error_obj, dict) else str(error_obj)
        return None, _tool_error(
            server,
            tool_name,
            message=str(message or "JSON-RPC error response"),
            failure_class="tool_failure",
        )
    result = call_payload.get("result")
    if not isinstance(result, dict):
        return None, _tool_error(
            server,
            tool_name,
            message="missing JSON-RPC result object",
            failure_class="protocol_failure",
        )
    if result.get("isError") is True:
        content = result.get("content")
        messages = (
            [
                str(item["text"])
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            ]
            if isinstance(content, list)
            else []
        )
        return None, _tool_error(
            server,
            tool_name,
            message="\n".join(messages) or "MCP tool reported an error",
            failure_class="tool_failure",
        )
    return result, None


def discover_tools(target: Path, *, timeout: float | None = None) -> dict[str, Any]:
    servers, errors = enabled_servers(target)
    if errors:
        return {"target": str(target), "tools": [], "servers": [], "errors": errors}
    tools: list[QualifiedTool] = []
    server_reports: list[dict[str, Any]] = []
    seen_qualified: set[str] = set()
    for name in sorted(servers):
        server = servers[name]
        server_tools, server_error = list_server_tools(server, timeout=timeout)
        if server_error:
            server_reports.append({"server": name, "status": "error", "error": server_error})
            continue
        collisions = [tool.qualified_name for tool in server_tools if tool.qualified_name in seen_qualified]
        for tool in server_tools:
            seen_qualified.add(tool.qualified_name)
            tools.append(tool)
        server_reports.append(
            {
                "server": name,
                "status": "ok",
                "transport": server.transport,
                "tool_count": len(server_tools),
                "collisions": collisions,
            }
        )
    tools.sort(key=lambda item: item.qualified_name)
    return {
        "target": str(target),
        "tools": [tool.to_public_dict() for tool in tools],
        "servers": server_reports,
        "errors": [],
    }


def call_qualified_tool(
    target: Path,
    qualified_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    try:
        server_name, tool_name = split_qualified_name(qualified_name)
    except ValueError as exc:
        return {
            "error": True,
            "qualified_name": qualified_name,
            "message": str(exc),
            "failure_class": "invalid_tool",
        }
    servers, errors = enabled_servers(target)
    if errors:
        return {"error": True, "qualified_name": qualified_name, "message": errors[0], "failure_class": "catalog_error"}
    server = servers.get(server_name)
    if server is None:
        return {
            "error": True,
            "server": server_name,
            "tool": tool_name,
            "qualified_name": qualified_name,
            "message": f"unknown MCP server: {server_name}",
            "failure_class": "unknown_server",
        }
    result, error = call_server_tool(server, tool_name, arguments, timeout=timeout)
    if error:
        return error
    return {
        "error": False,
        "server": server_name,
        "tool": tool_name,
        "qualified_name": qualified_name,
        "result": result,
    }
