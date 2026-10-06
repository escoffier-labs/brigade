"""Ownership protocol contracts at the authenticated HTTP and SQLite boundaries."""

import base64
import json
import threading
from contextlib import contextmanager

import pytest

from brigade import fleet_hub
from brigade import worklore_client as client
from brigade import worklore_http as http
from brigade import worklore_store as store

NONCE_A = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
NONCE_B = base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")
NONCE_C = base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("=")
REVISION = "a" * 40
OFFER = {
    "action": "offer",
    "target_node": "node-a",
    "exclusions": ["do not publish"],
    "authorization_ref": "grant-1",
    "attempt_budget": {"cap": 2, "source_ref": "budget-1"},
}
CHECKPOINT = {
    "action": "checkpoint",
    "generation": 1,
    "repo_identity": "example/project",
    "write_scope": ["src/module.py"],
    "source_revision": REVISION,
    "next_action": {"kind": "await-merge", "resume_condition": "checks complete"},
    "evidence_refs": [{"kind": "receipt", "ref": "receipt-1", "source_revision": REVISION}],
}


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    for node in ("node-a", "node-b", "node-op"):
        fleet_hub.add_node(conn, node, node)
    item = store.create_item(conn, {"title": "Example", "kind": "repo"}, actor_id="admin", actor_type="operator")
    yield conn, item["work_id"], path
    conn.close()


def request(
    conn,
    work_id,
    body=None,
    *,
    node="node-a",
    admin=False,
    operator=False,
    revision=0,
    key="key-1",
    nonce=None,
    method="POST",
    query="",
):
    headers = {}
    if revision is not None:
        headers["If-Match"] = str(revision)
    if key is not None:
        headers["Idempotency-Key"] = key
    if nonce is not None:
        headers["X-Worklore-Holder"] = nonce
    return http.handle(
        conn,
        http.Request(
            method=method,
            path=f"/work/items/{work_id}/ownership{query}",
            node_id=None if admin else node,
            is_admin=admin,
            is_operator=operator,
            operator_authorization_resolved=True,
            body=body or {},
            headers=headers,
        ),
    )


def post(conn, work_id, body, **kwargs):
    status, payload = request(conn, work_id, body, **kwargs)
    assert status == 200, payload
    return payload["ownership"]


def owned(conn, work_id):
    post(conn, work_id, OFFER, admin=True, key="offer")
    return post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A)


def event_count(conn, work_id):
    return conn.execute(
        "SELECT COUNT(*) FROM work_events WHERE work_id=? AND event_type LIKE 'ownership-%'", (work_id,)
    ).fetchone()[0]


def test_initial_and_absent_reads_do_not_create_ownership(ledger):
    conn, work_id, _ = ledger
    status, payload = request(conn, work_id, method="GET")
    assert status == 200
    snapshot = payload["ownership"]
    assert (snapshot["state"], snapshot["revision"], snapshot["generation"]) == ("unowned", 0, 0)
    assert snapshot["liveness"] == "unknown" and snapshot["conflict_check"] == "not-performed"
    assert event_count(conn, work_id) == 0
    assert request(conn, "absent", method="GET")[0] == 404
    assert request(conn, "absent", OFFER, admin=True)[0] == 404


@pytest.mark.parametrize("action", ["offer", "withdraw", "accept", "checkpoint", "handoff", "release"])
def test_every_action_requires_cas_and_key(ledger, action):
    conn, work_id, _ = ledger
    body = dict(OFFER) if action == "offer" else dict(CHECKPOINT) if action == "checkpoint" else {"action": action}
    if action in {"accept", "handoff", "release"}:
        body["generation"] = 0
    if action == "handoff":
        body["target_node"] = "node-b"
    status, payload = request(conn, work_id, body, admin=action in {"offer", "withdraw"}, revision=None, nonce=NONCE_A)
    assert status == 400 and payload["code"] == "if-match-required"
    status, payload = request(conn, work_id, body, admin=action in {"offer", "withdraw"}, key=None, nonce=NONCE_A)
    assert status == 400 and payload["code"] == "idempotency-key-required"
    assert event_count(conn, work_id) == 0


@pytest.mark.parametrize("action", ["offer", "withdraw", "accept", "checkpoint", "handoff", "release"])
def test_every_action_refuses_stale_revision(ledger, action):
    conn, work_id, _ = ledger
    if action in {"offer", "withdraw", "accept"}:
        post(conn, work_id, OFFER, admin=True, key="offer")
        if action == "offer":
            post(conn, work_id, {"action": "withdraw"}, admin=True, revision=1, key="withdraw")
        body = OFFER if action == "offer" else {"action": action, **({"generation": 0} if action == "accept" else {})}
    else:
        owned(conn, work_id)
        body = CHECKPOINT if action == "checkpoint" else {"action": action, "generation": 1}
        if action == "handoff":
            body["target_node"] = "node-b"
    before = event_count(conn, work_id)
    status, payload = request(
        conn, work_id, body, admin=action in {"offer", "withdraw"}, revision=0, key="stale", nonce=NONCE_A
    )
    assert status == 409 and payload["code"] == "version-conflict"
    assert event_count(conn, work_id) == before


def test_explicit_operator_root_and_named_target(ledger):
    conn, work_id, _ = ledger
    assert request(conn, work_id, OFFER)[0] == 403
    post(conn, work_id, OFFER, node="node-op", operator=True, key="offer")
    for kwargs in ({"node": "node-b"}, {"admin": True}, {"node": "node-op", "operator": True}):
        status, payload = request(
            conn, work_id, {"action": "accept", "generation": 0}, revision=1, nonce=NONCE_A, **kwargs
        )
        assert status == 403 and payload["code"] == "holder-mismatch"
    first = post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A)
    assert first["granted_by"] == {"actor_type": "operator", "actor_id": "node-op"}
    assert first["attempt_budget"] == OFFER["attempt_budget"]
    assert request(conn, work_id, OFFER, admin=True, revision=2, key="over-owner")[1]["code"] == "ownership-conflict"
    assert (
        request(conn, work_id, CHECKPOINT, node="node-op", operator=True, revision=2, key="operator", nonce=NONCE_A)[0]
        == 403
    )


