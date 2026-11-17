"""Explicit campaign and member completion through the real store and HTTP handler (#1128)."""

import base64
import copy
import json
import subprocess
from pathlib import Path

import pytest

from brigade import cli, fleet_hub, worklore_campaigns as campaigns, worklore_client
from brigade import worklore_http as http
from brigade import worklore_store as store
from brigade import worklore_store_operations as operations
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


def campaign_body(members=("acme/one", "acme/two")):
    return {
        "campaign_key": "camp-1",
        "title": "Rotate keys",
        "acceptance": ["Checks pass"],
        "members": [{"repo": repo} for repo in members],
        "offer": copy.deepcopy(OFFER),
    }


def create(conn, *, members=("acme/one", "acme/two")):
    status, payload = call(conn, "/work/campaigns", campaign_body(members), admin=True)
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
    # One clock read stamps every step and the completion record.
    assert len({event["occurred_at"] for event in events[-5:]}) == 1


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


def test_wrong_if_match_on_a_campaign_is_refused_before_the_completeness_check(ledger):
    created = create(ledger)
    campaign_id = created["campaign"]["work_id"]
    status, refusal = complete(ledger, campaign_id, 99, operator=True)
    assert status == 409 and refusal["code"] == "version-conflict"


def test_a_terminal_campaign_is_refused_before_the_completeness_check(ledger):
    created = create(ledger)
    campaign_id = created["campaign"]["work_id"]
    status, canceled = call(
        ledger,
        f"/work/items/{campaign_id}/transitions",
        {"to_status": "canceled"},
        operator=True,
        headers={"If-Match": "1"},
    )
    assert status == 200, canceled
    status, refusal = complete(ledger, campaign_id, canceled["item"]["version"], operator=True)
    assert status == 400 and refusal["code"] == "invalid-transition"


def test_member_added_before_the_write_is_refused_and_changes_nothing(ledger, monkeypatch):
    """A member committed after the request started but before the walk is still seen."""
    created = create(ledger)
    campaign_id = created["campaign"]["work_id"]
    for row in shown(ledger)["members"]:
        assert complete(ledger, row["work_id"], row["version"], operator=True)[0] == 200
    db_path = next(row[2] for row in ledger.execute("PRAGMA database_list") if row[1] == "main")
    other = fleet_hub.open_db(Path(db_path))
    real = store.complete_item
    before = {}

    def add_then_complete(*args, **kwargs):
        campaigns.create_campaign(
            other,
            campaign_body(("acme/one", "acme/two", "acme/three")),
            actor_id="admin",
        )
        before["item"] = store.get_item(ledger, campaign_id)
        before["events"] = store.list_events(ledger, campaign_id)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "complete_item", add_then_complete)
    try:
        status, refusal = complete(ledger, campaign_id, created["campaign"]["version"], operator=True)
    finally:
        other.close()
    assert status == 409 and refusal["code"] == "campaign-incomplete"
    assert store.get_item(ledger, campaign_id) == before["item"]
    assert store.list_events(ledger, campaign_id) == before["events"]
    assert "acme/three" in {row["repo"] for row in shown(ledger)["members"]}


def test_refusal_on_a_later_step_rolls_the_earlier_steps_back(ledger, monkeypatch):
    create(ledger)
    work_id = member(ledger)["work_id"]
    real = operations._transition_locked
    steps = []

    def refuse_the_third_step(*args, **kwargs):
        steps.append(kwargs["to_status"])
        if len(steps) == 3:
            raise store.WorkloreConflict("refused midway", code="version-conflict")
        return real(*args, **kwargs)

    monkeypatch.setattr(operations, "_transition_locked", refuse_the_third_step)
    before = store.get_item(ledger, work_id)
    events = store.list_events(ledger, work_id)
    status, refusal = complete(ledger, work_id, before["version"], operator=True)
    assert status == 409 and refusal["code"] == "version-conflict"
    assert steps == ["ready", "claimed", "running"]
    assert store.get_item(ledger, work_id) == before
    assert store.list_events(ledger, work_id) == events


def complete_campaign(conn):
    """Create a campaign, complete every member, then complete the campaign item."""
    created = create(conn)
    campaign_id = created["campaign"]["work_id"]
    for row in shown(conn)["members"]:
        assert complete(conn, row["work_id"], row["version"], operator=True)[0] == 200
    status, payload = complete(conn, campaign_id, created["campaign"]["version"], operator=True)
    assert status == 200, payload
    return campaign_id


