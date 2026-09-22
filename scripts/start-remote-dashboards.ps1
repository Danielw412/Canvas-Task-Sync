<#
.SYNOPSIS
    Open the Canvas Task Sync dashboards hosted on the server, without the startup task.

.DESCRIPTION
    The server runs the backend and both dashboards. This script opens one SSH tunnel so
    that the usual loopback addresses on this laptop reach them. Nothing else runs here.

        laptop 127.0.0.1:8890 --ssh--> server 127.0.0.1:8790  backend + full dashboard
        laptop 127.0.0.1:8891 --ssh--> server 127.0.0.1:8891  simple dashboard

    Unlike the scheduled task (scripts\install-windows-startup.ps1 -ServerHost ...), this
    tunnel is not reopened if the connection drops.

.EXAMPLE
    .\scripts\start-remote-dashboards.ps1
    .\scripts\start-remote-dashboards.ps1 -ServerHost daniel@100.87.157.44 -NoBrowser
#>
[CmdletBinding()]
param(
    [string]$ServerHost = 'daniel@100.87.157.44',
    [int]$RemoteBackendPort = 8790,
    [int]$RemoteSimplePort = 8891,
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

function Test-PortInUse([int]$candidate) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $candidate -ErrorAction SilentlyContinue)
}

if (Test-Path $pidFile) {
    Write-Host 'A tunnel appears to be open already. Closing it first.' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'stop-remote-dashboards.ps1')
}

foreach ($busy in @($Port, $SimplePort)) {
    if (Test-PortInUse $busy) {
        throw "Port $busy is already in use. If the startup task is running, the dashboards are already available; otherwise stop the old server first (scripts\remove-windows-startup.ps1 disables the scheduled task)."
    }
}

Write-Host "Opening SSH tunnel to $ServerHost ..." -ForegroundColor Cyan
$tunnelLog = Join-Path $logDir 'ssh-tunnel.log'
$tunnel = Start-Process -FilePath 'ssh' -PassThru -WindowStyle Hidden -RedirectStandardError $tunnelLog -ArgumentList @(
    '-N',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=15',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ConnectTimeout=10',
    '-L', "127.0.0.1:${Port}:127.0.0.1:${RemoteBackendPort}",
    '-L', "127.0.0.1:${SimplePort}:127.0.0.1:${RemoteSimplePort}",
    $ServerHost
)

$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline -and -not ((Test-PortInUse $Port) -and (Test-PortInUse $SimplePort))) {
    if ($tunnel.HasExited) { throw "SSH tunnel exited immediately. See $tunnelLog" }
    Start-Sleep -Milliseconds 250
}
if (-not ((Test-PortInUse $Port) -and (Test-PortInUse $SimplePort))) {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    throw "SSH tunnel did not start listening on ports $Port and $SimplePort. See $tunnelLog"
}

@{
    tunnel_pid  = $tunnel.Id
    server_host = $ServerHost
    port        = $Port
    simple_port = $SimplePort
    started_at  = (Get-Date).ToString('o')
} | ConvertTo-Json | Set-Content -Path $pidFile -Encoding utf8

if (-not $NoBrowser) { Start-Process "http://127.0.0.1:${Port}/" }

Write-Host ''
Write-Host 'Connected to the Canvas Task Sync dashboards on the server.' -ForegroundColor Green
Write-Host "  Full dashboard   http://127.0.0.1:${Port}"
Write-Host "  Simple dashboard http://127.0.0.1:${SimplePort}"
Write-Host "  Server           ${ServerHost} (tunnel pid $($tunnel.Id))"
Write-Host ''
Write-Host 'Close the tunnel with: .\scripts\stop-remote-dashboards.ps1'
