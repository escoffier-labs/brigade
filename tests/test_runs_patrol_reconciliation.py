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
def test_stranded_requires_old_unlanded_in_progress_task(tmp_path, monkeypatch, signal, age_hours, landed, expected):
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
        monkeypatch.setenv("GIT_COMMITTER_DATE", stamp)
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


def test_successful_run_without_finish_time_does_not_prove_stranding(tmp_path):
    repo = _repo(tmp_path)
    ledger = _ledger(repo, worktree=repo)
    payload = json.loads(ledger.read_text())
    payload["tasks"][0]["claim"]["claimed_at"] = (NOW - timedelta(hours=48)).isoformat()
    ledger.write_text(json.dumps(payload))
    _write_run(repo, "20260820-120000-patrol01", status="ok", extra={"claim_id": "claim-1"})
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    assert run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state != "stranded"


def test_brief_summary_keeps_claim_rows_beside_ten_run_rows(tmp_path):
    from brigade import runs_patrol

    repo = _repo(tmp_path)
    for index in range(12):
        _write_run(repo, f"20260820-1200{index:02d}-patrol{index:02d}", status="ok")
    ledger = _ledger(repo, worktree=tmp_path / "gone")
    payload = json.loads(ledger.read_text())
    payload["tasks"][0]["claim"]["claim_id"] = "unmatched-claim"
    ledger.write_text(json.dumps(payload))
    summary = runs_patrol.brief_summary(repo)
    assert sum(summary["counts"].values()) > 10
    assert summary["truncated"] is True


def test_dispatch_observation_does_not_read_the_hub():
    import inspect

    from brigade.work_cmd import dispatch_apply

    assert "include_hub=False" in inspect.getsource(dispatch_apply)


def test_fresh_ahead_commit_defers_stranding_of_an_old_claim(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    ledger = _ledger(repo, worktree=repo)
    payload = json.loads(ledger.read_text())
    payload["tasks"][0]["claim"]["claimed_at"] = (NOW - timedelta(hours=48)).isoformat()
    ledger.write_text(json.dumps(payload))
    _git(repo, "branch", "base")
    _git(repo, "branch", "--set-upstream-to=base")
    (repo / "tracked.txt").write_text("fresh\n")
    monkeypatch.setenv("GIT_COMMITTER_DATE", (NOW - timedelta(minutes=5)).isoformat())
    _git(repo, "commit", "-am", "fresh change")
    lane = run_lane_state.collect_lanes(repo, now=NOW)[0]
    assert run_lane_state.classify(lane, NOW, run_lane_state.Thresholds()).state != "stranded"


def test_brief_summary_caps_claims_at_ten_with_few_runs(tmp_path):
    from brigade import runs_patrol

    repo = _repo(tmp_path)
    tasks = [
        {
            "id": f"task-{i}",
            "status": "in_progress",
            "claim": {"claim_id": f"c{i}", "actor": "worker", "claimed_at": OLD_STARTED},
        }
        for i in range(15)
    ]
    path = repo / ".brigade/work/tasks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tasks": tasks}))
    summary = runs_patrol.brief_summary(repo)
    assert sum(summary["counts"].values()) == 10
    assert summary["truncated"] is True


def test_one_slow_hub_inspection_keeps_the_other_observations(tmp_path, monkeypatch, hub):
    import threading
    import time

    from brigade import runs_patrol_hub

    repo = _repo(tmp_path)
    hub.extend([{**_hub_claim(repo), "target": f"repo-{i}"} for i in range(4)])
    monkeypatch.setattr(fleet_client, "REPORT_TIMEOUT_SECONDS", 0.5)
    release = threading.Event()
    real = fleet_client.inspect_claim

    def inspect(target, **kwargs):
        if target == "repo-0":
            release.wait(5)
        return real(target, **kwargs)

    monkeypatch.setattr(fleet_client, "inspect_claim", inspect)
    started = time.monotonic()
    try:
        observations, _ = runs_patrol_hub._snapshot("local-node", 50)
    finally:
        release.set()
    assert time.monotonic() - started < 2
    errors = {claim["target"]: error for claim, _, error in observations}
    assert errors["repo-0"] == "Hub claim inspection timed out"
    assert [errors[f"repo-{i}"] for i in (1, 2, 3)] == [None, None, None]
