"""Fleet routing authority: telemetry, selection, reservations, and snapshot."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_hub_routing, fleet_policy, fleet_quota
from brigade.fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden


NODE_LINUX = "11111111-1111-4111-8111-111111111111"
NODE_LINUX_2 = "22222222-2222-4222-8222-222222222222"
NODE_WINDOWS = "33333333-3333-4333-8333-333333333333"
NODE_CLOUD = "44444444-4444-4444-8444-444444444444"
CONTROLLER = "55555555-5555-4555-8555-555555555555"


def _now() -> datetime:
    return datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _iso(stamp: datetime | None = None) -> str:
    return (stamp or fleet_hub_routing._now()).isoformat()


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {
            "roles": {"impl": "seat-grok"},
            "data": {"allow_training": False},
            "execution": {"concurrency": 1},
        },
        "routing": {
            "enabled": True,
            "telemetry_ttl_seconds": 300,
            "reservation_ttl_seconds": 300,
            "quota_reserve_percent": 4,
            "workload_requirements": {
                "general": {"os": "linux", "capabilities": ["general"]},
                "windows-native": {"os": "windows", "capabilities": ["gui"]},
            },
            "quota_pools": {
                "pool-grok": {
                    "account_id": "acct-generic-grok",
                    "provider": "xai",
                    "reserve_percent": 4,
                    "freshness_seconds": 300,
                },
                "pool-grokbot": {
                    "account_id": "acct-generic-grokbot",
                    "provider": "xai",
                    "reserve_percent": 4,
                    "freshness_seconds": 300,
                },
            },
        },
        "machines": {
            "worker-linux-1": {
                "os": "linux",
                "capabilities": ["general"],
                "preferred_workloads": ["general"],
                "prohibited_workloads": ["windows-native"],
                "priority": 200,
                "concurrency": 1,
                "fallback": ["worker-linux-2"],
                "node_id": NODE_LINUX,
            },
            "worker-linux-2": {
                "os": "linux",
                "capabilities": ["general"],
                "priority": 100,
                "concurrency": 1,
                "node_id": NODE_LINUX_2,
            },
            "worker-windows-1": {
                "os": "windows",
                "capabilities": ["gui"],
                "preferred_workloads": ["windows-native"],
                "priority": 50,
                "concurrency": 1,
                "node_id": NODE_WINDOWS,
            },
            "worker-cloud-1": {
                "os": "linux",
                "capabilities": ["general", "cloud"],
                "priority": 10,
                "concurrency": 2,
                "node_id": NODE_CLOUD,
            },
            "origin-windows-1": {
                "os": "windows",
                "priority": 0,
                "concurrency": 0,
                "prohibited_workloads": ["general", "windows-native"],
                "node_id": CONTROLLER,
            },
        },
        "seats": {
            "seat-grok": {
                "provider": "xai",
                "model": "model-grok-1",
                "eligible_machines": ["worker-linux-1", "worker-linux-2", "worker-cloud-1"],
                "concurrency": 2,
                "quota_pool": "pool-grok",
                "fallback": ["seat-grokbot"],
            },
            "seat-grokbot": {
                "provider": "xai",
                "model": "model-grokbot-1",
                "eligible_machines": ["worker-cloud-1"],
                "concurrency": 1,
                "quota_pool": "pool-grokbot",
            },
            "seat-train": {
                "provider": "xai",
                "model": "model-train-1",
                "training_allowed": True,
                "eligible_machines": ["worker-linux-1"],
                "quota_pool": "pool-grok",
            },
        },
        "consumers": {"consumer-one": {"reload": "refreshable"}},
        "repositories": {
            "repo/public": {"privacy": "public"},
            "repo/private": {"privacy": "private"},
        },
    }


def _quota_obs(observation_id: str, pool_id: str, account_id: str, used: float = 10.0) -> dict:
    stamp_dt = fleet_hub_routing._now()
    stamp = stamp_dt.isoformat()
    return {
        "observation_id": observation_id,
        "account_id": account_id,
        "pool_id": pool_id,
        "provider": "xai",
        "source": "crossusage-probe",
        "source_ref": f"ref-{observation_id}",
        "collected_at": stamp,
        "provider_time": stamp,
        "expires_at": (stamp_dt + timedelta(seconds=300)).isoformat(),
        "status": "current",
        "windows": [
            {
                "window_id": "hourly",
                "label": "hourly",
                "used": used,
                "limit": 100.0,
                "unit": "percent",
                "resets_at": (stamp_dt + timedelta(seconds=3600)).isoformat(),
                "period_duration_ms": 3_600_000,
            }
        ],
    }


def _fresh_quota(conn) -> None:
    stamp = _iso()
    token = stamp.replace(":", "").replace("-", "").replace("+", "").replace(".", "")
    fleet_quota.ingest_observations(
        conn,
        {
            "schema": fleet_quota.QUOTA_SCHEMA,
            "collected_at": stamp,
            "observations": [
                _quota_obs(f"obs-grok-{token}", "pool-grok", "acct-generic-grok", 10.0),
                _quota_obs(f"obs-bot-{token}", "pool-grokbot", "acct-generic-grokbot", 10.0),
            ],
        },
    )


def _observe(
    conn,
    node_id: str,
    *,
    status: str = "available",
    load: float = 0.0,
    observed_at: str | None = None,
    credential_state: str = "ok",
    running: dict | None = None,
    active_claims: list | None = None,
    usable_seats: list[str] | None = None,
) -> dict:
    body = {
        "node_id": node_id,
        "observed_at": observed_at or _iso(),
        "ttl_seconds": 300,
        "status": status,
        "load": load,
        "running": running or {"session_ids": [], "run_ids": []},
        "active_claims": active_claims if active_claims is not None else [],
        "usable_seats": ["seat-grok"] if usable_seats is None else usable_seats,
        "credential_state": credential_state,
    }
    return fleet_hub_routing.observe_machine(conn, body, caller_node=node_id)


def _observe_linux_fleet(conn) -> None:
    _observe(conn, NODE_LINUX, load=0.0)
    _observe(conn, NODE_LINUX_2, load=0.0)
    _observe(conn, NODE_CLOUD, load=0.0)
    _observe(conn, NODE_WINDOWS, load=0.0, usable_seats=[])


def _route(conn, *, session_id: str = "session-1", **extra) -> dict:
    request = {
        "consumer": "consumer-one",
        "repo_identity": "repo/public",
        "session_id": session_id,
        "origin": "origin-windows-1",
        "workload": "general",
    }
    request.update(extra)
    return fleet_hub_routing.route(conn, request, caller_node=CONTROLLER)


def _save_policy(conn) -> None:
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="enable routing")


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_hub_routing, "_now", _now)
    monkeypatch.setattr(fleet_quota, "_now", _now)
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        _save_policy(connection)
        yield connection
    finally:
        connection.close()


def test_windows_origin_ordinary_linux_routes_to_preferred_linux(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, origin="origin-windows-1")
    assert decision["schema"] == fleet_hub_routing.ROUTE_SCHEMA
    assert decision["selected"] == {"machine": "worker-linux-1", "seat": "seat-grok"}
    assert decision["reservation_id"]
    assert decision["policy_version"] == 2
    machines = [row["machine"] for row in decision["candidates"]]
    assert "worker-linux-1" in machines
    assert decision["selected"]["machine"] != "worker-windows-1"


def test_busy_preferred_falls_back_to_next_safe_host(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    _observe(conn, NODE_LINUX, status="busy", load=1.0, observed_at=_iso(_now() + timedelta(seconds=1)))
    decision = _route(conn)
    assert decision["selected"]["machine"] == "worker-linux-2"
    reasons = {row["machine"]: row["reasons"] for row in decision["candidates"] if row["machine"] == "worker-linux-1"}
    assert any("capacity" in reason or "busy" in reason for reason in reasons["worker-linux-1"])


def test_explicit_reason_override_is_recorded_on_the_receipt(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(
        conn,
        machine="worker-linux-2",
        override_reason="operator pinned the spare linux worker",
    )
    assert decision["selected"]["machine"] == "worker-linux-2"
    assert decision["override_reason"] == "operator pinned the spare linux worker"
    assert decision["reason"]


def test_explicit_override_without_reason_is_refused(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, machine="worker-linux-2")
    assert decision["selected"] is None
    assert decision["reason"] == "override-reason-required"
    assert decision["decision_id"]


def test_explicit_ineligible_override_does_not_fall_through(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(
        conn,
        machine="worker-windows-1",
        override_reason="try the windows box anyway",
    )
    assert decision["selected"] is None
    assert decision["reason"] == "machine-override-ineligible"
    assert decision["reservation_id"] is None
    assert any(row["machine"] == "worker-windows-1" for row in decision["candidates"])


def test_rejects_prohibited_wrong_os(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, workload="windows-native", session_id="session-win")
    assert decision["selected"] is None or decision["selected"]["machine"] == "worker-windows-1"
    linux_rows = [row for row in decision["candidates"] if row["machine"] == "worker-linux-1"]
    assert linux_rows
    assert any("prohibited" in reason or "wrong-os" in reason for reason in linux_rows[0]["reasons"])


def test_stale_capacity_and_auth_are_not_safe_idle(conn):
    _fresh_quota(conn)
    _observe(conn, NODE_LINUX_2)
    _observe(conn, NODE_CLOUD)
    _observe(conn, NODE_WINDOWS, usable_seats=[])
    _observe(conn, NODE_LINUX, observed_at=_iso(_now() - timedelta(seconds=900)))
    stale = _route(conn, session_id="session-stale")
    assert stale["selected"]["machine"] != "worker-linux-1"
    _observe(conn, NODE_LINUX, observed_at=_iso(), credential_state="stale")
    auth = _route(conn, session_id="session-auth")
    assert auth["selected"] is None or auth["selected"]["machine"] != "worker-linux-1"
    reasons = []
    for row in stale["candidates"] + auth["candidates"]:
        if row["machine"] == "worker-linux-1":
            reasons.extend(row["reasons"])
    assert any("stale" in reason or "auth" in reason or "unknown" in reason for reason in reasons)


def test_claim_collision_uses_live_db_claims_not_client_list(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    now = fleet_hub._now_epoch()
    conn.execute(
        "INSERT INTO claims (target, owner_node, owner_conductor, harness, role, job, session, "
        "holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
        "VALUES (?, ?, NULL, NULL, NULL, NULL, ?, 'holder-a', ?, ?, 900, ?)",
        (
            "repo/other",
            NODE_LINUX,
            "session-foreign",
            _iso(),
            _iso(),
            now + 900,
        ),
    )
    conn.commit()
    _observe(conn, NODE_LINUX, active_claims=[], observed_at=_iso(_now() + timedelta(seconds=1)))
    decision = _route(conn)
    assert decision["selected"]["machine"] != "worker-linux-1"
    linux = next(row for row in decision["candidates"] if row["machine"] == "worker-linux-1")
    assert any("claim" in reason or "capacity" in reason for reason in linux["reasons"])


def test_training_disallowed_for_private_repository(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(
        conn,
        repo_identity="repo/private",
        seat="seat-train",
        override_reason="try the training seat",
    )
    assert decision["selected"] is None or decision["selected"]["seat"] != "seat-train"
    train = [row for row in decision["candidates"] if row["seat"] == "seat-train"]
    assert train
    assert any("training" in reason for reason in train[0]["reasons"])


def test_unknown_workload_and_unmapped_node_are_recorded(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    unknown = _route(conn, workload="not-a-workload", session_id="session-unknown-work")
    assert unknown["selected"] is None
    assert unknown["reason"] == "unknown-workload"
    broken = _document()
    broken["machines"]["worker-linux-1"].pop("node_id")
    fleet_hub_policy.save_policy(conn, broken, expected_version=2, actor="operator", reason="drop node map")
    _observe(conn, NODE_LINUX_2)
    _observe(conn, NODE_CLOUD)
    unmapped = _route(conn, session_id="session-unmapped")
    linux = [row for row in unmapped["candidates"] if row["machine"] == "worker-linux-1"]
    assert linux
    assert any("unmapped" in reason for reason in linux[0]["reasons"])


def test_telemetry_does_not_stale_static_policy(conn):
    before = fleet_hub_policy.current_policy(conn)
    _observe_linux_fleet(conn)
    _fresh_quota(conn)
    after = fleet_hub_policy.current_policy(conn)
    assert after["revision"] == before["revision"]
    assert after["digest"] == before["digest"]
    assert fleet_quota.quota_version(conn) >= 1


def test_idempotent_request_replay_and_digest_conflict(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    first = _route(conn, session_id="session-replay")
    second = _route(conn, session_id="session-replay")
    assert first["decision_id"] == second["decision_id"]
    assert first["reservation_id"] == second["reservation_id"]
    with pytest.raises(FleetHubConflict):
        _route(conn, session_id="session-replay", workload="windows-native")


def test_target_revalidation_does_not_select_again(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    first = _route(conn, session_id="session-target")
    replay = fleet_hub_routing.route(
        conn,
        {
            "consumer": "consumer-one",
            "repo_identity": "repo/public",
            "session_id": "session-target",
            "origin": "origin-windows-1",
            "workload": "general",
            "decision_id": first["decision_id"],
        },
        caller_node=NODE_LINUX,
    )
    assert replay["decision_id"] == first["decision_id"]
    assert replay["selected"] == first["selected"]
    with pytest.raises(FleetHubForbidden):
        fleet_hub_routing.route(
            conn,
            {
                "consumer": "consumer-one",
                "repo_identity": "repo/public",
                "session_id": "session-target",
                "origin": "origin-windows-1",
                "workload": "general",
                "decision_id": first["decision_id"],
            },
            caller_node=NODE_LINUX_2,
        )


def test_wrong_owner_cannot_renew_or_release(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, session_id="session-own")
    request = {
        "reservation_id": decision["reservation_id"],
        "decision_id": decision["decision_id"],
        "session_id": "session-own",
    }
    fleet_hub_routing.claim_dispatch_intent(conn, request, caller_node=NODE_LINUX)
    with pytest.raises(FleetHubForbidden):
        fleet_hub_routing.renew_reservation(conn, request, caller_node=NODE_LINUX_2)
    with pytest.raises(FleetHubForbidden):
        fleet_hub_routing.release_reservation(conn, request, caller_node=NODE_LINUX_2)
    renewed = fleet_hub_routing.renew_reservation(conn, request, caller_node=NODE_LINUX)
    assert renewed["schema"] == fleet_hub_routing.RESERVATION_SCHEMA
    assert renewed["state"] == "active"


def test_originating_controller_may_cancel_unclaimed_intent_only(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, session_id="session-cancel")
    request = {
        "reservation_id": decision["reservation_id"],
        "decision_id": decision["decision_id"],
        "session_id": "session-cancel",
    }
    cancelled = fleet_hub_routing.cancel_unclaimed_intent(conn, request, caller_node=CONTROLLER)
    assert cancelled["state"] == "released"
    decision2 = _route(conn, session_id="session-claimed")
    request2 = {
        "reservation_id": decision2["reservation_id"],
        "decision_id": decision2["decision_id"],
        "session_id": "session-claimed",
    }
    fleet_hub_routing.claim_dispatch_intent(conn, request2, caller_node=NODE_LINUX)
    with pytest.raises(FleetHubForbidden):
        fleet_hub_routing.cancel_unclaimed_intent(conn, request2, caller_node=CONTROLLER)


def test_ambiguous_accepted_expiry_keeps_reservation(conn, monkeypatch):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, session_id="session-held")
    request = {
        "reservation_id": decision["reservation_id"],
        "decision_id": decision["decision_id"],
        "session_id": "session-held",
    }
    fleet_hub_routing.claim_dispatch_intent(conn, request, caller_node=NODE_LINUX)

    def later() -> datetime:
        return _now() + timedelta(seconds=900)

    monkeypatch.setattr(fleet_hub_routing, "_now", later)
    with pytest.raises((FleetHubError, FleetHubConflict, FleetHubForbidden)):
        fleet_hub_routing.renew_reservation(conn, request, caller_node=NODE_LINUX)
    snap = fleet_hub_routing.snapshot(conn)
    linux = next(row for row in snap["machines"] if row["id"] == "worker-linux-1")
    assert linux["available_slots"] in {0, None}
    assert linux["active_claims"] != 0 or linux["unused_reason"]
    other = _route(conn, session_id="session-next")
    assert other["selected"] is None or other["selected"]["machine"] != "worker-linux-1"
    released = fleet_hub_routing.release_reservation(conn, request, caller_node=NODE_LINUX)
    assert released["state"] == "released"


def test_unclaimed_expiry_frees_the_slot(conn, monkeypatch):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    first = _route(conn, session_id="session-unclaimed")
    assert first["selected"]["machine"] == "worker-linux-1"

    def later() -> datetime:
        return _now() + timedelta(seconds=900)

    monkeypatch.setattr(fleet_hub_routing, "_now", later)
    monkeypatch.setattr(fleet_quota, "_now", later)
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    second = _route(conn, session_id="session-after-expiry")
    assert second.get("selected"), second
    assert second["selected"]["machine"] == "worker-linux-1"
    assert second["decision_id"] != first["decision_id"]


def test_old_telemetry_cannot_overwrite_a_fresh_snapshot(conn):
    fresh = _observe(conn, NODE_LINUX, load=0.0, observed_at=_iso())
    stale = _observe(conn, NODE_LINUX, load=0.9, observed_at=_iso(_now() - timedelta(seconds=30)))
    assert stale["ignored"] is True or stale["load"] == fresh["load"]
    snap = fleet_hub_routing.snapshot(conn)
    linux = next(row for row in snap["machines"] if row["id"] == "worker-linux-1")
    assert linux["load"] == 0.0


def test_zero_load_is_real_and_unknown_is_not_idle(conn):
    _fresh_quota(conn)
    _observe(conn, NODE_LINUX, load=0.0, status="unknown")
    _observe(conn, NODE_LINUX_2, load=0.0)
    _observe(conn, NODE_CLOUD, load=0.0)
    decision = _route(conn)
    assert decision["selected"]["machine"] != "worker-linux-1"
    linux = next(row for row in decision["candidates"] if row["machine"] == "worker-linux-1")
    assert any("unknown" in reason for reason in linux["reasons"])


def test_snapshot_uses_stored_decisions_and_unknown_not_zero(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, session_id="session-snap")
    snap = fleet_hub_routing.snapshot(conn)
    assert snap["policy"]["version"] == 2
    assert snap["policy"]["digest"]
    assert any(row["decision_id"] == decision["decision_id"] for row in snap["routes"])
    linux = next(row for row in snap["machines"] if row["id"] == "worker-linux-1")
    assert linux["os"] == "linux"
    assert linux["capacity"] == 1
    assert linux["available_slots"] == 0
    ghost = next(row for row in snap["machines"] if row["id"] == "worker-windows-1")
    assert ghost["last_observed_at"]
    assert snap["queue"]["eligible_count"] is None or isinstance(snap["queue"]["eligible_count"], int)
    assert "blocked" in snap["queue"]


def test_work_id_callback_holds_unsupported_or_unready_work(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)

    def lookup(_conn, work_id: str):
        return {
            "work_id": work_id,
            "status": "captured",
            "burn_eligible": True,
            "execution_mode": "agent",
            "acceptance": ["done"],
            "effective_acceptance": ["done"],
            "attempt_count": 0,
            "blocker": None,
            "review_after": None,
            "spend_by": None,
        }

    fleet_hub_routing.set_work_item_lookup(lookup)
    try:
        decision = _route(conn, session_id="session-work", work_id="wl-not-ready")
        assert decision["selected"] is None
        assert "work" in decision["reason"] or "held" in decision["reason"]
    finally:
        fleet_hub_routing.set_work_item_lookup(None)


def test_missing_work_item_is_structured_held_not_invented(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    fleet_hub_routing.set_work_item_lookup(None)
    decision = _route(conn, session_id="session-missing-work", work_id="wl-does-not-exist")
    assert decision["selected"] is None
    assert decision["reason"] in {"work-missing", "work-held", "worklore-integration-pending"}


def test_atomic_concurrent_same_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_hub_routing, "_now", _now)
    monkeypatch.setattr(fleet_quota, "_now", _now)
    db = tmp_path / "fleet.db"
    setup = fleet_hub.init_db(db)
    _save_policy(setup)
    _fresh_quota(setup)
    _observe_linux_fleet(setup)
    setup.close()

    results: list[dict] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def worker(session_id: str) -> None:
        connection = sqlite3.connect(str(db), timeout=10)
        connection.execute("PRAGMA busy_timeout=8000")
        barrier.wait()
        try:
            payload = fleet_hub_routing.route(
                connection,
                {
                    "consumer": "consumer-one",
                    "repo_identity": "repo/public",
                    "session_id": session_id,
                    "origin": "origin-windows-1",
                    "workload": "general",
                },
                caller_node=CONTROLLER,
            )
        finally:
            connection.close()
        with lock:
            results.append(payload)

    threads = [
        threading.Thread(target=worker, args=("session-a",)),
        threading.Thread(target=worker, args=("session-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    selected = [row["selected"]["machine"] for row in results if row.get("selected")]
    assert len(selected) == 2
    assert selected.count("worker-linux-1") == 1
    assert len(set(selected)) == 2


def test_routing_schema_tables_are_additive(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "fleet_machine_telemetry",
        "fleet_route_decisions",
        "fleet_reservations",
        "fleet_dispatch_intents",
        "fleet_quota_observations",
    } <= tables
    assert fleet_hub.SCHEMA_VERSION >= 21


def test_missing_credentials_and_empty_usable_seats_are_not_known_capacity(conn):
    _fresh_quota(conn)
    _observe(conn, NODE_LINUX_2)
    _observe(conn, NODE_CLOUD)
    with pytest.raises(FleetHubError):
        fleet_hub_routing.observe_machine(
            conn,
            {
                "node_id": NODE_LINUX,
                "observed_at": _iso(),
                "ttl_seconds": 300,
                "status": "available",
                "load": 0.0,
                "running": {"session_ids": [], "run_ids": []},
                "active_claims": [],
                "usable_seats": ["seat-grok"],
            },
            caller_node=NODE_LINUX,
        )
    _observe(conn, NODE_LINUX, usable_seats=[])
    decision = _route(conn, session_id="session-empty-usable")
    linux = next(row for row in decision["candidates"] if row["machine"] == "worker-linux-1")
    assert linux["eligible"] is False
    assert any("unusable" in reason or "unknown" in reason for reason in linux["reasons"])
    assert decision["selected"] is None or decision["selected"]["machine"] != "worker-linux-1"


def test_telemetry_expires_at_ttl_boundary_and_uses_min_policy_and_report_ttl(conn):
    _fresh_quota(conn)
    _observe(conn, NODE_LINUX_2)
    _observe(conn, NODE_CLOUD)
    body = {
        "node_id": NODE_LINUX,
        "observed_at": _iso(_now() - timedelta(seconds=300)),
        "ttl_seconds": 300,
        "status": "available",
        "load": 0.0,
        "running": {"session_ids": [], "run_ids": []},
        "active_claims": [],
        "usable_seats": ["seat-grok"],
        "credential_state": "ok",
    }
    fleet_hub_routing.observe_machine(conn, body, caller_node=NODE_LINUX)
    stale = _route(conn, session_id="session-ttl-boundary")
    linux = next(row for row in stale["candidates"] if row["machine"] == "worker-linux-1")
    assert linux["eligible"] is False
    assert any("stale" in reason or "unknown" in reason for reason in linux["reasons"])

    short = dict(body)
    short["observed_at"] = _iso(_now() - timedelta(seconds=20))
    short["ttl_seconds"] = 10
    fleet_hub_routing.observe_machine(conn, short, caller_node=NODE_LINUX)
    report = _route(conn, session_id="session-report-ttl")
    linux = next(row for row in report["candidates"] if row["machine"] == "worker-linux-1")
    assert linux["eligible"] is False


def test_same_instant_conflicting_telemetry_is_unknown_not_first_wins(conn):
    stamp = _iso()
    fleet_hub_routing.observe_machine(
        conn,
        {
            "node_id": NODE_LINUX,
            "observed_at": stamp,
            "ttl_seconds": 300,
            "status": "available",
            "load": 0.0,
            "running": {"session_ids": [], "run_ids": []},
            "active_claims": [],
            "usable_seats": ["seat-grok"],
            "credential_state": "ok",
        },
        caller_node=NODE_LINUX,
    )
    with pytest.raises((FleetHubConflict, FleetHubError)):
        fleet_hub_routing.observe_machine(
            conn,
            {
                "node_id": NODE_LINUX,
                "observed_at": stamp,
                "ttl_seconds": 300,
                "status": "busy",
                "load": 1.0,
                "running": {"session_ids": [], "run_ids": []},
                "active_claims": [],
                "usable_seats": ["seat-grok"],
                "credential_state": "ok",
            },
            caller_node=NODE_LINUX,
        )


def test_reservation_and_linked_claim_count_as_one_occupancy(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    bumped = _document()
    bumped["machines"]["worker-linux-1"]["concurrency"] = 2
    fleet_hub_policy.save_policy(conn, bumped, expected_version=2, actor="operator", reason="two slots")
    first = _route(conn, session_id="session-linked")
    assert first["selected"]["machine"] == "worker-linux-1"
    now = fleet_hub._now_epoch()
    conn.execute(
        "INSERT INTO claims (target, owner_node, owner_conductor, harness, role, job, session, "
        "holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
        "VALUES (?, ?, NULL, NULL, NULL, NULL, ?, 'holder-linked', ?, ?, 900, ?)",
        ("repo/linked", NODE_LINUX, "session-linked", _iso(), _iso(), now + 900),
    )
    conn.commit()
    second = _route(conn, session_id="session-next-slot")
    assert second["selected"]["machine"] == "worker-linux-1"


def test_another_node_cannot_reuse_a_session_key(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    first = _route(conn, session_id="session-owned")
    with pytest.raises(FleetHubForbidden):
        fleet_hub_routing.route(
            conn,
            {
                "consumer": "consumer-one",
                "repo_identity": "repo/public",
                "session_id": "session-owned",
                "origin": "worker-linux-2",
                "workload": "general",
            },
            caller_node=NODE_LINUX_2,
        )
    assert first["selected"]["machine"] == "worker-linux-1"


def test_unknown_role_mapping_does_not_fall_back_to_every_seat(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    broken = _document()
    broken["defaults"]["roles"] = {}
    fleet_hub_policy.save_policy(conn, broken, expected_version=2, actor="operator", reason="drop impl")
    decision = _route(conn, session_id="session-no-role")
    assert decision["selected"] is None
    assert decision["reason"] in {"no-eligible-candidate", "unknown-role", "role-unmapped"}


def test_queued_denial_can_be_retried_when_capacity_appears(conn):
    _fresh_quota(conn)
    denied = _route(conn, session_id="session-retry")
    assert denied["selected"] is None
    _observe_linux_fleet(conn)
    retried = _route(conn, session_id="session-retry")
    assert retried["selected"]["machine"] == "worker-linux-1"
    assert retried["decision_id"] != denied["decision_id"]


def test_public_route_envelope_is_exact_and_omits_private_fields(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    decision = _route(conn, session_id="session-public")
    public = fleet_hub_routing.public_route(decision)
    assert set(public) == {
        "schema",
        "decision_id",
        "policy_version",
        "policy_digest",
        "selected",
        "reason",
        "candidates",
        "reservation_id",
        "expires_at",
    }
    assert public["schema"] == fleet_hub_routing.ROUTE_SCHEMA
    for row in public["candidates"]:
        assert set(row) == {"machine", "seat", "eligible", "reason"}
    dumped = str(public)
    assert "token" not in dumped.lower()
    assert "observed_id" not in dumped


def test_evaluate_work_item_uses_canonical_exclusion_and_does_not_default_missing_attempts(conn):
    missing_attempts = {
        "work_id": "wl-1",
        "status": "ready",
        "burn_eligible": True,
        "execution_mode": "agent",
        "acceptance": ["done"],
        "effective_acceptance": ["done"],
        "blocker": None,
        "review_after": None,
        "spend_by": None,
    }
    verdict = fleet_hub_routing.evaluate_work_item(missing_attempts)
    assert verdict["ok"] is False

    not_burn = {
        "work_id": "wl-2",
        "status": "ready",
        "burn_eligible": False,
        "execution_mode": "agent",
        "acceptance": ["done"],
        "effective_acceptance": ["done"],
        "attempt_count": 0,
        "blocker": None,
        "review_after": None,
        "spend_by": None,
    }
    held = fleet_hub_routing.evaluate_work_item(not_burn)
    assert held["ok"] is False
    assert "held" in str(held["reason"]) or "eligible" in str(held.get("detail") or "")

    future = {
        **not_burn,
        "work_id": "wl-3",
        "burn_eligible": True,
        # exclusion_bucket compares review_after against the real clock, so the
        # hold must be anchored to wall time rather than the frozen test clock.
        "review_after": _iso(datetime.now(timezone.utc) + timedelta(days=1)),
        "attempt_count": 0,
    }
    delayed = fleet_hub_routing.evaluate_work_item(future)
    assert delayed["ok"] is False


def test_zero_concurrency_is_disabled_not_unlimited(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    disabled = _document()
    disabled["machines"]["worker-linux-1"]["concurrency"] = 0
    fleet_hub_policy.save_policy(conn, disabled, expected_version=2, actor="operator", reason="disable linux-1")
    decision = _route(conn, session_id="session-zero-concurrency")
    linux = next(row for row in decision["candidates"] if row["machine"] == "worker-linux-1")
    assert linux["eligible"] is False
    assert any("disabled" in reason or "capacity" in reason for reason in linux["reasons"])
    assert decision["selected"] is None or decision["selected"]["machine"] != "worker-linux-1"


def test_snapshot_does_not_label_eligible_unused_without_known_work(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    snap = fleet_hub_routing.snapshot(conn)
    linux = next(row for row in snap["machines"] if row["id"] == "worker-linux-2")
    assert linux["unused_reason"] != "eligible-unused"
    assert snap["queue"]["eligible_count"] is None or isinstance(snap["queue"]["eligible_count"], int)
    assert snap["queue"].get("complete") is not True
    assert "ALLCLEAR" not in str(snap).upper()


def test_unclaimed_expired_reservation_cannot_revalidate(conn, monkeypatch):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    first = _route(conn, session_id="session-expired-revalidate")

    def later() -> datetime:
        return _now() + timedelta(seconds=900)

    monkeypatch.setattr(fleet_hub_routing, "_now", later)
    with pytest.raises((FleetHubError, FleetHubConflict, FleetHubForbidden)):
        fleet_hub_routing.route(
            conn,
            {
                "consumer": "consumer-one",
                "repo_identity": "repo/public",
                "session_id": "session-expired-revalidate",
                "origin": "origin-windows-1",
                "workload": "general",
                "decision_id": first["decision_id"],
            },
            caller_node=NODE_LINUX,
        )


def _insert_legacy_lease(conn, *, lease_id: str, seat: str, node_id: str) -> None:
    import time

    conn.execute(
        "INSERT INTO model_leases (lease_id, seat, owner_node, holder_token, acquired_at, expires_at, released_at, "
        "provider, model, launch_model, account_id, pool, decision_id, session_id, consumer, context_hash, "
        "request_id, policy_digest, policy_version) "
        "VALUES (?, ?, ?, 'holder', ?, ?, NULL, 'xai', 'grok-4.6', NULL, NULL, NULL, NULL, NULL, 'brigade-run', "
        "NULL, ?, NULL, NULL)",
        (lease_id, seat, node_id, fleet_hub._epoch_to_iso(time.time()), time.time() + 3600, lease_id),
    )
    conn.commit()


def test_active_legacy_lease_counts_against_shared_seat_capacity(conn):
    _fresh_quota(conn)
    _observe_linux_fleet(conn)
    document = fleet_hub_policy.current_policy(conn)["document"]
    seat_capacity = int(document["seats"]["seat-grok"].get("concurrency") or 0)
    assert seat_capacity >= 1
    for index in range(seat_capacity):
        _insert_legacy_lease(conn, lease_id=f"lease-legacy-{index}", seat="seat-grok", node_id=NODE_LINUX)
    decision = _route(conn)
    assert decision["selected"] is None, decision
    grok_rows = [row for row in decision["candidates"] if row["seat"] == "seat-grok"]
    assert grok_rows
    assert all("machine-capacity" in row["reasons"] for row in grok_rows)
    conn.execute(
        "UPDATE model_leases SET released_at=? WHERE seat='seat-grok'", (fleet_hub_routing._now().isoformat(),)
    )
    conn.commit()
    freed = _route(conn, session_id="session-2")
    assert freed["selected"] is not None, freed
