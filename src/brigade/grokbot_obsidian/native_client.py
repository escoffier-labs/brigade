"""Fixed Streamable-HTTP client for the Local REST native MCP allowlist."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, NoReturn

from .adapters import NATIVE_MCP_TOOLS
from .operator_adapter import OPERATOR_ADAPTER_TOOLS
from .contracts import ERROR_MESSAGES, ObsidianError
from .runtime_config import required_upstream_url
from .tls import CONNECT_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES, pinned_fetch

CLIENT_NAME = "grokbot-obsidian-operator"
CLIENT_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"
MAX_RPC_ID = 1_000_000
MAX_REPLACEMENT_BYTES = 262_144
MAX_JSONRPC_ENVELOPE_BYTES = 16_384
MAX_OUTBOUND_REQUEST_BYTES = MAX_REPLACEMENT_BYTES + MAX_JSONRPC_ENVELOPE_BYTES
MAX_OUTBOUND_HARD_CEILING_BYTES = 524_288
MAX_REQUEST_BYTES = MAX_OUTBOUND_REQUEST_BYTES
SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")

if not MAX_OUTBOUND_REQUEST_BYTES < MAX_OUTBOUND_HARD_CEILING_BYTES:
    raise AssertionError("outbound cap must stay below the hard ceiling")


def _unavailable() -> NoReturn:
    raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"])


def _protocol() -> NoReturn:
    raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])


def encode_jsonrpc(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def tools_call_payload(name: str, arguments: Mapping[str, Any], request_id: int = MAX_RPC_ID) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments)},
    }


def outbound_tools_call_size(name: str, arguments: Mapping[str, Any], request_id: int = MAX_RPC_ID) -> int:
    return len(encode_jsonrpc(tools_call_payload(name, arguments, request_id)))


def assert_outbound_tools_call_fits(name: str, arguments: Mapping[str, Any]) -> None:
    if outbound_tools_call_size(name, arguments) > MAX_OUTBOUND_REQUEST_BYTES:
        raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])


def serialize_structured_replacement(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _header(headers: Mapping[str, Any], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected and isinstance(value, str):
            return value
    return ""


def _parse_json_rpc(body: bytes, request_id: int) -> object:
    if not body:
        _unavailable()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _unavailable()
    if not isinstance(payload, dict) or payload.get("id") != request_id:
        _protocol()
    if payload.get("error") is not None:
        _unavailable()
    if "result" not in payload:
        _protocol()
    return payload["result"]


def _parse_sse_json(body: bytes, request_id: int) -> object:
    current: list[str] = []
    for raw_line in body.splitlines():
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            _unavailable()
        if line == "":
            if not current:
                continue
            joined = "\n".join(current)
            current = []
            try:
                payload = json.loads(joined)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id") == request_id:
                if payload.get("error") is not None:
                    _unavailable()
                if "result" not in payload:
                    _protocol()
                return payload["result"]
            continue
        if line.startswith("data:"):
            current.append(line[5:].lstrip())
    _unavailable()
    raise AssertionError("unreachable")


class StreamableNativeMcpClient:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        fetch: Callable[..., object],
    ):
        if not isinstance(api_key, str) or len(api_key) < 16 or len(api_key) > 4096 or "\x00" in api_key:
            _unavailable()
        self._url = required_upstream_url(url)
        self._api_key = api_key
        self._fetch = fetch
        self._session_id: str | None = None
        self._next_id = 1
        self._ready = False
        self._closed = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: Mapping[str, Any]) -> object:
        if self._closed:
            _unavailable()
        body = encode_jsonrpc(payload)
        if len(body) > MAX_OUTBOUND_REQUEST_BYTES:
            _protocol()
        try:
            result = self._fetch(
                self._url,
                method="POST",
                headers=self._headers(),
                data=body,
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except ObsidianError:
            raise
        except Exception as exc:
            raise ObsidianError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
        if not isinstance(result, dict):
            _unavailable()
        raw_body = result.get("body")
        if not isinstance(raw_body, (bytes, bytearray)):
            _unavailable()
        if len(raw_body) > MAX_RESPONSE_BYTES:
            _unavailable()
        headers = result.get("headers")
        if not isinstance(headers, Mapping):
            _unavailable()
        session = _header(headers, "mcp-session-id")
        if session:
            if not SESSION_ID.fullmatch(session):
                _protocol()
            self._session_id = session
        return result

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None, *, notification: bool = False) -> object:
        if notification:
            payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = dict(params)
            self._post(payload)
            return None
        if not 1 <= self._next_id <= MAX_RPC_ID:
            _unavailable()
        request_id = self._next_id
        self._next_id += 1
        result = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})})
        if not isinstance(result, dict):
            _unavailable()
        headers = result.get("headers")
        body = result.get("body")
        if not isinstance(headers, Mapping) or not isinstance(body, (bytes, bytearray)):
            _unavailable()
        content_type = _header(headers, "content-type").casefold()
        if "text/event-stream" in content_type:
            return _parse_sse_json(bytes(body), request_id)
        return _parse_json_rpc(bytes(body), request_id)

    def _ensure_session(self) -> None:
        if self._ready:
            return
        initialized = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        if not isinstance(initialized, dict) or not initialized.get("protocolVersion"):
            _protocol()
        self._rpc("notifications/initialized", notification=True)
        self._ready = True

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> object:
        if name not in NATIVE_MCP_TOOLS and name not in OPERATOR_ADAPTER_TOOLS:
            _protocol()
        self._ensure_session()
        return self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})

    def list_tools(self) -> object:
        self._ensure_session()
        return self._rpc("tools/list", {})

    def close(self) -> None:
        self._closed = True
        self._ready = False
        self._session_id = None
        self._api_key = ""


def create_native_mcp_client(
    *,
    url: str,
    api_key: str,
    ca_bytes: bytes,
    pins: tuple[str, ...],
    fetch: Callable[..., object] | None = None,
) -> StreamableNativeMcpClient:
    transport = fetch

    def send(*args: Any, **kwargs: Any) -> object:
        nonlocal transport
        if transport is None:
            transport = pinned_fetch(ca_bytes, pins)
        return transport(*args, **kwargs)

    return StreamableNativeMcpClient(url=url, api_key=api_key, fetch=send)
