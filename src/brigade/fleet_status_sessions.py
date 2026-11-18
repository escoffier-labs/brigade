"""Slim session records for the Fleet Hub control-plane status response.

``brigade fleet policy status --json`` reads ``GET /policy/status`` through
:mod:`brigade.fleet_client_policy`, which refuses a body over 512 KiB. Session
rows from :func:`brigade.fleet_hub_policy.list_session_states` carry the whole
loaded and pending policy snapshot (``loaded_snapshot``, ``pending_snapshot``,
``sources``, ``effective``, ``selected``), which is most of the payload and
none of the use: the Deck stores session mappings without reading a field, and
the CLI only prints the JSON.

The status projection therefore keeps identity, state and revision facts and
drops the snapshots. The policy page, the session endpoints and the impact view
keep the full rows from ``list_session_states``, so nothing that renders a
snapshot loses it. New fields belong in :data:`STATUS_SESSION_FIELDS`; the
projection is not a place to pass a row through.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# Identity, state and revision facts the status response needs. Deliberately no
# loaded or pending snapshot field: each one repeated the policy document.
STATUS_SESSION_FIELDS: tuple[str, ...] = (
    "session_id",
    "receipt_key",
    "external_session_id",
    "consumer",
    "owner_node",
    "repo_identity",
    "origin",
    "source",
    "state",
    "context_state",
    "revision",
    "current_revision",
    "digest",
    "context_hash",
    "loaded_at",
    "refresh_state",
    "refresh_requested_at",
    "refresh_updated_at",
    "refresh_detail",
    "pending_apply_required",
    "reload",
)


def status_session(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one full session row onto :data:`STATUS_SESSION_FIELDS`."""
    return {field: row.get(field) for field in STATUS_SESSION_FIELDS}


def status_sessions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project every session row for the control-plane status response."""
    return [status_session(row) for row in rows]