@pytest.mark.parametrize("target", ["node-a", "node-b"])
def test_handoff_fences_old_holder_and_preserves_grant(ledger, target):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    checkpoint = post(conn, work_id, CHECKPOINT, revision=2, key="checkpoint", nonce=NONCE_A)
    handoff = post(
        conn,
        work_id,
        {"action": "handoff", "generation": 1, "target_node": target},
        revision=3,
        key="handoff",
        nonce=NONCE_A,
    )
    assert handoff["state"] == "handoff-pending" and handoff["owner_node"] == "node-a"
    assert (
        request(conn, work_id, CHECKPOINT, revision=4, key="pending", nonce=NONCE_A)[1]["code"] == "ownership-conflict"
    )
    accepted = post(
        conn, work_id, {"action": "accept", "generation": 1}, node=target, revision=4, key="accept-2", nonce=NONCE_B
    )
    assert accepted["generation"] == 2 and accepted["owner_node"] == target
    assert accepted["exclusions"] == OFFER["exclusions"] and accepted["attempt_budget"] == OFFER["attempt_budget"]
    assert accepted["liveness"] == "unknown" and accepted["conflict_check"] == "not-performed"
    status, payload = request(
        conn, work_id, {"action": "release", "generation": 1}, revision=5, key="old", nonce=NONCE_A
    )
    assert status in (403, 409) and payload["code"] in {"holder-mismatch", "stale-generation"}
    assert (
        request(
            conn,
            work_id,
            {"action": "release", "generation": 2},
            node=target,
            revision=5,
            key="wrong-nonce",
            nonce=NONCE_A,
        )[1]["code"]
        == "holder-mismatch"
    )
    released = post(
        conn, work_id, {"action": "release", "generation": 2}, node=target, revision=5, key="release", nonce=NONCE_B
    )
    assert released["generation"] == 2 and released["state"] == "unowned"
    assert checkpoint["revision"] == 3 and checkpoint["evidence_refs"] == CHECKPOINT["evidence_refs"]
    post(conn, work_id, OFFER, admin=True, revision=6, key="reoffer")
    status, refusal = request(
        conn, work_id, {"action": "accept", "generation": 2}, revision=7, key="reuse", nonce=NONCE_A
    )
    assert status == 409 and refusal["code"] == "ownership-conflict"
    assert event_count(conn, work_id) == 7
    third = post(conn, work_id, {"action": "accept", "generation": 2}, revision=7, key="accept-3", nonce=NONCE_C)
    assert third["generation"] == 3


@pytest.mark.parametrize("action", ["offer", "withdraw", "accept", "checkpoint", "handoff", "release"])
def test_historical_replay_is_immutable_authenticated_and_not_a_write(ledger, action):
    conn, work_id, _ = ledger
    kwargs = {"admin": action in {"offer", "withdraw"}, "key": "original"}
    if action == "offer":
        body, revision = OFFER, 0
    elif action == "withdraw":
        post(conn, work_id, OFFER, admin=True, key="offer")
        body, revision = {"action": "withdraw"}, 1
    elif action == "accept":
        post(conn, work_id, OFFER, admin=True, key="offer")
        body, revision = {"action": "accept", "generation": 0}, 1
    else:
        owned(conn, work_id)
        body, revision = dict(CHECKPOINT) if action == "checkpoint" else {"action": action, "generation": 1}, 2
        if action == "handoff":
            body["target_node"] = "node-b"
    if not kwargs["admin"]:
        kwargs["nonce"] = NONCE_A
    original = post(conn, work_id, body, revision=revision, **kwargs)
    if action in {"offer", "withdraw", "release"}:
        follow = {"action": "withdraw"} if action == "offer" else OFFER
        post(conn, work_id, follow, admin=True, revision=original["revision"], key="later")
    elif action == "handoff":
        post(
            conn, work_id, {"action": "accept", "generation": 1}, node="node-b", revision=3, key="later", nonce=NONCE_B
        )
    else:
        post(
            conn,
            work_id,
            {"action": "release", "generation": 1},
            revision=original["revision"],
            key="later",
            nonce=NONCE_A,
        )
    before = event_count(conn, work_id)
    assert post(conn, work_id, body, revision=revision, **kwargs) == original
    assert request(conn, work_id, body, revision=revision + 1, **kwargs)[1]["code"] == "idempotency-conflict"
    if not kwargs["admin"]:
        wrong = {**kwargs, "nonce": NONCE_B}
        status, refusal = request(conn, work_id, body, revision=revision, **wrong)
        assert status == 403 and refusal["code"] == "holder-mismatch"
        wrong = {**kwargs, "node": "node-b"}
        assert request(conn, work_id, body, revision=revision, **wrong)[0] in (403, 409)
        wrong_body = {**body, "generation": 9}
        status, refusal = request(conn, work_id, wrong_body, revision=revision, **kwargs)
        assert status == 409 and refusal["code"] == "stale-generation"
    assert event_count(conn, work_id) == before


@pytest.mark.parametrize("action", ["accept", "checkpoint"])
def test_concurrent_cas_uses_separate_connections(ledger, action):
    conn, work_id, path = ledger
    if action == "accept":
        post(conn, work_id, OFFER, admin=True, key="offer")
        body, revision = {"action": "accept", "generation": 0}, 1
    else:
        owned(conn, work_id)
        body, revision = CHECKPOINT, 2
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def contend(index):
        db = fleet_hub.open_db(path)
        try:
            barrier.wait(timeout=5)
            results.append(request(db, work_id, body, revision=revision, key=f"concurrent-{index}", nonce=NONCE_A))
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=contend, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert sorted(status for status, _ in results) == [200, 409]
    assert event_count(conn, work_id) == revision + 1


