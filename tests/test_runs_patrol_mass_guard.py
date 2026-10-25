"""Patrol refuses mass repairs that look like a host or network fault."""

from __future__ import annotations

import json

import pytest

from brigade import cli, runs_patrol, runs_patrol_actions
from brigade.run_lane_state import Classification
from tests.test_run_reap import NOW, _repo, _write_run


def _rows(dead: int, stalled: int = 0, healthy: int = 0) -> list[dict]:
    states = ["dead"] * dead + ["stalled"] * stalled + ["active"] * healthy
    return [{"lane_id": f"lane-{index}", "state": state} for index, state in enumerate(states)]


@pytest.mark.parametrize(
    "dead,stalled,healthy",
    [
        (4, 0, 16),  # below both thresholds
        (3, 2, 15),  # at the count threshold, fraction 0.25
        (2, 3, 5),  # at the fraction threshold, count 5
        (2, 0, 0),  # all lanes dead, but too few for the fraction rule
    ],
)
def test_guard_allows_repairs_below_or_at_threshold(dead, stalled, healthy):
    assert runs_patrol_actions.environment_fault(_rows(dead, stalled, healthy)) is None


@pytest.mark.parametrize(
    "dead,stalled,healthy,reason",
    [
        (4, 2, 30, "count"),  # 6 affected lanes is over the count of 5
        (2, 1, 2, "fraction"),  # 3 of 5 lanes is over half
    ],
)
def test_guard_reports_counts_over_threshold(dead, stalled, healthy, reason):
    finding = runs_patrol_actions.environment_fault(_rows(dead, stalled, healthy))
    assert finding is not None
    assert finding["kind"] == "suspected_environment_fault"
    assert (finding["dead"], finding["stalled"], finding["affected"]) == (dead, stalled, dead + stalled)
    assert finding["lanes"] == dead + stalled + healthy
    assert finding["max_count"] == runs_patrol_actions.MASS_FAULT_MAX_COUNT
    assert finding["max_fraction"] == runs_patrol_actions.MASS_FAULT_MAX_FRACTION
    assert reason in finding["exceeded"]


def test_guard_thresholds_are_configurable():
    rows = _rows(3, 0, 17)
    assert runs_patrol_actions.environment_fault(rows) is None
    assert runs_patrol_actions.environment_fault(rows, max_count=2) is not None
    assert runs_patrol_actions.environment_fault(_rows(3, 0, 3), max_fraction=0.4) is not None


def _dead_lanes(tmp_path, monkeypatch, *, dead: int, healthy: int = 0):
    """Create runs and pin their classification so only the guard decides."""
    repo = _repo(tmp_path)
    states = {}
    for index in range(dead + healthy):
        run_id = f"20260820-120000-lane{index:04d}"
        _write_run(repo, run_id)
        states[run_id] = "dead" if index < dead else "active"

    def classify(lane, *args, **kwargs):
        name = lane.run_dir.name if lane.run_dir else ""
        return Classification(states.get(name, "unknown"), "pinned for test", ())

    reaped = []

    def reap(lane, row, thresholds, now):
        reaped.append(lane.run_dir.name)
        return {"status": "refused", "reason": "test double"}

    monkeypatch.setattr(runs_patrol, "classify", classify)
    monkeypatch.setattr(runs_patrol_actions, "_reap", reap)
    return repo, reaped


def _run(repo, capsys, **kwargs):
    code = runs_patrol.patrol(target=repo, now=NOW, json_output=True, **kwargs)
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out else None, captured.err


def test_apply_below_threshold_reaps_without_a_finding(tmp_path, monkeypatch, capsys):
    repo, reaped = _dead_lanes(tmp_path, monkeypatch, dead=4, healthy=16)
    code, report, err = _run(repo, capsys, apply="reap")
    assert code == 0, err
    assert len(reaped) == 4
    assert "findings" not in report


def test_apply_at_threshold_still_reaps(tmp_path, monkeypatch, capsys):
    repo, reaped = _dead_lanes(tmp_path, monkeypatch, dead=5, healthy=15)
    code, report, err = _run(repo, capsys, apply="reap,release-claim")
    assert code == 0, err
    assert len(reaped) == 5
    assert "findings" not in report


def test_apply_over_threshold_skips_destructive_actions_and_fails(tmp_path, monkeypatch, capsys):
    repo, reaped = _dead_lanes(tmp_path, monkeypatch, dead=6, healthy=14)
    code, report, err = _run(repo, capsys, apply="reap,release-claim,escalate")
    assert code == 1, err
    assert reaped == []
    (finding,) = report["findings"]
    assert finding["kind"] == "suspected_environment_fault"
    assert (finding["dead"], finding["stalled"], finding["lanes"]) == (6, 0, 20)
    assert finding["skipped_actions"] == ["reap", "release-claim"]
    # Escalation is not destructive and still reports the dead lanes.
    assert {item["action"] for item in report["actions"]} == {"escalate"}
    assert report["exit_code"] == 1
    assert "mass_repair_override" not in report
    receipt = json.loads((repo / report["receipt"]).read_text())
    assert receipt["findings"] == report["findings"] and receipt["exit_code"] == 1


