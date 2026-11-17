"""fleet place: read-only advisory placement over the routing evaluator and the node project inventory."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from io import StringIO

import pytest

from brigade import (
    cli,
    fleet_client_policy,
    fleet_hub,
    fleet_hub_placement,
    fleet_hub_policy,
    fleet_hub_policy_api,
    fleet_hub_routing,
    fleet_hub_sessions,
    fleet_node_projects,
    fleet_quota,
)
from tests.test_fleet_node_projects import REPO
from tests.test_fleet_routing import (
    CONTROLLER,
    NODE_CLOUD,
    NODE_LINUX,
    NODE_LINUX_2,
    _document,
    _fresh_quota,
    _iso,
    _now,
    _observe,
)

ENV = {NODE_LINUX: "env-linux-one", NODE_LINUX_2: "env-linux-two", NODE_CLOUD: "env-cloud-one"}
PROJECT = {NODE_LINUX: "proj-linux-one", NODE_LINUX_2: "proj-linux-two", NODE_CLOUD: "proj-cloud-one"}
MACHINE_BY_NODE = {NODE_LINUX: "worker-linux-1", NODE_LINUX_2: "worker-linux-2", NODE_CLOUD: "worker-cloud-1"}
BOUND = {"worker-linux-1": ENV[NODE_LINUX], "worker-linux-2": ENV[NODE_LINUX_2], "worker-cloud-1": ENV[NODE_CLOUD]}


def _save(conn, *, bindings: dict[str, str] | None = None, repo_machines: list[str] | None = None) -> None:
    document = _document()
    for machine, environment_id in (BOUND if bindings is None else bindings).items():
        document["machines"][machine]["t3_environment_id"] = environment_id
    if repo_machines is not None:
        document["repositories"][REPO] = {"eligible_machines": repo_machines}
    current = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.save_policy(
        conn, document, expected_version=current["revision"], actor="operator", reason="placement"
    )


def _report(conn, node: str, *, repo: str = REPO, offset: int = 0, ttl: int = 900) -> None:
    fleet_node_projects.observe_projects(
        conn,
        {
            "node_id": node,
            "environment_id": ENV[node],
            "observed_at": _iso(_now() + timedelta(seconds=offset)),
            "ttl_seconds": ttl,
            "projects": [{"project_id": PROJECT[node], "repo_key": repo}],
        },
        node,
    )


def _session(conn, node: str, session_id: str) -> None:
    fleet_hub_sessions.handle_session(
        conn,
        {
            "action": "upsert",
            "harness": "claude",
            "session_id": session_id,
            "repo_identity": "github.com/example/project",
            "identity_scope": "fleet",
            "repo_label": "project",
            "checkout_path": "/repos/project",
            "branch": "main",
            "dirty_paths": [],
            "dirty_truncated": False,
        },
        caller_node=node,
    )


def _place(conn, *, caller: str = NODE_LINUX, **body) -> dict:
    return fleet_hub_placement.place(conn, {"action": "place", "repo": REPO, **body}, caller)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_hub_routing, "_now", _now)
    monkeypatch.setattr(fleet_quota, "_now", _now)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: _now().timestamp())
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        fleet_hub_policy.save_policy(connection, _document(), expected_version=1, actor="operator", reason="routing")
        _save(connection)
        _fresh_quota(connection)
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def fleet(conn):
    """Three linux machines, each reporting the repository as a bound project at low CPU load."""
    for node in (NODE_LINUX, NODE_LINUX_2, NODE_CLOUD):
        _report(conn, node)
        _observe(conn, node, load=0.10)
    return conn


def _row_counts(conn) -> dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in names}


def _reasons(payload: dict, machine: str) -> list[str]:
    return next(row["reasons"] for row in payload["candidates"] if row["machine"] == machine)


def test_origin_under_threshold_stays_local(fleet):
    result = _place(fleet)
    assert result["schema"] == "brigade.fleet_placement.v1"
    assert result["status"] == "recommended" and result["advisory"] is True
    assert result["reason"] == "local-under-threshold" and result["origin_machine"] == "worker-linux-1"
    assert result["repo_key"] == REPO and result["threshold"] == 0.70
    assert result["trust"] == "node-reported telemetry and inventory; advisory only, never admission"
    chosen = result["recommended"]
    assert chosen["machine"] == "worker-linux-1" and chosen["node_id"] == NODE_LINUX
    assert chosen["environment_id"] == ENV[NODE_LINUX] and chosen["project_id"] == PROJECT[NODE_LINUX]
    assert chosen["effective_load"] == 0.1
    assert chosen["load_inputs"] == {"cpu": 0.1, "sessions": 0, "concurrency": 1}
    assert chosen["telemetry_observed_at"] and chosen["inventory_observed_at"]
    assert result["policy_version"] == fleet_hub_policy.current_policy(fleet)["revision"]
    assert result["policy_digest"].startswith("sha256:")
    text = json.dumps(result)
    assert "admitted" not in text and "reserved" not in text


def test_over_threshold_origin_sheds_to_a_peer_with_the_repo(fleet):
    _observe(fleet, NODE_LINUX, load=0.80, observed_at=_iso(_now() + timedelta(seconds=1)))
    result = _place(fleet)
    assert result["status"] == "recommended" and result["reason"] == "peer-ranked"
    assert result["recommended"]["machine"] == "worker-linux-2"
    assert "machine-load-shed" in _reasons(result, "worker-linux-1")


def test_load_at_the_threshold_is_shed(fleet):
    _observe(fleet, NODE_LINUX, load=0.70, observed_at=_iso(_now() + timedelta(seconds=1)))
    assert "machine-load-shed" in _reasons(_place(fleet), "worker-linux-1")
    assert _place(fleet, threshold=0.71)["recommended"]["machine"] == "worker-linux-1"


def test_session_floor_alone_can_push_a_machine_over_the_threshold(fleet):
    _session(fleet, NODE_LINUX, "live-session-one")
    result = _place(fleet)
    assert "machine-load-shed" in _reasons(result, "worker-linux-1")
    assert result["recommended"]["machine"] == "worker-linux-2"
    shed = next(row for row in result["candidates"] if row["machine"] == "worker-linux-1")
    assert shed["effective_load"] == 1.0
    # The cloud machine has concurrency 2, so one live session is a 0.5 floor, under the threshold.
    _session(fleet, NODE_CLOUD, "live-session-two")
    cloud = next(row for row in _place(fleet)["candidates"] if row["machine"] == "worker-cloud-1")
    assert cloud["effective_load"] == 0.5 and cloud["eligible"] is True


def test_effective_load_is_the_max_of_cpu_and_session_ratio(fleet):
    _observe(fleet, NODE_CLOUD, load=0.60, observed_at=_iso(_now() + timedelta(seconds=1)))
    _session(fleet, NODE_CLOUD, "live-session-one")
    cloud = next(row for row in _place(fleet)["candidates"] if row["machine"] == "worker-cloud-1")
    assert cloud["effective_load"] == 0.6


def test_ranking_among_peers_uses_existing_rank_then_lower_load(fleet):
    _observe(fleet, NODE_LINUX, load=0.90, observed_at=_iso(_now() + timedelta(seconds=1)))
    # worker-linux-2 outranks worker-cloud-1 by priority even though the cloud machine is less loaded.
    _observe(fleet, NODE_CLOUD, load=0.01, observed_at=_iso(_now() + timedelta(seconds=1)))
    assert _place(fleet)["recommended"]["machine"] == "worker-linux-2"


def test_caller_with_no_machine_gets_the_ranked_peer(fleet):
    result = _place(fleet, caller=CONTROLLER)
    assert result["origin_machine"] == "origin-windows-1" and result["reason"] == "peer-ranked"
    assert result["recommended"]["machine"] == "worker-linux-1"


def test_stale_missing_and_null_load_telemetry_are_excluded_with_named_reasons(conn):
    _report(conn, NODE_LINUX)
    _report(conn, NODE_LINUX_2)
    _report(conn, NODE_CLOUD)
    _observe(conn, NODE_LINUX, observed_at=_iso(_now() - timedelta(seconds=1000)))
    _observe(conn, NODE_LINUX_2, load=None)
    # No telemetry has ever been observed for the cloud node.
    result = _place(conn)
    assert result["status"] == "none" and result["reason"] == "no-eligible-candidate"
    assert "machine-stale-telemetry" in _reasons(result, "worker-linux-1")
    assert "machine-load-unknown" in _reasons(result, "worker-linux-2")
    assert "machine-unknown-telemetry" in _reasons(result, "worker-cloud-1")
    assert all(row["effective_load"] is None and not row["eligible"] for row in result["candidates"])


def test_unbound_and_stale_inventory_are_excluded_with_named_reasons(conn):
    # worker-linux-2 has no environment binding, so its report is stored but read as unbound.
    _save(conn, bindings={"worker-linux-1": ENV[NODE_LINUX], "worker-cloud-1": ENV[NODE_CLOUD]})
    _report(conn, NODE_LINUX)
    _report(conn, NODE_LINUX_2)
    _report(conn, NODE_CLOUD, offset=-2000, ttl=900)
    for node in (NODE_LINUX, NODE_LINUX_2, NODE_CLOUD):
        _observe(conn, node, load=0.10)
    result = _place(conn, caller=NODE_LINUX_2)
    assert "inventory-unbound" in _reasons(result, "worker-linux-2")
    assert "inventory-stale" in _reasons(result, "worker-cloud-1")
    assert result["recommended"]["machine"] == "worker-linux-1"
    assert result["origin_machine"] == "worker-linux-2" and result["reason"] == "peer-ranked"


def test_unbound_everywhere_and_unknown_repo_are_explicit_denials(conn):
    _save(conn, bindings={})
    for node in (NODE_LINUX, NODE_LINUX_2, NODE_CLOUD):
        _report(conn, node)
        _observe(conn, node, load=0.10)
    result = _place(conn)
    assert result["status"] == "none" and result["recommended"] is None
    assert result["reason"] == "repo-not-placeable"
    unknown = _place(conn, repo="example.test/org/never-seen")
    assert unknown["status"] == "none" and unknown["reason"] == "repo-not-placeable"
    assert unknown["recommended"] is None


def test_policy_repository_eligible_machines_is_an_extra_filter(fleet):
    _save(fleet, repo_machines=["worker-linux-2"])
    result = _place(fleet)
    assert result["recommended"]["machine"] == "worker-linux-2"
    assert "repo-machine-not-eligible" in _reasons(result, "worker-linux-1")


def test_expiry_is_the_earliest_of_telemetry_inventory_and_two_minutes(conn):
    for node in (NODE_LINUX, NODE_LINUX_2, NODE_CLOUD):
        _report(conn, node)
        _observe(conn, node, load=0.10)
    decided = _now()
    base = _place(conn)
    assert base["decided_at"] == decided.isoformat()
    assert base["expires_at"] == (decided + timedelta(seconds=120)).isoformat()
    _observe(conn, NODE_LINUX, load=0.10, ttl_seconds=30, observed_at=_iso(decided + timedelta(seconds=1)))
    telemetry = _place(conn)
    assert telemetry["expires_at"] == (decided + timedelta(seconds=31)).isoformat()
    _report(conn, NODE_LINUX, offset=2, ttl=10)
    inventory = _place(conn)
    assert inventory["expires_at"] == (decided + timedelta(seconds=12)).isoformat()


def test_output_carries_only_the_selected_machines_ids(fleet):
    result = _place(fleet)
    text = json.dumps(result)
    assert ENV[NODE_LINUX] in text and PROJECT[NODE_LINUX] in text
    for node in (NODE_LINUX_2, NODE_CLOUD):
        assert ENV[node] not in text and PROJECT[node] not in text
    assert all(set(row) == {"machine", "eligible", "effective_load", "reasons"} for row in result["candidates"])
    peer = _place(fleet, caller=CONTROLLER)
    peer_text = json.dumps(peer)
    assert ENV[NODE_LINUX_2] not in peer_text and ENV[NODE_CLOUD] not in peer_text


def test_placement_writes_nothing(fleet):
    _session(fleet, NODE_LINUX, "live-session-one")
    fleet.commit()
    before_counts, before_changes = _row_counts(fleet), fleet.total_changes
    before_tables = set(before_counts)
    result = _place(fleet)
    assert result["status"] == "recommended"
    assert _row_counts(fleet) == before_counts and fleet.total_changes == before_changes
    assert set(_row_counts(fleet)) == before_tables
    assert fleet.in_transaction is False
    assert fleet.execute("PRAGMA query_only").fetchone()[0] == 0
    # A refused write after placement still works, so query_only was restored.
    fleet.execute("CREATE TABLE IF NOT EXISTS scratch_after (x)")
    fleet.rollback()


def test_placement_creates_no_table_or_quota_singleton_on_a_db_that_lacks_them(fleet):
    fleet.execute("DELETE FROM fleet_quota_meta")
    fleet.execute("DROP TABLE fleet_node_project_snapshots")
    fleet.commit()
    before = _row_counts(fleet)
    assert _place(fleet)["status"] == "recommended"
    assert _row_counts(fleet) == before
    assert fleet.execute("SELECT COUNT(*) FROM fleet_quota_meta").fetchone()[0] == 0
    # With the quota store gone the evaluator reports the pool unknown and still creates nothing.
    fleet.execute("DROP TABLE fleet_quota_observations")
    fleet.execute("DROP TABLE fleet_quota_meta")
    fleet.commit()
    before = _row_counts(fleet)
    result = _place(fleet)
    assert result["status"] == "none" and "seat-quota-unknown" in _reasons(result, "worker-linux-1")
    assert _row_counts(fleet) == before
    # With no inventory table the repository is simply not placeable, and no table appears.
    fleet.execute("DROP TABLE fleet_node_projects")
    fleet.commit()
    before = _row_counts(fleet)
    result = _place(fleet)
    assert result["status"] == "none" and result["reason"] == "repo-not-placeable"
    assert _row_counts(fleet) == before
    assert not {"fleet_quota_meta", "fleet_quota_observations", "fleet_node_projects"} & set(_row_counts(fleet))


def test_missing_routing_tables_are_reported_not_created(conn):
    conn.execute("DROP TABLE fleet_machine_telemetry")
    conn.commit()
    before = _row_counts(conn)
    result = _place(conn)
    assert result["status"] == "none" and result["reason"] == "routing-not-initialized"
    assert _row_counts(conn) == before


def test_placement_works_on_a_read_only_connection(fleet, tmp_path):
    fleet.commit()
    readonly = sqlite3.connect(f"file:{tmp_path / 'fleet.db'}?mode=ro", uri=True)
    try:
        result = fleet_hub_placement.place(readonly, {"repo": REPO}, NODE_LINUX)
        assert result["status"] == "recommended" and result["recommended"]["machine"] == "worker-linux-1"
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("CREATE TABLE nope (x)")
    finally:
        readonly.close()


def test_default_evaluator_arguments_leave_candidates_unchanged(fleet):
    document = fleet_hub_policy.current_policy(fleet)["document"]
    routing = document["routing"]
    resolution = fleet_hub_policy_api.fleet_policy.resolve_policy(document, None, None)
    common = dict(
        document=document,
        routing=routing,
        workload="general",
        requirements=routing["workload_requirements"]["general"],
        resolution=resolution,
        now=_now(),
    )
    default = fleet_hub_routing._collect_candidates(fleet, **common)
    explicit = fleet_hub_routing._collect_candidates(fleet, **common, quota_read_only=False, extra_reasons=None)
    quiet = fleet_hub_routing._collect_candidates(fleet, **common, quota_read_only=True, extra_reasons=lambda *_: [])
    assert default == explicit == quiet
    assert default[2] is not None


def test_node_action_dispatch_is_read_only_and_needs_a_node_token(fleet):
    fleet.commit()
    before, changes = _row_counts(fleet), fleet.total_changes
    status, payload = fleet_hub_policy_api.handle_policy(
        fleet, {"action": "place", "repo": REPO}, caller_node=NODE_LINUX, is_admin=False
    )
    assert status == 200 and payload["recommended"]["machine"] == "worker-linux-1"
    assert _row_counts(fleet) == before and fleet.total_changes == changes and fleet.in_transaction is False
    for is_admin in (False, True):
        with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as refused:
            fleet_hub_policy_api.handle_policy(
                fleet, {"action": "place", "repo": REPO}, caller_node=None, is_admin=is_admin
            )
        assert refused.value.status == 403
    assert "place" in fleet_hub_policy_api.NODE_ACTIONS


@pytest.mark.parametrize(
    "body",
    [
        {"repo": ""},
        {"repo": "https://user:secret@example.test/org/repo"},
        {"repo": REPO, "threshold": 1.5},
        {"repo": REPO, "threshold": -0.1},
        {"repo": REPO, "threshold": True},
        {"repo": REPO, "extra": 1},
        {},
    ],
)
def test_invalid_requests_are_refused_without_writing(fleet, body):
    fleet.commit()
    before = _row_counts(fleet)
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as refused:
        fleet_hub_policy_api.handle_policy(fleet, {"action": "place", **body}, caller_node=NODE_LINUX, is_admin=False)
    assert refused.value.code == "invalid-request"
    assert _row_counts(fleet) == before


def test_routing_disabled_is_a_named_denial(conn):
    document = _document()
    document["routing"]["enabled"] = False
    fleet_hub_policy.save_policy(
        conn,
        document,
        expected_version=fleet_hub_policy.current_policy(conn)["revision"],
        actor="operator",
        reason="off",
    )
    result = _place(conn)
    assert result["status"] == "none" and result["reason"] == "routing-disabled"


def test_client_posts_the_place_action_with_a_node_token(monkeypatch):
    seen = {}

    def fake_post(body, *, admin):
        seen.update(body=body, admin=admin)
        return {"ok": True}

    monkeypatch.setattr(fleet_client_policy, "post_policy", fake_post)
    assert fleet_client_policy.place(repo=REPO, threshold=0.5) == {"ok": True}
    assert seen == {"body": {"action": "place", "repo": REPO, "threshold": 0.5}, "admin": False}
    fleet_client_policy.place(repo=REPO)
    assert seen["body"] == {"action": "place", "repo": REPO}


def _cli(monkeypatch, argv: list[str]) -> tuple[int, str]:
    out = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", StringIO())
    try:
        rc = cli.main(["fleet", "place", *argv])
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, out.getvalue()


def test_cli_prints_one_line_or_json_and_exits_nonzero_when_nothing_is_placed(monkeypatch):
    recommended = {
        "status": "recommended",
        "reason": "local-under-threshold",
        "recommended": {"machine": "worker-linux-1", "environment_id": "env-a", "project_id": "proj-a"},
    }
    calls = []
    monkeypatch.setattr(fleet_client_policy, "place", lambda **kwargs: calls.append(kwargs) or recommended)
    rc, out = _cli(monkeypatch, ["--repo", REPO])
    assert rc == 0 and calls == [{"repo": REPO, "threshold": 0.70}]
    assert out.strip() == (
        "place: worker-linux-1 environment=env-a project=proj-a reason=local-under-threshold (advisory)"
    )
    rc, out = _cli(monkeypatch, ["--repo", REPO, "--threshold", "0.4", "--json"])
    assert rc == 0 and json.loads(out) == recommended and calls[-1]["threshold"] == 0.4
    monkeypatch.setattr(
        fleet_client_policy,
        "place",
        lambda **kwargs: {"status": "none", "reason": "repo-not-placeable", "recommended": None},
    )
    rc, out = _cli(monkeypatch, ["--repo", REPO])
    assert rc == 1 and out.strip() == "place: none reason=repo-not-placeable (advisory)"
    rc, _ = _cli(monkeypatch, ["--repo", REPO, "--threshold", "2"])
    assert rc != 0
