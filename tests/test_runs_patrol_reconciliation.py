"""Patrol reconciliation of unlanded work and fleet claim observations."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from brigade import fleet_client, run_lane_state, work_cmd
from brigade.runs_patrol import patrol
from tests.test_run_reap import NOW, OLD_STARTED, _git, _repo, _write_run
from tests.test_runs_patrol import _ledger, _snapshot


@pytest.mark.parametrize("signal", ["success", "ahead"])
@pytest.mark.parametrize("age_hours,landed,expected", [(25, False, "stranded"), (23, False, None), (25, True, None)])
def test_stranded_requires_old_unlanded_in_progress_task(tmp_path, signal, age_hours, landed, expected):
    repo = _repo(tmp_path)
    ledger = _ledger(repo, worktree=repo)
    payload = json.loads(ledger.read_text())
    stamp = (NOW - timedelta(hours=age_hours)).isoformat()
    payload["tasks"][0]["claim"]["claimed_at"] = stamp
    if landed:
        payload["tasks"][0]["landing"] = {"commit": "abc123"}
    ledger.write_text(json.dumps(payload))
    if signal == "success":
        _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1", "finished_at": stamp})
    else:
        _git(repo, "branch", "base")
        _git(repo, "branch", "--set-upstream-to=base")
        (repo / "tracked.txt").write_text("finished change\n")
        _git(repo, "commit", "-am", "finished change")
    before = _snapshot(repo)
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    result = run_lane_state.classify(lane, NOW, run_lane_state.Thresholds())
    assert result.state == (expected or ("complete" if signal == "success" else "unknown"))
    assert _snapshot(repo) == before


def test_recent_worktree_change_defers_stranding(tmp_path):
    repo = _repo(tmp_path)
    _ledger(repo, worktree=repo)
    (repo / "tracked.txt").write_text("still changing\n")
    os.utime(repo / "tracked.txt", (NOW.timestamp() - 60, NOW.timestamp() - 60))
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    assert run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state == "unknown"


def test_failed_run_outcome_precedes_old_dirty_claim(tmp_path):
    repo = _repo(tmp_path)
    _ledger(repo, worktree=repo)
    _write_run(
        repo, "20260820-120000-patrol01", status="failed", extra={"claim_id": "claim-1", "finished_at": OLD_STARTED}
    )
    changed = repo / "tracked.txt"
    changed.write_text("abandoned failure\n")
    os.utime(changed, (NOW.timestamp() - 172800, NOW.timestamp() - 172800))
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    assert run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state == "failed"


@pytest.fixture
def hub(monkeypatch):
    rows = []
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.invalid", "node_token": "fake"}
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "local-node")
    monkeypatch.setattr(
        fleet_client,
        "fetch_claims",
        lambda: [{key: value for key, value in row.items() if key != "run_dir"} for row in rows],
    )

    def inspect(target, **kwargs):
        row = next(row for row in rows if row["target"] == target and row["owner_node"] == "local-node")
        return fleet_client.ClaimDecision(granted=True, reason="ok", claim=row, lock_run_dir=row.get("run_dir"))

    monkeypatch.setattr(fleet_client, "inspect_claim", inspect)
    return rows


def _hub_claim(repo, *, owner="local-node", run="20260820-120000-patrol01"):
    return {
        "target": f"example-repo-{owner}",
        "owner_node": owner,
        "run_dir": str(repo / ".brigade/runs" / run),
        "claimed_at": OLD_STARTED,
        "holder": "private-fence-value",
    }


def test_hub_joins_only_this_nodes_exact_run_path(tmp_path, capsys, hub):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"finished_at": OLD_STARTED})
    hub.extend([_hub_claim(repo), _hub_claim(repo, owner="other-node")])
    before = _snapshot(repo)
    assert patrol(target=repo, now=NOW, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["lanes"]) == 2
    local, remote = payload["lanes"]
    assert local["state"] == "complete"
    assert local["hub_claim"]["owner_node"] == "local-node"
    assert remote["state"] == "unknown"
    assert remote["display_only"] is True
    assert any(item["source"] == "fleet:claims" for item in remote["evidence"])
    assert "private-fence-value" not in json.dumps(payload)
    assert _snapshot(repo) == before


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("missing", "claim-without-run"),
        ("no-path", "unknown"),
        ("outside", "unknown"),
        ("corrupt", "unknown"),
        ("foreign", "unknown"),
        ("malformed-path", "unknown"),
    ],
)
def test_unmatched_hub_claim_is_conservative(tmp_path, hub, kind, expected):
    repo = _repo(tmp_path)
    row = _hub_claim(repo)
    if kind == "no-path":
        row.pop("run_dir")
    elif kind == "outside":
        row["run_dir"] = str(tmp_path / "other/.brigade/runs/20260820-120000-patrol01")
    elif kind == "corrupt":
        run = _write_run(repo, "20260820-120000-patrol01")
        (run / "run.json").write_text("corrupt")
    elif kind == "foreign":
        row["owner_node"] = "other-node"
    elif kind == "malformed-path":
        row["run_dir"] = str(repo / ".brigade/runs/not a run")
    hub.append(row)
    lanes = run_lane_state.collect_lanes(repo, now=NOW)
    lane = next(lane for lane in lanes if lane.hub_claim)
    assert run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state == expected


def test_hub_failure_is_reported_without_discarding_local_result(tmp_path, monkeypatch, hub):
    repo = _repo(tmp_path)
    _write_run(repo, "20260820-120000-patrol01", status="ok")

    def fail():
        raise fleet_client.FleetClientError("unreachable")

    monkeypatch.setattr(fleet_client, "fetch_claims", fail)
    lanes = run_lane_state.collect_lanes(repo, now=NOW)
    assert {run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state for lane in lanes} == {
        "complete",
        "unknown",
    }


def test_hub_row_limit_is_explicit(tmp_path, hub):
    repo = _repo(tmp_path)
    hub.extend(_hub_claim(repo, owner=f"other-{index}") for index in range(4))
    lanes = run_lane_state.collect_lanes(repo, now=NOW, limit=2)
    assert len(lanes) == 2
    assert lanes.truncated


def test_brief_has_bounded_local_patrol_summary(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    for index in range(12):
        _write_run(repo, f"20260820-120000-patrol{index:02d}", status="ok")
    calls = []
    collect = run_lane_state._collect_run

    def observe(*args, **kwargs):
        calls.append(args[0])
        return collect(*args, **kwargs)

    monkeypatch.setattr(run_lane_state, "_collect_run", observe)
    monkeypatch.setattr(fleet_client, "fetch_claims", lambda: pytest.fail("brief must not fetch Hub claims"))
    assert work_cmd.brief(target=repo) == 0
    assert "patrol: complete=10 (truncated)" in capsys.readouterr().out
    assert len(calls) == 10
