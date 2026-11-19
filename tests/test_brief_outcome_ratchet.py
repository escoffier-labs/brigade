"""The session brief names the last advisory outcome ratchet report (#2589)."""

from __future__ import annotations

from datetime import datetime, timezone

from brigade import outcome, outcome_ratchet_report as report_mod
from brigade.work_cmd.session import briefing

NOW = datetime(2026, 11, 17, 7, 30, tzinfo=timezone.utc)


class _Item:
    def __init__(self, artifact_id: str) -> None:
        self.decision = outcome.Decision(
            artifact_id,
            "hold",
            "candidate",
            "withheld: verified regression present",
            code=outcome.REGRESSION_HOLD,
        )
        self.prior_status = "candidate"
        self.cohorts = None
        self.scorecard = None


def test_line_says_no_report_yet_when_absent():
    assert briefing._outcome_ratchet_line({"present": False}) == "outcome_ratchet: no report yet"
    assert briefing._outcome_ratchet_line(None) == "outcome_ratchet: no report yet"


def test_line_lists_holds_and_leading_reason():
    payload = {
        "present": True,
        "generated_at": NOW.isoformat(),
        "mode": "dry-run",
        "regression_holds": 2,
        "regression_hold_ids": ["skill-a", "skill-b"],
        "ineligibility_rate": 1.0,
        "leading_reason": "unattributed",
    }
    assert briefing._outcome_ratchet_line(payload) == (
        "outcome_ratchet: generated_at=2026-11-17T07\\x3a30\\x3a00\\x2b00\\x3a00 "
        "mode=dry-run regression_holds=2 (skill-a, skill-b) "
        "ineligibility_rate=1.0 leading=unattributed pending_physical_actions=0"
    )


def test_line_shows_the_pending_physical_action_count():
    payload = {"present": True, "generated_at": NOW.isoformat(), "mode": "applied", "pending_physical_actions": 3}
    assert briefing._outcome_ratchet_line(payload).endswith("pending_physical_actions=3")


def test_line_shows_stale_only_when_nonzero():
    base = {"present": True, "generated_at": NOW.isoformat(), "mode": "applied", "pending_physical_actions": 2}
    assert briefing._outcome_ratchet_line({**base, "stale_physical_actions": 62}).endswith(
        "pending_physical_actions=2 stale=62"
    )
    assert "stale=" not in briefing._outcome_ratchet_line({**base, "stale_physical_actions": 0})


def test_line_marks_an_applied_run():
    payload = {
        "present": True,
        "generated_at": NOW.isoformat(),
        "mode": "applied",
        "regression_holds": 0,
        "regression_hold_ids": [],
    }
    assert "mode=applied" in briefing._outcome_ratchet_line(payload)


def test_line_counts_holds_beyond_the_listed_ids():
    payload = {
        "present": True,
        "generated_at": NOW.isoformat(),
        "mode": "dry-run",
        "regression_holds": 20,
        "regression_hold_ids": ["skill-a", "skill-b"],
    }
    line = briefing._outcome_ratchet_line(payload)
    assert "regression_holds=20 (skill-a, skill-b +18 more)" in line


def test_line_never_says_none_beside_a_nonzero_hold_count():
    payload = {
        "present": True,
        "generated_at": NOW.isoformat(),
        "mode": "dry-run",
        "regression_holds": 3,
        "regression_hold_ids": [],
    }
    line = briefing._outcome_ratchet_line(payload)
    assert "(none)" not in line
    assert "regression_holds=3 (3 not listed)" in line


def test_line_handles_empty_hold_list():
    payload = {"present": True, "generated_at": NOW.isoformat(), "mode": "dry-run", "regression_holds": 0}
    line = briefing._outcome_ratchet_line(payload)
    assert "regression_holds=0 (none)" in line
    assert "leading=none" in line


def test_brief_payload_carries_one_bounded_summary(tmp_path):
    empty = briefing._brief_payload(tmp_path)
    assert empty["outcome_ratchet"] == {"present": False}

    report_mod.write_report(
        tmp_path,
        [_Item("skill-a"), _Item("skill-b")],
        apply=False,
        generated_at=NOW,
    )
    payload = briefing._brief_payload(tmp_path)
    ratchet = payload["outcome_ratchet"]
    assert ratchet["present"] is True
    assert ratchet["generated_at"] == NOW.isoformat()
    assert ratchet["mode"] == "dry-run"
    assert ratchet["regression_holds"] == 2
    assert ratchet["regression_hold_ids"] == ["skill-a", "skill-b"]
    assert "regressions" not in ratchet


def test_proposals_line_reads_the_persisted_counts():
    base = {"present": True, "generated_at": NOW.isoformat(), "mode": "dry-run"}
    assert briefing._outcome_proposals_line({"present": False}) == "outcome_proposals: no report yet"
    assert briefing._outcome_proposals_line(None) == "outcome_proposals: no report yet"
    assert briefing._outcome_proposals_line(base).startswith("outcome_proposals: not requested")
    counts = {"filed": 2, "existing": 1, "flagged": 3, "skipped": 0}
    assert briefing._outcome_proposals_line({**base, "outcome_proposals": counts}) == (
        "outcome_proposals: filed=2 existing=1 flagged=3 skipped=0"
    )


def test_brief_payload_carries_the_proposal_counts_from_the_report(tmp_path):
    counts = {"filed": 1, "existing": 0, "flagged": 2, "skipped": 4}
    report_mod.write_report(tmp_path, [_Item("skill-a")], apply=False, generated_at=NOW, proposal_counts=counts)
    ratchet = briefing._brief_payload(tmp_path)["outcome_ratchet"]
    assert ratchet["outcome_proposals"] == counts

    report_mod.write_report(tmp_path, [_Item("skill-a")], apply=False, generated_at=NOW)
    assert briefing._brief_payload(tmp_path)["outcome_ratchet"]["outcome_proposals"] is None
