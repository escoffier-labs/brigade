"""Campaign self-assignment contracts through HTTP and competing SQLite connections."""

import base64
import copy
import json
import threading

import pytest

from brigade import fleet_hub
from brigade import worklore_campaigns as campaigns
from brigade import worklore_http as http
from brigade import worklore_store as store

NONCE_A = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
NONCE_B = base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")
OFFER = {
    "allowed_nodes": ["node-a", "node-b"],
    "per_node_concurrency": 1,
    "attempt_budget": {"cap": 2, "source_ref": "budget-1"},
    "authorization_ref": "grant-1",
    "exclusions": ["acme/excluded"],
    "lease_seconds": 60,
}


def call(conn, path, body=None, *, node="node-a", admin=False, headers=None, method="POST"):
    return http.handle(
        conn,
        http.Request(
            method=method,
            path=path,
            node_id=None if admin else node,
            is_admin=admin,
            operator_authorization_resolved=True,
            body=body or {},
            headers=headers or {},
        ),
    )


def create_body(*, offer=OFFER, key="camp-1"):
    body = {
        "campaign_key": key,
        "title": "Update fixtures",
        "acceptance": ["Checks pass"],
        "members": [{"repo": repo} for repo in ("acme/one", "acme/two", "acme/excluded")],
    }
    if offer is not None:
        body["offer"] = copy.deepcopy(offer)
    return body


def create(conn, *, offer=OFFER, key="camp-1"):
    status, payload = call(conn, "/work/campaigns", create_body(offer=offer, key=key), admin=True)
    assert status in {200, 201}, payload
    return payload


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    clock = ["2026-01-01T00:00:00+00:00"]
    monkeypatch.setattr(store, "_utc_now", lambda: clock[0])
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    for node in ("node-a", "node-b", "node-c"):
        fleet_hub.add_node(conn, node, node)
    yield conn, path, clock
    conn.close()


def members(conn):
    return {m["repo"]: m["work_id"] for m in campaigns.show_campaign(conn, "camp-1")["members"]}


def act(conn, work_id, action="take", *, revision=0, generation=0, key="take-1", nonce=NONCE_A, **kwargs):
    return call(
        conn,
        f"/work/items/{work_id}/ownership",
        {"action": action, "generation": generation},
        headers={"If-Match": str(revision), "Idempotency-Key": key, "X-Worklore-Holder": nonce},
        **kwargs,
    )


def snapshot(conn, work_id):
    return call(conn, f"/work/items/{work_id}/ownership", method="GET")[1]["ownership"]


def test_legacy_campaign_is_closed_until_explicit_offer_and_member_has_backlink(ledger):
    conn, path, _ = ledger
    made = create(conn, offer=None)
    work_id = members(conn)["acme/one"]
    status, refusal = act(conn, work_id)
    assert status == 403 and refusal["code"] == "campaign-not-authorized"
    before = store.list_events(conn, work_id)
    # Existing schema-40 databases need no new table/column to gain authorization.
    conn.close()
    conn = fleet_hub.open_db(path)
    try:
        assert act(conn, work_id)[0] == 403
        authorized = create(conn)
        assert authorized["campaign"]["offer"] == OFFER
        status, result = act(conn, work_id)
        assert status == 200, result
        assert result["ownership"]["generation"] == 1
        item = call(conn, f"/work/items/{work_id}", method="GET")[1]["item"]
        assert item["campaign"] == {
            "campaign_key": "camp-1",
            "work_id": made["campaign"]["work_id"],
            "repo": "acme/one",
        }
        assert item["attempt_count"] == 0 and item["status"] == "captured"
        assert len(store.list_events(conn, work_id)) == len(before) + 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "change",
    [
        {"allowed_nodes": []},
        {"allowed_nodes": ["admin"]},
        {"allowed_nodes": ["node-a"] * 2},
        {"allowed_nodes": ["node-a"] * 33},
        {"per_node_concurrency": 0},
        {"per_node_concurrency": True},
        {"per_node_concurrency": 33},
        {"attempt_budget": {"cap": 0, "source_ref": "budget-1"}},
        {"exclusions": ["../private"]},
        {"lease_seconds": 0},
        {"lease_seconds": 3601},
        {"unexpected": "ignored?"},
    ],
)
def test_invalid_offer_refuses_entire_campaign(ledger, change):
    conn, _, _ = ledger
    status, refusal = call(conn, "/work/campaigns", create_body(offer={**OFFER, **change}), admin=True)
    assert status == 400 and refusal["code"] in {"field-bound", "unknown-field"}
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 0


def test_conflicting_offer_cannot_change_authority_or_add_members(ledger):
    conn, _, _ = ledger
    create(conn)
    before = conn.execute("SELECT COUNT(*) FROM work_events").fetchone()[0]
    assert create(conn)["campaign"]["offer"] == OFFER
    body = {
        "campaign_key": "camp-1",
        "title": "Update fixtures",
        "acceptance": ["Checks pass"],
        "members": [{"repo": "acme/new"}],
        "offer": {**OFFER, "allowed_nodes": ["node-c"]},
    }
    status, refusal = call(conn, "/work/campaigns", body, admin=True)
    assert status == 409 and refusal["code"] == "campaign-offer-conflict"
    assert conn.execute("SELECT COUNT(*) FROM work_events").fetchone()[0] == before
    assert "acme/new" not in members(conn)


