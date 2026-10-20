"""Shared zero-dependency MCP JSON-RPC dispatch and stdio loop.

Stations expose local data over MCP (skills, memory cards). The JSON-RPC
envelope and the request loop are identical;
only the resources and tools differ. Each station supplies four callbacks and
this module owns the protocol, so a new read-only MCP surface is a few lines.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Callable

ResourceLister = Callable[[], list[dict[str, Any]]]
ResourceReader = Callable[[str], "tuple[str | None, str | None]"]
ToolLister = Callable[[], list[dict[str, Any]]]
ToolCaller = Callable[[str, dict[str, Any]], "tuple[object, bool]"]


def response(
    request_id: object, *, result: object | None = None, error: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result if result is not None else {}
    return payload


SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LEGACY_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class Handlers:
    server_name: str
    list_resources: ResourceLister
    read_resource: ResourceReader
    list_tools: ToolLister
    call_tool: ToolCaller


def negotiate_protocol(requested: object) -> str:
    """Echo a supported client version; answer the newest for an unknown one.

    A request that omits the version keeps the legacy answer older stdio
    clients already received.
    """
    if not isinstance(requested, str) or not requested:
        return LEGACY_PROTOCOL_VERSION
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return SUPPORTED_PROTOCOL_VERSIONS[0]


def dispatch(request: object, handlers: Handlers) -> dict[str, Any] | None:
    """Answer one decoded JSON-RPC message. None means send nothing (a notification)."""
    if not isinstance(request, dict):
        return response(None, error={"code": -32600, "message": "invalid request"})
    request_id = request.get("id")
    method = str(request.get("method") or "")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    if request_id is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        return response(
            request_id,
            result={
                "protocolVersion": negotiate_protocol(params.get("protocolVersion")),
                "capabilities": {"resources": {}, "tools": {}},
                "serverInfo": {"name": handlers.server_name, "version": "0"},
            },
        )
    if method == "ping":
        return response(request_id, result={})
    if method == "resources/list":
        return response(request_id, result={"resources": handlers.list_resources()})
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        text, mime_type = handlers.read_resource(uri)
        if text is None:
            return response(request_id, error={"code": -32004, "message": f"resource not found: {uri}"})
        return response(request_id, result={"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]})
    if method == "tools/list":
        return response(request_id, result={"tools": handlers.list_tools()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        payload, failed = handlers.call_tool(name, arguments)
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
        return response(request_id, result={"content": [{"type": "text", "text": text}], "isError": failed})
    return response(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def serve_stdio_handlers(handlers: Handlers) -> int:
    """Run an MCP server over stdin/stdout until EOF."""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _emit(response(None, error={"code": -32700, "message": "parse error"}))
            continue
        reply = dispatch(request, handlers)
        if reply is not None:
            _emit(reply)
    return 0


def serve_stdio(
    *,
    server_name: str,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
) -> int:
    """Run an MCP server over stdin/stdout until EOF.

    read_resource returns (text, mime_type) or (None, None) when the uri is not
    served. call_tool returns (payload, is_error); a non-string payload is
    rendered as indented JSON.
    """
    return serve_stdio_handlers(Handlers(server_name, list_resources, read_resource, list_tools, call_tool))
