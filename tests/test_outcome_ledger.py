"""Outcome ledger: trial sourcing, the decision rule and the recompute check (#2589).

Git history is real (a temporary repository). The Actions API is a fixture that answers
only the endpoints the ledger is allowed to read, so a read of the checks API fails the test.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brigade import outcome as outcome_core, outcome_ledger as L, outcome_ledger_actions as A, shipped_history
from brigade.outcome_ledger_git import Repo, bundle_fingerprint

ROOT = Path(__file__).resolve().parents[1]
SLUG = "example-org/example-repo"
REPO_ID = 4242
OTHER_REPO_ID = 9999
SKILL_DIR = "src/brigade/templates/skills/note"
SUBJECT = f"{SKILL_DIR}/SKILL.md"
JOB = "outcome-attest/skill-note"
GOOD = "---\nname: note\n---\n# note\n1. write the note\n"
WORSE = "---\nname: note\n---\n# note\n"
T = "2026-11-%02dT%s"
CUTOFF = "2026-11-24T00:00:00Z"
NOW = dt.datetime(2026, 11, 24, 1, 0, tzinfo=dt.timezone.utc)
CUTOFF_DT = dt.datetime(2026, 11, 24, 0, 0, tzinfo=dt.timezone.utc)


def digest(text: str) -> str:
    return shipped_history.skill_digest(text.encode())


class FakeGh:
    """Answers the Actions and pull request endpoints only. Anything else fails the test."""

    def __init__(self) -> None:
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.overrides: dict[str, Any] = {}
        self._next_id = 1000

    def attest(
        self,
        sha: str,
        *attempts: list[tuple[str, str]],
        name: str = JOB,
        done: str = "2026-11-22T00:10:00Z",
        **run_overrides: Any,
    ) -> dict[str, Any]:
        """Register one run. Each attempt is a list of (job name suffix is ignored) (conclusion, completed_at)."""
        self._next_id += 1
        run: dict[str, Any] = {
            "id": self._next_id,
            "path": A.ATTEST_WORKFLOW_PATH,
            "event": "push",
            "head_branch": "main",
            "head_sha": sha,
            "repository": {"id": REPO_ID},
            "head_repository": {"id": REPO_ID},
            "status": "completed",
            "run_attempt": len(attempts),
            "_jobs": {},
        }
        for number, jobs in enumerate(attempts, start=1):
            run["_jobs"][number] = [
                {
                    "id": self._next_id * 10 + number * 100 + index,
                    "name": name,
                    "status": "completed",
                    "conclusion": conclusion,
                    "run_id": self._next_id,
                    "run_attempt": number,
                    "head_sha": sha,
                    "completed_at": completed or done,
                }
                for index, (conclusion, completed) in enumerate(jobs)
            ]
        run.update(run_overrides)
        self.runs.setdefault(sha, []).append(run)
        return run

    def passed(self, sha: str, done: str = "2026-11-22T00:10:00Z", **kwargs: Any) -> dict[str, Any]:
        return self.attest(sha, [("success", done)], **kwargs)

    def failed(self, sha: str, done: str = "2026-11-22T00:10:00Z", **kwargs: Any) -> dict[str, Any]:
        return self.attest(sha, [("failure", done)], **kwargs)

    def __call__(self, endpoint: str) -> Any:
        self.calls.append(endpoint)
        path, _, query = endpoint.partition("?")
        params = dict(item.split("=") for item in query.split("&")) if query else {}
        parts = path.split("/")
        base = ["repos", *SLUG.split("/")]
        assert parts[:3] == base, endpoint
        rest = parts[3:]
        for needle, answer in self.overrides.items():
            if needle in endpoint:
                return answer
        if not rest:
            return {"id": REPO_ID}
        if rest[:3] == ["actions", "workflows", A.ATTEST_WORKFLOW_FILE] and rest[3:] == ["runs"]:
            rows = [{k: v for k, v in run.items() if k != "_jobs"} for run in self.runs.get(params["head_sha"], [])]
            return self.page(rows, params, "workflow_runs")
        if rest[:2] == ["actions", "runs"] and rest[3] == "attempts" and rest[5:] == ["jobs"]:
            run_id, attempt = int(rest[2]), int(rest[4])
            for runs in self.runs.values():
                for run in runs:
                    if run["id"] == run_id:
                        return self.page(run["_jobs"].get(attempt, []), params, "jobs")
            return self.page([], params, "jobs")
        if rest[0] == "pulls" and len(rest) == 2:
            return {"head": {"sha": self.pulls[int(rest[1])]["head"]}}
        if rest[0] == "pulls" and rest[2:] == ["reviews"]:
            size, number = int(params.get("per_page", "100")), int(params.get("page", "1"))
            return self.pulls[int(rest[1])]["reviews"][(number - 1) * size : number * size]
        raise AssertionError(f"the ledger may not read {endpoint}")

    @staticmethod
    def page(rows: list[dict[str, Any]], params: dict[str, str], key: str) -> dict[str, Any]:
        size, number = int(params.get("per_page", "100")), int(params.get("page", "1"))
        return {"total_count": len(rows), key: rows[(number - 1) * size : number * size]}


class World:
    """A temporary repository on ``main`` plus the Actions fixture."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        self.root = root
        self.gh = FakeGh()
        self.git("init", "-q", "-b", "main")
        manifest = (ROOT / "verify/manifests/skill-note.json").read_bytes().decode()
        history = {"schema": 1, "files": {"skill:note": [{"sha256": digest(GOOD), "tag": "v0.5.0"}]}}
        self.c0 = self.commit(
            {
                "verify/manifests/skill-note.json": manifest,
                f"{SKILL_DIR}/SKILL.md": GOOD,
                f"{SKILL_DIR}/CHANGELOG.md": "# changelog\n",
                f"{SKILL_DIR}/skill.json": '{"id": "note", "n": 0}\n',
                "src/brigade/templates/shipped-file-history.json": json.dumps(history),
                "README.md": "readme\n",
            },
            T % (21, "00:00:00Z"),
        )
        self.repo = Repo(root)
        self.ev = L.Evidence(self.repo, self.gh, SLUG, REPO_ID)

    def git(self, *args: str, when: str | None = None) -> str:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        if when:
            env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        done = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-C",
                str(self.root),
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return done.stdout.strip()

    def commit(self, files: dict[str, str | None], when: str, branch: str | None = None) -> str:
        if branch:
            self.git("checkout", "-q", branch)
        for name, text in files.items():
            path = self.root / name
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode())
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"commit at {when}", when=when)
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str, base: str) -> None:
        self.git("branch", "-f", name, base)

    def touch_skill_json(self, n: int, when: str) -> str:
        return self.commit({f"{SKILL_DIR}/skill.json": f'{{"id": "note", "n": {n}}}\n'}, when, branch="main")

    def approved_row(self, commit: str, basis: str = "release", **extra: Any) -> dict[str, Any]:
        files = self.repo.snapshot(commit, SKILL_DIR)
        row: dict[str, Any] = {
            "basis": basis,
            "commit": commit,
            "fingerprint": bundle_fingerprint(files),
            "skill_md_sha256": shipped_history.skill_digest(files["SKILL.md"]),
        }
        row.update(extra)
        return row

    def definition(self, *approved: dict[str, Any]) -> dict[str, L.SubjectDef]:
        return {"skill:note": L.SubjectDef("skill:note", "skill-note", SUBJECT, self.c0, tuple(approved))}

    def build(
        self,
        *,
        prev_raw: bytes | None,
        covers: str,
        approved: tuple[dict[str, Any], ...],
        cutoff: str = CUTOFF,
    ) -> bytes:
        prev = L.parse_ledger(prev_raw) if prev_raw else None
        ledger = L.build_ledger(
            self.ev,
            prev=prev,
            prev_sha256=L.sha256_hex(prev_raw) if prev_raw else L.GENESIS_PREV,
            definitions=self.definition(*approved),
            covers_sha=covers,
            cutoff_at=cutoff,
        )
        return L.canonical_bytes(ledger)

    def propose(self, raw: bytes, base: str, name: str = "pr", extra: dict[str, str | None] | None = None) -> str:
        """Commit ``raw`` as the ledger on a branch off ``base``."""
        self.branch(name, base)
        files: dict[str, str | None] = {L.LEDGER_PATH: raw.decode(), **(extra or {})}
        return self.commit(files, "2026-11-24T00:30:00Z", branch=name)

    def verify(
        self, raw: bytes, main_raw: bytes | None, *, head: str = "pr", pr: int | None = 7, **kwargs: Any
    ) -> list[str]:
        self.git("checkout", "-q", "main")
        return L.verify_ledger(
            self.ev,
            raw=raw,
            main_raw=main_raw,
            main_ref="main",
            head_ref=head,
            context=L.EventContext("pull_request", pr),
            now=kwargs.pop("now", NOW),
            **kwargs,
        )


