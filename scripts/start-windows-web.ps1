[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# This remains a foreground diagnostic launcher. The installed scheduled task invokes
# pythonw.exe directly so no PowerShell process is needed for Windows startup.
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configPath = Join-Path $projectRoot "config\courses.yaml"
$startupModulePath = Join-Path $projectRoot "src\canvas_task_sync\windows_startup.py"
$logDirectory = Join-Path $projectRoot ".canvas-task-sync"
$logPath = Join-Path $logDirectory "web-startup.log"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "The project virtual environment was not found at $pythonPath"
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "The course configuration was not found at $configPath"
}

if (-not (Test-Path -LiteralPath $startupModulePath -PathType Leaf)) {
    throw "The Windows startup module was not found at $startupModulePath"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location -LiteralPath $projectRoot

Write-Host "Starting the foreground diagnostic server. Logs: $logPath"
& $pythonPath -m canvas_task_sync.windows_startup `
    --config $configPath `
    --log-path $logPath
exit $LASTEXITCODE
