"""Durable legacy lifecycle fencing, exercised against real SQLite authority."""

from __future__ import annotations

import sqlite3
import json
import threading
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection

import pytest

from brigade import fleet_client, fleet_hub as hub


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
        conn.execute("SELECT * FROM claim_retired_holders ORDER BY target, owner_node, holder_token").fetchall(),
    )


def test_retirement_horizon_boundary_preserves_target_fences(authority):
    from brigade import fleet_hub_claims

    conn, _, clock = authority
    request(conn, "release")
    original = fence(conn)
    horizon = getattr(fleet_hub_claims, "RETIRED_HOLDER_HORIZON_SECONDS", 604800)
    clock["now"] += horizon
    assert request(conn)[1]["reason"] == "stale-holder"
    clock["now"] += 1
    # Expired history is no longer authority, even before a background sweep.
    assert request(conn)[1]["granted"] is True
    assert fence(conn) == original + 1
    assert conn.execute("SELECT COUNT(*) FROM claim_retired_holders").fetchone()[0] == 0


def test_expired_history_makes_room_before_capacity_refusal(authority, monkeypatch):
    from brigade import fleet_hub_claims

    conn, _, clock = authority
    request(conn, "release")
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS", 1)
    clock["now"] += getattr(fleet_hub_claims, "RETIRED_HOLDER_HORIZON_SECONDS", 604800) + 1
    assert request(conn, "release", holder="fake-next")[0] == 200
    assert fence(conn) == 2
    assert conn.execute("SELECT holder_token FROM claim_retired_holders").fetchall() == [("fake-next",)]


def test_per_node_retirement_quota_isolates_abusive_free_target_releases(authority, monkeypatch):
    from brigade import fleet_hub_claims

    conn, _, _ = authority
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS_PER_NODE", 1, raising=False)
    request(conn, "release", node="fake-abuser")
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubError, match="quota"):
        request(conn, "release", node="fake-abuser", holder="fake-next")
    assert snapshot(conn) == before
    assert request(conn, node="fake-neighbor", holder="fake-fresh")[0] == 200
    assert request(conn, "release", node="fake-neighbor", holder="fake-fresh")[0] == 200


def test_prune_leaves_unretirable_unrelated_target_in_place_for_live_retry(authority, monkeypatch):
    from brigade import fleet_hub_claims

    conn, _, clock = authority
    request(conn, ttl_seconds=1)
    request(conn, "release", target="fake-other-history")
    request(conn, target="fake-neighbor", node="fake-neighbor")
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS", 1)
    clock["now"] += 1
    assert request(conn, target="fake-neighbor", node="fake-neighbor")[0] == 200
    assert conn.execute("SELECT 1 FROM claims WHERE target='fake-target'").fetchone()
    assert fence(conn) == 1
    with pytest.raises(hub.FleetHubError, match="capacity"):
        request(conn, holder="fake-new-holder")


@pytest.mark.parametrize("extra", [{}, {"generation": 0}], ids=["legacy", "generation"])
def test_refused_cleanup_also_refuses_late_acquire(authority, monkeypatch, extra):
    from brigade import fleet_hub_claims

    conn, _, _ = authority
    request(conn, "release", target="fake-budget")
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS", 1)
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubError, match="capacity"):
        request(conn, "release")
    with pytest.raises(hub.FleetHubError, match="capacity"):
        request(conn, **extra)
    assert snapshot(conn) == before


def test_schema33_migration_preserves_retirements_and_starts_conservative_horizon(authority):
    conn, path, clock = authority
    request(conn)
    live = snapshot(conn)[:2]
    conn.execute("DROP TABLE claim_retired_holders")
    conn.execute("""CREATE TABLE claim_retired_holders (
        target TEXT NOT NULL, owner_node TEXT NOT NULL, holder_token TEXT NOT NULL,
        generation INTEGER NOT NULL
            CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807),
        PRIMARY KEY (target, owner_node, holder_token)
        )""")
    conn.execute("INSERT INTO claim_retired_holders VALUES ('fake-target','fake-node','fake-old',1)")
    conn.execute("PRAGMA user_version=33")
    conn.commit()
    with hub.init_db(path) as migrated:
        assert snapshot(migrated)[:2] == live
        assert migrated.execute("SELECT retired_at FROM claim_retired_holders").fetchone() == (clock["now"],)
        assert request(migrated, holder="fake-old")[1]["reason"] == "stale-holder"
        assert request(migrated, "renew")[0] == 200


def test_unmigrated_schema32_claim_export_does_not_require_retired_table(authority):
    from datetime import datetime, timezone
    from brigade import fleet_export, fleet_hub_claims

    conn, _, clock = authority
    request(conn)
    conn.execute("DROP TABLE claim_retired_holders")
    conn.execute("PRAGMA user_version=32")
    conn.commit()
    fleet_hub_claims.validate_schema(conn)
    records = list(fleet_export.iter_claim_records(conn, now=datetime.fromtimestamp(clock["now"], timezone.utc)))
    assert len(records) == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 32
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='claim_retired_holders'").fetchone()


@pytest.mark.parametrize("scope", ["node", "force"])
def test_ingress_snapshot_missing_schema_is_fence_refusal(authority, scope):
    from brigade.fleet_hub_claims import ClaimFenceError

    conn, _, _ = authority
    conn.execute("DROP TABLE claim_fences")
    conn.commit()
    with pytest.raises(ClaimFenceError, match="fence storage"):
        request(conn, "release", scope=scope, acquired_at="2023-11-14T22:13:20Z")


def test_missing_row_holder_release_is_idempotent_stale_payload(authority):
    conn, _, _ = authority
    request(conn, "release")
    before = snapshot(conn)
    assert request(conn, "release") == (200, {"released": False, "reason": "stale-holder"})
    assert snapshot(conn) == before