@pytest.fixture()
def world(tmp_path: Path) -> World:
    return World(tmp_path / "repo")


@pytest.fixture()
def promoted(world: World) -> dict[str, Any]:
    """main: c0, genesis ledger, two attested skill.json touches. A promotion ledger is ready as a PR."""
    genesis = world.build(
        prev_raw=None, covers=world.c0, approved=(world.approved_row(world.c0),), cutoff=T % (21, "00:00:00Z")
    )
    world.commit({L.LEDGER_PATH: genesis.decode()}, T % (21, "00:05:00Z"), branch="main")
    c1 = world.touch_skill_json(1, T % (22, "00:00:00Z"))
    c2 = world.touch_skill_json(2, T % (23, "00:00:00Z"))
    world.gh.passed(c1, done=T % (22, "00:10:00Z"))
    world.gh.passed(c2, done=T % (23, "00:10:00Z"))
    approved = (world.approved_row(world.c0),)
    raw = world.build(prev_raw=genesis, covers=c2, approved=approved)
    head = world.propose(raw, c2)
    return {"genesis": genesis, "raw": raw, "c1": c1, "c2": c2, "head": head, "approved": approved}


# ---------------------------------------------------------------------------
# Trial sourcing: Actions runs and jobs only, never check runs
# ---------------------------------------------------------------------------

