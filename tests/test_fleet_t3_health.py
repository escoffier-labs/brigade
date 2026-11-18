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
    conn.execute("INSERT INTO fleet_t3_observations VALUES (?, ?, ?, ?)", (NODE_LINUX, "garbage", "garbage", "garbage"))
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
            "INSERT OR REPLACE INTO fleet_t3_observations VALUES (?, ?, ?, ?)",
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
    conn.execute("INSERT INTO fleet_t3_observations VALUES (?, ?, ?, ?)", (NODE_LINUX, stored, "x", "x"))
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
