"""Optional MCP event transport wrapper and request identity context."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import logging
from typing import Any, TYPE_CHECKING

from .contracts import DotError

if TYPE_CHECKING:
    from .events import EventService

IDENTITY: ContextVar[str | None] = ContextVar("dot_identity", default=None)


class EventError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        self.code, self.reason = (code, reason)


class EventsApp:
    """Authenticated inner ASGI layer; inject capabilities after SDK serialization."""

    def __init__(self, app: Any, service: EventService, *, enabled: bool) -> None:
        self.app, self.service, self.enabled = app, service, enabled

    @property
    def router(self) -> Any:
        return self.app.router

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> Any:
        if not self.enabled:
            return await self.app(scope, receive, send)
        if scope["type"] == "lifespan":
            worker = None

            async def wrapped_send(message: dict[str, Any]) -> None:
                nonlocal worker
                if message["type"] == "lifespan.startup.complete":
                    worker = asyncio.create_task(self.service.run())
                if message["type"] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed") and worker:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass
                await send(message)

            return await self.app(scope, receive, wrapped_send)
        message = await receive()
        request = json.loads(message.get("body", b"{}"))
        method = request.get("method")
        if method == "server/discover" or method in ("events/list", "events/subscribe", "events/unsubscribe"):
            response: dict[str, Any]
            headers = dict(scope.get("headers", []))
            if b"mcp-method" in headers and headers[b"mcp-method"].decode("ascii", "replace") != method:
                response = dict(error={"code": -32020, "message": "Method header mismatch"})
            else:
                try:
                    if method == "server/discover":
                        result = dict(
                            serverInfo={"name": "dot-t3", "version": "3"},
                            supportedVersions=["2026-07-28"],
                            capabilities={"tools": {}, "events": {}},
                        )
                    else:
                        params = request.get("params", {})
                        if not isinstance(params, dict):
                            raise EventError(-32602, "invalid_arguments")
                        params = {k: v for k, v in params.items() if k != "_meta"}
                        result = await self.service.rpc(IDENTITY.get(), method, params)
                    response = {"result": result}
                except EventError as error:
                    response = {
                        "error": {"code": error.code, "message": error.reason, "data": {"reason": error.reason}}
                    }
                except Exception as error:
                    code = error.code if isinstance(error, DotError) else ""
                    logging.getLogger("dot-events").warning(
                        "events_rpc_failed method=%s error=%s code=%s", method, type(error).__name__, code[:40]
                    )
                    response = {"error": {"code": -32603, "message": "events_unavailable"}}
            response.update(jsonrpc="2.0", id=request.get("id"))
            body = json.dumps(response, separators=(",", ":")).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
                }
            )
            return await send({"type": "http.response.body", "body": body})
        consumed = False

        async def replay() -> Any:
            nonlocal consumed
            if not consumed:
                consumed = True
                return message
            return await receive()

        if method != "initialize":
            return await self.app(scope, replay, send)
        messages: list[dict[str, Any]] = []

        async def capture(msg: dict[str, Any]) -> None:
            messages.append(msg)

        await self.app(scope, replay, capture)
        body = b"".join((m.get("body", b"") for m in messages if m["type"] == "http.response.body"))
        try:
            response = json.loads(body)
            result = response["result"]
            result["capabilities"]["events"] = {}
            result["supportedVersions"] = ["2026-07-28"]
            body = json.dumps(response, separators=(",", ":")).encode()
            start = next((m for m in messages if m["type"] == "http.response.start"))
            start = {**start, "headers": [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]}
            await send(start)
            await send({"type": "http.response.body", "body": body})
        except (KeyError, ValueError, StopIteration):
            for msg in messages:
                await send(msg)