@pytest.mark.parametrize("caller", [{"node": "node-c"}, {"admin": True}, {"node": None}, {"node": "missing"}])
def test_take_requires_authenticated_allowed_enrolled_node(ledger, caller):
    conn, _, _ = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    before = store.list_events(conn, work_id)
    assert act(conn, work_id, **caller)[0] in {401, 403}
    assert store.list_events(conn, work_id) == before


@pytest.mark.parametrize(
    "status,attempts,offer_cap,repo,drift",
    [
        ("completed", 0, 2, "acme/one", False),
        ("canceled", 0, 2, "acme/one", False),
        ("archived", 0, 2, "acme/one", False),
        ("blocked", 0, 2, "acme/one", False),
        ("running", 0, 2, "acme/one", False),
        ("claimed", 0, 2, "acme/one", False),
        ("verifying", 0, 2, "acme/one", False),
        ("deferred", 0, 2, "acme/one", False),
        ("captured", 2, 100, "acme/one", False),
        ("ready", 1, 1, "acme/one", False),
        ("ready", 0, 2, "acme/excluded", False),
        ("ready", 0, 2, "acme/one", True),
    ],
)
def test_ineligible_member_refused_without_grant(ledger, status, attempts, offer_cap, repo, drift):
    conn, _, _ = ledger
    create(conn, offer={**OFFER, "attempt_budget": {"cap": offer_cap, "source_ref": "budget-1"}})
    work_id = members(conn)[repo]
    conn.execute("UPDATE work_items SET status=?, attempt_count=? WHERE work_id=?", (status, attempts, work_id))
    conn.commit()
    if drift:
        campaign_id = campaigns.show_campaign(conn, "camp-1")["campaign"]["work_id"]
        store.patch_item(conn, campaign_id, {"description": "Changed"}, expected_version=1, actor_id="admin")
    before = store.list_events(conn, work_id)
    assert act(conn, work_id)[0] == 409
    assert store.list_events(conn, work_id) == before
    assert snapshot(conn, work_id)["state"] == "unowned"


def test_replay_is_same_grant_at_cap_and_conflicting_reuse_is_refused(ledger):
    conn, _, clock = ledger
    create(conn)
    ids = members(conn)
    work_id = ids["acme/one"]
    status, grant = act(conn, work_id)
    assert status == 200, grant
    assert grant["ownership"]["attempt_budget"] == OFFER["attempt_budget"]
    assert grant["ownership"]["authorization_ref"] == "grant-1"
    assert grant["ownership"]["exclusions"] == OFFER["exclusions"]
    before = store.list_events(conn, work_id)
    clock[0] = "2026-01-01T00:00:30+00:00"
    assert act(conn, work_id) == (200, grant)
    for target, kwargs in [(work_id, {"revision": 1}), (work_id, {"nonce": NONCE_B}), (ids["acme/two"], {})]:
        status, refusal = act(conn, target, **kwargs)
        assert status == 409 and refusal["code"] == "idempotency-conflict"
    assert store.list_events(conn, work_id) == before
    status, refusal = act(conn, ids["acme/two"], key="different")
    assert status == 409 and refusal["code"] == "campaign-concurrency-limit"
    assert NONCE_A not in json.dumps(store.list_events(conn, work_id))