@pytest.mark.parametrize("target", ["missing", "admin", "node-b"])
def test_target_must_be_enrolled_and_nonrevoked(ledger, target):
    conn, work_id, _ = ledger
    fleet_hub.revoke_node(conn, "node-b")
    assert request(conn, work_id, {**OFFER, "target_node": target}, admin=True)[0] in (400, 403)
    owned(conn, work_id)
    assert request(
        conn, work_id, {"action": "handoff", "generation": 1, "target_node": target}, revision=2, nonce=NONCE_A
    )[0] in (400, 403)
    assert event_count(conn, work_id) == 2


def test_revocation_and_lost_operator_privilege_refuse_replays_without_transfer(ledger):
    conn, work_id, _ = ledger
    post(conn, work_id, OFFER, node="node-op", operator=True, key="offer")
    post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A)
    fleet_hub.revoke_node(conn, "node-a")
    assert (
        request(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A)[0] == 403
    )
    assert request(conn, work_id, OFFER, node="node-op", key="offer")[0] == 403
    snapshot = request(conn, work_id, method="GET", node="node-b")[1]["ownership"]
    assert snapshot["owner_node"] == "node-a" and snapshot["liveness"] == "unknown"
    assert event_count(conn, work_id) == 2


@pytest.mark.parametrize("action", ["release", "withdraw"])
@pytest.mark.parametrize("terminal", ["canceled", "completed", "archived"])
def test_terminal_items_allow_safety_exits(ledger, action, terminal):
    conn, work_id, _ = ledger
    if action == "release":
        owned(conn, work_id)
        revision, body = 2, {"action": action, "generation": 1}
    else:
        post(conn, work_id, OFFER, admin=True, key="offer")
        revision, body = 1, {"action": action}
    make_terminal(conn, work_id, terminal)
    result = post(conn, work_id, body, admin=action == "withdraw", revision=revision, key="exit", nonce=NONCE_A)
    assert result["state"] == "unowned"
    assert (
        request(conn, work_id, OFFER, admin=True, revision=revision + 1, key="terminal")[1]["code"]
        == "ownership-conflict"
    )


@pytest.mark.parametrize("action", ["accept", "checkpoint", "handoff"])
@pytest.mark.parametrize("terminal", ["canceled", "completed", "archived"])
def test_terminal_items_refuse_new_owner_progress(ledger, action, terminal):
    conn, work_id, _ = ledger
    if action == "accept":
        post(conn, work_id, OFFER, admin=True, key="offer")
        body, revision = {"action": action, "generation": 0}, 1
    else:
        owned(conn, work_id)
        body = CHECKPOINT if action == "checkpoint" else {"action": action, "generation": 1, "target_node": "node-b"}
        revision = 2
    make_terminal(conn, work_id, terminal)
    assert (
        request(conn, work_id, body, revision=revision, key="terminal", nonce=NONCE_A)[1]["code"]
        == "ownership-conflict"
    )


def test_exact_event_cap_and_safety_release(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    for revision in range(2, 198):
        post(conn, work_id, CHECKPOINT, revision=revision, key=f"checkpoint-{revision}", nonce=NONCE_A)
    assert event_count(conn, work_id) == 198
    assert (
        request(
            conn,
            work_id,
            {"action": "handoff", "generation": 1, "target_node": "node-b"},
            revision=198,
            key="full-handoff",
            nonce=NONCE_A,
        )[1]["code"]
        == "ownership-capacity-exhausted"
    )
    for revision in (198, 199):
        post(conn, work_id, CHECKPOINT, revision=revision, key=f"checkpoint-{revision}", nonce=NONCE_A)
    assert (
        request(conn, work_id, CHECKPOINT, revision=200, key="full", nonce=NONCE_A)[1]["code"]
        == "ownership-capacity-exhausted"
    )
    released = post(conn, work_id, {"action": "release", "generation": 1}, revision=200, key="exit", nonce=NONCE_A)
    assert released["revision"] == 201 and event_count(conn, work_id) == 201
    assert (
        request(conn, work_id, OFFER, admin=True, revision=201, key="reoffer")[1]["code"]
        == "ownership-capacity-exhausted"
    )


def test_197_slots_allows_handoff_accept_and_exit(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    for revision in range(2, 197):
        post(conn, work_id, CHECKPOINT, revision=revision, key=f"checkpoint-{revision}", nonce=NONCE_A)
    post(
        conn,
        work_id,
        {"action": "handoff", "generation": 1, "target_node": "node-b"},
        revision=197,
        key="handoff",
        nonce=NONCE_A,
    )
    post(
        conn, work_id, {"action": "accept", "generation": 1}, node="node-b", revision=198, key="accept-2", nonce=NONCE_B
    )
    post(conn, work_id, {**CHECKPOINT, "generation": 2}, node="node-b", revision=199, key="last", nonce=NONCE_B)
    post(conn, work_id, {"action": "release", "generation": 2}, node="node-b", revision=200, key="exit", nonce=NONCE_B)
    assert event_count(conn, work_id) == 201


def test_offer_capacity_and_withdraw_boundary(ledger):
    conn, work_id, _ = ledger
    for index in range(99):
        revision = index * 2
        post(conn, work_id, OFFER, admin=True, revision=revision, key=f"offer-{index}")
        post(conn, work_id, {"action": "withdraw"}, admin=True, revision=revision + 1, key=f"withdraw-{index}")
    assert event_count(conn, work_id) == 198
    assert (
        request(conn, work_id, OFFER, admin=True, revision=198, key="full")[1]["code"] == "ownership-capacity-exhausted"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../file",
        "src/../file",
        "./file",
        "src//file",
        "src/",
        "C:/file",
        "\\share\\file",
        "src/*.py",
        "src/?",
        "src/[ab]",
        "src\\file",
        "src/\x00file",
    ],
)
def test_scope_rejects_noncanonical_paths(ledger, path):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert request(conn, work_id, {**CHECKPOINT, "write_scope": [path]}, revision=2, nonce=NONCE_A)[0] == 400
    assert event_count(conn, work_id) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"owner_node": "node-b"},
        {"nonce": NONCE_A},
        {"merged": True},
        {"conclusion": "passed"},
        {"transcript": "raw"},
        {"execution_handle": "session-1"},
        {"repo_identity": "x" * 256},
        {"write_scope": ["x"] * 33},
        {"write_scope": ["x" * 257]},
        {"source_revision": "A" * 40},
        {"source_revision": "a" * 39},
        {"next_action": {"kind": "merge", "resume_condition": ""}},
        {"next_action": {"kind": "verify", "resume_condition": "x" * 513}},
        {"evidence_refs": [{"kind": "receipt", "ref": "x", "source_revision": REVISION, "outcome": "passed"}]},
        {"evidence_refs": [{"kind": "raw-result", "ref": "x", "source_revision": REVISION}]},
        {"evidence_refs": CHECKPOINT["evidence_refs"] * 21},
        {"evidence_refs": [{"kind": "receipt", "ref": "x" * 129, "source_revision": REVISION}]},
        {"generation": True},
        {"generation": -1},
        {"generation": 10000000000},
        {"repo_identity": "token=" + "x" * 20},
    ],
)
def test_checkpoint_strict_metadata_bounds(ledger, patch):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    status, payload = request(conn, work_id, {**CHECKPOINT, **patch}, revision=2, nonce=NONCE_A)
    assert status == 400 and payload["code"] in {"unknown-field", "field-bound", "private-data"}
    assert event_count(conn, work_id) == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"exclusions": ["x"] * 33},
        {"exclusions": ["x" * 257]},
        {"exclusions": ["line\nbreak"]},
        {"authorization_ref": "x" * 129},
        {"authorization_ref": "bad/ref"},
        {"attempt_budget": {"cap": 0, "source_ref": "budget-1"}},
        {"attempt_budget": {"cap": 100001, "source_ref": "budget-1"}},
        {"attempt_budget": {"cap": True, "source_ref": "budget-1"}},
        {"attempt_budget": {"cap": 1, "source_ref": "x" * 129}},
        {"attempt_budget": {"cap": 1, "source_ref": "budget-1", "result": "raw"}},
    ],
)
def test_offer_strict_metadata_bounds(ledger, patch):
    conn, work_id, _ = ledger
    assert request(conn, work_id, {**OFFER, **patch}, admin=True)[0] == 400
    assert event_count(conn, work_id) == 0


