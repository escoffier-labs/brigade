"""The /policy/status session projection stays inside the bounded client.

``brigade fleet policy status --json`` reads the status body through
``fleet_client_policy``, which refuses anything over 512 KiB. The live Hub
crossed that ceiling because every session row carried its full loaded and
pending policy snapshot. These tests seed real Hub tables with large snapshots
and pin both halves of the contract: status keeps the light facts, and
``list_session_states`` still returns the full rows for the policy page.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import pytest

from brigade import (
    fleet_command_deck,
    fleet_hub,
    fleet_hub_policy,
    fleet_hub_routing,
    fleet_policy,
    fleet_status_sessions,
)

NODE_A = "11111111-1111-4111-8111-111111111111"
SESSION_COUNT = 50
# A 50-session status body must stay well under the 512 KiB client ceiling.
SLIM_STATUS_LIMIT_BYTES = 64 * 1024
HEAVY_FIELDS = ("loaded_snapshot", "pending_snapshot", "sources", "effective", "selected")


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _document() -> dict[str, Any]:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"roles": {"impl": "seat-alpha"}, "data": {"allow_training": False}},
        "machines": {"worker-linux-1": {"os": "linux", "node_id": NODE_A, "concurrency": 2}},
        "seats": {"seat-alpha": {"provider": "provider-a", "model": "model-a-1"}},
        "consumers": {"t3-fleet": {"reload": "refreshable"}},
        "repositories": {"repo/public": {"privacy": "public"}},
    }


def _snapshot_payload(label: str) -> dict[str, Any]:
    """A policy-shaped snapshot about as heavy as the live session rows."""
    return {
        "roles": {f"role-{index}": "seat-alpha" for index in range(30)},
        "notes": [f"{label} policy note {index}" for index in range(45)],
        "bindings": {
            f"consumer-{index}": {"cli": "cursor-agent", "model": "model-a-1", "tier": "standard"}
            for index in range(12)
        },
    }


def _context_hash(index: int) -> str:
    return "sha256:" + hashlib.sha256(f"session-{index}".encode("utf-8")).hexdigest()


def _seed_session(conn: sqlite3.Connection, index: int) -> None:
    """Record a pending snapshot, acknowledge it, then leave a newer pending behind."""
    current = fleet_hub_policy.current_policy(conn)
    pending = fleet_hub_policy.record_pending_policy(
        conn,
        session_id=f"session-{index:02d}",
        consumer="t3-fleet",
        node_id=NODE_A,
        repo_identity="repo/public",
        origin="t3-fleet",
        revision=current["revision"],
        digest=current["digest"],
        overrides={},
        override_reason=None,
        effective=_snapshot_payload(f"loaded-{index}"),
        sources=_snapshot_payload(f"sources-{index}"),
        selected={},
        context_hash=_context_hash(index),
    )
    fleet_hub_policy.record_acknowledged_policy(
        conn, pending, node_id=NODE_A, expected_context_hash=_context_hash(index)
    )
    # Every later policy generation leaves the session another pending snapshot.
    fleet_hub_policy.record_pending_policy(
        conn,
        session_id=f"session-{index:02d}",
        consumer="t3-fleet",
        node_id=NODE_A,
        repo_identity="repo/public",
        origin="t3-fleet",
        revision=current["revision"],
        digest=current["digest"],
        overrides={},
        override_reason=None,
        effective=_snapshot_payload(f"pending-{index}"),
        sources=_snapshot_payload(f"pending-sources-{index}"),
        selected={},
        context_hash=_context_hash(index + 1000),
    )


def _seed_hub(conn: sqlite3.Connection) -> None:
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="tester", reason="install policy")
    for index in range(SESSION_COUNT):
        _seed_session(conn, index)


def _slim_body(status: dict[str, Any]) -> bytes:
    # Sorted separators match the Hub's canonical JSON rendering closely enough
    # to compare the status shapes, not the encoder.
    return json.dumps(status, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_status_sessions_keep_light_fields_and_drop_snapshots(conn):
    _seed_hub(conn)
    sessions = fleet_hub_routing.control_plane_status(conn)["sessions"]

    assert len(sessions) == SESSION_COUNT
    for row in sessions:
        assert set(row) == set(fleet_status_sessions.STATUS_SESSION_FIELDS)
        for heavy in HEAVY_FIELDS:
            assert heavy not in row

    first = next(row for row in sessions if row["external_session_id"] == "session-00")
    assert first["session_id"].startswith("p:")
    assert first["consumer"] == "t3-fleet"
    assert first["owner_node"] == NODE_A
    assert first["repo_identity"] == "repo/public"
    assert first["origin"] == "t3-fleet"
    assert first["revision"] == 2
    assert first["current_revision"] == 2
    assert first["digest"].startswith("sha256:")
    assert first["context_hash"] == _context_hash(0)
    assert first["state"] == "refreshable"
    assert first["context_state"] == "stale"
    assert first["pending_apply_required"] is True
    assert first["reload"] == "refreshable"
    assert first["receipt_key"] == first["session_id"]


def test_status_body_stays_small_for_fifty_sessions(conn):
    _seed_hub(conn)
    status = fleet_hub_routing.control_plane_status(conn)
    slim_body = _slim_body(status)
    assert len(slim_body) < SLIM_STATUS_LIMIT_BYTES, (
        f"status body is {len(slim_body)} bytes for {SESSION_COUNT} sessions; "
        f"the client ceiling is {SLIM_STATUS_LIMIT_BYTES} in this test"
    )

    full = dict(status)
    full["sessions"] = fleet_hub_policy.list_session_states(conn)
    full_body = _slim_body(full)
    # The full rows are what overflowed the live client: several times the slim body.
    assert len(full_body) > len(slim_body) * 4, (len(full_body), len(slim_body))


def test_list_session_states_still_returns_full_rows(conn):
    _seed_hub(conn)
    rows = fleet_hub_policy.list_session_states(conn)

    assert len(rows) == SESSION_COUNT
    first = next(row for row in rows if row["external_session_id"] == "session-00")
    for heavy in ("loaded_snapshot", "pending_snapshot", "sources", "effective"):
        assert heavy in first
    assert first["loaded_snapshot"]["effective"] == _snapshot_payload("loaded-0")
    assert first["pending_snapshot"]["effective"] == _snapshot_payload("pending-0")
    assert first["sources"] == _snapshot_payload("sources-0")
    assert first["effective"] == _snapshot_payload("loaded-0")
    # The heavy rows are orders of magnitude larger than the status projection.
    assert len(json.dumps(first)) > 5 * len(json.dumps(fleet_status_sessions.status_session(first)))


def test_status_drops_exactly_the_heavy_fields(conn):
    """A new light field on the full row must be added to status on purpose, not dropped silently."""
    _seed_hub(conn)
    full = fleet_hub_policy.list_session_states(conn)[0]
    assert set(full) - set(fleet_status_sessions.STATUS_SESSION_FIELDS) == set(HEAVY_FIELDS)
    assert set(fleet_status_sessions.STATUS_SESSION_FIELDS) <= set(full)


def test_deck_keeps_the_session_section_from_status(conn):
    _seed_hub(conn)
    status = fleet_hub_routing.control_plane_status(conn)
    plane = fleet_command_deck.control_plane_from_snapshot(status)

    assert plane is not None
    assert "sessions" in plane.present
    assert len(plane.sessions) == SESSION_COUNT
    assert {row["external_session_id"] for row in plane.sessions} >= {"session-00", "session-49"}
