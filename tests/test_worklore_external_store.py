"""Dormant external ownership at real, disposable SQLite transaction boundaries."""

import base64
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, worklore_external as external, worklore_ownership as ownership
from brigade import worklore_scope as scope, worklore_store as store
from brigade.worklore_validate import WorkloreValidationError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
EPOCH = "a" * 32
TOKEN_BINDING = "1" * 64
NONCE = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
NONCE_B = base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "node-a", "Fixture")
    conn.execute(
        "INSERT INTO fleet_message_principals "
        "(principal_id,name,label,token_hash,created_at,enrollment_id,authorization_enrollment_epoch,role) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("agent:example", "example", "Fixture", TOKEN_BINDING, NOW.isoformat(), "enrollment-a", EPOCH, "agent"),
    )
    conn.commit()
    monkeypatch.setattr(store, "_utc_now", lambda: NOW.isoformat())
    yield conn, path
    conn.close()


def item(conn):
    return store.create_item(conn, {"title": "Fixture", "kind": "repo"}, actor_id="admin", actor_type="operator")[
        "work_id"
    ]


def observed(**changes):
    return external.AuthorityObservation(
        **{
            "principal": external.PrincipalRef("agent", "agent:example"),
            "enrollment_id": "enrollment-a",
            "authorization_enrollment_epoch": EPOCH,
            "current_credential_generation": external.CredentialBinding(TOKEN_BINDING),
            "authenticated_credential_generation": external.CredentialBinding(TOKEN_BINDING),
            "messaging_grant_epoch": None,
            "mesh_key_epoch": None,
            "activity": "active",
            **changes,
        }
    )


def admission(work_id, **changes):
    return {
        "protocol_version": 1,
        "admission_ref": "admission-example",
        "work_id": work_id,
        "generation": 1,
        "principal": {"kind": "agent", "id": "agent:example"},
        "enrollment_id": "enrollment-a",
        "authorization_enrollment_epoch": EPOCH,
        "repo_identity": "example/project",
        "allowed_paths": ["src", "tests"],
        "max_lease_seconds": 300,
        "actions": ["register", "read", "renew", "release", "report"],
        "authorization_ref": "authorization-example",
        "approval_ref": None,
        "campaign_member": False,
        **changes,
    }


def registration(work_id, paths=None):
    return {
        "protocol_version": 1,
        "admission_ref": "admission-example",
        "work_id": work_id,
        "generation": 1,
        "provider": "example-cloud",
        "task_id": "task-a",
        "thread_id": "thread-a",
        "repo_identity": "example/project",
        "write_scope": paths or ["src/package"],
        "source_revision": "a" * 40,
    }


def act(conn, work_id, body, revision, *, key=None, admin=False, auth=None, nonce=NONCE):
    return ownership.external_ownership_action(
        conn,
        work_id,
        body,
        expected_revision=revision,
        idempotency_key=key or f"request-{revision}",
        authority=None if admin else auth or observed(),
        operator_authorized=admin,
        holder_nonce=None if admin else nonce,
    )


def offer(conn, work_id=None, **changes):
    work_id = work_id or item(conn)
    result = act(conn, work_id, {"action": "admit", "admission": admission(work_id, **changes)}, 0, admin=True)
    return work_id, result


def register(conn, work_id, paths=None, **kwargs):
    return act(
        conn,
        work_id,
        {"action": "register", "generation": 1, "registration": registration(work_id, paths), "lease_seconds": 120},
        1,
        **kwargs,
    )


def active(conn, paths=None):
    work_id, _ = offer(conn)
    return work_id, register(conn, work_id, paths)


def count(conn, work_id):
    return conn.execute("SELECT COUNT(*) FROM work_events WHERE work_id=?", (work_id,)).fetchone()[0]