def test_status_exposes_retired_history_budget(http_authority):
    from brigade import fleet_hub_claims

    conn, _, address = http_authority
    request(conn, "release")
    client = HTTPConnection(*address, timeout=5)
    try:
        client.request("GET", "/status", headers={"Authorization": "Bearer fake-admin-token"})
        response = client.getresponse()
        assert response.status == 200
        budget = json.loads(response.read())["claim_fencing"]
        assert budget["retired_holder_rows"] == 1
        assert budget["retired_holder_horizon_seconds"] == fleet_hub_claims.RETIRED_HOLDER_HORIZON_SECONDS
    finally:
        client.close()


@pytest.mark.parametrize("damage", ["orphaned", "invalid-generation", "missing-table"])
def test_status_refuses_corrupt_retirement_storage(http_authority, damage):
    conn, _, address = http_authority
    request(conn, "release")
    if damage == "orphaned":
        conn.execute("DELETE FROM claim_fences")
    elif damage == "invalid-generation":
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE claim_retired_holders SET generation = -1")
    else:
        conn.execute("DROP TABLE claim_retired_holders")
    conn.commit()
    client = HTTPConnection(*address, timeout=5)
    try:
        client.request("GET", "/status", headers={"Authorization": "Bearer fake-admin-token"})
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 503
        assert payload == {
            "error": (
                "claim retired-holder fence storage is missing or corrupt"
                if damage == "missing-table"
                else "claim retired-holder fence is missing or corrupt"
            ),
            "reason": "claim-fence-refused",
            "owner": {},
        }
    finally:
        client.close()


@pytest.mark.parametrize("damage", ["invalid-generation", "live-claim-without-fence"])
def test_status_refuses_corrupt_generation_fences(http_authority, damage):
    conn, _, address = http_authority
    if damage == "invalid-generation":
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("INSERT INTO claim_fences (target, generation) VALUES ('fake-orphan-target', 0)")
    else:
        request(conn, "acquire")
        conn.execute("DELETE FROM claim_fences WHERE target = 'fake-target'")
    conn.commit()
    client = HTTPConnection(*address, timeout=5)
    try:
        client.request("GET", "/status", headers={"Authorization": "Bearer fake-admin-token"})
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 503
        assert payload == {
            "error": "claim generation fence is missing or corrupt",
            "reason": "claim-fence-refused",
            "owner": {},
        }
    finally:
        client.close()


def test_fence_refusal_never_admits_local_only_work(http_authority, monkeypatch):
    conn, _, address = http_authority
    conn.execute("DROP TABLE claim_fences")
    conn.commit()
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": f"http://{address[0]}:{address[1]}", "token": "fake-admin-token"},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "fake-node")
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", lambda *a, **kw: pytest.fail("fallback cleanup"))
    with pytest.raises(fleet_client.FleetClaimHeldError, match="claim-fence-refused"):
        with fleet_client.repo_claim("fake-target"):
            pytest.fail("fence refusal admitted local-only work")


def test_missing_holder_release_fences_a_late_legacy_acquire(authority):
    conn, path, _ = authority
    assert request(conn, "release") == (200, {"released": False, "reason": "stale-holder"})
    # The abandoned POST can first reach the hub after cleanup and restart.
    with hub.init_db(path) as reopened:
        status, result = request(reopened)
        assert status == 409
        assert result["granted"] is False
        assert hub.list_claims(reopened, include_all=True) == []
        # A different holder remains a valid legacy client.
        assert request(reopened, holder="fake-fresh-holder")[0] == 200


@pytest.mark.parametrize("action", ["acquire", "renew", "release"])
def test_stale_generation_cannot_act_on_same_holder_new_incarnation(authority, action):
    conn, _, _ = authority
    _, first = request(conn)
    old_generation = first["claim"]["generation"]
    assert request(conn, "release")[0] == 200
    _, current = request(conn, generation=fence(conn))
    assert current["claim"]["generation"] > old_generation
    before = snapshot(conn)
    status, _ = request(conn, action, generation=old_generation)
    assert status == 409
    assert snapshot(conn) == before
    assert request(conn, action, generation=current["claim"]["generation"])[0] == 200


def test_stale_force_release_cannot_delete_new_generation(authority):
    conn, _, _ = authority
    _, first = request(conn)
    old_generation = first["claim"]["generation"]
    assert request(conn, "release")[0] == 200
    assert request(conn, holder="fake-next-holder")[0] == 200
    before = snapshot(conn)
    status, _ = request(conn, "release", scope="force", generation=old_generation)
    assert status == 409
    assert snapshot(conn) == before


@pytest.mark.parametrize("scope", ["node", "force"])
def test_generationless_operator_release_is_bound_before_waiting_for_write_lock(authority, scope):
    conn, path, _ = authority
    _, original = request(conn)

    class ReplacementBeforeBegin:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                with hub.init_db(path) as other:
                    request(other, "release")
                    request(other, holder="fake-next-holder")
            return conn.execute(sql, *args)

    status, result = request(
        ReplacementBeforeBegin(), "release", scope=scope, acquired_at=original["claim"]["acquired_at"]
    )
    assert status == 409 and result["reason"] == "stale-generation"
    assert hub.list_claims(conn)[0]["generation"] == 3


