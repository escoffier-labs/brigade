from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys

import pytest
from packaging.version import Version

from brigade import __version__
from brigade.update_cmd import BETA_PREVIEW_BASE
from scripts.prepare_dev_wheel import derive_dev_version, next_minor_version, prepare_dev_wheel, read_stable_version

ROOT = Path(__file__).resolve().parents[1]


STABLE_PUBLISH_TAG_FILTER = "v[0-9]+.[0-9]+.[0-9]+"
STABLE_PUBLISH_TAG_GUARD = re.compile(r"v\d+\.\d+\.\d+")


def test_publish_job_is_gated_by_matching_version_tag_before_environment():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    job = text.index("  build-and-publish:")
    guard = text.index("    if: github.ref_type == 'tag' && startsWith(github.ref_name, 'v')", job)
    environment = text.index("    environment: pypi", job)

    assert job < guard < environment


def test_publish_version_check_is_unconditional_and_precedes_build():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    version_check = text.index("      - name: Verify tag matches every declared version")
    install = text.index("      - name: Install build tooling")
    build = text.index("      - name: Build sdist + wheel")
    publish = text.index("      - name: Publish to PyPI")

    validate_job = text.index("  validate-release:")
    first_build_job = text.index("  build-rust-native:")
    assert "        if:" not in text[validate_job:first_build_job]
    assert version_check < install < build < publish


def test_ci_checks_managed_snapshot():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "python scripts/managed_snapshot.py --check" in text


def test_published_artifact_acceptance_matrix_waits_for_pypi_and_uses_platform_wrappers():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()

    job = text.index("  published-artifact-acceptance:")
    build = text.index("  build-and-publish:")
    assert build < job
    section = text[job:]
    assert "needs: build-and-publish" in section
    assert "runs-on: ${{ matrix.runner }}" in section
    assert "ubuntu-latest" in section
    assert "macos-15" in section
    assert "windows-latest" in section
    assert "shell: powershell" in section
    assert "if: github.ref_type == 'tag' && startsWith(github.ref_name, 'v')" in section
    assert "windows-native-acceptance.ps1" in section
    assert "-InstallMode pypi" in section
    assert "-BrigadeVersion" in section
    assert "scripts/published-artifact-acceptance.py" in section
    assert "self-hosted" not in section


def test_publish_workflow_builds_the_complete_native_matrix_then_attests_and_releases_before_pypi():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()

    for runner in ("ubuntu-latest", "ubuntu-24.04-arm", "macos-15-intel", "macos-15", "windows-latest"):
        assert runner in text
    for subject in (
        "graphtrail-linux-amd64",
        "graphtrail-mcp-windows-amd64.exe",
        "miseledger-darwin-arm64",
        "sessionfind-linux-arm64",
        "agent-notify-linux-amd64",
        "agent-notify-linux-arm64",
        "agent-notify-darwin-amd64",
        "agent-notify-darwin-arm64",
        "agent-notify-windows-amd64.exe",
        "component-manifest-v1.json",
    ):
        assert subject in text
    assert "actions/attest@v4" in text
    assert "id-token: write" in text
    assert "attestations: write" in text
    assert "artifact-metadata: write" in text
    assert "gh attestation verify" in text
    assert "--signer-workflow escoffier-labs/brigade/.github/workflows/publish.yml" in text
    assert "component-manifest-v1.json" in text
    assert "checksums.txt" in text
    assert "brigade-cli==${VERSION}" in text

    assemble = text.index("  assemble-release:")
    release = text.index("  create-release:")
    release_gate = text.index("  release-asset-gate:")
    pypi = text.index("  build-and-publish:")
    acceptance = text.index("  published-artifact-acceptance:")
    assert assemble < release < release_gate < pypi < acceptance


def test_go_native_build_disables_setup_go_cache_for_the_cross_platform_matrix():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    section = text[text.index("  build-go-native:") : text.index("  assemble-release:")]

    setup_go = section[
        section.index("      - uses: actions/setup-go@v7") : section.index("      - name: Build pure-Go")
    ]
    assert "go-version: stable" in setup_go
    assert "cache: false" in setup_go


