#requires -Version 5.1
<#
.SYNOPSIS
    Windows native component acceptance for Brigade issue #357 Phase 1.
#>
param(
    [Parameter(Mandatory = $false)]
    [ValidateSet("source", "pypi")]
    [string]$InstallMode = "source",

    [Parameter(Mandatory = $false)]
    [string]$BrigadeVersion = "",

    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "==> $Message"
}

function Assert-CommandPresent {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name must resolve on PATH during acceptance"
    }
}

function Assert-CommandMissing {
    param([string]$Name)
    if (Get-Command $Name -ErrorAction SilentlyContinue) {
        throw "$Name must not be available on PATH during acceptance"
    }
}

function Save-EnvSnapshot {
    param([string[]]$Names)
    $snapshot = @{}
    foreach ($name in $Names) {
        $item = Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        if ($item) {
            $snapshot[$name] = $item.Value
        }
        else {
            $snapshot[$name] = $null
        }
    }
    return $snapshot
}

function Restore-EnvSnapshot {
    param([hashtable]$Snapshot)
    foreach ($entry in $Snapshot.GetEnumerator()) {
        if ($null -eq $entry.Value) {
            Remove-Item -Path ("Env:{0}" -f $entry.Key) -ErrorAction SilentlyContinue
        }
        else {
            Set-Item -Path ("Env:{0}" -f $entry.Key) -Value $entry.Value
        }
    }
}

function Get-PythonExeDir {
    param([string]$PythonExe = "python")
    $python = Get-Command $PythonExe -ErrorAction Stop
    return (Split-Path -Parent $python.Source)
}

function Get-PythonScriptsDir {
    param([string]$PythonExe = "python")
    $scriptDir = & $PythonExe -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    if (-not $scriptDir) {
        throw "unable to resolve Python scripts directory"
    }
    return $scriptDir.Trim()
}

function Get-PipxBinDir {
    param([string]$BootstrapPython)
    if ($env:PIPX_BIN_DIR) {
        return $env:PIPX_BIN_DIR
    }
    $binDir = & $BootstrapPython -m pipx environment --value PIPX_BIN_DIR 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $binDir) {
        return $null
    }
    return $binDir.Trim()
}

function Initialize-PipxBootstrap {
    param(
        [string]$SystemPython,
        [string]$BootstrapRoot
    )
    $bootstrapVenv = Join-Path $BootstrapRoot "bootstrap-venv"
    $null = & $SystemPython -m venv $bootstrapVenv
    if ($LASTEXITCODE -ne 0) {
        throw "bootstrap venv creation failed"
    }
    $bootstrapPython = Join-Path $bootstrapVenv "Scripts\python.exe"
    if (-not (Test-Path $bootstrapPython)) {
        throw "bootstrap python missing at $bootstrapPython"
    }
    & $bootstrapPython -m pip install --upgrade pip pipx 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "bootstrap pip/pipx install failed"
    }
    return $bootstrapPython
}

function Invoke-Pipx {
    param(
        [string]$BootstrapPython,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$PipxArgs
    )
    & $BootstrapPython -m pipx @PipxArgs
}

function Remove-BrigadePipxInstall {
    param([string]$BootstrapPython)
    Invoke-Pipx -BootstrapPython $BootstrapPython uninstall brigade-cli 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Pipx -BootstrapPython $BootstrapPython uninstall brigade 2>$null | Out-Null
    }
}

function Get-BoundedStderr {
    param(
        [string]$Path,
        [int]$MaxChars = 2000
    )
    if (-not (Test-Path $Path)) {
        return ""
    }
    $text = (Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue)
    if (-not $text) {
        return ""
    }
    $text = $text.Trim()
    if ($text.Length -le $MaxChars) {
        return $text
    }
    return $text.Substring(0, $MaxChars) + "..."
}