SHA = "a" * 40


def trial(gh: FakeGh, sha: str = SHA, cutoff: dt.datetime = CUTOFF_DT) -> dict[str, Any] | None:
    return A.trial_for_commit(gh, SLUG, REPO_ID, "skill-note", sha, cutoff)


def test_a_passing_attest_job_is_one_helped_trial():
    gh = FakeGh()
    gh.passed(SHA)
    found = trial(gh)
    assert found is not None and found["outcome"] == "helped" and found["sha"] == SHA
    assert not any("check-runs" in call or "statuses" in call for call in gh.calls)


@pytest.mark.parametrize(
    "override",
    [
        {"path": ".github/workflows/other.yml"},
        {"path": ".github/workflows/outcome-attest.yml@refs/heads/feature"},
        {"event": "pull_request"},
        {"event": "workflow_dispatch"},
        {"head_branch": "feature"},
        {"head_sha": "b" * 40},
        {"repository": {"id": OTHER_REPO_ID}},
        {"head_repository": {"id": OTHER_REPO_ID}},
        {"head_repository": None},
    ],
    ids=[
        "other-path",
        "path-with-ref",
        "pr-event",
        "dispatch-event",
        "other-branch",
        "other-sha",
        "other-repo",
        "fork-head",
        "no-head-repo",
    ],
)
def test_a_run_that_fails_any_identity_check_is_not_a_trial(override):
    gh = FakeGh()
    gh.passed(SHA, **override)
    assert trial(gh) is None


def test_a_job_with_another_name_or_a_foreign_run_id_is_not_a_trial():
    gh = FakeGh()
    gh.passed(SHA, name="outcome-attest/other-manifest")
    assert trial(gh) is None
    gh = FakeGh()
    run = gh.passed(SHA)
    run["_jobs"][1][0]["run_id"] = 1
    assert trial(gh) is None
    gh = FakeGh()
    run = gh.passed(SHA)
    run["_jobs"][1][0]["head_sha"] = "b" * 40
    assert trial(gh) is None


def test_a_failed_attempt_counts_as_hurt_even_when_a_later_attempt_passes():
    gh = FakeGh()
    gh.attest(SHA, [("failure", "2026-11-22T00:10:00Z")], [("success", "2026-11-22T00:20:00Z")])
    found = trial(gh)
    assert found is not None and found["outcome"] == "hurt"
    assert [job["attempt"] for job in found["jobs"]] == [1, 2]


def test_an_attempt_finished_after_the_cutoff_leaves_the_verdict_pending():
    """A later attempt can still turn the verdict, so nothing is recorded until it is visible."""
    gh = FakeGh()
    gh.attest(SHA, [("success", "2026-11-23T00:10:00Z")], [("failure", "2026-11-25T00:10:00Z")])
    assert trial(gh) is None
    gh = FakeGh()
    gh.attest(SHA, [("success", "2026-11-25T00:10:00Z")])
    assert trial(gh) is None


