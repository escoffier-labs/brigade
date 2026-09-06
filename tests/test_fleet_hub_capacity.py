"""Shared reservation + legacy lease occupancy helper."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_capacity, fleet_hub_policy, fleet_policy
from brigade.fleet_hub import FleetHubError


NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
SEAT = "seat-alpha"
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"data": {"allow_training": False}},
        "machines": {"worker-linux-1": {"os": "linux", "concurrency": 2, "node_id": NODE_A}},
        "seats": {
            SEAT: {
                "provider": "provider-a",
                "model": "model-a-1",
                "effort": "high",
                "concurrency": 1,
                "enabled": True,
                "bindings": {
                    "brigade": {"cli": "cli-alpha"},
                    "t3_fleet": {"instance_id": "inst-alpha"},
                    "native": {},
                },
            }
        },
        "consumers": {
            "brigade-run": {"reload": "refreshable", "coverage": "unverified"},
            "t3-fleet": {"reload": "none", "coverage": "unverified"},
        },
        "repositories": {"repo/public": {"privacy": "public"}},
    }


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    fleet_hub_policy.save_policy(connection, _document(), expected_version=1, actor="operator", reason="seed")
    try:
        yield connection
    finally:
        connection.close()


def _insert_reservation(
    conn, *, reservation_id, decision_id, session_id, node_id, accepted=1, released=False, expires=None
):
    stamp = (expires or NOW + timedelta(minutes=5)).isoformat()
    state = "released" if released else "active"
    conn.execute(
        "INSERT INTO fleet_reservations (reservation_id, decision_id, session_id, machine, seat, node_id, "
        "origin_node, state, accepted, expires_at, created_at, updated_at) "
        "VALUES (?, ?, ?, 'worker-linux-1', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reservation_id,
            decision_id,
            session_id,
            SEAT,
            node_id,
            NODE_A,
            state,
            int(accepted),
            stamp,
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()


def _insert_lease(
    conn,
    *,
    lease_id,
    node_id=NODE_A,
    released=False,
    expires=None,
    provider="provider-a",
    model="model-a-1",
    decision_id=None,
    session_id=None,
    context_hash=None,
):
    expires_at = (expires or NOW + timedelta(hours=1)).timestamp()
    conn.execute(
        "INSERT INTO model_leases (lease_id, seat, owner_node, holder_token, acquired_at, expires_at, released_at, "
        "provider, model, launch_model, account_id, pool, decision_id, session_id, consumer, context_hash, "
        "request_id, policy_digest, policy_version) "
        "VALUES (?, ?, ?, 'holder', ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 'brigade-run', ?, ?, NULL, NULL)",
        (
            lease_id,
            SEAT,
            node_id,
            fleet_hub._epoch_to_iso(NOW.timestamp()),
            expires_at,
            None if not released else NOW.isoformat(),
            provider,
            model,
            decision_id,
            session_id,
            context_hash,
            lease_id,
        ),
    )
    conn.commit()


def test_legacy_unlinked_lease_occupies_seat(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_lease(conn, lease_id="lease-old")
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["seat"] == 1
    assert "legacy-unlinked" in usage["reasons"]


def test_t3_reservation_occupies_seat_against_new_lease(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_reservation(
        conn, reservation_id="res-1", decision_id="dec-1", session_id="sess-1", node_id=NODE_A, accepted=1
    )
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["seat"] == 1
    usage = fleet_hub_capacity.capacity_usage(
        conn,
        document=document,
        seat=SEAT,
        now=NOW,
        exclude_execution={"decision_id": "dec-1", "session_id": "sess-1", "node_id": NODE_A},
    )
    assert usage["used"]["seat"] == 0


def test_linked_same_execution_counted_once(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_reservation(
        conn, reservation_id="res-1", decision_id="dec-1", session_id="sess-1", node_id=NODE_A, accepted=1
    )
    _insert_lease(
        conn,
        lease_id="lease-1",
        decision_id="dec-1",
        session_id="sess-1",
        node_id=NODE_A,
        context_hash="sha256:" + ("ab" * 32),
    )
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["seat"] == 1
    assert "legacy-unlinked" not in usage["reasons"]


def test_partial_linkage_without_proof_hash_is_not_deduplicated(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_reservation(
        conn, reservation_id="res-1", decision_id="dec-1", session_id="sess-1", node_id=NODE_A, accepted=1
    )
    _insert_lease(conn, lease_id="lease-1", decision_id="dec-1", session_id="sess-1", node_id=NODE_A)
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["seat"] == 2
    assert "legacy-unlinked" in usage["reasons"]


def test_wrong_execution_cannot_exclude_another_job(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_reservation(
        conn, reservation_id="res-1", decision_id="dec-1", session_id="sess-1", node_id=NODE_A, accepted=1
    )
    with pytest.raises(FleetHubError):
        fleet_hub_capacity.capacity_usage(
            conn,
            document=document,
            seat=SEAT,
            now=NOW,
            exclude_execution={"decision_id": "dec-2", "session_id": "sess-1", "node_id": NODE_A},
        )
    with pytest.raises(FleetHubError):
        fleet_hub_capacity.capacity_usage(
            conn,
            document=document,
            seat=SEAT,
            now=NOW,
            exclude_execution={"decision_id": "dec-1", "session_id": "sess-1", "node_id": NODE_B},
        )


def test_released_and_expired_unaccepted_do_not_occupy(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_reservation(
        conn,
        reservation_id="res-released",
        decision_id="dec-r",
        session_id="sess-r",
        node_id=NODE_A,
        released=True,
    )
    _insert_reservation(
        conn,
        reservation_id="res-expired",
        decision_id="dec-e",
        session_id="sess-e",
        node_id=NODE_A,
        accepted=0,
        expires=NOW - timedelta(minutes=1),
    )
    _insert_lease(conn, lease_id="lease-released", released=True)
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["seat"] == 0


def test_accepted_expired_reservation_still_occupies(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_reservation(
        conn,
        reservation_id="res-lost",
        decision_id="dec-l",
        session_id="sess-l",
        node_id=NODE_A,
        accepted=1,
        expires=NOW - timedelta(minutes=1),
    )
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["seat"] == 1


def test_changed_roster_identity_does_not_erase_old_lease(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    _insert_lease(conn, lease_id="lease-old", provider="old-provider", model="old-model")
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["seat"] == 1
    assert usage["used"]["provider"] == 1
    assert "legacy-identity-drift" in usage["reasons"]


def test_unconfigured_account_and_pool_are_null(conn):
    document = fleet_hub_policy.current_policy(conn)["document"]
    usage = fleet_hub_capacity.capacity_usage(conn, document=document, seat=SEAT, now=NOW)
    assert usage["used"]["account"] is None
    assert usage["used"]["pool"] is None


def test_concurrent_connections_serialize_capacity_write(tmp_path):
    db = tmp_path / "fleet.db"
    first = fleet_hub.init_db(db)
    fleet_hub_policy.save_policy(first, _document(), expected_version=1, actor="operator", reason="seed")
    document = fleet_hub_policy.current_policy(first)["document"]
    _insert_lease(first, lease_id="lease-1")
    first.execute("BEGIN IMMEDIATE")
    usage_first = fleet_hub_capacity.capacity_usage(first, document=document, seat=SEAT, now=NOW)
    assert usage_first["used"]["seat"] == 1

    observed: dict[str, object] = {}
    entered = threading.Event()

    def contender() -> None:
        second = sqlite3.connect(str(db), timeout=10)
        second.row_factory = sqlite3.Row
        try:
            entered.set()
            second.execute("BEGIN IMMEDIATE")
            usage = fleet_hub_capacity.capacity_usage(second, document=document, seat=SEAT, now=NOW)
            observed["seat"] = usage["used"]["seat"]
            second.commit()
        except Exception as exc:  # pragma: no cover - surfaced through the assertion below
            observed["error"] = exc
        finally:
            second.close()

    worker = threading.Thread(target=contender)
    worker.start()
    assert entered.wait(5)
    worker.join(0.5)
    # The second connection must block behind the first BEGIN IMMEDIATE.
    assert worker.is_alive()
    assert "seat" not in observed
    # Writing under the held lock, then releasing it, is what the contender sees.
    _insert_lease(first, lease_id="lease-2")
    worker.join(10)
    assert not worker.is_alive()
    assert "error" not in observed, observed.get("error")
    assert observed["seat"] == 2
    first.close()
