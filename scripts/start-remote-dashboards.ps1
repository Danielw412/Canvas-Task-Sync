<#
.SYNOPSIS
    Start the Canvas Task Sync dashboards on this laptop against the server backend.

.DESCRIPTION
    Opens an SSH tunnel to the authoritative backend and starts both dashboards locally.
    Nothing on this machine runs a sync, touches Google credentials, or writes the
    operational database: every API call is forwarded over the tunnel.

        laptop :8890  full dashboard + API proxy
        laptop :8891  simple dashboard
              |
              +-- ssh -N -L 8879:127.0.0.1:8790 -> server :8790 (authoritative backend)

.EXAMPLE
    .\scripts\start-remote-dashboards.ps1
    .\scripts\start-remote-dashboards.ps1 -ServerHost daniel@192.168.1.186 -NoBrowser
#>
[CmdletBinding()]
param(
    [string]$ServerHost = 'daniel@192.168.1.186',
    [int]$TunnelPort = 8879,
    [int]$RemoteBackendPort = 8790,
    [int]$Port = 8890,
    [int]$SimplePort = 8891,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot '.canvas-task-sync'
$pidFile = Join-Path $runtimeDir 'remote-dashboards.json'
$logDir = Join-Path $runtimeDir 'logs'
New-Item -ItemType Directory -Force -Path $runtimeDir, $logDir | Out-Null

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

function Test-PortInUse([int]$candidate) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $candidate -ErrorAction SilentlyContinue)
}

if (Test-Path $pidFile) {
    Write-Host 'Dashboards appear to be running already. Stopping them first.' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'stop-remote-dashboards.ps1')
}

foreach ($busy in @($Port, $SimplePort)) {
    if (Test-PortInUse $busy) {
        throw "Port $busy is already in use. Stop the old Canvas Task Sync server first (scripts\remove-windows-startup.ps1 disables the scheduled task)."
    }
}

Write-Host "Opening SSH tunnel ${TunnelPort} -> ${ServerHost}:${RemoteBackendPort} ..." -ForegroundColor Cyan
$tunnelLog = Join-Path $logDir 'ssh-tunnel.log'
$tunnel = Start-Process -FilePath 'ssh' -PassThru -WindowStyle Hidden -RedirectStandardError $tunnelLog -ArgumentList @(
    '-N',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-L', "${TunnelPort}:127.0.0.1:${RemoteBackendPort}",
    $ServerHost
)

# The tunnel must be listening before the proxy starts, or the first dashboard load fails.
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline -and -not (Test-PortInUse $TunnelPort)) {
    if ($tunnel.HasExited) { throw "SSH tunnel exited immediately. See $tunnelLog" }
    Start-Sleep -Milliseconds 250
}
if (-not (Test-PortInUse $TunnelPort)) {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    throw "SSH tunnel did not start listening on port $TunnelPort. See $tunnelLog"
}
Write-Host "  tunnel up (pid $($tunnel.Id))" -ForegroundColor Green

Write-Host "Starting dashboards on 127.0.0.1:${Port} and 127.0.0.1:${SimplePort} ..." -ForegroundColor Cyan
$dashboardArgs = @(
    '-m', 'canvas_task_sync',
    'web',
    '--remote', "http://127.0.0.1:${TunnelPort}",
    '--remote-host-header', "127.0.0.1:${RemoteBackendPort}",
    '--port', "$Port",
    '--simple-port', "$SimplePort"
)
if ($NoBrowser) { $dashboardArgs += '--no-open' }

$dashboardLog = Join-Path $logDir 'dashboards.log'
$dashboard = Start-Process -FilePath $python -PassThru -WindowStyle Hidden `
    -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $dashboardLog `
    -RedirectStandardError (Join-Path $logDir 'dashboards.err.log') `
    -ArgumentList $dashboardArgs

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline -and -not (Test-PortInUse $Port)) {
    if ($dashboard.HasExited) {
        Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
        throw "Dashboards exited immediately. See $logDir"
    }
    Start-Sleep -Milliseconds 250
}

@{
    tunnel_pid    = $tunnel.Id
    dashboard_pid = $dashboard.Id
    server_host   = $ServerHost
    tunnel_port   = $TunnelPort
    port          = $Port
    simple_port   = $SimplePort
    started_at    = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -Path $pidFile -Encoding utf8

Write-Host ''
Write-Host 'Canvas Task Sync dashboards are running against the server backend.' -ForegroundColor Green
Write-Host "  Full dashboard   http://127.0.0.1:${Port}"
Write-Host "  Simple dashboard http://127.0.0.1:${SimplePort}"
Write-Host "  Backend          ${ServerHost}:${RemoteBackendPort} (via 127.0.0.1:${TunnelPort})"
Write-Host "  Logs             $logDir"
Write-Host ''
Write-Host 'Stop with: .\scripts\stop-remote-dashboards.ps1'