@pytest.mark.parametrize("ending", ["release", "expire", "supersede", "force"])
def test_retirement_survives_restart_and_refuses_only_old_holder(authority, ending):
    conn, path, clock = authority
    request(conn, ttl_seconds=1, lock={"token": "fake-lock"})
    if ending == "expire":
        clock["now"] += 1
        request(conn, target="fake-prune")
    elif ending == "supersede":
        request(conn, holder="fake-replacement", scope="node", supersede={"token": "fake-lock"})
    else:
        request(conn, "release", **({"scope": "force", "node": "fake-operator"} if ending == "force" else {}))
    with hub.init_db(path) as reopened:
        for action in ("acquire", "renew", "release"):
            status, response = request(reopened, action)
            assert status == (200 if action == "release" and ending != "supersede" else 409)
            assert response["reason"] == "stale-holder"
        if ending == "supersede":
            assert request(reopened, "renew", holder="fake-replacement")[1]["renewed"] is True
        else:
            assert request(reopened, holder="fake-fresh")[1]["granted"] is True
            assert request(reopened, "renew", holder="fake-fresh")[1]["renewed"] is True
            assert request(reopened, "release", holder="fake-fresh")[1]["released"] is True


def test_retired_holder_capacity_rolls_back_every_transition(authority, monkeypatch):
    from brigade import fleet_hub_claims

    conn, _, clock = authority
    request(conn, "release", holder="fake-first")
    request(conn, ttl_seconds=1)
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS", 1)
    before = snapshot(conn)
    for scope in ("holder", "node", "force"):
        with pytest.raises(hub.FleetHubError, match="retired-holder fence capacity exceeded"):
            request(
                conn,
                "release",
                scope=scope,
                **({"acquired_at": hub.list_claims(conn)[0]["acquired_at"]} if scope == "node" else {}),
            )
        assert snapshot(conn) == before
    clock["now"] += 1
    with pytest.raises(hub.FleetHubError, match="retired-holder fence capacity exceeded"):
        request(conn, target="fake-new")
    assert snapshot(conn) == before
    # Existing retired markers still refuse stale requests without needing space.
    assert request(conn, "release", holder="fake-first")[1]["reason"] == "stale-holder"
    assert snapshot(conn) == before


def test_schema32_migration_preserves_live_claim_and_fences(authority):
    conn, path, _ = authority
    request(conn)
    before = snapshot(conn)[:2]
    conn.execute("DROP TABLE claim_retired_holders")
    conn.execute("PRAGMA user_version=32")
    conn.commit()
    with hub.init_db(path) as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == hub.SCHEMA_VERSION
        assert snapshot(migrated)[:2] == before
        assert request(migrated, "renew")[0] == 200
        assert request(migrated, "release")[0] == 200
    with hub.init_db(path) as reopened:
        assert request(reopened)[1]["reason"] == "stale-holder"


@pytest.mark.parametrize("damage", ["drop", "invalid", "future", "orphan", "trigger"])
def test_corrupt_retired_holder_state_refuses_without_repair(authority, damage):
    conn, path, _ = authority
    request(conn, "release")
    if damage == "drop":
        conn.execute("DROP TABLE claim_retired_holders")
    elif damage == "trigger":
        conn.execute("CREATE TRIGGER fake_retired AFTER DELETE ON claim_retired_holders BEGIN SELECT 1; END")
    else:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        if damage == "orphan":
            conn.execute("UPDATE claim_retired_holders SET target = 'fake-orphan'")
        else:
            conn.execute("UPDATE claim_retired_holders SET generation = ?", (0 if damage == "invalid" else 100,))
    conn.commit()
    before = conn.iterdump()
    original = list(before)
    with pytest.raises(hub.FleetHubError, match="fence"):
        hub.init_db(path)
    assert list(conn.iterdump()) == original


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "1", 2**63])
def test_optional_generation_validation_is_strict(authority, value):
    conn, _, _ = authority
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubError, match="generation"):
        request(conn, generation=value)
    assert snapshot(conn) == before


def test_new_client_with_old_hub_ignores_additive_generation(monkeypatch):
    # A legacy endpoint fixture ignores unknown fields and returns no fence
    # metadata. The contract here is client serialization and response handling.
    posted = []
    live = None

    def old_endpoint(url, token, body, *, timeout):
        nonlocal live
        posted.append(body)
        if body["action"] == "acquire":
            live = {"target": body["target"], "owner_node": body["node_id"]}
            return 200, {"granted": True, "claim": live}
        if body["action"] == "inspect":
            return 200, {"inspected": True, "claim": live}
        if body["action"] == "renew":
            return 200, {"renewed": live is not None, "claim": live}
        live = None
        return 200, {"released": True}

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", old_endpoint)
    settings = {"node_id": "fake-node", "hub_url": "https://hub.example.invalid", "token": "fake-token"}
    assert fleet_client.acquire_claim("fake-target", holder="fake-holder", generation=0, **settings).granted
    assert fleet_client.renew_claim("fake-target", holder="fake-holder", generation=1, **settings).granted
    assert fleet_client.release_claim("fake-target", force=True, **settings).granted
    assert [body["action"] for body in posted] == ["acquire", "renew", "inspect", "release"]
    assert posted[0]["generation"] == 0 and posted[1]["generation"] == 1
    assert posted[-1]["generation"] == 0


def test_client_force_release_sends_inspected_generation_and_stale_is_terminal(authority, monkeypatch):
    conn, _, _ = authority
    request(conn)

    def endpoint(url, token, body, *, timeout):
        if body["action"] == "release":
            assert body["generation"] == 1
            request(conn, "release")
            request(conn, holder="fake-new-holder")
        return hub.handle_claim(conn, body)

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", endpoint)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda fn, **kw: fn())
    settings = {"node_id": "fake-node", "hub_url": "https://hub.example.invalid", "token": "fake-token"}
    assert fleet_client.release_claim("fake-target", force=True, **settings).reason == "stale-generation"
    assert fleet_client.acquire_claim("fake-target", holder="fake-holder", **settings).reason == "stale-holder"
    assert hub.list_claims(conn)[0]["generation"] == 3