def test_publish_acceptance_covers_native_arm64_and_rosetta_without_self_hosted_runner():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    section = text[text.index("  published-artifact-acceptance:") :]

    assert "ubuntu-24.04-arm" in section
    assert "macos-15" in section
    assert "--rosetta-darwin-amd64" in section
    rosetta = section[
        section.index("Ensure Rosetta for darwin-amd64 smoke") : section.index(
            "Run published artifact acceptance (Unix)"
        )
    ]
    assert "matrix.extra == '--rosetta-darwin-amd64'" in rosetta
    assert "/usr/bin/arch -x86_64 /usr/bin/true" in rosetta
    assert "softwareupdate --install-rosetta --agree-to-license" in rosetta
    assert "self-hosted" not in section
    script = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    assert "pypi.org/pypi/brigade-cli/json" in script
    assert "Get-BrigadeCliVersion" in script
    assert r"if ($line -match '^brigade\s+(.+)$')" in script
    assert "Assert-BrigadeVersionMatches" in script
    assert "Assert-BrigadeResolvesFromPipxBin" in script
    assert "Set-AcceptancePath" in script
    assert "Assert-AcceptanceToolchainPresent" in script
    assert "Initialize-PipxBootstrap" in script
    assert "bootstrap-venv" in script
    assert "python -m pip install --upgrade pip pipx" not in script
    assert "Remove-BrigadePipxInstall" in script
    assert '"HOME"' in script
    assert '"APPDATA"' in script
    pypi_function = script[
        script.index("function Install-BrigadeFromPyPI") : script.index("function Set-AcceptancePath")
    ]
    assert "Assert-BrigadeVersionMatches" not in pypi_function
    main = script[script.index("$acceptRoot = $null") :]
    assert main.index("Set-AcceptancePath") < main.index("Install-BrigadeFromPyPI")
    assert main.index("Install-BrigadeFromPyPI") < main.index("Assert-BrigadeResolvesFromPipxBin")
    assert main.index("Assert-BrigadeResolvesFromPipxBin") < main.index("Assert-BrigadeVersionMatches")
    assert "self-hosted" not in section


def test_publish_release_reruns_compare_existing_assets_then_upload_only_missing_assets():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    section = text[text.index("  create-release:") : text.index("  release-asset-gate:")]

    assert 'gh release view "$TAG" --repo "$GITHUB_REPOSITORY" --json assets' in section
    assert "existing release asset differs from local release asset" in section
    assert "existing release has unexpected asset" in section
    assert 'gh release upload "$TAG" "release-assets/$asset"' in section
    assert 'gh release create "$TAG" release-assets/* --repo "$GITHUB_REPOSITORY" --verify-tag' in section
    assert "--target" not in section
    assert "--clobber" not in section


def test_publish_workflow_builds_five_agent_notify_binaries_with_release_metadata_ldflags():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    section = text[text.index("  build-agent-notify-native:") : text.index("  assemble-release:")]

    assert "needs: validate-release" in section
    assert "if: github.ref_type == 'tag' && startsWith(github.ref_name, 'v')" in section
    # Exactly five platform targets, one binary each.
    assert section.count("- platform: ") == 5
    for platform in (
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
    ):
        assert f"- platform: {platform}" in section
    assert "working-directory: stations/notify" in section
    assert "CGO_ENABLED: '0'" in section
    assert "go build -trimpath" in section
    assert "./cmd/agent-notify" in section
    # Release metadata injection: ldflags with the three -X main.* fields plus
    # the trimpath and size-stripping flags (-s -w) preserved. A bare `go build`
    # would leave dev/unknown/unknown, so the workflow must inject ldflags.
    assert "-ldflags" in section
    assert "-X main.version=" in section
    assert "-X main.commit=" in section
    assert "-X main.buildDate=" in section
    assert " -s -w" in section
    # Version is the tag without the leading v; commit is the full release SHA;
    # build date is one UTC timestamp computed via `date -u`.
    assert "${{ github.ref_name }}" in section
    assert "${AGENT_NOTIFY_TAG#v}" in section
    assert "${{ github.sha }}" in section
    assert "AGENT_NOTIFY_COMMIT" in section
    # BUILD_DATE is derived deterministically from the tagged commit's committer
    # timestamp, not from wall-clock time, so re-runs reproduce the same metadata.
    assert "git show -s --format=%ct" in section
    assert 'date -u -d "@$(git show -s --format=%ct ${AGENT_NOTIFY_COMMIT})" +%Y-%m-%dT%H:%M:%SZ' in section
    # The wall-clock form must be absent so a re-run cannot drift the build date.
    assert 'BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"' not in section
    assert "date -u +%Y-%m-%dT%H:%M:%SZ" not in section
    assert "actions/upload-artifact@v7" in section
    assert "if-no-files-found: error" in section


