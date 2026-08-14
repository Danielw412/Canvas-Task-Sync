[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configPath = Join-Path $projectRoot "config\courses.yaml"
$logDirectory = Join-Path $projectRoot ".canvas-task-sync"
$logPath = Join-Path $logDirectory "web-startup.log"

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location -LiteralPath $projectRoot

"[$([DateTimeOffset]::Now.ToString('o'))] Starting Canvas Task Sync." | Out-File `
    -LiteralPath $logPath `
    -Encoding Unicode
$ErrorActionPreference = "Continue"
& $pythonPath -m canvas_task_sync --config $configPath web --no-open *>> $logPath
exit $LASTEXITCODE
