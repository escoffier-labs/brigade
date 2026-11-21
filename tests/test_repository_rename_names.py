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
    # Both functions carry their own copy so the test harnesses can extract each one alone.
    assert text.count(f'$acceptedRepositories = @("{OLD}", "{NEW}")') == 2
    assert "-cne $repository" in text
    assert '"--repo", $Repository, "--signer-workflow", "$Repository/.github/workflows/publish.yml"' in text
    assert "-Repository $repository" in text
    assert '"escoffier-labs/brigade/releases' not in text


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda path: path.name)
def test_workflow_repository_guards_accept_both_names(workflow):
    for line in workflow.read_text().splitlines():
        if "github.repository ==" in line:
            assert OLD in line and NEW in line, f"{workflow.name} guard names only one repository: {line.strip()}"


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
