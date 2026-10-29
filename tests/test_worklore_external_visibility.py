"""Unscoped event histories omit the reserved external ownership family."""

import json
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from urllib.parse import urlencode

import pytest

from brigade import fleet_hub
from brigade import worklore_store as store
from tests.test_fleet_cloud import ADMIN_TOKEN, _hub, _request


_PREFIX = "external-ownership-"
_HIDDEN_TYPES = [_PREFIX, _PREFIX + "offered", _PREFIX + "future", _PREFIX + "雪"]
_VISIBLE_TYPES = ["legacy", "External-ownership-offered", "external-Ownership-x", "xexternal-ownership-x"]
_MARKERS = [f"hidden-{field}-marker" for field in ("actor", "node", "run", "status", "time", "id", "nested")]


def _ledger(path, *, encoding="UTF-8"):
    initial = sqlite3.connect(path)
    try:
        initial.execute(f"PRAGMA encoding='{encoding}'")
        initial.execute("CREATE TABLE encoding_fixture (value TEXT)")
        initial.commit()
    finally:
        initial.close()
    conn = fleet_hub.init_db(path)
    item = store.create_item(conn, {"title": "Visibility fixture", "kind": "fleet"}, actor_id="fixture")
    conn.execute("DELETE FROM work_events")
    conn.commit()
    return conn, item["work_id"]


def _event(conn, work_id, seq, event_type="legacy", *, hidden=False, detail=None, occurred_at="2026-01-02"):
    actor, node, run, status, time, event_id, nested = _MARKERS
    conn.execute(
        "INSERT INTO work_events (work_id,event_id,event_type,from_status,to_status,actor_type,actor_id,"
        "node_id,run_id,detail_json,occurred_at,received_at,seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            work_id,
            f"{event_id}-{seq}" if hidden else f"public-{seq}",
            event_type,
            status if hidden else "captured",
            status if hidden else "ready",
            actor if hidden else "operator",
            actor if hidden else "fixture",
            node if hidden else None,
            run if hidden else None,
            detail if detail is not None else json.dumps({"nested": {"value": nested}} if hidden else {"seq": seq}),
            time if hidden else occurred_at,
            time if hidden else occurred_at,
            seq,
        ),
    )


def _pages(read):
    pages = []
    cursor = None
    for _ in range(30):
        page = read(cursor)
        assert read(cursor) == page, "cursor replay must be stable"
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
    pytest.fail("event pagination did not terminate")


def _snapshot(conn, work_id):
    return {
        "direct": store.list_events(conn, work_id),
        "recent": store.recent_events(conn, work_id, limit=3),
        "item": _pages(lambda cursor: store.list_events_page(conn, work_id, limit=2, cursor=cursor)),
        "global": _pages(lambda cursor: store.list_all_events(conn, limit=2, cursor=cursor)),
        "filtered": _pages(lambda cursor: store.list_all_events(conn, work_id=work_id, limit=2, cursor=cursor)),
    }


