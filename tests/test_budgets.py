"""Tests for the canonical brigade.budgets source of truth."""

from __future__ import annotations

from pathlib import Path

from brigade import budgets, budgets_cmd


def test_bootstrap_flat_limit_invariant():
    # soft < hard < ceiling, so the auditor always has headroom below truncation.
    assert (
        budgets.DEFAULT_BOOTSTRAP_SOFT_LIMIT
        < budgets.DEFAULT_BOOTSTRAP_HARD_LIMIT
        < budgets.BOOTSTRAP_HARD_LIMIT_CEILING
    )


def test_per_file_budgets_stay_within_ceiling():
    for name, budget in budgets.BOOTSTRAP_BUDGETS.items():
        assert budget <= budgets.BOOTSTRAP_HARD_LIMIT_CEILING, name


def test_route_would_exceed_budget_guards_only_bootstrap(tmp_path: Path):
    tools = tmp_path / "TOOLS.md"
    tools.write_text("x" * (budgets.BOOTSTRAP_BUDGETS["TOOLS.md"] - 5))
    exceed, budget = budgets.route_would_exceed_budget(tools, "more content")
    assert exceed is True and budget == budgets.BOOTSTRAP_BUDGETS["TOOLS.md"]

    learnings = tmp_path / "LEARNINGS.md"
    learnings.write_text("y" * 50_000)
    exceed, budget = budgets.route_would_exceed_budget(learnings, "more")
    assert exceed is False and budget is None


def test_budgets_show_json_reports_canonical_values(capsys):
    assert budgets_cmd.show(json_output=True) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["bootstrap_budgets"]["TOOLS.md"] == budgets.BOOTSTRAP_BUDGETS["TOOLS.md"]
    assert payload["handoff_backlog_stale_days"] == budgets.HANDOFF_BACKLOG_STALE_DAYS


def test_budgets_check_fails_oversized_bootstrap_file(tmp_path: Path, capsys):
    (tmp_path / "TOOLS.md").write_text("x" * (budgets.BOOTSTRAP_BUDGETS["TOOLS.md"] + 1))

    assert budgets_cmd.check(target=tmp_path, json_output=True) == 1

    import json

    payload = json.loads(capsys.readouterr().out)
    tools = [row for row in payload["checks"] if row["name"] == "TOOLS.md"][0]
    assert tools["status"] == "fail"


def test_thresholds_match_the_design_examples():
    assert budgets.threshold(4000, budgets.ACT_AT_PERCENT) == 3400
    assert budgets.threshold(4000, budgets.TRIM_TO_PERCENT) == 2800
    assert budgets.threshold(12_000, budgets.TRIM_TO_PERCENT) == 8400
    assert budgets.threshold(20_000, budgets.ACT_AT_PERCENT) == 17_000


def test_limit_for_takes_the_smallest_cap_and_names_it():
    caps = [("loader:openclaw:defaults-max-chars", 40_000), ("loader:openclaw:user-md-fixed-cap", 4000)]
    assert budgets.limit_for(8000, "target:table", caps) == (4000, "loader:openclaw:user-md-fixed-cap")
    assert budgets.limit_for(12_000, "target:table", caps[:1]) == (12_000, "target:table")
    assert budgets.limit_for(20_000, "target:default", []) == (20_000, "target:default")
    # A tie goes to the loader, since a harness enforces it.
    assert budgets.limit_for(4000, "target:config", caps[1:]) == (4000, "loader:openclaw:user-md-fixed-cap")
