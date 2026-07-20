from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _workflow_job_section(text: str, job_name: str) -> str:
    start = text.index(f"  {job_name}:")
    next_job = re.search(r"^  [a-z0-9][a-z0-9-]*:$", text[start + 1 :], re.MULTILINE)
    end = start + 1 + next_job.start() if next_job else len(text)
    return text[start:end]


def test_ci_workflow_path_filter_has_valid_structure_and_engine_paths():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    changes = _workflow_job_section(text, "changes")

    assert "jobs:\n  changes:" in text
    assert "runs-on: ubuntu-latest" in changes
    assert "uses: dorny/paths-filter@v3" in changes
    assert "code_graph: ${{ steps.filter.outputs.code_graph }}" in changes
    assert "evidence_ledger: ${{ steps.filter.outputs.evidence_ledger }}" in changes
    assert "code-graph:" not in changes
    assert "evidence-ledger:" not in changes

    for path in (
        "engines/code-graph/**",
        "engines/evidence-ledger/**",
        "src/brigade/templates/components/manifest-v1.json",
        ".github/workflows/ci.yml",
    ):
        assert path in changes


def test_ci_workflow_path_filter_can_read_pull_request_files():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    changes = _workflow_job_section(text, "changes")

    assert "permissions:" in changes
    assert "contents: read" in changes
    assert "pull-requests: read" in changes


def test_ci_workflow_gates_native_engine_jobs_with_valid_expressions():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    expected_jobs = {
        "code-graph-msrv": "code_graph",
        "code-graph-build-and-test": "code_graph",
        "code-graph-feature-configurations": "code_graph",
        "code-graph-windows": "code_graph",
        "evidence-ledger-test": "evidence_ledger",
    }

    job_names = re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", text, re.MULTILINE)
    assert {name for name in job_names if name.startswith(("code-graph-", "evidence-ledger-"))} == set(expected_jobs)

    for job_name, output_name in expected_jobs.items():
        section = _workflow_job_section(text, job_name)
        assert "needs: changes" in section
        assert f"if: ${{{{ needs.changes.outputs.{output_name} == 'true' }}}}" in section
        assert "self-hosted" not in section


def test_ci_workflow_runs_embedded_engine_commands_from_engine_directories():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    for job_name in (
        "code-graph-msrv",
        "code-graph-build-and-test",
        "code-graph-feature-configurations",
        "code-graph-windows",
    ):
        section = _workflow_job_section(text, job_name)
        assert "working-directory: engines/code-graph" in section
        assert "uses: Swatinem/rust-cache@v2" in section
        assert "workspaces: engines/code-graph" in section

    code_graph = _workflow_job_section(text, "code-graph-build-and-test")
    for command in (
        "cargo fmt --check",
        "cargo clippy --all-targets --all-features -- -D warnings",
        "cargo test --all-features",
        "cargo build --release",
    ):
        assert command in code_graph

    feature_configurations = _workflow_job_section(text, "code-graph-feature-configurations")
    assert "cargo check --locked ${{ matrix.cargo_args }}" in feature_configurations

    evidence_ledger = _workflow_job_section(text, "evidence-ledger-test")
    assert "working-directory: engines/evidence-ledger" in evidence_ledger
    for command in (
        "go test ./...",
        "go vet ./...",
        "go install golang.org/x/vuln/cmd/govulncheck@v1.3.0",
        "govulncheck ./...",
        "go build -o bin/miseledger ./cmd/miseledger",
        "go build -o bin/sessionfind ./cmd/sessionfind",
        "scripts/check_release_workflow.sh",
        "scripts/smoke_archive.sh",
        "scripts/smoke_mcp.sh",
        "scripts/smoke_http.sh",
    ):
        assert command in evidence_ledger


def test_ci_workflow_does_not_skip_docs_only_content_guard():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "paths-ignore:" not in text
    assert "content-guard:" in text
    assert "python -m content_guard scan" in text


def test_agents_doc_names_ci_only_jobs_outside_local_verify():
    text = (ROOT / "AGENTS.md").read_text()

    assert "CI-only" in text
    for job in (
        "content-guard",
        "repo-metadata",
        "install-from-source",
        "quickstart-smoke",
        "windows-native-acceptance",
    ):
        assert job in text


def test_quickstart_smoke_covers_linux_macos_and_windows_powershell():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in text
    assert "shell: pwsh" in text
    assert "brigade operator quickstart --target $target" in text
    assert "brigade operator doctor --target $target" in text


def test_ci_windows_native_acceptance_job_uses_script_and_source_install():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    job = text.index("  windows-native-acceptance:")
    quickstart = text.index("  quickstart-smoke:")
    assert job > quickstart
    section = text[job:]
    assert "runs-on: windows-latest" in section
    assert "shell: powershell" in section
    assert "windows-native-acceptance.ps1" in section
    assert "-InstallMode source" in section
    assert "brigade setup" not in section
    assert "self-hosted" not in section