@pytest.mark.parametrize(
    "nonce",
    [
        None,
        "",
        "x" * 43,
        NONCE_A + "=",
        NONCE_A[:-1] + "F",
        base64.urlsafe_b64encode(b"a" * 31).decode().rstrip("="),
        "x" * 10000,
    ],
)
def test_nonce_requires_canonical_32_bytes_and_never_echoes(ledger, nonce):
    conn, work_id, _ = ledger
    post(conn, work_id, OFFER, admin=True, key="offer")
    status, payload = request(conn, work_id, {"action": "accept", "generation": 0}, revision=1, nonce=nonce)
    assert status == 400
    if nonce:
        assert nonce not in json.dumps(payload)
    assert event_count(conn, work_id) == 1


def test_secrets_absent_history_immutable_and_item_version_preserved(ledger):
    conn, work_id, _ = ledger
    before = store.get_item(conn, work_id)
    schema = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    owned(conn, work_id)
    post(conn, work_id, CHECKPOINT, revision=2, key="checkpoint", nonce=NONCE_A)
    old_events = store.list_events_page(conn, work_id, limit=100)["events"]
    post(conn, work_id, {**CHECKPOINT, "evidence_refs": []}, revision=3, key="later", nonce=NONCE_A)
    events = store.list_events_page(conn, work_id, limit=100)["events"]
    assert events[: len(old_events)] == old_events
    serialized = json.dumps(events)
    assert NONCE_A not in serialized and NONCE_B not in serialized
    assert store.get_item(conn, work_id) == before
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    assert conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    assert "holder_hash" not in request(conn, work_id, method="GET")[1]["ownership"]
    assert store.list_all_events(conn, work_id=work_id, limit=100)["events"]


def test_key_action_query_and_maximum_valid_metadata(ledger):
    conn, work_id, _ = ledger
    assert request(conn, work_id, OFFER, admin=True, key="x" * 129)[0] == 400
    assert request(conn, work_id, {"action": "x" * 129}, admin=True)[0] == 400
    assert request(conn, work_id, method="GET", query="?holder=hidden")[0] == 400
    offer = {
        **OFFER,
        "exclusions": ["x" * 256] * 32,
        "authorization_ref": "x" * 128,
        "attempt_budget": {"cap": 100000, "source_ref": "x" * 128},
    }
    post(conn, work_id, offer, admin=True, key="x" * 128)
    post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A)
    maximum = {
        **CHECKPOINT,
        "repo_identity": "r" * 255,
        "write_scope": [f"{i:02d}" + "x" * 254 for i in range(32)],
        "next_action": {"kind": "blocked", "resume_condition": "x" * 512},
        "evidence_refs": [{"kind": "worklore-event", "ref": "r" * 128, "source_revision": REVISION}] * 20,
    }
    result = post(conn, work_id, maximum, revision=2, key="max", nonce=NONCE_A)
    assert result["repo_identity"] == maximum["repo_identity"]