def test_publish_workflow_assemble_release_counts_25_native_assets_and_27_release_files():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assemble = text[text.index("  assemble-release:") : text.index("  create-release:")]

    assert 'test "$(find downloaded -type f | wc -l)" -eq 25' in assemble
    assert 'test "$(find release-assets -maxdepth 1 -type f | wc -l)" -eq 27' in assemble
    assert "pattern: agent-notify-*" in assemble
    # assemble-release waits on the agent-notify native job.
    needs_line = next(line for line in assemble.splitlines() if line.strip().startswith("needs:"))
    assert "build-agent-notify-native" in needs_line


def test_publish_release_gate_requires_27_release_assets():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    gate = text[text.index("  release-asset-gate:") : text.index("  build-and-publish:")]
    assert 'test "$(find release-assets -maxdepth 1 -type f | wc -l)" -eq 27' in gate


def _load_workflow_yaml(path: Path):
    try:
        import yaml
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyyaml", "-q"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import yaml
    return yaml.safe_load(path.read_text())


def test_publish_workflow_yaml_is_valid():
    _load_workflow_yaml(ROOT / ".github" / "workflows" / "publish.yml")
    _load_workflow_yaml(ROOT / ".github" / "workflows" / "publish-dev.yml")


def test_publish_workflow_tag_filter_is_narrower_than_v_star():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    trigger = text[text.index("on:") : text.index("permissions:")]
    assert f"- '{STABLE_PUBLISH_TAG_FILTER}'" in trigger
    assert "- 'v*'" not in trigger


def test_publish_workflow_rejects_prerelease_and_development_tags():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()

    guard = text.index("  validate-stable-tag:")
    validate = text.index("  validate-release:")
    assert guard < validate
    section = text[guard:validate]
    assert STABLE_PUBLISH_TAG_GUARD.pattern in section
    assert "stable publish requires an exact vX.Y.Z tag" in section
    validate_section = text[validate : text.index("  build-rust-native:")]
    assert "needs: validate-stable-tag" in validate_section


@pytest.mark.parametrize("tag", ["v0.27.0", "v10.20.300"])
def test_stable_publish_tag_guard_accepts_exact_release_tags(tag):
    assert STABLE_PUBLISH_TAG_GUARD.fullmatch(tag)


@pytest.mark.parametrize("tag", ["v0.27.0.dev20260804", "v0.27.0b1", "v0.27.0-rc1"])
def test_stable_publish_tag_guard_rejects_prerelease_and_development_tags(tag):
    assert STABLE_PUBLISH_TAG_GUARD.fullmatch(tag) is None


def test_dev_publish_channel_builds_only_a_wheel_without_stable_release_gates():
    text = (ROOT / ".github" / "workflows" / "publish-dev.yml").read_text()

    assert "schedule:" in text
    assert "workflow_dispatch" in text
    assert "refs/heads/main" in text
    assert 'prepare_dev_wheel.py --date-suffix "$(date -u +%Y%m%d)"' in text
    assert "python -m build --wheel" in text
    assert "gh-action-pypi-publish" in text
    assert "skip-existing: true" in text
    assert "dev-v" not in text
    for stable_only_step in (
        "validate-release",
        "build-rust-native",
        "build-go-native",
        "build-agent-notify-native",
        "version_sync.py --check",
        "gh release create",
        "published-artifact-acceptance",
    ):
        assert stable_only_step not in text


def test_next_minor_version_advances_the_minor_line():
    assert next_minor_version("0.26.0") == "0.27.0"
    assert next_minor_version("1.9.4") == "1.10.0"


@pytest.mark.parametrize(
    "stable_version",
    ["0.26", "v0.26.0", "0.26.0.1", "0.26.0b1", "not-a-version"],
)
def test_next_minor_version_rejects_invalid_stable_bases(stable_version):
    with pytest.raises(ValueError, match="strict numeric X.Y.Z"):
        next_minor_version(stable_version)


def test_derive_dev_version_stamps_the_declared_line():
    assert derive_dev_version("0.28.0", "20260808") == "0.28.0.dev20260808"
    assert derive_dev_version("0.27.0", "20260808") == "0.27.0.dev20260808"


