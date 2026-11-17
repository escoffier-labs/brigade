"""Owner liveness derivation: states, precedence, scoping, privacy, writes, and readers."""

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
# After NOW: an end that happens after the ownership snapshot's last update.
LATER = "2026-01-01T00:10:00+00:00"
BEFORE = "2025-12-31T22:00:00+00:00"
REPO = "example/project"
NONCE = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
NONCE_B = base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")
OFFER = {
    "action": "offer",
    "target_node": "node-a",
    "exclusions": [],
    "authorization_ref": "grant-1",
    "attempt_budget": {"cap": 1, "source_ref": "budget-1"},
}
CHECKPOINT = {
    "action": "checkpoint",
    "generation": 1,
    "repo_identity": REPO,
    "write_scope": ["src/module.py"],
    "source_revision": "a" * 40,
    "next_action": {"kind": "await-review", "resume_condition": ""},
    "evidence_refs": [],
}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    # One fixed Hub clock: ownership writes and derived reads share it, so no
    # test depends on wall time or sleeps.
    monkeypatch.setattr(store, "_utc_now", lambda: NOW)
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


def checkpointed(conn, work_id, repo=REPO):
    """Owned, and checkpointed so the snapshot names a repository identity."""
    owned(conn, work_id)
    ownership.ownership_action(
        conn,
        work_id,
        CHECKPOINT | {"repo_identity": repo},
        expected_revision=2,
        idempotency_key="checkpoint",
        actor_id="node-a",
        actor_type="node",
        holder_nonce=NONCE,
    )


def _epoch(stamp):
    return datetime.fromisoformat(stamp).timestamp()


def session(
    conn,
    *,
    state="active",
    ended=None,
    stamp=NOW,
    repo=REPO,
    node="node-a",
    expires=None,
    session_id="PRIVATE-SESSION-ID",
    cloud=None,
):
    expires_at = _epoch(stamp) + 900 if expires is None else expires
    conn.execute(
        "INSERT INTO interactive_sessions (node_id, harness, session_id, repo_identity, identity_scope, "
        "repo_label, checkout_path, branch, dirty_paths_json, dirty_truncated, state, started_at, "
        "heartbeat_at, ended_at, ttl_seconds, expires_at, cloud_context_json) VALUES "
        "(?,'claude',?,?,'node','example','/tmp/work',NULL,'[]',0,?,?,?,?,900,?,?)",
        (node, session_id, repo, state, stamp, stamp, ended, expires_at, cloud),
    )
    conn.commit()


def claim(conn, *, target=None, stamp=NOW, node="node-a", expires=None):
    target = f"{REPO}@PRIVATE-CLAIM-TARGET" if target is None else target
    expires_at = _epoch(stamp) + 900 if expires is None else expires
    conn.execute(
        "INSERT INTO claims (target, owner_node, holder_token, acquired_at, renewed_at, ttl_seconds, expires_at) "
        "VALUES (?,?,'PRIVATE-HOLDER-TOKEN',?,?,900,?)",
        (target, node, stamp, stamp, expires_at),
    )
    conn.commit()


def finish_run(conn, work_id, state="run.completed", *, node="node-a", run="run-a"):
    store._append_event(
        conn,
        work_id=work_id,
        event_type="execution-linked",
        from_status=None,
        to_status=None,
        actor_type="node",
        actor_id=node,
        node_id=node,
        run_id=run,
        now=NOW,
        detail={},
    )
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, repo, seat, harness, state, ts, received_at) "
        "VALUES (?,?,1,'d',?,'seat','claude',?,?,?)",
        (node, run, REPO, state, NOW, NOW),
    )
    conn.commit()


def lapse_lease(conn, work_id, deadline="2025-12-31T00:00:00+00:00"):
    """Rewrite the newest ownership snapshot's campaign deadline, as a lapsed lease."""
    placeholders = ",".join("?" for _ in ownership.EVENT_TYPES)
    row = conn.execute(
        f"SELECT seq, detail_json FROM work_events WHERE work_id=? AND event_type IN ({placeholders}) "
        "ORDER BY seq DESC LIMIT 1",
        (work_id, *ownership.EVENT_TYPES.values()),
    ).fetchone()
    detail = json.loads(row[1])
    detail["ownership"]["expires_at"] = deadline
    conn.execute("UPDATE work_events SET detail_json=? WHERE seq=?", (json.dumps(detail), row[0]))
    conn.commit()


