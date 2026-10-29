"""Prepared context contracts at the real SQLite read boundary."""

import base64
import importlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from brigade import fleet_hub, worklore_ownership as ownership, worklore_store as store
from brigade.worklore_validate import WorkloreValidationError

STAMP = "2026-01-01T00:00:00+02:00"
NONCE = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
NONCE_B = base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")
CHECKPOINT = {
    "action": "checkpoint",
    "generation": 1,
    "repo_identity": "example/project",
    "write_scope": ["src"],
    "source_revision": "a" * 40,
    "next_action": {"kind": "verify", "resume_condition": ""},
    "evidence_refs": [{"kind": "receipt", "ref": "receipt-a", "source_revision": "b" * 40}],
}
REPORT = {
    "version": 1,
    "provider": "dot",
    "session_id": "session-a",
    "agent_label": "worker-a",
    "observed_at": STAMP,
    "sequence": 91,
    "progress": "Checking fixture",
    "evidence_refs": ["receipt-a", "https://example.com/check/1"],
}


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    for node in ("node-a", "node-b"):
        fleet_hub.add_node(conn, node, node)
    item = store.create_item(conn, {"title": "Fixture", "kind": "repo"}, actor_id="admin", actor_type="operator")
    yield conn, item["work_id"], path
    conn.close()


def query(conn, work_id):
    return importlib.import_module("brigade.worklore_prepared_handoff").get_prepared_handoff(conn, work_id)


def action(conn, work_id, body, revision, *, node="node-a", admin=False):
    return ownership.ownership_action(
        conn,
        work_id,
        body,
        expected_revision=revision,
        idempotency_key=f"action-{revision}",
        actor_id="admin" if admin else node,
        actor_type="operator" if admin else "node",
        is_admin=admin,
        holder_nonce=None if admin else NONCE_B if node == "node-b" else NONCE,
    )


def offer(conn, work_id):
    return action(
        conn,
        work_id,
        {
            "action": "offer",
            "target_node": "node-a",
            "exclusions": [],
            "authorization_ref": "grant-a",
            "attempt_budget": {"cap": 2, "source_ref": "budget-a"},
        },
        0,
        admin=True,
    )


def recorded_context(conn, work_id):
    offer(conn, work_id)
    action(conn, work_id, {"action": "accept", "generation": 0}, 1)
    action(conn, work_id, CHECKPOINT, 2)
    return action(conn, work_id, {"action": "report", "generation": 1, "report": REPORT}, 3)


def append_execution(conn, work_id, node="node-a", run="run-a"):
    store._append_event(
        conn,
        work_id=work_id,
        event_type="execution-linked",
        from_status=None,
        to_status=None,
        actor_id=node,
        actor_type="node",
        node_id=node,
        run_id=run,
        now=STAMP,
        detail={"holder_hash": "INTERNAL-MARKER", "transcript": "RAW-MARKER"},
    )


def test_archived_item_retains_prepared_context(ledger):
    conn, work_id, _ = ledger
    recorded_context(conn, work_id)
    for version, status in enumerate(("canceled", "archived"), start=1):
        store.transition(conn, work_id, to_status=status, expected_version=version, actor_id="admin")

    view = query(conn, work_id)

    assert view["item"]["status"] == "archived"
    assert view["checkpoint"]["source_revision"] == CHECKPOINT["source_revision"]
    assert view["last_report"]["data"]["session_id"] == REPORT["session_id"]


def test_initial_exact_schema_nulls_and_read_only_transaction(ledger):
    conn, work_id, _ = ledger
    before = conn.total_changes
    statements = []
    conn.set_trace_callback(statements.append)
    view = query(conn, work_id)
    assert set(view) == {
        "schema",
        "observed_at",
        "consistency",
        "visibility",
        "authority",
        "person_attribution",
        "historical_human_attribution",
        "item",
        "ownership",
        "checkpoint",
        "last_report",
        "run_refs",
    }
    assert view["schema"] == "brigade.prepared_handoff.v1"
    assert datetime.fromisoformat(view["observed_at"]).tzinfo is not None
    assert view["consistency"] == "single-db-snapshot"
    assert view["visibility"] == "fleet-trust"
    assert view["authority"] == "read-only-context"
    assert view["person_attribution"] == view["historical_human_attribution"] == "unrecorded"
    assert view["item"] == {
        "work_id": work_id,
        "version": 1,
        "title": "Fixture",
        "kind": "repo",
        "status": "captured",
        "acceptance": [],
        "effective_acceptance": [],
        "blocker": None,
    }
    assert view["ownership"] == {
        "state": "unowned",
        "revision": 0,
        "generation": 0,
        "owner_node": None,
        "offered_to": None,
        "last_seq": None,
        "updated_at": None,
        "liveness": "unknown",
    }
    assert view["checkpoint"] is view["last_report"] is None
    assert view["run_refs"] == {
        "items": [],
        "truncated": False,
        "coverage": "recorded-execution-events",
        "ownership_generation_binding": "unavailable",
    }
    assert not conn.in_transaction and conn.total_changes == before
    assert [sql for sql in statements if sql in {"BEGIN", "COMMIT", "ROLLBACK"}] == ["BEGIN", "COMMIT"]