def raw_event(conn, work_id, event_type, detail):
    action = detail.get("action")
    actor_type = (
        "agent" if action in {"register", "renew", "report", "release"} else "hub" if action == "expire" else "admin"
    )
    if "admission" in detail.get("ownership", {}):
        detail = json.loads(json.dumps(detail))
        detail["ownership"]["last_seq"] = conn.execute(f"SELECT {store._EVENT_SEQ_SQL}").fetchone()[0]
    store._append_event(
        conn,
        work_id=work_id,
        event_type=event_type,
        from_status=None,
        to_status=None,
        actor_type=actor_type,
        actor_id="agent:example" if actor_type == "agent" else actor_type,
        now=NOW.isoformat(),
        detail=detail,
    )
    conn.commit()


def test_registration_is_prospective_typed_and_legacy_domain_is_permanent(db):
    conn, _ = db
    work_id, pending = offer(conn)
    assert pending["state"] == "admitted" and pending["revision"] == 1
    lease = register(conn, work_id)
    assert lease["state"] == "active" and lease["revision"] == 2
    assert lease["owner_node"] is None
    assert lease["owner_principal"] == {"kind": "agent", "id": "agent:example"}
    assert lease["binding"]["task_id"] == "task-a"
    assert lease["physical_writer_stopped"] is False
    released = act(conn, work_id, {"action": "release", "generation": 1}, 2)
    assert released["state"] == "released"
    with pytest.raises(store.WorkloreConflict, match="external"):
        ownership.get_ownership(conn, work_id)
    with pytest.raises(store.WorkloreConflict):
        act(conn, work_id, {"action": "admit", "admission": admission(work_id)}, 3, admin=True)
    rows = conn.execute(
        "SELECT event_type,actor_type,node_id,detail_json FROM work_events WHERE work_id=? ORDER BY seq", (work_id,)
    ).fetchall()
    assert [r[0] for r in rows[1:]] == [
        "external-ownership-admitted",
        "external-ownership-registered",
        "external-ownership-released",
    ]
    assert [r[1] for r in rows[1:]] == ["admin", "agent", "agent"]
    assert all(r[2] is None for r in rows[1:])
    assert TOKEN_BINDING not in str(rows) and NONCE not in str(rows)
    assert "holder_hash" not in str(released)


@pytest.mark.parametrize(
    "column,value,code",
    [
        ("revoked_at", NOW.isoformat(), "principal-revoked"),
        ("role", "teammate", "principal-forbidden"),
        ("enrollment_id", None, "enrollment-mismatch"),
        ("enrollment_id", "enrollment-b", "enrollment-mismatch"),
        ("authorization_enrollment_epoch", "b" * 32, "enrollment-mismatch"),
        ("token_hash", "2" * 64, "stale-credential"),
    ],
)
def test_authority_is_revalidated_from_database_under_write_fence(db, column, value, code):
    conn, path = db
    work_id, _ = offer(conn)
    preauthenticated = observed()
    other = sqlite3.connect(path)
    other.execute(f"UPDATE fleet_message_principals SET {column}=?", (value,))
    other.commit()
    other.close()
    before = count(conn, work_id)
    with pytest.raises(store.WorkloreForbidden) as caught:
        register(conn, work_id, auth=preauthenticated)
    assert caught.value.code == code
    assert count(conn, work_id) == before and not conn.in_transaction


def test_holder_replay_is_authenticated_immutable_and_consumes_zero_capacity(db):
    conn, _ = db
    work_id, _ = active(conn)
    body = {"action": "renew", "generation": 1, "lease_seconds": 180}
    first = act(conn, work_id, body, 2, key="renewal")
    before = count(conn, work_id)
    assert act(conn, work_id, body, 2, key="renewal") == first
    assert count(conn, work_id) == before
    for change in ({"generation": 2}, {"lease_seconds": 181}, {"action": "release"}):
        bad = body | change
        if bad["action"] == "release":
            bad.pop("lease_seconds")
        with pytest.raises((store.WorkloreConflict, WorkloreValidationError)):
            act(conn, work_id, bad, 2, key="renewal")
    with pytest.raises(store.WorkloreForbidden):
        act(conn, work_id, {"action": "release", "generation": 1}, 3, nonce=NONCE_B)
    with pytest.raises(store.WorkloreForbidden):
        act(
            conn,
            work_id,
            body,
            2,
            key="renewal",
            auth=observed(principal=external.PrincipalRef("agent", "agent:other")),
        )
    assert count(conn, work_id) == before


