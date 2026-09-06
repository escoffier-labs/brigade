"""fleet work next: read-only first-eligible burn item with a dry-run route."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_client_policy, fleet_hub, fleet_hub_policy, fleet_hub_policy_api, fleet_hub_routing
from brigade import fleet_policy, fleet_quota, worklore_store

NODE_LINUX = "11111111-1111-4111-8111-111111111111"
NODE_LINUX_2 = "22222222-2222-4222-8222-222222222222"
NODE_WINDOWS = "33333333-3333-4333-8333-333333333333"
NODE_CLOUD = "44444444-4444-4444-8444-444444444444"
CONTROLLER = "55555555-5555-4555-8555-555555555555"
ADMIN_TOKEN = "test-admin-token-work-next"


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
            },
            "quota_pools": {
                "pool-grok": {
                    "account_id": "acct-generic-grok",
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
                "prohibited_workloads": ["general"],
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
            },
        },
        "consumers": {"brigade-run": {"reload": "refreshable"}},
        "repositories": {"repo/public": {"privacy": "public"}},
    }


def _quota_obs(observation_id: str) -> dict:
    stamp_dt = fleet_hub_routing._now()
    stamp = stamp_dt.isoformat()
    return {
        "observation_id": observation_id,
        "account_id": "acct-generic-grok",
        "pool_id": "pool-grok",
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
                "used": 10.0,
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
            "observations": [_quota_obs(f"obs-grok-{token}")],
        },
    )


def _observe(conn, node_id: str, *, usable_seats: list | None = None) -> dict:
    return fleet_hub_routing.observe_machine(
        conn,
        {
            "node_id": node_id,
            "observed_at": _iso(),
            "ttl_seconds": 300,
            "status": "available",
            "load": 0.0,
            "running": {"session_ids": [], "run_ids": []},
            "active_claims": [],
            "usable_seats": ["seat-grok"] if usable_seats is None else usable_seats,
            "credential_state": "ok",
        },
        caller_node=node_id,
    )


def _observe_fleet(conn) -> None:
    _observe(conn, NODE_LINUX)
    _observe(conn, NODE_LINUX_2)
    _observe(conn, NODE_CLOUD)
    _observe(conn, NODE_WINDOWS, usable_seats=[])


def _seed(conn, title: str, *, rank: int = 100, **extra) -> str:
    body: dict = {
        "title": title,
        "kind": "fleet",
        "acceptance": ["done when complete"],
        "burn_eligible": True,
        "execution_mode": "agent",
        "burn_rank": rank,
    }
    body.update(extra)
    item = worklore_store.create_item(conn, body, actor_id="admin")
    ready = worklore_store.transition(conn, item["work_id"], to_status="ready", expected_version=1, actor_id="admin")
    return str(ready["work_id"])


def _counts(conn) -> tuple[int, int]:
    reservations = conn.execute("SELECT COUNT(*) FROM fleet_reservations").fetchone()[0]
    decisions = conn.execute("SELECT COUNT(*) FROM fleet_route_decisions").fetchone()[0]
    return int(reservations), int(decisions)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_hub_routing, "_now", _now)
    monkeypatch.setattr(fleet_quota, "_now", _now)
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        fleet_hub_policy.save_policy(connection, _document(), expected_version=1, actor="operator", reason="test")
        _fresh_quota(connection)
        _observe_fleet(connection)
        yield connection
    finally:
        connection.close()


def test_selects_first_eligible_in_burn_order(conn):
    first = _seed(conn, "Second seeded", rank=20)
    second = _seed(conn, "First seeded", rank=5)
    assert first != second
    result = fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    assert result["schema"] == fleet_hub_routing.WORK_NEXT_SCHEMA
    assert result["work_id"] == second
    assert result["item"]["work_id"] == second
    assert result["item"]["title"] == "First seeded"
    assert "description" not in result["item"]
    assert "prompt" not in result["item"]
    assert "body" not in result["item"]
    assert result["route"]["selected"] == {"machine": "worker-linux-1", "seat": "seat-grok"}
    assert result["route"]["reason"] == "selected"
    assert result["route"]["candidates"]
    assert result["considered"] == []


def test_excluded_items_are_reported_as_buckets(conn):
    blocked = _seed(conn, "Blocked item", rank=1, blocker="waiting on vendor")
    eligible = _seed(conn, "Eligible item", rank=2)
    result = fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    assert result["work_id"] == eligible
    assert result["considered"] == [{"work_id": blocked, "bucket": "blocker"}]
    assert result["route"]["selected"] == {"machine": "worker-linux-1", "seat": "seat-grok"}


def test_source_policy_exclusion_is_respected(conn):
    work_id = _seed(conn, "Imported item", rank=1)
    conn.execute(
        "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, external_state, "
        "external_updated_at, source_policy, source_acceptance_json, synced_at, stale_at, adapter_id, owner_node) "
        "VALUES (?, ?, 'github', 'org/repo#1', NULL, NULL, NULL, '2026-09-05T11:00:00+00:00', 'closed', '[]', "
        "'2026-09-05T11:00:00+00:00', NULL, 'github', 'node-a')",
        (f"lnk-{work_id}", work_id),
    )
    conn.commit()
    eligible = _seed(conn, "Native item", rank=2)
    result = fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    assert result["work_id"] == eligible
    assert result["considered"] == [{"work_id": work_id, "bucket": "source-policy"}]


def test_unreadable_lookup_is_never_eligible(conn):
    _seed(conn, "Only item", rank=1)
    fleet_hub_routing.set_work_item_lookup(lambda _conn, _work_id: None)
    try:
        result = fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    finally:
        fleet_hub_routing.set_work_item_lookup(None)
    assert result["work_id"] is None
    assert result["item"] is None
    assert result["route"]["reason"] == "no-eligible-work"
    assert len(result["considered"]) == 1
    assert result["considered"][0]["bucket"] == "unreadable"


def test_empty_queue_reports_no_eligible_work(conn):
    result = fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    assert result["schema"] == fleet_hub_routing.WORK_NEXT_SCHEMA
    assert result["work_id"] is None
    assert result["item"] is None
    assert result["route"] == {"selected": None, "candidates": [], "reason": "no-eligible-work"}
    assert result["considered"] == []


def test_unenrolled_consumer_is_not_routed(conn):
    _seed(conn, "Only item", rank=1)
    result = fleet_hub_routing.work_next(conn, consumer="stranger", workload="general")
    assert result["work_id"] is not None
    assert result["route"]["reason"] == "enrollment-required"
    assert result["route"]["selected"] is None


def test_work_next_persists_nothing(conn):
    _seed(conn, "Only item", rank=1)
    before = _counts(conn)
    fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    fleet_hub_routing.preview_route_for_work(conn, consumer="brigade-run", workload="general")
    status, payload = fleet_hub_policy_api.handle_policy(
        conn, {"action": "work-next"}, caller_node=CONTROLLER, is_admin=False
    )
    assert status == 200
    assert payload["schema"] == fleet_hub_routing.WORK_NEXT_SCHEMA
    assert _counts(conn) == before


def test_policy_api_requires_a_node_token(conn):
    _seed(conn, "Only item", rank=1)
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as excinfo:
        fleet_hub_policy_api.handle_policy(conn, {"action": "work-next"}, caller_node=None, is_admin=False)
    assert excinfo.value.status == 403
    assert excinfo.value.code == "auth-failed"
    status, payload = fleet_hub_policy_api.handle_policy(
        conn,
        {"action": "work-next", "consumer": "brigade-run", "workload": "general"},
        caller_node=CONTROLLER,
        is_admin=False,
    )
    assert status == 200
    assert payload["work_id"] is not None
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as excinfo:
        fleet_hub_policy_api.handle_policy(
            conn, {"action": "work-next", "bogus": True}, caller_node=CONTROLLER, is_admin=False
        )
    assert excinfo.value.code == "invalid-request"


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


def _request(hub, method: str, path: str, *, token: str | None = None, body: dict | None = None):
    host, port, _db = hub
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(
        method, path, body=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers
    )
    response = connection.getresponse()
    raw = response.read().decode("utf-8")
    payload = json.loads(raw) if raw else {}
    connection.close()
    return response.status, payload


def test_http_auth_node_token_ok_no_token_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_hub_routing, "_now", _now)
    monkeypatch.setattr(fleet_quota, "_now", _now)
    with _hub(tmp_path) as hub:
        db = fleet_hub.open_db(hub[2])
        try:
            fleet_hub_policy.save_policy(db, _document(), expected_version=1, actor="operator", reason="test")
            _node, node_token = fleet_hub.add_node(db, CONTROLLER, "controller")
        finally:
            db.close()
        status, _payload = _request(hub, "POST", "/policy", body={"action": "work-next"})
        assert status in (401, 403)
        status, payload = _request(hub, "POST", "/policy", token=node_token, body={"action": "work-next"})
        assert status == 200, payload
        assert payload["schema"] == fleet_hub_routing.WORK_NEXT_SCHEMA
        assert payload["route"]["reason"] == "no-eligible-work"


def test_cli_json_reports_item_machine_seat_and_reason(monkeypatch, capsys):
    from brigade import cli

    payload = {
        "schema": fleet_hub_routing.WORK_NEXT_SCHEMA,
        "work_id": "wl-abc123",
        "item": {"work_id": "wl-abc123", "title": "Seeded item", "kind": "fleet"},
        "route": {
            "selected": {"machine": "worker-linux-1", "seat": "seat-grok"},
            "candidates": [],
            "reason": "selected",
        },
        "considered": [],
    }
    seen: dict = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return payload

    monkeypatch.setattr(fleet_client_policy, "work_next", _fake)
    assert cli.main(["fleet", "work", "next", "--json"]) == 0
    assert seen == {"consumer": "brigade-run", "workload": "general"}
    out = json.loads(capsys.readouterr().out)
    assert out["work_id"] == "wl-abc123"
    assert out["route"]["selected"] == {"machine": "worker-linux-1", "seat": "seat-grok"}


def test_cli_table_names_item_machine_seat_and_reason(monkeypatch, capsys):
    from brigade import cli

    monkeypatch.setattr(
        fleet_client_policy,
        "work_next",
        lambda **kwargs: {
            "schema": fleet_hub_routing.WORK_NEXT_SCHEMA,
            "work_id": "wl-abc123",
            "item": {"work_id": "wl-abc123", "title": "Seeded item"},
            "route": {
                "selected": {"machine": "worker-linux-1", "seat": "seat-grok"},
                "candidates": [],
                "reason": "selected",
            },
            "considered": [{"work_id": "wl-zzz", "bucket": "blocker"}],
        },
    )
    assert cli.main(["fleet", "work", "next"]) == 0
    out = capsys.readouterr().out
    assert "wl-abc123" in out
    assert "worker-linux-1" in out
    assert "seat-grok" in out
    assert "selected" in out


def test_cli_empty_queue_exits_nonzero(monkeypatch, capsys):
    from brigade import cli

    monkeypatch.setattr(
        fleet_client_policy,
        "work_next",
        lambda **kwargs: {
            "schema": fleet_hub_routing.WORK_NEXT_SCHEMA,
            "work_id": None,
            "item": None,
            "route": {"selected": None, "candidates": [], "reason": "no-eligible-work"},
            "considered": [],
        },
    )
    assert cli.main(["fleet", "work", "next"]) == 1
    assert "no eligible work" in capsys.readouterr().out


def test_snapshot_reports_work_next_integration(conn):
    assert fleet_hub_routing.snapshot(conn)["worklore_integration"] == "work-next"


def test_considered_length_is_bounded(conn, monkeypatch):
    for index in range(120):
        _seed(conn, f"Blocked {index:03d}", rank=index + 1, blocker="waiting")
    result = fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    assert result["work_id"] is None
    assert len(result["considered"]) == fleet_hub_routing.WORK_NEXT_CONSIDERED_MAX
    assert all(entry["bucket"] == "blocker" for entry in result["considered"])


def test_row_count_unchanged_after_work_next(conn):
    _seed(conn, "Only item", rank=1)
    tables = ("fleet_reservations", "fleet_route_decisions", "work_items", "work_events")
    before = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
    fleet_hub_routing.work_next(conn, consumer="brigade-run", workload="general")
    after = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
    assert after == before
