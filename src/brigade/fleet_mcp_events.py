"""MCP Events methods for the fleet station: ``events/list``, ``events/subscribe``, ``events/unsubscribe``.

The station holds no subscription state and takes no identity from the request.
Each method forwards to the Fleet Hub as the station's own principal credential,
and the hub authorizes, verifies the callback, stores the subscription and
delivers the webhooks (see ``fleet_hub_events``). Off unless the station is
started with ``--events``; the hub separately stays off until its operator names
callback hosts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import fleet_client_messages as messages
from .mcp_server import McpExtension, McpRpcError

# Seconds. The hub's callback verification can take ten seconds before it answers.
HUB_TIMEOUT_SECONDS = 30.0
CALLBACK_ENDPOINT_ERROR = -32015
_INVALID_PARAMS = -32602
_SERVER_ERROR = -32000
_HUB_CODES: dict[str, tuple[int, str]] = {
    "events_disabled": (_SERVER_ERROR, "events_disabled"),
    "events_invalid_arguments": (_INVALID_PARAMS, "invalid_arguments"),
    "events_invalid_ttl": (_INVALID_PARAMS, "invalid_ttl"),
    "events_invalid_delivery": (_INVALID_PARAMS, "invalid_delivery"),
    "events_forbidden": (_SERVER_ERROR, "forbidden"),
    "events_subscription_limit": (_SERVER_ERROR, "subscription_limit"),
    "events_busy": (_SERVER_ERROR, "busy"),
}
_CALLBACK_PREFIX = "events_callback_"


def _rpc_error(error: messages.FleetMessageHubError) -> McpRpcError:
    code = error.code if isinstance(error.code, str) else ""
    if code.startswith(_CALLBACK_PREFIX):
        reason = code[len(_CALLBACK_PREFIX) :]
        return McpRpcError(CALLBACK_ENDPOINT_ERROR, "callback_endpoint_error", {"reason": reason})
    if code in _HUB_CODES:
        rpc_code, reason = _HUB_CODES[code]
        return McpRpcError(rpc_code, reason, {"reason": reason})
    return McpRpcError(-32603, "events_unavailable")


class FleetEvents:
    def __init__(self, authority: Callable[[], messages.MessageAuthority]) -> None:
        self._authority = authority

    def _forward(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return messages._online(
                {"action": action, **params}, authority=self._authority(), timeout=HUB_TIMEOUT_SECONDS
            )
        except messages.FleetMessageHubError as error:
            raise _rpc_error(error) from None
        except Exception:  # noqa: BLE001 - credential or transport trouble must not leak details
            raise McpRpcError(-32603, "events_unavailable") from None

    def list(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) - {"cursor"} or params.get("cursor") not in (None, ""):
            raise McpRpcError(_INVALID_PARAMS, "invalid_arguments", {"reason": "invalid_arguments"})
        return self._forward("events_list", {})

    def subscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._forward("events_subscribe", params)

    def unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._forward("events_unsubscribe", params)

    def extension(self) -> McpExtension:
        return McpExtension(
            capabilities={"events": {}},
            methods={
                "events/list": self.list,
                "events/subscribe": self.subscribe,
                "events/unsubscribe": self.unsubscribe,
            },
        )
