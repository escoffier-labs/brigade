from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