def test_overlapping_registration_has_one_winner_and_disjoint_registration_succeeds(db):
    conn, path = db
    first, _ = offer(conn)
    second, _ = offer(conn)
    ready = threading.Barrier(2)

    def attempt(work_id):
        other = sqlite3.connect(path, timeout=5)
        try:
            ready.wait()
            return register(other, work_id)["state"]
        except scope.ScopeAdmissionError as error:
            return error.code
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first, second)))
    assert sorted(results) == ["active", "path-scope-conflict"]
    third, _ = offer(conn)
    assert register(conn, third, ["tests"])["state"] == "active"


def test_external_conflict_has_only_declared_public_principal_fields(db):
    conn, _ = db
    work_id, _ = active(conn)
    other, _ = offer(conn)
    with pytest.raises(scope.ScopeAdmissionError) as caught:
        register(conn, other)
    assert caught.value.details["conflicts"] == [
        {
            "work_id": work_id,
            "owner_node": None,
            "owner_principal": {"kind": "agent", "id": "agent:example"},
            "requested_path": "src/package",
            "held_path": "src/package",
            "resolution": "replace-scope-or-release-holder",
        }
    ]
    assert ownership.nonce_hash(NONCE) not in str(caught.value.details)


def test_expiry_is_overlap_only_atomic_and_allows_terminal_old_item(db, monkeypatch):
    conn, _ = db
    old, _ = active(conn)
    conn.execute("UPDATE work_items SET status='completed' WHERE work_id=?", (old,))
    conn.commit()
    successor, _ = offer(conn)
    monkeypatch.setattr(store, "_utc_now", lambda: (NOW + timedelta(seconds=120)).isoformat())
    before = count(conn, old)
    conn.execute(
        "CREATE TRIGGER fail_new_external BEFORE INSERT ON work_events "
        "WHEN NEW.event_type='external-ownership-registered' "
        "BEGIN SELECT RAISE(ABORT, 'fixture insertion failed'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="fixture insertion failed"):
        register(conn, successor, nonce=NONCE_B)
    assert count(conn, old) == before
    conn.execute("DROP TRIGGER fail_new_external")
    conn.commit()
    assert register(conn, successor, nonce=NONCE_B)["state"] == "active"
    ending = conn.execute(
        "SELECT event_type,actor_type FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (old,)
    ).fetchone()
    assert ending == ("external-ownership-expired", "hub")
    assert count(conn, old) == before + 1


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        '{"external_ownership_version":true,"action":"release"}',
        '{"external_ownership_version":1.0,"action":"release"}',
        '{"external_ownership_version":"1","action":"release"}',
        '{"action":"release"}',
    ],
)
def test_malformed_or_mistyped_ending_cannot_hide_reservations(db, raw):
    conn, _ = db
    old = item(conn)
    raw_event(conn, old, "external-ownership-released", {})
    conn.execute(
        "UPDATE work_events SET detail_json=? WHERE work_id=? AND event_type=?",
        (raw, old, "external-ownership-released"),
    )
    conn.commit()
    candidate, _ = offer(conn)
    with pytest.raises(scope.ScopeAdmissionError) as caught:
        register(conn, candidate)
    assert caught.value.code == "path-scope-unavailable"


@pytest.mark.parametrize("event_type", ["external-ownership-future", "external-ownership-REGISTERED"])
def test_unknown_reserved_namespace_refuses_legacy_and_new_scope(db, event_type):
    conn, _ = db
    old = item(conn)
    raw_event(conn, old, event_type, {})
    with pytest.raises(store.WorkloreConflict):
        ownership.get_ownership(conn, old)
    candidate, _ = offer(conn)
    with pytest.raises(scope.ScopeAdmissionError):
        register(conn, candidate)


