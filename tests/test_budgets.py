"""Tests for the canonical brigade.budgets source of truth."""
from __future__ import annotations

from pathlib import Path

from brigade import budgets


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
