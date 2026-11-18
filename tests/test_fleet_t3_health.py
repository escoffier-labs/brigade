"""Hub record of native T3 observer health: unknown is never zero (#2166 slice 6)."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from brigade import cli, fleet_client, fleet_hub, fleet_hub_policy, fleet_hub_policy_api, fleet_t3_health
from brigade.fleet_hub import FleetHubError, FleetHubForbidden
from tests.test_fleet_routing import NODE_LINUX, NODE_LINUX_2, _document

MOMENT = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
# The optional table as it was before node memory reporting (#2710). engines/hub/schema/lockstep.py reads
# this literal to keep the old shape a reviewed variant, so a live hub that still has it stays ready.
PRE_MEMORY_OBSERVATIONS = """CREATE TABLE fleet_t3_observations (
    node_id TEXT NOT NULL PRIMARY KEY,
    last_success_at TEXT,
    last_attempt_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);"""
SESSION_TTL = 300  # fleet_openclaw_observer.TTL_SECONDS


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_t3_health, "_now", lambda: MOMENT)
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        fleet_hub_policy.save_policy(connection, _document(), expected_version=1, actor="operator", reason="routing")
        yield connection
    finally:
        connection.close()


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(conn):
    return conn.execute(
        "SELECT node_id, last_success_at, last_attempt_at FROM fleet_t3_observations ORDER BY node_id"
    ).fetchall()


def _seed(conn, node: str, success_age: float | None, attempt_age: float = 0) -> None:
    """A health row with a success ``success_age`` seconds before MOMENT (None: never succeeded)."""
    fleet_t3_health.ensure_schema(conn)
    success = None if success_age is None else (MOMENT - timedelta(seconds=success_age)).isoformat()
    attempt = (MOMENT - timedelta(seconds=attempt_age)).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO fleet_t3_observations (node_id, last_success_at, last_attempt_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (node, success, attempt, attempt),
    )
    conn.commit()


def test_success_is_stamped_with_the_hub_clock_and_failure_keeps_it(conn, monkeypatch):
    result = fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True}, NODE_LINUX)
    assert result["ok"] is True and result["machine"] == "worker-linux-1"
    assert _rows(conn) == [(NODE_LINUX, MOMENT.isoformat(), MOMENT.isoformat())]
    later = MOMENT + timedelta(seconds=120)
    monkeypatch.setattr(fleet_t3_health, "_now", lambda: later)
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": False}, NODE_LINUX)
    # The attempt moves, the last success does not: a failure never looks like current presence.
    assert _rows(conn) == [(NODE_LINUX, MOMENT.isoformat(), later.isoformat())]


def test_failure_first_leaves_no_success_and_reads_stale(conn):
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": False}, NODE_LINUX)
    assert _rows(conn)[0][1] is None
    assert fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX}


@pytest.mark.parametrize("field", ["observed_at", "last_success_at", "timestamp", "ttl_seconds"])
def test_a_node_supplied_time_is_refused_and_never_decides_freshness(conn, field):
    with pytest.raises(FleetHubError, match="unknown field"):
        fleet_t3_health.observe_t3(
            conn, {"node_id": NODE_LINUX, "ok": True, field: (MOMENT + timedelta(days=9)).isoformat()}, NODE_LINUX
        )
    assert "fleet_t3_observations" not in _tables(conn)


@pytest.mark.parametrize("ok", [None, 1, "true", [True]])
def test_ok_must_be_a_real_boolean(conn, ok):
    with pytest.raises(FleetHubError, match="must be a boolean"):
        fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": ok}, NODE_LINUX)


def test_a_non_object_body_and_an_unmapped_node_are_rejected(conn):
    with pytest.raises(FleetHubError, match="JSON object"):
        fleet_t3_health.observe_t3(conn, [1], NODE_LINUX)
    ghost = "99999999-9999-4999-8999-999999999999"
    with pytest.raises(FleetHubError, match="not a configured machine"):
        fleet_t3_health.observe_t3(conn, {"node_id": ghost, "ok": True}, ghost)
    assert "fleet_t3_observations" not in _tables(conn)


def test_a_node_cannot_write_another_nodes_health_row(conn):
    with pytest.raises(FleetHubForbidden):
        fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX_2, "ok": True}, NODE_LINUX)
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as raised:
        fleet_hub_policy_api.handle_policy(
            conn, {"action": "t3-observe", "node_id": NODE_LINUX_2, "ok": True}, caller_node=NODE_LINUX, is_admin=False
        )
    assert raised.value.status == 403
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as anonymous:
        fleet_hub_policy_api.handle_policy(
            conn, {"action": "t3-observe", "node_id": NODE_LINUX, "ok": True}, caller_node=None, is_admin=True
        )
    assert anonymous.value.status == 403
    assert "fleet_t3_observations" not in _tables(conn)


def test_the_node_action_dispatches_for_the_calling_node(conn):
    status, payload = fleet_hub_policy_api.handle_policy(
        conn, {"action": "t3-observe", "node_id": NODE_LINUX, "ok": True}, caller_node=NODE_LINUX, is_admin=False
    )
    assert status == 200 and payload["machine"] == "worker-linux-1" and payload["ok"] is True
    assert [row[0] for row in _rows(conn)] == [NODE_LINUX]


def test_the_client_wrapper_posts_the_t3_observe_action(monkeypatch):
    from brigade import fleet_client_policy

    calls = []
    monkeypatch.setattr(
        fleet_client_policy, "post_policy", lambda payload, *, admin: calls.append((dict(payload), admin)) or {}
    )
    fleet_client_policy.observe_t3({"node_id": NODE_LINUX, "ok": True})
    assert calls == [({"node_id": NODE_LINUX, "ok": True, "action": "t3-observe"}, False)]


def test_stale_threshold_equals_the_session_ttl(conn):
    assert fleet_t3_health.STALE_AFTER_SECONDS == SESSION_TTL
    from brigade import fleet_openclaw_observer

    assert fleet_openclaw_observer.TTL_SECONDS == SESSION_TTL


def test_two_missed_cycles_stay_known_and_the_third_becomes_unknown(conn):
    cycle = 120
    _seed(conn, NODE_LINUX, 2 * cycle)  # two missed 120 s cycles (age 240 s): the 300 s session rows are still live
    _seed(conn, NODE_LINUX_2, 3 * cycle)  # third missed cycle (age 360 s): session rows may have expired
    assert fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX_2}
    assert fleet_t3_health.fresh_nodes(conn, MOMENT) == {NODE_LINUX}
    assert fleet_t3_health.stale_nodes(conn, MOMENT + timedelta(seconds=59)) == {NODE_LINUX_2}
    assert fleet_t3_health.stale_nodes(conn, MOMENT + timedelta(seconds=60)) == {NODE_LINUX, NODE_LINUX_2}


def test_fresh_and_stale_never_overlap_and_a_node_without_a_row_is_in_neither(conn):
    _seed(conn, NODE_LINUX, 5)
    _seed(conn, NODE_LINUX_2, 9999)
    assert fleet_t3_health.fresh_nodes(conn, MOMENT) == {NODE_LINUX}
    assert fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX_2}
    assert fleet_t3_health.classify_nodes(conn, MOMENT) == ({NODE_LINUX}, {NODE_LINUX_2})


def test_fresh_nodes_is_empty_without_a_table_and_none_on_a_read_failure(conn):
    assert fleet_t3_health.fresh_nodes(conn, MOMENT) == set()
    assert "fleet_t3_observations" not in _tables(conn)

    class Broken:
        def execute(self, *_args):
            raise sqlite3.DatabaseError("malformed")

    assert fleet_t3_health.fresh_nodes(Broken(), MOMENT) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(("age", "stale"), [(0, False), (299, False), (299.9, False), (300, True), (3600, True)])
def test_the_boundary_is_fail_closed_at_exactly_the_ttl(conn, age, stale):
    _seed(conn, NODE_LINUX, age)
    assert (fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX}) is stale


def test_a_node_with_no_row_is_never_listed(conn):
    _seed(conn, NODE_LINUX_2, 10_000)
    assert fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX_2}


def test_a_missing_table_is_an_empty_set_and_is_not_created(conn):
    before = _tables(conn)
    assert fleet_t3_health.stale_nodes(conn, MOMENT) == set()
    assert fleet_t3_health.unknown_view(conn, MOMENT) == {"nodes_unknown": []}
    assert _tables(conn) == before and "fleet_t3_observations" not in before


def test_stale_nodes_performs_no_writes(conn):
    _seed(conn, NODE_LINUX, 9999)
    before_changes, before_rows = conn.total_changes, _rows(conn)
    conn.execute("PRAGMA query_only=ON")
    try:
        assert fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX}
    finally:
        conn.execute("PRAGMA query_only=OFF")
    assert conn.total_changes == before_changes and _rows(conn) == before_rows and not conn.in_transaction


def test_a_read_failure_is_none_so_callers_treat_everything_as_unknown(conn):
    class Broken:
        def execute(self, *_args):
            raise sqlite3.DatabaseError("database disk image is malformed")

    assert fleet_t3_health.stale_nodes(Broken(), MOMENT) is None  # type: ignore[arg-type]
    assert fleet_t3_health.unknown_view(Broken(), MOMENT) == {  # type: ignore[arg-type]
        "nodes_unknown": [],
        "nodes_unknown_error": True,
    }

    class Locked:
        def execute(self, *_args):
            raise sqlite3.OperationalError("database is locked")

    assert fleet_t3_health.stale_nodes(Locked(), MOMENT) is None  # type: ignore[arg-type]


def test_an_unparseable_stored_stamp_is_unknown_not_known(conn):
    fleet_t3_health.ensure_schema(conn)
    conn.execute(
        "INSERT INTO fleet_t3_observations (node_id, last_success_at, last_attempt_at, updated_at) VALUES (?, ?, ?, ?)",
        (NODE_LINUX, "garbage", "garbage", "garbage"),
    )
    conn.commit()
    assert fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX}


def test_the_table_is_optional_and_created_on_first_write_only(tmp_path):
    fresh = fleet_hub.init_db(tmp_path / "fresh.db")
    try:
        assert "fleet_t3_observations" not in _tables(fresh)
    finally:
        fresh.close()


# --- /sessions view and the CLI footer -------------------------------------------------------------------------


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_t3_health, "_now", lambda: MOMENT)
    db = tmp_path / "hub" / "fleet.db"
    token = "test-token-12345"
    server = fleet_hub.make_server("127.0.0.1", 0, db, token, allow_admin_writes=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", token, db
    server.shutdown()
    server.server_close()


def _get_sessions(url: str, token: str) -> dict:
    request = urllib.request.Request(url + "/sessions", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _seed_db(db, node: str, success_age: float | None) -> None:
    connection = sqlite3.connect(db)
    try:
        fleet_t3_health.ensure_schema(connection)
        # The HTTP handler reads the real clock, so seed relative to now rather than to MOMENT.
        now = datetime.now(timezone.utc)
        success = None if success_age is None else (now - timedelta(seconds=success_age)).isoformat()
        connection.execute(
            "INSERT OR REPLACE INTO fleet_t3_observations (node_id, last_success_at, last_attempt_at, updated_at) VALUES (?, ?, ?, ?)",
            (node, success, now.isoformat(), now.isoformat()),
        )
        connection.commit()
    finally:
        connection.close()


def test_sessions_payload_without_a_health_row_is_unchanged(hub):
    url, token, _db = hub
    payload = _get_sessions(url, token)
    assert payload == {"sessions": [], "nodes_unknown": []}


def test_sessions_payload_names_stale_nodes_and_keeps_the_rows_field(hub):
    url, token, db = hub
    _get_sessions(url, token)  # opens the schema
    _seed_db(db, NODE_LINUX, 9999)
    _seed_db(db, NODE_LINUX_2, 5)
    payload = _get_sessions(url, token)
    assert payload["nodes_unknown"] == [NODE_LINUX] and payload["sessions"] == []
    assert "nodes_unknown_error" not in payload


def test_client_fills_the_unknown_nodes_out_param(hub, monkeypatch):
    url, token, db = hub
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda **_: {"hub_url": url, "token": token})
    _get_sessions(url, token)
    _seed_db(db, NODE_LINUX, None)
    extras: dict = {}
    assert fleet_client.fetch_sessions(extras=extras) == []
    assert extras == {"nodes_unknown": [NODE_LINUX]}
    assert fleet_client.fetch_sessions() == []


def _run_cli(monkeypatch, capsys, unknown, *argv):
    monkeypatch.setattr(fleet_client, "fetch_sessions", lambda *, include_all, extras: extras.update(unknown) or [])
    assert cli.main(["fleet", "sessions", *argv]) == 0
    return capsys.readouterr().out


def test_cli_footer_names_machines_with_unknown_t3_sessions(monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, {"nodes_unknown": [NODE_LINUX, NODE_LINUX_2]})
    assert "(no active fleet sessions)" in out
    assert f"T3 sessions unknown for: {NODE_LINUX}, {NODE_LINUX_2}" in out
    assert "not zero" in out


def test_cli_footer_is_absent_when_every_node_is_known(monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, {})
    assert "unknown" not in out and "(no active fleet sessions)" in out


def test_cli_footer_reports_an_unreadable_health_table(monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, {"nodes_unknown": [], "nodes_unknown_error": True})
    assert "T3 sessions unknown: the hub could not read T3 observer health" in out


def test_cli_json_stdout_is_unchanged_and_unknown_nodes_warn_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(
        fleet_client,
        "fetch_sessions",
        lambda *, include_all, extras: extras.update({"nodes_unknown": [NODE_LINUX]}) or [],
    )
    assert cli.main(["fleet", "sessions", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [] and captured.out == "[]\n"
    assert captured.err.count("\n") == 1
    assert captured.err.startswith("warning: T3 sessions unknown for: " + NODE_LINUX)


def test_cli_json_warns_on_stderr_when_the_health_read_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        fleet_client,
        "fetch_sessions",
        lambda *, include_all, extras: extras.update({"nodes_unknown": [], "nodes_unknown_error": True}) or [],
    )
    assert cli.main(["fleet", "sessions", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n" and "could not read T3 observer health" in captured.err


def test_cli_json_is_silent_on_stderr_when_every_node_is_known(monkeypatch, capsys):
    monkeypatch.setattr(fleet_client, "fetch_sessions", lambda *, include_all, extras: [])
    assert cli.main(["fleet", "sessions", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n" and captured.err == ""


@pytest.mark.parametrize("stored", [5, 5.5, b"2026-09-05T12:00:00+00:00", "", "not a date"])
def test_a_non_text_or_unparseable_stored_value_is_unknown_not_a_crash(conn, stored):
    fleet_t3_health.ensure_schema(conn)
    conn.execute(
        "INSERT INTO fleet_t3_observations (node_id, last_success_at, last_attempt_at, updated_at) VALUES (?, ?, ?, ?)",
        (NODE_LINUX, stored, "x", "x"),
    )
    conn.commit()
    assert fleet_t3_health.classify_nodes(conn, MOMENT) == (set(), {NODE_LINUX})
    assert fleet_t3_health.stale_nodes(conn, MOMENT) == {NODE_LINUX}


def test_sessions_rows_and_unknown_nodes_come_from_one_read_snapshot(hub, monkeypatch):
    from brigade import fleet_hub_sessions

    url, token, db = hub
    _get_sessions(url, token)  # opens the schema
    _seed_db(db, NODE_LINUX_2, 5)  # creates the health table
    seen: dict[str, object] = {}
    original = fleet_hub_sessions.list_sessions

    def spy(conn, **kwargs):
        rows = original(conn, **kwargs)
        seen["in_transaction"] = conn.in_transaction
        seen["query_only"] = conn.execute("PRAGMA query_only").fetchone()[0]
        try:
            conn.execute("CREATE TABLE scratch_write (x)")
        except sqlite3.OperationalError:
            seen["write_refused"] = True
        # A write that commits after the rows were read must not show up in the same response.
        _seed_db(db, NODE_LINUX, 9999)
        return rows

    monkeypatch.setattr(fleet_hub_sessions, "list_sessions", spy)
    payload = _get_sessions(url, token)
    assert seen == {"in_transaction": True, "query_only": 1, "write_refused": True}
    assert payload["nodes_unknown"] == []
    # The next request sees it.
    monkeypatch.setattr(fleet_hub_sessions, "list_sessions", original)
    assert _get_sessions(url, token)["nodes_unknown"] == [NODE_LINUX]


# --- node memory reading (#2710) -------------------------------------------------------------------------------


def _memory_rows(conn):
    return conn.execute("SELECT node_id, memory_used FROM fleet_t3_observations ORDER BY node_id").fetchall()


def _columns(conn) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(fleet_t3_observations)")]


def _lockstep():
    import runpy
    from pathlib import Path

    return runpy.run_path(str(Path(__file__).resolve().parents[1] / "engines/hub/schema/lockstep.py"))


def test_a_successful_report_stores_memory_and_a_failed_attempt_keeps_the_last_good_reading(conn, monkeypatch):
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": 0.42}, NODE_LINUX)
    assert _memory_rows(conn) == [(NODE_LINUX, 0.42)]
    later = MOMENT + timedelta(seconds=120)
    monkeypatch.setattr(fleet_t3_health, "_now", lambda: later)
    # Neither a failed attempt nor a (wrongly) memory-bearing failed attempt may move the reading or the stamp.
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": False}, NODE_LINUX)
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": False, "memory_used": 0.99}, NODE_LINUX)
    assert _memory_rows(conn) == [(NODE_LINUX, 0.42)]
    assert _rows(conn) == [(NODE_LINUX, MOMENT.isoformat(), later.isoformat())]


def test_a_failed_first_attempt_stores_no_memory(conn):
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": False, "memory_used": 0.5}, NODE_LINUX)
    assert _memory_rows(conn) == [(NODE_LINUX, None)] and _rows(conn)[0][1] is None


def test_a_success_without_memory_clears_the_old_reading(conn):
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": 0.8}, NODE_LINUX)
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True}, NODE_LINUX)
    assert _memory_rows(conn) == [(NODE_LINUX, None)]


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, 0.5, 0.0001])
def test_memory_in_range_is_accepted(conn, value):
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": value}, NODE_LINUX)
    assert _memory_rows(conn) == [(NODE_LINUX, float(value))]


@pytest.mark.parametrize(
    "value",
    [-0.0001, -1, 1.0001, 2, 100, float("nan"), float("inf"), float("-inf"), True, False, "0.5", [0.5], {"v": 1}, b"1"],
)
def test_invalid_memory_is_rejected_and_never_stored(conn, value):
    with pytest.raises(FleetHubError, match="memory_used"):
        fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": value}, NODE_LINUX)
    # Rejected before the table is created or touched.
    assert "fleet_t3_observations" not in _tables(conn)


def test_invalid_memory_never_overwrites_a_stored_reading(conn):
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": 0.3}, NODE_LINUX)
    with pytest.raises(FleetHubError):
        fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": 7}, NODE_LINUX)
    assert _memory_rows(conn) == [(NODE_LINUX, 0.3)]


@pytest.mark.parametrize("value", [-0.5, 1.5, float("nan"), float("inf"), True, "0.5"])
def test_the_policy_endpoint_answers_400_for_invalid_memory(conn, value):
    with pytest.raises(fleet_hub_policy_api.FleetPolicyApiError) as raised:
        fleet_hub_policy_api.handle_policy(
            conn,
            {"action": "t3-observe", "node_id": NODE_LINUX, "ok": True, "memory_used": value},
            caller_node=NODE_LINUX,
            is_admin=False,
        )
    assert raised.value.status == 400
    assert "fleet_t3_observations" not in _tables(conn)


def test_the_policy_endpoint_stores_a_valid_reading(conn):
    status, payload = fleet_hub_policy_api.handle_policy(
        conn,
        {"action": "t3-observe", "node_id": NODE_LINUX, "ok": True, "memory_used": 0.25},
        caller_node=NODE_LINUX,
        is_admin=False,
    )
    assert status == 200 and payload["ok"] is True and "memory_used" not in payload
    assert _memory_rows(conn) == [(NODE_LINUX, 0.25)]


def _old_shape(conn) -> None:
    conn.execute(PRE_MEMORY_OBSERVATIONS)
    conn.commit()


def test_an_old_shape_table_is_accepted_by_lockstep_and_upgraded_in_place_by_the_first_report(conn):
    lockstep = _lockstep()
    required = json.loads(lockstep["REQUIRED"].read_text(encoding="utf-8"))
    _old_shape(conn)
    stamp = MOMENT.isoformat()
    conn.execute("INSERT INTO fleet_t3_observations VALUES (?, ?, ?, ?)", (NODE_LINUX_2, stamp, stamp, stamp))
    conn.commit()
    assert "memory_used" not in _columns(conn)
    # Before any report: the live Hub's old shape must still be a reviewed one, or CT 108 refuses readiness.
    assert lockstep["compatible"](lockstep["snapshot"](conn), required)
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": 0.6}, NODE_LINUX)
    assert _columns(conn)[-1] == "memory_used"
    # The old row survives the ALTER with no reading; the new one has its reading.
    assert _memory_rows(conn) == [(NODE_LINUX, 0.6), (NODE_LINUX_2, None)]
    assert _rows(conn)[1] == (NODE_LINUX_2, stamp, stamp)
    # After the upgrade: the ALTERed shape is reviewed too, not only a freshly created table.
    assert lockstep["compatible"](lockstep["snapshot"](conn), required)


def test_a_database_with_a_fresh_table_is_accepted_by_lockstep(conn):
    lockstep = _lockstep()
    fleet_t3_health.observe_t3(conn, {"node_id": NODE_LINUX, "ok": True, "memory_used": 0.6}, NODE_LINUX)
    required = json.loads(lockstep["REQUIRED"].read_text(encoding="utf-8"))
    assert lockstep["compatible"](lockstep["snapshot"](conn), required)


def test_a_shape_nobody_reviewed_is_refused_by_lockstep(conn):
    lockstep = _lockstep()
    required = json.loads(lockstep["REQUIRED"].read_text(encoding="utf-8"))
    _old_shape(conn)
    conn.execute("ALTER TABLE fleet_t3_observations ADD COLUMN surprise TEXT")
    conn.commit()
    assert not lockstep["compatible"](lockstep["snapshot"](conn), required)


def test_ensure_schema_is_idempotent_and_adds_the_column_once(conn):
    _old_shape(conn)
    fleet_t3_health.ensure_schema(conn)
    fleet_t3_health.ensure_schema(conn)
    assert _columns(conn).count("memory_used") == 1


def test_the_lockstep_generator_lists_the_old_shape_as_a_variant(tmp_path):
    lockstep = _lockstep()
    required = lockstep["generate"](tmp_path)
    variants = required["variants"]["fleet_t3_observations"]
    assert [c for c in variants[0]["column_order"]] != [c for c in variants[1]["column_order"]]
    orders = sorted(len(v["column_order"]) for v in variants)
    assert orders == [4, 5] and "fleet_t3_observations" in required["optional"]["tables"]


def _seed_memory(conn, node: str, success_age: float | None, memory) -> None:
    _seed(conn, node, success_age)
    conn.execute("UPDATE fleet_t3_observations SET memory_used=? WHERE node_id=?", (memory, node))
    conn.commit()


def test_memory_by_node_lists_only_fresh_nodes(conn):
    _seed_memory(conn, NODE_LINUX, 10, 0.4)
    _seed_memory(conn, NODE_LINUX_2, 301, 0.9)  # stale: the reading is as old as the session count
    assert fleet_t3_health.memory_by_node(conn, MOMENT) == {NODE_LINUX: 0.4}


@pytest.mark.parametrize(("age", "present"), [(0, True), (299, True), (299.9, True), (300, False), (3600, False)])
def test_memory_freshness_uses_the_same_boundary_as_session_health(conn, age, present):
    _seed_memory(conn, NODE_LINUX, age, 0.5)
    assert (NODE_LINUX in fleet_t3_health.memory_by_node(conn, MOMENT)) is present
    assert (NODE_LINUX in fleet_t3_health.fresh_nodes(conn, MOMENT)) is present


def test_memory_by_node_skips_missing_failed_and_invalid_readings(conn):
    _seed_memory(conn, NODE_LINUX, 5, None)  # no reading
    _seed_memory(conn, NODE_LINUX_2, None, 0.5)  # never succeeded
    third = "33333333-3333-4333-8333-333333333333"
    fourth = "44444444-4444-4444-8444-444444444444"
    fifth = "55555555-5555-4555-8555-555555555555"
    _seed_memory(conn, third, 5, 1.5)  # a stored value outside 0..1 is never trusted
    _seed_memory(conn, fourth, 5, "lots")  # not a number (a numeric-looking string is coerced by REAL affinity)
    _seed_memory(conn, fifth, 5, -0.1)
    assert fleet_t3_health.memory_by_node(conn, MOMENT) == {}


def test_memory_by_node_tolerates_bad_stamps(conn):
    fleet_t3_health.ensure_schema(conn)
    conn.execute(
        "INSERT INTO fleet_t3_observations (node_id, last_success_at, last_attempt_at, updated_at, memory_used) "
        "VALUES (?, ?, ?, ?, ?)",
        (NODE_LINUX, "garbage", "x", "x", 0.5),
    )
    conn.commit()
    assert fleet_t3_health.memory_by_node(conn, MOMENT) == {}


def test_memory_by_node_missing_table_and_old_table_are_empty_and_nothing_is_created_or_altered(conn):
    before = _tables(conn)
    assert fleet_t3_health.memory_by_node(conn, MOMENT) == {}
    assert _tables(conn) == before
    _old_shape(conn)
    assert fleet_t3_health.memory_by_node(conn, MOMENT) == {}
    assert "memory_used" not in _columns(conn)  # a read never upgrades the table


def test_memory_by_node_performs_no_writes(conn):
    _seed_memory(conn, NODE_LINUX, 5, 0.5)
    before_changes, before_rows = conn.total_changes, _memory_rows(conn)
    conn.execute("PRAGMA query_only=ON")
    try:
        assert fleet_t3_health.memory_by_node(conn, MOMENT) == {NODE_LINUX: 0.5}
    finally:
        conn.execute("PRAGMA query_only=OFF")
    assert conn.total_changes == before_changes and _memory_rows(conn) == before_rows and not conn.in_transaction


def test_a_memory_read_failure_is_none(conn):
    class Broken:
        def execute(self, *_args):
            raise sqlite3.DatabaseError("database disk image is malformed")

    class Locked:
        def execute(self, *_args):
            raise sqlite3.OperationalError("database is locked")

    assert fleet_t3_health.memory_by_node(Broken(), MOMENT) is None  # type: ignore[arg-type]
    assert fleet_t3_health.memory_by_node(Locked(), MOMENT) is None  # type: ignore[arg-type]
