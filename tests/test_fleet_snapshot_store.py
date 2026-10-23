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


@pytest.mark.parametrize("budget", ["count", "bytes"])
def test_caller_budget_evicts_only_its_oldest_walk(store, monkeypatch, budget):
    fleet_hub.add_node(store, "other")
    other = seed(store, 3, caller="other")
    oldest = seed(store, 3)
    size = store.execute("SELECT payload_bytes FROM fleet_read_snapshots WHERE caller='reader'").fetchone()[0]
    if budget == "count":
        monkeypatch.setattr(paging, "SNAPSHOT_MAX_PER_CALLER", 2)
    else:
        monkeypatch.setattr(paging, "SNAPSHOT_MAX_PER_CALLER_BYTES", 2 * size)
    newer = seed(store, 3)
    newest = seed(store, 3)
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshots WHERE caller='reader'").fetchone()[0] == 2
    assert (
        store.execute("SELECT SUM(payload_bytes) FROM fleet_read_snapshots WHERE caller='reader'").fetchone()[0]
        <= 2 * size
    )
    with pytest.raises(messages.MessageError, match="restart from the first page") as caught:
        read(store, oldest["next_cursor"])
    assert caught.value.code == "paging_cursor_expired"
    assert not isinstance(caught.value, messages.MessageQuotaError)
    for page, caller in ((other, "other"), (newer, "reader"), (newest, "reader")):
        assert read(store, page["next_cursor"], caller=caller)["claims"][0]["target"] == "item-00001"


def test_snapshot_larger_than_caller_byte_budget_fails_without_eviction(store, monkeypatch):
    first = seed(store, 3)
    monkeypatch.setattr(paging, "SNAPSHOT_MAX_PER_CALLER_BYTES", 1)
    with pytest.raises(messages.MessageQuotaError, match="capacity"):
        seed(store, 3)
    assert read(store, first["next_cursor"])["next_cursor"]


@pytest.mark.parametrize("budget", ["count", "bytes"])
def test_global_budget_evicts_oldest_idle_walk_across_callers(store, monkeypatch, budget):
    clock = [1000.0]
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: clock[0])
    for caller in ("other", "newcomer"):
        fleet_hub.add_node(store, caller)
    touched = seed(store, 4)
    clock[0] += 1
    idle = seed(store, 4, caller="other")
    size = store.execute("SELECT SUM(payload_bytes) FROM fleet_read_snapshots").fetchone()[0]
    if budget == "count":
        monkeypatch.setattr(paging, "SNAPSHOT_MAX_COUNT", 2)
    else:
        monkeypatch.setattr(paging, "SNAPSHOT_MAX_BYTES", size)
    clock[0] += 61
    assert read(store, touched["next_cursor"])["next_cursor"]
    clock[0] += 1
    fresh = seed(store, 4, caller="newcomer")
    with pytest.raises(messages.MessageError, match="restart from the first page") as caught:
        read(store, idle["next_cursor"], caller="other")
    assert caught.value.code == "paging_cursor_expired"
    assert not isinstance(caught.value, messages.MessageQuotaError)
    assert read(store, touched["next_cursor"])["claims"][0]["target"] == "item-00001"
    assert read(store, fresh["next_cursor"], caller="newcomer")["next_cursor"]
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 2
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshot_rows").fetchone()[0] == 8


def test_global_budget_refuses_when_all_walks_were_recently_accessed(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: clock[0])
    for caller in ("other", "newcomer"):
        fleet_hub.add_node(store, caller)
    first = seed(store, 4)
    second = seed(store, 4, caller="other")
    monkeypatch.setattr(paging, "SNAPSHOT_MAX_COUNT", 2)
    clock[0] += 61
    for page, caller in ((first, "reader"), (second, "other")):
        assert read(store, page["next_cursor"], caller=caller)["next_cursor"]
    clock[0] += 60
    with pytest.raises(messages.MessageQuotaError) as caught:
        seed(store, 4, caller="newcomer")
    assert caught.value.code == "paging_snapshot_capacity"
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 2
    for page, caller in ((first, "reader"), (second, "other")):
        assert read(store, page["next_cursor"], caller=caller)["next_cursor"]