def test_namespace_is_binary_and_legacy_replay_refuses_any_external_history(db):
    conn, _ = db
    work_id = item(conn)
    offer_body = {
        "action": "offer",
        "target_node": "node-a",
        "exclusions": [],
        "authorization_ref": "grant-a",
        "attempt_budget": {"cap": 2, "source_ref": "budget-a"},
    }
    kwargs = dict(
        expected_revision=0, idempotency_key="legacy-offer", actor_id="admin", actor_type="admin", is_admin=True
    )
    legacy = ownership.ownership_action(conn, work_id, offer_body, **kwargs)
    raw_event(conn, work_id, "External-ownership-future", {})
    assert ownership.ownership_action(conn, work_id, offer_body, **kwargs) == legacy
    raw_event(conn, work_id, "external-ownership-future", {})
    with pytest.raises(store.WorkloreConflict):
        ownership.ownership_action(conn, work_id, offer_body, **kwargs)


@pytest.mark.parametrize(
    "action,start,allowed",
    [
        ("admit", 197, True),
        ("admit", 198, False),
        ("register", 197, True),
        ("register", 198, True),
        ("register", 199, False),
        ("renew", 197, True),
        ("renew", 198, True),
        ("renew", 199, False),
        ("release", 199, True),
        ("release", 200, True),
        ("release", 201, False),
        ("withdraw", 200, True),
        ("withdraw", 201, False),
    ],
)
def test_capacity_is_per_item_and_reserves_progress_and_exit_slots(db, action, start, allowed):
    conn, _ = db
    if action == "admit":
        work_id = item(conn)
    elif action in {"register", "withdraw"}:
        work_id, _ = offer(conn)
    else:
        work_id, _ = active(conn)
    existing = conn.execute(
        "SELECT COUNT(*) FROM work_events WHERE work_id=? AND event_type IN ('external-ownership-admitted','external-ownership-registered')",
        (work_id,),
    ).fetchone()[0]
    # Old valid legacy reset history contributes to the same capacity domain.
    for _ in range(start - existing):
        raw_event(conn, work_id, "ownership-released", {"ownership": ownership._initial(), "action": "release"})
        conn.execute("UPDATE work_events SET seq=-rowid WHERE rowid=(SELECT MAX(rowid) FROM work_events)")
    conn.commit()
    before = count(conn, work_id)

    def perform():
        if action == "admit":
            return act(conn, work_id, {"action": "admit", "admission": admission(work_id)}, 0, admin=True)
        if action == "register":
            return register(conn, work_id)
        body = {"action": action, "generation": 1}
        if action == "renew":
            body["lease_seconds"] = 120
        return act(conn, work_id, body, 1 if action == "withdraw" else 2, admin=action == "withdraw")

    if allowed:
        perform()
        assert count(conn, work_id) == before + 1
    else:
        with pytest.raises(store.WorkloreConflict) as caught:
            perform()
        assert caught.value.code == "ownership-capacity-exhausted"
        assert count(conn, work_id) == before


@pytest.mark.parametrize("kind", ["ended", "admitted", "live", "unknown"])
def test_completed_endings_do_not_spend_snapshot_bound_but_other_candidates_do(db, kind):
    conn, _ = db
    template, _ = active(conn)
    if kind == "ended":
        act(conn, template, {"action": "release", "generation": 1}, 2)
    elif kind == "admitted":
        template, _ = offer(conn)
    row = conn.execute(
        "SELECT event_type,detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (template,)
    ).fetchone()
    for _index in range(scope.MAX_SCOPE_SNAPSHOTS):
        identifier = item(conn)
        detail = json.loads(row[1])
        detail["ownership"]["admission"]["work_id"] = identifier
        if detail["ownership"]["binding"]:
            detail["ownership"]["binding"]["work_id"] = identifier
        raw_event(conn, identifier, "external-ownership-future" if kind == "unknown" else row[0], detail)
    candidate, _ = offer(conn)
    if kind == "ended":
        assert register(conn, candidate)["state"] == "active"
    else:
        with pytest.raises(scope.ScopeAdmissionError) as caught:
            register(conn, candidate)
        assert caught.value.code in {"path-scope-capacity-exhausted", "path-scope-unavailable"}


