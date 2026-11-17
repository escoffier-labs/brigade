"""Owner liveness derivation: states, precedence, privacy, writes, and readers."""

import base64
import json
from datetime import datetime

import pytest

from brigade import fleet_hub, worklore_http as http
from brigade import worklore_liveness as liveness
from brigade import worklore_ownership as ownership
from brigade import worklore_prepared_handoff as prepared
from brigade import worklore_store as store
from brigade.worklore_validate import WorkloreValidationError

NOW = "2026-01-01T00:00:00+00:00"
NONCE = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
OFFER = {
    "action": "offer",
    "target_node": "node-a",
    "exclusions": [],
    "authorization_ref": "grant-1",
    "attempt_budget": {"cap": 1, "source_ref": "budget-1"},
}


@pytest.fixture
def ledger(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    for node in ("node-a", "node-b"):
        fleet_hub.add_node(conn, node, node)
    item = store.create_item(conn, {"title": "Fixture", "kind": "repo"}, actor_id="admin", actor_type="operator")
    yield conn, item["work_id"]
    conn.close()


def owned(conn, work_id):
    ownership.ownership_action(
        conn,
        work_id,
        OFFER,
        expected_revision=0,
        idempotency_key="offer",
        actor_id="admin",
        actor_type="operator",
        is_admin=True,
    )
    ownership.ownership_action(
        conn,
        work_id,
        {"action": "accept", "generation": 0},
        expected_revision=1,
        idempotency_key="accept",
        actor_id="node-a",
        actor_type="node",
        holder_nonce=NONCE,
    )


def _epoch(stamp):
    return datetime.fromisoformat(stamp).timestamp()


def session(conn, *, state="active", ended=None, stamp=NOW):
    conn.execute(
        "INSERT INTO interactive_sessions (node_id, harness, session_id, repo_identity, identity_scope, "
        "repo_label, checkout_path, branch, dirty_paths_json, dirty_truncated, state, started_at, "
        "heartbeat_at, ended_at, ttl_seconds, expires_at) VALUES "
        "('node-a','claude','PRIVATE-SESSION-ID','example/project','node','example','/tmp/work',NULL,'[]',0,?,?,?,?,900,?)",
        (state, stamp, stamp, ended, _epoch(stamp) + 900),
    )
    conn.commit()


def claim(conn, stamp=NOW):
    conn.execute(
        "INSERT INTO claims (target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
        "VALUES ('PRIVATE-CLAIM-TARGET','node-a','PRIVATE-HOLDER-TOKEN',?,?,900,?)",
        (stamp, stamp, _epoch(stamp) + 900),
    )
    conn.commit()


def finish_run(conn, work_id, state="run.completed"):
    store._append_event(
        conn,
        work_id=work_id,
        event_type="execution-linked",
        from_status=None,
        to_status=None,
        actor_type="node",
        actor_id="node-a",
        node_id="node-a",
        run_id="run-a",
        now=NOW,
        detail={},
    )
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at) "
        "VALUES ('node-a','run-a',1,'d','example/project','seat','claude',?,?,?)",
        (state, NOW, NOW),
    )
    conn.commit()


def lapse_lease(conn, work_id):
    row = conn.execute(
        "SELECT seq, detail_json FROM work_events WHERE work_id=? AND event_type='ownership-accepted' "
        "ORDER BY seq DESC LIMIT 1",
        (work_id,),
    ).fetchone()
    detail = json.loads(row[1])
    detail["ownership"]["expires_at"] = "2025-12-31T00:00:00+00:00"
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    conn.commit()


def read(conn, work_id):
    return ownership.get_ownership(conn, work_id, now=NOW)


@pytest.mark.parametrize(
    "setup,expected",
    [
        (lambda conn, work_id: session(conn), ("live-owner", "session")),
        (lambda conn, work_id: claim(conn), ("live-owner", "claim")),
        (lambda conn, work_id: session(conn, state="ended", ended=NOW), ("idle-owner", "session")),
        (lambda conn, work_id: lapse_lease(conn, work_id), ("expired-observation", "lease")),
        (lambda conn, work_id: finish_run(conn, work_id), ("terminal-execution", "run")),
    ],
    ids=["live-session", "live-claim", "ended-session", "lapsed-lease", "terminal-run"],
)
def test_each_liveness_state(ledger, setup, expected):
    conn, work_id = ledger
    owned(conn, work_id)
    setup(conn, work_id)
    before = conn.total_changes
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == expected
    assert conn.total_changes == before


@pytest.mark.parametrize("own", [False, True], ids=["unowned", "owned-no-facts"])
def test_unknown_without_owner_or_facts(ledger, own):
    conn, work_id = ledger
    if own:
        owned(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"], snapshot["liveness_freshness"]) == (
        "unknown",
        "none",
        "unknown",
    )


