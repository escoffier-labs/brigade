"""Every check that names the repository accepts both names across the rename (#2816).

The repository is renamed from escoffier-labs/brigade to escoffier-labs/brigadestack after
0.32.0 publishes. The release before the rename and every release after it must publish and
verify, and releases attested under the old name must keep verifying.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from brigade import update_cmd

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
OLD = "escoffier-labs/brigade"
NEW = "escoffier-labs/brigadestack"
# The immutable repository ID survives the rename; a reused name carries another one.
REPOSITORY_ID = 1238165856


def _load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_script_accepts_exactly_the_two_names_update_cmd_knows():
    assert (update_cmd.REPOSITORY, update_cmd.RENAMED_REPOSITORY) == (OLD, NEW)
    expected = (OLD, NEW)
    verifier = _load("verify_component_manifest_provenance.py", "rename_verifier")
    generator = _load("generate_component_manifest.py", "rename_generator")
    acceptance = _load("published-artifact-acceptance.py", "rename_acceptance")
    assert (verifier.REPOSITORY_ID, acceptance.REPOSITORY_ID) == (REPOSITORY_ID, REPOSITORY_ID)
    assert update_cmd.REPOSITORY_ID == REPOSITORY_ID
    assert verifier.ACCEPTED_REPOSITORIES == expected
    assert generator.ACCEPTED_REPOSITORIES == expected
    assert acceptance.ACCEPTED_REPOSITORIES == expected


def test_windows_acceptance_script_accepts_the_same_two_names_and_follows_the_manifest():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    # One copy in Get-ManifestRepository and one lookup candidate list in Assert-ReleaseManifestAndAssets.
    assert text.count(f'@("{OLD}", "{NEW}")') == 2
    assert "-cne $repository" in text
    assert '"--repo", $Repository, "--signer-workflow", "$Repository/.github/workflows/publish.yml"' in text
    assert "-Repository $repository" in text
    assert "Get-ManifestRepository -Manifest $manifest" in text
    assert "Assert-UniqueManifestKeys -ManifestPath $manifestPath" in text
    assert 'if (-not $Repository) { throw "canonical attestation verification failed: repository is required" }' in text
    assert "GITHUB_REPOSITORY" not in text
    assert "$repositoryId = 1238165856" in text
    assert "Assert-RepositoryId -Repository $candidate" in text
    assert text.count("Assert-RepositoryId -Repository $repository") == 2
    assert '"--format", "json"' in text
    assert '$identifier -cne "1238165856"' in text


def test_published_acceptance_derives_the_repository_from_the_manifest_and_pins_only_on_request():
    text = (WORKFLOWS / "publish.yml").read_text()
    assert 'published-artifact-acceptance.py --brigade-version "${BRIGADE_VERSION#v}" --pin-workflow-repository' in text
    script = (ROOT / "scripts/published-artifact-acceptance.py").read_text()
    assert "GITHUB_REPOSITORY" in script
    assert "def release_repository" not in script


def test_npm_prepare_accepts_a_pinned_release_under_either_name_for_both_urls():
    text = (ROOT / "npm/feasibility/prepare.py").read_text()
    assert 'for name in ("escoffier-labs/brigade", "escoffier-labs/brigadestack")' in text
    assert 'component["checksumsUrl"] != base + "checksums.txt"' in text
    assert "github.com/escoffier-labs/brigade/releases/download" not in text


_COMPARISON = re.compile(r"==|!=|contains\(|in \{")
_SLUG = re.compile(r"""['"]escoffier-labs/brigade(?:stack)?['"]""")
GUARD = "github.repository_id == '1238165856'"


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_no_workflow_guard_trusts_a_repository_name(workflow):
    # A name is only a label: after the rename anyone in the organization can create a new repository
    # called escoffier-labs/brigade. Guards compare github.repository_id, never a slug (==, !=, contains()).
    # The revalidation workflow's python asserts name the two slugs but also pin GITHUB_REPOSITORY_ID.
    for line in workflow.read_text().splitlines():
        if "github.repository" in line and _COMPARISON.search(line):
            assert not _SLUG.search(line), f"{workflow.name} guards on a repository name: {line.strip()}"


@pytest.mark.parametrize(
    "line",
    [
        "if: github.repository != 'escoffier-labs/brigade'",
        "if: contains(fromJSON('[\"escoffier-labs/brigade\"]'), github.repository)",
        "if: github.repository == 'escoffier-labs/brigadestack'",
    ],
)
def test_guard_scan_would_catch_a_name_guard_in_any_form(line):
    assert "github.repository" in line and _COMPARISON.search(line) and _SLUG.search(line)


