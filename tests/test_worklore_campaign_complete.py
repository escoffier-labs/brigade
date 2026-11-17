"""Explicit campaign and member completion through the real store and HTTP handler (#1128)."""

import base64
import copy
import json

import pytest

from brigade import cli, fleet_hub, worklore_campaigns as campaigns, worklore_client
from brigade import worklore_http as http
from brigade import worklore_store as store
from brigade.worklore_validate import completion_path

NONCE = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
OFFER = {
    "allowed_nodes": ["node-a", "node-b"],
    "per_node_concurrency": 1,
    "attempt_budget": {"cap": 2, "source_ref": "budget-1"},
    "authorization_ref": "grant-1",
    "exclusions": [],
    "lease_seconds": 60,
}


def call(conn, path, body=None, *, node="node-a", admin=False, operator=False, headers=None, method="POST"):
    return http.handle(
        conn,
        http.Request(
            method=method,
            path=path,
            node_id=None if admin else node,
            is_admin=admin,
            is_operator=operator,
            operator_authorization_resolved=True,
            body=body or {},
            headers=headers or {},
        ),
    )


def create(conn, *, members=("acme/one", "acme/two")):
    body = {
        "campaign_key": "camp-1",
        "title": "Rotate keys",
        "acceptance": ["Checks pass"],
        "members": [{"repo": repo} for repo in members],
        "offer": copy.deepcopy(OFFER),
    }
    status, payload = call(conn, "/work/campaigns", body, admin=True)
    assert status in {200, 201}, payload
    return payload


def shown(conn):
    return campaigns.show_campaign(conn, "camp-1")


def member(conn, repo="acme/one"):
    return next(m for m in shown(conn)["members"] if m["repo"] == repo)


def complete(conn, work_id, version, *, evidence=(), **kwargs):
    return call(
        conn,
        f"/work/items/{work_id}/completions",
        {"evidence_refs": list(evidence)} if evidence else {},
        headers={"If-Match": str(version)},
        **kwargs,
    )


def take(conn, work_id, *, revision=0, generation=0, key="take-1"):
    return call(
        conn,
        f"/work/items/{work_id}/ownership",
        {"action": "take", "generation": generation},
        headers={"If-Match": str(revision), "Idempotency-Key": key, "X-Worklore-Holder": NONCE},
    )


def checkpoint(conn, work_id, *, repo="acme/one", revision=1, generation=1):
    return call(
        conn,
        f"/work/items/{work_id}/ownership",
        {
            "action": "checkpoint",
            "generation": generation,
            "repo_identity": repo,
            "write_scope": [],
            "source_revision": "a" * 40,
            "next_action": {"kind": "await-review", "resume_condition": "review the run"},
            "evidence_refs": [],
        },
        headers={"If-Match": str(revision), "Idempotency-Key": "checkpoint-1", "X-Worklore-Holder": NONCE},
    )


def ownership(conn, work_id):
    return call(conn, f"/work/items/{work_id}/ownership", method="GET")[1]["ownership"]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_utc_now", lambda: "2026-01-01T00:00:00+00:00")
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    for node in ("node-a", "node-b"):
        fleet_hub.add_node(conn, node, node)
    yield conn
    conn.close()


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("captured", ["ready", "claimed", "running", "verifying", "completed"]),
        ("defining", ["ready", "claimed", "running", "verifying", "completed"]),
        ("ready", ["claimed", "running", "verifying", "completed"]),
        ("claimed", ["running", "verifying", "completed"]),
        ("running", ["verifying", "completed"]),
        ("verifying", ["completed"]),
        ("deferred", ["ready", "claimed", "running", "verifying", "completed"]),
        ("blocked", ["ready", "claimed", "running", "verifying", "completed"]),
    ],
)
def test_completion_path_for_every_non_terminal_status(current, expected):
    assert completion_path(current) == expected