@contextmanager
def real_hub(tmp_path):
    server = fleet_hub.make_server(
        "127.0.0.1", 0, tmp_path / "http.db", "test-admin-token"
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_http_client_header_node_auth_and_error_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    monkeypatch.setenv("BRIGADE_WORKLORE_OPERATOR_NODES", "node-op")
    with real_hub(tmp_path) as server:
        db = fleet_hub.open_db(tmp_path / "http.db")
        _, token = fleet_hub.add_node(db, "node-op", "Operator")
        item = store.create_item(db, {"title": "Example", "kind": "repo"}, actor_id="admin", actor_type="operator")
        work_id = item["work_id"]
        settings = {
            "hub_url": f"http://127.0.0.1:{server.server_address[1]}",
            "node_token": token,
            "admin_token": "test-admin-token",
        }  # content-guard: allow loopback-ipv4
        monkeypatch.setattr(client, "load_fleet_settings", lambda: settings)
        assert client.get_ownership(work_id)["ownership"]["revision"] == 0
        client.ownership_action(work_id, {**OFFER, "target_node": "node-op"}, if_match=0, idempotency_key="offer")
        nonce = client.new_ownership_nonce()
        assert len(base64.urlsafe_b64decode(nonce + "=")) == 32
        result = client.ownership_action(
            work_id, {"action": "accept", "generation": 0}, if_match=1, idempotency_key="accept", holder_nonce=nonce
        )
        assert result["ownership"]["owner_node"] == "node-op"
        assert nonce not in json.dumps(result)
        assert nonce not in json.dumps(store.list_all_events(db, work_id=work_id)["events"])
        with pytest.raises(client.WorkloreClientError) as error:
            client.ownership_action(
                work_id,
                {"action": "release", "generation": 1},
                if_match=2,
                idempotency_key="wrong",
                holder_nonce=NONCE_A,
            )
        assert error.value.code == "holder-mismatch" and nonce not in str(error.value)
        settings["node_token"] = ""
        with pytest.raises(client.FleetClientError, match="no fleet node token"):
            client.ownership_action(work_id, {"action": "withdraw"}, if_match=2, idempotency_key="no-admin-fallback")
        settings["node_token"] = token
        fleet_hub.revoke_node(db, "node-op")
        with pytest.raises(client.FleetClientError):
            client.ownership_action(
                work_id, {"action": "accept", "generation": 0}, if_match=1, idempotency_key="accept", holder_nonce=nonce
            )
        db.close()


def test_private_nonce_as_unknown_query_key_is_not_reflected(ledger):
    conn, work_id, _ = ledger
    status, payload = request(conn, work_id, method="GET", query=f"?{NONCE_A}=value")
    assert status == 400 and payload["code"] == "unknown-field"
    assert NONCE_A not in json.dumps(payload)


def test_exclusions_reject_unicode_line_separators(ledger):
    conn, work_id, _ = ledger
    assert request(conn, work_id, {**OFFER, "exclusions": ["line\u2028break"]}, admin=True)[0] == 400
    assert event_count(conn, work_id) == 0


def test_expired_claim_takeover_never_transfers_ownership(ledger, monkeypatch):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    before = request(conn, work_id, method="GET")[1]
    clock = {"now": 1700000000.0}
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: clock["now"])
    claim = {
        "action": "acquire",
        "target": "example-project",
        "node_id": "node-a",
        "holder": "session-a",
        "ttl_seconds": 1,
    }
    assert fleet_hub.handle_claim(conn, claim, caller_node="node-a")[0] == 200
    clock["now"] += 10000
    assert (
        fleet_hub.handle_claim(conn, {**claim, "node_id": "node-b", "holder": "session-b"}, caller_node="node-b")[0]
        == 200
    )
    assert request(conn, work_id, method="GET")[1] == before
    assert (
        request(
            conn,
            work_id,
            {"action": "accept", "generation": 1},
            node="node-b",
            revision=2,
            key="takeover",
            nonce=NONCE_B,
        )[0]
        == 403
    )
    assert event_count(conn, work_id) == 2