@pytest.mark.parametrize(
    ("conclusion", "reason"),
    [
        ("failure", "failure"),
        ("timed_out", "timed_out"),
        ("cancelled", "cancelled"),
        ("skipped", "skipped"),
        ("neutral", "neutral"),
        ("stale", "stale"),
        ("action_required", "action_required"),
        ("startup_failure", "startup_failure"),
        ("a_conclusion_github_adds_later", "other"),
    ],
)
def test_every_conclusion_but_success_is_hurt_with_its_own_reason(conclusion, reason):
    gh = FakeGh()
    gh.attest(SHA, [(conclusion, "2026-11-22T00:10:00Z")])
    found = trial(gh)
    assert found is not None and (found["outcome"], found["reason"]) == ("hurt", reason)


def test_only_success_is_helped_and_a_helped_row_has_the_same_keys_as_a_hurt_one():
    gh = FakeGh()
    gh.passed(SHA)
    helped = trial(gh)
    gh = FakeGh()
    gh.failed(SHA)
    hurt = trial(gh)
    assert helped is not None and hurt is not None
    assert helped["outcome"] == "helped" and helped["reason"] is None
    assert set(helped) == set(hurt) == {"sha", "outcome", "reason", "jobs"}


def test_a_cancelled_attempt_next_to_a_passing_one_still_hurts():
    gh = FakeGh()
    gh.attest(SHA, [("cancelled", "2026-11-22T00:10:00Z")], [("success", "2026-11-22T00:20:00Z")])
    found = trial(gh)
    assert found is not None and (found["outcome"], found["reason"]) == ("hurt", "cancelled")


def test_a_timed_out_job_is_a_hurt_trial_like_a_failure():
    """A worse revision that hangs instead of failing must still be rolled back."""
    gh = FakeGh()
    gh.attest(SHA, [("timed_out", "2026-11-22T00:10:00Z")])
    found = trial(gh)
    assert found is not None and found["outcome"] == "hurt"
    gh = FakeGh()
    gh.attest(SHA, [("timed_out", "2026-11-22T00:10:00Z")], [("success", "2026-11-22T00:20:00Z")])
    found = trial(gh)
    assert found is not None and found["outcome"] == "hurt"


def test_an_unfinished_job_is_not_a_trial():
    gh = FakeGh()
    run = gh.passed(SHA)
    run["_jobs"][1][0]["status"] = "in_progress"
    assert trial(gh) is None


def test_two_runs_on_one_commit_make_one_trial():
    gh = FakeGh()
    gh.passed(SHA)
    gh.failed(SHA)
    found = trial(gh)
    assert found is not None and found["outcome"] == "hurt" and len({job["run_id"] for job in found["jobs"]}) == 2


# ---------------------------------------------------------------------------
# The decision rule
# ---------------------------------------------------------------------------

OLD, NEW = "f1" * 32, "f2" * 32


def decide(*, current=NEW, approved=(OLD, NEW), demoted=(), prior=None, helped=0, hurt=0, other=(), last=None):
    trials = [{"fingerprint": current, "outcome": "helped"}] * helped + [
        {"fingerprint": current, "outcome": "hurt"}
    ] * hurt
    trials += [{"fingerprint": fp, "outcome": out} for fp, out in other]
    return L.decide_subject(
        artifact_id="note",
        fingerprint=current,
        approved=approved,
        demoted=demoted,
        prior_status=prior,
        trials=trials,
        last_action_at=last,
        cutoff=CUTOFF_DT,
        config=outcome_core.ReconcileConfig(),
    )


def test_one_helped_trial_is_not_enough_and_two_promote_approved_text():
    one = decide(helped=1)
    assert (one.action, one.status, one.restore_to) == ("hold", "candidate", None)
    two = decide(helped=2)
    assert (two.action, two.status, two.band) == ("install", "promoted", "promoted")


def test_attested_text_that_was_never_approved_waits_for_the_owner():
    result = decide(approved=(OLD,), helped=2)
    assert (result.action, result.code, result.status, result.restore_to) == (
        "hold",
        "awaiting_owner",
        "candidate",
        None,
    )