def test_admission_registration_and_ending_replay_return_the_identical_public_result(db):
    conn, _ = db
    work_id = item(conn)
    body = {"action": "admit", "admission": admission(work_id)}
    pending = act(conn, work_id, body, 0, admin=True)
    assert act(conn, work_id, body, 0, admin=True) == pending
    registered = register(conn, work_id)
    assert register(conn, work_id) == registered
    release = {"action": "release", "generation": 1}
    ending = act(conn, work_id, release, 2)
    assert act(conn, work_id, release, 2) == ending
    assert count(conn, work_id) == 4


def test_reports_keep_initial_binding_and_do_not_refresh_or_relabel_the_lease(db):
    conn, _ = db
    work_id, lease = active(conn)
    report = {"version": 1, "branch": "feature/example", "head_commit": "b" * 40, "issues": [], "pull_requests": []}
    body = {"action": "report", "generation": 1, "report": report}
    reported = act(conn, work_id, body, 2)
    assert reported["last_report"] == report
    assert reported["binding"]["source_revision"] == "a" * 40
    assert reported["expires_at"] == lease["expires_at"] and reported["heartbeat"] == lease["heartbeat"]
    assert act(conn, work_id, body, 2) == reported
    with pytest.raises(WorkloreValidationError):
        act(conn, work_id, body | {"task_id": "task-other"}, 3)
    assert count(conn, work_id) == 4


def test_existing_legacy_reservation_is_preserved_and_legacy_cannot_expire_external(db, monkeypatch):
    from tests.test_worklore_ownership import CHECKPOINT, NONCE_A, owned

    conn, _ = db
    legacy = item(conn)
    owned(conn, legacy)
    ownership.ownership_action(
        conn,
        legacy,
        CHECKPOINT,
        expected_revision=2,
        idempotency_key="checkpoint",
        actor_id="node-a",
        actor_type="node",
        holder_nonce=NONCE_A,
    )
    candidate, _ = offer(conn)
    with pytest.raises(scope.ScopeAdmissionError):
        register(conn, candidate, ["src"])
    assert ownership.get_ownership(conn, legacy)["state"] == "owned"
    work_id, _ = active(conn, ["tests"])
    before = count(conn, work_id)
    monkeypatch.setattr(store, "_utc_now", lambda: (NOW + timedelta(seconds=121)).isoformat())
    with pytest.raises(scope.ScopeAdmissionError):
        ownership.ownership_action(
            conn,
            legacy,
            CHECKPOINT | {"write_scope": ["tests"]},
            expected_revision=3,
            idempotency_key="elapsed",
            actor_id="node-a",
            actor_type="node",
            holder_nonce=NONCE_A,
        )
    assert count(conn, work_id) == before


def test_external_registration_cannot_reuse_a_departed_legacy_holder_nonce(db):
    from tests.test_worklore_ownership import NONCE_A, owned

    conn, _ = db
    work_id = item(conn)
    owned(conn, work_id)
    ownership.ownership_action(
        conn,
        work_id,
        {"action": "release", "generation": 1},
        expected_revision=2,
        idempotency_key="legacy-release",
        actor_id="node-a",
        actor_type="node",
        holder_nonce=NONCE_A,
    )
    act(conn, work_id, {"action": "admit", "admission": admission(work_id, generation=2)}, 3, admin=True)
    body = {
        "action": "register",
        "generation": 2,
        "registration": registration(work_id) | {"generation": 2},
        "lease_seconds": 120,
    }
    with pytest.raises(store.WorkloreConflict, match="fresh holder"):
        act(conn, work_id, body, 4, nonce=NONCE_A)
    assert act(conn, work_id, body, 4, nonce=NONCE_B)["state"] == "active"


@pytest.mark.parametrize(
    "raw",
    [
        {"action": "expire", "generation": 1},
        {"action": "release", "generation": 1, "is_admin": True},
        {"action": "release", "generation": 1, "event_type": "external-ownership-expired"},
    ],
)
def test_body_fields_never_choose_authority_or_server_event_type(db, raw):
    conn, _ = db
    work_id, _ = active(conn)
    before = count(conn, work_id)
    with pytest.raises(WorkloreValidationError):
        act(conn, work_id, raw, 2)
    assert count(conn, work_id) == before


