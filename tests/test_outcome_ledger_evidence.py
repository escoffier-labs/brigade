# ruff: noqa: F811 - the shared ledger fixtures are imported and used by name
"""Outcome ledger evidence rules: completeness, append only trials, fail closed reads (#2589).

Each test names the way a hurt verdict could otherwise disappear from the ledger.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from brigade import outcome_ledger as L, outcome_ledger_actions as A
from brigade.outcome_ledger_git import GitError, bundle_fingerprint
from tests.test_outcome_ledger import (  # noqa: F401 - pytest fixtures
    CUTOFF_DT,
    NOW,
    REPO_ID,
    SHA,
    SKILL_DIR,
    SLUG,
    T,
    FakeGh,
    World,
    owner_case,
    owner_pr,
    promoted,
    trial,
    world,
)

# ---------------------------------------------------------------------------
# 1. The stored cutoff does not decide which evidence counts
# ---------------------------------------------------------------------------


def test_a_cutoff_set_at_the_covers_commit_time_cannot_hide_its_own_failed_verdict(world, promoted):
    tip = world.touch_skill_json(3, T % (23, "06:00:00Z"))
    world.gh.failed(tip, done=T % (23, "06:10:00Z"))
    # The tip's committer time is before the failure finished, so the failure is "after the cutoff".
    hidden = world.build(
        prev_raw=promoted["raw"], covers=tip, approved=promoted["approved"], cutoff=world.repo.commit_time(tip)
    )
    subject = json.loads(hidden)["subjects"]["skill:note"]
    assert [row["outcome"] for row in subject["trials"]] == ["helped", "helped"]
    assert subject["status"] == "promoted"
    world.propose(hidden, tip, name="hidden")
    errors = world.verify(hidden, promoted["raw"], head="hidden")
    assert errors and any("stale" in error and tip[:12] in error for error in errors), errors


def test_a_verdict_that_arrives_after_the_cutoff_makes_the_ledger_stale(world, promoted):
    tip = world.touch_skill_json(3, T % (23, "06:00:00Z"))
    run = world.gh.passed(tip, done=T % (23, "06:10:00Z"))
    raw = world.build(prev_raw=promoted["raw"], covers=tip, approved=promoted["approved"], cutoff=T % (23, "06:30:00Z"))
    world.propose(raw, tip, name="late")
    assert world.verify(raw, promoted["raw"], head="late") == []
    # A failed rerun finishes after the ledger's cutoff. The ledger read the commit as helped.
    first_job = run["_jobs"][1][0]
    run["_jobs"][2] = [dict(first_job, run_attempt=2, conclusion="failure", completed_at=T % (23, "08:00:00Z"), id=1)]
    run["run_attempt"] = 2
    errors = world.verify(raw, promoted["raw"], head="late")
    assert any("stale" in error for error in errors), errors


@pytest.mark.parametrize("where", ["run", "job"])
def test_a_ledger_built_while_the_tip_verdict_is_still_running_is_stale(world, promoted, where):
    tip = world.touch_skill_json(3, T % (23, "06:00:00Z"))
    run = world.gh.failed(tip, done=T % (23, "06:10:00Z"))
    job = run["_jobs"][1][0]
    job["status"] = "in_progress"
    if where == "run":
        run["status"] = "in_progress"
    raw = world.build(prev_raw=promoted["raw"], covers=tip, approved=promoted["approved"], cutoff=T % (23, "06:30:00Z"))
    assert [row["outcome"] for row in json.loads(raw)["subjects"]["skill:note"]["trials"]] == ["helped", "helped"]
    world.propose(raw, tip, name="running")
    errors = world.verify(raw, promoted["raw"], head="running")
    assert any("still running" in error and tip[:12] in error for error in errors), errors
    # Once it finishes, the failure is a hurt verdict the ledger lacks, so the same ledger stays rejected.
    job["status"] = "completed"
    run["status"] = "completed"
    assert world.verify(raw, promoted["raw"], head="running")


def test_a_ledger_older_than_the_age_limit_is_rejected(world, promoted):
    late = NOW + L.MAX_LEDGER_AGE
    errors = world.verify(promoted["raw"], promoted["genesis"], now=late)
    assert any("ledger age" in error for error in errors), errors
    assert not any("ledger age" in error for error in world.verify(promoted["raw"], promoted["genesis"]))


# ---------------------------------------------------------------------------
# 2. Trials are append only evidence
# ---------------------------------------------------------------------------


@pytest.fixture()
def hurt_history(world):
    """main: c1 failed attest, c2 passed. R1 covers c2. Then Actions retention deletes every run."""
    genesis = world.build(
        prev_raw=None, covers=world.c0, approved=(world.approved_row(world.c0),), cutoff=T % (21, "00:00:00Z")
    )
    world.commit({L.LEDGER_PATH: genesis.decode()}, T % (21, "00:05:00Z"), branch="main")
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    c2 = world.touch_skill_json(2, T % (23, "00:00:00Z"))
    world.gh.failed(c1, done=T % (22, "00:10:00Z"))
    world.gh.passed(c2, done=T % (23, "00:10:00Z"))
    approved = (world.approved_row(world.c0),)
    first = world.build(prev_raw=genesis, covers=c2, approved=approved)
    return {"genesis": genesis, "first": first, "c1": c1, "c2": c2, "approved": approved}


def test_old_trials_are_carried_forward_when_the_api_has_forgotten_them(world, hurt_history):
    world.gh.runs.clear()  # Actions retention
    world.gh.calls.clear()
    c3 = world.touch_skill_json(3, T % (23, "12:00:00Z"))
    world.gh.passed(c3, done=T % (23, "12:10:00Z"))
    second = world.build(
        prev_raw=hurt_history["first"],
        covers=c3,
        approved=hurt_history["approved"],
        cutoff=T % (23, "13:00:00Z"),
    )
    trials = json.loads(second)["subjects"]["skill:note"]["trials"]
    assert [(row["sha"], row["outcome"]) for row in trials] == [
        (hurt_history["c1"], "hurt"),
        (hurt_history["c2"], "helped"),
        (c3, "helped"),
    ]
    old_asks = [call for call in world.gh.calls if hurt_history["c1"] in call or hurt_history["c2"] in call]
    assert old_asks == [], "the API is asked only about commits newer than the previous ledger"


def test_dropping_an_expired_failure_from_the_trials_is_rejected(world, hurt_history):
    world.gh.runs.clear()
    c3 = world.touch_skill_json(3, T % (23, "12:00:00Z"))
    world.gh.passed(c3, done=T % (23, "12:10:00Z"))
    cutoff = T % (23, "13:00:00Z")
    honest = json.loads(
        world.build(prev_raw=hurt_history["first"], covers=c3, approved=hurt_history["approved"], cutoff=cutoff)
    )
    forged = json.loads(json.dumps(honest))
    forged["subjects"]["skill:note"]["trials"] = [
        row for row in forged["subjects"]["skill:note"]["trials"] if row["outcome"] != "hurt"
    ]
    raw = L.canonical_bytes(forged)
    world.commit({L.LEDGER_PATH: hurt_history["first"].decode()}, T % (23, "12:20:00Z"), branch="main")
    world.propose(raw, c3, name="forged")
    errors = world.verify(
        raw, hurt_history["first"], head="forged", now=dt.datetime(2026, 11, 23, 14, 0, tzinfo=dt.timezone.utc)
    )
    assert any("may not change" in error for error in errors), errors


def test_the_honest_ledger_that_carries_an_expired_failure_verifies(world, hurt_history):
    world.gh.runs.clear()
    c3 = world.touch_skill_json(3, T % (23, "12:00:00Z"))
    world.gh.passed(c3, done=T % (23, "12:10:00Z"))
    raw = world.build(
        prev_raw=hurt_history["first"], covers=c3, approved=hurt_history["approved"], cutoff=T % (23, "13:00:00Z")
    )
    world.propose(raw, c3, name="honest")
    errors = world.verify(
        raw, hurt_history["first"], head="honest", now=dt.datetime(2026, 11, 23, 14, 0, tzinfo=dt.timezone.utc)
    )
    assert errors == []


def test_a_manifest_edit_keeps_old_hurt_trials_and_drops_nothing_else(world, hurt_history):
    manifest = json.loads((world.root / "verify/manifests/skill-note.json").read_text())
    manifest["checks"][0]["command"] = "true"
    c3 = world.commit(
        {"verify/manifests/skill-note.json": json.dumps(manifest, indent=2), f"{SKILL_DIR}/skill.json": '{"n": 3}\n'},
        T % (23, "12:00:00Z"),
        branch="main",
    )
    world.gh.passed(c3, done=T % (23, "12:10:00Z"))
    raw = world.build(
        prev_raw=hurt_history["first"], covers=c3, approved=hurt_history["approved"], cutoff=T % (23, "13:00:00Z")
    )
    rows = json.loads(raw)["subjects"]["skill:note"]["trials"]
    # A pin change restarts the cohort, but an earlier hurt verdict is never dropped (security review round 4).
    assert [row["sha"] for row in rows][-1] == c3
    carried = rows[:-1]
    assert carried and all(row["outcome"] == "hurt" for row in carried)


# ---------------------------------------------------------------------------
# 3 and 4. Reads fail closed
# ---------------------------------------------------------------------------


def test_a_run_with_more_attempts_than_the_cap_fails_closed_even_when_the_failure_is_last():
    gh = FakeGh()
    ok = ("success", "2026-11-22T00:10:00Z")
    gh.attest(SHA, *([[ok]] * A.MAX_ATTEMPTS), [("failure", "2026-11-22T00:20:00Z")])
    assert gh.runs[SHA][0]["run_attempt"] == A.MAX_ATTEMPTS + 1
    with pytest.raises(A.EvidenceError):
        trial(gh)


def test_a_run_at_exactly_the_cap_is_read_in_full():
    gh = FakeGh()
    ok = ("success", "2026-11-22T00:10:00Z")
    gh.attest(SHA, *([[ok]] * (A.MAX_ATTEMPTS - 1)), [("failure", "2026-11-22T00:20:00Z")])
    found = trial(gh)
    assert found is not None and found["outcome"] == "hurt"


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"total_count": 1},
        {"workflow_runs": None, "total_count": 0},
        {"workflow_runs": "none", "total_count": 0},
        {"workflow_runs": [], "message": "rate limited"},
        {"workflow_runs": ["not an object"], "total_count": 1},
        {"workflow_runs": [], "total_count": "0"},
        [],
        None,
    ],
    ids=[
        "empty",
        "no-key",
        "null-list",
        "string-list",
        "no-total",
        "non-object-row",
        "string-total",
        "bare-list",
        "null",
    ],
)
def test_a_malformed_runs_payload_is_not_read_as_no_runs(answer):
    gh = FakeGh()
    gh.failed(SHA)
    gh.overrides["/runs?"] = answer
    with pytest.raises(A.EvidenceError):
        trial(gh)


@pytest.mark.parametrize(
    "answer",
    [{}, {"jobs": None, "total_count": 0}, {"jobs": [], "message": "x"}, {"jobs": [1], "total_count": 1}, []],
    ids=["empty", "null-list", "no-total", "non-object-row", "bare-list"],
)
def test_a_malformed_jobs_payload_is_not_read_as_no_jobs(answer):
    gh = FakeGh()
    gh.failed(SHA)
    gh.overrides["/jobs"] = answer
    with pytest.raises(A.EvidenceError):
        trial(gh)


def test_a_finished_attest_job_with_an_unreadable_verdict_fails_closed():
    for field, value in (("conclusion", None), ("completed_at", "soon"), ("id", "x")):
        gh = FakeGh()
        run = gh.failed(SHA)
        run["_jobs"][1][0][field] = value
        with pytest.raises(A.EvidenceError):
            trial(gh)


def test_a_page_that_stops_short_of_total_count_fails_closed():
    gh = FakeGh()
    run = gh.failed(SHA)
    first = {k: v for k, v in run.items() if k != "_jobs"}

    def paged(endpoint: str) -> Any:
        if "workflows" in endpoint:
            return {"total_count": 5, "workflow_runs": [first] if "&page=1" in endpoint else []}
        return gh(endpoint)

    with pytest.raises(A.EvidenceError):
        A.trial_for_commit(paged, SLUG, REPO_ID, "skill-note", SHA, CUTOFF_DT)


def test_runs_and_jobs_are_read_across_pages(monkeypatch):
    monkeypatch.setattr(A, "PER_PAGE", 2)
    gh = FakeGh()
    for _ in range(4):
        gh.passed(SHA)
    last = gh.failed(SHA)
    # Jobs paginate too: the failing attempt sits behind two passing jobs of the same name.
    last["_jobs"][1] = [dict(last["_jobs"][1][0], id=900 + i, conclusion="success") for i in range(2)] + last["_jobs"][
        1
    ]
    found = trial(gh)
    assert found is not None and found["outcome"] == "hurt" and len(found["jobs"]) == 7
    assert any("page=3" in call for call in gh.calls)


def test_a_failed_read_stops_the_ledger_build_and_the_check(world, promoted):
    tip = world.touch_skill_json(3, T % (23, "12:00:00Z"))
    world.gh.passed(tip, done=T % (23, "12:10:00Z"))
    raw = world.build(prev_raw=promoted["genesis"], covers=tip, approved=promoted["approved"])
    world.propose(raw, tip, name="bad-api")
    world.gh.overrides["/runs?"] = {"workflow_runs": None}
    with pytest.raises(A.EvidenceError):
        world.build(prev_raw=promoted["raw"], covers=tip, approved=promoted["approved"])
    errors = world.verify(raw, promoted["genesis"], head="bad-api")
    assert any("could not finish" in error and "EvidenceError" in error for error in errors), errors


def test_reviews_that_are_not_a_list_fail_closed():
    gh = FakeGh()
    gh.pulls[7] = {"head": "a" * 40, "reviews": []}
    gh.overrides["/reviews"] = {"message": "gone"}
    with pytest.raises(A.EvidenceError):
        A.owner_review_ok(gh, SLUG, 7, ("solomonneas",))


# ---------------------------------------------------------------------------
# 7. Timed out jobs
# ---------------------------------------------------------------------------


def test_a_worse_revision_that_times_out_is_rolled_back(world, promoted):
    worse = world.commit(
        {f"{SKILL_DIR}/SKILL.md": "---\nname: note\n---\n# note\n"}, T % (23, "08:00:00Z"), branch="main"
    )
    world.gh.attest(worse, [("timed_out", T % (23, "08:10:00Z"))])
    raw = world.build(prev_raw=promoted["genesis"], covers=worse, approved=promoted["approved"])
    subject = json.loads(raw)["subjects"]["skill:note"]
    assert (subject["decision"]["action"], subject["decision"]["code"]) == ("rollback", "regression_restore")
    assert subject["restore"]["to_commit"] == world.c0


# ---------------------------------------------------------------------------
# 8. Each commit needs its own evidence
# ---------------------------------------------------------------------------


def test_an_out_of_scope_failed_tip_does_not_supply_the_scoped_commits_verdict(world, promoted):
    first = world.touch_skill_json(3, T % (23, "06:00:00Z"))
    tip = world.commit({"README.md": "tip\n"}, T % (23, "06:05:00Z"), branch="main")
    world.gh.failed(tip, done=T % (23, "06:20:00Z"))
    raw = world.build(prev_raw=promoted["raw"], covers=tip, approved=promoted["approved"])
    trials = json.loads(raw)["subjects"]["skill:note"]["trials"]
    assert [row["sha"] for row in trials] == [promoted["c1"], promoted["c2"], first]
    assert (trials[-1]["outcome"], trials[-1]["reason"]) == ("hurt", "missing")
    assert trials[-1]["fingerprint"] == bundle_fingerprint(world.repo.snapshot(first, SKILL_DIR))
    decision = json.loads(raw)["subjects"]["skill:note"]["decision"]
    assert decision["code"] == "no_approved_target", "a hurt verdict reaches the decision, so the text cannot promote"
    world.propose(raw, tip, name="range")
    assert world.verify(raw, promoted["raw"], head="range") == []


def test_an_out_of_scope_successful_tip_records_no_row(world, promoted):
    first = world.touch_skill_json(3, T % (23, "06:00:00Z"))
    tip = world.commit({"README.md": "tip\n"}, T % (23, "06:05:00Z"), branch="main")
    world.gh.passed(tip, done=T % (23, "06:20:00Z"))
    trials = json.loads(world.build(prev_raw=promoted["raw"], covers=tip, approved=promoted["approved"]))["subjects"][
        "skill:note"
    ]["trials"]
    assert [row["sha"] for row in trials] == [promoted["c1"], promoted["c2"], first]
    assert (trials[-1]["outcome"], trials[-1]["reason"]) == ("hurt", "missing")


def test_the_stale_check_names_the_scoped_commit_before_an_out_of_scope_tip(world, promoted):
    first = world.touch_skill_json(3, T % (23, "06:00:00Z"))
    tip = world.commit({"README.md": "tip\n"}, T % (23, "06:05:00Z"), branch="main")
    world.gh.failed(tip, done=T % (23, "06:20:00Z"))
    errors = world.verify(promoted["raw"], promoted["genesis"])
    assert any("stale" in error and first[:12] in error for error in errors), errors


# ---------------------------------------------------------------------------
# 9. Fail closed on an unreadable bundle
# ---------------------------------------------------------------------------


def test_a_bundle_that_exists_but_cannot_be_read_fails_closed_instead_of_reading_as_absent(world, promoted):
    link = world.root / SKILL_DIR / "link"
    world.git("checkout", "-q", "main")
    link.symlink_to("SKILL.md")
    world.git("add", "-A")
    world.git("commit", "-q", "-m", "symlink", when=T % (23, "09:00:00Z"))
    bad = world.git("rev-parse", "HEAD")
    world.gh.passed(bad, done=T % (23, "09:10:00Z"))
    world.commit({f"{SKILL_DIR}/link": None}, T % (23, "10:00:00Z"), branch="main")
    covers = world.git("rev-parse", "HEAD")
    with pytest.raises(GitError):
        world.build(prev_raw=promoted["raw"], covers=covers, approved=promoted["approved"])


def test_a_covers_commit_with_no_bundle_is_still_an_error(world):
    world.commit(
        {f"{SKILL_DIR}/SKILL.md": None, f"{SKILL_DIR}/skill.json": None, f"{SKILL_DIR}/CHANGELOG.md": None},
        T % (22, "00:00:00Z"),
        branch="main",
    )
    with pytest.raises(L.LedgerError):
        world.build(prev_raw=None, covers=world.git("rev-parse", "HEAD"), approved=(world.approved_row(world.c0),))


# ---------------------------------------------------------------------------
# 10. Owner review pagination
# ---------------------------------------------------------------------------


def _commenters(count: int, commit: str) -> list[dict[str, Any]]:
    return [{"user": {"login": f"bot-{n}"}, "state": "COMMENTED", "commit_id": commit} for n in range(count)]


def test_an_owner_approval_on_the_second_page_of_reviews_counts():
    head = "a" * 40
    gh = FakeGh()
    owner = {"user": {"login": "solomonneas"}, "state": "APPROVED", "commit_id": head}
    gh.pulls[7] = {"head": head, "reviews": [*_commenters(A.PER_PAGE, head), owner]}
    assert A.owner_review_ok(gh, SLUG, 7, ("solomonneas",)) is True
    assert any("page=2" in call for call in gh.calls)


def test_a_change_request_on_the_second_page_cancels_an_approval_on_the_first():
    head = "a" * 40
    gh = FakeGh()
    owner = {"user": {"login": "solomonneas"}, "state": "APPROVED", "commit_id": head}
    changes = dict(owner, state="CHANGES_REQUESTED")
    gh.pulls[7] = {"head": head, "reviews": [owner, *_commenters(A.PER_PAGE - 1, head), changes]}
    assert A.owner_review_ok(gh, SLUG, 7, ("solomonneas",)) is False


def test_a_full_last_page_is_followed_by_an_empty_page():
    head = "a" * 40
    gh = FakeGh()
    gh.pulls[7] = {"head": head, "reviews": _commenters(A.PER_PAGE, head)}
    assert A.owner_review_ok(gh, SLUG, 7, ("solomonneas",)) is False
    assert [call for call in gh.calls if "reviews" in call][-1].endswith("page=2")


# ---------------------------------------------------------------------------
# 5. A landed change is verified against the ledger it replaced
# ---------------------------------------------------------------------------


def test_a_landed_ledger_is_verified_with_the_same_rules(world, promoted):
    late = NOW + L.MAX_LEDGER_AGE + dt.timedelta(hours=3)
    assert world.verify(promoted["raw"], promoted["genesis"], now=late, landed=True) == []
    edited = json.loads(promoted["raw"])
    edited["subjects"]["skill:note"]["trials"].pop()
    raw = L.canonical_bytes(edited)
    world.propose(raw, promoted["c2"], name="landed-edit")
    errors = world.verify(raw, promoted["genesis"], head="landed-edit", landed=True)
    assert any("differs" in error for error in errors), errors


def test_a_landed_ledger_may_share_its_push_with_other_changes(world, promoted):
    world.propose(promoted["raw"], promoted["c2"], name="batch", extra={"README.md": "also merged\n"})
    assert world.verify(promoted["raw"], promoted["genesis"], head="batch", landed=True) == []
    errors = world.verify(promoted["raw"], promoted["genesis"], head="batch")
    assert any("may not touch README.md" in error for error in errors), errors