@pytest.mark.parametrize("same_node", [False, True])
def test_competing_connections_serialize_member_and_per_node_limit(ledger, same_node):
    conn, path, _ = ledger
    create(conn)
    ids = members(conn)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def contender(index):
        db = fleet_hub.open_db(path)
        try:
            barrier.wait(timeout=5)
            work_id = ids["acme/two"] if same_node and index else ids["acme/one"]
            results.append(
                act(
                    db,
                    work_id,
                    node="node-a" if same_node or not index else "node-b",
                    nonce=NONCE_B if index else NONCE_A,
                    key=f"race-{index}",
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    threads = [threading.Thread(target=contender, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert sorted(status for status, _ in results) == [200, 409]
    held = [snapshot(conn, work_id) for work_id in ids.values()]
    assert sum(s["state"] == "owned" for s in held) == 1


def test_expired_holder_reclaimed_with_new_generation_cannot_write(ledger):
    conn, _, clock = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    status, original = act(conn, work_id)
    assert status == 200, original
    clock[0] = "2026-01-01T00:01:00+00:00"
    checkpoint = {
        "action": "checkpoint",
        "generation": 1,
        "repo_identity": "acme/one",
        "write_scope": ["src"],
        "source_revision": "a" * 40,
        "next_action": {"kind": "implement", "resume_condition": ""},
        "evidence_refs": [],
    }
    headers = {"If-Match": "1", "Idempotency-Key": "expired-checkpoint", "X-Worklore-Holder": NONCE_A}
    status, refusal = call(conn, f"/work/items/{work_id}/ownership", checkpoint, headers=headers)
    assert status == 409 and refusal["code"] == "stale-generation"
    status, new = act(conn, work_id, revision=1, generation=1, key="reclaim", node="node-b", nonce=NONCE_B)
    assert status == 200, new
    assert new["ownership"]["generation"] == 2 and new["ownership"]["owner_node"] == "node-b"
    headers["If-Match"] = "2"
    assert call(conn, f"/work/items/{work_id}/ownership", checkpoint, headers=headers)[1]["code"] == "stale-generation"
    assert act(conn, work_id) == (200, original)  # historical reply cannot restore authority
    assert snapshot(conn, work_id) == new["ownership"]
    assert (
        act(conn, work_id, "release", revision=2, generation=2, key="release", node="node-b", nonce=NONCE_A)[0] == 403
    )
    assert (
        act(conn, work_id, "release", revision=2, generation=2, key="release", node="node-b", nonce=NONCE_B)[0] == 200
    )


def test_campaign_members_cannot_bypass_offer_with_legacy_assignment(ledger):
    conn, _, _ = ledger
    create(conn, offer=None)
    work_id = members(conn)["acme/one"]
    body = {
        "action": "offer",
        "target_node": "node-a",
        "exclusions": [],
        "authorization_ref": "grant-2",
        "attempt_budget": {"cap": 2, "source_ref": "budget-1"},
    }
    status, refusal = call(
        conn,
        f"/work/items/{work_id}/ownership",
        body,
        admin=True,
        headers={"If-Match": "0", "Idempotency-Key": "bypass"},
    )
    assert status == 403 and refusal["code"] == "campaign-not-authorized"


def test_expired_running_member_can_be_reclaimed_but_attempt_limit_still_applies(ledger):
    conn, _, clock = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    conn.execute("UPDATE work_items SET status='running', attempt_count=1 WHERE work_id=?", (work_id,))
    conn.commit()
    clock[0] = "2026-01-01T00:01:00+00:00"
    status, grant = act(conn, work_id, node="node-b", nonce=NONCE_B, revision=1, generation=1, key="reclaim")
    assert status == 200, grant
    assert grant["ownership"]["generation"] == 2
    conn.execute("UPDATE work_items SET attempt_count=2 WHERE work_id=?", (work_id,))
    conn.commit()
    clock[0] = "2026-01-01T00:02:00+00:00"
    status, refusal = act(conn, work_id, revision=2, generation=2, key="exhausted")
    assert status == 409 and refusal["code"] == "campaign-ineligible"


def test_independent_nodes_have_separate_caps_and_release_frees_capacity(ledger):
    conn, _, _ = ledger
    create(conn)
    ids = members(conn)
    assert act(conn, ids["acme/one"])[0] == 200
    assert act(conn, ids["acme/two"], node="node-b", nonce=NONCE_B, key="take-b")[0] == 200
    assert campaigns.show_campaign(conn, "camp-1")["campaign"]["summary"]["in_flight"] == 2
    assert act(conn, ids["acme/one"], "release", revision=1, generation=1, key="release")[0] == 200
    status, grant = act(conn, ids["acme/one"], revision=2, generation=1, nonce=NONCE_B, key="retake")
    assert status == 200 and grant["ownership"]["generation"] == 2


def checkpoint(conn, work_id, *, revision=1, generation=1, repo="acme/one", key="checkpoint", nonce=NONCE_A, **kwargs):
    body = {
        "action": "checkpoint",
        "generation": generation,
        "repo_identity": repo,
        "write_scope": ["src"],
        "source_revision": "a" * 40,
        "next_action": {"kind": "implement", "resume_condition": ""},
        "evidence_refs": [],
    }
    return call(
        conn,
        f"/work/items/{work_id}/ownership",
        body,
        headers={"If-Match": str(revision), "Idempotency-Key": key, "X-Worklore-Holder": nonce},
        **kwargs,
    )


def test_checkpoint_is_bound_to_member_repo_and_reclaim_clears_old_scope(ledger):
    conn, _, clock = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    assert checkpoint(conn, work_id, repo="acme/other")[0] == 403
    assert checkpoint(conn, work_id)[0] == 200
    create(conn, key="camp-2")
    peer = campaigns.show_campaign(conn, "camp-2")["members"]
    peer_id = next(m["work_id"] for m in peer if m["repo"] == "acme/one")
    assert act(conn, peer_id, node="node-b", nonce=NONCE_B, key="peer")[0] == 200
    status, refusal = checkpoint(conn, peer_id, node="node-b", nonce=NONCE_B, key="peer-scope")
    assert status == 409 and refusal["code"] == "path-scope-conflict"
    clock[0] = "2026-01-01T00:01:00+00:00"
    status, reclaimed = act(conn, work_id, revision=2, generation=1, nonce=NONCE_B, key="reclaim")
    assert status == 200 and reclaimed["ownership"]["write_scope"] == []
    assert act(conn, peer_id, revision=1, generation=1, key="peer-reclaim")[0] == 200
    assert checkpoint(conn, peer_id, revision=2, generation=2, key="new-scope")[0] == 200
    status, refusal = checkpoint(conn, work_id, revision=3, generation=2, key="reclaimed-scope", nonce=NONCE_B)
    assert status == 409 and refusal["code"] == "path-scope-conflict"


def test_campaign_cancellation_stops_new_progress_but_holder_can_release(ledger):
    conn, _, _ = ledger
    made = create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    store.transition(conn, made["campaign"]["work_id"], to_status="canceled", expected_version=1, actor_id="admin")
    assert act(conn, members(conn)["acme/two"], node="node-b", nonce=NONCE_B, key="new")[0] == 409
    status, refusal = checkpoint(conn, work_id)
    assert status == 409 and refusal["code"] == "campaign-ineligible"
    assert act(conn, work_id, "release", revision=1, generation=1, key="release")[0] == 200


def test_take_storage_failure_rolls_back_and_same_request_can_retry(ledger):
    conn, _, _ = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    before = store.list_events(conn, work_id)
    conn.execute(
        "CREATE TRIGGER reject_take BEFORE INSERT ON work_events WHEN NEW.event_type='ownership-taken' "
        "BEGIN SELECT RAISE(ABORT, 'fixture-storage-failure'); END"
    )
    conn.commit()
    assert act(conn, work_id)[0] == 500
    assert not conn.in_transaction
    assert store.list_events(conn, work_id) == before
    assert snapshot(conn, work_id)["generation"] == 0
    conn.execute("DROP TRIGGER reject_take")
    conn.commit()
    assert act(conn, work_id)[0] == 200


def test_real_http_take_uses_bearer_identity_and_rejects_spoofed_body(tmp_path, monkeypatch):
    from brigade import worklore_client as client

    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    server = fleet_hub.make_server(
        "127.0.0.1",  # content-guard: allow loopback-ipv4
        0,
        tmp_path / "http.db",
        "test-admin-token",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = fleet_hub.open_db(tmp_path / "http.db")
    try:
        _, allowed_token = fleet_hub.add_node(db, "node-a", "Allowed node")
        _, other_token = fleet_hub.add_node(db, "node-c", "Other node")
        create(db)
        work_id = members(db)["acme/one"]
        settings = {
            "hub_url": f"http://127.0.0.1:{server.server_address[1]}",  # content-guard: allow loopback-ipv4
            "node_token": other_token,
            "admin_token": "test-admin-token",
        }
        monkeypatch.setattr(client, "load_fleet_settings", lambda: settings)

        def take(body, key="take"):
            return client.ownership_action(work_id, body, if_match=0, idempotency_key=key, holder_nonce=NONCE_A)

        with pytest.raises(client.WorkloreClientError) as denial:
            take({"action": "take", "generation": 0})
        assert denial.value.code == "campaign-not-authorized"
        with pytest.raises(client.WorkloreClientError) as spoof:
            take({"action": "take", "generation": 0, "node_id": "node-a"})
        assert spoof.value.code == "unknown-field"
        settings["node_token"] = allowed_token
        grant = take({"action": "take", "generation": 0})
        assert grant["ownership"]["owner_node"] == "node-a"
        assert client.get_item(work_id)["item"]["campaign"]["campaign_key"] == "camp-1"
        fleet_hub.revoke_node(db, "node-a")
        with pytest.raises(client.WorkloreClientError):
            take({"action": "take", "generation": 0})
        assert snapshot(db, work_id) == grant["ownership"]
    finally:
        db.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_exact_take_replay_survives_legacy_item_key_reuse(ledger):
    conn, _, _ = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    status, grant = act(conn, work_id)
    assert status == 200, grant
    legacy = store.create_item(conn, {"title": "Independent work", "kind": "repo"}, actor_id="admin")
    status, reply = http.handle(
        conn,
        http.Request(
            method="POST",
            path=f"/work/items/{legacy['work_id']}/ownership",
            node_id="node-a",
            is_operator=True,
            operator_authorization_resolved=True,
            body={
                "action": "offer",
                "target_node": "node-b",
                "authorization_ref": "grant-2",
                "attempt_budget": {"cap": 2, "source_ref": "budget-1"},
                "exclusions": [],
            },
            headers={"If-Match": "0", "Idempotency-Key": "take-1"},
        ),
    )
    assert status == 200, reply  # Legacy ownership keeps its item-scoped keys.
    before = store.list_events(conn, work_id)
    assert act(conn, work_id) == (200, grant)
    assert store.list_events(conn, work_id) == before


@pytest.fixture
def live_campaign(tmp_path, monkeypatch):
    """Exercise the real bearer authentication boundary, not caller role fields."""
    import urllib.error
    import urllib.request

    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    monkeypatch.setenv("BRIGADE_WORKLORE_OPERATOR_NODES", "node-c")
    clock = ["2026-01-01T00:00:00+00:00"]
    monkeypatch.setattr(store, "_utc_now", lambda: clock[0])
    path = tmp_path / "live.db"
    conn = fleet_hub.init_db(path)
    tokens = {"admin": "test-admin-token"}
    for node in ("node-a", "node-b", "node-c", "node-d"):
        _, tokens[node] = fleet_hub.add_node(conn, node, node)
    server = fleet_hub.make_server(
        "127.0.0.1",
        0,
        path,
        tokens["admin"],  # content-guard: allow loopback-ipv4
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(work_id, tail, body, *, node="node-a", headers=None, method="POST"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/work/items/{work_id}/{tail}",  # content-guard: allow loopback-ipv4
            data=json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {tokens[node]}", "Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    yield conn, clock, request, server, tokens
    server.shutdown()
    server.server_close()
    thread.join()
    conn.close()


def fleet_run(conn, node="node-a", run="run-1"):
    conn.execute(
        "INSERT INTO events (node_id, run_id, sequence, digest, state, ts, received_at) "
        "VALUES (?, ?, 1, 'fixture-digest', 'run.created', 't', 't')",
        (node, run),
    )
    conn.commit()


@pytest.mark.parametrize("endpoint", ["execution", "attempts"])
@pytest.mark.parametrize("condition", ["no-offer", "unowned", "disallowed", "canceled", "expired", "reclaimed"])
def test_campaign_execution_and_attempt_http_require_current_grant(live_campaign, endpoint, condition):
    conn, clock, request, _, _ = live_campaign
    made = create(conn, offer=None if condition == "no-offer" else OFFER)
    work_id = members(conn)["acme/one"]
    node = "node-d" if condition == "disallowed" else "node-a"
    fleet_run(conn, node)
    if condition not in {"no-offer", "unowned", "disallowed"}:
        assert act(conn, work_id)[0] == 200
    if endpoint == "attempts":
        # Trusted operator observation creates a real link, without granting node authority.
        assert request(work_id, "execution", {"node_id": node, "run_id": "run-1"}, node="admin")[0] == 201
    if condition == "canceled":
        store.transition(conn, made["campaign"]["work_id"], to_status="canceled", expected_version=1, actor_id="admin")
    if condition in {"expired", "reclaimed"}:
        clock[0] = "2026-01-01T00:01:00+00:00"
    if condition == "reclaimed":
        assert act(conn, work_id, node="node-b", revision=1, generation=1, nonce=NONCE_B, key="reclaim")[0] == 200
    body = {"node_id": node, "run_id": "run-1"} if endpoint == "execution" else {"action": "failed", "run_id": "run-1"}
    before = store.list_events(conn, work_id)
    item = store.get_item(conn, work_id)
    status, refusal = request(
        work_id,
        endpoint,
        body,
        node=node,
        headers={"If-Match": str(item["version"]), "X-Worklore-Holder": NONCE_A, "X-Worklore-Generation": "1"},
    )
    assert status in {403, 409}, refusal
    assert refusal["code"] in {
        "campaign-not-authorized",
        "campaign-ineligible",
        "holder-mismatch",
        "stale-generation",
        "ownership-conflict",
    }
    assert store.list_events(conn, work_id) == before
    assert store.get_item(conn, work_id) == item
    if endpoint == "execution":
        assert not store.list_links(conn, work_id)


def test_campaign_http_execution_binding_replay_and_budget(live_campaign):
    conn, clock, request, _, _ = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    fleet_run(conn)
    assert act(conn, work_id)[0] == 200
    headers = {"X-Worklore-Holder": NONCE_A, "X-Worklore-Generation": "1", "If-Match": "1"}
    execution = {"node_id": "node-a", "run_id": "run-1"}
    status, link = request(work_id, "execution", execution, headers=headers)
    assert status == 201, link
    failed = {"action": "failed", "run_id": "run-1"}
    assert request(work_id, "attempts", failed, headers=headers)[1]["item"]["attempt_count"] == 1
    before = store.list_events(conn, work_id)
    clock[0] = "2026-01-01T00:01:00+00:00"
    assert request(work_id, "execution", execution, headers=headers) == (201, link)
    assert request(work_id, "attempts", failed, headers=headers)[1]["item"]["attempt_count"] == 1
    assert store.list_events(conn, work_id) == before
    # Same authenticated node with a fresh capability cannot spend the old run's budget.
    assert act(conn, work_id, revision=1, generation=1, nonce=NONCE_B, key="reclaim")[0] == 200
    assert (
        request(
            work_id,
            "attempts",
            {**failed, "action": "started"},
            headers={"X-Worklore-Holder": NONCE_B, "X-Worklore-Generation": "2", "If-Match": "2"},
        )[0]
        == 409
    )
    assert store.get_item(conn, work_id)["attempt_count"] == 1


def execution_state(conn):
    """Compare persisted effects, including counters and retention, across HTTP refusals."""
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        for table in ("work_items", "work_events", "work_links")
    }


def fill_execution_retention(conn, request, work_id, headers):
    for index in range(51):
        run_id = f"retained-{index}"
        fleet_run(conn, run=run_id)
        status, reply = request(work_id, "execution", {"node_id": "node-a", "run_id": run_id}, headers=headers)
        assert status == 201, reply
    links = store.list_links(conn, work_id)
    assert len(links) == 50
    assert "node-a/retained-0" not in {link["external_key"] for link in links}


@pytest.mark.parametrize("destination", ["reclaimed", "other-member", "noncampaign"])
@pytest.mark.parametrize("observer", ["node-a", "admin", "operator"])
def test_campaign_execution_history_survives_retention_and_refuses_rebinding(
    live_campaign, monkeypatch, destination, observer
):
    conn, clock, request, _, _ = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    headers = {"X-Worklore-Holder": NONCE_A, "X-Worklore-Generation": "1", "If-Match": "1"}
    fill_execution_retention(conn, request, work_id, headers)
    clock[0] = "2026-01-01T00:01:00+00:00"
    if destination == "reclaimed":
        target = work_id
        assert act(conn, target, revision=1, generation=1, nonce=NONCE_B, key="reclaim")[0] == 200
        generation = "2"
    elif destination == "other-member":
        target = members(conn)["acme/two"]
        assert act(conn, target, nonce=NONCE_B, key="other-member")[0] == 200
        generation = "1"
    else:
        target = store.create_item(conn, {"title": "Independent work", "kind": "repo"}, actor_id="admin")["work_id"]
        generation = "0"
    # Legacy observations may follow a known binding; checking only the latest event loses it.
    if destination == "noncampaign":
        store._append_event(
            conn,
            work_id=work_id,
            event_type="execution-linked",
            actor_type="operator",
            actor_id="admin",
            node_id="node-a",
            run_id="retained-0",
            from_status=None,
            to_status=None,
            now=clock[0],
            detail={"external_key": "node-a/retained-0"},
        )
        conn.commit()
    headers = {"X-Worklore-Holder": NONCE_B, "X-Worklore-Generation": generation, "If-Match": "1"}
    if observer == "operator":
        monkeypatch.setenv("BRIGADE_WORKLORE_OPERATOR_NODES", "node-a,node-c")
    before = execution_state(conn)
    status, refusal = request(
        target,
        "execution",
        {"node_id": "node-a", "run_id": "retained-0"},
        headers=headers,
        node="node-a" if observer == "operator" else observer,
    )
    assert status == 409 and refusal["code"] == "link-conflict", refusal
    monkeypatch.setenv("BRIGADE_WORKLORE_OPERATOR_NODES", "node-c")
    status, refusal = request(target, "attempts", {"action": "failed", "run_id": "retained-0"}, headers=headers)
    assert status == 403 and refusal["code"] == "attempt-forbidden", refusal
    assert execution_state(conn) == before
    # Retention protection must not refuse a fresh run under the new grant.
    fleet_run(conn, run="fresh-grant-run")
    assert request(target, "execution", {"node_id": "node-a", "run_id": "fresh-grant-run"}, headers=headers)[0] == 201
    status, result = request(target, "attempts", {"action": "failed", "run_id": "fresh-grant-run"}, headers=headers)
    assert status == 200 and result["item"]["attempt_count"] == 1, result


def test_campaign_evicted_same_grant_replay_and_legacy_observations_are_inert(live_campaign):
    conn, _, request, _, _ = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    headers = {"X-Worklore-Holder": NONCE_A, "X-Worklore-Generation": "1", "If-Match": "1"}
    # This is the legacy unbound execution event shape, still emitted by trusted observations.
    fleet_run(conn, run="legacy-observed")
    assert request(work_id, "execution", {"node_id": "node-a", "run_id": "legacy-observed"}, node="admin")[0] == 201
    before = execution_state(conn)
    status, refusal = request(work_id, "attempts", {"action": "failed", "run_id": "legacy-observed"}, headers=headers)
    assert status == 409 and refusal["code"] == "stale-generation"
    assert execution_state(conn) == before
    fill_execution_retention(conn, request, work_id, headers)
    before = execution_state(conn)
    for run_id in ("retained-0", "legacy-observed"):
        for observer in ("node-a", "admin"):
            status, refusal = request(
                work_id, "execution", {"node_id": "node-a", "run_id": run_id}, node=observer, headers=headers
            )
            assert status == 409 and refusal["code"] == "link-conflict", refusal
            assert execution_state(conn) == before
    # A retained own-run reply remains inert even without the holder capability.
    retained = {"node_id": "node-a", "run_id": "retained-50"}
    assert request(work_id, "execution", retained)[0] == 201
    assert execution_state(conn) == before
    fleet_run(conn, run="unauthorized-fresh")
    status, refusal = request(work_id, "execution", {"node_id": "node-a", "run_id": "unauthorized-fresh"})
    assert status == 403 and refusal["code"] == "holder-mismatch"
    assert execution_state(conn) == before


def test_unbound_noncampaign_execution_can_still_relink_after_retention(live_campaign):
    conn, _, request, _, _ = live_campaign
    work_id = store.create_item(conn, {"title": "Independent work", "kind": "repo"}, actor_id="admin")["work_id"]
    fill_execution_retention(conn, request, work_id, {})
    assert request(work_id, "execution", {"node_id": "node-a", "run_id": "retained-0"})[0] == 201
    assert len(store.list_links(conn, work_id)) == 50


@pytest.mark.parametrize("patch", [{"description": "Changed member"}, {"acceptance": ["Different checks"]}])
def test_take_refuses_actual_member_drift_through_normal_patch(ledger, patch):
    conn, _, _ = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    status, result = call(conn, f"/work/items/{work_id}", patch, admin=True, method="PATCH", headers={"If-Match": "1"})
    assert status == 200, result
    before = store.list_events(conn, work_id)
    status, refusal = act(conn, work_id)
    assert status == 409 and refusal["code"] == "campaign-ineligible"
    assert store.list_events(conn, work_id) == before
    assert snapshot(conn, work_id)["state"] == "unowned"
    assert act(conn, members(conn)["acme/two"], key="unchanged")[0] == 200


def seed_ownership_history(conn, work_id, count):
    from brigade import worklore_ownership as ownership

    # Simulate a long valid departed history; latest projection is explicitly unowned.
    state = ownership._initial()
    for _ in range(count):
        store._append_event(
            conn,
            work_id=work_id,
            event_type="ownership-released",
            actor_type="node",
            actor_id="node-a",
            from_status=None,
            to_status=None,
            now=store._utc_now(),
            detail={"action": "release", "ownership": state},
        )
    conn.commit()


@pytest.mark.parametrize("count", [197, 198, 199])
def test_campaign_take_reserves_checkpoint_and_safety_exit_capacity(ledger, count):
    conn, _, _ = ledger
    create(conn)
    work_id = members(conn)["acme/one"]
    seed_ownership_history(conn, work_id, count)
    before = store.list_events(conn, work_id)
    status, result = act(conn, work_id)
    if count > 197:
        assert status == 409 and result["code"] == "ownership-capacity-exhausted"
        assert store.list_events(conn, work_id) == before
    else:
        assert status == 200, result
        assert checkpoint(conn, work_id)[0] == 200
        assert checkpoint(conn, work_id, revision=2, key="progress")[0] == 200
        assert act(conn, work_id, "release", revision=3, generation=1, key="release")[0] == 200
        assert snapshot(conn, work_id)["state"] == "unowned"


def recovery_request(request, work_id, *, node="admin", revision=2, generation=1, key="recover", **fields):
    return request(
        work_id,
        "ownership",
        {
            "action": "recover",
            "generation": generation,
            "holder_quiesced": True,
            "evidence_ref": "receipt-quiescence",
            **fields,
        },
        node=node,
        headers={"If-Match": str(revision), "Idempotency-Key": key},
    )


@pytest.mark.parametrize("operator", ["admin", "node-c"])
def test_campaign_recovery_is_audited_and_frees_canceled_absent_holder_scope(live_campaign, operator):
    conn, clock, request, _, _ = live_campaign
    made = create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    assert checkpoint(conn, work_id)[0] == 200
    fleet_hub.revoke_node(conn, "node-a")
    store.transition(conn, made["campaign"]["work_id"], to_status="canceled", expected_version=1, actor_id="admin")
    clock[0] = "2026-01-01T00:01:00+00:00"
    create(conn, key="camp-2")
    peer_id = next(m["work_id"] for m in campaigns.show_campaign(conn, "camp-2")["members"] if m["repo"] == "acme/one")
    assert act(conn, peer_id, node="node-b", nonce=NONCE_B, key="peer")[0] == 200
    assert checkpoint(conn, peer_id, node="node-b", nonce=NONCE_B, key="peer-scope")[1]["code"] == "path-scope-conflict"
    before = store.list_events(conn, work_id)
    assert recovery_request(request, work_id, node="node-b")[0] == 403
    assert store.list_events(conn, work_id) == before
    status, result = recovery_request(request, work_id, node=operator)
    assert status == 200, result
    assert result["ownership"]["state"] == "unowned" and result["ownership"]["generation"] == 2
    assert result["ownership"]["write_scope"] == []
    event = store.list_events(conn, work_id)[-1]
    assert event["actor_id"] == operator
    assert event["detail"]["recovery"] == {
        "holder_quiesced": True,
        "evidence_ref": "receipt-quiescence",
        "verification": "operator-reported-unverified",
    }
    after = store.list_events(conn, work_id)
    assert recovery_request(request, work_id, node=operator) == (200, result)
    assert store.list_events(conn, work_id) == after
    assert recovery_request(request, work_id, node=operator, evidence_ref="different")[0] == 409
    assert checkpoint(conn, peer_id, node="node-b", nonce=NONCE_B, key="peer-scope")[0] == 200
    # Keep the bearer enrolled while proving the old capability itself is fenced.
    fleet_hub.add_node(conn, "node-a", "Returned holder")
    assert checkpoint(conn, work_id, revision=3, key="stale")[1]["code"] == "stale-generation"
    assert act(conn, work_id, "release", revision=3, generation=1, key="stale-release")[1]["code"] == "stale-generation"


@pytest.mark.parametrize(
    "fields,revision,generation",
    [
        ({"holder_quiesced": False}, 2, 1),
        ({"holder_quiesced": 1}, 2, 1),
        ({"evidence_ref": ""}, 2, 1),
        ({"evidence_ref": "/private/evidence"}, 2, 1),
        ({"evidence_ref": None}, 2, 1),
        ({}, 1, 1),
        ({}, 2, 0),
    ],
)
def test_campaign_recovery_refuses_invalid_ack_evidence_and_stale_fences(live_campaign, fields, revision, generation):
    conn, _, request, _, _ = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    assert checkpoint(conn, work_id)[0] == 200
    before = store.list_events(conn, work_id)
    status, _ = recovery_request(request, work_id, revision=revision, generation=generation, **fields)
    assert status in {400, 409}
    assert store.list_events(conn, work_id) == before


def test_campaign_recovery_replay_cannot_clear_a_new_holder(live_campaign):
    conn, _, request, _, _ = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    assert recovery_request(request, work_id, revision=1)[0] == 200
    assert act(conn, work_id, node="node-b", revision=2, generation=2, nonce=NONCE_B, key="new-holder")[0] == 200
    held = snapshot(conn, work_id)
    before = store.list_events(conn, work_id)
    assert recovery_request(request, work_id, revision=1)[0] == 200
    assert snapshot(conn, work_id) == held
    assert store.list_events(conn, work_id) == before


def test_campaign_client_transports_execution_and_attempt_capability(live_campaign, monkeypatch):
    from brigade import worklore_client as client

    conn, _, _, server, tokens = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    fleet_run(conn)
    monkeypatch.setattr(
        client,
        "load_fleet_settings",
        lambda: {
            "hub_url": f"http://127.0.0.1:{server.server_address[1]}",  # content-guard: allow loopback-ipv4
            "node_token": tokens["node-a"],
        },
    )
    client.ownership_action(
        work_id, {"action": "take", "generation": 0}, if_match=0, idempotency_key="client-take", holder_nonce=NONCE_A
    )
    link = client.link_execution(work_id, {"node_id": "node-a", "run_id": "run-1"}, generation=1, holder_nonce=NONCE_A)
    assert link["link"]["external_key"] == "node-a/run-1"
    with pytest.raises(client.WorkloreClientError) as denial:
        client.record_attempt(
            work_id, {"action": "started", "run_id": "run-1"}, if_match=1, generation=1, holder_nonce=NONCE_B
        )
    assert denial.value.code == "holder-mismatch"
    assert (
        client.record_attempt(
            work_id, {"action": "failed", "run_id": "run-1"}, if_match=1, generation=1, holder_nonce=NONCE_A
        )["item"]["attempt_count"]
        == 1
    )


@pytest.mark.parametrize("node", ["admin", "node-c"])
def test_campaign_operator_observations_and_reset_use_trusted_identity(live_campaign, node):
    conn, _, request, _, _ = live_campaign
    made = create(conn, offer=None)
    work_id = members(conn)["acme/one"]
    fleet_run(conn, node="node-c")
    store.transition(conn, made["campaign"]["work_id"], to_status="canceled", expected_version=1, actor_id="admin")
    assert request(work_id, "execution", {"node_id": "node-c", "run_id": "run-1"}, node=node)[0] == 201
    status, result = request(
        work_id, "attempts", {"action": "failed", "run_id": "run-1"}, node=node, headers={"If-Match": "1"}
    )
    assert status == 200 and result["item"]["attempt_count"] == 1
    assert request(work_id, "attempts", {"action": "reset"}, node="node-a", headers={"If-Match": "2"})[0] == 403
    status, result = request(work_id, "attempts", {"action": "reset"}, node=node, headers={"If-Match": "2"})
    assert status == 200 and result["item"]["attempt_count"] == 0
    before = store.list_events(conn, work_id)
    assert request(work_id, "execution", {"node_id": "node-a", "run_id": "run-1", "is_operator": True})[0] == 400
    assert store.list_events(conn, work_id) == before
    with pytest.raises(store.WorkloreForbidden):
        store.record_attempt(conn, work_id, action="reset", expected_version=3, actor_id="admin", actor_type="operator")


@pytest.mark.parametrize("count,expected", [(200, 200), (201, 409)])
def test_campaign_recovery_obeys_safety_exit_capacity(live_campaign, count, expected):
    conn, _, request, _, _ = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    # Fill a held history with real checkpoints, preserving the actual holder projection.
    while (
        conn.execute(
            "SELECT COUNT(*) FROM work_events WHERE work_id=? AND event_type LIKE 'ownership-%'", (work_id,)
        ).fetchone()[0]
        < 200
    ):
        held = snapshot(conn, work_id)
        assert checkpoint(conn, work_id, revision=held["revision"], key=f"checkpoint-{held['revision']}")[0] == 200
    if count == 201:
        held = snapshot(conn, work_id)
        # A persisted exhausted held history remains bounded, including operator recovery.
        store._append_event(
            conn,
            work_id=work_id,
            event_type="ownership-checkpoint",
            actor_type="node",
            actor_id="node-a",
            from_status=None,
            to_status=None,
            now=store._utc_now(),
            detail={"action": "checkpoint", "ownership": held},
        )
        conn.commit()
    held = snapshot(conn, work_id)
    before = store.list_events(conn, work_id)
    status, result = recovery_request(request, work_id, revision=held["revision"])
    assert status == expected, result
    if expected == 409:
        assert result["code"] == "ownership-capacity-exhausted"
        assert store.list_events(conn, work_id) == before
    else:
        assert len(store.list_events(conn, work_id)) == len(before) + 1


def test_recovery_requires_complete_body_and_cannot_force_noncampaign_release(live_campaign):
    conn, _, request, _, _ = live_campaign
    create(conn)
    work_id = members(conn)["acme/one"]
    assert act(conn, work_id)[0] == 200
    before = store.list_events(conn, work_id)
    for missing in ("holder_quiesced", "evidence_ref"):
        body = {"action": "recover", "generation": 1, "holder_quiesced": True, "evidence_ref": "receipt-1"}
        del body[missing]
        assert (
            request(work_id, "ownership", body, node="admin", headers={"If-Match": "1", "Idempotency-Key": "missing"})[
                0
            ]
            == 400
        )
    assert store.list_events(conn, work_id) == before
    item = store.create_item(conn, {"title": "Legacy holder", "kind": "fleet"}, actor_id="admin")
    assert recovery_request(request, item["work_id"], revision=0, generation=0)[0] == 403
