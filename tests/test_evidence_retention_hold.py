"""Holds and hold revisions at the retention owner boundary."""

from dataclasses import replace

import pytest

from tests.test_evidence_retention import evaluate

pytest_plugins = ["tests.test_evidence_retention"]


@pytest.mark.parametrize("state", ["active", "unknown", "unavailable", "indeterminate"])
def test_active_or_unestablished_hold_blocks_prerequisite(case, state):
    result = evaluate(case, hold=replace(case["hold"], state=state))
    assert result.hold.state == state and result.prune_prerequisite == "blocked"


@pytest.mark.parametrize("state", ["unheld", "released"])
def test_unheld_and_explicitly_released_holds_remain_distinct(case, state):
    result = evaluate(case, hold=replace(case["hold"], state=state))
    assert result.hold.state == state and result.prune_prerequisite == "supported"


def test_hold_revision_change_cannot_reuse_old_release(case):
    released = replace(case["hold"], state="released")
    result = evaluate(case, hold=released, hold_revision="hold-2")
    assert result.hold.state == "mismatch" and result.prune_prerequisite == "blocked"
    assert result.enforcement.state == "mismatch"


def test_expired_release_evidence_does_not_mean_currently_unheld(case):
    released = replace(
        case["hold"], state="released", binding=replace(case["hold"].binding, valid_until=case["evaluated_at"])
    )
    result = evaluate(case, hold=released)
    assert result.hold.state == "stale" and result.hold.observed_state == "released"
    assert result.prune_prerequisite == "blocked"