def test_windows_native_acceptance_script_is_tracked_by_gitignore_negation():
    gitignore = (ROOT / ".gitignore").read_text()
    script = ROOT / "scripts/windows-native-acceptance.ps1"

    assert "/scripts/*" in gitignore
    assert "!/scripts/windows-native-acceptance.ps1" in gitignore
    assert script.is_file()
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1


def test_ci_windows_native_acceptance_script_covers_required_flow():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()

    assert '$env:LOCALAPPDATA = Join-Path $acceptRoot "localappdata"' in text
    assert "$env:HOME = $profileRoot" in text
    assert '$env:APPDATA = Join-Path $profileRoot "AppData\\Roaming"' in text
    assert '"HOME"' in text
    assert '"APPDATA"' in text
    assert "Save-EnvSnapshot" in text
    assert "Restore-EnvSnapshot" in text
    assert "Get-PythonExeDir" in text
    assert "Initialize-PipxBootstrap" in text
    assert "bootstrap-venv" in text
    assert "-m venv" in text
    assert "Invoke-Pipx" in text
    assert "-m pipx" in text
    assert "python -m pip install --upgrade pip pipx" not in text
    assert "& python -m pip install" not in text
    assert "Get-BrigadeCliVersion" in text
    assert r"if ($line -match '^brigade\s+(.+)$')" in text
    assert "Assert-CommandPresent" in text
    assert 'Assert-CommandPresent "python"' in text
    assert 'Assert-CommandPresent "git"' in text
    assert "Assert-CommandMissing" in text
    assert 'Assert-CommandMissing "go"' in text
    assert 'Assert-CommandMissing "cargo"' in text
    assert "Set-AcceptancePath" in text
    assert "Assert-AcceptanceToolchainPresent" in text
    assert "Assert-BrigadeResolvesFromPipxBin" in text
    assert "Remove-BrigadePipxInstall" in text
    assert "Assert-BrigadeVersionMatches" in text
    assert "Invoke-ExternalCommand" in text
    assert "Get-BoundedStderr" in text
    assert "brigade setup" in text
    assert "brigade setup --offline" in text
    assert "brigade version --components --json" in text
    assert 'if ($component.status -ne "healthy")' in text
    assert "def call_greet" in text
    assert "git init" in text
    assert "brigade operator quickstart" in text
    assert "Assert-OperatorDoctorReady" in text
    assert "$payload.ready" in text
    assert "$payload.blocking_issue_count" in text
    assert "--db $dbPath sync $workRepo" in text
    assert "callers greet" in text
    assert 'if ($callersOutput -notmatch "call_greet")' in text
    assert "brigadewinacceptance" in text
    assert "brigade work verify run" in text
    assert "brigade receipts export miseledger" in text
    assert "import adapter" in text
    assert "$importPayload.inserted_items" in text
    assert "$importPayload.already_known" in text
    assert "$acceptanceMarker" in text
    assert "recorded_executable" in text
    assert "$env:XDG_CONFIG_HOME" in text
    assert "$env:XDG_DATA_HOME" in text
    assert "$env:XDG_CACHE_HOME" in text
    assert "finally" in text
    assert "#requires -Version 5.1" in text


def test_windows_native_acceptance_path_and_install_ordering():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    main = text[text.index("$acceptRoot = $null") :]

    set_path = main.index("Set-AcceptancePath")
    toolchain = main.index("Assert-AcceptanceToolchainPresent")
    source_install = main.index("Install-BrigadeFromSource")
    pypi_install = main.index("Install-BrigadeFromPyPI")
    resolve_brigade = main.index("Assert-BrigadeResolvesFromPipxBin")
    version_match = main.index("Assert-BrigadeVersionMatches")

    assert set_path < toolchain < source_install
    assert set_path < toolchain < pypi_install
    assert source_install < resolve_brigade
    assert pypi_install < resolve_brigade
    assert resolve_brigade < version_match

    pypi_function = text[text.index("function Install-BrigadeFromPyPI") : text.index("function Set-AcceptancePath")]
    assert "Assert-BrigadeVersionMatches" not in pypi_function


def test_windows_native_acceptance_json_commands_keep_stderr_separate():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()

    doctor = text[
        text.index("function Assert-OperatorDoctorReady") : text.index("function Assert-AllComponentsHealthy")
    ]
    assert "2>&1" not in doctor
    assert "Invoke-ExternalCommand" in doctor

    import_section = text[
        text.index('Write-Step "miseledger import adapter"') : text.index('Write-Step "miseledger search"')
    ]
    assert "2>&1" not in import_section
    assert "Invoke-ExternalCommand" in import_section
    assert "ConvertFrom-Json" in import_section

    search_section = text[
        text.index('Write-Step "miseledger search"') : text.index('Write-Step "Windows native acceptance passed"')
    ]
    assert "2>&1" not in search_section
    assert "Invoke-ExternalCommand" in search_section


def _extract_powershell_function(text: str, name: str) -> str:
    start = text.index(f"function {name}")
    lines = text[start:].splitlines()
    result = [lines[0]]
    depth = lines[0].count("{") - lines[0].count("}")
    for line in lines[1:]:
        result.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return "\n".join(result)


