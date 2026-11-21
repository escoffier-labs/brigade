"""CI observation for scoreable trials (#2779). Display only.

The host can show that CI was seen passing for a trial. State on the host is forgeable
by a same-user agent, so that answer must never promote, rank, band or gate anything.
``gh api`` is always mocked.
"""

from __future__ import annotations

import copy
import datetime as dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brigade import ci_attestation, outcome as outcome_core, scorecard, verify_trial
from brigade.ci_attestation import GhNotFound, GhUnavailable

from tests.test_scorecard import (
    _effectiveness_command,
    _fixture_binding,
    _write_verify_receipt,
)
from tests.test_verify_trial import _write_skill_subject

pytest_plugins = ["tests.test_verify_trial"]

SLUG = "acme/widgets"
HEAD = "a" * 40
BRANCH = "feat/trial"
MANIFEST = "scorecard-skill-fixture"
NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
WORKFLOW_PATH = ".github/workflows/outcome-attest.yml"


@pytest.fixture(autouse=True)
def _observation_enabled(monkeypatch):
    monkeypatch.delenv(ci_attestation.DISABLE_ENV, raising=False)


@pytest.fixture
def merged_repo(scoreable_target: Path) -> tuple[Path, str]:
    """A workspace whose origin is a GitHub repo and whose HEAD is the merged commit on origin/main."""
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://github.com/{SLUG}.git"], cwd=scoreable_target, check=True
    )
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=scoreable_target, check=True)
    merge = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=scoreable_target, check=True, capture_output=True, text=True
    ).stdout.strip()
    return scoreable_target, merge