def test_expiry_cleanup_removes_snapshot_rows_and_counts(store, monkeypatch):
    first = seed(store, 3)
    now = fleet_hub._now_epoch()
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: now + paging.SNAPSHOT_TTL_SECONDS + 1)
    seed(store, 3)
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 1
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshot_groups").fetchone()[0] == 1
    assert store.execute("SELECT COUNT(*) FROM fleet_read_snapshot_rows").fetchone()[0] == 3
    with pytest.raises(messages.MessageError, match="expired"):
        read(store, first["next_cursor"])


@pytest.mark.parametrize("previous_version", [41, fleet_hub.SCHEMA_VERSION])
def test_old_format_current_schema_migrates_idempotently_and_rejects_cursor(store, previous_version):
    first = seed(store, 3)
    store.execute("UPDATE fleet_read_snapshots SET payload=?", (json.dumps({"claims": []}),))
    if "last_access" in {row[1] for row in store.execute("PRAGMA table_info(fleet_read_snapshots)")}:
        store.execute("ALTER TABLE fleet_read_snapshots DROP COLUMN last_access")
    objects = store.execute(
        "SELECT type,name FROM sqlite_master WHERE name GLOB 'fleet_read_snapshot_*' OR name GLOB 'fleet_read_visibility_*' "
        "ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'index' THEN 1 ELSE 2 END"
    ).fetchall()
    for kind, name in objects:
        store.execute(f'DROP {kind} "{name}"')
    store.execute(f"PRAGMA user_version={previous_version}")
    store.commit()
    path = store.execute("PRAGMA database_list").fetchone()[2]
    for _ in range(2):
        conn = fleet_hub.init_db(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
            assert "last_access" in {row[1] for row in conn.execute("PRAGMA table_info(fleet_read_snapshots)")}
            with pytest.raises(messages.MessageError, match="restart from the first page"):
                read(conn, first["next_cursor"])
            fresh = seed(conn, 3)
            assert read(conn, fresh["next_cursor"], limit=200)["next_cursor"] is None
        finally:
            conn.close()


def test_cached_wire_cost_preserves_oversized_count_and_skips_payload(store, monkeypatch):
    monkeypatch.setattr(paging, "PAGE_MAX_BYTES", 20000)
    raw = {"action": "claims", "limit": 1}
    data = {
        "claims": [
            {"owner_node": "reader", "target": f"item-{i}", "note": "x" * (16000 if i == 1 else 500)} for i in range(4)
        ]
    }
    first = paging.page(store, raw, data, caller="reader", limit=1, visible=lambda *_: True, snapshot_write=True)
    store.commit()
    loads = json.loads
    decoded = []

    def measured_loads(value, *args, **kwargs):
        decoded.append(len(value))
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", measured_loads)
    fetched_bytes = []

    def count_text_bytes(_cursor, row):
        fetched_bytes.extend(len(value.encode()) for value in row if isinstance(value, str))
        return row

    store.row_factory = count_text_bytes
    try:
        page = read(store, first["next_cursor"])
    finally:
        store.row_factory = None
    assert sum(fetched_bytes) < 2000, fetched_bytes
    assert page["claims"][0]["target"] == "item-2"
    assert page["oversized"] == 1 and page["total"] == 4 and page["next_cursor"]
    assert max(decoded) < 2000


def test_continuation_seeks_past_revoked_prefix_with_stale_statistics(store):
    from brigade import fleet_hub_message_grants as grants

    fleet_hub.add_node(store, "other")
    messages.add_principal(store, {"name": "limited", "may_send_to": ["*"]})
    measurements = []
    for count in (32, 2048):
        grants.replace_grants(store, "agent:limited", ["*"])
        raw = {"action": "claims", "limit": 1}
        data = {
            "claims": [
                {"owner_node": "reader" if i < count else "other", "target": f"item-{i:05d}"} for i in range(count + 3)
            ]
        }
        first = paging.page(
            store, raw, data, caller="agent:limited", limit=1, visible=lambda *_: True, snapshot_write=True
        )
        # Statistics collected before grant narrowing can favor the primary key
        # over the visibility index unless the seek is required explicitly.
        store.execute("ANALYZE")
        grants.replace_grants(store, "agent:limited", ["other"])
        store.commit()
        steps = []
        store.set_progress_handler(lambda steps=steps: steps.append(1) or 0, 1)
        try:
            page = read(store, first["next_cursor"], caller="agent:limited")
        finally:
            store.set_progress_handler(None, 0)
        assert page["claims"][0]["target"] == f"item-{count:05d}"
        assert page["total"] == 3 and page["next_cursor"]
        measurements.append(len(steps))
    assert measurements[1] <= measurements[0] * 2, measurements
