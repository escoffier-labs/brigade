"""Durable legacy lifecycle fencing, exercised against real SQLite authority."""

from __future__ import annotations

import sqlite3
import json
import threading
from http.client import HTTPConnection

import pytest

from brigade import fleet_hub as hub


@pytest.fixture()
def authority(tmp_path, monkeypatch):
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(hub, "_now_epoch", lambda: clock["now"])
    path = tmp_path / "hub.db"
    conn = hub.init_db(path)
    yield conn, path, clock
    conn.close()


def request(conn, action="acquire", target="fake-target", node="fake-node", holder="fake-holder", **extra):
    return hub.handle_claim(conn, {"action": action, "target": target, "node_id": node, "holder": holder, **extra})


def fence(conn, target="fake-target"):
    return conn.execute("SELECT generation FROM claim_fences WHERE target = ?", (target,)).fetchone()[0]


def snapshot(conn):
    return (
        conn.execute("SELECT * FROM claims ORDER BY target").fetchall(),
        conn.execute("SELECT * FROM claim_fences ORDER BY target").fetchall(),
    )


@pytest.fixture()
def http_authority(authority):
    conn, path, clock = authority
    server = hub.make_server("127.0.0.1", 0, path, "fake-admin-token", allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield conn, clock, server.server_address
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def http_claim(address, method, target="fake-target"):
    client = HTTPConnection(*address, timeout=5)
    try:
        body = json.dumps({"action": "acquire", "target": target, "node_id": "fake-node", "holder": "fake-holder"})
        client.request(
            method,
            "/claims",
            body=body if method == "POST" else None,
            headers={"Authorization": "Bearer fake-admin-token", "Content-Type": "application/json"},
        )
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("damage", ["missing", "negative", "text", "real"])
def test_http_fence_refusal_is_structured_503_without_mutation(http_authority, method, damage):
    conn, _, address = http_authority
    request(conn)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    if damage == "missing":
        conn.execute("DELETE FROM claim_fences")
    else:
        conn.execute(
            "UPDATE claim_fences SET generation = ?", ({"negative": -1, "text": "broken", "real": 1.5}[damage],)
        )
    conn.commit()
    before = snapshot(conn)
    assert http_claim(address, method) == (503, {"error": "claim generation fence is missing or corrupt"})
    assert snapshot(conn) == before


@pytest.mark.parametrize("refusal", ["capacity", "exhausted"])
def test_http_storage_limits_are_503_and_roll_back_pruning(http_authority, monkeypatch, refusal):
    from brigade import fleet_hub_claims

    conn, clock, address = http_authority
    request(conn, ttl_seconds=1)
    if refusal == "capacity":
        monkeypatch.setattr(fleet_hub_claims, "MAX_CLAIM_TARGETS", 1)
    else:
        conn.execute("UPDATE claim_fences SET generation = ?", (2**63 - 1,))
        conn.commit()
    clock["now"] += 2
    before = snapshot(conn)
    error = "claim generation fence capacity exceeded" if refusal == "capacity" else "claim generation fence exhausted"
    assert http_claim(address, "POST", target="fake-new") == (503, {"error": error})
    assert snapshot(conn) == before


def test_schema25_migration_preserves_live_rows_and_seeds_once(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(hub._CLAIMS_SCHEMA)
    conn.execute(
        "INSERT INTO claims (target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at, lock_token) "
        "VALUES ('fake-target', 'fake-node', 'fake-holder', 'original', 'renewed', 900, 9999999999, 'fake-lock')"
    )
    original = conn.execute("SELECT * FROM claims").fetchall()
    conn.execute("PRAGMA user_version=25")
    conn.commit()
    conn.close()
    with hub.init_db(path) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 26
        assert migrated.execute("SELECT * FROM claims").fetchall() == original
        assert fence(migrated) == 1
        claim = hub.list_claims(migrated)[0]
        assert claim["generation"] == 1
        assert claim["protocol"] == "legacy"
        assert "fake-holder" not in str(claim) and "fake-lock" not in str(claim)
        request(migrated, "release")
        assert fence(migrated) == 2
    with hub.init_db(path) as reopened:
        assert fence(reopened) == 2


def test_live_retry_renew_release_missing_release_and_reacquire(authority):
    conn, _, clock = authority
    _, first = request(conn, ttl_seconds=10)
    assert first["claim"]["generation"] == 1
    clock["now"] += 1
    _, retry = request(conn, ttl_seconds=20)
    assert retry["claim"]["generation"] == 1
    assert retry["claim"]["acquired_at"] == first["claim"]["acquired_at"]
    assert request(conn, "renew")[1]["claim"]["generation"] == 1
    assert request(conn, "release") == (200, {"released": True})
    assert request(conn, "inspect")[1]["generation"] == 2
    assert request(conn, "release") == (200, {"released": False})
    assert fence(conn) == 3
    assert request(conn)[1]["claim"]["generation"] == 4
    assert hub.list_claims(conn)[0]["generation"] == 4


def test_global_expiry_retirement_is_once_and_survives_free_restart(authority):
    conn, path, clock = authority
    assert request(conn, ttl_seconds=1)[1]["claim"]["generation"] == 1
    request(conn, target="fake-other", ttl_seconds=1)
    clock["now"] += 1
    # Legacy inspect hides expiry but does not retire it.
    assert request(conn, "inspect")[1]["claim"] is None
    assert fence(conn) == 1
    request(conn, target="fake-trigger")
    assert fence(conn) == fence(conn, "fake-other") == 2
    request(conn, target="fake-trigger")
    assert fence(conn) == 2
    with hub.init_db(path) as reopened:
        assert request(reopened)[1]["claim"]["generation"] == 3
        assert fence(reopened, "fake-other") == 2


def test_exact_supersede_advances_but_live_retry_does_not(authority):
    conn, _, _ = authority
    lock = {"token": "fake-lock", "acquired_at": "2025-01-01", "run_dir": "fake-run"}
    assert request(conn, lock=lock)[1]["claim"]["generation"] == 1
    status, receipt = request(conn, holder="fake-next", scope="node", lock={"token": "fake-next-lock"}, supersede=lock)
    assert status == 200
    assert receipt["claim"]["generation"] == 2
    assert receipt["superseded"]["generation"] == 1
    assert request(conn, holder="fake-next")[1]["claim"]["generation"] == 2


@pytest.mark.parametrize(
    "action,extra",
    [
        ("acquire", {}),
        ("renew", {}),
        ("release", {}),
        ("release", {"scope": "node", "acquired_at": "stale"}),
        ("release", {"scope": "force", "acquired_at": "stale"}),
    ],
)
def test_refused_operation_does_not_change_row_or_fence(authority, action, extra):
    conn, _, _ = authority
    assert request(conn)[1]["claim"]["generation"] == 1
    before = snapshot(conn)
    status, payload = request(conn, action, holder="fake-wrong", **extra)
    assert status == 409
    assert payload["owner"]["generation"] == 1
    assert snapshot(conn) == before


@pytest.mark.parametrize("scope", ["holder", "node", "force"])
def test_release_scopes_advance_exactly_once_even_for_expired_row(authority, scope):
    conn, _, clock = authority
    _, first = request(conn, ttl_seconds=1)
    assert first["claim"]["generation"] == 1
    clock["now"] += 2
    extra = {"scope": scope}
    if scope == "node":
        extra["acquired_at"] = first["claim"]["acquired_at"]
    assert request(conn, "release", **extra)[1]["released"]
    assert fence(conn) == 2
    request(conn, target="fake-trigger")
    assert fence(conn) == 2


def test_capacity_refuses_new_target_and_rolls_back_global_pruning(authority, monkeypatch):
    conn, _, clock = authority
    assert request(conn, ttl_seconds=1)[1]["claim"]["generation"] == 1
    from brigade import fleet_hub_claims

    monkeypatch.setattr(fleet_hub_claims, "MAX_CLAIM_TARGETS", 1)
    before = snapshot(conn)
    clock["now"] += 2
    with pytest.raises(hub.FleetHubError, match="capacity"):
        request(conn, target="fake-new")
    assert snapshot(conn) == before
    assert not conn.in_transaction
    # Existing target remains usable at the cap, with retirement + acquisition.
    assert request(conn)[1]["claim"]["generation"] == 3
    request(conn, "release")
    assert fence(conn) == 4
    with pytest.raises(hub.FleetHubError, match="capacity"):
        request(conn, "release", target="fake-new")
    assert fence(conn) == 4


@pytest.mark.parametrize("action", ["acquire", "release"])
def test_generation_exhaustion_rolls_back_retirement_and_ownership(authority, action):
    conn, _, clock = authority
    assert request(conn, ttl_seconds=1)[1]["claim"]["generation"] == 1
    conn.execute("UPDATE claim_fences SET generation = ?", (2**63 - 1,))
    conn.commit()
    clock["now"] += 2
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubError, match="exhaust"):
        request(conn, action)
    assert snapshot(conn) == before
    assert not conn.in_transaction


@pytest.mark.parametrize("damage", ["missing", "negative", "text", "real"])
def test_existing_claim_with_missing_or_corrupt_fence_refuses(authority, damage):
    conn, path, _ = authority
    assert request(conn)[1]["claim"]["generation"] == 1
    conn.execute("PRAGMA ignore_check_constraints=ON")
    if damage == "missing":
        conn.execute("DELETE FROM claim_fences")
    else:
        value = {"negative": -1, "text": "broken", "real": 1.5}[damage]
        conn.execute("UPDATE claim_fences SET generation = ?", (value,))
    conn.commit()
    before = snapshot(conn)
    for action in ("acquire", "renew", "release", "inspect"):
        with pytest.raises(hub.FleetHubError, match="fence"):
            request(conn, action)
        assert snapshot(conn) == before
        assert not conn.in_transaction
    with pytest.raises(hub.FleetHubError, match="fence"):
        hub.list_claims(conn)
    with pytest.raises(hub.FleetHubError, match="fence"):
        hub.init_db(path)
    assert snapshot(conn) == before


def test_current_schema_never_rebuilds_deleted_fence_table(authority):
    conn, path, _ = authority
    assert request(conn)[1]["claim"]["generation"] == 1
    conn.execute("DROP TABLE claim_fences")
    conn.commit()
    with pytest.raises(hub.FleetHubError, match="fence"):
        hub.init_db(path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'claim_fences'").fetchone() is None


def test_migration_capacity_failure_preserves_schema25(tmp_path, monkeypatch):
    from brigade import fleet_hub_claims

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(hub._CLAIMS_SCHEMA)
    for target in ("fake-one", "fake-two"):
        conn.execute(
            "INSERT INTO claims (target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
            "VALUES (?, 'fake-node', 'fake-holder', 'original', 'renewed', 900, 9999999999)",
            (target,),
        )
    conn.execute("PRAGMA user_version=25")
    conn.commit()
    before = conn.execute("SELECT * FROM claims").fetchall()
    monkeypatch.setattr(fleet_hub_claims, "MAX_CLAIM_TARGETS", 1)
    with pytest.raises(hub.FleetHubError, match="capacity"):
        hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
    assert conn.execute("SELECT * FROM claims").fetchall() == before
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'claim_fences'").fetchone() is None
    conn.close()


@pytest.mark.parametrize(
    "action,wrong", [("acquire", False), ("renew", False), ("acquire", True), ("renew", True), ("release", True)]
)
def test_response_uses_decision_snapshot_before_commit(authority, action, wrong):
    conn, path, _ = authority
    request(conn)

    class CommitInterleaving(sqlite3.Connection):
        """A second real SQLite writer runs immediately after the first commit."""

        def commit(self):
            super().commit()
            with hub.open_db(path) as other:
                request(other, "release", scope="force")
                request(other, node="fake-later-node", holder="fake-later-holder")

    racing = sqlite3.connect(path, factory=CommitInterleaving)
    try:
        status, payload = request(racing, action, holder="fake-wrong" if wrong else "fake-holder")
        assert status == (409 if wrong else 200)
        decision = payload["owner" if wrong else "claim"]
        assert decision["owner_node"] == "fake-node"
        assert decision["generation"] == 1
        assert hub.list_claims(conn)[0]["owner_node"] == "fake-later-node"
    finally:
        racing.close()


def test_newer_schema_refuses_without_changes(authority):
    conn, path, _ = authority
    request(conn)
    conn.execute("PRAGMA user_version=27")
    conn.commit()
    with pytest.raises(hub.FleetHubError, match="schema version 27"):
        hub.init_db(path)
    with pytest.raises(hub.FleetHubError, match="schema version 27"):
        hub.open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 27