@pytest.mark.parametrize(
    "dead,healthy,expected",
    [(0, 4, 0), (2, 0, 0), (3, 3, 0), (3, 2, 1), (5, 15, 0), (6, 14, 1)],
)
def test_escalation_only_pass_reports_mass_fault_status(tmp_path, monkeypatch, capsys, dead, healthy, expected):
    repo, reaped = _dead_lanes(tmp_path, monkeypatch, dead=dead, healthy=healthy)
    code, report, err = _run(repo, capsys, apply="escalate")
    assert code == expected, err
    assert report["exit_code"] == expected and reaped == []
    assert {item["action"] for item in report["actions"]} == ({"escalate"} if dead else set())
    if expected:
        (finding,) = report["findings"]
        assert finding["kind"] == "suspected_environment_fault"
        assert "skipped_actions" not in finding and "overridden" not in finding
    else:
        assert "findings" not in report
    receipt = json.loads((repo / report["receipt"]).read_text())
    assert receipt["exit_code"] == expected


def test_forced_override_reaps_and_is_recorded_in_the_receipt(tmp_path, monkeypatch, capsys):
    repo, reaped = _dead_lanes(tmp_path, monkeypatch, dead=6, healthy=14)
    code, report, err = _run(repo, capsys, apply="reap", force_mass_repair=True)
    assert code == 0, err
    assert len(reaped) == 6
    (finding,) = report["findings"]
    assert finding["overridden"] is True and "skipped_actions" not in finding
    assert report["mass_repair_override"] == {"forced": True, "actions": ["reap"]}
    receipt = json.loads((repo / report["receipt"]).read_text())
    assert receipt["mass_repair_override"] == report["mass_repair_override"]


def test_configured_count_threshold_blocks_a_smaller_mass_reap(tmp_path, monkeypatch, capsys):
    repo, reaped = _dead_lanes(tmp_path, monkeypatch, dead=3, healthy=17)
    code, report, _ = _run(repo, capsys, apply="reap", mass_max_count=2)
    assert code == 1 and reaped == []
    assert report["findings"][0]["max_count"] == 2


def test_read_only_output_gains_only_the_finding(tmp_path, monkeypatch, capsys):
    repo, _ = _dead_lanes(tmp_path, monkeypatch, dead=6, healthy=14)
    code, report, err = _run(repo, capsys)
    assert code == 0, err
    (finding,) = report["findings"]
    assert finding["kind"] == "suspected_environment_fault" and "skipped_actions" not in finding
    assert report["actions"] == [] and report["receipt"] is None
    assert not (repo / ".brigade/patrol").exists()

    code, _, _ = _run(repo, capsys, fail_on="dead")
    assert code == 1  # fail-on keeps its own meaning


def test_text_output_names_the_finding(tmp_path, monkeypatch, capsys):
    repo, _ = _dead_lanes(tmp_path, monkeypatch, dead=6, healthy=14)
    assert runs_patrol.patrol(target=repo, now=NOW, apply="reap") == 1
    out = capsys.readouterr().out
    assert "suspected_environment_fault: 6 of 20 lanes dead or stalled" in out
    assert "skipped reap" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--force-mass-repair"],
        ["--mass-max-count", "-1"],
        ["--mass-max-fraction", "1.5"],
        ["--mass-max-fraction", "nan"],
    ],
)
def test_guard_options_are_validated_before_writing(tmp_path, capsys, argv):
    repo = _repo(tmp_path)
    assert cli.main(["runs", "patrol", "--target", str(repo), *argv]) == 2
    assert "error: patrol:" in capsys.readouterr().err
    assert not (repo / ".brigade/patrol").exists()


def test_cli_wires_guard_options(tmp_path, monkeypatch, capsys):
    repo, reaped = _dead_lanes(tmp_path, monkeypatch, dead=3, healthy=17)
    argv = ["runs", "patrol", "--target", str(repo), "--json", "--apply", "reap"]
    assert cli.main([*argv, "--mass-max-count", "2", "--mass-max-fraction", "0.9"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["findings"][0]["max_fraction"] == 0.9 and reaped == []
    assert cli.main([*argv, "--mass-max-count", "2", "--force-mass-repair"]) == 0
    assert len(reaped) == 3
