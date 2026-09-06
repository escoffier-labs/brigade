"""Fleet Hub policy authority: immutable revisions, CAS writes, and session receipts."""

from __future__ import annotations

import copy
import sqlite3

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_policy
from brigade.fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden


NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _document() -> dict:
    return {
        "schema": fleet_policy.POLICY_SCHEMA,
        "defaults": {"roles": {"impl": "seat-alpha"}, "data": {"allow_training": False}},
        "machines": {"worker-linux-1": {"os": "linux", "concurrency": 2}},
        "seats": {
            "seat-alpha": {"provider": "provider-a", "model": "model-a-1"},
            "seat-beta": {"provider": "provider-b", "model": "model-b-1"},
        },
        "consumers": {
            "consumer-one": {"reload": "refreshable"},
            "consumer-two": {"reload": "restart-required"},
        },
        "repositories": {"repo/public": {"privacy": "public"}},
    }


def test_init_db_seeds_a_safe_empty_authority(conn):
    current = fleet_hub_policy.current_policy(conn)
    assert current["revision"] == 1
    assert current["schema"] == fleet_policy.POLICY_SCHEMA
    assert current["document"] == fleet_policy.parse_document(fleet_policy.empty_document())
    assert current["parent_revision"] is None
    assert current["digest"] == fleet_policy.document_digest(current["document"])
    assert current["actor"] == "schema"


def test_schema_is_additive_and_leaves_legacy_model_policy_intact(conn, tmp_path):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "fleet_policy_revisions",
        "fleet_policy_meta",
        "fleet_policy_sessions",
        "fleet_policy_pending",
    } <= tables
    assert {"model_policy", "model_roster_meta", "run_preference"} <= tables

    conn.execute(
        "INSERT INTO model_policy (seat, provider, model, enabled, limit_count, notes, updated_at, reasoning) "
        "VALUES ('seat-alpha', 'provider-a', 'model-a-1', 1, 1, '', '2026-01-01T00:00:00+00:00', 'none')"
    )
    conn.commit()
    assert fleet_hub.list_model_policy(conn)[0]["seat"] == "seat-alpha"


def test_save_policy_appends_an_immutable_revision(conn):
    saved = fleet_hub_policy.save_policy(
        conn, _document(), expected_version=1, actor="operator", reason="first real policy"
    )
    assert saved["revision"] == 2
    assert saved["parent_revision"] == 1
    assert saved["actor"] == "operator"
    assert saved["reason"] == "first real policy"
    assert saved["digest"] == fleet_policy.document_digest(saved["document"])

    current = fleet_hub_policy.current_policy(conn)
    assert current["revision"] == 2
    assert current["document"]["seats"]["seat-alpha"]["model"] == "model-a-1"

    history = fleet_hub_policy.list_revisions(conn)
    assert [row["revision"] for row in history] == [2, 1]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fleet_policy_revisions (revision, schema, digest, document, actor, reason, created_at, "
            "parent_revision) VALUES (2, 'x', 'x', '{}', 'x', 'x', 'x', 1)"
        )


def test_save_policy_is_compare_and_swap(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    with pytest.raises(FleetHubConflict) as excinfo:
        fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="stale")
    assert "policy_revision_conflict" in str(excinfo.value)
    assert fleet_hub_policy.current_policy(conn)["revision"] == 2


def test_save_policy_rejects_malformed_documents_without_writing(conn):
    broken = _document()
    broken["seats"]["seat-alpha"]["speed"] = "fast"
    with pytest.raises(FleetHubError):
        fleet_hub_policy.save_policy(conn, broken, expected_version=1, actor="operator", reason="bad")
    assert fleet_hub_policy.current_policy(conn)["revision"] == 1
    assert conn.execute("SELECT COUNT(*) FROM fleet_policy_revisions").fetchone()[0] == 1
    assert conn.in_transaction is False


