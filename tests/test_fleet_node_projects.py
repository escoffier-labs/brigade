"""Hub store for each node's T3 project inventory, keyed by repository identity."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_hub_policy_api, fleet_node_projects, fleet_policy
from brigade.fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden
from tests.test_fleet_routing import NODE_LINUX, NODE_LINUX_2, _document

ENV_ONE = "env-aaaa-1111"
ENV_TWO = "env-bbbb-2222"
REPO = "example.test/org/repo"


def _stamp(offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def _body(node_id: str = NODE_LINUX, *, environment_id: str = ENV_ONE, observed_at: str | None = None, projects=None):
    return {
        "node_id": node_id,
        "environment_id": environment_id,
        "observed_at": observed_at or _stamp(),
        "ttl_seconds": 900,
        "projects": [{"project_id": "p1", "repo_key": REPO}] if projects is None else projects,
    }


def _bind(conn, **bindings: str) -> None:
    """Save a policy revision binding machine names to T3 environment ids."""
    document = _document()
    for machine, environment_id in bindings.items():
        document["machines"][machine.replace("_", "-")]["t3_environment_id"] = environment_id
    current = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.save_policy(
        conn, document, expected_version=current["revision"], actor="operator", reason="bind environments"
    )


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        fleet_hub_policy.save_policy(connection, _document(), expected_version=1, actor="operator", reason="routing")
        yield connection
    finally:
        connection.close()


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _snapshots(conn):
    return conn.execute("SELECT node_id, environment_id FROM fleet_node_project_snapshots").fetchall()


def _stored(conn):
    return conn.execute(
        "SELECT node_id, environment_id, project_id, repo_key, bound FROM fleet_node_projects ORDER BY project_id"
    ).fetchall()


def test_observe_stores_normalized_rows_and_reads_them_back(conn):
    _bind(conn, worker_linux_1=ENV_ONE)
    result = fleet_node_projects.observe_projects(
        conn, _body(projects=[{"project_id": "p1", "repo_key": "Example.TEST/Org/Repo.git"}]), NODE_LINUX
    )
    assert result["ignored"] is False and result["projects"] == 1 and result["bound"] is True
    rows = fleet_node_projects.projects_for_repo(conn, REPO)
    assert [(r["machine"], r["environment_id"], r["project_id"], r["repo_key"]) for r in rows] == [
        ("worker-linux-1", ENV_ONE, "p1", REPO)
    ]
    assert rows[0]["bound"] is True and rows[0]["stale"] is False and rows[0]["expires_at"]
    assert fleet_node_projects.projects_for_repo(conn, "Example.test/ORG/repo.git") == rows
    assert fleet_node_projects.projects_for_repo(conn, "example.test/org/other") == []


def test_node_cannot_write_another_nodes_inventory(conn):
    with pytest.raises(FleetHubForbidden):
        fleet_node_projects.observe_projects(conn, _body(NODE_LINUX_2), NODE_LINUX)
    assert "fleet_node_projects" not in _tables(conn)
    # The same rule holds through the HTTP dispatch for a node token.
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as raised:
        fleet_hub_policy_api.handle_policy(
            conn, {"action": "projects-observe", **_body(NODE_LINUX_2)}, caller_node=NODE_LINUX, is_admin=False
        )
    assert raised.value.status == 403
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as anonymous:
        fleet_hub_policy_api.handle_policy(
            conn, {"action": "projects-observe", **_body()}, caller_node=None, is_admin=True
        )
    assert anonymous.value.status == 403


def test_projects_observe_dispatches_for_the_calling_node(conn):
    status, payload = fleet_hub_policy_api.handle_policy(
        conn, {"action": "projects-observe", **_body()}, caller_node=NODE_LINUX, is_admin=False
    )
    assert status == 200 and payload["machine"] == "worker-linux-1" and payload["projects"] == 1


def test_unmapped_node_is_rejected(conn):
    with pytest.raises(FleetHubError, match="not a configured machine"):
        fleet_node_projects.observe_projects(
            conn, _body("99999999-9999-4999-8999-999999999999"), "99999999-9999-4999-8999-999999999999"
        )


def test_environment_binding_mismatch_is_forbidden_and_stores_nothing(conn):
    _bind(conn, worker_linux_1=ENV_ONE)
    with pytest.raises(FleetHubForbidden, match="environment-binding-mismatch"):
        fleet_node_projects.observe_projects(conn, _body(environment_id=ENV_TWO), NODE_LINUX)
    assert _stored(conn) == [] and _snapshots(conn) == []
    # A matching report after the rejected one stores normally.
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    assert [row[4] for row in _stored(conn)] == [1]


def test_binding_mismatch_clears_only_that_nodes_stored_rows_and_persists(conn):
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    fleet_node_projects.observe_projects(conn, _body(NODE_LINUX_2, environment_id=ENV_TWO), NODE_LINUX_2)
    _bind(conn, worker_linux_1=ENV_TWO, worker_linux_2=ENV_TWO.replace("bbbb", "cccc"))
    # Rebinding after the report makes the next honest report a mismatch for the first node.
    with pytest.raises(FleetHubForbidden, match="environment-binding-mismatch"):
        fleet_node_projects.observe_projects(conn, _body(environment_id=ENV_ONE, observed_at=_stamp(5)), NODE_LINUX)
    assert [row[0] for row in _stored(conn)] == [NODE_LINUX_2]
    assert [row[0] for row in _snapshots(conn)] == [NODE_LINUX_2]
    assert [r["node_id"] for r in fleet_node_projects.projects_for_repo(conn, REPO)] == [NODE_LINUX_2]
    assert not conn.in_transaction


def test_binding_mismatch_clear_survives_the_policy_handlers_rollback(conn):
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    conn.commit()
    _bind(conn, worker_linux_1=ENV_TWO)
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as raised:
        fleet_hub_policy_api.handle_policy(
            conn,
            {"action": "projects-observe", **_body(environment_id=ENV_ONE, observed_at=_stamp(5))},
            caller_node=NODE_LINUX,
            is_admin=False,
        )
    assert raised.value.status == 403 and "environment-binding-mismatch" in str(raised.value)
    assert _stored(conn) == [] and _snapshots(conn) == []


def test_binding_check_runs_inside_the_write_transaction(conn, monkeypatch):
    seen = []
    real = fleet_hub_policy.current_policy

    def spy(connection):
        seen.append(connection.in_transaction)
        return real(connection)

    monkeypatch.setattr(fleet_hub_policy, "current_policy", spy)
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    assert seen == [True]


def test_unbound_machine_is_stored_but_read_as_unbound(conn):
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    assert [row[4] for row in _stored(conn)] == [0]
    rows = fleet_node_projects.projects_for_repo(conn, REPO)
    assert [r["bound"] for r in rows] == [False]


def test_policy_rebinding_changes_the_read_without_a_republish(conn):
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    assert [r["bound"] for r in fleet_node_projects.projects_for_repo(conn, REPO)] == [False]
    _bind(conn, worker_linux_1=ENV_ONE)
    assert [r["bound"] for r in fleet_node_projects.projects_for_repo(conn, REPO)] == [True]
    _bind(conn, worker_linux_1=ENV_TWO)
    assert [r["bound"] for r in fleet_node_projects.projects_for_repo(conn, REPO)] == [False]
    _bind(conn)
    assert [r["bound"] for r in fleet_node_projects.projects_for_repo(conn, REPO)] == [False]


def test_duplicate_environment_ids_are_rejected_at_policy_save(conn):
    document = _document()
    document["machines"]["worker-linux-1"]["t3_environment_id"] = ENV_ONE
    document["machines"]["worker-linux-2"]["t3_environment_id"] = ENV_ONE
    with pytest.raises(fleet_policy.FleetPolicyError, match="t3_environment_id"):
        fleet_policy.parse_document(document)
    with pytest.raises(FleetHubError):
        fleet_hub_policy.save_policy(conn, document, expected_version=2, actor="operator", reason="duplicate")


@pytest.mark.parametrize("value", ["", "has space", "under_score", "slash/ed", "-leading", "x" * 129, 7])
def test_environment_id_shape_is_enforced_in_policy(value):
    document = _document()
    document["machines"]["worker-linux-1"]["t3_environment_id"] = value
    with pytest.raises(fleet_policy.FleetPolicyError):
        fleet_policy.parse_document(document)


def test_unbound_machine_policy_keeps_its_digest():
    parsed = fleet_policy.parse_document(_document())
    assert "t3_environment_id" not in parsed["machines"]["worker-linux-1"]


def test_older_snapshot_never_overwrites_a_fresher_one(conn):
    newer = _stamp(-10)
    older = _stamp(-60)
    fleet_node_projects.observe_projects(conn, _body(observed_at=newer), NODE_LINUX)
    ignored = fleet_node_projects.observe_projects(
        conn,
        _body(observed_at=older, projects=[{"project_id": "other", "repo_key": "example.test/org/other"}]),
        NODE_LINUX,
    )
    assert ignored["ignored"] is True and ignored["observed_at"] == newer
    assert [row[2] for row in _stored(conn)] == ["p1"]
    # An empty fresher snapshot clears, and an older full one still cannot resurrect rows.
    fleet_node_projects.observe_projects(conn, _body(observed_at=_stamp(), projects=[]), NODE_LINUX)
    assert _stored(conn) == []
    fleet_node_projects.observe_projects(conn, _body(observed_at=older), NODE_LINUX)
    assert _stored(conn) == []


def test_same_instant_snapshots_that_disagree_conflict(conn):
    instant = _stamp(-5)
    fleet_node_projects.observe_projects(conn, _body(observed_at=instant), NODE_LINUX)
    fleet_node_projects.observe_projects(conn, _body(observed_at=instant), NODE_LINUX)
    with pytest.raises(FleetHubConflict, match="projects-conflict"):
        fleet_node_projects.observe_projects(
            conn, _body(observed_at=instant, projects=[{"project_id": "p2", "repo_key": REPO}]), NODE_LINUX
        )
    with pytest.raises(FleetHubConflict):
        fleet_node_projects.observe_projects(conn, _body(observed_at=instant, environment_id=ENV_TWO), NODE_LINUX)
    assert [row[2] for row in _stored(conn)] == ["p1"]


def test_newer_snapshot_replaces_the_whole_set(conn):
    fleet_node_projects.observe_projects(
        conn,
        _body(
            observed_at=_stamp(-30),
            projects=[
                {"project_id": "p1", "repo_key": REPO},
                {"project_id": "p2", "repo_key": "example.test/org/other"},
            ],
        ),
        NODE_LINUX,
    )
    fleet_node_projects.observe_projects(conn, _body(observed_at=_stamp()), NODE_LINUX)
    assert [row[2] for row in _stored(conn)] == ["p1"]


def test_nodes_do_not_touch_each_others_rows(conn):
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    fleet_node_projects.observe_projects(conn, _body(NODE_LINUX_2, environment_id=ENV_TWO), NODE_LINUX_2)
    rows = fleet_node_projects.projects_for_repo(conn, REPO)
    assert [r["node_id"] for r in rows] == sorted([NODE_LINUX, NODE_LINUX_2])
    fleet_node_projects.observe_projects(conn, _body(observed_at=_stamp(3), projects=[]), NODE_LINUX)
    assert [r["node_id"] for r in fleet_node_projects.projects_for_repo(conn, REPO)] == [NODE_LINUX_2]


@pytest.mark.parametrize(
    "patch",
    [
        {"observed_at": "not-a-time"},
        {"observed_at": "2026-01-01T00:00:00"},
        {"observed_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        {"ttl_seconds": 0},
        {"ttl_seconds": 86401},
        {"ttl_seconds": True},
        {"ttl_seconds": None},
        {"environment_id": ""},
        {"environment_id": "x" * 257},
        {"environment_id": "bad\nid"},
        {"projects": "nope"},
        {"projects": [{"project_id": "", "repo_key": REPO}]},
        {"projects": [{"project_id": "p", "repo_key": "https://token@example.test/org/repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "bad\nkey"}]},
        {"projects": [{"project_id": "p", "repo_key": ".git"}]},
        {"projects": [{"project_id": "p", "repo_key": "/srv/projects/client"}]},
        {"projects": [{"project_id": "p", "repo_key": "My board"}]},
        {"projects": [{"project_id": "p", "repo_key": "~/work/client"}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test\\org\\repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test/org/../repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test/org/./repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test/repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "intranet/org/repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "deploy@host.invalid/org/repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "ssh://example.test/org/repo"}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test/org/re po"}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test/org/repo\n"}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test/org/repo/"}]},
        {"projects": [{"project_id": "p", "repo_key": 7}]},
        {"projects": [{"project_id": "p" * 513, "repo_key": REPO}]},
        {"projects": [{"project_id": "has space", "repo_key": REPO}]},
        {"projects": [{"project_id": "p", "repo_key": "example.test/" + "a" * 513}]},
        {"projects": [{"project_id": "p", "repo_key": REPO}, {"project_id": "p", "repo_key": REPO}]},
        {
            "projects": [
                {"project_id": "p", "repo_key": REPO},
                {"project_id": "p", "repo_key": "example.test/org/other"},
            ]
        },
        {"projects": [{"project_id": f"p{i}", "repo_key": REPO} for i in range(fleet_node_projects.MAX_PROJECTS + 1)]},
    ],
)
def test_invalid_reports_are_rejected_and_store_nothing(conn, patch):
    with pytest.raises(FleetHubError):
        fleet_node_projects.observe_projects(conn, {**_body(), **patch}, NODE_LINUX)
    assert "fleet_node_projects" not in _tables(conn)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.TEST/Org/Repo.git", "example.test/org/repo"),
        ("example.test/org/repo", "example.test/org/repo"),
        ("git.example.test/group/sub/repo.name_1", "git.example.test/group/sub/repo.name_1"),
        ("example.test/org/.github", "example.test/org/.github"),
        ("example.test/org/repo.git.git", "example.test/org/repo.git"),
    ],
)
def test_canonical_repo_keys_normalize(raw, expected):
    assert fleet_node_projects.normalize_repo_key(raw) == expected


def test_project_id_and_repo_key_share_the_512_limit(conn):
    assert fleet_node_projects.MAX_ID_CHARS == 512
    key = "example.test/org/" + "a" * (512 - len("example.test/org/"))
    result = fleet_node_projects.observe_projects(
        conn, _body(projects=[{"project_id": "p" * 512, "repo_key": key}]), NODE_LINUX
    )
    assert result["projects"] == 1


def test_projects_at_the_cap_are_accepted(conn):
    projects = [{"project_id": f"p{i}", "repo_key": REPO} for i in range(fleet_node_projects.MAX_PROJECTS)]
    result = fleet_node_projects.observe_projects(conn, _body(projects=projects), NODE_LINUX)
    assert result["projects"] == fleet_node_projects.MAX_PROJECTS


def test_expired_rows_are_marked_stale(conn):
    fleet_node_projects.observe_projects(conn, _body(observed_at=_stamp(-2000)), NODE_LINUX)
    assert [r["stale"] for r in fleet_node_projects.projects_for_repo(conn, REPO)] == [True]
    now = datetime.now(timezone.utc) - timedelta(seconds=2000)
    assert [r["stale"] for r in fleet_node_projects.projects_for_repo(conn, REPO, now)] == [False]


def test_projects_for_repo_without_the_table_returns_empty_and_creates_nothing(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "bare.db")
    try:
        before = _tables(connection)
        assert fleet_node_projects.projects_for_repo(connection, REPO) == []
        assert _tables(connection) == before and "fleet_node_projects" not in before
    finally:
        connection.close()


def test_projects_for_repo_performs_no_writes(conn):
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    conn.commit()
    before_changes = conn.total_changes
    before_rows = _stored(conn)
    before_tables = _tables(conn)
    for key in (REPO, "example.test/missing/x", "", "bad\nkey"):
        fleet_node_projects.projects_for_repo(conn, key)
    assert conn.total_changes == before_changes
    assert _stored(conn) == before_rows and _tables(conn) == before_tables
    assert not conn.in_transaction


def test_inventory_columns_hold_no_paths_titles_or_urls(conn):
    fleet_node_projects.observe_projects(conn, _body(), NODE_LINUX)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(fleet_node_projects)")]
    assert columns == [
        "node_id",
        "environment_id",
        "project_id",
        "repo_key",
        "observed_at",
        "ttl_seconds",
        "updated_at",
        "bound",
    ]
    assert "/srv" not in json.dumps(_stored(conn))