def test_active_caller_transaction_is_untouched_and_query_failure_cleans_up(ledger):
    conn, work_id, _ = ledger
    conn.execute("UPDATE work_items SET title='Pending' WHERE work_id=?", (work_id,))
    statements = []
    conn.set_trace_callback(statements.append)
    with pytest.raises(sqlite3.ProgrammingError):
        query(conn, work_id)
    assert conn.in_transaction and statements == []
    assert store.get_item(conn, work_id)["title"] == "Pending"
    conn.rollback()
    assert store.get_item(conn, work_id)["title"] == "Fixture"
    with pytest.raises(store.WorkloreNotFound):
        query(conn, "absent")
    assert not conn.in_transaction
    conn.execute("UPDATE work_items SET title=? WHERE work_id=?", ("x" * 241, work_id))
    conn.commit()
    with pytest.raises(WorkloreValidationError):
        query(conn, work_id)
    assert not conn.in_transaction


def test_sql_read_failure_rolls_back_only_the_owned_transaction(ledger):
    conn, work_id, _ = ledger
    conn.set_authorizer(
        lambda operation, table, *_: (
            sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_READ and table == "work_events" else sqlite3.SQLITE_OK
        )
    )
    with pytest.raises(sqlite3.DatabaseError):
        query(conn, work_id)
    assert not conn.in_transaction
    conn.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
    assert query(conn, work_id)["ownership"]["revision"] == 0


def test_snapshot_precedes_observation_and_survives_a_concurrent_writer(ledger, monkeypatch):
    conn, work_id, path = ledger
    conn.execute("PRAGMA journal_mode=WAL")
    real_get = store.get_item
    real_now = store._utc_now
    first_read = []
    clock_reads = []
    reader_thread = threading.get_ident()

    def write():
        with sqlite3.connect(path) as writer:
            store.patch_item(
                writer, work_id, {"title": "New"}, expected_version=1, actor_id="admin", actor_type="operator"
            )
            offer(writer, work_id)

    def get_then_write(connection, identifier):
        item = real_get(connection, identifier)
        if connection is conn and not first_read:
            first_read.append(True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(write).result(timeout=5)
        return item

    def observed():
        if threading.get_ident() == reader_thread:
            assert first_read, "observed_at was captured before the snapshot-establishing read"
            clock_reads.append(True)
        return real_now()

    monkeypatch.setattr(store, "get_item", get_then_write)
    monkeypatch.setattr(store, "_utc_now", observed)
    view = query(conn, work_id)
    assert clock_reads
    assert (view["item"]["title"], view["item"]["version"], view["ownership"]["revision"]) == ("Fixture", 1, 0)
    assert (real_get(conn, work_id)["title"], ownership.get_ownership(conn, work_id)["revision"]) == ("New", 1)


def test_retained_checkpoint_report_and_versions_are_explicitly_unbound(ledger):
    conn, work_id, _ = ledger
    recorded = recorded_context(conn, work_id)
    store.patch_item(
        conn,
        work_id,
        {"title": "Edited", "description": "OMITTED-MARKER"},
        expected_version=1,
        actor_id="admin",
        actor_type="operator",
    )
    action(conn, work_id, {"action": "handoff", "generation": 1, "target_node": "node-b"}, 4)
    action(conn, work_id, {"action": "accept", "generation": 1}, 5, node="node-b")
    append_execution(conn, work_id)
    conn.commit()
    view = query(conn, work_id)
    assert (view["item"]["version"], view["ownership"]["revision"], view["ownership"]["generation"]) == (2, 6, 2)
    assert view["ownership"]["last_seq"] != view["item"]["version"]
    assert view["ownership"]["owner_node"] == "node-b"
    assert view["checkpoint"] == {
        **{key: value for key, value in CHECKPOINT.items() if key not in {"action", "generation"}},
        "conflict_check": "path-admission",
        "conflict_check_freshness": "historical-checkpoint-admission",
        "evidence_verification": "reported-unverified",
        "checkpoint_current_item_version_binding": "unavailable",
    }
    report = view["last_report"]
    assert set(report) == {"data", "reporter_node", "freshness"}
    assert report["reporter_node"] == "node-a" and report["freshness"] == "unassessed"
    assert report["data"] == {key: value for key, value in recorded["last_report"].items() if key != "reporter_node"}
    assert report["data"]["sequence"] == 91
    assert report["data"]["observed_at"] == recorded["last_report"]["observed_at"]
    assert "MARKER" not in json.dumps(view)
    assert not any(key in json.dumps(view) for key in ("holder_hash", "authorization_ref", "detail_json"))


def test_saved_report_strips_only_storage_fields_and_preserves_reported_timestamp(ledger):
    conn, work_id, _ = ledger
    recorded_context(conn, work_id)
    seq, raw = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? AND event_type='ownership-reported'",
        (work_id,),
    ).fetchone()
    detail = json.loads(raw)
    report = detail["ownership"]["last_report"]
    report.update(observed_at=STAMP, evidence_verification="stored-label", provider_lifecycle="stored-label")
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), seq))
    conn.commit()
    view = query(conn, work_id)["last_report"]
    assert view["reporter_node"] == "node-a"
    assert view["data"]["observed_at"] == STAMP and view["data"]["sequence"] == 91
    assert view["data"]["evidence_verification"] == "reported-unverified"
    assert view["data"]["provider_lifecycle"] == "unobserved"
    report["transcript"] = "RAW-MARKER"
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), seq))
    conn.commit()
    with pytest.raises(ValueError, match="unsupported report fields"):
        query(conn, work_id)
    assert not conn.in_transaction


