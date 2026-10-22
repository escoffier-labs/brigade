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
