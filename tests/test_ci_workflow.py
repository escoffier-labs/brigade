from pathlib import Path
import json
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
    assert "notify: ${{ steps.filter.outputs.notify }}" in changes
    assert "code-graph:" not in changes
    assert "evidence-ledger:" not in changes
    assert "stations/notify:" not in changes

    for path in (
        "engines/code-graph/**",
        "engines/evidence-ledger/**",
        "stations/notify/**",
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


def test_ci_workflow_gates_notify_jobs_with_valid_expressions():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    expected_jobs = {
        "notify-build-and-test": "notify",
        "notify-windows": "notify",
    }

    job_names = re.findall(r"^  ([a-z0-9][a-z0-9-]*):$", text, re.MULTILINE)
    assert {name for name in job_names if name.startswith("notify-")} == set(expected_jobs)

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


def test_ci_workflow_runs_notify_go_commands_from_notify_directory():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    ubuntu = _workflow_job_section(text, "notify-build-and-test")
    assert "runs-on: ubuntu-latest" in ubuntu
    assert "working-directory: stations/notify" in ubuntu
    # Two setup-go v5 steps: Go 1.22 for build parity, then stable for the scanner.
    assert ubuntu.count("uses: actions/setup-go@v5") == 2
    # Both Go setup steps pin the notify go.sum so the root cache warning clears.
    assert ubuntu.count("cache-dependency-path: stations/notify/go.sum") == 2
    # Checkout must not persist credentials (CodeRabbit review comment 3630426956).
    assert "persist-credentials: false" in ubuntu
    # Go 1.22 lane runs build, vet, and race tests before the stable scanner lane.
    go122 = ubuntu.index("go-version: '1.22'")
    build = ubuntu.index("go build ./...")
    assert go122 < build
    race = ubuntu.index("go test -race ./...")
    # Stable Go lands after the race test and before govulncheck installation.
    stable = ubuntu.index("go-version: stable")
    assert race < stable
    govulncheck_install = ubuntu.index("go install golang.org/x/vuln/cmd/govulncheck@v1.3.0")
    assert stable < govulncheck_install
    for command in (
        "go build ./...",
        "go vet ./...",
        "go test -race ./...",
        "go install golang.org/x/vuln/cmd/govulncheck@v1.3.0",
        "govulncheck ./...",
    ):
        assert command in ubuntu

    windows = _workflow_job_section(text, "notify-windows")
    assert "runs-on: windows-latest" in windows
    assert "working-directory: stations/notify" in windows
    assert "uses: actions/setup-go@v5" in windows
    assert "go-version: '1.22'" in windows
    assert "persist-credentials: false" in windows
    assert "cache-dependency-path: stations/notify/go.sum" in windows
    for command in (
        "go build ./...",
        "go vet ./...",
        "go test -race ./...",
    ):
        assert command in windows
    assert "govulncheck" not in windows


def test_ci_workflow_does_not_skip_docs_only_content_guard():
    text = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "paths-ignore:" not in text
    assert "content-guard:" in text
    assert "python -m content_guard scan" in text


def test_ci_component_manifest_provenance_job_installs_dev_test_dependencies():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    section = _workflow_job_section(text, "component-manifest-provenance")

    install = 'python -m pip install -e ".[dev]"'
    pytest = "python -m pytest tests/test_component_manifest_provenance.py -q"
    assert install in section
    assert section.index(install) < section.index(pytest)


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


def test_windows_native_acceptance_source_setup_uses_standalone_manifest_online_and_offline():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    online = text.index('Write-Step "brigade setup (online)"')
    setup = text[
        text.rfind('if ($InstallMode -eq "source") {', 0, online) : text.index("$report = Get-ComponentReport")
    ]

    source = re.search(r'if \(\$InstallMode -eq "source"\) \{(?P<body>.*?)\n    \}', setup, re.DOTALL)
    assert source is not None
    assert "& brigade setup --manifest-source standalone" in source.group("body")
    assert "& brigade setup --offline --manifest-source standalone" in source.group("body")


def test_windows_native_acceptance_pypi_setup_keeps_exact_manifest_default_and_digest_check():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    online = text.index('Write-Step "brigade setup (online)"')
    setup = text[
        text.rfind('if ($InstallMode -eq "source") {', 0, online) : text.index("$report = Get-ComponentReport")
    ]

    published = re.search(r"else \{(?P<body>.*?)\n    \}", setup, re.DOTALL)
    assert published is not None
    assert re.search(r"(?m)^        & brigade setup$", published.group("body"))
    assert re.search(r"(?m)^        & brigade setup --offline$", published.group("body"))
    assert "--manifest-source standalone" not in published.group("body")
    assert "Assert-ManagedComponentDigests -Manifest $releaseManifest" in text


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


def test_windows_native_acceptance_constructs_managed_prefix_with_one_platform_separator():
    text = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    managed_path = _extract_powershell_function(text, "Get-ManagedExecutablePath")

    assert (
        ".TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)"
    ) in managed_path
    assert '$managedPrefix = "$managedRoot$([System.IO.Path]::DirectorySeparatorChar)"' in managed_path
    assert '.TrimEnd("\\\\")' not in managed_path
    assert '$managedPrefix = "$managedRoot\\\\"' not in managed_path


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


def test_windows_native_acceptance_source_mode_skips_only_bundled_unpublished_components():
    """Source acceptance derives the skip set from the bundled compatibility
    manifest's empty-asset entries, not from the component report's status, so a
    component with declared assets that reports "unsupported" still fails."""
    script = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    manifest = json.loads((ROOT / "src/brigade/templates/components/manifest-v1.json").read_text())

    unpublished = [component_id for component_id, record in manifest["components"].items() if not record.get("assets")]
    published = [component_id for component_id, record in manifest["components"].items() if record.get("assets")]
    assert "agent-notify" in unpublished
    assert published, "at least one published component must remain strictly required"

    unpublished_fn = _extract_powershell_function(script, "Get-UnpublishedComponentIds")
    assert "function Get-UnpublishedComponentIds" in unpublished_fn
    assert "ConvertFrom-Json" in unpublished_fn
    assert "PSObject.Properties.Count -eq 0" in unpublished_fn
    # The skip set is read from the bundled manifest on disk, not from the
    # component report, so an unsupported component with declared assets is
    # never treated as skippable.
    assert "$Report" not in unpublished_fn

    main = script[script.index("$acceptRoot = $null") :]
    source_block = main[main.index('if ($InstallMode -eq "source") {') : main.index("$report = Get-ComponentReport")]
    assert 'Join-Path $RepoRoot "src\\brigade\\templates\\components\\manifest-v1.json"' in source_block
    assert "$unpublishedIds = Get-UnpublishedComponentIds -ManifestPath $bundledManifestPath" in source_block
    assert "$unpublishedIds = @" in main
    assert "Assert-AllComponentsHealthy -Report $report -Skippable $unpublishedIds" in main

    healthy_fn = _extract_powershell_function(script, "Assert-AllComponentsHealthy")
    assert "[string[]]$Skippable = @()" in healthy_fn
    assert "$skippableSet" in healthy_fn
    assert 'if ($component.status -ne "unsupported")' in healthy_fn
    assert 'if ($component.status -ne "healthy")' in healthy_fn

    # The agent-notify absolute-path smoke is gated on the computed required set
    # so source mode skips an unpublished agent-notify instead of invoking a
    # missing managed binary.
    assert '$requiredIds = @("agent-notify", "graphtrail", "graphtrail-mcp", "miseledger", "sessionfind") |' in main
    assert "Where-Object { $unpublishedIds -notcontains $_ }" in main
    assert 'if ($requiredIds -contains "agent-notify") {' in main
    agent_block = main[main.index('if ($requiredIds -contains "agent-notify") {') :]
    assert (
        'Get-ManagedExecutablePath -Report $report -ComponentId "agent-notify" -ManagedBin $managedBin' in agent_block
    )
    assert "& $agentNotifyExe version --json" in agent_block


def test_windows_native_acceptance_pypi_mode_never_skips_agent_notify():
    """Published/release acceptance keeps the strict five-component contract:
    no skip set, agent-notify must install, digest-check, and smoke by absolute
    managed path."""
    script = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    main = script[script.index("$acceptRoot = $null") :]

    # The skip set is only populated in source mode; pypi mode leaves it empty.
    source_block = main[main.index('if ($InstallMode -eq "source") {') : main.index("$report = Get-ComponentReport")]
    assert "$unpublishedIds = @()" in source_block
    assert "Get-UnpublishedComponentIds" in source_block

    assert "Assert-ManagedComponentDigests -Manifest $releaseManifest -Report $report -ManagedBin $managedBin" in main
    digest_fn = _extract_powershell_function(script, "Assert-ManagedComponentDigests")
    assert '"agent-notify"' in digest_fn
    assert '"graphtrail", "graphtrail-mcp", "miseledger", "sessionfind"' in digest_fn

    release_fn = _extract_powershell_function(script, "Assert-ReleaseManifestAndAssets")
    for component_id in ("agent-notify", "graphtrail", "graphtrail-mcp", "miseledger", "sessionfind"):
        assert component_id in release_fn
    assert (
        'foreach ($platform in @("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64"))'
        in release_fn
    )


def test_windows_native_acceptance_pypi_mode_rejects_bare_agent_notify_metadata():
    """Published acceptance must reject the dev/unknown/unknown defaults a bare
    `go build` leaves behind and require the exact Brigade release version plus a
    hex git SHA (full or short) and a UTC build timestamp. Source-mode skip
    behavior is untouched: the validation only runs inside the required-ids
    agent-notify block, which source mode never enters for an unpublished
    agent-notify."""
    script = (ROOT / "scripts/windows-native-acceptance.ps1").read_text()
    validator = _extract_powershell_function(script, "Assert-AgentNotifyVersionMetadata")
    assert "function Assert-AgentNotifyVersionMetadata" in validator
    assert "[string]$Version" in validator
    assert '$Payload.version -eq "dev"' in validator
    assert '$Payload.version -eq "unknown"' in validator
    assert "$Payload.version -ne $Version" in validator
    assert "$Payload.commit -eq " in validator
    assert "$Payload.commit -notmatch '^[0-9a-f]{7,40}$'" in validator
    assert "$Payload.build_date -eq " in validator
    assert r"$Payload.build_date -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$'" in validator

    main = script[script.index("$acceptRoot = $null") :]
    agent_block = main[main.index('if ($requiredIds -contains "agent-notify") {') :]
    assert "Assert-AgentNotifyVersionMetadata -Payload $agentNotifyPayload -Version $BrigadeVersion" in agent_block
    # The old loose "version field exists" check is gone.
    assert "if (-not $agentNotifyPayload.version)" not in agent_block
    # Source-mode skip behavior is preserved: the unpublished-ids derivation and
    # the required-ids gating are unchanged.
    source_block = main[main.index('if ($InstallMode -eq "source") {') : main.index("$report = Get-ComponentReport")]
    assert "$unpublishedIds = Get-UnpublishedComponentIds -ManifestPath $bundledManifestPath" in source_block
    assert "Where-Object { $unpublishedIds -notcontains $_ }" in main