def test_mixed_history_preserves_public_fields_order_pages_and_replayed_cursors(tmp_path):
    conn, work_id = _ledger(tmp_path / "mixed.db")
    try:
        for seq, event_type in zip((10, 20, 30, 40), _VISIBLE_TYPES, strict=True):
            _event(conn, work_id, seq, event_type, occurred_at="2026-01-01" if seq < 30 else "2026-01-02")
        conn.commit()
        control = _snapshot(conn, work_id)
        assert [event["event_id"] for event in control["direct"]] == [f"public-{seq}" for seq in (10, 20, 30, 40)]
        assert [event["event_id"] for event in control["recent"]] == [f"public-{seq}" for seq in (20, 30, 40)]
        for seq, event_type in zip((1, 15, 25, 45), _HIDDEN_TYPES, strict=True):
            _event(conn, work_id, seq, event_type, hidden=True)
            conn.execute(
                "UPDATE work_events SET occurred_at=? WHERE seq=?",
                ("2026-01-01" if seq < 30 else "2026-01-02", seq),
            )
        conn.commit()
        assert _snapshot(conn, work_id) == control
    finally:
        conn.close()


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
def test_text_and_blob_prefixes_are_exact_in_every_sqlite_encoding(tmp_path, encoding):
    conn, work_id = _ledger(tmp_path / "encoded.db", encoding=encoding)
    try:
        assert conn.execute("PRAGMA encoding").fetchone()[0].lower() == encoding.lower()
        for seq, event_type in enumerate(_VISIBLE_TYPES, 1):
            _event(conn, work_id, seq, event_type)
        _event(conn, work_id, 5, b"External-ownership-legacy")
        _event(conn, work_id, 6, b"xexternal-ownership-legacy")
        expected = store.list_events(conn, work_id)
        for seq, event_type in enumerate(_HIDDEN_TYPES, 10):
            _event(conn, work_id, seq, event_type, hidden=True)
            _event(conn, work_id, seq + 10, event_type.encode("utf-8"), hidden=True)
        conn.commit()
        assert store.list_events(conn, work_id) == expected
        assert store.recent_events(conn, work_id, limit=6) == expected
        assert store.list_events_page(conn, work_id)["events"] == expected
        assert store.list_all_events(conn)["events"] == list(reversed(expected))
    finally:
        conn.close()


def test_hidden_rows_do_not_consume_filtered_scan_budget_or_parse_large_details(tmp_path):
    conn, work_id = _ledger(tmp_path / "budget.db")
    try:
        _event(conn, work_id, 1, "target")
        for seq in range(2, store.LIST_SCAN_MAX + 5):
            _event(conn, work_id, seq, _PREFIX + "future", hidden=True, detail="[malformed" + "x" * 4096)
        conn.commit()
        page = store.list_all_events(conn, event_type="target", limit=1)
        assert [event["event_id"] for event in page["events"]] == ["public-1"]
        assert page["next_cursor"] is None
        assert store.recent_events(conn, work_id, limit=1) == page["events"]
    finally:
        conn.close()


def test_sparse_external_filter_scans_public_candidates_and_ignores_external_only_tail(tmp_path):
    conn, work_id = _ledger(tmp_path / "sparse.db")
    try:
        for seq in range(10, store.LIST_SCAN_MAX + 11):
            _event(conn, work_id, seq)
        # An older hidden-only tail must not make the final existence probe succeed.
        _event(conn, work_id, 1, _PREFIX + "future", hidden=True)
        conn.execute("UPDATE work_events SET occurred_at='2025-01-01' WHERE seq=1")
        conn.commit()
        control = _pages(lambda cursor: store.list_all_events(conn, event_type="absent", limit=1, cursor=cursor))
        external = _pages(
            lambda cursor: store.list_all_events(conn, event_type=_PREFIX + "future", limit=1, cursor=cursor)
        )
        assert all(page["events"] == [] for page in control)
        assert len(control) == 2 and control[0]["next_cursor"] and control[-1]["next_cursor"] is None
        assert external == control
        last_public = store._encode_cursor("events", {"occurred_at": "2026-01-02", "seq": "10"})
        assert store.list_all_events(conn, cursor=last_public) == {"events": [], "next_cursor": None}
    finally:
        conn.close()


def _cursor(kind, values):
    return urlsafe_b64encode(json.dumps({"kind": kind, "values": values}).encode()).decode().rstrip("=")


