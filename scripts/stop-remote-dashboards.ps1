<#
.SYNOPSIS
    Stop the local Canvas Task Sync dashboards and the SSH tunnel they use.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $repoRoot '.canvas-task-sync\remote-dashboards.json'

if (-not (Test-Path $pidFile)) {
    Write-Host 'No recorded dashboard session. Nothing to stop.' -ForegroundColor Yellow
    return
}

$session = Get-Content $pidFile -Raw | ConvertFrom-Json

# Python's launcher can sit between PowerShell and the process that owns the ports, so the
# children go first; killing only the recorded pid could otherwise strand a listener.
function Stop-Tree([int]$processId, [string]$label) {
    if (-not $processId) { return }
    if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        Write-Host "  $label (pid $processId) was not running."
        return
    }
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId" -ErrorAction SilentlyContinue)) {
        Stop-Tree $child.ProcessId "$label child"
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    Write-Host "  stopped $label (pid $processId)" -ForegroundColor Green
}

Stop-Tree $session.dashboard_pid 'dashboards'
Stop-Tree $session.tunnel_pid 'ssh tunnel'

Remove-Item $pidFile -Force
Write-Host 'Dashboards stopped. The server backend keeps running on its own.' -ForegroundColor Green
