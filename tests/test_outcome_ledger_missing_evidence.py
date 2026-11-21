# ruff: noqa: F811 - the shared ledger fixtures are imported and used by name
"""Absent attest evidence never helps a skill text (#2589).

The agent that works on this repository holds a token that can delete or cancel Actions
runs. A deleted run looks like "nothing to attest", so the ledger derives which commits
need a verdict from git and counts a missing one as hurt. Hurt only restores approved
text, which is the safe direction.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from brigade import outcome_ledger as L, outcome_ledger_actions as A
from brigade.outcome_ledger_trials import ATTEST_GRACE
from tests.test_outcome_ledger import (  # noqa: F401 - pytest fixtures
    SLUG,
    T,
    FakeGh,
    World,
    world,
)

PLAN_JOB = "outcome-attest-plan"


@pytest.fixture()
def base(world: World) -> dict[str, Any]:
    """main: c0 with a genesis ledger on top. Approved text is the c0 bundle."""
    approved = (world.approved_row(world.c0),)
    genesis = world.build(prev_raw=None, covers=world.c0, approved=approved, cutoff=T % (21, "00:00:00Z"))
    world.commit({L.LEDGER_PATH: genesis.decode()}, T % (21, "00:05:00Z"), branch="main")
    return {"genesis": genesis, "approved": approved}


def rows(raw: bytes) -> list[dict[str, Any]]:
    return json.loads(raw)["subjects"]["skill:note"]["trials"]


def summary(raw: bytes) -> list[tuple[str, str, str | None]]:
    return [(row["sha"], row["outcome"], row["reason"]) for row in rows(raw)]


def run_without_job(gh: FakeGh, sha: str, done: str) -> None:
    """A push run exists and its plan job finished, but no attest job ran."""
    gh.attest(sha, [("success", done)], name=PLAN_JOB)


def accepted(world: World, base: dict[str, Any], raw: bytes, covers: str, *, name: str, **kwargs: Any) -> list[str]:
    world.propose(raw, covers, name=name)
    return world.verify(raw, base["genesis"], head=name, **kwargs)


def tampered(raw: bytes, outcome: str, reason: str | None) -> bytes:
    ledger = json.loads(raw)
    for row in ledger["subjects"]["skill:note"]["trials"]:
        row["outcome"], row["reason"] = outcome, reason
    return L.canonical_bytes(ledger)


# ---------------------------------------------------------------------------
# A normal pass still helps
# ---------------------------------------------------------------------------


def test_a_single_commit_with_a_passing_job_is_still_one_helped_trial(world, base):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    world.gh.passed(c1, done=T % (22, "00:10:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=c1, approved=base["approved"])
    assert summary(raw) == [(c1, "helped", None)]
    assert rows(raw)[0]["jobs"][0]["conclusion"] == "success"
    world.propose(raw, c1, name="pass")
    assert world.verify(raw, base["genesis"], head="pass") == []


# ---------------------------------------------------------------------------
# A deleted run
# ---------------------------------------------------------------------------


def test_a_deleted_run_folds_its_commits_into_the_next_tip_whose_run_lacks_the_job(world, base):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))  # in scope, its run was deleted
    c2 = world.commit({"README.md": "later\n"}, T % (23, "00:00:00Z"), branch="main")
    run_without_job(world.gh, c2, T % (23, "00:10:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=c2, approved=base["approved"])
    assert summary(raw) == [(c2, "hurt", "missing")]
    assert rows(raw)[0]["jobs"] == []
    assert json.loads(raw)["subjects"]["skill:note"]["decision"]["code"] == "regression_hold"
    assert accepted(world, base, raw, c2, name="deleted-middle") == []
    # A ledger that reads the same history as helped, or as nothing, is not the recomputed one.
    emptied = json.loads(raw)
    emptied["subjects"]["skill:note"]["trials"] = []
    for name, forged in (("helped", tampered(raw, "helped", None)), ("empty", L.canonical_bytes(emptied))):
        world.propose(forged, c2, name=f"forged-{name}")
        errors = world.verify(forged, base["genesis"], head=f"forged-{name}")
        assert any("differs from the recomputed ledger" in error for error in errors), (name, errors)
    assert c1 != c2


def test_the_same_history_with_the_run_kept_is_helped_for_the_first_commit_only(world, base):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    world.gh.passed(c1, done=T % (22, "00:10:00Z"))
    c2 = world.commit({"README.md": "later\n"}, T % (23, "00:00:00Z"), branch="main")
    run_without_job(world.gh, c2, T % (23, "00:10:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=c2, approved=base["approved"])
    assert summary(raw) == [(c1, "helped", None)]


def test_a_deleted_trailing_run_counts_as_hurt_once_the_grace_window_is_over(world, base):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    world.gh.passed(c1, done=T % (22, "00:10:00Z"))
    c2 = world.touch_skill_json(2, T % (23, "00:00:00Z"))  # in scope, its run was deleted, nothing after it
    raw = world.build(prev_raw=base["genesis"], covers=c2, approved=base["approved"])
    assert summary(raw) == [(c1, "helped", None), (c2, "hurt", "missing")]
    assert accepted(world, base, raw, c2, name="deleted-trailing") == []
    helped = tampered(raw, "helped", None)
    world.propose(helped, c2, name="forged-trailing")
    assert world.verify(helped, base["genesis"], head="forged-trailing")


def test_deleting_the_run_of_a_commit_never_lets_a_text_promote(world, base):
    for number in (1, 2):
        sha = world.touch_skill_json(number, T % (22, f"0{number}:00:00Z"))
        world.gh.passed(sha, done=T % (22, f"0{number}:10:00Z"))
    kept = world.build(prev_raw=base["genesis"], covers=sha, approved=base["approved"])
    assert json.loads(kept)["subjects"]["skill:note"]["decision"]["action"] == "install"
    last = world.touch_skill_json(3, T % (22, "12:00:00Z"))  # its run was deleted
    raw = world.build(prev_raw=base["genesis"], covers=last, approved=base["approved"])
    decision = json.loads(raw)["subjects"]["skill:note"]["decision"]
    assert decision["action"] != "install", decision
    assert summary(raw)[-1] == (last, "hurt", "missing")


def test_a_ledger_that_never_recorded_the_deleted_trailing_commit_is_rejected(world, base):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    world.gh.passed(c1, done=T % (22, "00:10:00Z"))
    c2 = world.touch_skill_json(2, T % (23, "00:00:00Z"))
    # Written while c2 was still inside the grace window, so it records nothing for c2.
    early = world.build(prev_raw=base["genesis"], covers=c2, approved=base["approved"], cutoff=T % (23, "01:00:00Z"))
    assert summary(early) == [(c1, "helped", None)]
    world.propose(early, c2, name="early")
    errors = world.verify(early, base["genesis"], head="early")
    assert any("stale" in error and c2[:12] in error for error in errors), errors


# ---------------------------------------------------------------------------
# A cancelled or skipped job
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "neutral", "action_required"])
def test_a_job_that_did_not_conclude_with_success_is_hurt_with_its_reason(world, base, conclusion):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    world.gh.attest(c1, [(conclusion, T % (22, "00:10:00Z"))])
    raw = world.build(prev_raw=base["genesis"], covers=c1, approved=base["approved"])
    assert summary(raw) == [(c1, "hurt", conclusion)]
    assert accepted(world, base, raw, c1, name=f"job-{conclusion}") == []
    forged = tampered(raw, "helped", None)
    world.propose(forged, c1, name=f"forged-{conclusion}")
    assert world.verify(forged, base["genesis"], head=f"forged-{conclusion}")


def test_a_run_whose_attest_job_never_appeared_is_hurt_missing(world, base):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    run_without_job(world.gh, c1, T % (22, "00:10:00Z"))
    assert summary(world.build(prev_raw=base["genesis"], covers=c1, approved=base["approved"])) == [
        (c1, "hurt", "missing")
    ]


def test_every_attempt_needs_its_own_completed_job(world, base):
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    run = world.gh.attest(c1, [("success", T % (22, "00:10:00Z"))], [("success", T % (22, "00:20:00Z"))])
    run["_jobs"][2] = []  # the second attempt has no attest job
    raw = world.build(prev_raw=base["genesis"], covers=c1, approved=base["approved"])
    assert summary(raw) == [(c1, "hurt", "missing")]
    assert [job["attempt"] for job in rows(raw)[0]["jobs"]] == [1]


# ---------------------------------------------------------------------------
# A batched push
# ---------------------------------------------------------------------------


def batch(world: World) -> tuple[str, str, str]:
    first = world.commit({"README.md": "one\n"}, T % (23, "00:00:00Z"), branch="main")
    middle = world.touch_skill_json(1, T % (23, "00:01:00Z"))
    tip = world.commit({"README.md": "three\n"}, T % (23, "00:02:00Z"), branch="main")
    return first, middle, tip


def test_a_batched_push_where_only_the_middle_commit_touches_scope_is_helped_by_the_tip_job(world, base):
    _first, _middle, tip = batch(world)
    world.gh.passed(tip, done=T % (23, "00:20:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=tip, approved=base["approved"])
    assert summary(raw) == [(tip, "helped", None)]


def test_a_batched_push_where_only_the_middle_commit_touches_scope_is_hurt_when_the_tip_run_has_no_job(world, base):
    _first, _middle, tip = batch(world)
    run_without_job(world.gh, tip, T % (23, "00:20:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=tip, approved=base["approved"])
    assert summary(raw) == [(tip, "hurt", "missing")]
    assert accepted(world, base, raw, tip, name="batch") == []


def test_a_batched_push_that_never_touches_scope_needs_no_job(world, base):
    first = world.commit({"README.md": "one\n"}, T % (23, "00:00:00Z"), branch="main")
    world.commit({"README.md": "two\n"}, T % (23, "00:01:00Z"), branch="main")
    tip = world.commit({"README.md": "three\n"}, T % (23, "00:02:00Z"), branch="main")
    run_without_job(world.gh, tip, T % (23, "00:20:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=tip, approved=base["approved"])
    assert rows(raw) == []
    assert first


# ---------------------------------------------------------------------------
# The grace window
# ---------------------------------------------------------------------------


def test_the_grace_window_is_two_hours():
    assert ATTEST_GRACE.total_seconds() == 2 * 60 * 60


def test_an_in_scope_commit_without_a_run_makes_the_ledger_stale_inside_the_grace_window(world, base):
    c1 = world.touch_skill_json(1, T % (23, "12:00:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=c1, approved=base["approved"], cutoff=T % (23, "13:00:00Z"))
    assert rows(raw) == []
    world.propose(raw, c1, name="in-grace")
    now = dt.datetime(2026, 11, 23, 13, 30, tzinfo=dt.timezone.utc)
    errors = world.verify(raw, base["genesis"], head="in-grace", now=now)
    assert any("stale" in error and "grace" in error and c1[:12] in error for error in errors), errors


def test_after_the_grace_window_the_missing_run_counts_as_hurt(world, base):
    c1 = world.touch_skill_json(1, T % (23, "12:00:00Z"))
    raw = world.build(prev_raw=base["genesis"], covers=c1, approved=base["approved"], cutoff=T % (23, "14:00:00Z"))
    assert summary(raw) == [(c1, "hurt", "missing")]
    now = dt.datetime(2026, 11, 23, 14, 30, tzinfo=dt.timezone.utc)
    assert accepted(world, base, raw, c1, name="after-grace", now=now) == []


def test_the_grace_window_runs_from_the_newest_in_scope_commit_not_from_a_later_commit(world, base):
    c1 = world.touch_skill_json(1, T % (23, "10:00:00Z"))
    world.commit({"README.md": "later\n"}, T % (23, "13:59:00Z"), branch="main")
    tip = world.git("rev-parse", "HEAD")
    raw = world.build(prev_raw=base["genesis"], covers=tip, approved=base["approved"], cutoff=T % (23, "14:00:00Z"))
    assert summary(raw) == [(tip, "hurt", "missing")]
    assert c1


# ---------------------------------------------------------------------------
# Fields are read strictly
# ---------------------------------------------------------------------------

GH_SHA = "a" * 40


def one_run() -> tuple[FakeGh, dict[str, Any]]:
    gh = FakeGh()
    return gh, gh.passed(GH_SHA)


def read(gh: FakeGh, *, required: bool = False) -> Any:
    ref = dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc)
    return A.trial_for_commit(gh, SLUG, 4242, "skill-note", GH_SHA, ref, required=required)


@pytest.mark.parametrize("value", [None, 1, True, ["completed"]])
def test_a_run_status_that_is_missing_or_not_text_is_an_evidence_error(value):
    gh, run = one_run()
    del run["status"]
    with pytest.raises(A.EvidenceError):
        read(gh)
    if value is not None:
        run["status"] = value
        with pytest.raises(A.EvidenceError):
            read(gh)


@pytest.mark.parametrize("value", ["1", 1.0, True, None])
def test_a_job_attempt_that_is_missing_or_not_an_integer_is_an_evidence_error(value):
    gh, run = one_run()
    job = run["_jobs"][1][0]
    del job["run_attempt"]
    with pytest.raises(A.EvidenceError):
        read(gh)
    job["run_attempt"] = value
    with pytest.raises(A.EvidenceError):
        read(gh)


def test_a_job_status_that_is_missing_is_an_evidence_error():
    gh, run = one_run()
    del run["_jobs"][1][0]["status"]
    with pytest.raises(A.EvidenceError):
        read(gh)


def test_a_job_of_another_name_is_not_read_strictly():
    gh, run = one_run()
    other = dict(run["_jobs"][1][0], name=PLAN_JOB)
    del other["run_attempt"]
    run["_jobs"][1].append(other)
    found = read(gh)
    assert found is not None and found["outcome"] == "helped"


def test_a_job_from_another_attempt_or_run_is_ignored_so_the_attempt_reads_as_missing():
    gh, run = one_run()
    run["_jobs"][1][0]["run_attempt"] = 2
    found = read(gh, required=True)
    assert found is not None and (found["outcome"], found["reason"]) == ("hurt", "missing")
