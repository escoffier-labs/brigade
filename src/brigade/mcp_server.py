"""Shared zero-dependency MCP stdio server harness.

Several stations expose their local data over a read-only MCP stdio server
(skills, memory cards). The JSON-RPC envelope and the request loop are identical;
only the resources and tools differ. Each station supplies four callbacks and
this module owns the protocol, so a new read-only MCP surface is a few lines.
"""

from __future__ import annotations

import json
import sys
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


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def serve_stdio(
    *,
    server_name: str,
    list_resources: ResourceLister,
    read_resource: ResourceReader,
    list_tools: ToolLister,
    call_tool: ToolCaller,
) -> int:
    """Run a read-only MCP server over stdin/stdout until EOF.

    read_resource returns (text, mime_type) or (None, None) when the uri is not
    served. call_tool returns (payload, is_error); a non-string payload is
    rendered as indented JSON.
    """
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _emit(response(None, error={"code": -32700, "message": "parse error"}))
            continue
        if not isinstance(request, dict):
            _emit(response(None, error={"code": -32600, "message": "invalid request"}))
            continue
        request_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if request_id is None and method.startswith("notifications/"):
            continue
        if method == "initialize":
            _emit(
                response(
                    request_id,
                    result={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"resources": {}, "tools": {}},
                        "serverInfo": {"name": server_name, "version": "0"},
                    },
                )
            )
        elif method == "resources/list":
            _emit(response(request_id, result={"resources": list_resources()}))
        elif method == "resources/read":
            uri = str(params.get("uri") or "")
            text, mime_type = read_resource(uri)
            if text is None:
                _emit(response(request_id, error={"code": -32004, "message": f"resource not found: {uri}"}))
            else:
                _emit(response(request_id, result={"contents": [{"uri": uri, "mimeType": mime_type, "text": text}]}))
        elif method == "tools/list":
            _emit(response(request_id, result={"tools": list_tools()}))
        elif method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            payload, failed = call_tool(name, arguments)
            text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True)
            _emit(response(request_id, result={"content": [{"type": "text", "text": text}], "isError": failed}))
        else:
            _emit(response(request_id, error={"code": -32601, "message": f"method not found: {method}"}))
    return 0