def test_a_hurt_trial_on_promoted_approved_text_demotes_it_and_restores_the_older_approved_text():
    result = decide(prior="promoted", helped=3, hurt=1)
    assert (result.action, result.code, result.status) == ("rollback", outcome_core.REGRESSION_ROLLBACK, "demoted")
    assert result.demote_current and result.restore_to == OLD


def test_a_hurt_trial_on_unapproved_text_restores_the_newest_approved_text_even_after_helped_trials():
    result = decide(approved=(OLD,), helped=3, hurt=1)
    assert (result.action, result.code, result.restore_to) == ("rollback", "regression_restore", OLD)
    assert not result.demote_current


def test_with_no_approved_text_to_restore_a_regression_holds():
    result = decide(approved=(NEW,), prior="promoted", hurt=1)
    assert (result.action, result.code, result.restore_to) == ("hold", "no_approved_target", None)
    result = decide(approved=(), hurt=1)
    assert (result.action, result.code) == ("hold", "no_approved_target")


def test_demoted_text_is_never_a_restore_target_and_never_promotes_again():
    result = decide(current="f3" * 32, approved=(OLD, NEW, "f3" * 32), demoted=(NEW,), helped=5)
    assert result.restore_to is None and result.action == "install"
    again = decide(demoted=(NEW,), helped=5)
    assert (again.action, again.status, again.restore_to) == ("rollback", "demoted", OLD)
    only = decide(approved=(NEW,), demoted=(NEW,), helped=5)
    assert (only.action, only.code) == ("hold", "no_approved_target")


def test_approved_text_with_a_regression_it_has_not_outnumbered_stays_held():
    result = decide(prior="candidate", helped=1, hurt=1)
    assert (result.action, result.code, result.restore_to) == ("hold", outcome_core.REGRESSION_HOLD, None)


def test_hurt_trials_of_other_texts_do_not_count_against_the_current_text():
    result = decide(helped=2, other=[(OLD, "hurt")])
    assert result.action == "install"


def test_unapproved_text_cannot_stay_promoted_on_a_stale_status():
    result = decide(approved=(OLD,), prior="promoted", helped=2)
    assert result.status == "candidate" and result.code == "awaiting_owner"


def test_the_cohort_rule_is_the_scorecard_rule():
    """Promotion thresholds come from ReconcileConfig, not from numbers in the ledger."""
    config = outcome_core.ReconcileConfig()
    assert config.install_min_helped == 2
    assert outcome_core.wilson_lower_bound(2, 2) > config.effective_wilson_min
    assert L.policy_block(config)["scorecard_policy"] == "scorecard.v1"


# ---------------------------------------------------------------------------
# Building and recomputing the ledger
# ---------------------------------------------------------------------------


def test_two_attested_commits_on_approved_text_promote_and_the_recompute_accepts_it(world, promoted):
    ledger = json.loads(promoted["raw"])
    subject = ledger["subjects"]["skill:note"]
    assert (subject["status"], subject["band"], subject["decision"]["action"]) == ("promoted", "promoted", "install")
    assert [row["outcome"] for row in subject["trials"]] == ["helped", "helped"]
    assert len({row["fingerprint"] for row in subject["trials"]}) == 1
    assert world.verify(promoted["raw"], promoted["genesis"]) == []


def test_genesis_ledger_recomputes_without_a_previous_ledger(world):
    raw = world.build(
        prev_raw=None, covers=world.c0, approved=(world.approved_row(world.c0),), cutoff=T % (21, "00:00:00Z")
    )
    world.propose(raw, world.c0)
    assert world.verify(raw, None, now=dt.datetime(2026, 11, 21, 1, 0, tzinfo=dt.timezone.utc)) == []


def test_a_hand_edited_ledger_fails(world, promoted):
    edited = json.loads(promoted["raw"])
    edited["subjects"]["skill:note"]["trials"].pop()
    raw = L.canonical_bytes(edited)
    world.propose(raw, promoted["c2"], name="edited")
    assert any("differs" in error for error in world.verify(raw, promoted["genesis"], head="edited"))
    forced = json.loads(promoted["raw"])
    forced["subjects"]["skill:note"]["decision"]["reason"] = "forced"
    raw = L.canonical_bytes(forced)
    world.propose(raw, promoted["c2"], name="forced")
    assert any("differs" in error for error in world.verify(raw, promoted["genesis"], head="forced"))