def test_principal_revalidation_happens_after_begin_immediate(db):
    conn, _ = db
    work_id, _ = offer(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    register(conn, work_id)
    conn.set_trace_callback(None)
    begin = next(i for i, sql in enumerate(statements) if sql == "BEGIN IMMEDIATE")
    reads = [i for i, sql in enumerate(statements) if "FROM fleet_message_principals" in sql]
    assert reads and all(i > begin for i in reads)


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 2])
def test_pending_admission_is_fully_validated_before_nonreserving_treatment(db, version):
    conn, _ = db
    work_id, _ = offer(conn)
    row = conn.execute(
        "SELECT event_id,detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
    ).fetchone()
    detail = json.loads(row[1])
    detail["external_ownership_version"] = version
    conn.execute("UPDATE work_events SET detail_json=? WHERE event_id=?", (json.dumps(detail), row[0]))
    conn.commit()
    candidate, _ = offer(conn)
    with pytest.raises(scope.ScopeAdmissionError) as caught:
        register(conn, candidate)
    assert caught.value.code == "path-scope-unavailable"


def test_other_principal_does_not_receive_the_admitted_principals_revocation_state(db):
    conn, _ = db
    work_id, _ = offer(conn)
    conn.execute("UPDATE fleet_message_principals SET revoked_at=?", (NOW.isoformat(),))
    conn.commit()
    with pytest.raises(store.WorkloreForbidden) as caught:
        register(conn, work_id, auth=observed(principal=external.PrincipalRef("agent", "agent:other")))
    assert caught.value.code == "principal-forbidden"


def test_campaign_root_and_members_and_pending_legacy_handoff_cannot_be_admitted(db):
    from brigade import worklore_campaigns as campaigns
    from tests.test_worklore_ownership import NONCE_A, owned

    conn, _ = db
    campaigns.create_campaign(
        conn,
        {
            "campaign_key": "campaign-example",
            "title": "Fixture",
            "acceptance": ["Verify the fixture"],
            "members": [{"repo": "example/project"}],
        },
        actor_id="admin",
    )
    identifiers = [
        row[0]
        for row in conn.execute(
            "SELECT work_id FROM work_campaigns UNION ALL SELECT work_id FROM work_campaign_members"
        )
    ]
    assert len(identifiers) == 2
    for work_id in identifiers:
        with pytest.raises(store.WorkloreConflict, match="noncampaign"):
            offer(conn, work_id)
    fleet_hub.add_node(conn, "node-b", "Fixture")
    work_id = item(conn)
    owned(conn, work_id)
    ownership.ownership_action(
        conn,
        work_id,
        {"action": "handoff", "generation": 1, "target_node": "node-b"},
        expected_revision=2,
        idempotency_key="handoff",
        actor_id="node-a",
        actor_type="node",
        holder_nonce=NONCE_A,
    )
    with pytest.raises(store.WorkloreConflict, match="unowned"):
        act(conn, work_id, {"action": "admit", "admission": admission(work_id, generation=2)}, 3, admin=True)


def _clone_external_reservation(conn, template, *, path):
    identifier = item(conn)
    row = conn.execute(
        "SELECT event_type,detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (template,)
    ).fetchone()
    detail = json.loads(row[1])
    snapshot = detail["ownership"]
    snapshot["admission"]["work_id"] = identifier
    snapshot["binding"]["work_id"] = identifier
    snapshot["binding"]["write_scope"] = [path]
    raw_event(conn, identifier, row[0], detail)
    return identifier


