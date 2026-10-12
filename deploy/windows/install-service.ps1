<#
.SYNOPSIS
    Install effective-scale-OS as a Windows service (Service Control Manager), with the same
    restart policy the systemd unit uses: restart twice quickly, then every 5s, forever.

.DESCRIPTION
    The kernel is single-writer and WAL-backed: an abrupt exit is recoverable (proven by
    tests/test_chaos.py), so the service policy is "bring it straight back", not "nurse it".

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-service.ps1 `
        -PythonExe C:\Python311\python.exe -RepoRoot C:\opt\effective-scale `
        -DataDir C:\ProgramData\effective-scale
#>
param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [string]$DataDir = "$env:ProgramData\effective-scale",
    [string]$Listen = "0.0.0.0:8080",
    [string]$ServiceName = "effective-scale",
    [string]$AuthSecret = ""
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $PythonExe)) { throw "python not found: $PythonExe" }
if (-not (Test-Path (Join-Path $RepoRoot "src\effective_scale"))) { throw "not a checkout: $RepoRoot" }
if ([string]::IsNullOrWhiteSpace($AuthSecret)) {
    $AuthSecret = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
    Write-Host "generated ES_AUTH_SECRET (store it safely): $AuthSecret"
}
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

[Environment]::SetEnvironmentVariable("PYTHONPATH", (Join-Path $RepoRoot "src"), "Machine")
[Environment]::SetEnvironmentVariable("ES_AUTH_SECRET", $AuthSecret, "Machine")
[Environment]::SetEnvironmentVariable("ES_LISTEN", $Listen, "Machine")

$binary = '"{0}" -m effective_scale --store "{1}\effective_scale.db" --listen {2}' -f `
    $PythonExe, $DataDir, $Listen
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "service exists - updating binary path and restarting"
    sc.exe config $ServiceName binPath= $binary | Out-Null
    Restart-Service $ServiceName
} else {
    New-Service -Name $ServiceName -BinaryPathName $binary `
        -DisplayName "effective-scale-OS kernel" -StartupType Automatic | Out-Null
    sc.exe failure $ServiceName reset= 86400 actions= restart/2000/restart/2000/restart/5000 | Out-Null
    Start-Service $ServiceName
}
Write-Host "installed. Check: Invoke-RestMethod http://127.0.0.1:$($Listen.Split(':')[1])/v1/health/ready"
