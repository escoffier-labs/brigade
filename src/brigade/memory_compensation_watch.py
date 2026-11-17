"""Scheduled watch transport with hub-owned continuation and attested signals.

Each scope polls at most 32 pages of 32 applies. A cycle fixes its high-water
mark at the maximum eligible proposal id at its start and covers those N applies
within ceil(N / 1024) runs. Any proposal that remains eligible is scanned within
two cycles, including proposals that become eligible behind the current cursor.
No workspace cursor is read or written. Only the hub advances or resets progress.
"""

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
        # Exhausting the budget is a successful poll. The hub commits each
        # page continuation, so the next scheduled run resumes without local state.
    return results