def test_expiry_cleanup_is_limited_to_32_relevant_leases_and_ignores_unrelated_elapsed_rows(db, monkeypatch):
    conn, _ = db
    template, _ = active(conn, ["tests"])
    for _ in range(33):
        _clone_external_reservation(conn, template, path="tests")
    relevant = [_clone_external_reservation(conn, template, path="src/package") for _ in range(33)]
    candidate, _ = offer(conn)
    monkeypatch.setattr(store, "_utc_now", lambda: (NOW + timedelta(seconds=120)).isoformat())
    before = sum(count(conn, work_id) for work_id in relevant)
    with pytest.raises(scope.ScopeAdmissionError) as caught:
        register(conn, candidate)
    assert caught.value.code == "path-scope-capacity-exhausted"
    assert sum(count(conn, work_id) for work_id in relevant) == before
    # A valid server-written ending removes exactly one relevant candidate.
    row = conn.execute(
        "SELECT event_id,detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (relevant[0],)
    ).fetchone()
    detail = json.loads(row[1])
    detail["action"] = "expire"
    detail["ownership"].update(state="expired", updated_at=(NOW + timedelta(seconds=120)).isoformat())
    conn.execute(
        "UPDATE work_events SET event_type='external-ownership-expired',actor_type='hub',actor_id='hub',detail_json=? WHERE event_id=?",
        (json.dumps(detail), row[0]),
    )
    conn.commit()
    assert register(conn, candidate)["state"] == "active"
    assert (
        conn.execute("SELECT COUNT(*) FROM work_events WHERE event_type='external-ownership-expired'").fetchone()[0]
        == 33
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM work_events WHERE work_id=? AND event_type='external-ownership-expired'", (template,)
        ).fetchone()[0]
        == 0
    )


def test_admission_replay_cannot_cross_the_current_external_generation(db):
    conn, _ = db
    work_id = item(conn)
    body = {"action": "admit", "admission": admission(work_id)}
    act(conn, work_id, body, 0, admin=True)
    register(conn, work_id)
    row = conn.execute(
        "SELECT event_id,detail_json FROM work_events WHERE work_id=? ORDER BY seq DESC LIMIT 1", (work_id,)
    ).fetchone()
    detail = json.loads(row[1])
    detail["ownership"]["generation"] = 2
    detail["ownership"]["admission"]["generation"] = 2
    detail["ownership"]["binding"]["generation"] = 2
    conn.execute("UPDATE work_events SET detail_json=? WHERE event_id=?", (json.dumps(detail), row[0]))
    conn.commit()
    with pytest.raises(store.WorkloreConflict):
        act(conn, work_id, body, 0, admin=True)


def test_public_external_results_exclude_private_admission_provenance(db):
    conn, _ = db
    work_id, pending = offer(
        conn, authorization_ref="authorization-private-fixture", approval_ref="approval-private-fixture"
    )
    registered = register(conn, work_id)
    released = act(conn, work_id, {"action": "release", "generation": 1}, 2)
    replayed = register(conn, work_id)
    other_id, other_pending = offer(conn, approval_ref="withdraw-private-fixture")
    withdrawn = act(conn, other_id, {"action": "withdraw", "generation": 1}, 1, admin=True)
    private = ownership._external_current(conn, work_id)
    assert private["admission"]["approval_ref"] == "approval-private-fixture"
    assert private["admission"]["authorization_ref"] == "authorization-private-fixture"
    forbidden = {"admission", "enrollment_id", "authorization_enrollment_epoch", "authorization_ref", "approval_ref"}

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(child) for child in value.values()))
        if isinstance(value, (list, tuple)):
            return set().union(*(keys(child) for child in value))
        return set()

    for identifier, result in (
        (work_id, pending),
        (work_id, registered),
        (work_id, released),
        (work_id, replayed),
        (other_id, other_pending),
        (other_id, withdrawn),
    ):
        assert not forbidden.intersection(keys(result))
        assert result["claim_ref"] == {"work_id": identifier, "generation": 1}
        serialized = json.dumps(result)
        assert "authorization-private-fixture" not in serialized
        assert "approval-private-fixture" not in serialized
        assert "withdraw-private-fixture" not in serialized
        assert TOKEN_BINDING not in serialized
        assert NONCE not in serialized