def test_accept_replay_rejects_another_action_under_same_key(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert (
        request(conn, work_id, {"action": "release", "generation": 1}, revision=2, key="accept", nonce=NONCE_A)[1][
            "code"
        ]
        == "idempotency-conflict"
    )
    assert event_count(conn, work_id) == 2


@pytest.mark.parametrize(
    "action, count, expected",
    [("accept", 199, 200), ("accept", 200, None), ("withdraw", 200, 201), ("withdraw", 201, None)],
)
def test_capacity_checks_preexisting_offered_chains(ledger, action, count, expected):
    # A bounded imported/test database can already have a long offered chain.
    # Seed immutable old events; the public route must still apply exact bounds.
    conn, work_id, _ = ledger
    post(conn, work_id, OFFER, admin=True, key="offer")
    original = conn.execute(
        "SELECT detail_json FROM work_events WHERE work_id=? AND event_type='ownership-offered'", (work_id,)
    ).fetchone()[0]
    for index in range(1, count):
        detail = json.loads(original)
        detail["ownership"]["revision"] = index + 1
        conn.execute(
            "INSERT INTO work_events (work_id,event_id,event_type,actor_type,actor_id,detail_json,occurred_at,received_at,seq) VALUES (?,?,'ownership-offered','operator','admin',?,'2000-01-01','2000-01-01',?)",
            (work_id, f"historical-{index}", json.dumps(detail), index + 2),
        )
    conn.commit()
    body = {"action": action, **({"generation": 0} if action == "accept" else {})}
    status, payload = request(
        conn, work_id, body, admin=action == "withdraw", revision=count, key="boundary", nonce=NONCE_A
    )
    if expected is None:
        assert status == 409 and payload["code"] == "ownership-capacity-exhausted"
        assert event_count(conn, work_id) == count
    else:
        assert status == 200 and payload["ownership"]["revision"] == expected
        assert event_count(conn, work_id) == expected


def metadata_body(field, value):
    body = json.loads(json.dumps(OFFER if field in {"exclusions", "authorization_ref", "source_ref"} else CHECKPOINT))
    if field == "resume_condition":
        body["next_action"][field] = value
    elif field == "ref":
        body["evidence_refs"][0][field] = value
    elif field == "source_ref":
        body["attempt_budget"][field] = value
    else:
        body[field] = [value] if field in {"write_scope", "exclusions"} else value
    return body


@pytest.mark.parametrize("decoded", [b"a" * 32, b"\xfb\xff" * 16])
@pytest.mark.parametrize("encoding", ["raw", "base64", "unpadded", "hex", "HEX"])
@pytest.mark.parametrize(
    "field",
    [
        "repo_identity",
        "write_scope",
        "resume_condition",
        "ref",
        "exclusions",
        "authorization_ref",
        "source_ref",
        "key",
        "body-key",
    ],
)
def test_presented_nonce_echo_is_private_before_validation_or_storage(ledger, decoded, encoding, field):
    conn, work_id, _ = ledger
    nonce = base64.urlsafe_b64encode(decoded).decode().rstrip("=")
    variants = {
        "raw": nonce,
        "base64": base64.b64encode(decoded).decode(),
        "unpadded": base64.b64encode(decoded).decode().rstrip("="),
        "hex": decoded.hex(),
        "HEX": decoded.hex().upper(),
    }
    value = "prefix-" + variants[encoding] + "-suffix"
    offer = field in {"exclusions", "authorization_ref", "source_ref"}
    if not offer:
        post(conn, work_id, OFFER, admin=True, key="offer")
        post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=nonce)
    body = metadata_body(field, value) if field not in {"key", "body-key"} else dict(CHECKPOINT)
    if field == "body-key":
        body[value] = "value"
    before = store.list_all_events(conn, work_id=work_id)["events"]
    status, refusal = request(
        conn,
        work_id,
        body,
        admin=offer,
        revision=0 if offer else 2,
        key=value if field == "key" else "echo",
        nonce=nonce,
    )
    assert status == 400 and refusal["code"] == "private-data"
    assert refusal["error"] == "ownership metadata must not contain holder secret"
    assert variants[encoding] not in json.dumps(refusal)
    assert store.list_all_events(conn, work_id=work_id)["events"] == before


@pytest.mark.parametrize("initial_operator", [False, True])
@pytest.mark.parametrize("action", ["accept", "checkpoint", "release"])
def test_holder_replay_uses_stable_node_principal_after_role_change(ledger, initial_operator, action):
    conn, work_id, _ = ledger
    post(conn, work_id, OFFER, admin=True, key="offer")
    accept = {"action": "accept", "generation": 0}
    if action == "accept":
        body, revision = accept, 1
    else:
        post(conn, work_id, accept, revision=1, key="accept", nonce=NONCE_A)
        body, revision = CHECKPOINT if action == "checkpoint" else {"action": "release", "generation": 1}, 2
    original = post(conn, work_id, body, revision=revision, key="original", operator=initial_operator, nonce=NONCE_A)
    if action != "release":
        post(
            conn,
            work_id,
            {"action": "release", "generation": 1},
            revision=original["revision"],
            key="later",
            nonce=NONCE_A,
        )
    else:
        post(conn, work_id, OFFER, revision=original["revision"], key="later", admin=True)
    before = store.list_all_events(conn, work_id=work_id)["events"]
    assert (
        post(conn, work_id, body, revision=revision, key="original", operator=not initial_operator, nonce=NONCE_A)
        == original
    )
    status, refusal = request(
        conn, work_id, body, revision=revision + 1, key="original", operator=not initial_operator, nonce=NONCE_A
    )
    assert status == 409 and refusal["code"] == "idempotency-conflict"
    conflicting_body = (
        {**body, "write_scope": ["src/another.py"]}
        if action == "checkpoint"
        else {"action": "handoff", "generation": 1, "target_node": "node-b"}
    )
    status, refusal = request(
        conn,
        work_id,
        conflicting_body,
        revision=revision,
        key="original",
        operator=not initial_operator,
        nonce=NONCE_A,
    )
    assert status == 409 and refusal["code"] == "idempotency-conflict"
    assert store.list_all_events(conn, work_id=work_id)["events"] == before
    holder_events = conn.execute(
        "SELECT actor_type FROM work_events WHERE work_id=? AND event_type IN ('ownership-accepted','ownership-checkpoint','ownership-released')",
        (work_id,),
    ).fetchall()
    assert all(row[0] == "node" for row in holder_events)


@pytest.mark.parametrize("action", ["offer", "withdraw"])
def test_operator_replay_still_requires_current_privilege(ledger, action):
    conn, work_id, _ = ledger
    if action == "withdraw":
        post(conn, work_id, OFFER, admin=True, key="offer")
    body, revision = (OFFER, 0) if action == "offer" else ({"action": "withdraw"}, 1)
    original = post(conn, work_id, body, node="node-op", operator=True, revision=revision, key="original")
    before = store.list_all_events(conn, work_id=work_id)["events"]
    status, refusal = request(conn, work_id, body, node="node-op", operator=False, revision=revision, key="original")
    assert status == 403 and refusal["code"] == "forbidden"
    assert post(conn, work_id, body, node="node-op", operator=True, revision=revision, key="original") == original
    assert store.list_all_events(conn, work_id=work_id)["events"] == before


@pytest.mark.parametrize(
    "path",
    ["/srv/example/repo", "C:/work/example", r"C:\work\example", r"\\example\share\repo", "//example/share/repo"],
)
@pytest.mark.parametrize("embedded", [False, True])
@pytest.mark.parametrize(
    "field",
    ["repo_identity", "write_scope", "resume_condition", "ref", "exclusions", "authorization_ref", "source_ref"],
)
def test_ownership_metadata_refuses_absolute_paths(ledger, path, embedded, field):
    conn, work_id, _ = ledger
    value = "review ('" + path + "') later" if embedded else path
    offer = field in {"exclusions", "authorization_ref", "source_ref"}
    if not offer:
        owned(conn, work_id)
    status, refusal = request(
        conn,
        work_id,
        metadata_body(field, value),
        admin=offer,
        revision=0 if offer else 2,
        nonce=None if offer else NONCE_A,
    )
    assert status == 400 and refusal["code"] in {"field-bound", "private-data"}
    assert path not in refusal["error"]
    assert event_count(conn, work_id) == (0 if offer else 2)