def test_a_ledger_that_is_not_in_canonical_form_fails(world, promoted):
    raw = promoted["raw"].replace(b"\n", b" \n", 1)
    world.propose(raw, promoted["c2"], name="spaced")
    assert any("differs" in error for error in world.verify(raw, promoted["genesis"], head="spaced"))


def test_a_ledger_that_does_not_build_on_the_ledger_on_main_fails(world, promoted):
    edited = json.loads(promoted["raw"])
    edited["prev_ledger_sha256"] = "1" * 64
    raw = L.canonical_bytes(edited)
    world.propose(raw, promoted["c2"], name="forked")
    assert any("prev_ledger_sha256" in error for error in world.verify(raw, promoted["genesis"], head="forked"))


def test_a_stale_ledger_fails_once_main_has_a_newer_verdict(world, promoted):
    c3 = world.touch_skill_json(3, T % (23, "12:00:00Z"))
    world.gh.passed(c3, done=T % (23, "12:10:00Z"))
    errors = world.verify(promoted["raw"], promoted["genesis"])
    assert any("stale" in error for error in errors), errors


def test_a_newer_main_commit_without_a_verdict_does_not_make_the_ledger_stale(world, promoted):
    world.touch_skill_json(3, T % (23, "12:00:00Z"))
    assert world.verify(promoted["raw"], promoted["genesis"]) == []


def test_a_ledger_covering_history_outside_main_fails(world, promoted):
    world.git("checkout", "-q", "-b", "side", world.c0)
    side = world.commit({"README.md": "side\n"}, T % (22, "00:00:00Z"))
    world.git("checkout", "-q", "main")
    edited = json.loads(promoted["raw"])
    edited["covers"]["main_sha"] = side
    raw = L.canonical_bytes(edited)
    world.propose(raw, promoted["c2"], name="outside")
    assert any(
        "not in trusted main history" in error for error in world.verify(raw, promoted["genesis"], head="outside")
    )


@pytest.mark.parametrize("cutoff", ["2026-11-20T00:00:00Z", "2026-11-25T00:00:00Z"], ids=["before-covers", "future"])
def test_a_cutoff_outside_the_window_fails(world, promoted, cutoff):
    raw = world.build(prev_raw=promoted["genesis"], covers=promoted["c2"], approved=promoted["approved"], cutoff=cutoff)
    world.propose(raw, promoted["c2"], name="cutoff")
    assert any("cutoff_at" in error for error in world.verify(raw, promoted["genesis"], head="cutoff"))


def test_covers_may_not_move_backwards(world, promoted):
    raw = world.build(prev_raw=promoted["raw"], covers=promoted["c1"], approved=promoted["approved"])
    world.commit({L.LEDGER_PATH: promoted["raw"].decode()}, T % (24, "00:20:00Z"), branch="main")
    world.propose(raw, promoted["c2"], name="back")
    errors = world.verify(raw, promoted["raw"], head="back")
    assert any("moved backwards" in error for error in errors), errors


def test_a_manifest_edit_restarts_the_trial_count_under_the_new_manifest(world, promoted):
    manifest = json.loads((world.root / "verify/manifests/skill-note.json").read_text())
    manifest["checks"][0]["command"] = "true"
    c3 = world.commit(
        {"verify/manifests/skill-note.json": json.dumps(manifest, indent=2), f"{SKILL_DIR}/skill.json": '{"n": 3}\n'},
        T % (23, "06:00:00Z"),
        branch="main",
    )
    world.gh.passed(c3, done=T % (23, "06:10:00Z"))
    ledger = json.loads(world.build(prev_raw=promoted["genesis"], covers=c3, approved=promoted["approved"]))
    subject = ledger["subjects"]["skill:note"]
    assert [row["sha"] for row in subject["trials"]] == [c3]
    assert subject["manifest_sha256"] != json.loads(promoted["raw"])["subjects"]["skill:note"]["manifest_sha256"]
    assert subject["status"] == "candidate"


def test_a_failed_then_passed_attempt_makes_a_hurt_trial_in_the_ledger(world, promoted):
    c3 = world.touch_skill_json(3, T % (23, "06:00:00Z"))
    world.gh.attest(c3, [("failure", T % (23, "06:10:00Z"))], [("success", T % (23, "06:30:00Z"))])
    ledger = json.loads(world.build(prev_raw=promoted["raw"], covers=c3, approved=promoted["approved"]))
    subject = ledger["subjects"]["skill:note"]
    assert [row["outcome"] for row in subject["trials"]] == ["helped", "helped", "hurt"]
    # The only approved text is the failing one, so there is nothing to restore. It holds and flags.
    assert subject["status"] == "demoted"
    assert (subject["decision"]["action"], subject["decision"]["code"]) == ("hold", "no_approved_target")
    assert subject["demoted"] == [subject["current"]["fingerprint"]] and subject["restore"] is None