def test_unknown_for_terminal_item_even_with_live_session(ledger):
    conn, work_id = ledger
    owned(conn, work_id)
    session(conn)
    store.transition(conn, work_id, to_status="canceled", expected_version=1, actor_id="admin")
    conn.commit()
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


@pytest.mark.parametrize(
    "setup,expected",
    [
        (lambda conn, work_id: (session(conn), finish_run(conn, work_id, "run.failed")), ("live-owner", "session")),
        (lambda conn, work_id: (lapse_lease(conn, work_id), finish_run(conn, work_id)), ("terminal-execution", "run")),
    ],
    ids=["session-beats-run", "run-beats-lease"],
)
def test_precedence(ledger, setup, expected):
    conn, work_id = ledger
    owned(conn, work_id)
    setup(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == expected


@pytest.mark.parametrize("table", ["interactive_sessions", "claims"])
def test_missing_fact_table_returns_unknown_without_raising(ledger, table):
    conn, work_id = ledger
    owned(conn, work_id)
    conn.execute(f"DROP TABLE {table}")
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_get_response_names_only_state_basis_and_freshness(ledger):
    conn, work_id = ledger
    owned(conn, work_id)
    current = store._utc_now()
    session(conn, stamp=current)
    claim(conn, stamp=current)
    status, payload = http.handle(
        conn, http.Request(method="GET", path=f"/work/items/{work_id}/ownership", node_id="node-b")
    )
    assert status == 200
    snapshot = payload["ownership"]
    assert (snapshot["liveness"], snapshot["liveness_basis"], snapshot["liveness_freshness"]) == (
        "live-owner",
        "session",
        "recent",
    )
    serialized = json.dumps(payload)
    for marker in ("PRIVATE-SESSION-ID", "PRIVATE-CLAIM-TARGET", "PRIVATE-HOLDER-TOKEN", "pid", "claim_target"):
        assert marker not in serialized


def _prepared_view(state):
    return {
        "schema": "brigade.prepared_handoff.v1",
        "observed_at": NOW,
        "consistency": "single-db-snapshot",
        "visibility": "fleet-trust",
        "authority": "read-only-context",
        "person_attribution": "unrecorded",
        "historical_human_attribution": "unrecorded",
        "item": {
            "work_id": "work-fixture",
            "version": 1,
            "title": "Fixture",
            "kind": "repo",
            "status": "captured",
            "acceptance": [],
            "effective_acceptance": [],
            "blocker": None,
        },
        "ownership": {
            "state": "owned",
            "revision": 1,
            "generation": 1,
            "owner_node": "node-a",
            "offered_to": None,
            "last_seq": 2,
            "updated_at": NOW,
            "liveness": state,
        },
        "checkpoint": None,
        "last_report": None,
        "run_refs": {
            "items": [],
            "truncated": False,
            "coverage": "recorded-execution-events",
            "ownership_generation_binding": "unavailable",
        },
    }


def test_prepared_handoff_accepts_each_liveness_state():
    for state in sorted(liveness.LIVENESS_VALUES):
        view = prepared.validate_prepared_handoff(_prepared_view(state), "work-fixture")
        assert view["ownership"]["liveness"] == state


def test_prepared_handoff_refuses_other_liveness_words():
    for state in ("alive", "idle", "LIVE-OWNER", ""):
        with pytest.raises(WorkloreValidationError):
            prepared.validate_prepared_handoff(_prepared_view(state), "work-fixture")


def test_prepared_handoff_derives_liveness_and_report_freshness(ledger):
    conn, work_id = ledger
    owned(conn, work_id)
    session(conn)
    ownership.ownership_action(
        conn,
        work_id,
        {
            "action": "report",
            "generation": 1,
            "report": {
                "version": 1,
                "provider": "dot",
                "session_id": "session-a",
                "agent_label": "worker-a",
                "observed_at": NOW,
                "sequence": 1,
                "progress": "Checking",
                "evidence_refs": [],
            },
        },
        expected_revision=2,
        idempotency_key="report",
        actor_id="node-a",
        actor_type="node",
        holder_nonce=NONCE,
    )
    view = prepared.get_prepared_handoff(conn, work_id, now=NOW)
    assert (view["ownership"]["liveness"], view["last_report"]["freshness"]) == ("live-owner", "recent")


def test_freshness_for_is_bounded_and_fails_unknown():
    cases = {NOW: "recent", "2025-12-31T23:00:00+00:00": "stale", "2026-01-01T00:30:00+00:00": "future"}
    for stamp, expected in cases.items():
        assert liveness.freshness_for(stamp, now=NOW) == expected
    assert liveness.freshness_for(None, now=NOW) == "unknown"
    assert liveness.freshness_for("not-a-timestamp", now=NOW) == "unknown"


def test_owner_facts_tolerate_a_malformed_stamp(ledger):
    conn, work_id = ledger
    owned(conn, work_id)
    assert liveness.owner_facts(conn, {"owner_node": "node-a"}, work_id, now="bad") == liveness.OwnerFacts(
        owner_node="node-a"
    )