def test_save_policy_preserves_pins_by_refusing_an_implicit_change(conn):
    pinned = _document()
    pinned["seats"]["seat-alpha"]["pinned"] = True
    fleet_hub_policy.save_policy(conn, pinned, expected_version=1, actor="operator", reason="pin the seat")

    changed = copy.deepcopy(pinned)
    changed["seats"]["seat-alpha"]["model"] = "model-a-9"
    with pytest.raises(FleetHubError) as excinfo:
        fleet_hub_policy.save_policy(conn, changed, expected_version=2, actor="operator", reason="swap model")
    assert "pinned-seat-change" in str(excinfo.value)
    assert fleet_hub_policy.current_policy(conn)["revision"] == 2

    unpinned = copy.deepcopy(pinned)
    unpinned["seats"]["seat-alpha"]["pinned"] = False
    fleet_hub_policy.save_policy(conn, unpinned, expected_version=2, actor="operator", reason="unpin")
    changed_after_unpin = copy.deepcopy(unpinned)
    changed_after_unpin["seats"]["seat-alpha"]["model"] = "model-a-9"
    saved = fleet_hub_policy.save_policy(
        conn, changed_after_unpin, expected_version=3, actor="operator", reason="swap model"
    )
    assert saved["revision"] == 4


def test_preview_policy_is_dry_and_reports_diff_and_scope(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-1",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=2,
        digest=fleet_hub_policy.current_policy(conn)["digest"],
        source="startup",
    )

    proposed = _document()
    proposed["defaults"]["roles"]["impl"] = "seat-beta"
    preview = fleet_hub_policy.preview_policy(
        conn, proposed, expected_version=2, actor="operator", reason="switch impl"
    )
    assert preview["ok"] is True
    assert preview["current_revision"] == 2
    assert preview["next_revision"] == 3
    assert [item["path"] for item in preview["diff"]["changed"]] == ["defaults.roles.impl"]
    assert preview["affected"]["consumers"] == ["consumer-one", "consumer-two"]
    assert preview["affected"]["repositories"] == ["repo/public"]
    assert preview["affected"]["sessions"] == ["session-1"]

    assert fleet_hub_policy.current_policy(conn)["revision"] == 2
    assert conn.execute("SELECT COUNT(*) FROM fleet_policy_revisions").fetchone()[0] == 2