function Invoke-ExternalCommand {
    param(
        [scriptblock]$Command,
        [string]$FailureMessage,
        [string]$StderrRoot = ""
    )
    if (-not $StderrRoot) {
        $StderrRoot = [System.IO.Path]::GetTempPath()
    }
    $stderrFile = Join-Path $StderrRoot ("brigade-accept-stderr-{0}.txt" -f [guid]::NewGuid().ToString("N"))
    try {
        $stdout = & $Command 2> $stderrFile
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            $stderr = Get-BoundedStderr -Path $stderrFile
            if ($stderr) {
                throw "$FailureMessage (exit $exitCode): $stderr"
            }
            throw "$FailureMessage (exit $exitCode)"
        }
        return $stdout
    }
    finally {
        Remove-Item -LiteralPath $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-BrigadeCliVersion {
    $raw = Invoke-ExternalCommand -Command { & brigade --version } -FailureMessage "brigade --version failed"
    $line = ($raw | Select-Object -First 1).ToString().Trim()
    if ($line -match '^brigade\s+(.+)$') {
        return $Matches[1].Trim()
    }
    throw "unrecognized brigade --version output: $line"
}

function Assert-BrigadeVersionMatches {
    param([string]$Expected)
    $installed = Get-BrigadeCliVersion
    if ($installed -ne $Expected) {
        throw "installed brigade version mismatch: expected $Expected, got $installed"
    }
}

function Install-BrigadeFromSource {
    param(
        [string]$Root,
        [string]$BootstrapPython
    )
    Write-Step "Installing Brigade from source at $Root"
    Remove-BrigadePipxInstall -BootstrapPython $BootstrapPython
    Push-Location $Root
    try {
        Invoke-Pipx -BootstrapPython $BootstrapPython install .
        if ($LASTEXITCODE -ne 0) { throw "pipx install from source failed" }
    }
    finally {
        Pop-Location
    }
}

function Install-BrigadeFromPyPI {
    param(
        [string]$Version,
        [string]$BootstrapPython
    )
    if (-not $Version) {
        throw "BrigadeVersion is required when InstallMode is pypi"
    }
    Write-Step "Installing brigade-cli==$Version from PyPI"

    $maxAttempts = 36
    $installed = $false
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        $releaseReady = $false
        try {
            $response = Invoke-RestMethod -Uri "https://pypi.org/pypi/brigade-cli/json" -Method Get -UseBasicParsing
            $releaseNames = @($response.releases.PSObject.Properties.Name)
            if ($releaseNames -contains $Version) {
                $releaseReady = $true
            }
        }
        catch {
            # retry until timeout
        }

        if ($releaseReady) {
            Remove-BrigadePipxInstall -BootstrapPython $BootstrapPython
            Invoke-Pipx -BootstrapPython $BootstrapPython install "brigade-cli==$Version"
            if ($LASTEXITCODE -eq 0) {
                $installed = $true
                break
            }
            Remove-BrigadePipxInstall -BootstrapPython $BootstrapPython
        }
        Start-Sleep -Seconds 10
    }
    if (-not $installed) {
        throw "timed out installing brigade-cli==$Version from PyPI"
    }
}

function Set-AcceptancePath {
    param(
        [string]$PythonExeDir,
        [string]$PythonScripts,
        [string]$PipxBinDir
    )
    $paths = New-Object System.Collections.Generic.List[string]
    $paths.Add($PythonExeDir)
    $paths.Add($PythonScripts)
    if ($PipxBinDir) {
        $paths.Add($PipxBinDir)
    }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        $paths.Add((Split-Path -Parent $git.Source))
    }
    $systemRoot = $env:SystemRoot
    if (-not $systemRoot) {
        $systemRoot = "C:\Windows"
    }
    foreach ($segment in @(
            (Join-Path $systemRoot "System32"),
            (Join-Path $systemRoot "System32\Wbem"),
            (Join-Path $systemRoot "System32\WindowsPowerShell\v1.0")
        )) {
        if (Test-Path $segment) {
            $paths.Add($segment)
        }
    }
    $env:PATH = ($paths | Select-Object -Unique) -join ";"
}

function Assert-AcceptanceToolchainPresent {
    Assert-CommandPresent "python"
    Assert-CommandPresent "git"
    Assert-CommandMissing "go"
    Assert-CommandMissing "cargo"
}

function Assert-BrigadeResolvesFromPipxBin {
    param([string]$PipxBinDir)
    $command = Get-Command brigade -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "brigade must resolve on PATH after pipx install"
    }
    $expectedDir = (Resolve-Path -LiteralPath $PipxBinDir).Path
    $actualDir = (Split-Path -Parent $command.Source)
    if ($actualDir -ne $expectedDir) {
        throw "brigade must resolve from isolated PIPX_BIN_DIR ($expectedDir), got $($command.Source)"
    }
}

function Get-ComponentReport {
    param([string]$StderrRoot)
    $raw = Invoke-ExternalCommand -StderrRoot $StderrRoot -Command {
        & brigade version --components --json
    } -FailureMessage "brigade version --components --json failed"
    return ($raw | Out-String).Trim() | ConvertFrom-Json
}

