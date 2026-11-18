"""A node whose native T3 observer stopped reporting is unknown on the Deck, never idle (#2710)."""

from __future__ import annotations

import re

import pytest

from brigade import fleet_command_deck as deck
from brigade import fleet_deck_overview as overview
from brigade import fleet_hub, fleet_hub_policy, fleet_hub_routing, fleet_quota, fleet_t3_health
from brigade.fleet_t3_unknown import T3_UNKNOWN_LABELS
from tests.test_fleet_deck_overview import _plane, _station, _view
from tests.test_fleet_routing import (
    NODE_CLOUD,
    NODE_LINUX,
    NODE_LINUX_2,
    NODE_WINDOWS,
    _fresh_quota,
    _observe,
    _now,
    _save_policy,
    _t3_health,
)

LINUX_1 = "worker-linux-1"
LINUX_2 = "worker-linux-2"
STALE_AGE = fleet_t3_health.STALE_AFTER_SECONDS + 60


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """A real Hub database with routing enabled and the routing tests' fixed clock."""
    monkeypatch.setattr(fleet_hub_routing, "_now", _now)
    monkeypatch.setattr(fleet_quota, "_now", _now)
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        _save_policy(connection)
        yield connection
    finally:
        connection.close()


def _observe_machines(conn) -> None:
    """Fresh machine telemetry and a fresh T3 observer for every node; tests then age one of them."""
    _fresh_quota(conn)
    for node in (NODE_LINUX, NODE_LINUX_2, NODE_CLOUD, NODE_WINDOWS):
        _t3_health(conn, node)
    for node in (NODE_LINUX, NODE_LINUX_2, NODE_CLOUD):
        _observe(conn, node, load=0.0)
    _observe(conn, NODE_WINDOWS, load=0.0, usable_seats=[])


def _machine(conn, name: str) -> dict:
    return next(row for row in fleet_hub_routing.snapshot(conn)["machines"] if row["id"] == name)


def _deck_view(conn):
    """One station per Linux machine, joined to the snapshot the Hub would serve."""
    plane = deck.control_plane_from_snapshot(fleet_hub_routing.control_plane_status(conn))
    stations = (_station(NODE_LINUX, "linux-1", 1), _station(NODE_LINUX_2, "linux-2", 1))
    return _view(stations, plane=plane)


def test_reason_codes_match_the_health_classifier():
    assert set(T3_UNKNOWN_LABELS) == {fleet_t3_health.SESSIONS_UNKNOWN, fleet_t3_health.T3_HEALTH_MISSING}


def test_snapshot_keeps_slots_for_a_fresh_observer(conn):
    _observe_machines(conn)
    _t3_health(conn, NODE_LINUX, 0)
    _t3_health(conn, NODE_LINUX_2, STALE_AGE - 120)  # still inside the TTL
    for name in (LINUX_1, LINUX_2):
        row = _machine(conn, name)
        assert row["available_slots"] == 1
        assert row["unused_reason"] == "idle-capacity"


def test_snapshot_reports_a_stale_observer_as_unknown_not_free(conn):
    _observe_machines(conn)
    _t3_health(conn, NODE_LINUX_2, STALE_AGE)
    row = _machine(conn, LINUX_2)
    assert row["available_slots"] is None
    assert row["unused_reason"] == fleet_t3_health.SESSIONS_UNKNOWN
    assert row["state"] == "available"  # the machine's own telemetry is still fresh


def test_snapshot_reports_a_failed_observer_with_no_success_as_unknown(conn):
    _observe_machines(conn)
    _t3_health(conn, NODE_LINUX_2, None)
    row = _machine(conn, LINUX_2)
    assert row["available_slots"] is None
    assert row["unused_reason"] == fleet_t3_health.SESSIONS_UNKNOWN


def test_snapshot_reports_a_node_with_no_health_row_as_unknown(conn):
    _observe_machines(conn)
    conn.execute("DELETE FROM fleet_t3_observations WHERE node_id=?", (NODE_LINUX_2,))
    conn.commit()
    row = _machine(conn, LINUX_2)
    assert row["available_slots"] is None
    assert row["unused_reason"] == fleet_t3_health.T3_HEALTH_MISSING