@pytest.mark.parametrize(
    "identity", ["../repo", "repo/../other", "./repo", "repo//other", "repo/", "repo name", "repo/*"]
)
def test_repository_identity_is_canonical_relative(ledger, identity):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    assert request(conn, work_id, {**CHECKPOINT, "repo_identity": identity}, revision=2, nonce=NONCE_A)[0] == 400
    assert event_count(conn, work_id) == 2


@pytest.mark.parametrize("char", ["é", "😀"])
def test_serialized_event_byte_bound_and_small_safety_exit(ledger, char):
    conn, work_id, _ = ledger
    offer = {**OFFER, "exclusions": [char * 256] * 32}
    if char == "😀":
        status, refusal = request(conn, work_id, offer, admin=True)
        assert status == 400 and refusal["code"] == "field-bound"
        assert event_count(conn, work_id) == 0
        post(conn, work_id, OFFER, admin=True, key="small-offer")
        post(conn, work_id, {"action": "withdraw"}, admin=True, revision=1, key="exit")
    else:
        post(conn, work_id, offer, admin=True, key="offer")
        post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A)
        body = {**CHECKPOINT, "write_scope": [f"{i:02d}" + char * 254 for i in range(32)]}
        before = store.list_all_events(conn, work_id=work_id)["events"]
        status, refusal = request(conn, work_id, body, revision=2, key="large", nonce=NONCE_A)
        assert status == 400 and refusal["code"] == "field-bound"
        assert store.list_all_events(conn, work_id=work_id)["events"] == before
        post(conn, work_id, {"action": "release", "generation": 1}, revision=2, key="exit", nonce=NONCE_A)
    details = conn.execute(
        "SELECT detail_json FROM work_events WHERE work_id=? AND event_type LIKE 'ownership-%'", (work_id,)
    ).fetchall()
    assert all(len(row[0].encode()) <= 65536 for row in details)


@pytest.mark.parametrize("reacquire", [False, True])
def test_new_accept_cannot_reuse_any_prior_nonce_but_historical_replay_works(ledger, reacquire):
    conn, work_id, _ = ledger
    original = owned(conn, work_id)
    if reacquire:
        post(conn, work_id, {"action": "release", "generation": 1}, revision=2, key="release", nonce=NONCE_A)
        post(conn, work_id, OFFER, admin=True, revision=3, key="reoffer")
        revision = 4
    else:
        post(
            conn,
            work_id,
            {"action": "handoff", "generation": 1, "target_node": "node-a"},
            revision=2,
            key="handoff",
            nonce=NONCE_A,
        )
        revision = 3
    before = store.list_all_events(conn, work_id=work_id)["events"]
    status, refusal = request(
        conn, work_id, {"action": "accept", "generation": 1}, revision=revision, key="reuse", nonce=NONCE_A
    )
    assert status == 409 and refusal["code"] == "ownership-conflict"
    assert store.list_all_events(conn, work_id=work_id)["events"] == before
    fresh = post(conn, work_id, {"action": "accept", "generation": 1}, revision=revision, key="fresh", nonce=NONCE_B)
    assert fresh["generation"] == 2
    assert (
        post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A) == original
    )
    assert event_count(conn, work_id) == revision + 1
    assert (
        request(
            conn,
            work_id,
            {"action": "release", "generation": 2},
            revision=revision + 1,
            key="old-holder",
            nonce=NONCE_A,
        )[1]["code"]
        == "holder-mismatch"
    )


def make_terminal(conn, work_id, terminal):
    if terminal == "canceled":
        states, version = ["canceled"], 1
    else:
        store.patch_item(conn, work_id, {"acceptance": ["example acceptance"]}, expected_version=1, actor_id="admin")
        states, version = ["ready", "claimed", "running", "verifying", "completed"], 2
        if terminal == "archived":
            states.append("archived")
    for state in states:
        store.transition(conn, work_id, to_status=state, expected_version=version, actor_id="admin")
        version += 1


def test_relative_references_and_ordinary_prose_remain_supported(ledger):
    conn, work_id, _ = ledger
    offer = {**OFFER, "exclusions": ["leave docs/reference.md for later review"]}
    post(conn, work_id, offer, admin=True, key="offer")
    post(conn, work_id, {"action": "accept", "generation": 0}, revision=1, key="accept", nonce=NONCE_A)
    body = {
        **CHECKPOINT,
        "next_action": {"kind": "blocked", "resume_condition": "review src/module.py when checks complete"},
    }
    result = post(conn, work_id, body, revision=2, key="checkpoint", nonce=NONCE_A)
    assert result["next_action"] == body["next_action"] and result["exclusions"] == offer["exclusions"]


def test_accept_vs_withdraw_serializes_to_one_complete_result(ledger):
    conn, work_id, path = ledger
    offered = post(conn, work_id, OFFER, admin=True, key="offer")
    barrier = threading.Barrier(2)
    results, errors = {}, []

    def contend(action):
        db = fleet_hub.open_db(path)
        try:
            barrier.wait(timeout=5)
            body = {"action": action, **({"generation": 0} if action == "accept" else {})}
            results[action] = request(
                db,
                work_id,
                body,
                admin=action == "withdraw",
                revision=1,
                key=action,
                nonce=NONCE_A if action == "accept" else None,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=contend, args=(action,)) for action in ("accept", "withdraw")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert sorted(status for status, _ in results.values()) == [200, 409]
    winner = next(action for action, (status, _) in results.items() if status == 200)
    loser = "withdraw" if winner == "accept" else "accept"
    assert results[loser][1]["code"] == "version-conflict"
    final = request(conn, work_id, method="GET")[1]["ownership"]
    assert final == results[winner][1]["ownership"]
    assert final["state"] == ("owned" if winner == "accept" else "unowned")
    assert final["revision"] == 2 and event_count(conn, work_id) == 2
    assert offered["state"] == "offered" and offered["revision"] == 1


