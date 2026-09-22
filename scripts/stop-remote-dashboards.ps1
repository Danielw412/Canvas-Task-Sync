<#
.SYNOPSIS
    Close the SSH tunnel opened by start-remote-dashboards.ps1.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $repoRoot '.canvas-task-sync\remote-dashboards.json'

if (-not (Test-Path $pidFile)) {
    Write-Host 'No recorded tunnel. Nothing to stop.' -ForegroundColor Yellow
    return
}

$session = Get-Content $pidFile -Raw | ConvertFrom-Json

foreach ($processId in @($session.tunnel_pid, $session.dashboard_pid)) {
    # dashboard_pid is only present in sessions started by the old laptop-hosted script.
    if (-not $processId) { continue }
    if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        Write-Host "  pid $processId was not running."
        continue
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    Write-Host "  stopped pid $processId" -ForegroundColor Green
}

Remove-Item $pidFile -Force
Write-Host 'Tunnel closed. The dashboards keep running on the server.' -ForegroundColor Green
