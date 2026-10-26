"""Ownership view: charters from policy joined to live hub rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from brigade import (
    fleet_hub,
    fleet_client_policy,
    fleet_hub_claims,
    fleet_hub_messages,
    fleet_hub_policy,
    fleet_hub_sessions,
    fleet_owners,
    fleet_policy,
)

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


def _delegation(
    conn,
    delegation_id,
    *,
    released=False,
    accepted=False,
    expires=None,
    reservation_expires=None,
    origin=NODE_A,
    target=NODE_B,
):
    stamp = NOW.isoformat()
    expires_at = (expires or NOW + timedelta(minutes=5)).isoformat()
    reservation_expires_at = (reservation_expires or expires or NOW + timedelta(minutes=5)).isoformat()
    conn.execute(
        "INSERT INTO fleet_reservations (reservation_id, decision_id, session_id, machine, seat, node_id, "
        "origin_node, state, accepted, expires_at, created_at, updated_at) "
        "VALUES (?, ?, 'sess', 'machine-1', 'seat-1', ?, ?, ?, ?, ?, ?, ?)",
        (
            f"res-{delegation_id}",
            f"dec-{delegation_id}",
            target,
            origin,
            "released" if released else "active",
            1 if accepted else 0,
            reservation_expires_at,
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

    def broken(_conn, **_kwargs):
        raise sqlite3.OperationalError("no such table: claims")

    monkeypatch.setattr(fleet_hub_claims, "list_active_claims", broken)
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


def test_a_renewed_reservation_keeps_its_delegation_listed(conn):
    _delegation(conn, "del-renewed", expires=NOW - timedelta(minutes=1), reservation_expires=NOW + timedelta(minutes=5))
    _delegation(conn, "del-lapsed", expires=NOW + timedelta(minutes=5), reservation_expires=NOW - timedelta(minutes=1))
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert _refs(view.unassigned) == [("delegation", "del-renewed")]


def test_an_accepted_reservation_stays_listed_past_its_expiry(conn):
    # Routing counts an accepted, unreleased reservation as occupying its machine
    # whatever its expiry (fleet_hub_routing._reservation_occupies), so the map must too.
    _delegation(conn, "del-running", accepted=True, reservation_expires=NOW - timedelta(minutes=30))
    _delegation(conn, "del-released", accepted=True, released=True)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert _refs(view.unassigned) == [("delegation", "del-running")]


def test_expired_delegations_do_not_crowd_live_ones_out_of_the_source_limit(conn, monkeypatch):
    monkeypatch.setattr(fleet_owners, "SOURCE_LIMIT", 2)
    for index in range(3):
        _delegation(conn, f"del-dead-{index}", expires=NOW - timedelta(minutes=1))
    _delegation(conn, "del-live", expires=NOW + timedelta(minutes=5))
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert _refs(view.unassigned) == [("delegation", "del-live")]
    assert view.truncated_sources == ()


def test_grokbot_jobs_are_skipped_entirely_without_the_grokbot_flag(conn, monkeypatch):
    _job(conn, "grokbot-1", "running")
    _session(conn, "sess-1")

    def broken(*_args):
        raise AssertionError("the grokbot source must not be read")

    monkeypatch.setattr(fleet_owners, "_grokbot_jobs", broken)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp(), include_grokbot=False)
    assert _refs(view.unassigned) == [("session", "sess-1")]
    assert view.unknown_sources == ()


def test_corrupt_stored_json_degrades_instead_of_raising(conn, monkeypatch):
    _claim(conn)

    def corrupt_policy(_conn):
        raise json.JSONDecodeError("Expecting value", "{", 1)

    def corrupt_sessions(_conn, **_kwargs):
        raise json.JSONDecodeError("Expecting value", "{", 1)

    monkeypatch.setattr(fleet_hub_policy, "current_policy", corrupt_policy)
    monkeypatch.setattr(fleet_hub_sessions, "list_sessions", corrupt_sessions)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.policy_error == "policy read failed: JSONDecodeError"
    assert view.unknown_sources == ("sessions",)
    assert _refs(view.unassigned) == [("claim", "example/repo")]


def test_a_source_that_reaches_the_limit_is_reported_as_truncated(conn, monkeypatch):
    monkeypatch.setattr(fleet_owners, "SOURCE_LIMIT", 2)
    for index in range(3):
        _claim(conn, target=f"example/repo-{index}", node=f"node-{index}-0001")
    for index in range(3):
        _job(conn, f"grokbot-{index}", "queued")
    _session(conn, "sess-1")
    _session(conn, "sess-2")
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.truncated_sources == ("claims", "grokbot-jobs")
    assert len([item for item in view.unassigned if item.kind == "claim"]) == 2
    assert len([item for item in view.unassigned if item.kind == "grokbot-job"]) == 2
    assert len([item for item in view.unassigned if item.kind == "session"]) == 2


def _holding(index):
    return fleet_owners.Holding(
        kind="claim",
        ref=f"example/repo-{index:03}",
        repo="example/repo",
        node_label="Node A",
        detail="",
        since=NOW.isoformat(),
        harness="claude",
    )


def test_payload_caps_each_holdings_list_and_reports_the_totals():
    total = fleet_owners.JSON_HOLDINGS_LIMIT + 5
    items = [_holding(index) for index in range(total)]
    overlaps = [fleet_owners.Overlap(item=item, owners=("alpha", "beta")) for item in items]
    view = fleet_owners.OwnersView(
        policy_version=2,
        policy_error=None,
        charters={"alpha": {"match": {"harnesses": ["claude"]}}, "beta": {"match": {"harnesses": ["claude"]}}},
        by_owner={"alpha": items, "beta": items[:3]},
        unassigned=items,
        overlaps=overlaps,
        enrolled_principals=frozenset(),
        unknown_sources=(),
        truncated_sources=("claims",),
    )
    payload = fleet_owners.view_payload(view)
    alpha, beta = payload["owners"]
    assert len(alpha["holdings"]) == fleet_owners.JSON_HOLDINGS_LIMIT and alpha["holdings_total"] == total
    assert len(beta["holdings"]) == 3 and beta["holdings_total"] == 3
    assert len(payload["unassigned"]) == fleet_owners.JSON_HOLDINGS_LIMIT and payload["unassigned_total"] == total
    assert len(payload["overlaps"]) == fleet_owners.JSON_HOLDINGS_LIMIT and payload["overlaps_total"] == total
    assert payload["truncated_sources"] == ["claims"]


def test_a_full_active_session_page_is_reported_as_truncated(conn, monkeypatch):
    # list_sessions stops at its own LIST_ACTIVE_LIMIT, so the loader never sees
    # an extra row; a full page has to count as possibly truncated.
    monkeypatch.setattr(fleet_hub_sessions, "LIST_ACTIVE_LIMIT", 2)
    _session(conn, "sess-1")
    _session(conn, "sess-2")
    _session(conn, "sess-3")
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.truncated_sources == ("sessions",)
    assert len([item for item in view.unassigned if item.kind == "session"]) == 2


def test_payload_stays_under_the_response_budget_when_many_owners_share_holdings():
    items = [_holding(index) for index in range(fleet_owners.JSON_HOLDINGS_LIMIT)]
    names = [f"owner-{index:02}" for index in range(60)]
    view = fleet_owners.OwnersView(
        policy_version=2,
        policy_error=None,
        charters={name: {"label": name, "match": {"harnesses": ["claude"]}} for name in names},
        by_owner={name: items for name in names},
        unassigned=items,
        overlaps=[fleet_owners.Overlap(item=item, owners=tuple(names)) for item in items],
        enrolled_principals=frozenset(),
        unknown_sources=(),
        truncated_sources=(),
    )
    payload = fleet_owners.view_payload(view)
    assert len(json.dumps(payload).encode()) <= fleet_owners.JSON_RESPONSE_BUDGET_BYTES
    assert fleet_owners.JSON_RESPONSE_BUDGET_BYTES < fleet_client_policy.MAX_POLICY_RESPONSE_BYTES
    assert len(payload["unassigned"]) == fleet_owners.JSON_HOLDINGS_LIMIT
    assert all(owner["holdings_total"] == len(items) for owner in payload["owners"])
    assert payload["owners"][-1]["holdings"] == []


@pytest.mark.parametrize("label", ["Owner", "雪" * 16 + ' "quoted"'])
def test_valid_principal_heavy_policy_stays_within_response_cap(conn, label):
    principals = ["p" * 123 + f"{index:04}" for index in range(64)]
    _save_owners(
        conn,
        {f"owner-{index:02}": {"label": label, "match": {"principals": principals}} for index in range(30)},
    )
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.policy_error is None and len(view.charters) == 30

    payload = fleet_owners.view_payload(view)

    assert len(json.dumps(payload).encode()) <= fleet_owners.JSON_RESPONSE_BUDGET_BYTES
    assert payload["owners_total"] == 30
    assert payload["truncated_owners"] == []
    assert len(payload["owners"]) == 30
    for owner in payload["owners"]:
        assert "principals" not in owner["match"]
        assert [principal["name"] for principal in owner["principals"]] == principals


def test_owner_metadata_uses_shared_budget_and_reports_omitted_charters(conn, monkeypatch):
    names = ["alpha", "beta", "gamma"]
    _save_owners(conn, {name: {"label": name, "summary": '雪"' * 64} for name in names})
    monkeypatch.setattr(fleet_owners, "JSON_RESPONSE_BUDGET_BYTES", 1300)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())

    payload = fleet_owners.view_payload(view)

    assert len(json.dumps(payload).encode()) <= 1300
    included = [owner["name"] for owner in payload["owners"]]
    assert 0 < len(included) < len(names)
    assert included == names[: len(included)]
    assert payload["owners_total"] == len(names)
    assert payload["truncated_owners"] == names[len(included) :]
    assert payload["unknown_sources"] == []
    assert list(view.charters) == names


def test_owners_fetches_only_a_bounded_active_claim_page(conn, monkeypatch):
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW.timestamp())
    monkeypatch.setattr(fleet_owners, "SOURCE_LIMIT", 2)
    for prefix in ("a-expired", "z-active"):
        for index in range(4):
            _claim(conn, target=f"{prefix}/{index}")
    conn.execute("UPDATE claims SET expires_at = ? WHERE target LIKE 'a-expired/%'", (NOW.timestamp(),))
    conn.commit()
    before = list(conn.iterdump())
    fetched = []

    def count_claim_rows(cursor, row):
        if len(row) == 16 and cursor.description[0][0] == "target":
            fetched.append(row[0])
        return row

    conn.row_factory = count_claim_rows
    conn.execute("PRAGMA query_only=ON")
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())

    assert fetched == ["z-active/0", "z-active/1", "z-active/2"]
    assert _refs(view.unassigned) == [("claim", "z-active/0"), ("claim", "z-active/1")]
    assert view.truncated_sources == ("claims",)
    assert view.unknown_sources == ()
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("limit", [-1, 0, True, 1.5, "2"])
def test_bounded_active_claim_read_rejects_invalid_limits(conn, limit):
    with pytest.raises(ValueError, match="positive integer"):
        fleet_hub_claims.list_active_claims(conn, now_epoch=NOW.timestamp(), limit=limit)


@pytest.mark.parametrize("damage", ["missing", "negative", "table"])
def test_bounded_active_claim_read_preserves_fence_refusal(conn, monkeypatch, damage):
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW.timestamp())
    _claim(conn)
    if damage == "missing":
        conn.execute("DELETE FROM claim_fences")
    elif damage == "negative":
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE claim_fences SET generation = -1")
    else:
        conn.execute("DROP TABLE claim_fences")
    conn.commit()
    before = list(conn.iterdump())
    conn.execute("PRAGMA query_only=ON")

    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())

    assert view.unknown_sources == ("claims",)
    assert view.unassigned == []
    with pytest.raises(fleet_hub.FleetHubError, match="fence"):
        fleet_hub_claims.list_claims(conn)
    assert list(conn.iterdump()) == before


def test_general_claim_listing_still_validates_expired_fences(conn, monkeypatch):
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW.timestamp())
    _claim(conn)
    conn.execute("UPDATE claims SET expires_at = ?", (NOW.timestamp(),))
    conn.execute("DELETE FROM claim_fences")
    conn.commit()

    assert fleet_hub_claims.list_active_claims(conn, now_epoch=NOW.timestamp(), limit=2) == []
    with pytest.raises(fleet_hub.FleetHubError, match="fence"):
        fleet_hub_claims.list_claims(conn)