def dot_report(**updates):
    return {
        "version": 1,
        "provider": "dot",
        "session_id": "session-a",
        "agent_label": "worker-a",
        "source": "cloud_threads",
        "source_scope": "caller-created-tasks",
        "coverage": "explicitly-reported-sessions",
        "observed_at": "2026-01-01T00:00:00Z",
        "sequence": 1,
        "progress": "Checking fixture",
        "result": "Checks reported complete",
        "evidence_refs": ["receipt-a"],
        **updates,
    }


def test_holder_report_is_fenced_immutable_and_does_not_change_work(ledger):
    conn, work_id, _ = ledger
    accepted = owned(conn, work_id)
    item_before = store.get_item(conn, work_id)
    body = {"action": "report", "generation": 1, "report": dot_report()}
    reported = post(conn, work_id, body, revision=2, key="report-a", nonce=NONCE_A)
    assert reported["revision"] == 3
    assert reported["last_report"]["progress"] == "Checking fixture"
    assert reported["last_report"]["evidence_verification"] == "reported-unverified"
    for field in (
        "write_scope",
        "source_revision",
        "next_action",
        "state",
        "generation",
        "exclusions",
        "attempt_budget",
        "evidence_refs",
    ):
        assert reported[field] == accepted[field]
    assert store.get_item(conn, work_id) == item_before
    assert post(conn, work_id, body, revision=2, key="report-a", nonce=NONCE_A) == reported
    assert event_count(conn, work_id) == 3
    assert conn.execute("SELECT COUNT(*) FROM work_events WHERE event_type='ownership-reported'").fetchone()[0] == 1
    for kwargs in (
        {"node": "node-b", "nonce": NONCE_A},
        {"nonce": None},
        {"nonce": NONCE_B},
        {"admin": True, "nonce": NONCE_A},
    ):
        assert request(conn, work_id, body, revision=3, key="refused", **kwargs)[0] in {400, 403}
    status, refusal = request(
        conn, work_id, {**body, "report": dot_report(progress="Different")}, revision=2, key="report-a", nonce=NONCE_A
    )
    assert status == 409 and refusal["code"] == "idempotency-conflict"
    assert event_count(conn, work_id) == 3


def test_holder_report_refuses_unknown_fields_and_stale_observations(ledger):
    conn, work_id, _ = ledger
    owned(conn, work_id)
    body = {"action": "report", "generation": 1, "report": dot_report()}
    for metadata in (
        dot_report(transcript="not accepted"),
        dot_report(coverage="account-wide"),
        dot_report(progress=NONCE_A),
        dot_report(progress="x" * 401),
    ):
        status, _ = request(conn, work_id, {**body, "report": metadata}, revision=2, key="invalid", nonce=NONCE_A)
        assert status == 400
    post(conn, work_id, body, revision=2, key="report-a", nonce=NONCE_A)
    for metadata in (
        dot_report(sequence=0),
        dot_report(sequence=1, progress="Changed"),
        dot_report(sequence=2, observed_at="2025-01-01T00:00:00Z"),
    ):
        status, refusal = request(conn, work_id, {**body, "report": metadata}, revision=3, key="stale", nonce=NONCE_A)
        assert status == 409 and refusal["code"] == "ownership-conflict"
    assert event_count(conn, work_id) == 3


@pytest.mark.parametrize(
    "changed",
    [
        {"sequence": 9},
        {"sequence": 11, "agent_label": "other-agent"},
        {"sequence": 11, "parent_session_id": "other-parent"},
        {"sequence": 11, "source_scope": "authorized-visible-threads"},
        {"sequence": 11, "source": "explicit-metadata", "source_scope": "explicit-session"},
        {"sequence": 11, "repo_identity": "github.com/example/project"},
    ],
)
@pytest.mark.parametrize("direct", [False, True])
def test_interleaved_reports_keep_per_reporter_session_history_fenced(ledger, changed, direct):
    from brigade import worklore_ownership

    conn, work_id, _ = ledger
    owned(conn, work_id)
    post(
        conn,
        work_id,
        {"action": "report", "generation": 1, "report": dot_report(sequence=10)},
        revision=2,
        key="a-10",
        nonce=NONCE_A,
    )
    post(
        conn,
        work_id,
        {"action": "report", "generation": 1, "report": dot_report(session_id="session-b", sequence=1)},
        revision=3,
        key="b-1",
        nonce=NONCE_A,
    )
    before = store.list_all_events(conn, work_id=work_id)["events"]
    body = {"action": "report", "generation": 1, "report": dot_report(**changed)}
    if direct:
        with pytest.raises(store.WorkloreConflict) as refusal:
            worklore_ownership.ownership_action(
                conn,
                work_id,
                body,
                expected_revision=4,
                idempotency_key="a-again",
                actor_id="node-a",
                actor_type="node",
                holder_nonce=NONCE_A,
            )
        assert refusal.value.code == "ownership-conflict"
    else:
        status, refusal = request(conn, work_id, body, revision=4, key="a-again", nonce=NONCE_A)
        assert status == 409 and refusal["code"] == "ownership-conflict"
    assert store.list_all_events(conn, work_id=work_id)["events"] == before
    current = request(conn, work_id, method="GET")[1]["ownership"]
    assert current["last_report"]["session_id"] == "session-b"
    assert current["last_report"]["reporter_node"] == "node-a"
