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


# A per-scope grant refusal. A 401 or any other 403 still ends the run.
GRANT_MISSING = "memory_grant_missing"


def run(
    client: hubc.MemoryHubClient,
    scopes: tuple[str, ...],
    epoch: str,
    *,
    unwatched: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Poll each scope. Scopes this credential cannot reflect are appended to `unwatched`.

    A principal without the reflect right in a scope cannot have auto-applied
    there, so it has nothing to compensate in that scope. Another principal that
    holds the right watches its own applies.
    """
    status = client.call("status")
    if CAPABILITY not in status.get("capabilities", []):
        return []
    results = []
    for scope in scopes:
        fields: dict[str, Any] = {"scope": scope, "base_epoch": epoch}
        seen = set()
        for _ in range(32):
            try:
                response = client.call("compensation_watch", client_event_id=hubc.event_id(), **fields)
            except hubc.HubRefusal as exc:
                if exc.status != 403 or exc.code != GRANT_MISSING:
                    raise
                if unwatched is not None:
                    unwatched.append(scope)
                break
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