def test_approved_entries_are_append_only(world, promoted):
    changed = dict(promoted["approved"][0], skill_md_sha256="0" * 64)
    raw = world.build(prev_raw=promoted["genesis"], covers=promoted["c2"], approved=(changed,))
    world.propose(raw, promoted["c2"], name="rewrite")
    errors = world.verify(raw, promoted["genesis"], head="rewrite")
    assert any("appended" in error for error in errors), errors


def test_a_release_entry_must_be_a_recorded_release_text(world, promoted):
    worse = world.commit({SUBJECT: WORSE}, T % (23, "08:00:00Z"), branch="main")
    unreleased = world.approved_row(worse)
    raw = world.build(prev_raw=promoted["genesis"], covers=worse, approved=(*promoted["approved"], unreleased))
    world.propose(raw, worse, name="unreleased")
    errors = world.verify(raw, promoted["genesis"], head="unreleased")
    assert any("not a recorded release text" in error for error in errors), errors


def owner_pr(world, *, login="solomonneas", state="APPROVED", commit="head1", head="head1", number=7):
    world.gh.pulls[number] = {
        "head": head,
        "reviews": [{"user": {"login": login}, "state": state, "commit_id": commit}],
    }


@pytest.fixture()
def owner_case(world, promoted):
    worse = world.commit({SUBJECT: WORSE}, T % (23, "08:00:00Z"), branch="main")
    row = world.approved_row(worse, "owner_review", pr=7)
    raw = world.build(prev_raw=promoted["genesis"], covers=worse, approved=(*promoted["approved"], row))
    world.propose(raw, worse, name="approval")
    return {"raw": raw, "row": row, "worse": worse, "genesis": promoted["genesis"], "approved": promoted["approved"]}


def test_an_owner_review_on_the_head_commit_approves_new_text(world, owner_case):
    owner_pr(world, head="a" * 40, commit="a" * 40)
    assert world.verify(owner_case["raw"], owner_case["genesis"], head="approval") == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"login": "someone-else"},
        {"state": "COMMENTED"},
        {"state": "CHANGES_REQUESTED"},
        {"commit": "b" * 40},
    ],
    ids=["not-an-owner", "comment-only", "changes-requested", "older-commit"],
)
def test_new_text_is_not_approved_without_an_owner_review_of_the_head_commit(world, owner_case, kwargs):
    owner_pr(world, head="a" * 40, commit=kwargs.pop("commit", "a" * 40), **kwargs)
    errors = world.verify(owner_case["raw"], owner_case["genesis"], head="approval")
    assert any("owner approval" in error for error in errors), errors


def test_a_later_change_request_from_the_owner_cancels_the_approval(world, owner_case):
    owner_pr(world, head="a" * 40, commit="a" * 40)
    world.gh.pulls[7]["reviews"].append(
        {"user": {"login": "SolomonNeas"}, "state": "CHANGES_REQUESTED", "commit_id": "a" * 40}
    )
    assert any(
        "owner approval" in error for error in world.verify(owner_case["raw"], owner_case["genesis"], head="approval")
    )


def test_an_owner_entry_must_name_the_pull_request_that_carries_it(world, owner_case):
    owner_pr(world, head="a" * 40, commit="a" * 40)
    errors = world.verify(owner_case["raw"], owner_case["genesis"], head="approval", pr=99)
    assert any("this pull request" in error for error in errors), errors


# ---------------------------------------------------------------------------
# What a ledger change may touch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "scripts/ledger_recompute.py",
        "scripts/anything.py",
        "src/brigade/outcome_ledger.py",
        "src/brigade/outcome_ledger_actions.py",
        ".github/workflows/ci.yml",
        "verify/manifests/skill-note.json",
        f"{SKILL_DIR}/SKILL.md",
        "README.md",
    ],
)
def test_a_ledger_change_may_not_touch_anything_else(world, promoted, path):
    world.git("checkout", "-q", "main")
    head = world.propose(promoted["raw"], promoted["c2"], name="mixed", extra={path: "changed\n"})
    errors = world.verify(promoted["raw"], promoted["genesis"], head="mixed")
    assert any(f"may not touch {path}" in error for error in errors), errors
    assert head