function Assert-OperatorDoctorReady {
    param(
        [string]$Target,
        [string]$Profile,
        [string]$StderrRoot
    )
    $raw = Invoke-ExternalCommand -StderrRoot $StderrRoot -Command {
        & brigade operator doctor --target $Target --profile $Profile --json
    } -FailureMessage "operator doctor failed"
    if (-not $raw) {
        throw "operator doctor produced no JSON output"
    }
    $payload = ($raw | Out-String).Trim() | ConvertFrom-Json
    if (-not $payload.ready) {
        throw "operator doctor ready=false blocking_issue_count=$($payload.blocking_issue_count)"
    }
    if ($payload.blocking_issue_count -ne 0) {
        throw "operator doctor blocking_issue_count=$($payload.blocking_issue_count)"
    }
}

function Assert-AllComponentsHealthy {
    param($Report)
    if ($Report.components.Count -ne 4) {
        throw "expected 4 components, got $($Report.components.Count)"
    }
    foreach ($component in $Report.components) {
        if ($component.status -ne "healthy") {
            throw "component $($component.component_id) is $($component.status): $($component.detail)"
        }
    }
}

function Get-ManagedExecutablePath {
    param(
        $Report,
        [string]$ComponentId
    )
    $item = $Report.components | Where-Object { $_.component_id -eq $ComponentId } | Select-Object -First 1
    if (-not $item) {
        throw "missing component $ComponentId in report"
    }
    $path = $item.recorded_executable
    if (-not $path) {
        $path = $item.managed_executable_path
    }
    if (-not $path) {
        throw "no managed path for $ComponentId"
    }
    if (-not (Test-Path $path)) {
        throw "managed executable missing for ${ComponentId}: $path"
    }
    return $path
}