def test_source_tree_version_sorts_at_or_above_publish_dev_wheel_scheme():
    """Source / pipx-from-checkout versions must not sort below publish-dev wheels.

    Field trap: pyproject 0.27.0 < 0.28.0.devYYYYMMDD (PEP 440), so a main
    checkout looks older than the daily wheel built from that same tree.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    declared = read_stable_version(ROOT)
    derived = derive_dev_version(declared, today)
    published_scheme = f"{BETA_PREVIEW_BASE}.dev{today}"
    source = Version(__version__)
    assert source >= Version(derived)
    assert source >= Version(published_scheme)


@pytest.mark.parametrize(
    "stable_version",
    ["0.26", "v0.26.0", "0.26.0.1", "0.26.0b1", "not-a-version"],
)
def test_derive_dev_version_rejects_invalid_stable_bases(stable_version):
    with pytest.raises(ValueError, match="strict numeric X.Y.Z"):
        derive_dev_version(stable_version, "20260808")


def test_update_channels_documents_exact_dev_wheel_install():
    text = (ROOT / "docs" / "update-channels.md").read_text()

    assert "pipx install 'brigade-cli==0.28.0.dev20260907'" in text
    assert "pip install 'brigade-cli==0.28.0.dev20260907'" in text
    assert "default resolver does not select them" in text


def test_read_stable_version_accepts_exactly_one_numeric_project_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.26.1"\n')

    assert read_stable_version(tmp_path) == "0.26.1"


@pytest.mark.parametrize(
    "pyproject_text",
    [
        '[project]\nname = "pkg"\n',
        '[project]\nversion = "0.26.1.dev1"\n',
        '[project]\nversion = "v0.26.1"\n',
        '[project]\nversion = "0.26.1"\nversion = "0.26.2"\n',
    ],
)
def test_read_stable_version_rejects_malformed_or_ambiguous_pyproject(tmp_path, pyproject_text):
    (tmp_path / "pyproject.toml").write_text(pyproject_text)

    with pytest.raises(ValueError, match="exactly one strict numeric X.Y.Z"):
        read_stable_version(tmp_path)


def test_prepare_dev_wheel_stamps_python_versions_and_preserves_template_pins(tmp_path):
    (tmp_path / "src" / "brigade").mkdir(parents=True)
    (tmp_path / "src" / "brigade" / "templates").mkdir()
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.26.1"\n')
    (tmp_path / "src" / "brigade" / "__init__.py").write_text('__version__ = "0.26.1"\n')
    template = tmp_path / "src" / "brigade" / "templates" / "profile.json"
    template.write_text('{"_brigade_version": "0.26.1"}\n')

    prepare_dev_wheel(tmp_path, "0.26.1.dev20260809")

    assert 'version = "0.26.1.dev20260809"' in (tmp_path / "pyproject.toml").read_text()
    assert '__version__ = "0.26.1.dev20260809"' in (tmp_path / "src" / "brigade" / "__init__.py").read_text()
    assert template.read_text() == '{"_brigade_version": "0.26.1"}\n'


@pytest.mark.parametrize("version", ["0.26.1", "v0.26.1.dev1", "0.26.1-dev1", "0.26.dev1"])
def test_prepare_dev_wheel_rejects_non_dev_versions(tmp_path, version):
    with pytest.raises(ValueError, match="must match X.Y.Z.devN"):
        prepare_dev_wheel(tmp_path, version)


def test_published_artifact_acceptance_unix_step_passes_github_token_from_github_token():
    """Workflow contract: the Unix acceptance step must wire github.token into GITHUB_TOKEN.

    published-artifact-acceptance.py only attaches Authorization when GITHUB_TOKEN is
    set; github.token is not a default runner environment variable (issue #819).
    """
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    section = text[
        text.index("      - name: Run published artifact acceptance (Unix)") : text.index(
            "      - name: Run Windows native acceptance (published CLI)"
        )
    ]

    assert "if: matrix.kind == 'unix'" in section
    assert "BRIGADE_VERSION: ${{ github.ref_name }}" in section
    assert "GITHUB_TOKEN: ${{ github.token }}" in section
    assert "scripts/published-artifact-acceptance.py" in section


def test_publish_workflow_verifies_version_check_endpoint_after_pypi_upload():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    publish = text.index("      - name: Publish to PyPI")
    verify = text.index("      - name: Verify check.brigade.tools reports the published version")
    acceptance = text.index("  published-artifact-acceptance:")

    assert publish < verify < acceptance
    section = text[verify : text.index("  published-artifact-acceptance:")]
    assert "scripts/verify_version_check_endpoint.py" in section
    assert "EXPECTED_VERSION: ${{ github.ref_name }}" in section
    assert '--expected-version "${EXPECTED_VERSION#v}" --wait-seconds 1500' in section
    assert "skip-existing: true" in text
    assert (
        "https://check.brigade.tools/v1/version" in (ROOT / "scripts" / "verify_version_check_endpoint.py").read_text()
    )


def test_npm_release_workflow_has_dispatch_source_oidc_and_native_gates():
    workflow = _load_workflow_yaml(ROOT / ".github/workflows/npm-publish.yml")
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    assert set(trigger["workflow_dispatch"]["inputs"]) == {"expected_sha", "release_tag"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    jobs = workflow["jobs"]
    assert set(jobs) == {"build", "acceptance", "publish", "registry-acceptance", "release-gate"}
    assert set(jobs["publish"]["needs"]) == {"build", "acceptance"}
    assert jobs["publish"]["environment"] == "npm"
    assert jobs["publish"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["registry-acceptance"]["needs"] == "publish"
    assert set(jobs["release-gate"]["needs"]) == set(jobs) - {"release-gate"}
    for name, job in jobs.items():
        if name != "publish":
            assert "id-token" not in job.get("permissions", {})
            assert "environment" not in job
        for step in job["steps"]:
            if "uses" not in step:
                continue
            assert re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", step["uses"])
            if step["uses"].startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
            if step["uses"].startswith("actions/setup-node@"):
                assert step["with"]["node-version"] == "24.22.0"
            if step["uses"].startswith("actions/upload-artifact@"):
                assert step["with"]["retention-days"] >= 14
    for name in ("acceptance", "registry-acceptance"):
        matrix = jobs[name]["strategy"]["matrix"]["include"]
        assert {row["platform"] for row in matrix} == {
            "linux-x64",
            "linux-arm64",
            "darwin-x64",
            "darwin-arm64",
            "win32-x64",
        }
        assert all("self-hosted" not in row["os"] for row in matrix)
    build = "\n".join(s.get("run", "") for s in jobs["build"]["steps"])
    assert "refs/heads/main" in build and "EXPECTED_SHA" in build and "RELEASE_TAG" in build
    assert "gh attestation verify" in build
    publish = "\n".join(s.get("run", "") for s in jobs["publish"]["steps"])
    assert "publish.cjs" in publish
    assert 'test "$GITHUB_REF" = refs/heads/main' in publish
    assert 'test "$GITHUB_SHA" = "$EXPECTED_SHA"' in publish
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in publish
    assert 'test "$RELEASE_TAG" = v0.30.0' in publish
    guard = next(step for step in jobs["publish"]["steps"] if "EXPECTED_SHA" in step.get("env", {}))
    assert guard["env"]["EXPECTED_SHA"] == "${{ inputs.expected_sha }}"
    assert guard["env"]["RELEASE_TAG"] == "${{ inputs.release_tag }}"
    registry = "\n".join(s.get("run", "") for s in jobs["registry-acceptance"]["steps"])
    assert "--registry-spec" in registry


def revalidation_python(step_name):
    """Execute the workflow-owned guard, rather than a test copy of its policy."""
    import textwrap

    workflow = _load_workflow_yaml(ROOT / ".github/workflows/npm-registry-revalidate.yml")
    step = next(s for s in workflow["jobs"]["registry-acceptance"]["steps"] if s.get("name") == step_name)
    return textwrap.dedent(re.search(r"python - <<'PYCODE'\n(.*?)^\s*PYCODE$", step["run"], re.M | re.S)[1])


def test_npm_revalidation_is_read_only_and_pins_two_source_identities():
    import json
    import os
    import shlex

    workflow = _load_workflow_yaml(ROOT / ".github/workflows/npm-registry-revalidate.yml")
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["env"]["PACKAGING_SHA"] == "26eb95cd19beacc9750811613e68cc7a2c82d229"
    assert workflow["env"]["ORIGINAL_RUN_ID"] == "39256315934"
    assert workflow["env"]["ORIGINAL_ARTIFACT_ID"] == "12188086054"
    assert workflow["env"]["ORIGINAL_ARTIFACT_NAME"] == "stable-npm-release-archives"
    assert workflow["env"]["ORIGINAL_ZIP_SHA256"] == "73f0bf768ce1a60a5adbd4d0b38448de74772f72ca8d621da9b2fec6bed4ac2a"
    jobs = workflow["jobs"]
    assert set(jobs) == {"registry-acceptance", "revalidation-gate"}
    native = jobs["registry-acceptance"]
    assert native["timeout-minutes"] <= 20
    assert native["strategy"]["fail-fast"] is False
    assert native["strategy"]["matrix"]["include"] == [
        {"os": "macos-15", "platform": "darwin-arm64"},
        {"os": "macos-15-intel", "platform": "darwin-x64"},
    ]
    checkouts = [s["with"] for s in native["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert [(c["path"], c["ref"]) for c in checkouts] == [
        ("payload-source", "${{ env.PACKAGING_SHA }}"),
        ("validation-harness", "${{ github.sha }}"),
    ]
    assert all(c["persist-credentials"] is False for c in checkouts)
    # Execute only the workflow's JS argv builder, never acceptance or prepare.
    # Select by executable path so step names and shell variable names can change.
    invocations = [s for s in native["steps"] if "node -e " in s.get("run", "")]
    acceptance = [s for s in invocations if "acceptance.cjs" in s["run"]]
    preparation = [s for s in invocations if "prepare.py" in s["run"]]
    assert len(acceptance) == 1 and len(preparation) == 2
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": "/fixture/workspace",
        "RUNNER_TEMP": "/fixture/temp",
        "PACKAGING_SHA": workflow["env"]["PACKAGING_SHA"],
        "EXPECTED_PLATFORM": "darwin-arm64",
    }
    for step in [*acceptance, *preparation]:
        assert step["working-directory"] == "payload-source"
        builder = re.search(r"\bnode -e ('[^']*')", step["run"])
        assert builder, "workflow must provide an independently executable argv builder"
        argv = json.loads(
            subprocess.check_output(["node", "-e", shlex.split(builder[1])[0]], env=env, text=True, timeout=10)
        )
        if step in acceptance:
            assert argv[:2] == ["node", "/fixture/workspace/validation-harness/npm/feasibility/acceptance.cjs"]
            assert argv[argv.index("--expected-packaging-sha") + 1] == env["PACKAGING_SHA"]
            assert argv[argv.index("--registry-spec") + 1] == "@escoffier-labs/brigade@0.29.0"
        else:
            assert argv == ["python", "npm/feasibility/prepare.py", "--verify-artifacts", "/fixture/temp/npm-artifacts"]
    assert native["steps"].index(preparation[0]) < native["steps"].index(acceptance[0])
    assert native["steps"].index(acceptance[0]) < native["steps"].index(preparation[1])
    assert preparation[1]["if"] == "always()"
    assert {"GITHUB_SHA", "GITHUB_REF"}.isdisjoint(workflow.get("env", {}))
    for job in jobs.values():
        assert {"GITHUB_SHA", "GITHUB_REF"}.isdisjoint(job.get("env", {}))
        assert "environment" not in job and "permissions" not in job
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"actions/[a-z-]+@[a-f0-9]{40}", step["uses"])
            assert {"GITHUB_SHA", "GITHUB_REF"}.isdisjoint(step.get("env", {}))
            if "GH_TOKEN" in step.get("env", {}):
                assert step["name"] == "Fetch original artifact and API metadata"
            assert not re.search(
                r"npm (?:publish|pack)|publish\.cjs|--out.*npm-artifacts|secrets\.", step.get("run", "")
            )
    gate = jobs["revalidation-gate"]
    assert gate["if"] == "always()" and gate["needs"] == "registry-acceptance"
    assert gate["steps"][0]["env"]["REGISTRY_RESULT"] == "${{ needs.registry-acceptance.result }}"
    assert 'test "$REGISTRY_RESULT" = success' in gate["steps"][0]["run"]


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "artifact-id",
        "run-id",
        "repository",
        "source",
        "expired",
        "digest-metadata",
        "digest-bytes",
        "unsafe-name",
        "duplicate-name",
        "symlink",
        "extra-file",
    ],
)
def test_npm_revalidation_rejects_substituted_or_unsafe_original_artifacts(tmp_path, mutation):
    import hashlib
    import json
    import os
    import stat
    import zipfile

    workflow = _load_workflow_yaml(ROOT / ".github/workflows/npm-registry-revalidate.yml")
    env = {**os.environ, **workflow["env"], "RUNNER_TEMP": str(tmp_path)}
    proof = tmp_path / "npm-revalidation"
    proof.mkdir()
    archive = proof / "original-archives.zip"
    names = (
        ["escoffier-labs-brigade-0.29.0.tgz"]
        + [
            f"escoffier-labs-brigade-uv-{p}-0.29.0.tgz"
            for p in ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64")
        ]
        + ["artifacts.json", "runtime.json"]
    )
    if mutation == "unsafe-name":
        names[-1] = "../runtime.json"
    if mutation == "extra-file":
        names.append("unexpected.txt")
    with zipfile.ZipFile(archive, "w") as z:
        for name in names:
            if mutation == "symlink" and name == "runtime.json":
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(entry, "elsewhere")
            else:
                z.writestr(name, b"original archive fixture")
        if mutation == "duplicate-name":
            with pytest.warns(UserWarning, match="Duplicate name"):
                z.writestr(names[0], b"duplicate")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    env["ORIGINAL_ZIP_SHA256"] = digest
    metadata = {
        "id": 12188086054,
        "name": "stable-npm-release-archives",
        "expired": False,
        "expires_at": "2099-01-01T00:00:00Z",
        "digest": "sha256:" + digest,
        "workflow_run": {
            "id": 39256315934,
            "head_sha": env["PACKAGING_SHA"],
            "repository_id": 123,
            "head_repository_id": 123,
        },
    }
    run = {
        "id": 39256315934,
        "head_sha": env["PACKAGING_SHA"],
        "event": "workflow_dispatch",
        "path": ".github/workflows/npm-publish.yml",
        "repository": {"id": 123, "full_name": "escoffier-labs/brigade"},
        "head_repository": {"id": 123, "full_name": "escoffier-labs/brigade"},
    }
    if mutation == "artifact-id":
        metadata["id"] += 1
    elif mutation == "run-id":
        metadata["workflow_run"]["id"] += 1
    elif mutation == "repository":
        run["repository"]["full_name"] = "someone/brigade"
    elif mutation == "source":
        metadata["workflow_run"]["head_sha"] = "b" * 40
    elif mutation == "expired":
        metadata["expires_at"] = "2000-01-01T00:00:00Z"
    elif mutation == "digest-metadata":
        metadata["digest"] = "sha256:" + "0" * 64
    elif mutation == "digest-bytes":
        archive.write_bytes(archive.read_bytes() + b"changed")
    (proof / "artifact-api.json").write_text(json.dumps(metadata))
    (proof / "original-run-api.json").write_text(json.dumps(run))
    result = subprocess.run(
        [sys.executable, "-c", revalidation_python("Verify original pins and safely extract")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (mutation is None), result.stderr
    payload = tmp_path / "npm-artifacts"
    if mutation is None:
        assert sorted(p.name for p in payload.iterdir()) == sorted(names)
        assert json.loads((proof / "artifact-verification.json").read_text())["zipSha256"] == digest
    else:
        assert not payload.exists(), "reject the complete inventory before extracting any file"


@pytest.mark.parametrize(
    "mutation", [None, "harness-head", "payload-head", "dirty", "provenance", "malformed-sha", "non-main"]
)
def test_npm_revalidation_checks_real_clean_checkout_identities(tmp_path, mutation):
    import json
    import os

    heads = {}
    for name in ("payload-source", "validation-harness"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        scripts = repo / "npm/feasibility"
        scripts.mkdir(parents=True)
        for filename in ("provenance.cjs", "prepare.py", "acceptance.cjs"):
            (scripts / filename).write_text("provenance" if filename == "provenance.cjs" else name)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        heads[name] = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_WORKSPACE": str(tmp_path),
        "PACKAGING_SHA": heads["payload-source"],
        "GITHUB_SHA": heads["validation-harness"],
        "GITHUB_REPOSITORY": "escoffier-labs/brigade",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": "escoffier-labs/brigade/.github/workflows/npm-registry-revalidate.yml@refs/heads/main",
    }
    if mutation == "harness-head":
        env["GITHUB_SHA"] = "a" * 40
    elif mutation == "payload-head":
        env["PACKAGING_SHA"] = "a" * 40
    elif mutation == "non-main":
        env["GITHUB_REF"] = "refs/heads/other"
        env["GITHUB_WORKFLOW_REF"] = (
            "escoffier-labs/brigade/.github/workflows/npm-registry-revalidate.yml@refs/heads/other"
        )
    elif mutation == "malformed-sha":
        env["GITHUB_SHA"] = "invalid"
    elif mutation == "dirty":
        (tmp_path / "validation-harness/unexpected").write_text("dirty")
    elif mutation == "provenance":
        repo = tmp_path / "validation-harness"
        (repo / "npm/feasibility/provenance.cjs").write_text("changed")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "changed",
            ],
            check=True,
        )
        env["GITHUB_SHA"] = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    # Toolchain provisioning is a separate workflow contract. Keep real Git I/O
    # while supplying pinned version probes on hosts with another dev toolchain.
    probes = """import subprocess
original = subprocess.check_output
def probe(argv, **kwargs):
    versions = {'node': 'v24.22.0', 'npm': '11.19.1', 'python': 'Python 3.12.1'}
    return versions[argv[0]] if argv[1:] == ['--version'] and argv[0] in versions else original(argv, **kwargs)
subprocess.check_output = probe
"""
    result = subprocess.run(
        [sys.executable, "-c", probes + revalidation_python("Record independent clean source identities")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (mutation is None), result.stderr
    if mutation is None:
        receipt = json.loads((tmp_path / "npm-revalidation/identities.json").read_text())
        assert receipt["packaging"]["sha"] == heads["payload-source"]
        assert receipt["validation"]["sha"] == heads["validation-harness"]


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "status",
        "cancellationGate",
        "platform",
        "acceptanceMode",
        "nodeVersion",
        "npmVersion",
        "distributionMode",
        "validationSha",
        "packagingSha",
        "pathEntries",
        "npxFresh",
        "missing-mode",
        "duplicate-mode",
        "survivor",
        "missing-final-proof",
        "signature-exit",
        "lock-integrity",
        "missing-lock",
    ],
)
def test_npm_revalidation_requires_complete_native_receipt(tmp_path, mutation):
    import json
    import os

    packaging, validation = "a" * 40, "b" * 40
    modes = [
        {"signal": signal, "scope": scope} for signal in ("SIGINT", "SIGTERM", "SIGHUP") for scope in ("pid", "group")
    ]
    cancellation = [{"mode": f"posix-{m['scope']}-{m['signal']}", "caught": True, "survivors": 0} for m in modes]
    artifacts = [
        {"name": "@escoffier-labs/brigade", "integrity": "sha512-launcher"},
        {
            "name": "@escoffier-labs/brigade-uv-darwin-arm64",
            "platform": "darwin-arm64",
            "integrity": "sha512-companion",
        },
    ]
    packages = [{"name": a["name"], "integrity": a["integrity"]} for a in artifacts]
    checks = (
        [{"name": "npm-audit-signatures", "exit": 0}]
        + [
            {
                "name": "registry-lockfile-integrity",
                "file": str(tmp_path / suffix / "package-lock.json"),
                "packages": packages,
            }
            for suffix in ("prefix", "npx-cache/_npx/install")
        ]
        + [
            {"name": name, "mode": c["mode"], "survivors": []}
            for name in ("cancellation-process-proof", "cancellation-final-no-survivors")
            for c in cancellation
        ]
    )
    receipt = {
        "status": "passed",
        "cancellationGate": "passed",
        "platform": "darwin-arm64",
        "expectedPlatform": "darwin-arm64",
        "acceptanceMode": "native-default",
        "nodeVersion": "v24.22.0",
        "npmVersion": "11.19.1",
        "distributionMode": "registry",
        "validationSha": validation,
        "packagingSha": packaging,
        "source": {"sourceCommit": packaging},
        "root": str(tmp_path.resolve()),
        "pathExcludesPythonAndUv": True,
        "pathEntries": [],
        "npxFresh": {"prefixExists": False, "cacheExists": False},
        "cancellationModes": modes,
        "cancellation": cancellation,
        "checks": checks,
    }
    if mutation in {
        "status",
        "cancellationGate",
        "platform",
        "acceptanceMode",
        "nodeVersion",
        "npmVersion",
        "distributionMode",
        "validationSha",
        "packagingSha",
    }:
        receipt[mutation] = "wrong"
    elif mutation == "pathEntries":
        receipt["pathEntries"] = ["python3"]
    elif mutation == "npxFresh":
        receipt["npxFresh"]["cacheExists"] = True
    elif mutation == "missing-mode":
        modes.pop()
    elif mutation == "duplicate-mode":
        modes[-1] = modes[0]
    elif mutation == "survivor":
        cancellation[-1]["survivors"] = 1
    elif mutation == "missing-final-proof":
        checks.pop()
    elif mutation == "signature-exit":
        checks[0]["exit"] = 1
    elif mutation == "lock-integrity":
        checks[1]["packages"] = [{"name": a["name"], "integrity": "sha512-wrong"} for a in artifacts]
    elif mutation == "missing-lock":
        checks.pop(1)
    for folder in ("npm-receipts", "npm-artifacts"):
        (tmp_path / folder).mkdir()
    (tmp_path / "npm-receipts/acceptance.json").write_text(json.dumps(receipt))
    (tmp_path / "npm-artifacts/artifacts.json").write_text(json.dumps({"artifacts": artifacts}))
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "EXPECTED_PLATFORM": "darwin-arm64",
        "GITHUB_SHA": validation,
        "PACKAGING_SHA": packaging,
    }
    result = subprocess.run(
        [sys.executable, "-c", revalidation_python("Require complete native registry receipt")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (mutation is None), result.stderr