def test_client_force_release_on_unseen_target_fences_generation_zero(authority, monkeypatch):
    conn, _, _ = authority

    def endpoint(url, token, body, *, timeout):
        if body["action"] == "release":
            request(conn, holder="fake-new-holder")
        return hub.handle_claim(conn, body)

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", endpoint)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda fn, **kw: fn())
    outcome = fleet_client.release_claim(
        "fake-target", force=True, node_id="fake-node", hub_url="https://hub.example.invalid", token="fake-token"
    )
    assert outcome.reason == "stale-generation"
    assert hub.list_claims(conn)[0]["generation"] == 1


@pytest.fixture()
def http_authority(authority):
    conn, path, clock = authority
    server = hub.make_server("127.0.0.1", 0, path, "fake-admin-token", allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield conn, clock, server.server_address
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def http_claim(address, method, target="fake-target", action="acquire", **extra):
    client = HTTPConnection(*address, timeout=5)
    try:
        body = json.dumps(
            {"action": action, "target": target, "node_id": "fake-node", "holder": "fake-holder", **extra}
        )
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


@pytest.mark.parametrize(
    "method,action", [("GET", "inspect"), ("POST", "acquire"), ("POST", "renew"), ("POST", "release")]
)
@pytest.mark.parametrize("damage", ["missing", "negative", "text", "real"])
def test_http_fence_refusal_status_depends_on_mutation(http_authority, method, action, damage):
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
    assert http_claim(address, method, action=action) == (
        503 if method == "GET" else 409,
        {"error": "claim generation fence is missing or corrupt", "reason": "claim-fence-refused", "owner": {}},
    )
    assert snapshot(conn) == before


@pytest.mark.parametrize("refusal", ["capacity", "exhausted"])
def test_http_storage_limits_are_409_and_roll_back_pruning(http_authority, monkeypatch, refusal):
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
    assert http_claim(address, "POST", target="fake-new") == (
        409,
        {"error": error, "reason": "claim-fence-refused", "owner": {}},
    )
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
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == hub.SCHEMA_VERSION
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
    assert request(conn, "release", holder="fake-missing-holder") == (
        200,
        {"released": False, "reason": "stale-holder"},
    )
    assert fence(conn) == 3
    assert request(conn, holder="fake-fresh-holder")[1]["claim"]["generation"] == 4
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
        assert request(reopened, holder="fake-fresh-holder")[1]["claim"]["generation"] == 3
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
    assert request(conn, holder="fake-fresh-holder")[1]["claim"]["generation"] == 3
    request(conn, "release", holder="fake-fresh-holder")
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
    newer_version = hub.SCHEMA_VERSION + 1
    conn.execute(f"PRAGMA user_version={newer_version}")
    conn.commit()
    with pytest.raises(hub.FleetHubError, match=f"schema version {newer_version}"):
        hub.init_db(path)
    with pytest.raises(hub.FleetHubError, match=f"schema version {newer_version}"):
        hub.open_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == newer_version


@pytest.mark.parametrize("target", ["x" * 1025, "é" * 513], ids=["ascii", "multibyte"])
@pytest.mark.parametrize("action", ["acquire", "release"])
def test_new_target_byte_limit_rolls_back_all_state(authority, target, action):
    conn, _, clock = authority
    request(conn, ttl_seconds=1)
    clock["now"] += 2
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubUnprocessable, match="UTF-8 byte limit"):
        request(conn, action, target=target)
    assert snapshot(conn) == before
    assert not conn.in_transaction


@pytest.mark.parametrize("target", ["x" * 1024, "é" * 512], ids=["ascii", "multibyte"])
def test_new_target_accepts_exact_utf8_byte_limit(authority, target):
    conn, _, _ = authority
    assert request(conn, target=target)[1]["claim"]["generation"] == 1
    assert request(conn, "release", target=target) == (200, {"released": True})
    assert fence(conn, target) == 2


@pytest.mark.parametrize(
    "target",
    ["é" * 513, "fake-secret-" + "x" * 1024, "fake-secret-\ud800"],
    ids=["multibyte", "secret", "invalid-utf8"],
)
def test_http_invalid_target_is_sanitized(http_authority, target):
    conn, clock, address = http_authority
    request(conn, ttl_seconds=1)
    clock["now"] += 2
    before = snapshot(conn)
    status, payload = http_claim(address, "POST", target=target)
    assert status == (422 if "\ud800" in target else 409)
    if "\ud800" not in target:
        assert payload["reason"] == "claim-target-too-long"
        assert payload["owner"] == {}
    error = payload["error"]
    assert error["code"] == "invalid-field"
    assert error["field"] == "target"
    if "\ud800" not in target:
        assert error["limit"] == 1024
        assert error["unit"] == "utf8-bytes"
    assert "fake-secret" not in json.dumps(payload)
    assert snapshot(conn) == before


def test_migration_preserves_oversize_legacy_target_and_its_operability(authority):
    conn, path, _ = authority
    request(conn)
    target = "é" * 1025
    conn.execute("UPDATE claims SET target = ?", (target,))
    conn.execute("DROP TABLE claim_fences")
    conn.execute("PRAGMA user_version=25")
    conn.commit()
    original = conn.execute("SELECT * FROM claims").fetchall()
    with hub.init_db(path) as migrated:
        assert migrated.execute("SELECT * FROM claims").fetchall() == original
        assert fence(migrated, target) == 1
        assert request(migrated, "renew", target=target)[0] == 200
        assert request(migrated, "release", target=target)[0] == 200
        assert request(migrated, target=target, holder="fake-fresh-holder")[1]["claim"]["generation"] == 3


@pytest.mark.parametrize("path", ["/view/machines", "/view/repos", "/", "/deck", "/deck/repos"])
def test_authenticated_html_fence_refusal_is_structured_503(http_authority, path):
    conn, _, address = http_authority
    request(conn)
    conn.execute("DELETE FROM claim_fences")
    conn.commit()
    before = snapshot(conn)
    client = HTTPConnection(*address, timeout=5)
    try:
        client.request("GET", path, headers={"Authorization": "Bearer fake-admin-token"})
        response = client.getresponse()
        assert response.status == 503
        assert json.loads(response.read()) == {
            "error": "claim generation fence is missing or corrupt",
            "reason": "claim-fence-refused",
            "owner": {},
        }
    finally:
        client.close()
    assert snapshot(conn) == before


