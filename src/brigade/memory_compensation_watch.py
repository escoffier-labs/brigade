"""Scheduled watch transport. The hub resolves verifier-attested signals."""

from __future__ import annotations

from typing import Any

from . import memory_hub_client as hubc

CAPABILITY = "automatic_compensation"


def run(
    client: hubc.MemoryHubClient,
    scopes: tuple[str, ...],
    epoch: str,
) -> list[dict[str, Any]]:
    status = client.call("status")
    if CAPABILITY not in status.get("capabilities", []):
        return []
    results = []
    for scope in scopes:
        fields: dict[str, Any] = {"scope": scope, "base_epoch": epoch}
        seen = set()
        for _ in range(32):
            response = client.call("compensation_watch", client_event_id=hubc.event_id(), **fields)
            results.extend(response["results"])
            after = response.get("next_after")
            if after is None:
                break
            if not isinstance(after, str) or hubc.event_id_ms(after) is None or after in seen:
                raise ValueError("invalid compensation watch cursor")
            seen.add(after)
            fields["after"] = after
        else:
            raise ValueError("compensation watch page bound exceeded")
    return results
