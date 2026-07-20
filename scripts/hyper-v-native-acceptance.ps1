#requires -Version 5.1
<#
.SYNOPSIS
    Maintainer-operated Hyper-V clean-checkpoint acceptance for a published Brigade tag.
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$VmName,

    [Parameter(Mandatory = $true)]
    [string]$BrigadeVersion,

    [Parameter(Mandatory = $true)]
    [pscredential]$Credential,

    [string]$GuestScript = "C:\BrigadeAcceptance\windows-native-acceptance.ps1",

    [ValidateRange(1, 900)]
    [int]$ReadinessTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$tag = "v$BrigadeVersion"
if ($BrigadeVersion.StartsWith("v")) { throw "BrigadeVersion must omit the v prefix" }
if (-not (Get-VMSnapshot -VMName $VmName -Name "clean" -ErrorAction SilentlyContinue)) {
    throw "Hyper-V VM $VmName has no required clean checkpoint"
}

Stop-VM -Name $VmName -Force -ErrorAction SilentlyContinue
Restore-VMSnapshot -VMName $VmName -Name "clean" -Confirm:$false
Start-VM -Name $VmName

function Wait-GuestReadiness {
    param(
        [string]$Name,
        [pscredential]$GuestCredential,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $heartbeat = Get-VMIntegrationService -VMName $Name -Name "Heartbeat" -ErrorAction SilentlyContinue
        if ($heartbeat -and $heartbeat.Enabled -and $heartbeat.PrimaryStatusDescription -eq "OK") {
            break
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if (-not $heartbeat -or -not $heartbeat.Enabled -or $heartbeat.PrimaryStatusDescription -ne "OK") {
        throw "Hyper-V VM $Name timed out waiting for VM heartbeat after $TimeoutSeconds seconds"
    }

    $lastError = $null
    do {
        try {
            Invoke-Command -VMName $Name -Credential $GuestCredential -ScriptBlock { $true } -ErrorAction Stop | Out-Null
            return
        } catch {
            $lastError = $_.Exception.Message
            Start-Sleep -Seconds 2
        }
    } while ((Get-Date) -lt $deadline)
    throw "Hyper-V VM $Name timed out waiting for PowerShell Direct guest readiness after $TimeoutSeconds seconds: $lastError"
}

Wait-GuestReadiness -Name $VmName -GuestCredential $Credential -TimeoutSeconds $ReadinessTimeoutSeconds

Invoke-Command -VMName $VmName -Credential $Credential -ScriptBlock {
    param($GuestScript, $Version, $ReleaseTag)
    $ErrorActionPreference = "Stop"
    function Assert-CommandMissing { param([string]$Name) if (Get-Command $Name -ErrorAction SilentlyContinue) { throw "$Name must be absent in the clean VM" } }
    Assert-CommandMissing "go"
    Assert-CommandMissing "cargo"
    if (-not (Test-Path -LiteralPath $GuestScript)) { throw "guest acceptance script is missing: $GuestScript" }
    & $GuestScript -InstallMode pypi -BrigadeVersion $Version
    if ($LASTEXITCODE -ne 0) { throw "guest published acceptance failed for $ReleaseTag" }
    [pscustomobject]@{ vm = $env:COMPUTERNAME; release_tag = $ReleaseTag; checkpoint = "clean" } | ConvertTo-Json -Compress
} -ArgumentList $GuestScript, $BrigadeVersion, $tag