@pytest.mark.parametrize("reader", ["validate_schema", "generation", "list_claims"])
def test_real_sqlite_read_lock_remains_operational_error(authority, reader):
    from brigade import fleet_hub_claims

    conn, path, _ = authority
    request(conn)
    conn.execute("PRAGMA journal_mode=DELETE")
    other = sqlite3.connect(path, timeout=0)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            if reader == "generation":
                fleet_hub_claims.generation(other, "fake-target", has_claim=True)
            else:
                getattr(fleet_hub_claims, reader)(other)
    finally:
        conn.rollback()
        other.close()


@pytest.mark.parametrize("version", [22, 24, 25])
def test_old_schema_preserves_and_validates_existing_fence_history(authority, version):
    conn, path, _ = authority
    request(conn)
    request(conn, "release")
    request(conn)
    request(conn, "release", target="fake-free-history")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    before = snapshot(conn)
    with hub.init_db(path) as migrated:
        assert snapshot(migrated) == before
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == hub.SCHEMA_VERSION
    with hub.init_db(path) as reopened:
        assert snapshot(reopened) == before


@pytest.mark.parametrize("damage", ["missing", "corrupt", "missing-column"])
def test_old_schema_never_backfills_unknown_existing_fences(authority, damage):
    conn, path, _ = authority
    request(conn)
    if damage == "missing":
        conn.execute("DELETE FROM claim_fences")
    elif damage == "corrupt":
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE claim_fences SET generation=-1")
    else:
        conn.execute("ALTER TABLE claim_fences RENAME COLUMN generation TO unknown_history")
    conn.execute("PRAGMA user_version=25")
    conn.commit()
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubError, match="fence"):
        hub.init_db(path)
    assert snapshot(conn) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 25


def test_two_connections_compete_for_one_new_target(authority):
    conn, path, _ = authority
    barrier = threading.Barrier(2)

    def acquire(holder):
        with hub.open_db(path) as other:
            barrier.wait(timeout=5)
            return request(other, holder=holder)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(acquire, holder) for holder in ("fake-one", "fake-two")]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(status for status, _ in results) == [200, 409]
    assert fence(conn) == 1
    assert len(hub.list_claims(conn)) == 1
    assert all(payload.get("claim", payload.get("owner"))["generation"] == 1 for _, payload in results)


def test_two_connections_migrate_schema25_once(authority):
    conn, path, _ = authority
    request(conn)
    original = conn.execute("SELECT * FROM claims").fetchall()
    conn.execute("DROP TABLE claim_fences")
    conn.execute("PRAGMA user_version=25")
    conn.commit()
    barrier = threading.Barrier(2)

    def migrate():
        with sqlite3.connect(path, timeout=5) as other:
            barrier.wait(timeout=5)
            # Use the production transaction owner without init_db's process-local lock.
            hub._init_schema(other, path)
            return other.execute("PRAGMA user_version").fetchone()[0], snapshot(other)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert all(version == hub.SCHEMA_VERSION for version, _ in results)
    assert results[0][1] == results[1][1] == (original, [("fake-target", 1)], [])


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_http_newer_schema_is_static_503_without_mutation(http_authority, method):
    conn, _, address = http_authority
    request(conn)
    before = snapshot(conn)
    conn.execute(f"PRAGMA user_version={hub.SCHEMA_VERSION + 1}")
    conn.commit()
    assert http_claim(address, method) == (503, {"error": "fleet hub database schema is newer than supported"})
    assert snapshot(conn) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == hub.SCHEMA_VERSION + 1


def test_claim_request_newer_schema_uses_static_refusal(authority):
    conn, _, _ = authority
    conn.execute(f"PRAGMA user_version={hub.SCHEMA_VERSION + 1}")
    conn.commit()
    with pytest.raises(hub.FleetHubError, match="^fleet hub database schema is newer than supported$"):
        request(conn)
    assert snapshot(conn) == ([], [], [])


_CHECK = "CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807)"
_UNSUPPORTED_FENCE_TABLES = {
    "no-primary-key": f"CREATE TABLE claim_fences (target TEXT NOT NULL, generation INTEGER NOT NULL {_CHECK})",
    "no-check": "CREATE TABLE claim_fences (target TEXT NOT NULL PRIMARY KEY, generation INTEGER NOT NULL)",
    "nocase": f"CREATE TABLE claim_fences (target TEXT NOT NULL PRIMARY KEY COLLATE NOCASE, generation INTEGER NOT NULL {_CHECK})",
    "nullable-target": f"CREATE TABLE claim_fences (target TEXT PRIMARY KEY, generation INTEGER NOT NULL {_CHECK})",
    "nullable-generation": f"CREATE TABLE claim_fences (target TEXT NOT NULL PRIMARY KEY, generation INTEGER {_CHECK})",
    "extra-column": f"CREATE TABLE claim_fences (target TEXT NOT NULL PRIMARY KEY, generation INTEGER NOT NULL {_CHECK}, note TEXT)",
}


def fence_ddl(conn):
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'claim_fences' COLLATE NOCASE"
    ).fetchall()


