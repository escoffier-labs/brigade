"""Bounded stdlib HTTP client for the n8n Public API."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping
from urllib.parse import urlunsplit, urlsplit

from .contracts import (
    ACTION_IDS,
    ERROR_MESSAGES,
    MAX_COLLECTION,
    MAX_RESPONSE_BYTES,
    N8nError,
    parse_action_id,
    parse_safe_path_segment,
)
from .runtime_config import validate_base_url

ACTION_PATHS = {
    "deactivate-workflow": "/api/v1/workflows/{id}/deactivate",
    "archive-workflow": "/api/v1/workflows/{id}/archive",
    "unarchive-workflow": "/api/v1/workflows/{id}/unarchive",
    "cancel-execution": "/api/v1/executions/{id}/stop",
}
DEFAULT_TIMEOUT_SECONDS = 5
Transport = Callable[..., tuple[int, Mapping[str, str], bytes]]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])


def _header(headers: Mapping[str, Any], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == expected:
            return str(value)
    return ""


def _build_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _closed_collection_query() -> str:
    return f"limit={MAX_COLLECTION + 1}"


def _build_collection_url(base_url: str, path: str) -> str:
    if path not in {"/api/v1/workflows", "/api/v1/executions"}:
        raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, path, _closed_collection_query(), ""))


def _read_bounded(response: Any, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(4096, maximum + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])
        chunks.append(chunk)
    return b"".join(chunks)


def _default_transport(
    url: str,
    *,
    method: str,
    headers: Mapping[str, str],
    timeout: float,
    max_response_bytes: int,
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(_RejectRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = _read_bounded(response, max_response_bytes)
            return int(response.status), dict(response.headers.items()), body
    except N8nError:
        raise
    except TimeoutError as exc:
        raise N8nError("timeout", ERROR_MESSAGES["timeout"]) from exc
    except urllib.error.HTTPError as exc:
        try:
            _read_bounded(exc, max_response_bytes)
        except N8nError:
            pass
        except OSError:
            pass
        if exc.code == 404:
            raise N8nError("not_found", ERROR_MESSAGES["not_found"]) from exc
        if exc.code in {401, 403}:
            raise N8nError("denied", ERROR_MESSAGES["denied"]) from exc
        if exc.code in {301, 302, 303, 307, 308}:
            raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"]) from exc
        raise N8nError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or (isinstance(reason, OSError) and getattr(reason, "errno", None) is None):
            message = str(reason).casefold() if reason is not None else ""
            if "timed out" in message or "timeout" in message:
                raise N8nError("timeout", ERROR_MESSAGES["timeout"]) from exc
        if isinstance(reason, socket.timeout):
            raise N8nError("timeout", ERROR_MESSAGES["timeout"]) from exc
        raise N8nError("unavailable", ERROR_MESSAGES["unavailable"]) from exc
    except socket.timeout as exc:
        raise N8nError("timeout", ERROR_MESSAGES["timeout"]) from exc
    except OSError as exc:
        raise N8nError("unavailable", ERROR_MESSAGES["unavailable"]) from exc


class N8nClient:
    """JSON-only n8n Public API client. The API key is never logged or returned."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
    ):
        self.base_url = validate_base_url(base_url)
        if not isinstance(api_key, str) or not api_key:
            raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._transport = transport

    def __repr__(self) -> str:
        return f"N8nClient(base_url={self.base_url!r})"

    def request_path(self, method: str, template: str, target_id: str) -> Any:
        identifier = parse_safe_path_segment(target_id)
        if "{id}" not in template or not template.startswith("/api/v1/"):
            raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
        return self.get_json(template.replace("{id}", identifier), method=method)

    def _request_json(self, url: str, *, method: str) -> Any:
        headers = {"X-N8N-API-KEY": self._api_key, "Accept": "application/json"}
        if self._transport is not None:
            status, response_headers, body = self._transport(
                url,
                method=method,
                headers=headers,
                timeout=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
            )
        else:
            status, response_headers, body = _default_transport(
                url,
                method=method,
                headers=headers,
                timeout=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
            )
        if status == 404:
            raise N8nError("not_found", ERROR_MESSAGES["not_found"])
        if status in {401, 403}:
            raise N8nError("denied", ERROR_MESSAGES["denied"])
        if status >= 300:
            raise N8nError("unavailable" if status >= 500 else "protocol_error", ERROR_MESSAGES["protocol_error"])
        content_type = _header(response_headers, "Content-Type").casefold()
        if "application/json" not in content_type:
            raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])
        if len(body) > self.max_response_bytes:
            raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"])
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise N8nError("protocol_error", ERROR_MESSAGES["protocol_error"]) from exc
        return payload

    def get_json(self, path: str, *, method: str = "GET") -> Any:
        if not isinstance(path, str) or not path.startswith("/api/v1/") or ".." in path or "?" in path or "#" in path:
            raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
        return self._request_json(_build_url(self.base_url, path), method=method)

    def list_workflows(self) -> Any:
        return self._request_json(_build_collection_url(self.base_url, "/api/v1/workflows"), method="GET")

    def get_workflow(self, workflow_id: str) -> Any:
        return self.request_path("GET", "/api/v1/workflows/{id}", workflow_id)

    def list_executions(self) -> Any:
        return self._request_json(_build_collection_url(self.base_url, "/api/v1/executions"), method="GET")

    def get_execution(self, execution_id: str) -> Any:
        return self.request_path("GET", "/api/v1/executions/{id}", execution_id)

    def mutate(self, action_id: str, target_id: str) -> Any:
        parsed = parse_action_id(action_id)
        if parsed not in ACTION_IDS:
            raise N8nError("invalid_request", ERROR_MESSAGES["invalid_request"])
        return self.request_path("POST", ACTION_PATHS[parsed], target_id)
