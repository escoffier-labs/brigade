"""Patrol shares the CLI's fenced release proof without forcing ownership."""

from __future__ import annotations

import json

import pytest

from brigade import fleet_claim_release, fleet_client, runguard
from tests.test_run_reap import _repo, _write_run, _write_stale_lock

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
