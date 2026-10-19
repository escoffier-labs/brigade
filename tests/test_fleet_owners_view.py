"""Ownership view: charters from policy joined to live hub rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_claims, fleet_hub_messages, fleet_hub_policy, fleet_owners, fleet_policy

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NODE_A = "node-a-0001"
NODE_B = "node-b-0002"


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _save_owners(conn, owners):
    document = fleet_policy.empty_document()
    document["owners"] = owners
    revision = fleet_hub_policy.current_policy(conn)["revision"]
    fleet_hub_policy.save_policy(conn, document, expected_version=revision, actor="admin", reason="seed owners")


def _claim(conn, target="example/repo", node=NODE_A, **labels):
    request = {"action": "acquire", "target": target, "node_id": node, "holder": "holder-1", "conductor": "orch"}
    status, _ = fleet_hub.handle_claim(conn, {**request, **labels})
    assert status == 200


def _job(conn, job_id, state, claimant=NODE_A):
    stamp = NOW.isoformat()
    conn.execute(
        "INSERT INTO grokbot_jobs ("
        "job_id, role, repository, label, task_digest, idempotency_key_hash, state, item_revision, sequence, "
        "created_at, updated_at, queued_at, timeout_seconds, artifact_kind, owner_node, claimant_node"
        ") VALUES (?, 'implementation-worker', 'example/repo', 'Job', ?, ?, ?, 1, 1, ?, ?, ?, 900, 'draft-pr', "
        "'queue-owner', ?)",
        (job_id, "d" * 64, job_id.ljust(64, "f"), state, stamp, stamp, stamp, claimant),
    )


def _session(conn, session_id, harness="claude", node=NODE_A, expires=None):
    stamp = NOW.isoformat()
    conn.execute(
        "INSERT INTO interactive_sessions ("
        "node_id, harness, session_id, repo_identity, identity_scope, repo_label, checkout_path, branch, "
        "dirty_paths_json, dirty_truncated, state, started_at, heartbeat_at, ended_at, ttl_seconds, expires_at"
        ") VALUES (?, ?, ?, 'example/repo', 'fleet', 'repo', '/work/repo', 'main', '[]', 0, 'active', ?, ?, NULL, "
        "900, ?)",
        (node, harness, session_id, stamp, stamp, expires or NOW.timestamp() + 900),
    )


def _delegation(conn, delegation_id, *, released=False, expires=None, origin=NODE_A, target=NODE_B):
    stamp = NOW.isoformat()
    expires_at = (expires or NOW + timedelta(minutes=5)).isoformat()
    conn.execute(
        "INSERT INTO fleet_reservations (reservation_id, decision_id, session_id, machine, seat, node_id, "
        "origin_node, state, accepted, expires_at, created_at, updated_at) "
        "VALUES (?, ?, 'sess', 'machine-1', 'seat-1', ?, ?, ?, 1, ?, ?, ?)",
        (
            f"res-{delegation_id}",
            f"dec-{delegation_id}",
            target,
            origin,
            "released" if released else "active",
            expires_at,
            stamp,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO fleet_policy_delegations (delegation_id, parent_request_id, origin_node, decision_id, "
        "reservation_id, session_id, consumer, adapter, repo_identity, seat, workload, policy_version, "
        "policy_digest, target_machine, target_node, source_revision, parent_metadata, created_at, expires_at, "
        "document) VALUES (?, ?, ?, ?, ?, 'sess', 'brigade-run', 't3-fleet', 'example/repo', 'seat-1', 'build', 1, "
        "'sha256:x', 'machine-1', ?, ?, NULL, ?, ?, '{}')",
        (
            delegation_id,
            f"parent-{delegation_id}",
            origin,
            f"dec-{delegation_id}",
            f"res-{delegation_id}",
            target,
            "a" * 40,
            stamp,
            expires_at,
        ),
    )


def _refs(items):
    return sorted((item.kind, item.ref) for item in items)


def test_without_charters_every_live_item_is_unassigned(conn):
    _claim(conn, harness="claude")
    _job(conn, "grokbot-1", "queued")
    _session(conn, "sess-1")
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.policy_version == 1 and view.policy_error is None
    assert view.charters == {} and view.unknown_sources == ()
    assert _refs(view.unassigned) == [("claim", "example/repo"), ("grokbot-job", "grokbot-1"), ("session", "sess-1")]


def test_live_rows_from_all_four_sources_reach_their_owners(conn):
    fleet_hub.add_node(conn, NODE_A, "Node A")
    _save_owners(
        conn,
        {
            "grokbot": {"label": "Grok Bot", "match": {"grokbot_jobs": True}},
            "builders": {"label": "Builders", "match": {"harnesses": ["claude"]}},
            "dot": {"label": "Dot", "match": {"session_harnesses": ["dot"]}},
            "t3": {"label": "T3", "match": {"adapters": ["t3-fleet"]}},
        },
    )
    _claim(conn, harness="claude", role="impl")
    _job(conn, "grokbot-1", "running")
    _session(conn, "sess-dot", harness="dot")
    _session(conn, "sess-other", harness="codex")
    _delegation(conn, "del-1")
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert _refs(view.by_owner["grokbot"]) == [("grokbot-job", "grokbot-1")]
    assert _refs(view.by_owner["builders"]) == [("claim", "example/repo")]
    assert _refs(view.by_owner["dot"]) == [("session", "sess-dot")]
    assert _refs(view.by_owner["t3"]) == [("delegation", "del-1")]
    assert _refs(view.unassigned) == [("session", "sess-other")]
    claim = view.by_owner["builders"][0]
    assert claim.node_label == "Node A" and claim.detail == "impl claude"
    job = view.by_owner["grokbot"][0]
    assert job.node_label == "Node A" and job.detail == "implementation-worker running"
    assert view.by_owner["t3"][0].node_label == NODE_B[:8]
    assert view.by_owner["t3"][0].detail == "seat-1 build"


def test_finished_and_expired_rows_are_not_holdings(conn):
    _claim(conn, target="expired/repo")
    conn.execute("UPDATE claims SET expires_at = 0 WHERE target = 'expired/repo'")
    _job(conn, "grokbot-done", "completed")
    _job(conn, "grokbot-queued", "queued", claimant=None)
    _session(conn, "sess-old", expires=NOW.timestamp() - 1)
    _delegation(conn, "del-released", released=True)
    _delegation(conn, "del-expired", expires=NOW - timedelta(seconds=1))
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert _refs(view.unassigned) == [("grokbot-job", "grokbot-queued")]
    assert view.unassigned[0].nodes == ()


def test_enrolled_principals_exclude_revoked_ones(conn):
    fleet_hub_messages.add_principal(conn, {"name": "miller"})
    fleet_hub_messages.add_principal(conn, {"name": "retired"})
    conn.execute("UPDATE fleet_message_principals SET revoked_at = ? WHERE name = 'retired'", (NOW.isoformat(),))
    conn.commit()
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.enrolled_principals == frozenset({"miller"})


def test_policy_read_failure_keeps_live_items_and_names_the_failure(conn, monkeypatch):
    _claim(conn)

    def broken(_conn):
        raise fleet_hub.FleetHubError("revision metadata is missing")

    monkeypatch.setattr(fleet_hub_policy, "current_policy", broken)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.policy_version is None
    assert view.policy_error == "policy read failed: FleetHubError"
    assert view.charters == {}
    assert _refs(view.unassigned) == [("claim", "example/repo")]


def test_a_failing_source_is_marked_unknown_and_the_others_still_load(conn, monkeypatch):
    _job(conn, "grokbot-1", "queued")

    def broken(_conn):
        raise sqlite3.OperationalError("no such table: claims")

    monkeypatch.setattr(fleet_hub_claims, "list_claims", broken)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.unknown_sources == ("claims",)
    assert _refs(view.unassigned) == [("grokbot-job", "grokbot-1")]


def test_loading_the_view_writes_nothing(conn):
    _save_owners(conn, {"alpha": {"label": "Alpha", "match": {"harnesses": ["claude"]}}})
    _claim(conn, harness="claude")
    _job(conn, "grokbot-1", "queued")
    _session(conn, "sess-1")
    conn.commit()
    before = list(conn.iterdump())
    conn.execute("PRAGMA query_only=ON")
    fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert not conn.in_transaction
    assert list(conn.iterdump()) == before


def test_payload_is_the_v1_json_shape(conn):
    fleet_hub_messages.add_principal(conn, {"name": "miller"})
    _save_owners(
        conn,
        {
            "zeta": {"label": "Zeta", "summary": "Quiet.", "owns": ["Notes"], "never": ["Deploys"]},
            "alpha": {
                "label": "Alpha",
                "contact": "agent:alpha",
                "match": {"harnesses": ["claude"], "principals": ["miller", "ghost"]},
            },
            "beta": {"label": "Beta", "match": {"nodes": [NODE_A]}},
        },
    )
    _claim(conn, harness="claude")
    payload = fleet_owners.view_payload(fleet_owners.load_view(conn, now_epoch=NOW.timestamp()))
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schema"] == "brigade.fleet_owners.v1"
    assert payload["policy_version"] == 2 and payload["policy_error"] is None
    assert [owner["name"] for owner in payload["owners"]] == ["alpha", "beta", "zeta"]
    alpha, beta, zeta = payload["owners"]
    assert alpha["contact"] == "agent:alpha" and alpha["charter_only"] is False
    assert alpha["principals"] == [{"name": "miller", "enrolled": True}, {"name": "ghost", "enrolled": False}]
    assert zeta["charter_only"] is True and zeta["summary"] == "Quiet." and zeta["never"] == ["Deploys"]
    assert alpha["holdings"][0]["ref"] == beta["holdings"][0]["ref"] == "example/repo"
    assert payload["unassigned"] == []
    assert payload["overlaps"] == [
        {**alpha["holdings"][0], "owners": ["alpha", "beta"]},
    ]
    assert payload["unknown_sources"] == []