def read(conn, work_id):
    return ownership.get_ownership(conn, work_id, now=NOW)


@pytest.mark.parametrize(
    "setup,expected",
    [
        (lambda conn, work_id: session(conn), ("live-owner", "session")),
        (lambda conn, work_id: claim(conn), ("live-owner", "claim")),
        (
            lambda conn, work_id: session(conn, state="ended", stamp=LATER, ended=LATER),
            ("idle-owner", "session"),
        ),
        (lambda conn, work_id: lapse_lease(conn, work_id), ("expired-observation", "lease")),
        (lambda conn, work_id: finish_run(conn, work_id), ("terminal-execution", "run")),
    ],
    ids=["live-session", "live-claim", "ended-session", "lapsed-lease", "terminal-run"],
)
def test_each_liveness_state(ledger, setup, expected):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    setup(conn, work_id)
    before = conn.total_changes
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == expected
    assert conn.total_changes == before


@pytest.mark.parametrize("own", [False, True], ids=["unowned", "owned-no-facts"])
def test_unknown_without_owner_or_facts(ledger, own):
    conn, work_id = ledger
    if own:
        checkpointed(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"], snapshot["liveness_freshness"]) == (
        "unknown",
        "none",
        "unknown",
    )


def test_unknown_for_terminal_item_even_with_live_session(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn)
    store.transition(conn, work_id, to_status="canceled", expected_version=1, actor_id="admin")
    conn.commit()
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_live_owner_with_terminal_run_reads_live_owner(ledger):
    """A live owner is never reported as terminal, even after a terminal run."""
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn)
    finish_run(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("live-owner", "session")


def test_terminal_run_linked_by_another_node_is_ignored(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    finish_run(conn, work_id, node="node-b", run="run-b")
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_previous_owners_terminal_run_does_not_make_the_new_owner_terminal(ledger):
    conn, work_id = ledger
    owned(conn, work_id)
    finish_run(conn, work_id)
    ownership.ownership_action(
        conn,
        work_id,
        {"action": "release", "generation": 1},
        expected_revision=2,
        idempotency_key="release",
        actor_id="node-a",
        actor_type="node",
        holder_nonce=NONCE,
    )
    ownership.ownership_action(
        conn,
        work_id,
        OFFER | {"target_node": "node-b"},
        expected_revision=3,
        idempotency_key="offer-b",
        actor_id="admin",
        actor_type="operator",
        is_admin=True,
    )
    ownership.ownership_action(
        conn,
        work_id,
        {"action": "accept", "generation": 1},
        expected_revision=4,
        idempotency_key="accept-b",
        actor_id="node-b",
        actor_type="node",
        holder_nonce=NONCE_B,
    )
    snapshot = read(conn, work_id)
    assert snapshot["owner_node"] == "node-b" and snapshot["generation"] == 2
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


@pytest.mark.parametrize(
    "setup",
    [
        lambda conn: session(conn, repo="example/other"),
        lambda conn: claim(conn, target="example/other@PRIVATE-CLAIM-TARGET"),
    ],
    ids=["session", "claim"],
)
def test_fact_for_another_repository_does_not_make_this_item_live(ledger, setup):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    setup(conn)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_claim_for_this_repository_counts(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    claim(conn, target="Example/Project@PRIVATE-CLAIM-TARGET")
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("live-owner", "claim")


def test_a_later_ended_session_never_hides_a_live_one(ledger):
    """Two sessions on one repository: the auxiliary one ending must not make the owner takeable."""
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn, session_id="s-live", stamp=BEFORE, expires=_epoch(NOW) + 900)
    session(conn, state="ended", stamp=NOW, ended=NOW, session_id="s-aux")
    finish_run(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("live-owner", "session")


def test_terminal_run_before_the_first_checkpoint_reads_unknown(ledger):
    """No repository means no session or claim was checked, so a terminal run proves nothing."""
    conn, work_id = ledger
    owned(conn, work_id)
    finish_run(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_newer_crashed_session_hides_an_older_ended_session(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn, state="ended", stamp=BEFORE, ended=BEFORE, session_id="s-old")
    session(
        conn,
        state="active",
        stamp="2025-12-31T23:59:00+00:00",
        expires=_epoch(NOW) - 30,
        session_id="s-crashed",
    )
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_ended_session_before_the_last_ownership_update_does_not_count(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn, state="ended", stamp=BEFORE, ended=BEFORE)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_finished_cloud_session_is_not_live(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn, cloud=json.dumps({"version": 2, "reported_state": "completed"}))
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_lapsed_lease_wins_over_a_live_session(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn)
    lapse_lease(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("expired-observation", "lease")


def test_freshness_ignores_an_unrelated_repository_heartbeat(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn, stamp=BEFORE, expires=_epoch(NOW) + 900)
    session(conn, stamp=NOW, repo="example/other", session_id="s-other")
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_freshness"]) == ("live-owner", "stale")


def test_backdated_now_does_not_let_an_expired_holder_capability_pass(ledger):
    conn, work_id = ledger
    owned(conn, work_id)
    lapse_lease(conn, work_id, "2020-01-01T00:00:00+00:00")
    with pytest.raises(store.WorkloreConflict) as refusal:
        ownership.get_ownership(
            conn,
            work_id,
            actor_id="node-a",
            holder_nonce=NONCE,
            now="2019-12-31T00:00:00+00:00",
        )
    assert refusal.value.code == "stale-generation"


def test_copied_terminal_status_set_matches_ownership():
    assert liveness._TERMINAL_STATUSES == ownership.TERMINAL_STATUSES


@pytest.mark.parametrize(
    "setup,expected",
    [
        (
            lambda conn, work_id: (session(conn), finish_run(conn, work_id, "run.failed")),
            ("live-owner", "session"),
        ),
        (
            lambda conn, work_id: (lapse_lease(conn, work_id), finish_run(conn, work_id)),
            ("expired-observation", "lease"),
        ),
    ],
    ids=["session-beats-run", "lease-beats-run"],
)
def test_precedence(ledger, setup, expected):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    setup(conn, work_id)
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == expected


@pytest.mark.parametrize("table", ["interactive_sessions", "claims"])
def test_missing_fact_table_returns_unknown_without_raising(ledger, table):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    conn.execute(f"DROP TABLE {table}")
    snapshot = read(conn, work_id)
    assert (snapshot["liveness"], snapshot["liveness_basis"]) == ("unknown", "none")


def test_get_response_names_only_state_basis_and_freshness(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
    session(conn, stamp=store._utc_now())
    claim(conn, stamp=store._utc_now())
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


REPORT_DATA = {
    "version": 1,
    "provider": "dot",
    "session_id": "session-a",
    "agent_label": "worker-a",
    "observed_at": NOW,
    "sequence": 1,
    "progress": "Checking fixture",
    "evidence_refs": ["receipt-a"],
    "source": "explicit-metadata",
    "source_scope": "explicit-session",
    "coverage": "explicitly-reported-sessions",
    "evidence_verification": "reported-unverified",
    "provider_lifecycle": "unobserved",
}


def _prepared_view(state, freshness="recent"):
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
        "last_report": None
        if freshness is None
        else {"data": REPORT_DATA, "reporter_node": "node-a", "freshness": freshness},
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


def test_prepared_handoff_accepts_an_older_hub_unassessed_freshness():
    """An older Hub still sends ``unassessed``; the reader must accept it."""
    view = prepared.validate_prepared_handoff(_prepared_view("unknown", "unassessed"), "work-fixture")
    assert view["ownership"]["liveness"] == "unknown"
    assert view["last_report"]["freshness"] == "unassessed"


def test_prepared_handoff_refuses_other_liveness_words():
    for state in ("alive", "idle", "LIVE-OWNER", ""):
        with pytest.raises(WorkloreValidationError):
            prepared.validate_prepared_handoff(_prepared_view(state), "work-fixture")


def test_prepared_handoff_derives_liveness_and_report_freshness(ledger):
    conn, work_id = ledger
    checkpointed(conn, work_id)
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
        expected_revision=3,
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
