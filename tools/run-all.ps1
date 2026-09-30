# Runs the whole MusicHistory pipeline in order. Every stage is resumable, so re-running
# after an interruption only does the remaining work. Logs go to data\reports\run_<stage>.log.
#
#   powershell -ExecutionPolicy Bypass -File tools\run-all.ps1            # all stages
#   powershell -ExecutionPolicy Bypass -File tools\run-all.ps1 -From analyze
param(
    [ValidateSet("canon", "fetch", "select", "analyze", "influence", "layout")]
    [string]$From = "canon"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$reports = Join-Path $root "data\reports"
New-Item -ItemType Directory -Force $reports | Out-Null

$stages = @(
    @{ Name = "canon";     Args = @("canon", "--pool-controls") },
    @{ Name = "fetch";     Args = @("fetch") },
    @{ Name = "select";    Args = @("select") },
    @{ Name = "analyze";   Args = @("analyze") },
    @{ Name = "influence"; Args = @("influence") },
    @{ Name = "layout";    Args = @("layout") }
)

$started = $false
foreach ($stage in $stages) {
    if ($stage.Name -eq $From) { $started = $true }
    if (-not $started) { continue }
    $log = Join-Path $reports ("run_" + $stage.Name + ".log")
    Write-Host ("== " + $stage.Name + " (log: " + $log + ")")
    Push-Location $root
    try {
        & $python -m musichistory @($stage.Args) *> $log
        if ($LASTEXITCODE -ne 0) { throw ("stage " + $stage.Name + " failed with exit code " + $LASTEXITCODE) }
    } finally {
        Pop-Location
    }
}

& $python -m musichistory status