def test_preview_policy_reports_conflicts_and_validation_errors_without_raising(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")

    stale = fleet_hub_policy.preview_policy(conn, _document(), expected_version=1, actor="operator", reason="stale")
    assert stale["ok"] is False
    assert [error["code"] for error in stale["errors"]] == ["policy_revision_conflict"]

    broken = _document()
    broken["seats"]["seat-alpha"]["speed"] = "fast"
    invalid = fleet_hub_policy.preview_policy(conn, broken, expected_version=2, actor="operator", reason="bad")
    assert invalid["ok"] is False
    assert [error["code"] for error in invalid["errors"]] == ["invalid_document"]


def test_rollback_creates_a_new_monotonic_revision_and_never_rewinds(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    second = _document()
    second["defaults"]["roles"]["impl"] = "seat-beta"
    fleet_hub_policy.save_policy(conn, second, expected_version=2, actor="operator", reason="second")

    rolled = fleet_hub_policy.rollback_policy(
        conn, to_revision=2, expected_version=3, actor="operator", reason="restore first policy"
    )
    assert rolled["revision"] == 4
    assert rolled["parent_revision"] == 3
    assert rolled["rolled_back_from"] == 3
    assert rolled["restored_revision"] == 2
    assert rolled["document"]["defaults"]["roles"]["impl"] == "seat-alpha"
    assert [row["revision"] for row in fleet_hub_policy.list_revisions(conn)] == [4, 3, 2, 1]


def test_rollback_revalidates_current_hard_rules(conn):
    pinned = _document()
    fleet_hub_policy.save_policy(conn, pinned, expected_version=1, actor="operator", reason="first")
    now_pinned = copy.deepcopy(pinned)
    now_pinned["seats"]["seat-alpha"]["pinned"] = True
    now_pinned["seats"]["seat-alpha"]["model"] = "model-a-2"
    fleet_hub_policy.save_policy(conn, now_pinned, expected_version=2, actor="operator", reason="pin a new model")

    with pytest.raises(FleetHubError) as excinfo:
        fleet_hub_policy.rollback_policy(
            conn, to_revision=2, expected_version=3, actor="operator", reason="undo the pin"
        )
    # Restoring revision 2 would unpin seat-alpha *and* move it back to
    # model-a-1 in one step, which is exactly what a pin exists to refuse.
    assert "unpin-and-change" in str(excinfo.value)
    current = fleet_hub_policy.current_policy(conn)
    assert current["revision"] == 3
    assert current["document"]["seats"]["seat-alpha"]["model"] == "model-a-2"


def test_rollback_rejects_unknown_and_conflicting_versions(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    with pytest.raises(FleetHubError):
        fleet_hub_policy.rollback_policy(conn, to_revision=99, expected_version=2, actor="operator", reason="nope")
    with pytest.raises(FleetHubConflict):
        fleet_hub_policy.rollback_policy(conn, to_revision=1, expected_version=1, actor="operator", reason="stale")


def test_resolve_policy_uses_the_saved_document(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    current = fleet_hub_policy.current_policy(conn)
    resolved = fleet_hub_policy.resolve_policy(current["document"], "consumer-one", "repo/public")
    assert resolved["effective"]["roles"]["impl"] == "seat-alpha"
    assert resolved["sources"]["roles.impl"]["layer"] == "fleet-defaults"


def test_session_receipt_records_the_loaded_version_and_owner(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    current = fleet_hub_policy.current_policy(conn)
    receipt = fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-1",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )
    assert receipt["revision"] == 2
    assert receipt["digest"] == current["digest"]
    assert receipt["source"] == "startup"
    assert receipt["owner_node"] == NODE_A
    assert receipt["loaded_at"]
    assert receipt["refresh_state"] == "none"

    with pytest.raises(FleetHubForbidden):
        fleet_hub_policy.record_session_policy(
            conn,
            session_id="session-1",
            consumer="consumer-one",
            node_id=NODE_B,
            repo_identity="repo/public",
            revision=current["revision"],
            digest=current["digest"],
            source="startup",
        )


def test_session_receipt_rejects_a_digest_that_does_not_match_the_revision(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    with pytest.raises(FleetHubError):
        fleet_hub_policy.record_session_policy(
            conn,
            session_id="session-1",
            consumer="consumer-one",
            node_id=NODE_A,
            repo_identity="repo/public",
            revision=2,
            digest="sha256:" + "0" * 64,
            source="startup",
        )


def test_session_states_track_current_stale_refreshable_and_restart_required(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    current = fleet_hub_policy.current_policy(conn)
    for session_id, consumer in (("session-one", "consumer-one"), ("session-two", "consumer-two")):
        fleet_hub_policy.record_session_policy(
            conn,
            session_id=session_id,
            consumer=consumer,
            node_id=NODE_A,
            repo_identity="repo/public",
            revision=current["revision"],
            digest=current["digest"],
            source="startup",
        )
    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-legacy",
        consumer="consumer-unregistered",
        node_id=NODE_A,
        repo_identity=None,
        revision=None,
        digest=None,
        source="presence-backfill",
    )

    states = {row["session_id"]: row["state"] for row in fleet_hub_policy.list_session_states(conn)}
    assert states == {"session-one": "current", "session-two": "current", "session-legacy": "unknown"}

    second = _document()
    second["defaults"]["roles"]["impl"] = "seat-beta"
    fleet_hub_policy.save_policy(conn, second, expected_version=2, actor="operator", reason="second")

    states = {row["session_id"]: row["state"] for row in fleet_hub_policy.list_session_states(conn)}
    assert states["session-one"] == "refreshable"
    assert states["session-two"] == "restart-required"
    assert states["session-legacy"] == "unknown"


def test_refresh_acknowledgements_are_only_recorded_when_they_happen(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    first = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-one",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=first["revision"],
        digest=first["digest"],
        source="startup",
    )
    second = _document()
    second["defaults"]["roles"]["impl"] = "seat-beta"
    fleet_hub_policy.save_policy(conn, second, expected_version=2, actor="operator", reason="second")
    latest = fleet_hub_policy.current_policy(conn)

    requested = fleet_hub_policy.request_session_refresh(conn, "session-one", actor="operator")
    assert requested["refresh_state"] == "requested"
    row = next(item for item in fleet_hub_policy.list_session_states(conn) if item["session_id"] == "session-one")
    assert row["refresh_state"] == "requested"
    assert row["state"] != "current"
    assert row["revision"] == 2

    failed = fleet_hub_policy.acknowledge_session_refresh(
        conn, "session-one", node_id=NODE_A, state="failed", detail="provider busy"
    )
    assert failed["refresh_state"] == "failed"
    assert failed["revision"] == 2

    applied = fleet_hub_policy.acknowledge_session_refresh(
        conn,
        "session-one",
        node_id=NODE_A,
        state="applied",
        revision=latest["revision"],
        digest=latest["digest"],
    )
    assert applied["refresh_state"] == "applied"
    assert applied["revision"] == 3
    row = next(item for item in fleet_hub_policy.list_session_states(conn) if item["session_id"] == "session-one")
    assert row["state"] == "current"


def test_refresh_acknowledgements_are_owner_checked_and_validated(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    current = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-one",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )
    with pytest.raises(FleetHubForbidden):
        fleet_hub_policy.acknowledge_session_refresh(conn, "session-one", node_id=NODE_B, state="applied")
    with pytest.raises(FleetHubError):
        fleet_hub_policy.acknowledge_session_refresh(conn, "session-one", node_id=NODE_A, state="rebooted")
    with pytest.raises(FleetHubError):
        fleet_hub_policy.acknowledge_session_refresh(conn, "session-missing", node_id=NODE_A, state="applied")
    with pytest.raises(FleetHubError):
        fleet_hub_policy.acknowledge_session_refresh(
            conn, "session-one", node_id=NODE_A, state="applied", revision=99, digest=current["digest"]
        )


def test_mutations_reject_oversized_and_control_bearing_metadata(conn):
    with pytest.raises(FleetHubError):
        fleet_hub_policy.save_policy(
            conn, _document(), expected_version=1, actor="operator", reason="x" * (fleet_hub_policy.MAX_REASON + 1)
        )
    with pytest.raises(FleetHubError):
        fleet_hub_policy.save_policy(
            conn, _document(), expected_version=1, actor="oper\nator", reason="control character"
        )
    assert fleet_hub_policy.current_policy(conn)["revision"] == 1


def test_session_receipt_refuses_a_different_consumer_or_repo_on_the_same_id(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    current = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-shared",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )
    with pytest.raises(FleetHubError):
        fleet_hub_policy.record_session_policy(
            conn,
            session_id="session-shared",
            consumer="consumer-two",
            node_id=NODE_A,
            repo_identity="repo/public",
            revision=current["revision"],
            digest=current["digest"],
            source="startup",
        )
    with pytest.raises(FleetHubError):
        fleet_hub_policy.record_session_policy(
            conn,
            session_id="session-shared",
            consumer="consumer-one",
            node_id=NODE_A,
            repo_identity="repo/other",
            revision=current["revision"],
            digest=current["digest"],
            source="startup",
        )
    row = next(item for item in fleet_hub_policy.list_session_states(conn) if item["session_id"] == "session-shared")
    assert row["consumer"] == "consumer-one"
    assert row["repo_identity"] == "repo/public"


def test_request_refresh_does_not_commit_an_outer_transaction(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    current = fleet_hub_policy.current_policy(conn)
    fleet_hub_policy.record_session_policy(
        conn,
        session_id="session-one",
        consumer="consumer-one",
        node_id=NODE_A,
        repo_identity="repo/public",
        revision=current["revision"],
        digest=current["digest"],
        source="startup",
    )
    conn.execute("BEGIN IMMEDIATE")
    fleet_hub_policy.request_session_refresh(conn, "session-one", actor="operator")
    assert conn.in_transaction is True
    conn.rollback()
    row = next(item for item in fleet_hub_policy.list_session_states(conn) if item["session_id"] == "session-one")
    assert row["refresh_state"] == "none"


def test_pending_policy_rejects_nonfinite_values(conn):
    fleet_hub_policy.save_policy(conn, _document(), expected_version=1, actor="operator", reason="first")
    current = fleet_hub_policy.current_policy(conn)
    with pytest.raises(FleetHubError):
        fleet_hub_policy.record_pending_policy(
            conn,
            session_id="session-one",
            consumer="consumer-one",
            node_id=NODE_A,
            repo_identity="repo/public",
            origin="local",
            revision=current["revision"],
            digest=current["digest"],
            overrides={},
            override_reason=None,
            effective={"data": {"allow_training": float("nan")}},
            sources={},
        )