@pytest.mark.parametrize("current", ["completed", "canceled", "archived"])
def test_completion_path_is_none_for_a_terminal_item(current):
    assert completion_path(current) is None


def test_ready_member_completes_and_records_the_path_and_evidence(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    status, ready = call(
        ledger,
        f"/work/items/{work_id}/transitions",
        {"to_status": "ready"},
        operator=True,
        headers={"If-Match": "1"},
    )
    assert status == 200 and ready["item"]["version"] == 2
    status, result = complete(ledger, work_id, 2, evidence=("receipt-1", "github-pr-2"), operator=True)
    assert status == 200, result
    item = result["item"]
    assert item["status"] == "completed" and item["version"] == 6
    events = store.list_events(ledger, work_id)
    transitions = [e for e in events if e["event_type"] == "transitioned"]
    assert [e["to_status"] for e in transitions[-4:]] == ["claimed", "running", "verifying", "completed"]
    recorded = events[-1]
    assert recorded["event_type"] == "completion-recorded"
    assert recorded["actor_type"] == "operator" and recorded["actor_id"] == "node-a"
    assert recorded["detail"] == {
        "path": ["claimed", "running", "verifying", "completed"],
        "evidence_refs": ["receipt-1", "github-pr-2"],
    }


def test_parked_member_completes_and_lands_in_the_terminal_bucket(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    assert take(ledger, work_id)[0] == 200
    assert checkpoint(ledger, work_id)[0] == 200
    assert member(ledger)["bucket"] == "in_flight"
    before = ownership(ledger, work_id)
    status, result = complete(ledger, work_id, 1, operator=True)
    assert status == 200, result
    assert result["item"]["status"] == "completed"
    parked = member(ledger)
    assert parked["bucket"] == "completed" and parked["status"] == "completed"
    assert shown(ledger)["campaign"]["summary"]["completed"] == 1
    assert ownership(ledger, work_id) == before


def test_already_terminal_member_is_refused_and_unchanged(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    assert complete(ledger, work_id, 1, operator=True)[0] == 200
    before = store.get_item(ledger, work_id)
    events = store.list_events(ledger, work_id)
    status, refusal = complete(ledger, work_id, before["version"], operator=True)
    assert status == 400 and refusal["code"] == "invalid-transition"
    assert store.get_item(ledger, work_id) == before
    assert store.list_events(ledger, work_id) == events


def test_wrong_if_match_is_refused_and_unchanged(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    before = store.get_item(ledger, work_id)
    status, refusal = complete(ledger, work_id, 99, operator=True)
    assert status == 409 and refusal["code"] == "version-conflict"
    assert store.get_item(ledger, work_id) == before


def test_refused_step_rolls_the_whole_completion_back(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    store.patch_item(ledger, work_id, {"acceptance": []}, expected_version=1, actor_id="admin")
    before = store.get_item(ledger, work_id)
    events = store.list_events(ledger, work_id)
    status, refusal = complete(ledger, work_id, before["version"], operator=True)
    assert status == 400 and refusal["code"] == "acceptance-required"
    assert store.get_item(ledger, work_id) == before
    assert store.list_events(ledger, work_id) == events


def test_non_operator_node_is_refused_like_the_transition_route(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    completion = complete(ledger, work_id, 1, operator=False)
    transition = call(
        ledger,
        f"/work/items/{work_id}/transitions",
        {"to_status": "ready"},
        operator=False,
        headers={"If-Match": "1"},
    )
    assert completion[0] == transition[0] == 403
    assert completion[1]["code"] == transition[1]["code"] == "forbidden"
    assert store.get_item(ledger, work_id)["status"] == "captured"


def test_holder_is_refused_like_the_transition_route(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    assert take(ledger, work_id)[0] == 200
    completion = call(
        ledger,
        f"/work/items/{work_id}/completions",
        {},
        operator=False,
        headers={"If-Match": "1", "X-Worklore-Holder": NONCE},
    )
    transition = call(
        ledger,
        f"/work/items/{work_id}/transitions",
        {"to_status": "ready"},
        operator=False,
        headers={"If-Match": "1", "X-Worklore-Holder": NONCE},
    )
    assert completion[0] == transition[0] == 403
    assert completion[1]["code"] == transition[1]["code"] == "forbidden"
    assert store.get_item(ledger, work_id)["status"] == "captured"


def test_admin_without_operator_flag_matches_the_transition_route(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    status, result = complete(ledger, work_id, 1, admin=True, operator=False)
    assert status == 200 and result["item"]["status"] == "completed"
    peer = member(ledger, "acme/two")["work_id"]
    transition = call(
        ledger,
        f"/work/items/{peer}/transitions",
        {"to_status": "ready"},
        admin=True,
        operator=False,
        headers={"If-Match": "1"},
    )
    assert transition[0] == 200


def test_campaign_is_incomplete_until_every_member_is_terminal(ledger):
    created = create(ledger)
    campaign_id = created["campaign"]["work_id"]
    status, refusal = complete(ledger, campaign_id, 1, operator=True)
    assert status == 409 and refusal["code"] == "campaign-incomplete"
    assert store.get_item(ledger, campaign_id)["status"] == "captured"
    assert [m["status"] for m in shown(ledger)["members"]] == ["captured", "captured"]
    for row in shown(ledger)["members"]:
        assert complete(ledger, row["work_id"], row["version"], operator=True)[0] == 200
    status, result = complete(ledger, campaign_id, 1, operator=True)
    assert status == 200 and result["item"]["status"] == "completed"
    assert shown(ledger)["campaign"]["summary"]["done"] is True


def test_completing_one_member_leaves_ownership_unchanged(ledger):
    create(ledger)
    work_id = member(ledger)["work_id"]
    assert take(ledger, work_id)[0] == 200
    assert checkpoint(ledger, work_id, repo="acme/one")[0] == 200
    before = ownership(ledger, work_id)
    assert complete(ledger, work_id, 1, operator=True)[0] == 200
    assert ownership(ledger, work_id) == before


@pytest.fixture
def cli_hub(ledger, monkeypatch):
    def request(method, path, body=None, headers=None):
        status, payload = call(ledger, path, body, admin=True, method=method, headers=headers or {})
        if status >= 400:
            raise worklore_client.WorkloreClientError(f"fleet hub work failed: HTTP {status}", code=payload["code"])
        return payload

    monkeypatch.setattr(worklore_client, "get_campaign", lambda key: request("GET", f"/work/campaigns/{key}"))
    monkeypatch.setattr(
        worklore_client,
        "complete_item",
        lambda work_id, *, evidence_refs=(), if_match=None: request(
            "POST",
            f"/work/items/{work_id}/completions",
            {"evidence_refs": list(evidence_refs)},
            {"If-Match": str(if_match)},
        ),
    )
    return ledger


def test_cli_completes_a_member_in_text(cli_hub, capsys):
    create(cli_hub)
    assert cli.main(["fleet", "campaign", "complete", "camp-1", "--repo", "acme/one", "--evidence", "receipt-1"]) == 0
    out = capsys.readouterr().out
    assert "completed member acme/one" in out and "status completed, version 6" in out
    assert member(cli_hub, "acme/one")["status"] == "completed"


def test_cli_completes_a_member_in_json(cli_hub, capsys):
    create(cli_hub)
    assert cli.main(["fleet", "campaign", "complete", "camp-1", "--repo", "acme/one", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["item"]["status"] == "completed" and payload["item"]["version"] == 6


def test_cli_prints_the_hub_code_and_exits_nonzero_on_refusal(cli_hub, capsys):
    create(cli_hub)
    assert cli.main(["fleet", "campaign", "complete", "camp-1"]) == 1
    err = capsys.readouterr().err
    assert "campaign-incomplete" in err