@pytest.mark.parametrize(
    "members",
    [
        pytest.param(("acme/one", "acme/two"), id="no-new-members"),
        pytest.param(("acme/one", "acme/two", "acme/three"), id="new-member"),
    ],
)
def test_recreating_a_completed_campaign_is_refused_and_writes_nothing(ledger, members):
    campaign_id = complete_campaign(ledger)
    before = store.get_item(ledger, campaign_id)
    events = store.list_events(ledger, campaign_id)
    stored_members = [row["repo"] for row in shown(ledger)["members"]]
    status, refusal = call(ledger, "/work/campaigns", campaign_body(members), admin=True)
    assert status == 409 and refusal["code"] == "campaign-closed"
    assert store.get_item(ledger, campaign_id) == before
    assert store.list_events(ledger, campaign_id) == events
    assert [row["repo"] for row in shown(ledger)["members"]] == stored_members


@pytest.mark.parametrize(
    "members",
    [
        pytest.param(("acme/one", "acme/two"), id="no-new-members"),
        pytest.param(("acme/one", "acme/three"), id="new-member"),
    ],
)
def test_recreating_a_canceled_campaign_is_refused_and_writes_nothing(ledger, members):
    created = create(ledger)
    campaign_id = created["campaign"]["work_id"]
    status, canceled = call(
        ledger,
        f"/work/items/{campaign_id}/transitions",
        {"to_status": "canceled"},
        operator=True,
        headers={"If-Match": "1"},
    )
    assert status == 200, canceled
    before = store.get_item(ledger, campaign_id)
    events = store.list_events(ledger, campaign_id)
    stored_members = [row["repo"] for row in shown(ledger)["members"]]
    status, refusal = call(ledger, "/work/campaigns", campaign_body(members), admin=True)
    assert status == 409 and refusal["code"] == "campaign-closed"
    assert store.get_item(ledger, campaign_id) == before
    assert store.list_events(ledger, campaign_id) == events
    assert [row["repo"] for row in shown(ledger)["members"]] == stored_members


def test_recreating_an_open_campaign_still_adds_only_the_missing_members(ledger):
    create(ledger)
    status, result = call(ledger, "/work/campaigns", campaign_body(("acme/one", "acme/two", "acme/three")), admin=True)
    assert status == 200, result
    assert result["created"] is False and result["added"] == ["acme/three"]
    assert result["existing"] == ["acme/one", "acme/two"]


@pytest.mark.parametrize("value", ["", 0, False, {}])
def test_malformed_evidence_refs_are_refused_and_change_nothing(ledger, value):
    create(ledger)
    work_id = member(ledger)["work_id"]
    before = store.get_item(ledger, work_id)
    events = store.list_events(ledger, work_id)
    status, refusal = call(
        ledger,
        f"/work/items/{work_id}/completions",
        {"evidence_refs": value},
        operator=True,
        headers={"If-Match": str(before["version"])},
    )
    assert status == 400 and refusal["code"] == "field-bound"
    assert store.get_item(ledger, work_id) == before
    assert store.list_events(ledger, work_id) == events


def test_null_or_absent_evidence_refs_means_no_evidence(ledger):
    create(ledger)
    for repo, body in (("acme/one", {"evidence_refs": None}), ("acme/two", {})):
        row = member(ledger, repo)
        status, result = call(
            ledger,
            f"/work/items/{row['work_id']}/completions",
            body,
            operator=True,
            headers={"If-Match": str(row["version"])},
        )
        assert status == 200, result
        recorded = store.list_events(ledger, row["work_id"])[-1]
        assert recorded["event_type"] == "completion-recorded"
        assert recorded["detail"]["evidence_refs"] == []


@pytest.fixture
def campaign_workspace(tmp_path):
    for name in ("one", "two"):
        path = tmp_path / name
        path.mkdir()
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin", f"https://github.com/acme/{name}.git"],
            check=True,
            capture_output=True,
        )
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade/repos.toml").write_text(
        '[[repo]]\nid="repo-one"\npath="one"\n[[repo]]\nid="repo-two"\npath="two"\n'
    )
    return tmp_path


def test_dry_run_reports_a_closed_campaign_without_writing(ledger, campaign_workspace, monkeypatch, capsys):
    complete_campaign(ledger)
    before = tuple(ledger.iterdump())
    posted = []
    monkeypatch.setattr(
        worklore_client,
        "get_campaign",
        lambda key: call(ledger, f"/work/campaigns/{key}", method="GET")[1],
    )
    monkeypatch.setattr(worklore_client, "create_campaign", lambda body: posted.append(body))
    monkeypatch.setattr(worklore_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.invalid"})
    assert (
        cli.main(
            [
                "fleet",
                "campaign",
                "create",
                "--target",
                str(campaign_workspace),
                "--campaign-id",
                "camp-1",
                "--title",
                "Rotate keys",
                "--acceptance",
                "Checks pass",
                "--dry-run",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "create would be refused (campaign-closed)" in out and "campaign is completed" in out
    assert posted == [] and tuple(ledger.iterdump()) == before


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
