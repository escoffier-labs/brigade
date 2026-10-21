"""Bounded action client, using the existing protected fleet credential loader."""

from __future__ import annotations

from typing import Any

from . import fleet_client, fleet_client_policy
from .fleet_action_auth import ActionError


def request(body: dict[str, Any], *, admin: bool = False, timeout: float = 2.5) -> dict[str, Any]:
    settings = fleet_client.load_fleet_settings()
    hub, token = settings["hub_url"], settings["admin_token" if admin else "node_token"]
    if not hub or not token:
        raise fleet_client.FleetClientError(
            "action request requires the configured hub and its own admin or node credential"
        )
    status, payload = fleet_client_policy._request(
        "POST", "/actions", hub_url=hub, token=token, body=body, timeout=timeout
    )
    if status != 200 or not isinstance(payload, dict):
        # Only stable, secret-free codes cross this boundary.
        reason = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(reason, str) or not reason.replace("-", "").isalnum() or len(reason) > 64:
            reason = "action-request-refused"
        raise ActionError(reason, status)
    return payload
