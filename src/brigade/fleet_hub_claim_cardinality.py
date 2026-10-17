"""Per-node active claim policy, independent of claim fencing and generations."""

from __future__ import annotations

import sqlite3
from typing import Any

from . import fleet_hub

MAX_ACTIVE_CLAIMS_PER_NODE = 1024


class ClaimCardinalityError(fleet_hub.FleetHubError):
    """A principal cannot add another live claim without releasing one."""

    def payload(self) -> dict[str, Any]:
        return {
            "granted": False,
            "reason": "claim-cardinality-exceeded",
            # Base clients classify a 409 without a dict owner as missing,
            # then admit local-only work. No target holder exists here.
            "owner": {},
            "error": str(self),
            "limit": MAX_ACTIVE_CLAIMS_PER_NODE,
        }


def check_new_claim(conn: sqlite3.Connection, node_id: str, now: float) -> None:
    """Called under the acquisition write transaction, only for a new target."""
    count = conn.execute(
        "SELECT count(*) FROM (SELECT 1 FROM claims WHERE owner_node = ? AND expires_at > ? LIMIT ?)",
        (node_id, now, MAX_ACTIVE_CLAIMS_PER_NODE),
    ).fetchone()[0]
    if count >= MAX_ACTIVE_CLAIMS_PER_NODE:
        raise ClaimCardinalityError(f"active claims per node exceed limit {MAX_ACTIVE_CLAIMS_PER_NODE}")
