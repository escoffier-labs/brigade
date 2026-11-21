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


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_workflow_repository_guards_accept_both_names(workflow):
    # Any comparison against a repository slug (==, !=, contains(...)) must name both repositories.
    for line in workflow.read_text().splitlines():
        if _COMPARISON.search(line) and _SLUG.search(line):
            assert OLD in line and NEW in line, f"{workflow.name} compares one repository only: {line.strip()}"


@pytest.mark.parametrize(
    "line",
    [
        "if: github.repository != 'escoffier-labs/brigade'",
        "if: contains(fromJSON('[\"escoffier-labs/brigade\"]'), github.repository)",
        "if: github.event.repository.full_name == 'escoffier-labs/brigade'",
    ],
)
def test_guard_scan_would_catch_a_single_name_guard_in_any_form(line):
    assert _COMPARISON.search(line) and _SLUG.search(line)


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


def test_npm_publish_attestation_follows_the_repository_the_release_manifest_records():
    text = (WORKFLOWS / "npm-publish.yml").read_text()
    assert "--print-repository" in text
    assert '--repo "$repository" --signer-workflow "$repository/.github/workflows/publish.yml"' in text


def test_revalidate_workflow_accepts_either_name_but_binds_the_run_to_the_current_repository():
    text = (WORKFLOWS / "npm-registry-revalidate.yml").read_text()
    assert "os.environ['GITHUB_REPOSITORY'] in {'escoffier-labs/brigade', 'escoffier-labs/brigadestack'}" in text
    assert "os.environ['GITHUB_REPOSITORY'] + '/.github/workflows/npm-registry-revalidate.yml@'" in text
    assert 'gh api "repos/$GITHUB_REPOSITORY/actions/runs/' in text
    assert "== os.environ['GITHUB_REPOSITORY']" in text


def test_fleet_hub_containerfile_warns_that_the_archive_digest_changes_with_the_rename():
    text = (ROOT / "ops/fleet-hub/Containerfile").read_text()
    assert "needs a new BRIGADE_SHA256" in text
    # The pins themselves are untouched by the rename work.
    assert re.search(
        r"^ARG BRIGADE_SHA256=5a0cd597e970d17eedf3597cfd33d1a909c1e8f33f9077184abb421cedf3e67f$", text, re.M
    )


# `brigade bug --submit` talks to GitHub search and issue creation, which do not follow a rename.


def test_bug_cmd_accepts_the_same_two_names():
    from brigade import bug_cmd

    assert bug_cmd.ACCEPTED_REPOSITORIES == (OLD, NEW)
    assert (bug_cmd.REPOSITORY, bug_cmd.RENAMED_REPOSITORY) == (update_cmd.REPOSITORY, update_cmd.RENAMED_REPOSITORY)


@pytest.mark.parametrize(
    ("reported", "expected"),
    [(OLD, OLD), (NEW, NEW), ("attacker/brigade", OLD), ("", OLD), ("escoffier-labs/brigade-hub", OLD)],
)
def test_current_repository_asks_rest_and_accepts_only_the_two_names(monkeypatch, reported, expected):
    from brigade import bug_cmd

    calls = []

    def fake_gh(*args):
        calls.append(args)
        return reported

    monkeypatch.setattr(bug_cmd, "gh", fake_gh)

    assert bug_cmd.current_repository() == expected
    assert calls == [("api", f"repos/{OLD}", "--jq", ".full_name")]


@pytest.mark.parametrize("error", [OSError("gh unavailable"), __import__("subprocess").SubprocessError("boom")])
def test_current_repository_falls_back_to_the_old_name_when_the_lookup_fails(monkeypatch, error):
    from brigade import bug_cmd

    def failing_gh(*args):
        raise error

    monkeypatch.setattr(bug_cmd, "gh", failing_gh)

    assert bug_cmd.current_repository() == OLD


def test_bug_search_and_submission_target_the_resolved_repository(monkeypatch):
    from brigade import bug_cmd

    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "api":
            return NEW
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
    assert not any(args[:2] == ("api", f"repos/{NEW}") for args in calls)


def test_bug_submission_still_reads_back_an_old_name_issue_url(monkeypatch):
    from brigade import bug_cmd

    monkeypatch.setattr(
        bug_cmd,
        "gh",
        lambda *args: OLD if args[0] == "api" else f"https://github.com/{OLD}/issues/9",
    )

    assert bug_cmd._submit("gh", {"user": {"title": "t"}}, "body", None) == f"https://github.com/{OLD}/issues/9"
