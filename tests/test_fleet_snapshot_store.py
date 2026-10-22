"""Snapshot storage complexity, authorization and lifecycle regressions."""

from __future__ import annotations

import base64
import json

import pytest

from brigade import fleet_hub, fleet_hub_messages as messages, fleet_hub_paging as paging


@pytest.fixture
def store(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    fleet_hub.add_node(conn, "reader")
    yield conn
    conn.close()


def seed(conn, count, *, caller="reader"):
    raw = {"action": "claims", "limit": 1}
    data = {"claims": [{"owner_node": "reader", "target": f"item-{i:05d}", "note": "x" * 1024} for i in range(count)]}
    first = paging.page(conn, raw, data, caller=caller, limit=1, visible=lambda *_: True, snapshot_write=True)
    conn.commit()
    return first


def read(conn, cursor, limit=1, *, caller="reader"):
    return messages.handle(conn, {"action": "claims", "cursor": cursor, "limit": limit}, caller=caller)[1]


def test_continuation_and_replay_work_are_bounded_by_page_size(store, monkeypatch):
    loads = json.loads
    measurements = []
    for count in (32, 2048):
        first = seed(store, count)
        decoded = []
        steps = []

        def measured_loads(value, *args, _decoded=decoded, **kwargs):
            _decoded.append(len(value.encode() if isinstance(value, str) else value))
            return loads(value, *args, **kwargs)

        for _ in range(2):
            store.set_progress_handler(lambda steps=steps: steps.append(1) or 0, 1)
            with monkeypatch.context() as patch:
                patch.setattr(json, "loads", measured_loads)
                page = read(store, first["next_cursor"])
            store.set_progress_handler(None, 0)
            assert page["claims"][0]["target"] == "item-00001"
            assert page["total"] == count
        measurements.append((sum(decoded), len(steps)))
    small, large = measurements
    assert large[0] < 8192, measurements
    assert large[0] <= small[0] * 2, measurements
    assert large[1] <= small[1] * 2, measurements


def test_agent_snapshot_and_cursor_contain_only_granted_claims(store):
    fleet_hub.add_node(store, "hidden")
    messages.add_principal(store, {"name": "limited", "may_send_to": ["reader"]})
    for index, owner in enumerate(["hidden", "hidden", "hidden", "reader", "reader"]):
        status, _ = fleet_hub.handle_claim(
            store,
            {"action": "acquire", "target": f"item-{index}", "node_id": owner, "holder": "example"},
            caller_node=owner,
        )
        assert status == 200
    first = messages.handle(store, {"action": "claims", "limit": 1}, caller="agent:limited")[1]
    sid, offsets = json.loads(base64.urlsafe_b64decode(first["next_cursor"]))
    assert offsets == [1]
    # Read both layouts so the same assertion can mutation-test the old store.
    payload = store.execute("SELECT payload FROM fleet_read_snapshots WHERE snapshot_id=?", (sid,)).fetchone()[0]
    if payload.startswith("{"):
        rows = json.loads(payload)["claims"]
    else:
        rows = [
            json.loads(row[0])
            for row in store.execute(
                "SELECT payload FROM fleet_read_snapshot_rows WHERE snapshot_id=? ORDER BY ordinal", (sid,)
            )
        ]
    assert len(rows) == 2
    assert {row["owner_node"] for row in rows} == {"reader"}


def test_repeated_complete_walks_release_snapshots_and_rows(store):
    for _ in range(10):
        first = seed(store, 3)
        last = read(store, first["next_cursor"], limit=200)
        assert last["next_cursor"] is None
        assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0
        assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshot_rows").fetchone()[0] == 0


def test_legacy_request_does_not_store_snapshot(store):
    for index in range(3):
        fleet_hub.handle_claim(
            store,
            {"action": "acquire", "target": f"item-{index}", "node_id": "reader", "holder": "example"},
            caller_node="reader",
        )
    # Lower the default page size without changing the explicit limit contract.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(paging, "PAGE_DEFAULT", 1)
        page = messages.handle(store, {"action": "claims"}, caller="reader")[1]
    assert page["truncated"] and page["next_cursor"] is None
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0