def test_snapshot_reports_every_machine_unknown_when_the_health_read_fails(conn, monkeypatch):
    _observe_machines(conn)
    monkeypatch.setattr(fleet_t3_health, "classify_nodes", lambda *_a, **_k: None)
    for name in (LINUX_1, LINUX_2):
        row = _machine(conn, name)
        assert row["available_slots"] is None
        assert row["unused_reason"] == fleet_t3_health.SESSIONS_UNKNOWN


def test_policy_limits_still_win_over_an_unknown_observer(conn):
    _observe_machines(conn)
    conn.execute("DROP TABLE fleet_t3_observations")  # no health rows at all
    changed = fleet_hub_policy.current_policy(conn)["document"]
    changed["machines"][LINUX_1]["enabled"] = False
    fleet_hub_policy.save_policy(conn, changed, expected_version=2, actor="operator", reason="off")
    row = _machine(conn, LINUX_1)
    assert row["unused_reason"] == "machine-disabled" and row["available_slots"] == 0


def test_deck_renders_a_fresh_node_as_idle_and_a_stale_node_as_unknown(conn):
    _observe_machines(conn)
    _t3_health(conn, NODE_LINUX_2, STALE_AGE)
    view = _deck_view(conn)
    fresh, stale = view.stations
    assert overview.station_status(fresh, overview.machine_for(view, fresh)) == "idle"
    assert overview.station_status(stale, overview.machine_for(view, stale)) == "unknown"
    assert overview.summarize(view).idle == "1"  # only the fresh node's slot; the stale one is not counted
    assert overview.summarize(view).offline == "0"  # unknown is not offline either
    machine = overview.machine_for(view, stale)
    assert overview.machine_notes(stale, machine, "unknown") == (("sessions unknown", True),)
    page = deck.render_control_plane(view.control_plane)
    fresh_row, stale_row = (re.search(rf"<tr><td>{name}</td>.*?</tr>", page).group(0) for name in (LINUX_1, LINUX_2))
    assert "<td>1</td>" in fresh_row and "idle-capacity" in fresh_row  # exactly as before
    assert "sessions unknown: T3 observer stale or unreadable" in stale_row
    assert "idle-capacity" not in stale_row and "<td>unknown</td>" in stale_row  # Free slots cell


def test_deck_names_a_missing_observer_and_leaves_the_seat_bar_unknown(conn):
    _observe_machines(conn)
    conn.execute("DELETE FROM fleet_t3_observations WHERE node_id=?", (NODE_LINUX_2,))
    conn.commit()
    view = _deck_view(conn)
    stale = view.stations[1]
    assert "sessions unknown: T3 observer not reporting" in deck.render_control_plane(view.control_plane)
    seats = overview._seats_html(stale, overview.machine_for(view, stale), "unknown")
    assert "1 unknown" in seats and "idle" not in seats


@pytest.mark.parametrize("name", [LINUX_1, LINUX_2])
def test_deck_renders_every_machine_unknown_when_the_health_read_fails(conn, monkeypatch, name):
    _observe_machines(conn)
    monkeypatch.setattr(fleet_t3_health, "classify_nodes", lambda *_a, **_k: None)
    view = _deck_view(conn)
    for station in view.stations:
        assert overview.station_status(station, overview.machine_for(view, station)) == "unknown"
    assert overview.summarize(view).idle == "0"


def test_a_fresh_machine_state_renders_exactly_as_before():
    """The pure deck path for a snapshot row with real slots is untouched."""
    from tests.test_fleet_deck_overview import NODE_A, _machine as machine_state

    view = _view((_station(NODE_A, "linux-a", 10),), plane=_plane([machine_state("linux-a", NODE_A)]))
    station = view.stations[0]
    assert overview.station_status(station, overview.machine_for(view, station)) == "idle"
    assert overview.summarize(view).idle == "8"
    assert "idle-capacity" in deck.render_control_plane(view.control_plane)
