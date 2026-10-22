"""Patrol shares the CLI's fenced release proof without forcing ownership."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from brigade import fleet_claim_release, fleet_client, runguard
from tests.test_run_reap import _repo, _write_run, _write_stale_lock
from tests.test_run_reap import NOW
from tests.test_runs_patrol_apply import _patrol

NODE = "11111111-1111-4111-8111-111111111111"
OTHER_NODE = "22222222-2222-4222-8111-222222222222"


@pytest.mark.parametrize(
    "condition", ["dead", "live", "foreign-node", "foreign-run", "foreign-lock", "no-generation", "reacquired"]
)
def test_patrol_hub_release_proof_and_generation_fence(tmp_path, monkeypatch, capsys, condition):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok")
    owner_run = _write_run(repo, "20260820-120000-patrol02", status="ok") if condition == "foreign-lock" else run
    _write_stale_lock(repo, owner_run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: condition == "live")
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    claim = {"owner_node": OTHER_NODE if condition == "foreign-node" else NODE, "acquired_at": "generation-1"}
    if condition == "no-generation":
        claim.pop("acquired_at")
    monkeypatch.setattr(
        fleet_client,
        "inspect_claim",
        lambda *args, **kwargs: fleet_client.ClaimDecision(
            granted=True,
            reason="ok",
            claim=claim,
            lock_run_dir=str(run.parent / "another-run") if condition == "foreign-run" else str(run),
        ),
    )
    released = []

    def release(target, **kwargs):
        released.append({"target": target, **kwargs})
        if condition == "reacquired":
            return fleet_client.ClaimDecision(
                granted=False, reason="held", owner={**claim, "acquired_at": "generation-2"}
            )
        return fleet_client.ClaimDecision(granted=True, reason="ok", claim=claim)

    monkeypatch.setattr(fleet_client, "release_claim", release)
    code = fleet_claim_release._release_claim(
        str(repo),
        as_path=True,
        node_override=None,
        force=False,
        as_json=True,
        expected_run_dir=run,
    )
    assert code == (0 if condition == "dead" else 1)
    if condition in {"dead", "reacquired"}:
        assert released == [{"target": "test-target", "node_id": NODE, "force": False, "acquired_at": "generation-1"}]
        assert json.loads(capsys.readouterr().out)["released"] == (condition == "dead")
    else:
        assert not released


def test_patrol_hub_refuses_changed_run_evidence(tmp_path, monkeypatch):
    from brigade import localio, runs_patrol_actions
    from brigade.run_lane_state import collect_lanes
    from tests.test_run_reap import NOW

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok")
    lane = collect_lanes(repo, now=NOW)[0]
    localio.write_json(run / "run.json", {**lane.run, "status": "dispatching", "started_at": NOW.isoformat()})
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    released = []

    def release(*args, **kwargs):
        released.append(kwargs)
        print(json.dumps({"released": True}))
        return 0

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    result = runs_patrol_actions._release_hub(lane, {"state": "complete"})
    assert result["status"] == "refused"
    assert not released


@pytest.mark.parametrize("reason", ["no-claim", "foreign-owner", "different-run-generation"])
@pytest.mark.parametrize("days", [0, 8])
def test_stable_hub_observation_fences_ticks_and_retention(tmp_path, monkeypatch, capsys, reason, days):
    from brigade import localio, runs_patrol_receipts

    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    owner = (
        None
        if reason == "no-claim"
        else {
            "owner_node": OTHER_NODE if reason == "foreign-owner" else NODE,
            "owner_conductor": "example",
            "acquired_at": "generation-1",
            "holder": "private-holder",
        }
    )
    probes, releases = [], []

    def inspect(*args, **kwargs):
        probes.append((args, kwargs))
        return fleet_client.ClaimDecision(
            granted=True, reason="ok", claim=owner, lock_run_dir=str(run.parent / "another-run")
        )

    monkeypatch.setattr(fleet_client, "inspect_claim", inspect)
    monkeypatch.setattr(fleet_client, "release_claim", lambda *args, **kwargs: releases.append(kwargs))
    first = _patrol(repo, capsys, apply="release-claim")
    action = first["actions"][0]
    assert action["scope"] == "hub" and action["status"] == "observed"
    assert action["result"]["reason"] == reason
    assert action["result"]["owner"] == (
        None if owner is None else {key: owner[key] for key in ("owner_node", "owner_conductor", "acquired_at")}
    )
    second = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=10, days=days))
    assert second["actions"] == []
    assert len(probes) == 1 and releases == []
    root = repo / ".brigade/patrol"
    if days:
        assert not (repo / first["receipt"]).exists()
        assert runs_patrol_receipts.read_fences(root)[0]["status"] == "observed"
    else:
        assert sum(len(json.loads(path.read_text())["actions"]) for path in root.glob("*/patrol.json")) == 1
    # Changed terminal evidence must authorize one new observation for the same lane.
    meta = json.loads((run / "run.json").read_text())
    localio.write_json(run / "run.json", {**meta, "status": "failed"})
    changed = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=20, days=days))
    assert changed["lanes"][0]["classification_digest"] != first["lanes"][0]["classification_digest"]
    assert changed["actions"][0]["idempotency_key"] != action["idempotency_key"]
    assert changed["actions"][0]["status"] == "observed"
    assert _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=30, days=days))["actions"] == []
    # A new run generation is a new lane and classification even with identical status.
    _write_run(repo, "20260820-120000-patrol02", status="failed")
    generation = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=40, days=days))
    assert len(generation["actions"]) == 1
    assert generation["actions"][0]["lane_id"] == "20260820-120000-patrol02"
    assert generation["actions"][0]["status"] == "observed"
    assert _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=50, days=days))["actions"] == []
    assert len(probes) == 3 and releases == []


@pytest.mark.parametrize(
    "reason", ["no-hub", "no-identity", "auth-failed", "hub-unavailable", "invalid", "quota", "other-error"]
)
def test_transient_hub_observation_retries_next_tick(tmp_path, monkeypatch, capsys, reason):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    probes, releases = [], []

    def inspect(*args, **kwargs):
        probes.append((args, kwargs))
        return fleet_client.ClaimDecision(granted=False, reason=reason)

    monkeypatch.setattr(fleet_client, "inspect_claim", inspect)
    monkeypatch.setattr(fleet_client, "release_claim", lambda *args, **kwargs: releases.append(kwargs))
    for minutes in (0, 10):
        report = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=minutes))
        assert report["actions"][0]["status"] == "refused"
    assert len(probes) == 2 and releases == []


@pytest.mark.parametrize("lock_run_dir", [None, ""])
def test_missing_hub_run_generation_stays_retryable(tmp_path, monkeypatch, capsys, lock_run_dir):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    probes = []

    def inspect(*args, **kwargs):
        probes.append(kwargs)
        return fleet_client.ClaimDecision(
            granted=True,
            reason="ok",
            claim={"owner_node": NODE, "acquired_at": "generation-1"},
            lock_run_dir=lock_run_dir,
        )

    monkeypatch.setattr(fleet_client, "inspect_claim", inspect)
    for minutes in (0, 10):
        report = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=minutes))
        assert report["actions"][0]["status"] == "refused"
    assert len(probes) == 2


@pytest.mark.parametrize("output", ["", "not-json", "[]", "null", '{"reason": []}', '{"reason": "quota"}'])
def test_unstructured_hub_release_refusal_stays_retryable(tmp_path, monkeypatch, capsys, output):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    calls = []

    def release(*args, **kwargs):
        calls.append(kwargs)
        print(output)
        return 1

    monkeypatch.setattr(fleet_claim_release, "_release_claim", release)
    for minutes in (0, 10):
        report = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=minutes))
        assert report["actions"][0]["status"] == "refused"
    assert len(calls) == 2


@pytest.mark.parametrize("shape", ["no-claim", "foreign-owner", "different-run-generation"])
@pytest.mark.parametrize("reason", ["missing", "held"])
def test_unconfirmed_hub_inspection_never_becomes_stable_fence(tmp_path, monkeypatch, capsys, reason, shape):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid"})
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    owner = (
        None
        if shape == "no-claim"
        else {
            "owner_node": OTHER_NODE if shape == "foreign-owner" else NODE,
            "owner_conductor": "example",
            "acquired_at": "generation-1",
        }
    )
    lock_run_dir = str(run.parent / "another-run") if shape == "different-run-generation" else None
    probes, releases = [], []

    def inspect(*args, **kwargs):
        probes.append((args, kwargs))
        return fleet_client.ClaimDecision(granted=False, reason=reason, claim=owner, lock_run_dir=lock_run_dir)

    monkeypatch.setattr(fleet_client, "inspect_claim", inspect)
    monkeypatch.setattr(fleet_client, "release_claim", lambda *args, **kwargs: releases.append(kwargs))
    for minutes in (0, 10):
        report = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=minutes))
        assert report["actions"][0]["status"] == "refused"
    assert len(probes) == 2 and releases == []


@pytest.mark.parametrize("malformed", [None, [], "x", True, 1], ids=["omitted", "list", "string", "bool", "number"])
def test_malformed_inspect_response_is_retried_not_fenced_as_no_claim(tmp_path, monkeypatch, capsys, malformed):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid", "token": "fake"})
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda fn, **kw: fn())
    actions, responses = [], []

    def endpoint(url, token, body, *, timeout):
        actions.append(body["action"])
        return 200, responses[0]

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", endpoint)
    responses.append({"inspected": True} if malformed is None else {"inspected": True, "claim": malformed})
    report = _patrol(repo, capsys, apply="release-claim", now=NOW)
    assert report["actions"][0]["status"] == "refused"
    assert actions == ["inspect"]
    responses[0] = {"inspected": True, "claim": None, "owned": False}
    report = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=10))
    assert actions == ["inspect", "inspect"]
    assert report["actions"][0]["status"] == "observed"
    assert report["actions"][0]["result"]["reason"] == "no-claim"


@pytest.mark.parametrize("owner_node", ["unknown", " ", "bad id", "a\x00b", "node\n", "x" * 129, "-lead"])
def test_invalid_inspect_owner_is_retried_without_foreign_owner_fence(tmp_path, monkeypatch, capsys, owner_node):
    from brigade import runs_patrol_receipts

    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid", "token": "fake"})
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda fn, **kw: fn())
    actions = []
    response = {"inspected": True, "claim": {"owner_node": owner_node}, "owned": False}

    def endpoint(url, token, body, *, timeout):
        actions.append(body["action"])
        return 200, response

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", endpoint)
    report = _patrol(repo, capsys, apply="release-claim", now=NOW)
    assert report["actions"][0]["status"] == "refused"
    assert actions == ["inspect"]
    assert not runs_patrol_receipts.read_fences(repo / ".brigade/patrol")
    response = {"inspected": True, "claim": None, "owned": False}
    report = _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=10))
    assert actions == ["inspect", "inspect"]
    assert report["actions"][0]["status"] == "observed"
    assert report["actions"][0]["result"]["reason"] == "no-claim"


def test_valid_legacy_inspect_owner_stably_fences_as_foreign_owner(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://hub.invalid", "token": "fake"})
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    monkeypatch.setattr(fleet_client, "_run_with_deadline", lambda fn, **kw: fn())
    actions = []

    def endpoint(url, token, body, *, timeout):
        actions.append(body["action"])
        return 200, {"inspected": True, "claim": {"owner_node": "other-node"}, "owned": False}

    monkeypatch.setattr(fleet_client, "_post_claim_blocking", endpoint)
    report = _patrol(repo, capsys, apply="release-claim", now=NOW)
    action = report["actions"][0]
    assert action["scope"] == "hub" and action["status"] == "observed"
    assert action["result"]["reason"] == "foreign-owner"
    assert action["result"]["owner"] == {"owner_node": "other-node"}
    assert json.loads((repo / report["receipt"]).read_text())["actions"][0]["status"] == "observed"
    assert _patrol(repo, capsys, apply="release-claim", now=NOW + timedelta(minutes=10))["actions"] == []
    assert actions == ["inspect"]


@pytest.mark.parametrize("lock", ["stale", "absent"])
def test_patrol_hub_release_holds_the_run_lock_through_the_hub_write(tmp_path, monkeypatch, capsys, lock):
    repo = _repo(tmp_path)
    run = _write_run(repo, "20260820-120000-patrol01", status="ok")
    if lock == "stale":
        _write_stale_lock(repo, run)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)
    monkeypatch.setattr(fleet_client, "find_workspace_for_path", lambda path: None)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda path: "test-target")
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda *args: NODE)
    claim = {"owner_node": NODE, "acquired_at": "generation-1"}
    monkeypatch.setattr(
        fleet_client,
        "inspect_claim",
        lambda *args, **kwargs: fleet_client.ClaimDecision(
            granted=True, reason="ok", claim=claim, lock_run_dir=str(run)
        ),
    )
    during = []

    def release(target, **kwargs):
        during.append((repo / ".brigade/run.lock").exists())
        return fleet_client.ClaimDecision(granted=True, reason="ok", claim=claim)

    monkeypatch.setattr(fleet_client, "release_claim", release)
    code = fleet_claim_release._release_claim(
        str(repo), as_path=True, node_override=None, force=False, as_json=True, expected_run_dir=run, hold_run_lock=True
    )
    assert code == 0
    assert during == [True]
    assert runguard.run_lock_state(repo, run) == "absent"
