# Synthetic rejection fixtures retain private paths and credential-like emails.
# content-guard: allow home-path file
# content-guard: allow email file
import sqlite3

import pytest
from brigade import fleet_hub
from brigade import worklore_store as store

# The hub refuses to project an observation whose source will not say which revision it is
# speaking for, so a revision is part of the ordinary shape of a test observation rather
# than something only the replay tests bother with.
_BASE_REVISION = "2026-01-01T00:00:00+00:00"


def _conn(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "worklore.db")
    store.ensure_schema(conn)
    return conn


def _legacy_items(conn, count, *, prefix="legacy"):
    """Insert old rows directly so a listing test can exercise a large ledger cheaply."""
    rows = [
        (
            f"wl-{prefix}-{index:06d}",
            f"{prefix} {index}",
            None,
            "fleet",
            None,
            "captured",
            "normal",
            0,
            0,
            "small",
            "manual",
            "[]",
            None,
            None,
            None,
            None,
            0,
            1,
            f"{index:020d}",
            f"{index:020d}",
            None,
        )
        for index in range(count)
    ]
    conn.executemany(
        "INSERT INTO work_items (work_id, title, description, kind, scope, status, priority, burn_eligible, "
        "burn_rank, token_appetite, execution_mode, acceptance_json, blocker, review_after, spend_by, ready_at, "
        "attempt_count, version, created_at, updated_at, archived_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def _within_sqlite_budget(conn, read):
    callbacks = 0

    def stop_after_fixed_work():
        nonlocal callbacks
        callbacks += 1
        return 1 if callbacks > 80 else 0

    conn.set_progress_handler(stop_after_fixed_work, 100)
    try:
        return read()
    finally:
        conn.set_progress_handler(None, 0)


def _legacy_events(conn, work_id, count, *, event_type="legacy"):
    rows = [
        (
            work_id,
            f"evt-{event_type}-{index:06d}",
            event_type,
            None,
            None,
            "operator",
            "admin",
            None,
            None,
            "{}",
            f"{index:020d}",
            f"{index:020d}",
            100_000 + index,
        )
        for index in range(count)
    ]
    conn.executemany(
        "INSERT INTO work_events (work_id, event_id, event_type, from_status, to_status, actor_type, actor_id, "
        "node_id, run_id, detail_json, occurred_at, received_at, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def test_fresh_hub_applies_worklore_schema_after_model_roster_v15(tmp_path):
    assert fleet_hub.SCHEMA_VERSION > 15
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM work_create_keys").fetchone()[0] == 0
    finally:
        conn.close()


def test_create_and_transition_share_one_transaction(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Fleet maintenance", "kind": "fleet"}, actor_id="admin")
    assert item["work_id"].startswith("wl-")
    assert item["status"] == "captured"
    assert item["version"] == 1
    events = store.list_events(conn, item["work_id"])
    assert [event["event_type"] for event in events] == ["created"]
    updated = store.transition(
        conn,
        item["work_id"],
        to_status="defining",
        expected_version=1,
        actor_type="operator",
        actor_id="admin",
    )
    assert updated["status"] == "defining" and updated["version"] == 2
    assert [event["event_type"] for event in store.list_events(conn, item["work_id"])] == [
        "created",
        "transitioned",
    ]


def test_ready_sets_ready_at_and_burn_order_is_stable(tmp_path):
    conn = _conn(tmp_path)
    first = store.create_item(conn, {"title": "A", "kind": "fleet"}, actor_id="admin")
    second = store.create_item(conn, {"title": "B", "kind": "fleet"}, actor_id="admin")
    for work_id, rank, spend_by in (
        (second["work_id"], 10, "2026-09-01T00:00:00+00:00"),
        (first["work_id"], 10, "2026-08-30T00:00:00+00:00"),
    ):
        store.patch_item(
            conn,
            work_id,
            {
                "acceptance": ["done"],
                "burn_eligible": True,
                "execution_mode": "agent",
                "burn_rank": rank,
                "spend_by": spend_by,
            },
            expected_version=1,
            actor_id="admin",
        )
        store.transition(conn, work_id, to_status="ready", expected_version=2, actor_id="admin")
    queue = store.burn_queue(conn)
    assert [item["title"] for item in queue["items"]] == ["A", "B"]
    assert queue["exclusions"]["attempt-limit"] == 0
    assert queue["exclusions"]["source-policy"] == 0


def test_exclusion_bucket_is_first_match(tmp_path):
    conn = _conn(tmp_path)
    store.create_item(conn, {"title": "Not ready", "kind": "fleet"}, actor_id="admin")
    queue = store.burn_queue(conn)
    assert queue["exclusions"]["not-ready"] == 1
    assert queue["exclusions"]["acceptance-required"] == 0


def test_work_links_require_existing_work_id(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO work_links (link_id, work_id, link_type, external_key, "
            "source_policy, source_acceptance_json, synced_at) VALUES "
            "('lnk-missing', 'wl-missing', 'url', 'https://example.com/a', "
            "'eligible', '[]', '2026-08-29T00:00:00+00:00')"
        )
        conn.commit()


def test_stale_version_does_not_write_an_event(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Conflict", "kind": "admin"}, actor_id="admin")
    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.patch_item(conn, item["work_id"], {"priority": "high"}, expected_version=0, actor_id="admin")
    assert excinfo.value.code == "version-conflict"
    assert store.list_events(conn, item["work_id"])[0]["event_type"] == "created"
    assert store.get_item(conn, item["work_id"])["version"] == 1


def test_unlink_advances_item_version_and_rejects_a_stale_writer(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Unlink race", "kind": "fleet"}, actor_id="admin")
    link = store.add_link(
        conn,
        item["work_id"],
        {"link_type": "url", "external_key": "https://example.invalid/unlink-race"},
        actor_id="admin",
    )
    conn.execute(
        "UPDATE work_items SET updated_at = ? WHERE work_id = ?", ("2000-01-01T00:00:00+00:00", item["work_id"])
    )
    conn.commit()

    unlinked = store.delete_link(
        conn,
        item["work_id"],
        link["link_id"],
        expected_version=item["version"],
        actor_id="admin",
    )

    assert unlinked["version"] == item["version"] + 1
    assert unlinked["updated_at"] != "2000-01-01T00:00:00+00:00"
    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.patch_item(
            conn, item["work_id"], {"priority": "high"}, expected_version=item["version"], actor_id="admin"
        )
    assert excinfo.value.code == "version-conflict"
    assert store.list_links(conn, item["work_id"]) == []


def test_import_batch_is_idempotent_and_keeps_scheduling(tmp_path):
    conn = _conn(tmp_path)
    observation = {
        "external_key": "escoffier-labs/brigade#1",
        "link_type": "github",
        "title": "Burn queue item",
        "description": "Do the work",
        "acceptance": ["tests pass"],
        "external_state": "open",
        "external_updated_at": "2026-08-29T00:00:00+00:00",
        "url": "https://github.com/escoffier-labs/brigade/issues/1",
        "display_ref": "escoffier-labs/brigade#1",
        "source_policy": "eligible",
    }
    first = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    store.patch_item(
        conn,
        first["items"][0]["work_id"],
        {"burn_rank": 5, "token_appetite": "large", "acceptance": ["operator override"]},
        expected_version=1,
        actor_id="admin",
    )
    conn.execute(
        "UPDATE work_sync_cursors SET last_success_at = '2026-01-01T00:00:00+00:00', "
        "updated_at = '2026-01-01T00:00:00+00:00' WHERE adapter_id = ?",
        ("github:escoffier-labs",),
    )
    conn.commit()
    second = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    replay_cursor = conn.execute(
        "SELECT last_success_at, updated_at FROM work_sync_cursors WHERE adapter_id = ?",
        ("github:escoffier-labs",),
    ).fetchone()
    assert replay_cursor[0] > "2026-01-01T00:00:00+00:00"
    assert replay_cursor[1] > "2026-01-01T00:00:00+00:00"
    later = dict(observation)
    later["acceptance"] = ["source refreshed"]
    later["external_updated_at"] = "2026-08-30T00:00:00+00:00"
    store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1b",
        observations=[later],
        actor_id="node-a",
        owner_node="node-a",
    )
    replay = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-1",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    item = store.get_item(conn, first["items"][0]["work_id"])
    links = store.list_links(conn, item["work_id"])
    assert second["created"] == 1 and second["updated"] == 0
    assert replay["created"] == second["created"]
    assert replay["updated"] == 0
    assert [entry["external_key"] for entry in replay["items"]] == [observation["external_key"]]
    assert item["burn_rank"] == 5
    assert item["token_appetite"] == "large"
    assert item["acceptance"] == ["operator override"]
    assert item["effective_acceptance"] == ["operator override"]
    assert links[0]["source_acceptance"] == ["source refreshed"]
    types = [event["event_type"] for event in store.list_events(conn, item["work_id"])]
    assert types.count("sync-observed") == 2


def test_import_rejects_secrets_paths_duplicate_keys_and_foreign_nodes(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(store.WorkloreValidationError) as excinfo:
        store.import_batch(
            conn,
            adapter_id="github:escoffier-labs",
            source_type="github",
            idempotency_key="batch-bad",
            observations=[
                {
                    "external_key": "a/b#1",
                    "link_type": "github",
                    "title": "x",
                    "source_policy": "eligible",
                    "prompt": "secret",
                }
            ],
            actor_id="node-a",
            owner_node="node-a",
        )
    assert excinfo.value.code == "unknown-field"
    store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="batch-2",
        observations=[
            {
                "external_key": "a/b#2",
                "link_type": "github",
                "title": "One",
                "url": "https://github.com/a/b/issues/2",
                "external_state": "open",
                "external_updated_at": "2026-01-01T00:00:00+00:00",
                "source_policy": "eligible",
            }
        ],
        actor_id="node-a",
        owner_node="node-a",
    )
    collision = store.import_batch(
        conn,
        adapter_id="github:other",
        source_type="github",
        idempotency_key="batch-3",
        observations=[
            {
                "external_key": "a/b#2",
                "link_type": "github",
                "title": "Two",
                "url": "https://github.com/a/b/issues/2",
                "external_state": "open",
                "external_updated_at": "2026-01-02T00:00:00+00:00",
                "source_policy": "eligible",
            }
        ],
        actor_id="node-b",
        owner_node="node-b",
    )
    assert collision["refused_details"][0]["reason"] == store.REFUSED_ADAPTER_IDENTITY_CONFLICT
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.import_batch(
            conn,
            adapter_id="github:escoffier-labs",
            source_type="github",
            idempotency_key="batch-4",
            observations=[
                {
                    "external_key": "a/b#3",
                    "link_type": "github",
                    "title": "Three",
                    "url": "https://github.com/a/b/issues/3",
                    "external_state": "open",
                    "external_updated_at": "2026-01-01T00:00:00+00:00",
                    "source_policy": "eligible",
                }
            ],
            actor_id="node-b",
            owner_node="node-b",
        )
    assert excinfo.value.code == "adapter-owner-mismatch"


def test_execution_link_requires_existing_run_for_caller_node(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Join run", "kind": "fleet"}, actor_id="admin")
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    linked = store.link_execution(conn, item["work_id"], node_id="node-a", run_id="run-1", actor_id="node-a")
    assert linked["link_type"] == "fleet-run"
    assert linked["external_key"] == "node-a/run-1"
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.link_execution(conn, item["work_id"], node_id="node-b", run_id="run-1", actor_id="node-a")
    assert excinfo.value.code == "execution-mismatch"


def test_execution_link_is_idempotent_and_conflicts_across_work_items(tmp_path):
    conn = _conn(tmp_path)
    first = store.create_item(conn, {"title": "First", "kind": "fleet"}, actor_id="admin")
    second = store.create_item(conn, {"title": "Second", "kind": "fleet"}, actor_id="admin")
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    linked = store.link_execution(conn, first["work_id"], node_id="node-a", run_id="run-1", actor_id="node-a")
    replay = store.link_execution(conn, first["work_id"], node_id="node-a", run_id="run-1", actor_id="node-a")
    assert replay == linked
    assert [event["event_type"] for event in store.list_events(conn, first["work_id"])] == [
        "created",
        "execution-linked",
    ]
    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.link_execution(conn, second["work_id"], node_id="node-a", run_id="run-1", actor_id="node-a")
    assert excinfo.value.code == "link-conflict"


def test_admin_links_are_validated_and_emit_stable_events(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Manual links", "kind": "admin"}, actor_id="admin")
    link = store.add_link(
        conn,
        item["work_id"],
        {"link_type": "url", "external_key": "https://example.invalid/runbook", "display_ref": "Runbook"},
        actor_id="admin",
    )
    assert link["url"] == "https://example.invalid/runbook"
    assert link["synced_at"]
    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.add_link(
            conn,
            item["work_id"],
            {"link_type": "url", "external_key": "https://example.invalid/runbook"},
            actor_id="admin",
        )
    assert excinfo.value.code == "link-conflict"
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.add_link(
            conn,
            item["work_id"],
            {"link_type": "fleet-run", "external_key": "node/run"},
            actor_id="admin",
        )
    assert excinfo.value.code == "link-forbidden"
    for link_type, external_key in (
        ("github", "escoffier-labs/brigade#99"),
        ("brigade", "receipt-99"),
    ):
        added = store.add_link(
            conn,
            item["work_id"],
            {"link_type": link_type, "external_key": external_key},
            actor_id="admin",
        )
        with pytest.raises(store.WorkloreConflict) as excinfo:
            store.add_link(
                conn,
                item["work_id"],
                {"link_type": link_type, "external_key": external_key},
                actor_id="admin",
            )
        assert excinfo.value.code == "link-conflict"
        store.delete_link(conn, item["work_id"], added["link_id"], actor_id="admin")
    store.delete_link(conn, item["work_id"], link["link_id"], actor_id="admin")
    assert store.list_links(conn, item["work_id"]) == []
    assert [event["event_type"] for event in store.list_events(conn, item["work_id"])] == [
        "created",
        "linked",
        "linked",
        "unlinked",
        "linked",
        "unlinked",
        "unlinked",
    ]


def test_import_counts_unchanged_without_events_and_replays_them(tmp_path):
    conn = _conn(tmp_path)
    observation = {
        "external_key": "escoffier-labs/brigade#10",
        "link_type": "github",
        "title": "Observed",
        "source_policy": "eligible",
        "url": "https://github.com/escoffier-labs/brigade/issues/10",
        "external_updated_at": _BASE_REVISION,
    }
    first = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="first",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    work_id = first["items"][0]["work_id"]
    unchanged = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="same-state",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    assert unchanged["created"] == 0 and unchanged["updated"] == 0 and unchanged["unchanged"] == 1
    assert [event["event_type"] for event in store.list_events(conn, work_id)] == ["created", "linked", "sync-observed"]
    assert (
        store.import_batch(
            conn,
            adapter_id="github:escoffier-labs",
            source_type="github",
            idempotency_key="same-state",
            observations=[observation],
            actor_id="node-a",
            owner_node="node-a",
        )
        == unchanged
    )


def test_list_keysets_filter_and_page_deterministically(tmp_path):
    conn = _conn(tmp_path)
    items = [
        store.create_item(conn, {"title": f"Item {index}", "kind": "fleet"}, actor_id="admin") for index in range(3)
    ]
    first = store.list_items(conn, kind="fleet", burn_eligible=False, limit=2)
    second = store.list_items(conn, kind="fleet", burn_eligible=False, limit=2, cursor=first["next_cursor"])
    assert [item["work_id"] for item in first["items"] + second["items"]] == [item["work_id"] for item in items]
    assert store.list_all_events(conn, event_type="created", limit=1)["next_cursor"]
    with pytest.raises(store.WorkloreValidationError) as excinfo:
        store.list_items(conn, limit=101)
    assert excinfo.value.code == "field-bound"


def test_record_attempt_failed_excludes_after_two(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Burn me", "kind": "fleet"}, actor_id="admin")
    store.patch_item(
        conn,
        item["work_id"],
        {"acceptance": ["done"], "burn_eligible": True, "execution_mode": "agent"},
        expected_version=1,
        actor_id="admin",
    )
    store.transition(conn, item["work_id"], to_status="ready", expected_version=2, actor_id="admin")
    store.record_attempt(conn, item["work_id"], action="failed", expected_version=3, actor_id="admin")
    store.record_attempt(conn, item["work_id"], action="failed", expected_version=4, actor_id="admin")
    queue = store.burn_queue(conn)
    assert queue["items"] == []
    assert queue["exclusions"]["attempt-limit"] == 1
    reset = store.record_attempt(conn, item["work_id"], action="reset", expected_version=5, actor_id="admin")
    assert reset["attempt_count"] == 0


def test_record_attempt_started_bumps_version_without_count(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Start me", "kind": "fleet"}, actor_id="admin")
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    store.link_execution(conn, item["work_id"], node_id="node-a", run_id="run-1", actor_id="node-a")
    started = store.record_attempt(
        conn,
        item["work_id"],
        action="started",
        expected_version=1,
        actor_id="node-a",
        node_id="node-a",
        run_id="run-1",
    )
    assert started["attempt_count"] == 0
    assert started["version"] == 2


def test_node_attempt_replays_are_idempotent_even_with_a_stale_version(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Replay", "kind": "fleet"}, actor_id="admin")
    _run_row(conn, run_id="run-replay")
    store.link_execution(conn, item["work_id"], node_id="node-a", run_id="run-replay", actor_id="node-a")

    started = store.record_attempt(
        conn,
        item["work_id"],
        action="started",
        expected_version=1,
        actor_id="node-a",
        node_id="node-a",
        run_id="run-replay",
    )
    replayed_start = store.record_attempt(
        conn,
        item["work_id"],
        action="started",
        expected_version=1,
        actor_id="node-a",
        node_id="node-a",
        run_id="run-replay",
    )
    failed = store.record_attempt(
        conn,
        item["work_id"],
        action="failed",
        expected_version=2,
        actor_id="node-a",
        node_id="node-a",
        run_id="run-replay",
    )
    replayed_failure = store.record_attempt(
        conn,
        item["work_id"],
        action="failed",
        expected_version=2,
        actor_id="node-a",
        node_id="node-a",
        run_id="run-replay",
    )

    assert replayed_start == started
    assert replayed_failure == failed
    assert failed["attempt_count"] == 1 and failed["version"] == 3
    assert [event["event_type"] for event in store.list_events(conn, item["work_id"])] == [
        "created",
        "execution-linked",
        "attempt-started",
        "attempt-failed",
    ]


def test_attempt_event_ceiling_refuses_new_unique_actions(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ITEM_MAX_ATTEMPT_EVENTS", 2)
    item = store.create_item(conn, {"title": "Bounded", "kind": "fleet"}, actor_id="admin")
    first = store.record_attempt(
        conn, item["work_id"], action="started", expected_version=1, actor_id="admin", run_id="run-1"
    )
    store.record_attempt(
        conn, item["work_id"], action="failed", expected_version=first["version"], actor_id="admin", run_id="run-2"
    )

    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.record_attempt(
            conn, item["work_id"], action="started", expected_version=3, actor_id="admin", run_id="run-3"
        )

    assert excinfo.value.code == "attempt-conflict"
    assert [event["event_type"] for event in store.list_events(conn, item["work_id"])] == [
        "created",
        "attempt-started",
        "attempt-failed",
    ]


def test_attempt_event_ceiling_uses_a_bounded_index_probe_for_legacy_events(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Legacy attempts", "kind": "fleet"}, actor_id="admin")
    recorded = store.record_attempt(
        conn,
        item["work_id"],
        action="started",
        expected_version=1,
        actor_id="admin",
        run_id="existing-run",
    )
    _legacy_events(conn, item["work_id"], 25_000, event_type="attempt-started")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        with pytest.raises(store.WorkloreConflict) as excinfo:
            _within_sqlite_budget(
                conn,
                lambda: store.record_attempt(
                    conn,
                    item["work_id"],
                    action="started",
                    expected_version=2,
                    actor_id="admin",
                    run_id="new-unique-run",
                ),
            )
    finally:
        conn.set_trace_callback(None)

    assert excinfo.value.code == "attempt-conflict"
    probe_reads = [statement for statement in statements if "work_events_attempt_work_seq" in statement]
    assert len(probe_reads) == 1
    assert "SELECT 1 FROM work_events INDEXED BY work_events_attempt_work_seq" in probe_reads[0]
    assert f"LIMIT 1 OFFSET {store.ITEM_MAX_ATTEMPT_EVENTS - 1}" in probe_reads[0]
    assert (
        store.record_attempt(
            conn,
            item["work_id"],
            action="started",
            expected_version=2,
            actor_id="admin",
            run_id="existing-run",
        )
        == recorded
    )


def test_reset_discards_its_run_id(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Reset", "kind": "fleet"}, actor_id="admin")

    store.record_attempt(
        conn, item["work_id"], action="reset", expected_version=1, actor_id="admin", run_id="run-do-not-persist"
    )

    assert store.list_events(conn, item["work_id"])[-1]["run_id"] is None


def test_node_failed_without_owned_fleet_run_is_forbidden(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "No run", "kind": "fleet"}, actor_id="admin")
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.record_attempt(
            conn,
            item["work_id"],
            action="failed",
            expected_version=1,
            actor_id="node-a",
            node_id="node-a",
            run_id="run-missing",
        )
    assert excinfo.value.code == "attempt-forbidden"


def test_list_items_source_filters_include_links(tmp_path):
    conn = _conn(tmp_path)
    native = store.create_item(conn, {"title": "Native only", "kind": "fleet"}, actor_id="admin")
    imported = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="list-1",
        observations=[
            {
                "external_key": "escoffier-labs/brigade#9",
                "link_type": "github",
                "title": "Linked issue",
                "source_policy": "eligible",
                "url": "https://github.com/escoffier-labs/brigade/issues/9",
                "external_updated_at": _BASE_REVISION,
            }
        ],
        actor_id="node-a",
        owner_node="node-a",
    )
    github_items = store.list_items(conn, source="github")
    native_items = store.list_items(conn, source="native")
    assert [item["work_id"] for item in github_items["items"]] == [imported["items"][0]["work_id"]]
    assert github_items["items"][0]["links"][0]["external_key"] == "escoffier-labs/brigade#9"
    assert [item["work_id"] for item in native_items["items"]] == [native["work_id"]]
    assert native_items["items"][0]["links"] == []
    events = store.list_all_events(conn)
    assert {event["event_type"] for event in events["events"]} >= {"created", "sync-observed"}


def test_filtered_item_listing_stops_at_its_candidate_budget_and_paginates_past_sparse_rows(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _legacy_items(conn, 25_000)
    conn.execute("UPDATE work_items SET status = 'ready', burn_eligible = 1")
    conn.commit()
    monkeypatch.setattr(store, "LIST_SCAN_MAX", 13, raising=False)

    sparse = _within_sqlite_budget(
        conn,
        lambda: store.list_items(conn, status="ready", kind="fleet", burn_eligible=True, source="github", limit=1),
    )
    assert sparse["items"] == []
    assert sparse["next_cursor"]

    conn.execute(
        "INSERT INTO work_links (link_id, work_id, link_type, external_key, source_policy, "
        "source_acceptance_json, synced_at) VALUES ('lnk-sparse', 'wl-legacy-000003', 'github', "
        "'example/repo#3', 'eligible', '[]', '00000000000000000003')"
    )
    conn.commit()
    monkeypatch.setattr(store, "LIST_SCAN_MAX", 2, raising=False)

    first = store.list_items(conn, status="ready", burn_eligible=True, source="github", limit=1)
    second = store.list_items(
        conn, status="ready", burn_eligible=True, source="github", limit=1, cursor=first["next_cursor"]
    )
    assert first["items"] == [] and first["next_cursor"]
    assert [item["work_id"] for item in second["items"]] == ["wl-legacy-000003"]
    assert second["next_cursor"]


def test_missing_source_sets_stale_and_appends_sync_stale(tmp_path):
    conn = _conn(tmp_path)
    observation = {
        "external_key": "escoffier-labs/brigade#8",
        "link_type": "github",
        "title": "Vanished",
        "source_policy": "eligible",
        "url": "https://github.com/escoffier-labs/brigade/issues/8",
        "external_updated_at": _BASE_REVISION,
    }
    first = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="stale-1",
        observations=[observation],
        actor_id="node-a",
        owner_node="node-a",
    )
    missing = dict(observation)
    missing["stale"] = True
    # Marking an identity stale changes the projection, so it is held to the same rule as
    # any other change: the observation has to advance the revision to be applied.
    missing["external_updated_at"] = "2026-01-02T00:00:00+00:00"
    store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="stale-2",
        observations=[missing],
        actor_id="node-a",
        owner_node="node-a",
    )
    work_id = first["items"][0]["work_id"]
    links = store.list_links(conn, work_id)
    assert links[0]["stale_at"] is not None
    assert "sync-stale" in [event["event_type"] for event in store.list_events(conn, work_id)]


def test_stale_source_does_not_exclude_an_item_with_another_eligible_non_stale_source(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_github_observation(81, acceptance=["done"])], key="k1")["items"][0]["work_id"]
    store.patch_item(
        conn,
        work_id,
        {"burn_eligible": True, "execution_mode": "agent"},
        expected_version=1,
        actor_id="admin",
    )
    store.transition(conn, work_id, to_status="ready", expected_version=2, actor_id="admin")
    conn.execute(
        "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, external_state, "
        "external_updated_at, source_policy, source_acceptance_json, synced_at, stale_at) "
        "VALUES ('lnk-eligible-sibling', ?, 'brigade', 'brigade#81', 'brigade#81', NULL, NULL, NULL, "
        "'eligible', '[]', '2999-01-01T00:00:00+00:00', NULL)",
        (work_id,),
    )
    conn.commit()
    stale = _github_observation(81, acceptance=["done"], stale=True)
    assert _import(conn, [stale], key="k2")["updated"] == 1

    queue = store.burn_queue(conn)

    assert [item["work_id"] for item in queue["items"]] == [work_id]
    assert queue["exclusions"]["source-policy"] == 0


def test_create_item_idempotency_is_scoped_to_operator_and_preserves_node(tmp_path):
    conn = _conn(tmp_path)
    body = {"title": "Retry native", "kind": "fleet"}
    first = store.create_item(
        conn,
        body,
        actor_id="ops-node",
        actor_type="operator",
        node_id="ops-node",
        idempotency_key="native-1",
    )
    replay = store.create_item(
        conn,
        body,
        actor_id="ops-node",
        actor_type="operator",
        node_id="ops-node",
        idempotency_key="native-1",
    )
    assert replay["work_id"] == first["work_id"]
    events = store.list_events(conn, first["work_id"])
    assert [event["event_type"] for event in events] == ["created"]
    assert events[0]["actor_type"] == "operator"
    assert events[0]["actor_id"] == "ops-node"
    assert events[0]["node_id"] == "ops-node"
    with pytest.raises(store.WorkloreConflict) as excinfo:
        store.create_item(
            conn,
            {"title": "Different payload", "kind": "fleet"},
            actor_id="ops-node",
            actor_type="operator",
            node_id="ops-node",
            idempotency_key="native-1",
        )
    assert excinfo.value.code == "import-conflict"
    other = store.create_item(
        conn,
        body,
        actor_id="admin",
        actor_type="operator",
        idempotency_key="native-1",
    )
    assert other["work_id"] != first["work_id"]
    stored = conn.execute(
        "SELECT actor_id, idempotency_key, work_id FROM work_create_keys ORDER BY actor_id"
    ).fetchall()
    assert [(row[0], row[1]) for row in stored] == [("admin", "native-1"), ("ops-node", "native-1")]


def _github_observation(number: int, **overrides):
    observation = {
        "external_key": f"escoffier-labs/brigade#{number}",
        "link_type": "github",
        "title": f"Issue {number}",
        "source_policy": "eligible",
        "url": f"https://github.com/escoffier-labs/brigade/issues/{number}",
        "external_updated_at": _BASE_REVISION,
    }
    observation.update(overrides)
    return observation


def _import(conn, observations, *, key, adapter="github:escoffier-labs"):
    return store.import_batch(
        conn,
        adapter_id=adapter,
        source_type="github",
        idempotency_key=key,
        observations=observations,
        actor_id="node-a",
        owner_node="node-a",
    )


def test_item_events_page_in_insertion_order_when_a_batch_shares_one_timestamp(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_github_observation(1)], key="k1")["items"][0]["work_id"]
    expected = [event["event_id"] for event in store.list_events(conn, work_id)]
    assert [event["event_type"] for event in store.list_events(conn, work_id)] == [
        "created",
        "linked",
        "sync-observed",
    ]
    occurred = {row[0] for row in conn.execute("SELECT occurred_at FROM work_events WHERE work_id = ?", (work_id,))}
    assert len(occurred) == 1, "the batch must share one timestamp for this test to mean anything"
    paged: list[str] = []
    cursor = None
    while True:
        page = store.list_events_page(conn, work_id, limit=1, cursor=cursor)
        paged.extend(event["event_id"] for event in page["events"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == expected
    assert all("seq" not in event for event in store.list_events_page(conn, work_id, limit=10)["events"])


def test_item_event_cursor_replay_is_stable(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_github_observation(2)], key="k1")["items"][0]["work_id"]
    first = store.list_events_page(conn, work_id, limit=2)
    replay = store.list_events_page(conn, work_id, limit=2, cursor=first["next_cursor"])
    assert replay == store.list_events_page(conn, work_id, limit=2, cursor=first["next_cursor"])
    assert [event["event_id"] for event in first["events"] + replay["events"]] == [
        event["event_id"] for event in store.list_events(conn, work_id)
    ]


def test_fleet_wide_events_break_shared_timestamp_ties_by_insertion_order(tmp_path):
    conn = _conn(tmp_path)
    _import(conn, [_github_observation(3)], key="k1")
    newest_first = [event["event_id"] for event in store.list_all_events(conn, limit=100)["events"]]
    assert newest_first == list(
        reversed([row[0] for row in conn.execute("SELECT event_id FROM work_events ORDER BY seq")])
    )
    paged: list[str] = []
    cursor = None
    while True:
        page = store.list_all_events(conn, limit=1, cursor=cursor)
        paged.extend(event["event_id"] for event in page["events"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == newest_first


def test_filtered_event_listing_stops_at_its_candidate_budget_and_paginates_past_sparse_rows(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Event ledger", "kind": "fleet"}, actor_id="admin")
    _legacy_events(conn, item["work_id"], 25_000)
    monkeypatch.setattr(store, "LIST_SCAN_MAX", 13, raising=False)

    sparse = _within_sqlite_budget(
        conn,
        lambda: store.list_all_events(conn, work_id=item["work_id"], event_type="missing", limit=1),
    )
    assert sparse["events"] == []
    assert sparse["next_cursor"]

    conn.execute(
        "UPDATE work_events SET event_type = 'target' WHERE work_id = ? AND event_id = 'evt-legacy-024997'",
        (item["work_id"],),
    )
    conn.commit()
    monkeypatch.setattr(store, "LIST_SCAN_MAX", 2, raising=False)

    first = store.list_all_events(conn, work_id=item["work_id"], event_type="target", limit=1)
    second = store.list_all_events(
        conn, work_id=item["work_id"], event_type="target", limit=1, cursor=first["next_cursor"]
    )
    assert first["events"] == [] and first["next_cursor"]
    assert [event["event_id"] for event in second["events"]] == ["evt-legacy-024997"]
    assert second["next_cursor"]


def test_event_pagination_rejects_a_cursor_that_lost_its_sequence(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Cursor", "kind": "fleet"}, actor_id="admin")
    stale = store._encode_cursor("item-events", {"occurred_at": "t", "event_id": "evt-1"})
    with pytest.raises(store.WorkloreValidationError) as excinfo:
        store.list_events_page(conn, item["work_id"], limit=1, cursor=stale)
    assert excinfo.value.code == "field-bound"


def test_unchanged_observation_refreshes_synced_at_without_events_or_version_churn(tmp_path):
    conn = _conn(tmp_path)
    observation = _github_observation(4)
    first = _import(conn, [observation], key="k1")
    work_id = first["items"][0]["work_id"]
    before_item = store.get_item(conn, work_id)
    before_link = store.list_links(conn, work_id)[0]
    conn.execute(
        "UPDATE work_links SET synced_at = ? WHERE link_id = ?", ("2000-01-01T00:00:00+00:00", before_link["link_id"])
    )
    conn.commit()
    events_before = store.list_events(conn, work_id)
    result = _import(conn, [observation], key="k2")
    after_link = store.list_links(conn, work_id)[0]
    assert result["created"] == 0 and result["updated"] == 0 and result["unchanged"] == 1
    assert after_link["synced_at"] > "2000-01-01T00:00:00+00:00"
    assert store.list_events(conn, work_id) == events_before
    assert store.get_item(conn, work_id)["version"] == before_item["version"]
    assert store.get_item(conn, work_id)["updated_at"] == before_item["updated_at"]


def test_oldest_eligible_link_is_authoritative_even_with_empty_acceptance(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_github_observation(5, acceptance=[])], key="k1")["items"][0]["work_id"]
    conn.execute(
        "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, external_state, "
        "external_updated_at, source_policy, source_acceptance_json, synced_at, stale_at) "
        "VALUES ('lnk-younger', ?, 'brigade', 'other#1', 'other#1', NULL, NULL, NULL, 'eligible', "
        "'[\"younger criterion\"]', '2999-01-01T00:00:00+00:00', NULL)",
        (work_id,),
    )
    conn.commit()
    assert store.get_item(conn, work_id)["effective_acceptance"] == []
    with pytest.raises(store.WorkloreValidationError) as excinfo:
        store.transition(
            conn,
            work_id,
            to_status="ready",
            expected_version=store.get_item(conn, work_id)["version"],
            actor_id="admin",
        )
    assert excinfo.value.code == "acceptance-required"


def test_eligible_adapter_link_stays_protected_from_operator_unlink(tmp_path):
    conn = _conn(tmp_path)
    imported = _import(conn, [_github_observation(6)], key="k1")
    work_id = imported["items"][0]["work_id"]
    link_id = store.list_links(conn, work_id)[0]["link_id"]
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.delete_link(conn, work_id, link_id, actor_id="admin")
    assert excinfo.value.code == "link-forbidden"
    assert len(store.list_links(conn, work_id)) == 1


def test_retired_adapter_link_unlinks_to_native_with_one_event_and_kept_schedule(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_github_observation(7)], key="k1")["items"][0]["work_id"]
    store.patch_item(
        conn,
        work_id,
        {
            "acceptance": ["stays put"],
            "burn_rank": 42,
            "burn_eligible": True,
            "execution_mode": "agent",
            "review_after": "2026-01-01T00:00:00+00:00",
            "spend_by": "2027-01-01T00:00:00+00:00",
        },
        expected_version=store.get_item(conn, work_id)["version"],
        actor_id="admin",
    )
    _import(
        conn,
        [_github_observation(7, source_policy="label-removed", external_updated_at="2026-01-02T00:00:00+00:00")],
        key="k2",
    )
    scheduled = store.get_item(conn, work_id)
    link_id = store.list_links(conn, work_id)[0]["link_id"]
    before = len(store.list_events(conn, work_id))
    store.delete_link(conn, work_id, link_id, actor_id="ops-node", actor_type="operator", node_id="ops-node")
    events = store.list_events(conn, work_id)
    assert len(events) == before + 1
    unlinked = events[-1]
    assert unlinked["event_type"] == "unlinked"
    assert unlinked["actor_type"] == "operator" and unlinked["actor_id"] == "ops-node"
    assert unlinked["detail"]["source_policy"] == "label-removed"
    assert unlinked["detail"]["became_native"] is True
    assert unlinked["detail"]["adapter_id"] == "github:escoffier-labs"
    assert store.list_links(conn, work_id) == []
    assert store.list_items(conn, source="native")["items"][0]["work_id"] == work_id
    assert store.list_items(conn, source="github")["items"] == []
    after = store.get_item(conn, work_id)
    for field in ("burn_rank", "review_after", "spend_by", "ready_at", "attempt_count"):
        assert after[field] == scheduled[field], field
    assert after["version"] == scheduled["version"] + 1


def test_unlinking_one_of_two_source_links_does_not_claim_native(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_github_observation(8)], key="k1")["items"][0]["work_id"]
    conn.execute(
        "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, external_state, "
        "external_updated_at, source_policy, source_acceptance_json, synced_at, stale_at) "
        "VALUES ('lnk-second', ?, 'brigade', 'repo#2', 'repo#2', NULL, NULL, NULL, 'eligible', '[]', "
        "'2999-01-01T00:00:00+00:00', NULL)",
        (work_id,),
    )
    conn.commit()
    _import(
        conn,
        [_github_observation(8, source_policy="closed", external_updated_at="2026-01-02T00:00:00+00:00")],
        key="k2",
    )
    github_link = next(link for link in store.list_links(conn, work_id) if link["link_type"] == "github")
    store.delete_link(conn, work_id, github_link["link_id"], actor_id="admin")
    unlinked = store.list_events(conn, work_id)[-1]
    assert "became_native" not in unlinked["detail"]
    assert [link["link_type"] for link in store.list_links(conn, work_id)] == ["brigade"]


def test_execution_link_authority_comes_from_the_admin_flag_not_the_actor_id(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Join run", "kind": "fleet"}, actor_id="admin")
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('node-a', 'run-1', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    with pytest.raises(store.WorkloreForbidden) as excinfo:
        store.link_execution(conn, item["work_id"], node_id="node-a", run_id="run-1", actor_id="admin")
    assert excinfo.value.code == "execution-mismatch"
    linked = store.link_execution(
        conn,
        item["work_id"],
        node_id="node-a",
        run_id="run-1",
        actor_id="admin",
        actor_type="operator",
        is_admin=True,
    )
    assert linked["external_key"] == "node-a/run-1"
    assert store.list_events(conn, item["work_id"])[-1]["actor_type"] == "operator"


def test_operator_node_execution_link_is_recorded_as_operator(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Join run", "kind": "fleet"}, actor_id="admin")
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES ('ops-node', 'run-9', 1, 'd', 'run.created', 't', 't')"
    )
    conn.commit()
    store.link_execution(
        conn,
        item["work_id"],
        node_id="ops-node",
        run_id="run-9",
        actor_id="ops-node",
        actor_type="operator",
    )
    event = store.list_events(conn, item["work_id"])[-1]
    assert event["event_type"] == "execution-linked"
    assert event["actor_type"] == "operator" and event["actor_id"] == "ops-node"


def test_exclusion_bucket_is_the_only_burn_rule_and_matches_the_queue(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Burnable", "kind": "fleet"}, actor_id="admin")
    work_id = item["work_id"]
    store.patch_item(
        conn,
        work_id,
        {"acceptance": ["done"], "burn_eligible": True, "execution_mode": "agent"},
        expected_version=1,
        actor_id="admin",
    )
    store.transition(conn, work_id, to_status="ready", expected_version=2, actor_id="admin")
    ready = store.get_item(conn, work_id)
    assert store.exclusion_bucket(ready, []) is None
    assert store.burn_queue(conn)["items"][0]["work_id"] == work_id
    assert store.exclusion_bucket(ready, ["label-removed"]) == "source-policy"
    assert store.exclusion_bucket({**ready, "attempt_count": 2}, []) == "attempt-limit"
    assert store.exclusion_bucket({**ready, "review_after": "2999-01-01T00:00:00Z"}, []) == "review-after"
    assert store.exclusion_bucket({**ready, "status": "captured"}, []) == "not-ready"


# --- adapter namespace ownership (Daybreak finding 2) ------------------------


def _cursor_owners(conn):
    return [tuple(row) for row in conn.execute("SELECT adapter_id, owner_node FROM work_sync_cursors")]


def test_empty_first_batch_does_not_reserve_the_adapter_namespace(tmp_path):
    conn = _conn(tmp_path)
    squat = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="squat-1",
        observations=[],
        actor_id="node-squatter",
        owner_node="node-squatter",
    )
    assert squat == {"created": 0, "updated": 0, "unchanged": 0, "refused": 0, "refused_details": [], "items": []}
    assert conn.execute("SELECT COUNT(*) FROM work_sync_cursors").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM work_import_keys").fetchone()[0] == 0
    real = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="real-1",
        observations=[_github_observation(1)],
        actor_id="node-a",
        owner_node="node-a",
    )
    assert real["created"] == 1
    assert _cursor_owners(conn) == [("github:escoffier-labs", "node-a")]


def test_empty_batch_from_the_owning_node_still_records_liveness(tmp_path):
    conn = _conn(tmp_path)
    _import(conn, [_github_observation(2)], key="k1")
    result = _import(conn, [], key="k2")
    assert result == {"created": 0, "updated": 0, "unchanged": 0, "refused": 0, "refused_details": [], "items": []}
    assert _cursor_owners(conn) == [("github:escoffier-labs", "node-a")]
    stored = conn.execute("SELECT COUNT(*) FROM work_import_keys WHERE idempotency_key = 'k2'").fetchone()
    assert stored[0] == 1


def test_adapter_id_must_be_namespaced_by_its_source_type(tmp_path):
    conn = _conn(tmp_path)
    for adapter_id, source_type in (
        ("brigade:escoffier-labs", "github"),
        ("github:escoffier-labs", "brigade"),
        ("escoffier-labs", "github"),
        ("github:", "github"),
        ("github:-bad", "github"),
    ):
        with pytest.raises(store.WorkloreValidationError) as excinfo:
            store.import_batch(
                conn,
                adapter_id=adapter_id,
                source_type=source_type,
                idempotency_key="ns-1",
                observations=[],
                actor_id="node-a",
                owner_node="node-a",
            )
        assert excinfo.value.code == "field-bound", adapter_id
    assert conn.execute("SELECT COUNT(*) FROM work_sync_cursors").fetchone()[0] == 0


def test_observation_link_type_must_match_the_batch_source_type(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(store.WorkloreValidationError) as excinfo:
        store.import_batch(
            conn,
            adapter_id="brigade:escoffier-labs",
            source_type="brigade",
            idempotency_key="mix-1",
            observations=[_github_observation(3)],
            actor_id="node-a",
            owner_node="node-a",
        )
    assert excinfo.value.code == "field-bound"
    assert conn.execute("SELECT COUNT(*) FROM work_links").fetchone()[0] == 0


def test_adapter_cannot_adopt_an_operator_managed_link(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Operator owned", "kind": "repo"}, actor_id="admin")
    store.add_link(
        conn,
        item["work_id"],
        {"link_type": "github", "external_key": "escoffier-labs/brigade#4"},
        actor_id="admin",
    )
    before = store.list_links(conn, item["work_id"])
    result = _import(conn, [_github_observation(4, title="Adapter takeover")], key="takeover-1")
    # The identity is refused, not adopted, and the refusal is named rather than inferred.
    assert (result["created"], result["updated"], result["unchanged"], result["refused"]) == (0, 0, 0, 1)
    assert result["refused_details"][0]["reason"] == store.REFUSED_OPERATOR_MANAGED
    assert store.list_links(conn, item["work_id"]) == before
    assert store.get_item(conn, item["work_id"])["title"] == "Operator owned"


def test_imported_identity_owner_node_mismatch_is_refused(tmp_path):
    conn = _conn(tmp_path)
    _import(conn, [_github_observation(5)], key="own-1")
    conn.execute(
        "UPDATE work_sync_cursors SET owner_node = 'node-b' WHERE adapter_id = ?",
        ("github:escoffier-labs",),
    )
    conn.commit()
    result = store.import_batch(
        conn,
        adapter_id="github:escoffier-labs",
        source_type="github",
        idempotency_key="own-2",
        observations=[_github_observation(5, title="Moved node")],
        actor_id="node-b",
        owner_node="node-b",
    )
    assert result["refused_details"][0]["reason"] == store.REFUSED_OWNER_NODE_MISMATCH
    assert store.list_items(conn)["items"][0]["title"] == "Issue 5"


# --- canonical observation validator (Daybreak finding 3) --------------------


def _rejects(conn, observation, *, code, key):
    with pytest.raises((store.WorkloreValidationError, store.WorkloreConflict)) as excinfo:
        _import(conn, [observation], key=key)
    assert excinfo.value.code == code, observation


def test_observation_validator_rejects_controls_private_paths_and_credentials(tmp_path):
    conn = _conn(tmp_path)
    many_deps = [{"type": "task", "id": str(index)} for index in range(51)]
    cases = (
        (_github_observation(10, title="bell \x07 title"), "field-bound"),
        (_github_observation(11, title="reset \x1b[2J title"), "field-bound"),
        (_github_observation(12, title="see /home/operator/notes"), "private-data"),
        (_github_observation(13, title="see /Users/operator/notes"), "private-data"),
        (_github_observation(14, title=r"see C:\Users\operator\notes"), "private-data"),
        (_github_observation(15, description="%USERPROFILE%\\secrets"), "private-data"),
        (_github_observation(16, display_ref="ghp_" + "a" * 36), "private-data"),
        (_github_observation(17, description="api_key=" + "A" * 24), "private-data"),
        (_github_observation(18, acceptance=["x" * 401]), "field-bound"),
        (_github_observation(19, acceptance=["ok"] * 21), "field-bound"),
        (_github_observation(20, url="https://user:pass@github.com/a/b/issues/20"), "private-data"),
        (_github_observation(21, url="http://github.com/a/b/issues/21"), "field-bound"),
        (_github_observation(22, url="https:///a/b/issues/22"), "field-bound"),
        (_github_observation(23, dependencies=many_deps), "field-bound"),
        (_github_observation(24, evidence_refs=[f"ref-{index}" for index in range(21)]), "field-bound"),
    )
    for index, (observation, code) in enumerate(cases):
        _rejects(conn, observation, code=code, key=f"bad-{index}")
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


def test_a_safe_link_cannot_be_updated_by_an_unsafe_observation(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_github_observation(25)], key="safe-1")["items"][0]["work_id"]
    before_item = store.get_item(conn, work_id)
    before_links = store.list_links(conn, work_id)
    unsafe = (
        (_github_observation(25, title="now at /home/operator/notes"), "private-data"),
        (_github_observation(25, title="now with \x1b]0;title\x07"), "field-bound"),
        (_github_observation(25, acceptance=["y" * 401]), "field-bound"),
        (_github_observation(25, url="https://operator:token@github.com/a/b/issues/25"), "private-data"),
    )
    for index, (observation, code) in enumerate(unsafe):
        _rejects(conn, observation, code=code, key=f"unsafe-{index}")
    assert store.get_item(conn, work_id) == before_item
    assert store.list_links(conn, work_id) == before_links


# --- bounded reads (burn pages, page-scoped links, storage ceilings) ---------


def _ready_item(conn, title, **patch):
    item = store.create_item(conn, {"title": title, "kind": "fleet"}, actor_id="admin")
    store.patch_item(
        conn,
        item["work_id"],
        {"acceptance": ["done"], "burn_eligible": True, "execution_mode": "agent", **patch},
        expected_version=1,
        actor_id="admin",
    )
    store.transition(conn, item["work_id"], to_status="ready", expected_version=2, actor_id="admin")
    return item["work_id"]


def _walk_burn(conn, **kwargs):
    seen: list[str] = []
    exclusions = {name: 0 for name in store.EXCLUSION_BUCKETS}
    cursor = None
    pages = 0
    while pages < 50:
        page = store.burn_queue(conn, cursor=cursor, **kwargs)
        pages += 1
        seen.extend(item["work_id"] for item in page["items"])
        for bucket, count in page["exclusions"].items():
            exclusions[bucket] += count
        cursor = page["next_cursor"]
        if cursor is None:
            return seen, exclusions, pages
    raise AssertionError("burn pagination did not terminate")


def test_burn_queue_pages_a_high_cardinality_ledger_without_losing_rows(tmp_path):
    conn = _conn(tmp_path)
    ready = {_ready_item(conn, f"Ready {index:03d}") for index in range(60)}
    for index in range(5):
        store.create_item(conn, {"title": f"Captured {index}", "kind": "fleet"}, actor_id="admin")
    first = store.burn_queue(conn)
    assert len(first["items"]) == store.BURN_PAGE_DEFAULT
    assert first["next_cursor"] is not None
    seen, exclusions, pages = _walk_burn(conn)
    assert sorted(seen) == sorted(ready)
    assert len(seen) == len(set(seen))
    assert exclusions["not-ready"] == 5
    assert pages > 1


def test_burn_page_size_is_bounded_and_the_cursor_is_validated(tmp_path):
    conn = _conn(tmp_path)
    for index in range(4):
        _ready_item(conn, f"Ready {index}")
    page = store.burn_queue(conn, limit=2)
    assert len(page["items"]) == 2 and page["next_cursor"] is not None
    rest = store.burn_queue(conn, limit=2, cursor=page["next_cursor"])
    assert len(rest["items"]) == 2 and rest["next_cursor"] is None
    for bad_limit in (0, 101, "many"):
        with pytest.raises(store.WorkloreValidationError):
            store.burn_queue(conn, limit=bad_limit)
    with pytest.raises(store.WorkloreValidationError):
        store.burn_queue(conn, cursor="not-a-cursor")
    with pytest.raises(store.WorkloreValidationError):
        store.burn_queue(conn, cursor=store.list_items(conn, limit=1)["next_cursor"])


def test_burn_scan_budget_stops_a_page_of_excluded_rows_and_hands_back_a_cursor(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "BURN_SCAN_MAX", 6)
    monkeypatch.setattr(store, "_BURN_SCAN_CHUNK", 4)
    for index in range(20):
        store.create_item(conn, {"title": f"Captured {index}", "kind": "fleet"}, actor_id="admin")
    page = store.burn_queue(conn)
    assert page["items"] == []
    assert sum(page["exclusions"].values()) == 6
    assert page["next_cursor"] is not None


def _link_reads(conn, read):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        result = read()
    finally:
        conn.set_trace_callback(None)
    return result, [statement for statement in statements if "FROM work_links" in statement]


def _assert_page_scoped_link_reads(reads, page_ids, off_page_ids):
    assert reads, "page read never consulted links"
    assert all("GROUP BY" not in statement and "work_id IN (" not in statement for statement in reads)
    assert all("WHERE work_id =" in statement and "LIMIT" in statement for statement in reads)
    assert all(any(f"work_id = '{work_id}'" in statement for work_id in page_ids) for statement in reads)
    assert all(work_id not in statement for statement in reads for work_id in off_page_ids)


def test_page_reads_load_links_only_for_the_current_page(tmp_path):
    conn = _conn(tmp_path)
    work_ids = [_ready_item(conn, f"Ready {index}") for index in range(6)]
    for read in (lambda: store.list_items(conn, limit=2), lambda: store.burn_queue(conn, limit=2)):
        page, reads = _link_reads(conn, read)
        page_ids = [item["work_id"] for item in page["items"]]
        _assert_page_scoped_link_reads(reads, page_ids, set(work_ids) - set(page_ids))


def test_recent_events_are_cut_by_sql_not_by_slicing_every_event(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Chatty", "kind": "fleet"}, actor_id="admin")
    work_id = item["work_id"]
    for version in range(1, 25):
        store.patch_item(conn, work_id, {"scope": f"scope-{version}"}, expected_version=version, actor_id="admin")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        recent = store.recent_events(conn, work_id)
    finally:
        conn.set_trace_callback(None)
    assert len(recent) == store.DETAIL_RECENT_EVENTS
    assert recent == store.list_events(conn, work_id)[-store.DETAIL_RECENT_EVENTS :]
    reads = [statement for statement in statements if "FROM work_events" in statement]
    assert reads and all("ORDER BY seq DESC LIMIT" in statement for statement in reads)


def _observation(number, *, title="Observed", updated_at=_BASE_REVISION):
    """One observation from a source that reports which revision it is speaking for.

    A revision is the ordinary case, because the hub will not write a projection without
    one. ``updated_at=None`` builds the unproven observation the guard refuses.
    """
    observation = {
        "external_key": f"escoffier-labs/brigade#{number}",
        "link_type": "github",
        "title": title,
        "source_policy": "eligible",
        "url": f"https://github.com/escoffier-labs/brigade/issues/{number}",
    }
    if updated_at is not None:
        observation["external_updated_at"] = updated_at
    return observation


def _import(conn, observations, *, key, adapter="github:escoffier-labs", node="node-a"):
    return store.import_batch(
        conn,
        adapter_id=adapter,
        source_type="github",
        idempotency_key=key,
        observations=observations,
        actor_id=node,
        owner_node=node,
    )


def test_adapter_storage_ceiling_refuses_new_identities_and_still_accepts_updates(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ADAPTER_IMPORT_MAX_LINKS", 1)
    assert _import(conn, [_observation(1)], key="first")["created"] == 1
    with pytest.raises(store.WorkloreConflict) as refused:
        _import(conn, [_observation(2)], key="second")
    assert refused.value.code == "import-conflict"
    renamed = _observation(1, title="Renamed", updated_at="2026-01-02T00:00:00+00:00")
    assert _import(conn, [renamed], key="third")["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM work_links").fetchone()[0] == 1


def test_owner_storage_ceiling_bounds_every_adapter_one_node_owns(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "OWNER_IMPORT_MAX_LINKS", 1)
    assert _import(conn, [_observation(1)], key="first")["created"] == 1
    with pytest.raises(store.WorkloreConflict) as refused:
        _import(conn, [_observation(2)], key="second", adapter="github:lidless-labs")
    assert refused.value.code == "import-conflict"


def test_link_owner_columns_are_backfilled_from_the_event_log_on_migration(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite 3.35")
    conn = _conn(tmp_path)
    work_id = _import(conn, [_observation(7)], key="first")["items"][0]["work_id"]
    conn.execute("DROP INDEX work_links_adapter")
    conn.execute("DROP INDEX work_links_owner")
    conn.execute("ALTER TABLE work_links DROP COLUMN adapter_id")
    conn.execute("ALTER TABLE work_links DROP COLUMN owner_node")
    conn.commit()
    store.ensure_schema(conn)
    conn.commit()
    assert conn.execute("SELECT adapter_id, owner_node FROM work_links").fetchone() == (
        "github:escoffier-labs",
        "node-a",
    )
    assert store._owning_adapter(conn, work_id, "github", "escoffier-labs/brigade#7") == (
        "github:escoffier-labs",
        "node-a",
    )
    with pytest.raises(store.WorkloreForbidden):
        _import(conn, [_observation(7)], key="other-node", node="node-b")


# --- native text validation (Daybreak finding 1) -----------------------------


_LEAKS = (
    ("private path", "/home/operator/notes"),
    ("credential", "ghp_" + "a" * 36),
)


def _create_bodies(leak):
    return [
        {"title": f"Fleet {leak}", "kind": "fleet", "description": f"see {leak}"},
        {"title": f"see {leak}", "kind": "fleet"},
        {"title": "Scoped", "kind": "fleet", "scope": leak},
        {"title": "Accepting", "kind": "fleet", "acceptance": ["ok", f"paste {leak}"]},
        {"title": "Blocked", "kind": "fleet", "blocker": f"waiting on {leak}"},
    ]


@pytest.mark.parametrize("label,leak", _LEAKS)
def test_native_create_rejects_private_data_in_every_durable_text_field(tmp_path, label, leak):
    conn = _conn(tmp_path)
    for body in _create_bodies(leak):
        with pytest.raises(store.WorkloreValidationError) as refused:
            store.create_item(conn, body, actor_id="admin")
        assert refused.value.code == "private-data", (label, body)
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


@pytest.mark.parametrize("label,leak", _LEAKS)
def test_native_patch_rejects_private_data_in_every_durable_text_field(tmp_path, label, leak):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Clean", "kind": "fleet"}, actor_id="admin")
    work_id = item["work_id"]
    patches = [
        {"title": f"see {leak}"},
        {"description": f"see {leak}"},
        {"scope": leak},
        {"acceptance": ["ok", f"paste {leak}"]},
        {"blocker": f"waiting on {leak}"},
    ]
    for patch in patches:
        with pytest.raises(store.WorkloreValidationError) as refused:
            store.patch_item(conn, work_id, patch, expected_version=1, actor_id="admin")
        assert refused.value.code == "private-data", (label, patch)
    after = store.get_item(conn, work_id)
    assert after["version"] == 1 and after["title"] == "Clean"


def test_native_text_keeps_its_existing_length_and_required_semantics(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(
        conn,
        {"title": "Empty optionals", "kind": "fleet", "description": "", "scope": "", "blocker": ""},
        actor_id="admin",
    )
    assert item["description"] == "" and item["scope"] == "" and item["blocker"] == ""
    patched = store.patch_item(conn, item["work_id"], {"scope": ""}, expected_version=1, actor_id="admin")
    assert patched["scope"] == ""
    for body, code in (
        ({"title": "", "kind": "fleet"}, "field-bound"),
        ({"title": "A" * (store.TITLE_MAX + 1), "kind": "fleet"}, "field-bound"),
        ({"kind": "fleet"}, "field-bound"),
        ({"title": "Long acceptance", "kind": "fleet", "acceptance": ["A" * 401]}, "field-bound"),
    ):
        with pytest.raises(store.WorkloreValidationError) as refused:
            store.create_item(conn, body, actor_id="admin")
        assert refused.value.code == code, body


# --- per-item link cardinality (Daybreak finding 2) --------------------------


def _run_row(conn, node_id="node-a", run_id="run-1", sequence=1):
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (node_id, run_id, sequence, "d", "run.created", "t", "t"),
    )
    conn.commit()


def _url_link(conn, work_id, number):
    return store.add_link(
        conn,
        work_id,
        {"link_type": "url", "external_key": f"https://example.com/{number}"},
        actor_id="admin",
    )


def test_item_link_ceiling_is_shared_by_operator_and_fleet_run_links(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ITEM_MAX_LINKS", 2)
    work_id = store.create_item(conn, {"title": "Linked", "kind": "fleet"}, actor_id="admin")["work_id"]
    _url_link(conn, work_id, 1)
    _url_link(conn, work_id, 2)
    with pytest.raises(store.WorkloreConflict) as refused:
        _url_link(conn, work_id, 3)
    assert refused.value.code == "link-conflict"
    assert "2 link ceiling" in str(refused.value)
    _run_row(conn)
    with pytest.raises(store.WorkloreConflict) as run_refused:
        store.link_execution(conn, work_id, node_id="node-a", run_id="run-1", actor_id="node-a")
    assert run_refused.value.code == "link-conflict"
    assert conn.execute("SELECT COUNT(*) FROM work_links WHERE work_id = ?", (work_id,)).fetchone()[0] == 2


def test_the_link_ceiling_is_per_item_not_per_ledger(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ITEM_MAX_LINKS", 1)
    first = store.create_item(conn, {"title": "First", "kind": "fleet"}, actor_id="admin")["work_id"]
    second = store.create_item(conn, {"title": "Second", "kind": "fleet"}, actor_id="admin")["work_id"]
    _url_link(conn, first, 1)
    _url_link(conn, second, 2)
    with pytest.raises(store.WorkloreConflict):
        _url_link(conn, first, 3)


def test_link_pages_walk_a_high_cardinality_item_with_a_stable_cursor(tmp_path):
    conn = _conn(tmp_path)
    work_id = store.create_item(conn, {"title": "Many links", "kind": "fleet"}, actor_id="admin")["work_id"]
    for number in range(7):
        _url_link(conn, work_id, number)
    seen = []
    cursor = None
    pages = 0
    while pages < 10:
        page = store.list_links_page(conn, work_id, limit=3, cursor=cursor)
        pages += 1
        assert len(page["links"]) <= 3
        seen.extend(link["link_id"] for link in page["links"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and pages == 3
    assert seen == [link["link_id"] for link in store.list_links(conn, work_id)]
    assert len(set(seen)) == 7


def test_link_page_rejects_a_bad_limit_or_cursor_and_an_unknown_item(tmp_path):
    conn = _conn(tmp_path)
    work_id = store.create_item(conn, {"title": "Paged", "kind": "fleet"}, actor_id="admin")["work_id"]
    for value in (0, 101, "many"):
        with pytest.raises(store.WorkloreValidationError):
            store.list_links_page(conn, work_id, limit=value)
    with pytest.raises(store.WorkloreValidationError) as refused:
        store.list_links_page(conn, work_id, cursor="not-a-cursor")
    assert refused.value.code == "field-bound"
    with pytest.raises(store.WorkloreNotFound):
        store.list_links_page(conn, "wl-missing")


def test_list_items_bounds_projected_links_and_reports_truncation(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ITEM_LINK_PROJECTION_MAX", 2)
    crowded = store.create_item(conn, {"title": "Crowded", "kind": "fleet"}, actor_id="admin")["work_id"]
    for number in range(5):
        _url_link(conn, crowded, number)
    quiet = store.create_item(conn, {"title": "Quiet", "kind": "fleet"}, actor_id="admin")["work_id"]
    _url_link(conn, quiet, 99)
    by_id = {item["work_id"]: item for item in store.list_items(conn)["items"]}
    assert len(by_id[crowded]["links"]) == 2 and by_id[crowded]["links_truncated"] is True
    assert len(by_id[quiet]["links"]) == 1 and by_id[quiet]["links_truncated"] is False
    assert [link["link_id"] for link in by_id[crowded]["links"]] == [
        link["link_id"] for link in store.list_links(conn, crowded)[:2]
    ]


def test_burn_and_detail_reads_summarize_links_instead_of_loading_them(tmp_path):
    conn = _conn(tmp_path)
    work_id = _ready_item(conn, "Crowded but burnable")
    for number in range(40):
        _url_link(conn, work_id, number)
    for read in (lambda: store.burn_queue(conn), lambda: store.get_item(conn, work_id)):
        _, reads = _link_reads(conn, read)
        assert reads, "read never consulted links"
        assert all("GROUP BY" not in statement and "work_id IN (" not in statement for statement in reads)
        assert all(f"WHERE work_id = '{work_id}'" in statement and "LIMIT 1" in statement for statement in reads)


def test_burn_policy_and_effective_acceptance_survive_a_crowded_item(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_observation(1)], key="first")["items"][0]["work_id"]
    store.patch_item(
        conn,
        work_id,
        {"burn_eligible": True, "execution_mode": "agent"},
        expected_version=1,
        actor_id="admin",
    )
    second = dict(_observation(1, updated_at="2026-01-02T00:00:00+00:00"), acceptance=["from source"])
    _import(conn, [second], key="second")
    for number in range(30):
        _url_link(conn, work_id, number)
    store.transition(conn, work_id, to_status="defining", expected_version=2, actor_id="admin")
    store.transition(conn, work_id, to_status="ready", expected_version=3, actor_id="admin")
    assert store.get_item(conn, work_id)["effective_acceptance"] == ["from source"]
    assert store.burn_queue(conn)["items"][0]["work_id"] == work_id
    store.patch_item(conn, work_id, {"acceptance": ["own"]}, expected_version=4, actor_id="admin")
    third = dict(
        _observation(1, updated_at="2026-01-03T00:00:00+00:00"),
        acceptance=["from source"],
        source_policy="closed",
    )
    _import(conn, [third], key="third")
    queue = store.burn_queue(conn)
    assert queue["items"] == [] and queue["exclusions"]["source-policy"] == 1


# --- adapter and owner storage beyond links (Daybreak finding 3) -------------


def _import_key_rows(conn, adapter="github:escoffier-labs"):
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT idempotency_key, owner_node FROM work_import_keys WHERE adapter_id = ? "
            "ORDER BY received_at ASC, rowid ASC",
            (adapter,),
        )
    ]


def test_repeated_unchanged_imports_expire_keys_past_the_adapter_window(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ADAPTER_IMPORT_KEY_RETENTION", 3)
    assert _import(conn, [_observation(1)], key="create")["created"] == 1
    events_after_create = conn.execute("SELECT COUNT(*) FROM work_events").fetchone()[0]
    for index in range(10):
        result = _import(conn, [_observation(1)], key=f"unchanged-{index}")
        assert (result["created"], result["updated"], result["unchanged"]) == (0, 0, 1)
    rows = _import_key_rows(conn)
    assert [key for key, _ in rows] == ["unchanged-7", "unchanged-8", "unchanged-9"]
    assert {owner for _, owner in rows} == {"node-a"}
    assert conn.execute("SELECT COUNT(*) FROM work_events").fetchone()[0] == events_after_create


def test_a_replay_inside_the_retention_window_is_still_exact(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ADAPTER_IMPORT_KEY_RETENTION", 3)
    first = _import(conn, [_observation(1), _observation(2)], key="batch")
    assert first["created"] == 2
    events = conn.execute("SELECT COUNT(*) FROM work_events").fetchone()[0]
    for index in range(2):
        _import(conn, [_observation(3 + index)], key=f"filler-{index}")
    replay = _import(conn, [_observation(1), _observation(2)], key="batch")
    assert replay["created"] == 2 and replay["updated"] == 0 and replay["unchanged"] == 0
    assert {item["external_key"] for item in replay["items"]} == {
        "escoffier-labs/brigade#1",
        "escoffier-labs/brigade#2",
    }
    assert conn.execute("SELECT COUNT(*) FROM work_events").fetchone()[0] == events + 6


def test_import_keys_expire_per_owner_across_every_adapter_it_owns(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "OWNER_IMPORT_KEY_RETENTION", 2)
    _import(conn, [_observation(1)], key="one", adapter="github:escoffier-labs")
    _import(conn, [_observation(2)], key="two", adapter="github:lidless-labs")
    _import(conn, [_observation(3)], key="three", adapter="github:solomonneas")
    remaining = [
        tuple(row)
        for row in conn.execute("SELECT adapter_id, idempotency_key FROM work_import_keys ORDER BY received_at ASC")
    ]
    assert remaining == [("github:lidless-labs", "two"), ("github:solomonneas", "three")]
    other = store.import_batch(
        conn,
        adapter_id="github:other-node",
        source_type="github",
        idempotency_key="kept",
        observations=[_observation(9)],
        actor_id="node-b",
        owner_node="node-b",
    )
    assert other["created"] == 1
    assert conn.execute("SELECT COUNT(*) FROM work_import_keys WHERE owner_node = 'node-b'").fetchone()[0] == 1


def test_changed_observations_expire_old_adapter_sync_events_only(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ADAPTER_SYNC_EVENT_RETENTION", 3)
    work_id = _import(conn, [_observation(1)], key="create")["items"][0]["work_id"]
    store.add_link(
        conn,
        work_id,
        {"link_type": "url", "external_key": "https://example.com/kept"},
        actor_id="admin",
    )
    for index in range(12):
        renamed = _observation(1, title=f"Renamed {index}", updated_at=f"2026-01-{index + 2:02d}T00:00:00+00:00")
        assert _import(conn, [renamed], key=f"change-{index}")["updated"] == 1
    types = [event["event_type"] for event in store.list_events(conn, work_id)]
    assert types.count("sync-observed") == 3
    assert types[:3] == ["created", "linked", "linked"]
    assert store.get_item(conn, work_id)["title"] == "Renamed 11"
    events = store.list_events(conn, work_id)
    assert [event["detail"]["adapter_id"] for event in events if event["event_type"] == "sync-observed"] == [
        "github:escoffier-labs"
    ] * 3


def test_adapter_event_retention_leaves_the_ownership_backfill_intact(tmp_path, monkeypatch):
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite 3.35")
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ADAPTER_SYNC_EVENT_RETENTION", 1)
    work_id = _import(conn, [_observation(7)], key="create")["items"][0]["work_id"]
    for index in range(5):
        _import(conn, [_observation(7, title=f"Renamed {index}")], key=f"change-{index}")
    conn.execute("DROP INDEX work_links_adapter")
    conn.execute("DROP INDEX work_links_owner")
    conn.execute("ALTER TABLE work_links DROP COLUMN adapter_id")
    conn.execute("ALTER TABLE work_links DROP COLUMN owner_node")
    conn.commit()
    store.ensure_schema(conn)
    conn.commit()
    assert store._owning_adapter(conn, work_id, "github", "escoffier-labs/brigade#7") == (
        "github:escoffier-labs",
        "node-a",
    )


def test_import_key_owner_is_backfilled_from_the_adapter_cursor_on_migration(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite 3.35")
    conn = _conn(tmp_path)
    _import(conn, [_observation(1)], key="legacy")
    conn.execute("DROP INDEX work_import_keys_adapter")
    conn.execute("DROP INDEX work_import_keys_owner")
    conn.execute("ALTER TABLE work_import_keys DROP COLUMN owner_node")
    conn.commit()
    store.ensure_schema(conn)
    conn.commit()
    assert _import_key_rows(conn) == [("legacy", "node-a")]


def test_unchanged_import_keeps_link_liveness_without_new_storage(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "ADAPTER_IMPORT_KEY_RETENTION", 2)
    work_id = _import(conn, [_observation(1)], key="create")["items"][0]["work_id"]
    baseline = store.list_links(conn, work_id)[0]
    versions = store.get_item(conn, work_id)["version"]
    for index in range(6):
        _import(conn, [_observation(1)], key=f"unchanged-{index}")
    refreshed = store.list_links(conn, work_id)[0]
    assert refreshed["synced_at"] >= baseline["synced_at"]
    assert {key: value for key, value in refreshed.items() if key != "synced_at"} == {
        key: value for key, value in baseline.items() if key != "synced_at"
    }
    assert store.get_item(conn, work_id)["version"] == versions
    assert conn.execute("SELECT COUNT(*) FROM work_import_keys").fetchone()[0] == 2


def _timed_observation(number, *, updated_at, state="open", title="Observed"):
    """One observation whose source reports when it last changed."""
    return {
        **_observation(number, title=title),
        "external_state": state,
        "external_updated_at": updated_at,
    }


def _expire_import_key(conn, key):
    """Drop one import key the way retention would, without waiting for 500 batches."""
    conn.execute("DELETE FROM work_import_keys WHERE idempotency_key = ?", (key,))
    conn.commit()


def _link_state(conn, work_id):
    link = store.list_links(conn, work_id)[0]
    return store.get_item(conn, work_id)["title"], link["external_state"], link["external_updated_at"]


def test_expired_import_key_replay_never_rolls_back_a_newer_observation(tmp_path):
    conn = _conn(tmp_path)
    older = _timed_observation(1, updated_at="2026-01-01T00:00:00+00:00", state="open", title="Older")
    newer = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="Newer")
    work_id = _import(conn, [older], key="A")["items"][0]["work_id"]
    _expire_import_key(conn, "A")
    assert _import(conn, [newer], key="B")["updated"] == 1
    replay = _import(conn, [older], key="A")
    assert (replay["created"], replay["updated"], replay["unchanged"], replay["refused"]) == (0, 0, 0, 1)
    assert replay["refused_details"][0]["reason"] == store.REFUSED_REVISION_STALE
    assert _link_state(conn, work_id) == ("Newer", "closed", "2026-02-01T00:00:00+00:00")


def test_a_refused_stale_replay_writes_nothing_at_all(tmp_path):
    conn = _conn(tmp_path)
    older = _timed_observation(1, updated_at="2026-01-01T00:00:00+00:00", state="open", title="Older")
    newer = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="Newer")
    work_id = _import(conn, [older], key="A")["items"][0]["work_id"]
    _expire_import_key(conn, "A")
    _import(conn, [newer], key="B")
    before_link = store.list_links(conn, work_id)[0]
    before_events = store.list_events(conn, work_id)
    before_version = store.get_item(conn, work_id)["version"]
    _import(conn, [older], key="A")
    assert store.list_links(conn, work_id)[0] == before_link
    assert store.list_events(conn, work_id) == before_events
    assert store.get_item(conn, work_id)["version"] == before_version


def test_an_exact_replay_of_the_newest_observation_is_applied_and_changes_nothing(tmp_path):
    conn = _conn(tmp_path)
    observation = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed")
    work_id = _import(conn, [observation], key="A")["items"][0]["work_id"]
    _expire_import_key(conn, "A")
    replay = _import(conn, [observation], key="A")
    assert (replay["created"], replay["updated"], replay["unchanged"]) == (0, 0, 1)
    assert _link_state(conn, work_id) == ("Observed", "closed", "2026-02-01T00:00:00+00:00")
    assert [event["event_type"] for event in store.list_events(conn, work_id)] == [
        "created",
        "linked",
        "sync-observed",
    ]


def test_an_observation_without_a_timestamp_cannot_displace_a_high_water(tmp_path):
    conn = _conn(tmp_path)
    timed = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="Timed")
    work_id = _import(conn, [timed], key="A")["items"][0]["work_id"]
    untimed = _observation(1, title="Untimed", updated_at=None)
    assert _import(conn, [untimed], key="B")["refused"] == 1
    assert _link_state(conn, work_id) == ("Timed", "closed", "2026-02-01T00:00:00+00:00")


def test_a_source_that_reports_no_revision_never_gets_a_projection_to_roll_back(tmp_path):
    conn = _conn(tmp_path)
    unproven = _observation(1, title="First", updated_at=None)
    result = _import(conn, [unproven], key="A")
    assert (result["created"], result["updated"], result["unchanged"], result["refused"]) == (0, 0, 0, 1)
    assert result["refused_details"][0]["reason"] == store.REFUSED_REVISION_MISSING
    assert store.list_items(conn)["items"] == []
    assert conn.execute("SELECT COUNT(*) FROM work_links").fetchone()[0] == 0


def test_an_unparseable_revision_never_gets_a_projection_to_roll_back(tmp_path):
    conn = _conn(tmp_path)
    garbage = _timed_observation(1, updated_at="not-a-timestamp", title="Garbage")
    result = _import(conn, [garbage], key="A")
    assert (result["created"], result["refused"]) == (0, 1)
    assert result["refused_details"][0]["reason"] == store.REFUSED_REVISION_MISSING
    assert store.list_items(conn)["items"] == []


def test_replay_cannot_roll_back_an_identity_whose_replay_reports_no_revision(tmp_path):
    conn = _conn(tmp_path)
    # A -> expire -> B -> replay A, where the replayed batch carries no revision at all.
    # Before the guard was made total this was the identity class that stayed
    # last-write-wins, so the replay overwrote B with A.
    first = _timed_observation(1, updated_at="2026-01-01T00:00:00+00:00", state="open", title="A")
    work_id = _import(conn, [first], key="A")["items"][0]["work_id"]
    _expire_import_key(conn, "A")
    second = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="B")
    assert _import(conn, [second], key="B")["updated"] == 1
    replay = _import(conn, [_observation(1, title="A", updated_at=None)], key="A")
    assert (replay["created"], replay["updated"], replay["unchanged"], replay["refused"]) == (0, 0, 0, 1)
    assert replay["refused_details"][0]["reason"] == store.REFUSED_REVISION_MISSING
    assert _link_state(conn, work_id) == ("B", "closed", "2026-02-01T00:00:00+00:00")


def test_replay_cannot_roll_back_an_identity_whose_replay_reports_an_unparseable_revision(tmp_path):
    conn = _conn(tmp_path)
    first = _timed_observation(1, updated_at="2026-01-01T00:00:00+00:00", state="open", title="A")
    work_id = _import(conn, [first], key="A")["items"][0]["work_id"]
    _expire_import_key(conn, "A")
    second = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="B")
    assert _import(conn, [second], key="B")["updated"] == 1
    garbage = _timed_observation(1, updated_at="not-a-timestamp", state="open", title="A")
    replay = _import(conn, [garbage], key="A")
    assert (replay["created"], replay["updated"], replay["unchanged"], replay["refused"]) == (0, 0, 0, 1)
    assert replay["refused_details"][0]["reason"] == store.REFUSED_REVISION_MISSING
    assert _link_state(conn, work_id) == ("B", "closed", "2026-02-01T00:00:00+00:00")


def test_a_missing_revision_is_refused_per_identity_and_the_valid_sibling_imports(tmp_path):
    conn = _conn(tmp_path)
    unproven = _observation(1, title="Unproven", updated_at=None)
    proven = _timed_observation(2, updated_at="2026-02-01T00:00:00+00:00", title="Proven")
    result = _import(conn, [unproven, proven], key="A")
    assert (result["created"], result["updated"], result["unchanged"], result["refused"]) == (1, 0, 0, 1)
    assert result["refused_details"] == [
        {"external_key": "escoffier-labs/brigade#1", "link_type": "github", "reason": store.REFUSED_REVISION_MISSING}
    ]
    titles = [item["title"] for item in store.list_items(conn)["items"]]
    assert titles == ["Proven"]


def _unguarded_legacy_link(conn, number, *, title="Legacy"):
    """One imported identity as a database written before ``source_version`` holds it.

    Its ``external_updated_at`` is NULL, so migration seeds it nothing and it is the only
    identity class that can still meet an observation carrying no revision.
    """
    item = store.create_item(conn, {"title": title, "kind": "repo"}, actor_id="admin")
    conn.execute(
        "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, "
        "external_state, external_updated_at, source_policy, source_acceptance_json, synced_at, "
        "stale_at, adapter_id, owner_node, source_version) "
        "VALUES (?, ?, 'github', ?, ?, ?, NULL, NULL, 'eligible', '[]', ?, NULL, ?, 'node-a', NULL)",
        (
            f"lnk-legacy-{number}",
            item["work_id"],
            f"escoffier-labs/brigade#{number}",
            f"escoffier-labs/brigade#{number}",
            f"https://github.com/escoffier-labs/brigade/issues/{number}",
            "2026-01-01T00:00:00+00:00",
            "github:escoffier-labs",
        ),
    )
    conn.commit()
    return item["work_id"]


def test_an_unproven_observation_that_changes_nothing_is_unchanged_not_refused(tmp_path):
    conn = _conn(tmp_path)
    # The one place a missing revision survives: it writes no projection, so there is no
    # state for a revision to protect, and refusing every re-poll of a settled legacy
    # identity would be noise rather than a rollback the operator needs to see.
    work_id = _unguarded_legacy_link(conn, 1, title="Legacy")
    result = _import(conn, [_observation(1, title="Legacy", updated_at=None)], key="A")
    assert (result["created"], result["updated"], result["unchanged"], result["refused"]) == (0, 0, 1, 0)
    assert store.get_item(conn, work_id)["title"] == "Legacy"


def test_an_unproven_observation_that_would_change_a_legacy_identity_is_refused(tmp_path):
    conn = _conn(tmp_path)
    work_id = _unguarded_legacy_link(conn, 1, title="Legacy")
    result = _import(conn, [_observation(1, title="Rewritten", updated_at=None)], key="A")
    assert (result["created"], result["updated"], result["unchanged"], result["refused"]) == (0, 0, 0, 1)
    assert result["refused_details"][0]["reason"] == store.REFUSED_REVISION_MISSING
    assert store.get_item(conn, work_id)["title"] == "Legacy"


def test_the_high_water_refuses_the_stale_and_still_admits_the_eligible(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", title="Newer")], key="A")[
        "items"
    ][0]["work_id"]
    stale = _timed_observation(1, updated_at="2026-01-01T00:00:00+00:00", title="Stale")
    assert _import(conn, [stale], key="B")["refused"] == 1
    eligible = _timed_observation(1, updated_at="2026-03-01T00:00:00+00:00", state="closed", title="Eligible")
    assert _import(conn, [eligible], key="C")["updated"] == 1
    assert _link_state(conn, work_id) == ("Eligible", "closed", "2026-03-01T00:00:00+00:00")
    assert _import(conn, [stale], key="D")["refused"] == 1
    assert store.get_item(conn, work_id)["title"] == "Eligible"


def test_offset_and_zulu_timestamps_compare_as_instants_not_as_text(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_timed_observation(1, updated_at="2026-02-01T00:00:00Z", title="Newer")], key="A")[
        "items"
    ][0]["work_id"]
    # 2026-01-31T23:00:00-05:00 is 04:00Z on the 1st: later than the high-water as an
    # instant, earlier as a string. The guard must read it as the instant it is.
    later = _timed_observation(1, updated_at="2026-01-31T23:00:00-05:00", title="Later")
    assert _import(conn, [later], key="B")["updated"] == 1
    assert store.get_item(conn, work_id)["title"] == "Later"


def test_the_source_high_water_stays_out_of_the_link_projection(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_timed_observation(1, updated_at="2026-02-01T00:00:00+00:00")], key="A")["items"][0][
        "work_id"
    ]
    link = store.list_links(conn, work_id)[0]
    assert "source_version" not in link
    assert "source_version" not in store.list_links_page(conn, work_id)["links"][0]
    assert "source_version" not in store.list_items(conn)["items"][0]["links"][0]


def test_the_high_water_column_is_bounded_by_the_links_it_rides_on(tmp_path):
    conn = _conn(tmp_path)
    for number in range(1, 6):
        _import(conn, [_timed_observation(number, updated_at="2026-02-01T00:00:00+00:00")], key=f"k{number}")
    links = conn.execute("SELECT COUNT(*) FROM work_links").fetchone()[0]
    stamped = conn.execute("SELECT COUNT(*) FROM work_links WHERE source_version IS NOT NULL").fetchone()[0]
    assert (links, stamped) == (5, 5)


def _legacy_links(conn, work_id, count, *, start=0):
    """Insert links straight into storage, the way a database written before ITEM_MAX_LINKS
    was enforced on write already holds them."""
    for index in range(start, start + count):
        conn.execute(
            "INSERT INTO work_links (link_id, work_id, link_type, external_key, display_ref, url, "
            "external_state, external_updated_at, source_policy, source_acceptance_json, synced_at, "
            "stale_at, adapter_id, owner_node, source_version) "
            "VALUES (?, ?, 'url', ?, ?, NULL, NULL, NULL, 'eligible', '[]', ?, NULL, NULL, NULL, NULL)",
            (f"lnk-legacy-{index:05d}", work_id, f"legacy-{index}", f"legacy-{index}", f"2026-01-01T00:{index:02d}:00"),
        )
    conn.commit()


def test_detail_and_burn_reads_stop_within_a_fixed_sqlite_work_budget_for_legacy_links(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    work_id = _ready_item(conn, "Legacy")
    _legacy_links(conn, work_id, 25_000)
    monkeypatch.setattr(store, "ITEM_LINK_PROJECTION_MAX", 5)

    def within_budget(read):
        callbacks = 0

        def stop_after_fixed_work():
            nonlocal callbacks
            callbacks += 1
            return 1 if callbacks > 80 else 0

        conn.set_progress_handler(stop_after_fixed_work, 100)
        try:
            return read()
        finally:
            conn.set_progress_handler(None, 0)

    page = within_budget(lambda: store.list_items(conn, limit=1))
    queue = within_budget(lambda: store.burn_queue(conn, limit=1))

    item = page["items"][0]
    assert len(item["links"]) == 5 and item["links_truncated"] is True
    assert [item["work_id"] for item in queue["items"]] == [work_id]


def test_an_item_inside_the_projection_budget_is_not_reported_as_truncated(tmp_path):
    conn = _conn(tmp_path)
    work_id = _ready_item(conn, "Small")
    _legacy_links(conn, work_id, 3)
    item = store.list_items(conn, limit=1)["items"][0]
    assert len(item["links"]) == 3 and item["links_truncated"] is False


def test_migration_prunes_import_keys_and_adapter_events_of_a_quiet_adapter(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_observation(1)], key="seed")["items"][0]["work_id"]
    for index in range(8):
        conn.execute(
            "INSERT INTO work_import_keys (adapter_id, idempotency_key, fingerprint, created, updated, "
            "unchanged, received_at, owner_node) VALUES ('github:quiet', ?, 'fp', 0, 0, 0, ?, 'node-a')",
            (f"quiet-{index}", f"2026-01-0{index + 1}T00:00:00+00:00"),
        )
    for index in range(8):
        conn.execute(
            "INSERT INTO work_events (work_id, event_id, event_type, from_status, to_status, actor_type, "
            "actor_id, node_id, run_id, detail_json, occurred_at, received_at, seq) "
            "VALUES (?, ?, 'sync-observed', 'captured', 'captured', 'adapter', 'node-a', 'node-a', NULL, "
            "'{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', ?)",
            (work_id, f"legacy-event-{index}", 1000 + index),
        )
    conn.commit()
    monkeypatch.setattr(store, "ADAPTER_IMPORT_KEY_RETENTION", 3)
    monkeypatch.setattr(store, "ADAPTER_SYNC_EVENT_RETENTION", 2)
    store.ensure_schema(conn)
    quiet = [
        row[0]
        for row in conn.execute(
            "SELECT idempotency_key FROM work_import_keys WHERE adapter_id = 'github:quiet' ORDER BY received_at ASC"
        )
    ]
    assert quiet == ["quiet-5", "quiet-6", "quiet-7"]
    kept = [event["event_type"] for event in store.list_events(conn, work_id)]
    assert kept.count("sync-observed") == 2
    assert kept[:2] == ["created", "linked"], "lifecycle events must survive the prune"


def test_migration_prunes_import_keys_per_owner_across_adapters(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    for index in range(6):
        conn.execute(
            "INSERT INTO work_import_keys (adapter_id, idempotency_key, fingerprint, created, updated, "
            "unchanged, received_at, owner_node) VALUES (?, ?, 'fp', 0, 0, 0, ?, 'node-a')",
            (f"github:org-{index}", f"key-{index}", f"2026-01-0{index + 1}T00:00:00+00:00"),
        )
    conn.commit()
    monkeypatch.setattr(store, "OWNER_IMPORT_KEY_RETENTION", 2)
    store.ensure_schema(conn)
    assert [row[0] for row in conn.execute("SELECT idempotency_key FROM work_import_keys ORDER BY received_at")] == [
        "key-4",
        "key-5",
    ]


def test_create_keys_are_one_identity_row_per_durable_item_and_cannot_grow_alone(tmp_path):
    conn = _conn(tmp_path)
    work_ids = []
    for index in range(5):
        item = store.create_item(
            conn, {"title": f"Native {index}", "kind": "fleet"}, actor_id="admin", idempotency_key=f"key-{index}"
        )
        work_ids.append(item["work_id"])
        # Every replay resolves to the same item and writes no second identity row.
        for _ in range(3):
            replay = store.create_item(
                conn, {"title": f"Native {index}", "kind": "fleet"}, actor_id="admin", idempotency_key=f"key-{index}"
            )
            assert replay["work_id"] == item["work_id"]
    keys = conn.execute("SELECT COUNT(*) FROM work_create_keys").fetchone()[0]
    items = conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
    assert keys == items == 5
    assert [row[0] for row in conn.execute("SELECT work_id FROM work_create_keys ORDER BY received_at")] == work_ids


def test_a_create_key_is_never_swept_because_expiring_it_would_permit_a_duplicate(tmp_path):
    conn = _conn(tmp_path)
    item = store.create_item(conn, {"title": "Native", "kind": "fleet"}, actor_id="admin", idempotency_key="once")
    store.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM work_create_keys").fetchone()[0] == 1
    replay = store.create_item(conn, {"title": "Native", "kind": "fleet"}, actor_id="admin", idempotency_key="once")
    assert replay["work_id"] == item["work_id"]
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
    # The row is pinned to its item, so no sweep can quietly free the key either.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM work_items WHERE work_id = ?", (item["work_id"],))


def test_creates_without_an_idempotency_key_write_no_create_key_row(tmp_path):
    conn = _conn(tmp_path)
    for index in range(4):
        store.create_item(conn, {"title": f"Anonymous {index}", "kind": "fleet"}, actor_id="admin")
    assert conn.execute("SELECT COUNT(*) FROM work_create_keys").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 4


# --- final sendback regressions ---------------------------------------------


def _v16_link_table(conn):
    """Reshape work_links the way a v16 database holds it: no per-identity high-water."""
    conn.execute(f"ALTER TABLE work_links DROP COLUMN {store._LINK_SOURCE_VERSION_COLUMN}")
    conn.execute("DELETE FROM work_schema_meta")
    conn.commit()


def test_a_migrated_v16_identity_bootstraps_a_high_water_from_its_stored_timestamp(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(
        conn, [_timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="Newer")], key="A"
    )["items"][0]["work_id"]
    _v16_link_table(conn)
    store.ensure_schema(conn)
    assert conn.execute("SELECT source_version FROM work_links").fetchone()[0] == "2026-02-01T00:00:00+00:00"
    older = _timed_observation(1, updated_at="2026-01-01T00:00:00+00:00", state="open", title="Older")
    assert _import(conn, [older], key="B")["refused"] == 1
    assert _link_state(conn, work_id) == ("Newer", "closed", "2026-02-01T00:00:00+00:00")


def test_a_migrated_identity_with_an_unparseable_timestamp_bootstraps_no_high_water(tmp_path):
    conn = _conn(tmp_path)
    # The hub no longer lets an unparseable revision into the ledger, so the only database
    # holding one is a v16 database that accepted it before the guard existed. Migration
    # must still seed nothing from it rather than storing text it cannot compare.
    work_id = _import(conn, [_timed_observation(1, updated_at="2026-02-01T00:00:00+00:00")], key="A")["items"][0][
        "work_id"
    ]
    conn.execute("UPDATE work_links SET external_updated_at = 'not-a-timestamp' WHERE work_id = ?", (work_id,))
    conn.commit()
    _v16_link_table(conn)
    store.ensure_schema(conn)
    assert conn.execute("SELECT source_version FROM work_links").fetchone()[0] is None


def test_an_equal_source_revision_with_a_changed_payload_is_refused(tmp_path):
    conn = _conn(tmp_path)
    first = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="open", title="First")
    work_id = _import(conn, [first], key="A")["items"][0]["work_id"]
    forged = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="Forged")
    result = _import(conn, [forged], key="B")
    assert (result["created"], result["updated"], result["unchanged"], result["refused"]) == (0, 0, 0, 1)
    assert _link_state(conn, work_id) == ("First", "open", "2026-02-01T00:00:00+00:00")


def test_equal_source_poll_preserves_an_operator_title_edit(tmp_path):
    conn = _conn(tmp_path)
    observation = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", title="Source title")
    work_id = _import(conn, [observation], key="A")["items"][0]["work_id"]
    store.patch_item(conn, work_id, {"title": "Operator title"}, expected_version=1, actor_id="admin")

    result = _import(conn, [observation], key="B")

    assert (result["updated"], result["unchanged"], result["refused"]) == (0, 1, 0)
    assert store.get_item(conn, work_id)["title"] == "Operator title"


def test_newer_source_title_update_advances_the_item_version(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(
        conn,
        [_timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", title="First title")],
        key="A",
    )["items"][0]["work_id"]

    result = _import(
        conn,
        [_timed_observation(1, updated_at="2026-02-02T00:00:00+00:00", title="Second title")],
        key="B",
    )

    assert (result["updated"], result["refused"]) == (1, 0)
    item = store.get_item(conn, work_id)
    assert (item["title"], item["version"]) == ("Second title", 2)


def test_newer_tombstone_keeps_the_last_known_external_state(tmp_path):
    conn = _conn(tmp_path)
    first = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="open")
    work_id = _import(conn, [first], key="A")["items"][0]["work_id"]
    missing = _timed_observation(1, updated_at="2026-02-02T00:00:00+00:00")
    missing.pop("external_state")
    missing["stale"] = True

    result = _import(conn, [missing], key="B")

    link = store.list_links(conn, work_id)[0]
    assert (result["updated"], link["external_state"], link["external_updated_at"]) == (
        1,
        "open",
        "2026-02-02T00:00:00+00:00",
    )
    assert link["stale_at"] is not None


def test_source_acceptance_ignores_newer_url_and_execution_links_after_an_unchanged_resync(tmp_path):
    conn = _conn(tmp_path)
    source = dict(_timed_observation(1, updated_at="2026-02-01T00:00:00+00:00"), acceptance=["source check"])
    work_id = _import(conn, [source], key="A")["items"][0]["work_id"]
    store.patch_item(
        conn,
        work_id,
        {"burn_eligible": True, "execution_mode": "agent"},
        expected_version=1,
        actor_id="admin",
    )
    store.transition(conn, work_id, to_status="defining", expected_version=2, actor_id="admin")
    store.transition(conn, work_id, to_status="ready", expected_version=3, actor_id="admin")
    source_link = store.list_links(conn, work_id)[0]
    conn.execute(
        "UPDATE work_links SET synced_at = ? WHERE link_id = ?", ("2000-01-01T00:00:00+00:00", source_link["link_id"])
    )
    conn.commit()
    _url_link(conn, work_id, "source-acceptance")
    _run_row(conn, run_id="source-acceptance")
    store.link_execution(conn, work_id, node_id="node-a", run_id="source-acceptance", actor_id="node-a")

    refreshed = _import(conn, [source], key="B")

    links = {link["link_type"]: link for link in store.list_links(conn, work_id)}
    assert refreshed["unchanged"] == 1
    assert links["github"]["synced_at"] > links["url"]["synced_at"]
    assert links["github"]["synced_at"] > links["fleet-run"]["synced_at"]
    assert store.get_item(conn, work_id)["effective_acceptance"] == ["source check"]
    assert [item["work_id"] for item in store.burn_queue(conn)["items"]] == [work_id]


def test_cross_adapter_identity_collision_refuses_only_that_identity(tmp_path):
    conn = _conn(tmp_path)
    _import(conn, [_observation(1, title="First adapter")], key="A")

    result = _import(
        conn,
        [_observation(1, title="Collides"), _observation(2, title="Sibling")],
        key="B",
        adapter="github:other",
    )

    assert (result["created"], result["refused"]) == (1, 1)
    assert result["refused_details"] == [
        {
            "external_key": "escoffier-labs/brigade#1",
            "link_type": "github",
            "reason": store.REFUSED_ADAPTER_IDENTITY_CONFLICT,
        }
    ]
    assert [item["title"] for item in store.list_items(conn)["items"]] == ["First adapter", "Sibling"]


def test_owner_node_mismatch_refuses_only_that_identity(tmp_path):
    conn = _conn(tmp_path)
    _import(conn, [_observation(1, title="Owned by node a")], key="A")
    conn.execute("UPDATE work_sync_cursors SET owner_node = 'node-b' WHERE adapter_id = ?", ("github:escoffier-labs",))
    conn.commit()

    result = _import(
        conn,
        [_observation(1, title="Node mismatch"), _observation(2, title="Sibling")],
        key="B",
        node="node-b",
    )

    assert (result["created"], result["refused"]) == (1, 1)
    assert result["refused_details"] == [
        {
            "external_key": "escoffier-labs/brigade#1",
            "link_type": "github",
            "reason": store.REFUSED_OWNER_NODE_MISMATCH,
        }
    ]
    assert [item["title"] for item in store.list_items(conn)["items"]] == ["Owned by node a", "Sibling"]


def test_a_missing_source_revision_after_a_high_water_is_refused_with_a_reason(tmp_path):
    conn = _conn(tmp_path)
    timed = _timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", state="closed", title="Timed")
    work_id = _import(conn, [timed], key="A")["items"][0]["work_id"]
    result = _import(conn, [_observation(1, title="Untimed", updated_at=None)], key="B")
    assert (result["unchanged"], result["refused"]) == (0, 1)
    assert result["refused_details"] == [
        {
            "external_key": "escoffier-labs/brigade#1",
            "link_type": "github",
            "reason": "source-revision-missing",
        }
    ]
    assert _link_state(conn, work_id) == ("Timed", "closed", "2026-02-01T00:00:00+00:00")


def test_a_replayed_import_key_reports_the_refusals_it_originally_counted(tmp_path):
    conn = _conn(tmp_path)
    _import(conn, [_timed_observation(1, updated_at="2026-02-01T00:00:00+00:00", title="Timed")], key="A")
    batch = [
        _observation(1, title="Untimed", updated_at=None),
        _timed_observation(2, updated_at="2026-02-01T00:00:00+00:00", title="Fresh"),
    ]
    first = _import(conn, batch, key="B")
    assert (first["created"], first["refused"]) == (1, 1)
    replay = _import(conn, batch, key="B")
    assert (replay["created"], replay["updated"], replay["unchanged"], replay["refused"]) == (1, 0, 0, 1)


def test_an_operator_identity_collision_refuses_only_that_observation(tmp_path):
    conn = _conn(tmp_path)
    native = store.create_item(conn, {"title": "Hand linked", "kind": "repo"}, actor_id="admin")
    store.add_link(
        conn,
        native["work_id"],
        {
            "link_type": "github",
            "external_key": "escoffier-labs/brigade#1",
            "url": "https://github.com/escoffier-labs/brigade/issues/1",
        },
        actor_id="admin",
    )
    result = _import(conn, [_observation(1, title="Collides"), _observation(2, title="Sibling")], key="A")
    assert (result["created"], result["refused"]) == (1, 1)
    assert result["refused_details"] == [
        {
            "external_key": "escoffier-labs/brigade#1",
            "link_type": "github",
            "reason": "operator-managed-identity",
        }
    ]
    assert store.get_item(conn, native["work_id"])["title"] == "Hand linked"
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 2


def test_an_operator_url_link_is_not_owned_by_an_adapter_sharing_its_external_key(tmp_path):
    conn = _conn(tmp_path)
    work_id = _import(conn, [_observation(1)], key="A")["items"][0]["work_id"]
    operator_link = store.add_link(
        conn,
        work_id,
        {
            "link_type": "url",
            "external_key": "escoffier-labs/brigade#1",
            "url": "https://example.invalid/tracker",
        },
        actor_id="admin",
    )
    # The github link with the same external_key is adapter owned and still eligible. That
    # must not reach across link types and freeze the operator's own url link.
    store.delete_link(conn, work_id, operator_link["link_id"], actor_id="admin")
    assert [link["link_type"] for link in store.list_links(conn, work_id)] == ["github"]


def test_an_operator_link_of_another_type_does_not_block_an_adapter_import(tmp_path):
    conn = _conn(tmp_path)
    native = store.create_item(conn, {"title": "Native", "kind": "repo"}, actor_id="admin")
    store.add_link(
        conn,
        native["work_id"],
        {
            "link_type": "url",
            "external_key": "escoffier-labs/brigade#1",
            "url": "https://example.invalid/tracker",
        },
        actor_id="admin",
    )
    result = _import(conn, [_observation(1, title="Imported")], key="A")
    assert (result["created"], result["refused"]) == (1, 0)


def _hub_run(conn, node_id, run_id, *, sequence):
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES (?, ?, ?, 'd', 'run.created', 't', 't')",
        (node_id, run_id, sequence),
    )
    conn.commit()


def test_repeated_fleet_runs_cannot_freeze_an_item_at_the_link_ceiling(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "FLEET_RUN_LINK_RETENTION", 3)
    work_id = store.create_item(conn, {"title": "Busy", "kind": "fleet"}, actor_id="admin")["work_id"]
    for index in range(8):
        _hub_run(conn, "node-a", f"run-{index}", sequence=index + 1)
        store.link_execution(conn, work_id, node_id="node-a", run_id=f"run-{index}", actor_id="node-a")
    keys = [link["external_key"] for link in store.list_links(conn, work_id)]
    assert len(keys) == 3
    assert set(keys) == {"node-a/run-5", "node-a/run-6", "node-a/run-7"}


def test_fleet_run_retention_leaves_operator_links_alone(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(store, "FLEET_RUN_LINK_RETENTION", 2)
    work_id = store.create_item(conn, {"title": "Busy", "kind": "fleet"}, actor_id="admin")["work_id"]
    store.add_link(conn, work_id, {"link_type": "url", "external_key": "https://example.invalid/a"}, actor_id="admin")
    for index in range(5):
        _hub_run(conn, "node-a", f"run-{index}", sequence=index + 1)
        store.link_execution(conn, work_id, node_id="node-a", run_id=f"run-{index}", actor_id="node-a")
    types = sorted(link["link_type"] for link in store.list_links(conn, work_id))
    assert types == ["fleet-run", "fleet-run", "url"]


def test_a_legacy_database_over_the_link_ceiling_starts_and_can_be_repaired(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    conn = fleet_hub.init_db(db)
    work_id = store.create_item(conn, {"title": "Legacy", "kind": "fleet"}, actor_id="admin")["work_id"]
    _legacy_links(conn, work_id, 6)
    conn.close()
    monkeypatch.setattr(store, "ITEM_MAX_LINKS", 4)
    started = fleet_hub.init_db(db)
    try:
        assert started.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        # Reads stay bounded by SQL rather than by trusting the ceiling.
        monkeypatch.setattr(store, "ITEM_LINK_PROJECTION_MAX", 2)
        item = store.list_items(started, limit=1)["items"][0]
        assert len(item["links"]) == 2 and item["links_truncated"] is True
        # New writes still enforce the ceiling.
        with pytest.raises(store.WorkloreConflict) as refused:
            store.add_link(
                started, work_id, {"link_type": "url", "external_key": "https://example.invalid/x"}, actor_id="admin"
            )
        assert refused.value.code == "link-conflict"
        # And the operator can delete back under it through the ordinary route.
        for link in store.list_links(started, work_id)[:3]:
            store.delete_link(started, work_id, link["link_id"], actor_id="admin")
        store.add_link(
            started, work_id, {"link_type": "url", "external_key": "https://example.invalid/x"}, actor_id="admin"
        )
        assert len(store.list_links(started, work_id)) == 4
    finally:
        started.close()


def test_a_legacy_over_ceiling_database_starts_with_worklore_disabled(tmp_path, monkeypatch):
    db = tmp_path / "disabled.db"
    conn = fleet_hub.init_db(db)
    work_id = store.create_item(conn, {"title": "Legacy", "kind": "fleet"}, actor_id="admin")["work_id"]
    _legacy_links(conn, work_id, 6)
    conn.close()
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "0")
    monkeypatch.setattr(store, "ITEM_MAX_LINKS", 4)
    started = fleet_hub.init_db(db)
    try:
        assert started.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    finally:
        started.close()


def _plan(conn, sql, params=()):
    return " ".join(str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params))


def test_the_item_page_and_the_sprint_log_read_from_named_indexes(tmp_path):
    conn = _conn(tmp_path)
    store.create_item(conn, {"title": "Indexed", "kind": "fleet"}, actor_id="admin")
    items_plan = _plan(conn, "SELECT work_id FROM work_items ORDER BY created_at ASC, work_id ASC LIMIT 1")
    assert "work_items_created_order" in items_plan and "TEMP B-TREE" not in items_plan
    events_plan = _plan(conn, "SELECT work_id FROM work_events ORDER BY occurred_at DESC, seq DESC LIMIT 1")
    assert "work_events_recent" in events_plan and "TEMP B-TREE" not in events_plan
    claim_plan = _plan(
        conn,
        "SELECT adapter_id FROM work_links WHERE work_id = ? AND link_type = ? AND external_key = ?",
        ("w", "github", "k"),
    )
    # The identity lookup resolves through the UNIQUE (link_type, external_key) constraint,
    # so ownership costs one indexed row read rather than a scan of the item's links.
    assert "USING INDEX" in claim_plan and "SCAN work_links" not in claim_plan
    retention_plan = _plan(
        conn,
        "SELECT seq FROM work_events INDEXED BY work_events_adapter_retention "
        "WHERE work_id = ? AND actor_type = 'adapter' AND event_type IN (?, ?)",
        ("w", "sync-observed", "sync-stale"),
    )
    assert "work_events_adapter_retention" in retention_plan
    assert "SCAN work_events" not in retention_plan
    attempt_plan = _plan(
        conn,
        "SELECT 1 FROM work_events INDEXED BY work_events_attempt_work_seq "
        "WHERE work_id = ? AND event_type IN (?, ?, ?) LIMIT 1 OFFSET ?",
        ("w", "attempt-started", "attempt-failed", "attempt-reset", store.ITEM_MAX_ATTEMPT_EVENTS - 1),
    )
    assert "work_events_attempt_work_seq" in attempt_plan
    assert "SCAN work_events" not in attempt_plan


def test_worklore_operations_are_explicitly_bound_by_the_store_facade():
    from brigade import worklore_store_operations as operations

    assert operations._core() is store


def test_the_legacy_quota_reconciliation_runs_once_not_on_every_start(tmp_path):
    conn = _conn(tmp_path)
    _import(conn, [_observation(1)], key="seed")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        store.ensure_schema(conn)
    finally:
        conn.set_trace_callback(None)
    assert not [statement for statement in statements if "ROW_NUMBER() OVER (PARTITION BY" in statement]


def test_fleet_hub_schema_is_v18_for_the_recoverable_link_ceiling(tmp_path):
    assert fleet_hub.SCHEMA_VERSION == 18
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(work_import_keys)")}
        assert "refused" in columns
        assert conn.execute("SELECT COUNT(*) FROM work_schema_meta").fetchone()[0] >= 1
    finally:
        conn.close()
