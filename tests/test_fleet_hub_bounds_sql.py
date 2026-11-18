"""Hub bounds (#1738): board next link cap, SQL-side lease and Grok Bot filters."""

from __future__ import annotations

import time

import pytest

from brigade import fleet_hub, fleet_hub_grokbot, fleet_hub_http, fleet_hub_status
from tests.test_fleet_cloud import (
    FEED_NODE,
    WORKER_NODE,
    _claim_body,
    _enroll_actors,
    _grokbot_enqueue,
    conn as conn,
)
from tests.test_fleet_dashboard import _bearer, _request, hub as hub


def test_next_board_offset_never_passes_the_endpoint_maximum():
    maximum = fleet_hub_status.BOARD_OFFSET_MAX
    limit = fleet_hub_status.BOARD_LIMIT
    assert fleet_hub_status.next_board_offset(0, limit) == limit
    assert fleet_hub_status.next_board_offset(maximum - limit, limit) == maximum
    # A partial last step lands on the maximum, which the endpoint accepts.
    assert fleet_hub_status.next_board_offset(maximum - 1, limit) == maximum
    # At the maximum there is no further page to link.
    assert fleet_hub_status.next_board_offset(maximum, limit) is None


@pytest.mark.parametrize("offset,expected", [(0, "offset=200"), (999_900, "offset=1000000")])
def test_board_page_links_only_offsets_the_endpoint_accepts(hub, monkeypatch, offset, expected):
    def fake_board_status(conn, *, offset, limit=fleet_hub_status.BOARD_LIMIT, **_kwargs):
        return fleet_hub_status.BoardStatus(runs=[], more=True, offset=offset, limit=limit)

    monkeypatch.setattr(fleet_hub_http, "board_status", fake_board_status)
    status, _headers, page = _request(hub, "GET", f"/view/repos?all=1&offset={offset}", headers=_bearer())
    assert status == 200
    assert expected in page


def test_board_page_at_the_maximum_offset_emits_no_next_link(hub, monkeypatch):
    maximum = fleet_hub_status.BOARD_OFFSET_MAX

    def fake_board_status(conn, *, offset, limit=fleet_hub_status.BOARD_LIMIT, **_kwargs):
        return fleet_hub_status.BoardStatus(runs=[], more=True, offset=offset, limit=limit)

    monkeypatch.setattr(fleet_hub_http, "board_status", fake_board_status)
    # An offset above the maximum is clamped, so it renders the maximum page.
    for requested in (maximum, maximum + 200):
        status, _headers, page = _request(hub, "GET", f"/view/repos?all=1&offset={requested}", headers=_bearer())
        assert status == 200
        assert 'class="fleet-more"' not in page
        assert "offset=1000200" not in page


def _insert_lease(conn, lease_id, *, expires_at, released_at=None, admitted_at="2026-01-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO cloud_leases (lease_id, provider, owner_node, holder_token, state, admitted_at, renewed_at, "
        "ttl_seconds, expires_at, released_at) VALUES (?, 'codex-cloud', 'node-a', 'tok', 'admitted', ?, ?, 60, ?, ?)",
        (lease_id, admitted_at, admitted_at, expires_at, released_at),
    )


def test_list_cloud_leases_filters_in_sql_with_unchanged_rows_and_order(conn):
    now = time.time()
    _insert_lease(conn, "live-old", expires_at=now + 600, admitted_at="2026-01-01T00:00:00Z")
    _insert_lease(conn, "live-new", expires_at=now + 600, admitted_at="2026-01-03T00:00:00Z")
    _insert_lease(conn, "expired", expires_at=now - 5, admitted_at="2026-01-02T00:00:00Z")
    _insert_lease(conn, "released", expires_at=now + 600, released_at="2026-01-02T01:00:00Z")
    _insert_lease(conn, "released-expired", expires_at=now - 5, released_at="2026-01-02T02:00:00Z")
    conn.commit()
    everything = fleet_hub.list_cloud_leases(conn, include_all=True)
    assert len(everything) == 5
    reference = [row for row in everything if not row["expired"] and row["released_at"] is None]
    assert [row["lease_id"] for row in reference] == ["live-new", "live-old"]
    assert fleet_hub.list_cloud_leases(conn) == reference


def test_deck_projection_filters_states_in_sql_with_unchanged_rows_and_order(conn):
    _enroll_actors(conn)
    ids = ["grokbot-" + char * 24 for char in "abc"]
    for index, job_id in enumerate(ids):
        status, _payload = fleet_hub_grokbot.handle_grokbot(
            conn,
            _grokbot_enqueue(job_id, digest=str(index) * 64, operation_id=f"op-enqueue-{index}"),
            caller_node=FEED_NODE,
        )
        assert status == 200
    claimed = fleet_hub_grokbot.handle_grokbot(conn, _claim_body(ids[1]), caller_node=WORKER_NODE)
    assert claimed[0] == 200
    conn.execute("UPDATE grokbot_jobs SET state = 'canceled' WHERE job_id = ?", (ids[2],))
    conn.commit()

    rows = conn.execute(f"SELECT {fleet_hub_grokbot._JOB_COLUMNS} FROM grokbot_jobs ORDER BY queued_at, job_id")
    jobs = [fleet_hub_grokbot._job_payload(fleet_hub_grokbot._job_dict(row)) for row in rows.fetchall()]
    projection = fleet_hub_grokbot.deck_projection(conn)
    assert projection == {
        "active": [job for job in jobs if job["state"] in fleet_hub_grokbot.WORK_STATES],
        "history": [job for job in jobs if job["state"] in fleet_hub_grokbot.TERMINAL_STATES],
    }
    assert [job["state"] for job in projection["history"]] == ["canceled"]
    assert len(projection["active"]) == 2
