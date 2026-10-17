"""Admin fleet board visibility for named agent inboxes (#1723)."""

from __future__ import annotations

import functools
import sqlite3
import urllib.request

import pytest

from brigade import fleet_hub, fleet_hub_enrollment, fleet_hub_messages
from tests.test_fleet_messages import ADMIN, NODE_B, _get, _post_path, _request

pytest_plugins = ["tests._fleet_message_fixtures"]


class _AgentInbox(tuple):
    def __repr__(self):
        return "<fixture agent inbox credentials redacted>"


@pytest.fixture
def agent_inbox(hub):
    base, token_a, _token_b, _db = hub
    status, enrolled = _post_path(base, ADMIN, "/message-principals", {"name": "fixture"})
    assert status == 201
    sent = []
    for number in range(2):
        status, result = _request(
            base,
            token_a,
            {"action": "send", "to": "agent:fixture", "subject": f"Task {number}", "body": "Review fixture"},
        )
        assert status == 201
        sent.append(result["message"]["id"])
    return _AgentInbox((enrolled["principal"], sent))


def test_admin_board_agent_count_drops_after_each_ack(hub, agent_inbox):
    base, token_a, _token_b, _db = hub
    principal, message_ids = agent_inbox
    assert _request(base, token_a, {"action": "send", "to": NODE_B, "subject": "Node", "body": "Keep"})[0] == 201
    page = _get(base, ADMIN, "/view/machines")
    assert 'aria-label="Agents"' in page
    assert 'data-principal="agent:fixture"' in page
    assert "2 unread messages" in page
    assert 'data-node="agent:fixture"' not in page
    assert "agent:fixture" not in _get(base, ADMIN, "/view/repos")
    assert _request(base, principal["token"], {"action": "ack", "id": message_ids[0]})[0] == 200
    page = _get(base, ADMIN, "/view/machines")
    assert 'data-principal="agent:fixture"' in page
    assert "2 unread messages" not in page
    assert page.count("1 unread message") == 2
    assert _request(base, principal["token"], {"action": "ack", "id": message_ids[1]})[0] == 200
    page = _get(base, ADMIN, "/view/machines")
    assert "agent:fixture" not in page
    assert "1 unread message" in page


def test_revoked_agent_is_hidden_while_messages_remain_unacked(hub, agent_inbox):
    base, _token_a, _token_b, db = hub
    principal, _message_ids = agent_inbox
    assert "agent:fixture" in _get(base, ADMIN, "/view/machines")
    status, _result = _post_path(
        base,
        ADMIN,
        "/message-principals",
        {"action": "revoke", "name": "fixture", "expected_enrollment_id": principal["enrollment_id"]},
    )
    assert status == 200
    assert "agent:fixture" not in _get(base, ADMIN, "/view/machines")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages WHERE acked_at IS NULL").fetchone()[0] == 2


def test_readonly_board_neither_queries_nor_discloses_agent_counts(hub, agent_inbox, monkeypatch):
    base, _token_a, _token_b, db = hub
    assert "agent:fixture" in _get(base, ADMIN, "/view/machines")

    def forbidden_query(_conn, limit=0):
        pytest.fail("read-only board queried private agent inbox totals")

    monkeypatch.setattr(fleet_hub_messages, "agent_unread_counts", forbidden_query)
    with sqlite3.connect(db) as conn:
        credential = fleet_hub_enrollment.redeem(conn, fleet_hub_enrollment.mint(conn)["code"])
    request = urllib.request.Request(
        base + "/view/machines", headers={"Cookie": f"{fleet_hub.DASHBOARD_COOKIE}={credential}"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        page = response.read().decode()
    assert "agent:fixture" not in page
    assert 'aria-label="Agents"' not in page
    assert "unread message" not in page


def test_agent_projection_uses_one_grouped_unacked_index_scan(hub, agent_inbox):
    _base, _token_a, _token_b, db = hub
    with sqlite3.connect(db) as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        assert fleet_hub_messages.agent_unread_counts(conn) == ({"agent:fixture": 2}, False)
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 1
        assert selects[0].upper().count("GROUP BY") == 1
        plan = conn.execute("EXPLAIN QUERY PLAN " + selects[0]).fetchall()
        assert any("fleet_messages_unacked_recipient" in row[3] for row in plan)
        assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_agent_projection_is_capped_and_flags_more(hub, agent_inbox, monkeypatch):
    base, token_a, _token_b, db = hub
    assert _post_path(base, ADMIN, "/message-principals", {"name": "second"})[0] == 201
    status, _result = _request(base, token_a, {"action": "send", "to": "agent:second", "subject": "S", "body": "B"})
    assert status == 201
    with sqlite3.connect(db) as conn:
        assert fleet_hub_messages.agent_unread_counts(conn, limit=1) == ({"agent:fixture": 2}, True)
        assert fleet_hub_messages.agent_unread_counts(conn, limit=2) == (
            {"agent:fixture": 2, "agent:second": 1},
            False,
        )
    monkeypatch.setattr(
        fleet_hub_messages, "agent_unread_counts", functools.partial(fleet_hub_messages.agent_unread_counts, limit=1)
    )
    page = _get(base, ADMIN, "/view/machines")
    assert 'data-principal="agent:fixture"' in page
    assert 'data-principal="agent:second"' not in page
    assert "More agents have unread messages than are shown." in page
