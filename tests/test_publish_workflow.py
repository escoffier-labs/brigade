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

    assert "        if:" not in text[version_check:install]
    assert version_check < install < build < publish


def test_ci_checks_managed_snapshot():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "python scripts/managed_snapshot.py --check" in text


def test_publish_windows_native_acceptance_waits_for_pypi_and_uses_script():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text()

    job = text.index("  windows-native-acceptance:")
    build = text.index("  build-and-publish:")
    assert build < job
    section = text[job:]
    assert "needs: build-and-publish" in section
    assert "runs-on: windows-latest" in section
    assert "shell: powershell" in section
    assert "if: github.ref_type == 'tag' && startsWith(github.ref_name, 'v')" in section
    assert "windows-native-acceptance.ps1" in section
    assert "-InstallMode pypi" in section
    assert "-BrigadeVersion" in section
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