def regression(world, promoted):
    """main: promoted history plus a worse SKILL.md whose attest run failed."""
    worse = world.commit({SUBJECT: WORSE}, T % (23, "08:00:00Z"), branch="main")
    world.gh.failed(worse, done=T % (23, "08:10:00Z"))
    raw = world.build(prev_raw=promoted["genesis"], covers=worse, approved=promoted["approved"])
    return worse, raw


def test_a_regression_restores_the_approved_text_and_the_restore_is_verified_byte_for_byte(world, promoted):
    worse, raw = regression(world, promoted)
    subject = json.loads(raw)["subjects"]["skill:note"]
    assert subject["decision"]["code"] == "regression_restore" and subject["decision"]["action"] == "rollback"
    assert subject["restore"] == {
        "from_commit": worse,
        "from_fingerprint": subject["current"]["fingerprint"],
        "to_commit": world.c0,
        "to_fingerprint": promoted["approved"][0]["fingerprint"],
    }
    restored = {f"{SKILL_DIR}/{name}": data.decode() for name, data in world.repo.snapshot(world.c0, SKILL_DIR).items()}
    world.propose(raw, worse, name="restore", extra=restored)
    assert world.verify(raw, promoted["genesis"], head="restore") == []


def test_a_restore_that_leaves_the_bad_text_or_changes_one_byte_fails(world, promoted):
    worse, raw = regression(world, promoted)
    world.propose(raw, worse, name="no-restore")
    errors = world.verify(raw, promoted["genesis"], head="no-restore")
    assert any("restored byte for byte" in error for error in errors), errors
    restored = {f"{SKILL_DIR}/{name}": data.decode() for name, data in world.repo.snapshot(world.c0, SKILL_DIR).items()}
    restored[f"{SKILL_DIR}/skill.json"] += " "
    world.propose(raw, worse, name="one-byte", extra=restored)
    errors = world.verify(raw, promoted["genesis"], head="one-byte")
    assert any("restored byte for byte" in error for error in errors), errors


def test_a_restore_may_still_touch_nothing_outside_the_bundle(world, promoted):
    worse, raw = regression(world, promoted)
    restored = {f"{SKILL_DIR}/{name}": data.decode() for name, data in world.repo.snapshot(world.c0, SKILL_DIR).items()}
    world.propose(raw, worse, name="restore-plus", extra={**restored, "scripts/ledger_recompute.py": "x\n"})
    errors = world.verify(raw, promoted["genesis"], head="restore-plus")
    assert any("may not touch scripts/ledger_recompute.py" in error for error in errors), errors


def test_a_ledger_with_no_restore_may_not_touch_the_bundle(world, promoted):
    world.propose(promoted["raw"], promoted["c2"], name="sneaky", extra={f"{SKILL_DIR}/SKILL.md": WORSE})
    errors = world.verify(promoted["raw"], promoted["genesis"], head="sneaky")
    assert any(f"may not touch {SUBJECT}" in error for error in errors), errors


def test_failing_to_read_evidence_fails_closed(world, promoted):
    def broken(_endpoint: str) -> Any:
        raise RuntimeError("network")

    world.ev = L.Evidence(world.repo, broken, SLUG, REPO_ID)
    # Stale check reads the Actions API only when a newer scope-touching commit exists.
    world.touch_skill_json(3, T % (23, "12:00:00Z"))
    errors = world.verify(promoted["raw"], promoted["genesis"])
    assert errors and any("could not finish" in error for error in errors)


@pytest.mark.parametrize(
    "raw",
    [b"", b"[]", b'{"schema": "other"}', b'{"schema": "brigade.outcome_ledger.v1", "schema": "x"}'],
)
def test_parse_rejects_malformed_ledgers(raw):
    with pytest.raises(L.LedgerError):
        L.parse_ledger(raw)


def test_module_sizes_stay_under_the_limit():
    for name in ("outcome_ledger", "outcome_ledger_actions", "outcome_ledger_git", "outcome_ledger_trials"):
        assert len((ROOT / f"src/brigade/{name}.py").read_text().splitlines()) < 2000
