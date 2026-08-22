[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# This compatibility launcher is also detached. The installed scheduled task invokes
# pythonw.exe directly, while manual/scripted invocations return after starting it.
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonwPath = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$configPath = Join-Path $projectRoot "config\courses.yaml"
$startupModulePath = Join-Path $projectRoot "src\canvas_task_sync\windows_startup.py"
$logDirectory = Join-Path $projectRoot ".canvas-task-sync"
$logPath = Join-Path $logDirectory "web-startup.log"
$simplePort = 8791

if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
    throw "The project virtual environment's windowless Python executable was not found at $pythonwPath"
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "The course configuration was not found at $configPath"
}

if (-not (Test-Path -LiteralPath $startupModulePath -PathType Leaf)) {
    throw "The Windows startup module was not found at $startupModulePath"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location -LiteralPath $projectRoot

Write-Host "Starting the background server. Logs: $logPath"
$actionArguments = '-m canvas_task_sync.windows_startup --config "{0}" --log-path "{1}" --simple-port {2}' -f $configPath, $logPath, $simplePort
$process = Start-Process `
    -FilePath $pythonwPath `
    -ArgumentList $actionArguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -PassThru

Write-Host "Background server started (PID $($process.Id))."