def _powershell_line_consumes_success_stream(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("&"):
        return True
    if "| Out-Host" in line or "| Out-Null" in line:
        return True
    assignment, _, _ = stripped.partition("=")
    return assignment.endswith("$null") or (assignment.startswith("$") and "&" not in assignment)


def test_windows_native_acceptance_bootstrap_return_is_single_python_path():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    bootstrap = _extract_powershell_function(text, "Initialize-PipxBootstrap")

    assert bootstrap.count("return ") == 1
    assert "return $bootstrapPython" in bootstrap

    venv_line = next(line for line in bootstrap.splitlines() if "-m venv" in line)
    pip_line = next(line for line in bootstrap.splitlines() if "-m pip install" in line)
    assert _powershell_line_consumes_success_stream(venv_line)
    assert _powershell_line_consumes_success_stream(pip_line)


def test_windows_native_acceptance_assigned_return_functions_do_not_leak_stdout():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    assigned_returns = (
        "Get-PythonExeDir",
        "Get-PythonScriptsDir",
        "Get-PipxBinDir",
        "Initialize-PipxBootstrap",
        "Get-ManagedExecutablePath",
    )

    for name in assigned_returns:
        body = _extract_powershell_function(text, name)
        assert "return " in body
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("&"):
                continue
            if "{ &" in line:
                continue
            assert _powershell_line_consumes_success_stream(line), (
                f"{name} leaks success-stream output from: {stripped}"
            )


def test_windows_native_acceptance_restricts_reported_executables_to_the_clean_managed_bin():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    managed_path = _extract_powershell_function(text, "Get-ManagedExecutablePath")

    assert "[System.IO.Path]::IsPathRooted($path)" in managed_path
    assert "Resolve-Path -LiteralPath $ManagedBin" in managed_path
    assert "Resolve-Path -LiteralPath $path" in managed_path
    assert "StartsWith($managedPrefix" in managed_path
    assert "-ManagedBin $managedBin" in text


def test_windows_native_acceptance_passes_managed_bin_to_every_executable_lookup():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    digest_assertion = _extract_powershell_function(text, "Assert-ManagedComponentDigests")

    assert "[string]$ManagedBin" in digest_assertion
    assert (
        "Get-ManagedExecutablePath -Report $Report -ComponentId $componentId -ManagedBin $ManagedBin"
        in digest_assertion
    )
    assert "Assert-ManagedComponentDigests -Manifest $releaseManifest -Report $report -ManagedBin $managedBin" in text
    for call in re.findall(r"(?m)^(?!function\s).*Get-ManagedExecutablePath[^\r\n]*", text):
        assert "-ManagedBin" in call


def test_windows_native_acceptance_miseledger_marker_is_in_verify_command_filename():
    """MiseLedger indexes work-verify item.text (command text), not command stdout."""
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    section = text[text.index("$acceptanceMarker =") : text.index('Write-Step "Windows native acceptance passed"')]

    assert "$acceptanceMarker = \"brigadewinacceptance$([guid]::NewGuid().ToString('N').Substring(0, 8))\"" in section
    assert '$verifyScriptName = "verify_$acceptanceMarker.py"' in section
    assert "$verifyScript = Join-Path $workRepo $verifyScriptName" in section
    assert '--command "python $verifyScriptName"' in section
    assert "& $miseledgerExe search $acceptanceMarker" in section
    assert "verify_smoke.py" not in section
    assert 'print("{0}")' not in section


def test_hyperv_acceptance_is_a_tracked_maintainer_contract_not_a_self_hosted_job():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    orchestrator = (ROOT / "scripts/hyper-v-native-acceptance.ps1").read_text()
    runbook = (ROOT / "docs/runbooks/hyper-v-native-acceptance.md").read_text()

    assert "self-hosted" not in workflow
    assert "Restore-VMSnapshot" in orchestrator
    assert '"clean"' in orchestrator
    assert "-BrigadeVersion" in orchestrator
    assert 'Assert-CommandMissing "go"' in orchestrator
    assert 'Assert-CommandMissing "cargo"' in orchestrator
    assert "Get-VMIntegrationService" in orchestrator
    assert "Heartbeat" in orchestrator
    assert "PowerShell Direct" in orchestrator
    assert "Start-Sleep -Seconds 2" in orchestrator
    assert "timed out waiting" in orchestrator
    assert orchestrator.index("Start-VM -Name $VmName") < orchestrator.index("Get-VMIntegrationService")
    assert orchestrator.index("Get-VMIntegrationService") < orchestrator.index("Invoke-Command -VMName")
    assert "exact immutable release tag" in runbook


def test_windows_native_acceptance_brigade_version_regex_matches_cli_output():
    script = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    assert r"if ($line -match '^brigade\s+(.+)$')" in script

    pattern = r"^brigade\s+(.+)$"
    for line, expected in (
        ("brigade 0.23.3", "0.23.3"),
        ("brigade 1.2.3", "1.2.3"),
    ):
        match = re.match(pattern, line.strip())
        assert match is not None
        assert match.group(1).strip() == expected