@pytest.mark.parametrize("version", [None, 25], ids=["current", "schema25"])
@pytest.mark.parametrize("shape", sorted(_UNSUPPORTED_FENCE_TABLES))
def test_unsupported_fence_table_shape_refuses_without_repair(authority, shape, version):
    conn, path, _ = authority
    request(conn)
    request(conn, "release")
    request(conn)
    request(conn, "release", target="fake-free-history")
    request(conn)
    history = conn.execute("SELECT target, generation FROM claim_fences ORDER BY target").fetchall()
    conn.execute("DROP TABLE claim_fences")
    conn.execute(_UNSUPPORTED_FENCE_TABLES[shape])
    conn.executemany("INSERT INTO claim_fences (target, generation) VALUES (?, ?)", history)
    if version is not None:
        conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    before = (snapshot(conn), fence_ddl(conn), conn.execute("PRAGMA user_version").fetchone()[0])
    assert [row[:2] for row in before[0][1]] == history
    with pytest.raises(hub.FleetHubError, match="fence"):
        hub.init_db(path)
    assert (snapshot(conn), fence_ddl(conn), conn.execute("PRAGMA user_version").fetchone()[0]) == before


@pytest.mark.parametrize(
    "extra", ["CREATE INDEX fake_idx ON claim_fences (generation)", "TRIGGER", "UPPERCASE_TRIGGER"]
)
def test_fence_table_with_extra_index_or_trigger_refuses(authority, extra):
    conn, path, _ = authority
    request(conn)
    if extra == "TRIGGER":
        extra = "CREATE TRIGGER fake_trg AFTER UPDATE ON claim_fences BEGIN SELECT 1; END"
    elif extra == "UPPERCASE_TRIGGER":
        # SQLite stores a trigger's tbl_name as written, so a case-sensitive lookup misses it.
        extra = "CREATE TRIGGER fake_trg AFTER UPDATE ON CLAIM_FENCES BEGIN SELECT 1; END"
    conn.execute(extra)
    conn.commit()
    before = (snapshot(conn), fence_ddl(conn))
    if extra.startswith("CREATE TRIGGER"):
        # The unchanged-state comparison must cover the trigger row, whatever its case.
        assert any(row[:2] == ("trigger", "fake_trg") for row in before[1])
    with pytest.raises(hub.FleetHubError, match="fence"):
        hub.init_db(path)
    assert (snapshot(conn), fence_ddl(conn)) == before


def test_old_schema_uppercase_fence_table_refuses_as_unsupported_without_repair(tmp_path):
    from brigade import fleet_hub_claims

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(hub._CLAIMS_SCHEMA)
    conn.execute(
        "INSERT INTO claims (target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
        "VALUES ('fake-target', 'fake-node', 'fake-holder', 'original', 'renewed', 900, 9999999999)"
    )
    # SQLite identifiers are ASCII case-insensitive, so this is the same table name.
    conn.execute(fleet_hub_claims._FENCE_SCHEMA.replace("claim_fences", "CLAIM_FENCES"))
    conn.execute("INSERT INTO CLAIM_FENCES (target, generation) VALUES ('fake-target', 7)")
    conn.execute("PRAGMA user_version=25")
    conn.commit()
    catalog = "SELECT type, name, sql FROM sqlite_master ORDER BY name"
    before = (
        conn.execute("SELECT * FROM claims").fetchall(),
        conn.execute("SELECT * FROM CLAIM_FENCES").fetchall(),
        conn.execute(catalog).fetchall(),
    )
    with pytest.raises(fleet_hub_claims.ClaimFenceError, match="unsupported definition"):
        hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
    assert (
        conn.execute("SELECT * FROM claims").fetchall(),
        conn.execute("SELECT * FROM CLAIM_FENCES").fetchall(),
        conn.execute(catalog).fetchall(),
    ) == before
    conn.close()


@pytest.mark.parametrize("action", ["acquire", "renew", "release", "inspect"])
def test_schema_upgrade_between_preflight_and_write_lock_refuses(authority, monkeypatch, action):
    conn, path, clock = authority
    request(conn)
    before = snapshot(conn)
    reads = []

    class UpgradeBeforeBegin(sqlite3.Connection):
        """A newer hub migrates on a second real connection after the preflight check."""

        upgraded = False

        def execute(self, sql, *args):
            if sql.startswith("BEGIN") and not self.upgraded:
                self.upgraded = True
                with sqlite3.connect(path, timeout=5) as newer:
                    newer.execute(f"PRAGMA user_version={hub.SCHEMA_VERSION + 1}")
            return super().execute(sql, *args)

    monkeypatch.setattr(hub, "_now_epoch", lambda: reads.append(1) or clock["now"])
    racing = sqlite3.connect(path, timeout=5, factory=UpgradeBeforeBegin)
    try:
        with pytest.raises(hub.FleetHubUnsupportedSchema, match="^fleet hub database schema is newer than supported$"):
            request(racing, action)
        assert racing.upgraded
        assert not racing.in_transaction
    finally:
        racing.close()
    assert reads == []
    assert snapshot(conn) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == hub.SCHEMA_VERSION + 1


@pytest.mark.parametrize("ending", ["expiry", "holder", "node", "force"])
def test_live_retirements_are_exempt_from_node_quota(authority, monkeypatch, ending):
    from brigade import fleet_hub_claims

    conn, _, clock = authority
    request(conn, ttl_seconds=1, lock={"token": "fake-lock"})
    request(conn, "release", target="fake-history")
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS_PER_NODE", 1)
    if ending == "expiry":
        clock["now"] += 1
        assert request(conn, node="fake-neighbor", holder="fake-next")[0] == 200
    else:
        assert request(conn, "release", scope=ending, acquired_at=hub.list_claims(conn)[0]["acquired_at"])[0] == 200
    assert conn.execute("SELECT COUNT(*) FROM claim_retired_holders").fetchone()[0] == 2
    assert request(conn, "renew")[1]["reason"] == "stale-holder"