@pytest.mark.parametrize(
    ("workflow", "count"),
    [("brigadestack-npm-publish.yml", 2), ("download-metrics.yml", 1)],
)
def test_publication_guards_compare_the_repository_id(workflow, count):
    text = (WORKFLOWS / workflow).read_text()
    assert text.count(GUARD) == count
    assert "github.repository ==" not in text


def test_ci_workflow_repository_guard_does_not_name_a_repository():
    # ci.yml's fork guard compares the PR head repo with github.repository, so it needs no slug.
    text = (WORKFLOWS / "ci.yml").read_text()
    guard = next(line for line in text.splitlines() if "pull_request.head.repo.full_name == github.repository" in line)
    assert "escoffier-labs/" not in guard


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_workflows_do_not_pin_a_signer_workflow_or_api_path_to_one_repository(workflow):
    text = workflow.read_text()
    assert f"{OLD}/.github/workflows/" not in text
    assert f"repos/{OLD}/" not in text
    assert f"{NEW}/.github/workflows/" not in text
    assert f"repos/{NEW}/" not in text


def test_publish_gate_verifies_attestations_under_the_repository_the_workflow_ran_in():
    text = (WORKFLOWS / "publish.yml").read_text()
    gate = text[text.index("  release-asset-gate:") : text.index("  build-and-publish:")]
    assert '--expected-repository "$GITHUB_REPOSITORY"' in gate
    assert '--repo "$GITHUB_REPOSITORY"' in gate
    assert '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/publish.yml"' in gate
    # The certificate must name the repository ID, not just the name the verifier was asked about.
    assert '--format json > "$RUNNER_TEMP/attestation.json"' in gate
    assert '--check-attestation-json "$RUNNER_TEMP/attestation.json"' in gate


def test_npm_publish_attestation_follows_the_repository_the_release_manifest_records():
    text = (WORKFLOWS / "npm-publish.yml").read_text()
    assert "--print-repository" in text
    assert '--repo "$repository" --signer-workflow "$repository/.github/workflows/publish.yml"' in text
    assert '--format json > "$RUNNER_TEMP/attestation.json"' in text
    assert '--check-attestation-json "$RUNNER_TEMP/attestation.json"' in text


def test_revalidate_workflow_accepts_either_name_but_binds_the_run_to_the_current_repository():
    text = (WORKFLOWS / "npm-registry-revalidate.yml").read_text()
    assert "os.environ['GITHUB_REPOSITORY'] in {'escoffier-labs/brigade', 'escoffier-labs/brigadestack'}" in text
    assert "os.environ['GITHUB_REPOSITORY'] + '/.github/workflows/npm-registry-revalidate.yml@'" in text
    assert 'gh api "repos/$GITHUB_REPOSITORY/actions/runs/' in text
    assert "== os.environ['GITHUB_REPOSITORY']" in text
    assert "os.environ['GITHUB_REPOSITORY_ID'] == '1238165856'" in text
    assert "run['repository']['id'] == run['head_repository']['id'] == 1238165856" in text


def test_fleet_hub_containerfile_warns_that_the_archive_digest_changes_with_the_rename():
    text = (ROOT / "ops/fleet-hub/Containerfile").read_text()
    assert "needs a new BRIGADE_SHA256" in text
    # The pins themselves are untouched by the rename work.
    assert re.search(
        r"^ARG BRIGADE_SHA256=5a0cd597e970d17eedf3597cfd33d1a909c1e8f33f9077184abb421cedf3e67f$", text, re.M
    )


# `brigade bug --submit` talks to GitHub search and issue creation, which do not follow a rename.


def test_bug_cmd_accepts_the_same_two_names_and_pins_the_repository_id():
    from brigade import bug_cmd

    assert bug_cmd.ACCEPTED_REPOSITORIES == (OLD, NEW)
    assert (bug_cmd.REPOSITORY, bug_cmd.RENAMED_REPOSITORY) == (update_cmd.REPOSITORY, update_cmd.RENAMED_REPOSITORY)
    assert bug_cmd.REPOSITORY_ID == REPOSITORY_ID


@pytest.mark.parametrize(("full_name", "expected"), [(OLD, OLD), (NEW, NEW)])
def test_current_repository_returns_the_name_only_for_the_real_repository_id(monkeypatch, full_name, expected):
    from brigade import bug_cmd

    calls = []

    def fake_gh(*args):
        calls.append(args)
        return f"{REPOSITORY_ID}\t{full_name}"

    monkeypatch.setattr(bug_cmd, "gh", fake_gh)

    assert bug_cmd.current_repository() == expected
    assert calls == [("api", f"repos/{OLD}", "--jq", "[.id, .full_name] | @tsv")]