$acceptRoot = $null
$envSnapshot = $null
try {
    $envSnapshot = Save-EnvSnapshot @(
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PATH",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "PIPX_HOME",
        "PIPX_BIN_DIR"
    )

    if ($env:RUNNER_TEMP) {
        $acceptRoot = Join-Path $env:RUNNER_TEMP ("brigade-native-acceptance-{0}" -f ([guid]::NewGuid().ToString("N").Substring(0, 8)))
    }
    else {
        $acceptRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("brigade-native-acceptance-{0}" -f ([guid]::NewGuid().ToString("N").Substring(0, 8)))
    }
    New-Item -ItemType Directory -Force -Path $acceptRoot | Out-Null

    $profileRoot = Join-Path $acceptRoot "profile"
    $env:USERPROFILE = $profileRoot
    $env:HOME = $profileRoot
    $env:LOCALAPPDATA = Join-Path $acceptRoot "localappdata"
    $env:APPDATA = Join-Path $profileRoot "AppData\Roaming"
    $env:PIPX_HOME = Join-Path $env:LOCALAPPDATA "pipx"
    $env:PIPX_BIN_DIR = Join-Path $env:LOCALAPPDATA "bin"
    $env:XDG_CONFIG_HOME = Join-Path $acceptRoot "xdg-config"
    $env:XDG_DATA_HOME = Join-Path $acceptRoot "xdg-data"
    $env:XDG_CACHE_HOME = Join-Path $acceptRoot "xdg-cache"
    New-Item -ItemType Directory -Force -Path @(
        $env:LOCALAPPDATA,
        $env:USERPROFILE,
        $env:APPDATA,
        $env:PIPX_HOME,
        $env:PIPX_BIN_DIR,
        $env:XDG_CONFIG_HOME,
        $env:XDG_DATA_HOME,
        $env:XDG_CACHE_HOME
    ) | Out-Null

    $systemPython = (Get-Command python -ErrorAction Stop).Source
    $pythonExeDir = Get-PythonExeDir -PythonExe $systemPython
    $pythonScriptsDir = Get-PythonScriptsDir -PythonExe $systemPython
    $bootstrapPython = Initialize-PipxBootstrap -SystemPython $systemPython -BootstrapRoot $acceptRoot
    $pipxBinDir = Get-PipxBinDir -BootstrapPython $bootstrapPython

    Set-AcceptancePath -PythonExeDir $pythonExeDir -PythonScripts $pythonScriptsDir -PipxBinDir $pipxBinDir
    Assert-AcceptanceToolchainPresent

    if ($InstallMode -eq "source") {
        if (-not $RepoRoot) {
            $RepoRoot = (Get-Location).Path
        }
        Install-BrigadeFromSource -Root $RepoRoot -BootstrapPython $bootstrapPython
    }
    else {
        Install-BrigadeFromPyPI -Version $BrigadeVersion -BootstrapPython $bootstrapPython
    }

    Assert-BrigadeResolvesFromPipxBin -PipxBinDir $pipxBinDir
    if ($InstallMode -eq "pypi") {
        Assert-BrigadeVersionMatches -Expected $BrigadeVersion
    }

    & brigade --version
    if ($LASTEXITCODE -ne 0) { throw "brigade --version failed" }

    Write-Step "brigade setup (online)"
    & brigade setup
    if ($LASTEXITCODE -ne 0) { throw "brigade setup failed" }

    Write-Step "brigade setup --offline"
    & brigade setup --offline
    if ($LASTEXITCODE -ne 0) { throw "brigade setup --offline failed" }

    $report = Get-ComponentReport -StderrRoot $acceptRoot
    Assert-AllComponentsHealthy $report

    $graphtrailExe = Get-ManagedExecutablePath -Report $report -ComponentId "graphtrail"
    $miseledgerExe = Get-ManagedExecutablePath -Report $report -ComponentId "miseledger"

    $workRepo = Join-Path $acceptRoot "repo"
    New-Item -ItemType Directory -Force -Path $workRepo | Out-Null
    Push-Location $workRepo
    try {
        & git init -q -b main
        if ($LASTEXITCODE -ne 0) { throw "git init failed" }

        $samplePath = Join-Path $workRepo "sample.py"
        @"
def greet():
    return "acceptance"


def call_greet():
    return greet()
"@ | Set-Content -Path $samplePath -Encoding UTF8

        Write-Step "operator quickstart"
        & brigade operator quickstart --target $workRepo --harnesses codex --json
        if ($LASTEXITCODE -ne 0) { throw "operator quickstart failed" }

        Write-Step "operator doctor"
        Assert-OperatorDoctorReady -Target $workRepo -Profile "local-operator" -StderrRoot $acceptRoot

        $dbPath = Join-Path $workRepo ".graphtrail\graphtrail.db"
        Write-Step "graphtrail sync"
        & $graphtrailExe --db $dbPath sync $workRepo
        if ($LASTEXITCODE -ne 0) { throw "graphtrail sync failed" }
        if (-not (Test-Path $dbPath)) {
            throw "graphtrail database missing at $dbPath"
        }

        Write-Step "graphtrail callers query"
        $callersOutput = & $graphtrailExe --db $dbPath callers greet 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw "graphtrail callers greet failed" }
        if ($callersOutput -notmatch "call_greet") {
            throw "graphtrail callers greet missing call_greet edge: $callersOutput"
        }

        $acceptanceMarker = "brigadewinacceptance$([guid]::NewGuid().ToString('N').Substring(0, 8))"
        $verifyScriptName = "verify_$acceptanceMarker.py"
        $verifyScript = Join-Path $workRepo $verifyScriptName
        'print("ok")' | Set-Content -Path $verifyScript -Encoding UTF8

        Write-Step "brigade work verify run"
        & brigade work verify run --target $workRepo --command "python $verifyScriptName" --capture brigade-work
        if ($LASTEXITCODE -ne 0) { throw "work verify run failed" }

        $exportPath = Join-Path $acceptRoot "receipts.jsonl"
        Write-Step "receipts export miseledger"
        & brigade receipts export miseledger --target $workRepo --out $exportPath --new-only
        if ($LASTEXITCODE -ne 0) { throw "receipts export failed" }
        if (-not (Test-Path $exportPath)) {
            throw "export file missing: $exportPath"
        }

        Write-Step "miseledger import adapter"
        $importStdout = Invoke-ExternalCommand -StderrRoot $acceptRoot -Command {
            & $miseledgerExe import adapter $exportPath --source brigade --json
        } -FailureMessage "miseledger import failed"
        $importPayload = ($importStdout | Out-String).Trim() | ConvertFrom-Json
        $inserted = [int]$importPayload.inserted_items
        $alreadyKnown = [int]$importPayload.already_known
        if (($inserted + $alreadyKnown) -lt 1) {
            throw "miseledger import did not ingest receipts: inserted_items=$inserted already_known=$alreadyKnown"
        }

        Write-Step "miseledger search"
        $searchStdout = Invoke-ExternalCommand -StderrRoot $acceptRoot -Command {
            & $miseledgerExe search $acceptanceMarker --limit 3
        } -FailureMessage "miseledger search failed"
        $searchOutput = $searchStdout | Out-String
        if ($searchOutput -notmatch [regex]::Escape($acceptanceMarker)) {
            throw "miseledger search missing acceptance marker: $searchOutput"
        }
    }
    finally {
        Pop-Location
    }

    Write-Step "Windows native acceptance passed"
}
finally {
    if ($envSnapshot) {
        Restore-EnvSnapshot -Snapshot $envSnapshot
    }
    if ($acceptRoot -and (Test-Path $acceptRoot)) {
        Remove-Item -LiteralPath $acceptRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