def test_external_filter_keeps_limit_and_cursor_validation(tmp_path):
    conn, work_id = _ledger(tmp_path / "validation.db")
    try:
        bad_cursors = [
            "!malformed!",
            _cursor("items", {"created_at": "2026", "work_id": "fixture"}),
            _cursor("events", {"occurred_at": "2026", "seq": "not-digits"}),
            _cursor("events", {"occurred_at": "2026", "seq": "9" * 40}),
        ]
        for event_type in (None, _PREFIX + "future"):
            for limit in (0, 101, "invalid"):
                with pytest.raises(store.WorkloreValidationError) as error:
                    store.list_all_events(conn, event_type=event_type, limit=limit)
                assert error.value.code == "field-bound"
            for cursor in bad_cursors[:-1]:
                with pytest.raises(store.WorkloreValidationError) as error:
                    store.list_all_events(conn, event_type=event_type, cursor=cursor)
                assert error.value.code == "field-bound"
                assert str(error.value) == "cursor is invalid"
            # HTTP translates the store's legacy SQLite binding overflow below.
            with pytest.raises(OverflowError):
                store.list_all_events(conn, event_type=event_type, cursor=bad_cursors[-1])
        for cursor in (bad_cursors[0], bad_cursors[1]):
            with pytest.raises(store.WorkloreValidationError) as error:
                store.list_events_page(conn, work_id, cursor=cursor)
            assert error.value.code == "field-bound"
    finally:
        conn.close()


@pytest.mark.parametrize(
    "event_type",
    _HIDDEN_TYPES
    + [b"external-ownership-\xff", bytearray(b"external-ownership-x"), memoryview(b"external-ownership-x")],
)
def test_projector_refuses_reserved_family_before_malformed_json(tmp_path, event_type):
    conn, work_id = _ledger(tmp_path / "projector.db")
    try:
        _event(conn, work_id, 1, detail="malformed-json")
        row = list(conn.execute("SELECT " + ",".join(store._EVENT_COLUMNS) + " FROM work_events").fetchone())
        row[2] = event_type
        with pytest.raises(store.WorkloreValidationError) as error:
            store._event_from_row(row)
        assert error.value.code == "field-bound"
        assert str(error.value) == "event is unavailable"
        row[2] = "legacy"
        with pytest.raises(json.JSONDecodeError):
            store._event_from_row(row)
    finally:
        conn.close()


def test_temporary_http_responses_and_decoded_cursors_omit_private_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as hub:
        conn = fleet_hub.open_db(hub[2])
        try:
            item = store.create_item(conn, {"title": "HTTP fixture", "kind": "fleet"}, actor_id="fixture")
            work_id = item["work_id"]
            conn.execute("DELETE FROM work_events")
            for seq in range(10, 10 + store.DETAIL_RECENT_EVENTS + 2):
                _event(conn, work_id, seq)
            conn.commit()
            paths = ["/work/events?limit=2", f"/work/items/{work_id}/events?limit=2", f"/work/items/{work_id}"]
            controls = [_request(hub, "GET", path, token=ADMIN_TOKEN) for path in paths]
            for seq, event_type in enumerate(_HIDDEN_TYPES, 100):
                _event(conn, work_id, seq, event_type, hidden=True)
            _event(conn, work_id, 200, _PREFIX + "large", hidden=True, detail="malformed" + "x" * 100_000)
            conn.commit()
            for path, control in zip(paths, controls, strict=True):
                result = _request(hub, "GET", path, token=ADMIN_TOKEN)
                assert result[0] == 200
                assert result == control
                payload = result[1]
                serialized = json.dumps(payload)
                cursor = payload.get("next_cursor")
                if cursor:
                    serialized += urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                for marker in [*_MARKERS, *_HIDDEN_TYPES]:
                    assert marker not in serialized
            for query in (
                {"limit": 0},
                {"cursor": "!malformed!"},
                {"cursor": _cursor("items", {"created_at": "2026", "work_id": "fixture"})},
                {"cursor": _cursor("events", {"occurred_at": "2026", "seq": "9" * 40})},
            ):
                status, body = _request(
                    hub,
                    "GET",
                    "/work/events?" + urlencode({"event_type": _PREFIX + "future", **query}),
                    token=ADMIN_TOKEN,
                )
                assert (status, body["code"]) == (400, "field-bound")
        finally:
            conn.close()
