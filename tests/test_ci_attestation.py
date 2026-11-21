"""CI attestation for scoreable trials (#2779).

Local session state may choose which trial is looked up and may veto, but the pass
must come from a GitHub Actions check run on ``main``. ``gh api`` is always mocked.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brigade import ci_attestation, scorecard, verify_trial
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


@pytest.fixture(autouse=True)
def _attestation_enabled(monkeypatch):
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
    """The GitHub API as it looks when CI really attested the merged commit."""
    return {
        f"repos/{SLUG}/commits/{HEAD}/pulls": [
            {
                "number": 7,
                "merged_at": "2026-11-20T00:00:00Z",
                "merge_commit_sha": merge,
                "commits": 2,
                "base": {"ref": "main"},
                "head": {"ref": BRANCH, "repo": {"full_name": SLUG}},
            }
        ],
        f"repos/{SLUG}/pulls/7/commits": [{"sha": "b" * 40}, {"sha": HEAD}],
        f"repos/{SLUG}/commits/{merge}/check-runs": {
            "check_runs": [
                {
                    "name": f"outcome-attest/{MANIFEST}",
                    "app": {"slug": "github-actions"},
                    "head_sha": merge,
                    "status": "completed",
                    "conclusion": "success",
                    "check_suite": {"id": 99},
                }
            ]
        },
        f"repos/{SLUG}/actions/runs": {
            "workflow_runs": [
                {
                    "check_suite_id": 99,
                    "path": ".github/workflows/outcome-attest.yml",
                    "head_branch": "main",
                    "event": "push",
                    "head_sha": merge,
                    "head_repository": {"full_name": SLUG},
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


def _attest(target: Path, routes: dict[str, Any], **kwargs: Any) -> tuple[ci_attestation.Attestation, FakeGh]:
    gh = FakeGh(routes)
    return ci_attestation.Attestor(target, gh_api=gh, now=lambda: NOW).attest(_receipt()), gh


def test_a_passing_attestor_check_run_on_the_merged_commit_attests(merged_repo):
    target, merge = merged_repo
    result, gh = _attest(target, _world(merge))
    assert result == ci_attestation.Attestation(True, "attested", merge)
    assert not any("status" in call.split("?")[0].rsplit("/", 1)[-1] for call in gh.calls)


def test_a_forged_commit_status_is_ignored_entirely(merged_repo):
    target, merge = merged_repo
    routes = _world(merge)
    routes[f"repos/{SLUG}/commits/{merge}/check-runs"] = {"check_runs": []}
    # A personal token can write this. It must never be read.
    routes[f"repos/{SLUG}/commits/{merge}/status"] = {"state": "success", "statuses": [{"context": "outcome-attest"}]}
    routes[f"repos/{SLUG}/commits/{merge}/statuses"] = [{"state": "success", "context": f"outcome-attest/{MANIFEST}"}]
    result, gh = _attest(target, routes)
    assert not result.attested
    assert result.reason == "no_check_run"
    assert not any(call.split("?")[0].endswith(("/status", "/statuses")) for call in gh.calls)


def _mutate(routes: dict[str, Any], merge: str, section: str, **changes: Any) -> None:
    key = {
        "check": f"repos/{SLUG}/commits/{merge}/check-runs",
        "run": f"repos/{SLUG}/actions/runs",
    }[section]
    inner = "check_runs" if section == "check" else "workflow_runs"
    routes[key][inner][0].update(changes)


@pytest.mark.parametrize(
    ("section", "changes", "reason"),
    [
        ("check", {"app": {"slug": "some-third-party-app"}}, "check_not_github_actions"),
        ("check", {"app": {"slug": "github-actions-lookalike"}}, "check_not_github_actions"),
        ("check", {"conclusion": "failure"}, "check_not_success"),
        ("check", {"status": "in_progress", "conclusion": None}, "check_not_success"),
        ("check", {"head_sha": "c" * 40}, "check_not_github_actions"),
        ("run", {"head_branch": "feature/evil"}, "run_not_attestor"),
        ("run", {"event": "pull_request"}, "run_not_attestor"),
        ("run", {"path": ".github/workflows/ci.yml"}, "run_not_attestor"),
        ("run", {"check_suite_id": 12345}, "run_not_attestor"),
        ("run", {"head_repository": {"full_name": "fork/widgets"}}, "run_not_attestor"),
    ],
)
def test_a_check_run_that_is_not_from_the_main_attestor_workflow_does_not_attest(merged_repo, section, changes, reason):
    target, merge = merged_repo
    routes = _world(merge)
    _mutate(routes, merge, section, **changes)
    result, _ = _attest(target, routes)
    assert (result.attested, result.reason) == (False, reason)


def test_a_check_run_for_a_different_manifest_does_not_attest(merged_repo):
    target, merge = merged_repo
    routes = _world(merge)
    _mutate(routes, merge, "check", name="outcome-attest/some-other-manifest")
    assert _attest(target, routes)[0].reason == "no_check_run"


def test_a_path_with_a_ref_suffix_still_names_the_attestor_workflow(merged_repo):
    target, merge = merged_repo
    routes = _world(merge)
    _mutate(routes, merge, "run", path=".github/workflows/outcome-attest.yml@refs/heads/main")
    assert _attest(target, routes)[0].attested


@pytest.mark.parametrize(
    "change",
    [
        {"merged_at": None},
        {"base": {"ref": "release"}},
        {"head": {"ref": "some-other-branch", "repo": {"full_name": SLUG}}},
        {"head": {"ref": BRANCH, "repo": {"full_name": "fork/widgets"}}},
        {"merge_commit_sha": HEAD},
        {"commits": 5000},
    ],
)
def test_a_local_receipt_that_does_not_map_to_a_merged_main_pr_stays_local(merged_repo, change):
    target, merge = merged_repo
    routes = _world(merge)
    routes[f"repos/{SLUG}/commits/{HEAD}/pulls"][0].update(change)
    assert not _attest(target, routes)[0].attested


def test_a_head_that_is_not_a_commit_of_the_pr_stays_local(merged_repo):
    """A baseline commit already on main is returned by the API for an old PR. It must not map."""
    target, merge = merged_repo
    routes = _world(merge)
    routes[f"repos/{SLUG}/pulls/7/commits"] = [{"sha": "b" * 40}]
    assert _attest(target, routes)[0].reason == "no_merged_pr"


@pytest.mark.parametrize("branch", ["main", "HEAD"])
def test_a_receipt_from_main_or_a_detached_head_is_never_mapped(merged_repo, branch):
    target, merge = merged_repo
    receipt = _receipt()
    receipt["git"]["branch"] = branch
    result = ci_attestation.Attestor(target, gh_api=FakeGh(_world(merge)), now=lambda: NOW).attest(receipt)
    assert not result.attested


def test_a_merge_commit_that_is_not_on_origin_main_is_vetoed_locally(merged_repo):
    target, _ = merged_repo
    result, _ = _attest(target, _world("d" * 40))
    assert (result.attested, result.reason) == (False, "commit_not_on_origin_main")


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
    assert not ci_attestation.Attestor(target, gh_api=gh, now=lambda: NOW).attest(receipt).attested
    assert gh.calls == []


def test_a_workspace_without_a_github_origin_never_calls_the_api(scoreable_target):
    gh = FakeGh({})
    assert not ci_attestation.Attestor(scoreable_target, gh_api=gh).attest(_receipt()).attested
    assert gh.calls == []


def test_the_disable_switch_skips_the_api(merged_repo, monkeypatch):
    target, merge = merged_repo
    monkeypatch.setenv(ci_attestation.DISABLE_ENV, "off")
    result, gh = _attest(target, _world(merge))
    assert (result.attested, result.reason, gh.calls) == (False, "disabled", [])


def test_offline_or_a_broken_gh_means_not_attested_and_never_raises(merged_repo):
    target, merge = merged_repo
    routes = {f"repos/{SLUG}/": GhUnavailable("no network")}
    result, gh = _attest(target, routes)
    assert (result.attested, result.reason) == (False, "offline")
    assert not (target / ci_attestation.CACHE_RELATIVE_PATH).exists(), "an outage must not be cached as a verdict"
    attestor = ci_attestation.Attestor(target, gh_api=gh, now=lambda: NOW)
    attestor.attest(_receipt())
    calls_after_first = len(gh.calls)
    attestor.attest({**_receipt(), "git": {"head": "e" * 40, "branch": BRANCH}})
    assert len(gh.calls) == calls_after_first, "after one transport failure the run stops calling the API"


def test_an_unexpected_exception_in_the_transport_is_swallowed(merged_repo):
    target, _ = merged_repo

    def boom(_endpoint: str) -> Any:
        raise RuntimeError("surprise")

    assert not ci_attestation.Attestor(target, gh_api=boom).attest(_receipt()).attested


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


def _cache_path(target: Path) -> Path:
    return target / ci_attestation.CACHE_RELATIVE_PATH


def _cache_key() -> str:
    return f"{SLUG}|{BRANCH}|{HEAD}|{MANIFEST}"


def _write_cache(target: Path, entries: dict[str, Any]) -> None:
    path = _cache_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": ci_attestation.CACHE_VERSION, "entries": entries}))


def test_a_tampered_cache_claiming_attested_is_reconfirmed_and_rejected(merged_repo):
    target, merge = merged_repo
    _write_cache(
        target,
        {_cache_key(): {"result": "attested", "reason": "attested", "commit": merge, "checked_at": NOW.isoformat()}},
    )
    routes = _world(merge)
    routes[f"repos/{SLUG}/commits/{merge}/check-runs"] = {"check_runs": []}
    result, gh = _attest(target, routes)
    assert not result.attested
    assert gh.calls, "a cached attested must be re-confirmed from the API"


def test_a_tampered_cache_claiming_attested_cannot_authorize_when_offline(merged_repo):
    target, merge = merged_repo
    _write_cache(target, {_cache_key(): {"result": "attested", "checked_at": NOW.isoformat()}})
    result, _ = _attest(target, {f"repos/{SLUG}/": GhUnavailable("offline")})
    assert not result.attested


def test_a_forged_cache_cannot_turn_a_real_pass_into_a_different_commit(merged_repo):
    target, merge = merged_repo
    _write_cache(target, {_cache_key(): {"result": "attested", "commit": "f" * 40, "checked_at": NOW.isoformat()}})
    result, _ = _attest(target, _world(merge))
    assert result == ci_attestation.Attestation(True, "attested", merge)


def test_a_real_pass_is_never_written_to_the_cache(merged_repo):
    target, merge = merged_repo
    assert _attest(target, _world(merge))[0].attested
    assert not _cache_path(target).exists()


def test_a_negative_result_is_cached_and_short_circuits_within_the_ttl(merged_repo):
    target, merge = merged_repo
    routes = _world(merge)
    routes[f"repos/{SLUG}/commits/{merge}/check-runs"] = {"check_runs": []}
    first, gh = _attest(target, routes)
    assert first.reason == "no_check_run"
    assert json.loads(_cache_path(target).read_text())["entries"][_cache_key()]["result"] == "not_attested"
    second, gh2 = _attest(target, _world(merge))  # CI would now say yes, but the fresh negative stands
    assert (second.attested, gh2.calls) == (False, [])


def test_an_expired_negative_is_checked_again(merged_repo):
    target, merge = merged_repo
    old = NOW - dt.timedelta(seconds=ci_attestation.NEGATIVE_TTL_SECONDS + 1)
    _write_cache(
        target, {_cache_key(): {"result": "not_attested", "reason": "no_check_run", "checked_at": old.isoformat()}}
    )
    assert _attest(target, _world(merge))[0].attested


@pytest.mark.parametrize("raw", ["not json", "[]", '{"version": 9, "entries": {}}', '{"version": 1, "entries": []}'])
def test_a_corrupt_cache_is_ignored(merged_repo, raw):
    target, merge = merged_repo
    path = _cache_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    assert _attest(target, _world(merge))[0].attested


def _eligible_local_trial(target: Path) -> None:
    skill = _write_skill_subject(target)
    fingerprint = verify_trial.subject_hash_for_path(target, str(skill.relative_to(target)))
    _write_verify_receipt(
        target,
        "run-a",
        binding=_fixture_binding(fingerprint=fingerprint, target=target),
        commands=[_effectiveness_command()],
        extra={"git": {"head": HEAD, "branch": BRANCH, "dirty_files": 0}},
    )


def _trial_counts(target: Path) -> tuple[int, int]:
    (card,) = scorecard.build_scorecards(target)
    effectiveness = card.dimensions["effectiveness"]
    return effectiveness["local_trials"], effectiveness["attested_trials"]


def _forge_session_state_and_cache(target: Path, merge: str) -> None:
    """Hook state, work sessions and the attestation cache: all writable by the scored agent."""
    state = target / ".brigade" / "work"
    forged: dict[str, Any] = {
        "sessions/forged.json": {"session_id": "forged", "attestation": "attested", "attested": True},
        "claude-hooks/forged.json": {"producer": "claude-hook:forged", "attestation": "attested"},
    }
    for name, payload in forged.items():
        path = state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    _write_cache(
        target,
        {_cache_key(): {"result": "attested", "reason": "attested", "commit": merge, "checked_at": NOW.isoformat()}},
    )


def _forge_receipt(target: Path) -> None:
    receipt_path = target / ".brigade" / "work" / "verify-runs" / "run-a" / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["attestation"] = "attested"
    receipt["subject_binding"]["attestation"] = "attested"
    receipt_path.write_text(json.dumps(receipt))


def test_editing_local_state_alone_cannot_attest_a_local_trial(merged_repo, monkeypatch):
    target, merge = merged_repo
    _eligible_local_trial(target)
    # CI never attested this commit: the only check run on it came from another app.
    world = _world(merge)
    world[f"repos/{SLUG}/commits/{merge}/check-runs"]["check_runs"][0]["app"] = {"slug": "evil-app"}
    monkeypatch.setattr(ci_attestation, "default_gh_api", FakeGh(world))
    assert _trial_counts(target) == (1, 0)

    _forge_session_state_and_cache(target, merge)
    assert _trial_counts(target) == (1, 0), "forged sessions, hook state and cache must not attest"

    _forge_receipt(target)
    cards = scorecard.build_scorecards(target)
    assert all(card.dimensions["effectiveness"]["attested_trials"] == 0 for card in cards)

    # Offline, the same forged state still attests nothing.
    monkeypatch.setattr(ci_attestation, "default_gh_api", FakeGh({f"repos/{SLUG}/": GhUnavailable("offline")}))
    assert all(card.dimensions["effectiveness"]["attested_trials"] == 0 for card in scorecard.build_scorecards(target))


def test_only_the_ci_record_attests_the_same_trial(merged_repo, monkeypatch):
    target, merge = merged_repo
    _eligible_local_trial(target)
    monkeypatch.setattr(ci_attestation, "default_gh_api", FakeGh(_world(merge)))
    assert _trial_counts(target) == (0, 1)


def test_an_ineligible_trial_is_never_attested_even_when_ci_passed(merged_repo, monkeypatch):
    target, merge = merged_repo
    skill = _write_skill_subject(target)
    fingerprint = verify_trial.subject_hash_for_path(target, str(skill.relative_to(target)))
    _write_verify_receipt(
        target,
        "run-b",
        binding=_fixture_binding(fingerprint=fingerprint, target=target),
        commands=[],  # no effectiveness command: not scoreable
        extra={"git": {"head": HEAD, "branch": BRANCH, "dirty_files": 0}},
    )
    gh = FakeGh(_world(merge))
    monkeypatch.setattr(ci_attestation, "default_gh_api", gh)
    audits = scorecard.audit_all_verify_receipts(target)
    assert all(audit.attestation == "local" for audit in audits)
    assert gh.calls == []


def test_the_attestor_workflow_matches_the_contract_the_reader_checks():
    root = Path(__file__).resolve().parents[1]
    assert ci_attestation.ATTEST_WORKFLOW_PATH == ".github/workflows/outcome-attest.yml"
    text = (root / ci_attestation.ATTEST_WORKFLOW_PATH).read_text()
    assert "pull_request_target" not in text
    assert "pull_request:" not in text
    assert "branches: [main]" in text
    assert "permissions:\n  contents: read\n" in text
    assert "secrets." not in text
    assert f"name: {ci_attestation.ATTEST_CHECK_PREFIX}${{{{ matrix.manifest }}}}" in text
    assert 'brigade work verify run --target . --manifest "$MANIFEST_ID"' in text
