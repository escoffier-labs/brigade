"""Read-only Hub seat usage: current occupancy without acquiring or writing."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from brigade import fleet_hub, fleet_hub_model_roster, fleet_hub_policy, fleet_policy

ADMIN_TOKEN = "test-admin-token"
NODE_A = "11111111-1111-4111-8111-111111111111"
SEAT = "seat-alpha"
NOW = datetime.now(timezone.utc)


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
                "bindings": {"brigade": {"cli": "cli-alpha"}, "t3_fleet": {"instance_id": "inst-alpha"}, "native": {}},
            }
        },
        "consumers": {"brigade-run": {"reload": "refreshable", "coverage": "unverified"}},
        "repositories": {},
    }


def _insert_reservation(conn, *, reservation_id, accepted=1, released=False, expires=None):
    stamp = (expires or NOW + timedelta(minutes=5)).isoformat()
    conn.execute(
        "INSERT INTO fleet_reservations (reservation_id, decision_id, session_id, machine, seat, node_id, "
        "origin_node, state, accepted, expires_at, created_at, updated_at) "
        "VALUES (?, ?, 'sess-1', 'worker-linux-1', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reservation_id,
            reservation_id,
            SEAT,
            NODE_A,
            NODE_A,
            "released" if released else "active",
            int(accepted),
            stamp,
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    conn.commit()


def _insert_lease(conn, *, lease_id, released=False, expires=None):
    conn.execute(
        "INSERT INTO model_leases (lease_id, seat, owner_node, holder_token, acquired_at, expires_at, "
        "released_at, provider, model, request_id) "
        "VALUES (?, ?, ?, 'holder', ?, ?, ?, 'provider-a', 'model-a-1', ?)",
        (
            lease_id,
            SEAT,
            NODE_A,
            fleet_hub._epoch_to_iso(NOW.timestamp()),
            (expires or NOW + timedelta(hours=1)).timestamp(),
            NOW.isoformat() if released else None,
            lease_id,
        ),
    )
    conn.commit()


def _leases(conn) -> list:
    return conn.execute("SELECT * FROM model_leases ORDER BY lease_id").fetchall()


def _reservations(conn) -> list:
    return conn.execute("SELECT * FROM fleet_reservations ORDER BY reservation_id").fetchall()


@contextmanager
def _hub(tmp_path):
    server = fleet_hub.make_server(
        "127.0.0.1", 0, tmp_path / "fleet.db", ADMIN_TOKEN
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_address[1], tmp_path / "fleet.db"  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, path: str, *, token: str | None = None):
    host, port, _db = hub
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    result = response.status, json.loads(response.read().decode("utf-8"))
    connection.close()
    return result


def test_usage_route_refuses_unauthenticated_and_unknown_callers(tmp_path):
    with _hub(tmp_path) as hub:
        assert _request(hub, "/models/usage") == (401, {"error": "unauthorized"})
        assert _request(hub, "/models/usage", token="unknown-token") == (401, {"error": "unauthorized"})
        db = fleet_hub.open_db(hub[2])
        try:
            _node, node_token = fleet_hub.add_node(db, NODE_A, "node-a")
        finally:
            db.close()
        status, payload = _request(hub, "/models/usage", token=node_token)
        assert status == 200
        assert payload["schema"] == "brigade.fleet_hub_seat_usage.v1"
        assert payload["seats"] == {}


def test_usage_route_reports_occupancy_without_writing(tmp_path):
    from brigade import fleet_policy_migration

    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            db.execute(
                "INSERT INTO model_policy (seat, provider, model, reasoning, enabled, limit_count, updated_at) "
                "VALUES (?, 'provider-a', 'model-a-1', 'high', 1, 1, 'fixture')",
                (SEAT,),
            )
            db.commit()
            fleet_hub_policy.save_policy(db, _document(), expected_version=1, actor="operator", reason="seed")
            preview = fleet_policy_migration.preview_migration(
                db,
                expected_policy_version=fleet_hub_policy.current_policy(db)["revision"],
                expected_roster_revision=int(
                    db.execute("SELECT revision FROM model_roster_meta WHERE singleton=1").fetchone()[0]
                ),
                annotations={"seats": {SEAT: {"pinned": False, "training_allowed": False}}},
            )
            assert preview["ok"] is True, preview
            fleet_policy_migration.activate_migration(
                db,
                expected_policy_version=preview["sources"]["policy_revision"],
                expected_roster_revision=preview["sources"]["roster_revision"],
                preview_digest=preview["preview_digest"],
                actor="operator",
                reason="adopt",
                annotations=preview.get("annotations"),
            )
            current = fleet_hub_policy.current_policy(db)
            _insert_reservation(db, reservation_id="res-active")
            _insert_lease(db, lease_id="lease-active")
            _insert_lease(db, lease_id="lease-released", released=True)
            _insert_reservation(db, reservation_id="res-expired", accepted=0, expires=NOW - timedelta(minutes=1))
            leases_before = _leases(db)
            reservations_before = _reservations(db)
        finally:
            db.close()

        status, payload = _request(hub, "/models/usage", token=ADMIN_TOKEN)
        assert status == 200
        assert payload["roster_revision"] == current["revision"]
        assert payload["policy_digest"] == current["digest"]
        assert payload["seats"][SEAT]["occupied"] == 2

        db = fleet_hub.open_db(hub[2])
        try:
            assert _leases(db) == leases_before
            assert _reservations(db) == reservations_before
        finally:
            db.close()


def test_legacy_usage_counts_only_unexpired_unreleased_leases(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "legacy.db")
    try:
        conn.execute(
            "INSERT INTO model_policy (seat, provider, model, reasoning, enabled, limit_count, updated_at) "
            "VALUES (?, 'legacy-provider', 'legacy-model', 'none', 1, 4, 'fixture')",
            (SEAT,),
        )
        _insert_lease(conn, lease_id="lease-live")
        _insert_lease(conn, lease_id="lease-expired", expires=NOW - timedelta(minutes=1))
        _insert_lease(conn, lease_id="lease-released", released=True)
        usage = fleet_hub_model_roster.seat_usage(conn, now=NOW)
        assert usage["roster_revision"] == fleet_hub_model_roster.raw_revision(conn)
        assert usage["policy_digest"] is None
        assert usage["seats"][SEAT]["occupied"] == 1
    finally:
        conn.close()