def test_external_lifecycle_and_expiry_leave_public_item_metadata_unchanged(db, monkeypatch):
    conn, _ = db

    def metadata(identifier):
        return tuple(
            conn.execute("SELECT version,updated_at,status FROM work_items WHERE work_id=?", (identifier,)).fetchone()
        )

    work_id = item(conn)
    before = metadata(work_id)
    offer(conn, work_id)
    assert metadata(work_id) == before
    register(conn, work_id)
    assert metadata(work_id) == before
    act(conn, work_id, {"action": "renew", "generation": 1, "lease_seconds": 120}, 2)
    assert metadata(work_id) == before
    report = {"version": 1, "branch": "feature/example", "head_commit": "b" * 40, "issues": [], "pull_requests": []}
    act(conn, work_id, {"action": "report", "generation": 1, "report": report}, 3)
    assert metadata(work_id) == before
    act(conn, work_id, {"action": "release", "generation": 1}, 4)
    assert metadata(work_id) == before
    register(conn, work_id)
    assert metadata(work_id) == before
    pending_id = item(conn)
    pending_before = metadata(pending_id)
    offer(conn, pending_id)
    act(conn, pending_id, {"action": "withdraw", "generation": 1}, 1, admin=True)
    assert metadata(pending_id) == pending_before
    old_id, _ = active(conn, ["src/expiry"])
    conn.execute("UPDATE work_items SET status='completed' WHERE work_id=?", (old_id,))
    conn.commit()
    old_before = metadata(old_id)
    successor = item(conn)
    successor_before = metadata(successor)
    offer(conn, successor)
    monkeypatch.setattr(store, "_utc_now", lambda: (NOW + timedelta(seconds=121)).isoformat())
    register(conn, successor, ["src/expiry"], nonce=NONCE_B)
    assert ownership._external_current(conn, old_id)["state"] == "expired"
    assert metadata(old_id) == old_before
    assert metadata(successor) == successor_before


@pytest.mark.parametrize("nonce", ["", "x", "A" * 42, "A" * 45, "\u00e9"])
def test_malformed_holder_nonce_is_a_validation_refusal_without_writes(db, nonce):
    conn, _ = db
    work_id, _ = offer(conn)
    before = conn.total_changes
    with pytest.raises(WorkloreValidationError) as caught:
        register(conn, work_id, nonce=nonce)
    assert caught.value.code == "field-bound"
    assert conn.total_changes == before
    assert ownership._external_current(conn, work_id)["state"] == "admitted"


@pytest.mark.parametrize("reader", ["legacy", "external", "prepared", "scope"])
@pytest.mark.parametrize(
    "event_type",
    [
        b"external-ownership-",
        b"external-ownership-future",
        b"external-ownership-registered",
        b"external-ownership-released",
    ],
)
def test_blob_external_namespace_cannot_bypass_domain_or_scope_fences(db, reader, event_type):
    conn, _ = db
    work_id = item(conn)
    raw_event(conn, work_id, "external-ownership-future", {})
    conn.execute(
        "UPDATE work_events SET event_type=? WHERE work_id=? AND event_type=?",
        (sqlite3.Binary(event_type), work_id, "external-ownership-future"),
    )
    conn.commit()
    if reader == "scope":
        candidate, _ = offer(conn)
        with pytest.raises(scope.ScopeAdmissionError) as caught:
            register(conn, candidate)
        assert caught.value.code == "path-scope-unavailable"
    else:
        from brigade import worklore_prepared_handoff as prepared

        read = {
            "legacy": ownership.get_ownership,
            "external": ownership._external_current,
            "prepared": prepared._current_ownership,
        }
        with pytest.raises(store.WorkloreConflict) as caught:
            read[reader](conn, work_id)
        assert caught.value.code == "ownership-conflict"


@pytest.mark.parametrize("blob", [False, True])
@pytest.mark.parametrize("event_type", ["External-ownership-future", "external-ownershipX"])
def test_near_matching_event_names_do_not_quarantine_unrelated_work(db, event_type, blob):
    conn, _ = db
    work_id = item(conn)
    raw_event(conn, work_id, event_type, {})
    if blob:
        conn.execute(
            "UPDATE work_events SET event_type=? WHERE work_id=? AND event_type=?",
            (sqlite3.Binary(event_type.encode()), work_id, event_type),
        )
        conn.commit()
    assert ownership.get_ownership(conn, work_id)["state"] == "unowned"
    assert ownership._external_current(conn, work_id) is None
    candidate, _ = offer(conn)
    assert register(conn, candidate)["state"] == "active"