@pytest.mark.parametrize("extra", [{}, {"generation": 0}], ids=["legacy", "generation"])
def test_fresh_acquire_checks_node_quota_with_or_without_generation(authority, monkeypatch, extra):
    from brigade import fleet_hub_claims

    conn, _, _ = authority
    request(conn, "release", target="fake-history")
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS_PER_NODE", 1)
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubError, match="quota"):
        request(conn, **extra)
    assert snapshot(conn) == before


@pytest.mark.parametrize("explicit_generation", [False, True])
def test_node_at_quota_cannot_supersede_chain(authority, monkeypatch, explicit_generation):
    from brigade import fleet_hub_claims

    conn, _, _ = authority
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS_PER_NODE", 1)
    lock = {"token": "fake-lock"}
    assert request(conn, lock=lock)[0] == 200
    next_lock = {"token": "fake-next-lock"}
    assert request(conn, holder="fake-next", scope="node", lock=next_lock, supersede=lock)[0] == 200
    before = snapshot(conn)
    extra = {"generation": fence(conn)} if explicit_generation else {}
    for holder in ("fake-chain-1", "fake-chain-2"):
        with pytest.raises(hub.FleetHubError, match="quota"):
            request(conn, holder=holder, scope="node", supersede=next_lock, **extra)
        assert snapshot(conn) == before
    assert request(conn, holder="fake-next")[0] == 200
    assert request(conn, "release", holder="fake-next")[0] == 200
    assert conn.execute("SELECT COUNT(*) FROM claim_retired_holders").fetchone()[0] == 2


def test_supersede_keeps_global_retirement_cap(authority, monkeypatch):
    from brigade import fleet_hub_claims

    conn, _, _ = authority
    request(conn, lock={"token": "fake-lock"})
    request(conn, "release", target="fake-history")
    monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS", 1)
    before = snapshot(conn)
    with pytest.raises(hub.FleetHubError, match="capacity"):
        request(conn, holder="fake-next", scope="node", supersede={"token": "fake-lock"})
    assert snapshot(conn) == before


def test_expiry_pruning_is_bounded_and_always_handles_requested_target(authority):
    conn, _, clock = authority
    for index in range(40):
        assert request(conn, target=f"fake-unrelated-{index:02}", ttl_seconds=1)[0] == 200
    assert request(conn, target="fake-z-requested", ttl_seconds=1)[0] == 200
    clock["now"] += 1
    assert request(conn, target="fake-z-requested", node="fake-neighbor", holder="fake-next")[0] == 200
    assert fence(conn, "fake-z-requested") == 3
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE expires_at <= ?", (clock["now"],)).fetchone()[0] == 8
    assert conn.execute("SELECT COUNT(*) FROM claim_retired_holders").fetchone()[0] == 33


@pytest.mark.parametrize(
    "damage", ["target-capacity", "retired-capacity", "corrupt", "target-too-long", "stale-holder"]
)
def test_new_hub_refusal_is_terminal_under_base_client_handling(http_authority, monkeypatch, damage):
    from brigade import fleet_hub_claims

    conn, _, address = http_authority
    request(conn, "release", target="fake-history")
    if damage == "target-capacity":
        monkeypatch.setattr(fleet_hub_claims, "MAX_CLAIM_TARGETS", 1)
    elif damage == "retired-capacity":
        monkeypatch.setattr(fleet_hub_claims, "MAX_RETIRED_HOLDERS", 1)
    elif damage == "corrupt":
        conn.execute("DROP TABLE claim_fences")
        conn.commit()
    elif damage == "stale-holder":
        request(conn, "release")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "fake-node")
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", lambda *a, **kw: pytest.fail("local fallback"))
    responses = []

    def base_acquire(*args, **kwargs):
        # Pre-fencing _claim_op (5527ed8e) ignores reason. An owner-less
        # 409 is 'missing', which repo_claim retries and then fails open.
        status, payload = http_claim(
            address, "POST", target="x" * 1025 if damage == "target-too-long" else "fake-target"
        )
        responses.append((status, payload))
        owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else None
        reason = ("held" if owner is not None else "missing") if status in (200, 409) else "hub-unavailable"
        return fleet_client.ClaimDecision(False, reason, owner=owner)

    monkeypatch.setattr(fleet_client, "acquire_claim", base_acquire)
    with pytest.raises(fleet_client.FleetClaimHeldError):
        with fleet_client.repo_claim("fake-target"):
            pytest.fail("base client admitted local-only work")
    assert len(responses) == 1
    assert responses[0][0] == 409
    assert responses[0][1]["reason"] == (
        "claim-target-too-long"
        if damage == "target-too-long"
        else "stale-holder"
        if damage == "stale-holder"
        else "claim-fence-refused"
    )


@pytest.mark.parametrize("recovers", [False, True])
def test_orphan_cleanup_retries_fence_refusal_until_success_or_deadline(monkeypatch, recovers):
    now, sleeps, attempts = [0.0], [], []

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    def release(*args, **kwargs):
        attempts.append(now[0])
        return fleet_client.ClaimDecision(
            recovers and len(attempts) == 3, "ok" if recovers and len(attempts) == 3 else "claim-fence-refused"
        )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(fleet_client.threading, "Thread", InlineThread)
    monkeypatch.setattr(fleet_client.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(fleet_client.time, "sleep", sleep)
    monkeypatch.setattr(fleet_client, "release_claim", release)
    monkeypatch.setattr(fleet_client, "ORPHAN_RELEASE_RETRY_SECONDS", 2)
    fleet_client._schedule_orphan_release(
        "fake-target", node_id="fake-node", holder="fake-holder", conductor=None, ttl_seconds=5
    )
    assert attempts == [0.0, 2.0, 4.0]
    assert sleeps == ([2, 2] if recovers else [2, 2, 1])


def test_ambiguous_acquire_then_fence_refusal_schedules_cleanup_without_admission(monkeypatch):
    attempts, cleanup = [], []

    def acquire(*args, **kwargs):
        attempts.append(kwargs["holder"])
        return fleet_client.ClaimDecision(False, "hub-unavailable" if len(attempts) == 1 else "claim-fence-refused")

    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "fake-node")
    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", lambda *a, **kw: cleanup.append(kw["holder"]))
    with pytest.raises(fleet_client.FleetClaimHeldError, match="claim-fence-refused"):
        with fleet_client.repo_claim("fake-target"):
            pytest.fail("refused admission")
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert cleanup == [attempts[0]]