def _world(merge: str) -> dict[str, Any]:
    """The GitHub API as it looks when the Actions check run passed on the merged commit."""
    return {
        f"repos/{SLUG}/commits/{HEAD}/pulls": [
            {"number": 7, "merged_at": "2026-11-20T00:00:00Z", "merge_commit_sha": merge, "base": {"ref": "main"}}
        ],
        f"repos/{SLUG}/commits/{merge}/check-runs": {
            "check_runs": [
                {
                    "name": f"outcome-attest/{MANIFEST}",
                    "app": {"slug": "github-actions"},
                    "head_sha": merge,
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
    }


class FakeGh:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, endpoint: str) -> Any:
        self.calls.append(endpoint)
        for prefix, payload in self.routes.items():
            if endpoint.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return copy.deepcopy(payload)
        raise GhNotFound(endpoint)


def _receipt() -> dict[str, Any]:
    return {
        "subject_binding": {"manifest_binding": {"manifest_id": MANIFEST}},
        "git": {"head": HEAD, "branch": BRANCH, "dirty_files": 0},
    }


def _observed(target: Path, routes: dict[str, Any]) -> tuple[bool, FakeGh]:
    gh = FakeGh(routes)
    return ci_attestation.CiObserver(target, gh_api=gh).observed(_receipt()), gh


def _check(routes: dict[str, Any], merge: str) -> dict[str, Any]:
    return routes[f"repos/{SLUG}/commits/{merge}/check-runs"]["check_runs"][0]


def test_a_passing_actions_check_run_on_the_merged_commit_is_observed(merged_repo):
    target, merge = merged_repo
    seen, gh = _observed(target, _world(merge))
    assert seen is True
    assert not any("statuses" in call for call in gh.calls), "commit statuses are never read"


@pytest.mark.parametrize(
    ("changes", "label"),
    [
        ({"app": {"slug": "some-other-app"}}, "other_app"),
        ({"app": {}}, "no_app"),
        ({"head_sha": "c" * 40}, "other_head_sha"),
        ({"conclusion": "failure"}, "failed"),
        ({"status": "in_progress", "conclusion": None}, "not_completed"),
        ({"name": "outcome-attest/another-manifest"}, "other_manifest"),
    ],
)
def test_a_check_run_that_is_not_a_matching_actions_pass_is_not_observed(merged_repo, changes, label):
    target, merge = merged_repo
    routes = _world(merge)
    _check(routes, merge).update(changes)
    assert _observed(target, routes)[0] is False, label


@pytest.mark.parametrize(
    "change",
    [
        lambda pr: pr.update(merged_at=None),
        lambda pr: pr.update(base={"ref": "release"}),
        lambda pr: pr.update(merge_commit_sha="short"),
    ],
)
def test_a_pull_request_that_is_not_merged_into_main_is_not_observed(merged_repo, change):
    target, merge = merged_repo
    routes = _world(merge)
    change(routes[f"repos/{SLUG}/commits/{HEAD}/pulls"][0])
    assert _observed(target, routes)[0] is False


@pytest.mark.parametrize("claims", ["no_git", "no_manifest", "short_head"])
def test_a_receipt_without_usable_claims_never_calls_the_api(merged_repo, claims):
    target, merge = merged_repo
    receipt = _receipt()
    if claims == "no_git":
        del receipt["git"]
    elif claims == "no_manifest":
        receipt["subject_binding"] = {}
    else:
        receipt["git"]["head"] = "abc123"
    gh = FakeGh(_world(merge))
    assert ci_attestation.CiObserver(target, gh_api=gh).observed(receipt) is False
    assert gh.calls == []


def test_a_workspace_without_a_github_origin_never_calls_the_api(scoreable_target):
    gh = FakeGh({})
    assert ci_attestation.CiObserver(scoreable_target, gh_api=gh).observed(_receipt()) is False
    assert gh.calls == []


def test_the_disable_switch_skips_the_api(merged_repo, monkeypatch):
    target, merge = merged_repo
    monkeypatch.setenv(ci_attestation.DISABLE_ENV, "off")
    seen, gh = _observed(target, _world(merge))
    assert (seen, gh.calls) == (False, [])


def test_offline_or_a_broken_gh_is_not_observed_and_stops_calling_the_api(merged_repo):
    target, _ = merged_repo
    gh = FakeGh({f"repos/{SLUG}/": GhUnavailable("no network")})
    observer = ci_attestation.CiObserver(target, gh_api=gh)
    assert observer.observed(_receipt()) is False
    calls_after_first = len(gh.calls)
    assert observer.observed({**_receipt(), "git": {"head": "e" * 40, "branch": BRANCH}}) is False
    assert len(gh.calls) == calls_after_first, "after one transport failure the run stops calling the API"


def test_a_missing_commit_is_not_observed(merged_repo):
    target, _ = merged_repo
    assert _observed(target, {})[0] is False


def test_an_unexpected_exception_in_the_transport_is_swallowed(merged_repo):
    target, _ = merged_repo

    def boom(_endpoint: str) -> Any:
        raise RuntimeError("surprise")

    assert ci_attestation.CiObserver(target, gh_api=boom).observed(_receipt()) is False


def test_the_answer_is_memoised_per_commit_and_manifest(merged_repo):
    target, merge = merged_repo
    gh = FakeGh(_world(merge))
    observer = ci_attestation.CiObserver(target, gh_api=gh)
    assert observer.observed(_receipt()) is True
    calls = len(gh.calls)
    assert observer.observed(_receipt()) is True
    assert len(gh.calls) == calls


def test_a_spent_call_budget_stops_lookups_for_the_rest_of_the_build(merged_repo):
    target, merge = merged_repo
    gh = FakeGh(_world(merge))
    observer = ci_attestation.CiObserver(target, gh_api=gh, max_calls=1)
    assert observer.observed(_receipt()) is False, "the pull request lookup spends the only call"
    assert len(gh.calls) == 1
    assert observer.observed({**_receipt(), "git": {"head": "e" * 40, "branch": BRANCH}}) is False
    assert len(gh.calls) == 1, "an exhausted budget reads as offline"


def test_a_spent_time_budget_stops_lookups_even_when_each_call_succeeds(merged_repo):
    target, merge = merged_repo
    now = [0.0]

    class SlowGh(FakeGh):
        def __call__(self, endpoint: str) -> Any:
            now[0] += 40.0
            return super().__call__(endpoint)

    gh = SlowGh(_world(merge))
    observer = ci_attestation.CiObserver(target, gh_api=gh, budget_seconds=60.0, clock=lambda: now[0])
    assert observer.observed(_receipt()) is True
    calls = len(gh.calls)
    assert observer.observed({**_receipt(), "git": {"head": "e" * 40, "branch": BRANCH}}) is False
    assert len(gh.calls) == calls, "no call starts after the build-wide deadline"


def test_default_gh_api_fails_closed_without_gh(monkeypatch):
    def missing(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(ci_attestation.subprocess, "run", missing)
    with pytest.raises(GhUnavailable):
        ci_attestation.default_gh_api("repos/acme/widgets/commits/x/pulls")


def test_default_gh_api_maps_404_and_bad_json(monkeypatch):
    def run_with(returncode: int, stdout: str, stderr: str):
        return lambda *_a, **_k: subprocess.CompletedProcess([], returncode, stdout, stderr)

    monkeypatch.setattr(ci_attestation.subprocess, "run", run_with(1, "", "gh: Not Found (HTTP 404)"))
    with pytest.raises(GhNotFound):
        ci_attestation.default_gh_api("repos/a/b/commits/x/pulls")
    monkeypatch.setattr(ci_attestation.subprocess, "run", run_with(0, "not json", ""))
    with pytest.raises(GhUnavailable):
        ci_attestation.default_gh_api("repos/a/b/commits/x/pulls")


def _eligible_local_trial(target: Path, run_id: str = "run-a", commands: list[Any] | None = None) -> None:
    skill = _write_skill_subject(target)
    fingerprint = verify_trial.subject_hash_for_path(target, str(skill.relative_to(target)))
    _write_verify_receipt(
        target,
        run_id,
        binding=_fixture_binding(fingerprint=fingerprint, target=target),
        commands=[_effectiveness_command()] if commands is None else commands,
        extra={"git": {"head": HEAD, "branch": BRANCH, "dirty_files": 0}},
    )


def _build_card(target: Path, monkeypatch, gh: Any) -> scorecard.SubjectScorecard:
    monkeypatch.setattr(ci_attestation, "default_gh_api", gh)
    (card,) = scorecard.build_scorecards(target)
    return card


def test_a_ci_observed_trial_is_displayed_and_stays_local(merged_repo, monkeypatch):
    target, merge = merged_repo
    _eligible_local_trial(target)
    card = _build_card(target, monkeypatch, FakeGh(_world(merge)))
    effectiveness = card.dimensions["effectiveness"]
    assert (effectiveness["local_trials"], effectiveness["attested_trials"]) == (1, 0)
    assert card.ci_observed_trials == 1
    assert [(audit.attestation, audit.ci_observed) for audit in card.receipt_trail] == [("local", True)]


def test_a_ci_observed_trial_gets_no_promotion_credit_it_did_not_already_have(merged_repo, monkeypatch):
    target, merge = merged_repo
    _eligible_local_trial(target)
    seen = _build_card(target, monkeypatch, FakeGh(_world(merge)))
    unseen = _build_card(target, monkeypatch, FakeGh({f"repos/{SLUG}/": GhUnavailable("offline")}))
    assert (seen.ci_observed_trials, unseen.ci_observed_trials) == (1, 0)
    # Every input a promotion decision reads is identical with and without the CI result.
    assert seen.dimensions == unseen.dimensions
    assert seen.utility_guardrails == unseen.utility_guardrails
    assert seen.ineligible_summary == unseen.ineligible_summary
    assert scorecard.classify_band(seen) == scorecard.classify_band(unseen)
    assert scorecard.utility_gate_reason(seen) == scorecard.utility_gate_reason(unseen)
    assert [a.attestation for a in seen.receipt_trail] == [a.attestation for a in unseen.receipt_trail]
    config = outcome_core.ReconcileConfig()
    decisions = [
        scorecard.decide_scorecard(
            card,
            artifact_id=card.subject.artifact_id,
            current_status="candidate",
            last_action_ts=None,
            now=NOW,
            config=config,
        )
        for card in (seen, unseen)
    ]
    assert decisions[0] == decisions[1]
    # The persisted scorecard form carries no CI field either.
    assert scorecard.subject_scorecard_to_dict(seen) == scorecard.subject_scorecard_to_dict(unseen)


def test_an_ineligible_trial_is_never_looked_up_in_ci(merged_repo, monkeypatch):
    target, merge = merged_repo
    _eligible_local_trial(target, run_id="run-b", commands=[])  # no effectiveness command: not scoreable
    gh = FakeGh(_world(merge))
    monkeypatch.setattr(ci_attestation, "default_gh_api", gh)
    audits = scorecard.audit_all_verify_receipts(target)
    assert all(audit.attestation == "local" and audit.ci_observed is False for audit in audits)
    assert gh.calls == []


def test_the_workflow_matches_the_contract_the_reader_checks():
    root = Path(__file__).resolve().parents[1]
    text = (root / WORKFLOW_PATH).read_text()
    assert "pull_request_target" not in text
    assert "pull_request:" not in text
    assert "branches: [main]" in text
    assert "permissions:\n  contents: read\n" in text
    assert "secrets." not in text
    for line in text.splitlines():
        if "uses:" in line:
            ref = line.split("uses:", 1)[1].split("#", 1)[0].strip().rsplit("@", 1)[1]
            assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), line
    assert f"name: {ci_attestation.ATTEST_CHECK_PREFIX}${{{{ matrix.manifest }}}}" in text
    assert 'brigade work verify run --target . --manifest "$MANIFEST_ID"' in text