@pytest.mark.parametrize(
    "reported",
    [
        f"999\t{OLD}",  # the old name now belongs to another repository
        f"{REPOSITORY_ID + 1}\t{NEW}",
        f"{REPOSITORY_ID}\tattacker/brigade",
        f"{REPOSITORY_ID}\tescoffier-labs/brigade-hub",
        f"{REPOSITORY_ID}",
        f"\t{OLD}",
        "",
    ],
)
def test_current_repository_refuses_a_name_that_is_not_the_brigade_repository_id(monkeypatch, reported):
    from brigade import bug_cmd

    monkeypatch.setattr(bug_cmd, "gh", lambda *args: reported)

    with pytest.raises(bug_cmd.RepositoryIdentityError, match="does not resolve to the Brigade repository ID"):
        bug_cmd.current_repository()


@pytest.mark.parametrize("error", [OSError("gh unavailable"), __import__("subprocess").SubprocessError("boom")])
def test_current_repository_does_not_fall_back_to_the_old_name_when_the_lookup_fails(monkeypatch, error):
    from brigade import bug_cmd

    def failing_gh(*args):
        raise error

    monkeypatch.setattr(bug_cmd, "gh", failing_gh)

    with pytest.raises(bug_cmd.RepositoryIdentityError, match="could not confirm"):
        bug_cmd.current_repository()


def test_bug_submission_is_refused_when_the_old_name_was_reused(monkeypatch):
    from brigade import bug_cmd
    from brigade.bug_redaction import ReportBlocked

    calls = []

    def fake_gh(*args):
        calls.append(args)
        return f"999\t{OLD}" if args[0] == "api" else "https://github.com/attacker/brigade/issues/1"

    monkeypatch.setattr(bug_cmd, "gh", fake_gh)
    bundle = {"user": {"title": "Doctor fails"}}

    for comment_on in (None, 41):
        with pytest.raises(ReportBlocked, match="nothing was submitted"):
            bug_cmd._submit("gh", bundle, "body", comment_on)
    # Only the identity lookup ran: no issue was created or commented on.
    assert [args[0] for args in calls] == ["api", "api"]


def test_bug_duplicate_search_is_skipped_rather_than_sent_to_a_reused_name(monkeypatch):
    from brigade import bug_cmd

    calls = []

    def fake_gh(*args):
        calls.append(args)
        return f"999\t{OLD}" if args[0] == "api" else "[]"

    monkeypatch.setattr(bug_cmd, "gh", fake_gh)
    monkeypatch.setattr(bug_cmd, "urlopen", lambda *a, **k: pytest.fail("search reached the network"))

    found, status, exact = bug_cmd._duplicates({"fingerprint": "abc", "last_error": None}, object())

    assert (found, status, exact) == ([], "dedup skipped", False)
    assert [args[0] for args in calls] == ["api"]


def test_bug_search_and_submission_target_the_verified_name(monkeypatch):
    from brigade import bug_cmd

    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "api":
            return f"{REPOSITORY_ID}\t{NEW}"
        if args[:2] == ("search", "issues"):
            return "[]"
        if args[:2] == ("issue", "create"):
            return f"https://github.com/{NEW}/issues/7"
        return ""

    monkeypatch.setattr(bug_cmd, "gh", fake_gh)
    bundle = {"user": {"title": "Doctor fails"}}

    assert bug_cmd._submit("gh", bundle, "body", None) == f"https://github.com/{NEW}/issues/7"
    assert bug_cmd._submit("gh", bundle, "body", 41) == f"https://github.com/{NEW}/issues/41"
    bug_cmd._search_issues("query", NEW)

    targeted = [args[args.index("--repo") + 1] for args in calls if "--repo" in args]
    assert targeted == [NEW, NEW, NEW]


def test_bug_submission_still_reads_back_an_old_name_issue_url(monkeypatch):
    from brigade import bug_cmd

    monkeypatch.setattr(
        bug_cmd,
        "gh",
        lambda *args: f"{REPOSITORY_ID}\t{OLD}" if args[0] == "api" else f"https://github.com/{OLD}/issues/9",
    )

    assert bug_cmd._submit("gh", {"user": {"title": "t"}}, "body", None) == f"https://github.com/{OLD}/issues/9"