def test_current_client_preserves_terminal_target_length_reason(http_authority, monkeypatch):
    _, _, address = http_authority
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": f"http://{address[0]}:{address[1]}", "token": "fake-admin-token"},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "fake-node")
    monkeypatch.setattr(fleet_client, "_schedule_orphan_release", lambda *a, **kw: pytest.fail("local fallback"))
    with pytest.raises(fleet_client.FleetClaimHeldError, match="claim-target-too-long"):
        with fleet_client.repo_claim("é" * 513):
            pytest.fail("oversized target admitted work")


@pytest.mark.parametrize("admission", ["normal", "ambiguous", "expired"])
def test_exit_fence_refusal_schedules_cleanup_and_recovers(http_authority, monkeypatch, admission):
    conn, _, address = http_authority
    monkeypatch.setattr(
        fleet_client,
        "load_fleet_config",
        lambda: {"hub_url": f"http://{address[0]}:{address[1]}", "token": "fake-admin-token"},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "fake-node")
    real_acquire, real_release = fleet_client.acquire_claim, fleet_client.release_claim
    now = [time.monotonic()]
    attempts, releases = [], []
    recovered = threading.Event()

    def acquire(*args, **kwargs):
        attempts.append(kwargs["holder"])
        if admission == "ambiguous" and len(attempts) == 1:
            return fleet_client.ClaimDecision(False, "hub-unavailable")
        result = real_acquire(*args, **kwargs)
        if admission == "expired":
            now[0] += 60
        return result

    def release(*args, **kwargs):
        releases.append(kwargs["holder"])
        if len(releases) <= 3:
            return fleet_client.ClaimDecision(False, "claim-fence-refused")
        result = real_release(*args, **kwargs)
        if result.granted:
            recovered.set()
        return result

    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    monkeypatch.setattr(fleet_client, "release_claim", release)
    monkeypatch.setattr(fleet_client, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=lambda seconds: None))
    monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 1000)
    if admission == "expired":
        with pytest.raises(fleet_client.FleetClaimExpiredError):
            with fleet_client.repo_claim("fake-target", ttl_seconds=60):
                pytest.fail("expired admission")
    else:
        with fleet_client.repo_claim("fake-target", ttl_seconds=60) as decision:
            assert decision.granted
    assert recovered.wait(5), "exit fence refusal abandoned cleanup before recovery"
    assert releases == [attempts[0]] * len(releases)
    assert snapshot(conn)[0] == []


@pytest.mark.parametrize("action", ["acquire", "renew", "release"])
@pytest.mark.parametrize("reason", ["stale-holder", "stale-generation"])
def test_free_target_terminal_refusal_has_compatibility_owner(http_authority, action, reason):
    conn, _, address = http_authority
    request(conn, "release")
    before = snapshot(conn)
    status, payload = http_claim(
        address, "POST", action=action, **({"generation": 0} if reason == "stale-generation" else {})
    )
    if action == "release" and reason == "stale-holder":
        assert (status, payload) == (200, {"released": False, "reason": "stale-holder"})
    else:
        assert status == 409
        assert payload["reason"] == reason
        assert payload["owner"] == {}
    assert snapshot(conn) == before


def test_free_target_stale_holder_heartbeat_is_terminal_under_base_client_handling(http_authority, monkeypatch):
    _, _, address = http_authority
    responses, acquisitions, lost = [], [], []
    notified = threading.Event()
    monkeypatch.setattr(fleet_client, "_thread", SimpleNamespace(interrupt_main=lambda: None))
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.example.invalid"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path=None: "fake-node")
    monkeypatch.setattr(fleet_client, "_claim_renew_interval", lambda ttl: 0.01)

    def base_decision(status, payload, key):
        # The deployed base client ignores fencing reasons. It maps a 409
        # with a non-dict owner to missing, allowing local-lock continuation.
        owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else None
        granted = status == 200 and payload.get(key) is True
        reason = "ok" if granted else "held" if owner is not None else "missing"
        return fleet_client.ClaimDecision(granted, reason, owner=owner)

    def acquire(target, **kwargs):
        acquisitions.append(kwargs["holder"])
        status, payload = http_claim(address, "POST", target=target, holder=kwargs["holder"])
        return base_decision(status, payload, "granted")

    def renew(target, **kwargs):
        assert http_claim(address, "POST", target=target, action="release", holder=kwargs["holder"])[0] == 200
        status, payload = http_claim(address, "POST", target=target, action="renew", holder=kwargs["holder"])
        responses.append((status, payload))
        return base_decision(status, payload, "renewed")

    def on_loss(reason):
        lost.append(reason)
        notified.set()

    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    monkeypatch.setattr(fleet_client, "renew_claim", renew)
    monkeypatch.setattr(
        fleet_client, "release_claim", lambda target, **kw: fleet_client.ClaimDecision(False, "stale-holder")
    )
    with fleet_client.repo_claim("fake-target", ttl_seconds=60, on_claim_lost=on_loss) as decision:
        assert notified.wait(2), "base client continued on the local lock after a stale-holder heartbeat refusal"
        assert decision.cancel_event.is_set()
    assert lost == ["held"]
    assert len(acquisitions) == 1
    assert len(responses) == 1
    assert responses[0][0] == 409
    assert responses[0][1]["reason"] == "stale-holder"
    assert responses[0][1]["owner"] == {}