def test_effective_acceptance_preserves_source_and_own_criteria_separately(ledger):
    conn, _, _ = ledger
    store.import_batch(
        conn,
        adapter_id="github:fixture",
        source_type="github",
        idempotency_key="import-a",
        actor_id="node-a",
        owner_node="node-a",
        observations=[
            {
                "external_key": "example/project#1",
                "link_type": "github",
                "title": "Imported",
                "source_policy": "eligible",
                "external_updated_at": STAMP,
                "acceptance": ["Source criterion"],
            }
        ],
    )
    imported = conn.execute("SELECT work_id FROM work_links WHERE adapter_id='github:fixture'").fetchone()[0]
    view = query(conn, imported)
    assert view["item"]["acceptance"] == []
    assert view["item"]["effective_acceptance"] == ["Source criterion"]
    store.patch_item(
        conn,
        imported,
        {"acceptance": ["Own criterion"]},
        expected_version=view["item"]["version"],
        actor_id="admin",
        actor_type="operator",
    )
    assert query(conn, imported)["item"]["effective_acceptance"] == ["Own criterion"]


@pytest.mark.parametrize("count", [0, 50, 51])
def test_run_refs_composite_dedup_newest_first_and_row_truncation(ledger, count):
    conn, work_id, _ = ledger
    for index in range(count):
        append_execution(conn, work_id, "node-b" if index == 0 else "node-a", "run-same")
    conn.commit()
    view = query(conn, work_id)["run_refs"]
    expected = [] if count == 0 else [{"node_id": "node-a", "run_id": "run-same"}]
    if count == 50:
        expected.append({"node_id": "node-b", "run_id": "run-same"})
    assert view["items"] == expected
    assert view["truncated"] is (count == 51)


def test_bounded_indexed_reads_ignore_unrelated_event_history(ledger):
    conn, work_id, _ = ledger
    for _ in range(3000):
        store._append_event(
            conn,
            work_id=work_id,
            event_type="sync-observed",
            from_status=None,
            to_status=None,
            actor_id="node-a",
            actor_type="node",
            now=STAMP,
            detail={"body": "RAW-MARKER"},
        )
    for index in range(70):
        append_execution(conn, work_id, run=f"run-{index}")
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)
    steps = []
    conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
    view = query(conn, work_id)
    conn.set_progress_handler(None, 0)
    assert len(view["run_refs"]["items"]) == 50
    assert view["run_refs"]["items"][0]["run_id"] == "run-69"
    assert len(steps) < 250
    selects = [sql for sql in statements if "execution-linked" in sql]
    assert len(selects) == 1
    plan = " ".join(str(row) for row in conn.execute("EXPLAIN QUERY PLAN " + selects[0]))
    assert "work_events_attempt_work_seq" in plan
    assert "TEMP B-TREE" not in plan
    assert "detail_json" not in selects[0] and "LIMIT 51" in selects[0]
