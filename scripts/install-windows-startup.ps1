[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$taskName = "Canvas Task Sync Web"
$websiteUrl = "http://127.0.0.1:8790/"
$port = 8790
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonwPath = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$configPath = Join-Path $projectRoot "config\courses.yaml"
$startupModulePath = Join-Path $projectRoot "src\canvas_task_sync\windows_startup.py"
$logDirectory = Join-Path $projectRoot ".canvas-task-sync"
$logPath = Join-Path $logDirectory "web-startup.log"
$principalUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $pythonwPath -PathType Leaf)) {
    throw "The project virtual environment's windowless Python executable was not found at $pythonwPath"
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "The course configuration was not found at $configPath"
}

if (-not (Test-Path -LiteralPath $startupModulePath -PathType Leaf)) {
    throw "The Windows startup module was not found at $startupModulePath"
}

# Fail early with a useful installer error if the venv does not contain the current package.
$importCheck = Start-Process `
    -FilePath $pythonwPath `
    -ArgumentList '-c "import canvas_task_sync.windows_startup"' `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
if ($importCheck.ExitCode -ne 0) {
    throw "The project's virtual environment cannot import canvas_task_sync.windows_startup. Reinstall the project into $projectRoot."
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

# Stop the previous action before replacing it so a rerun cannot leave two servers competing for
# the loopback port. Register-ScheduledTask -Force then updates the task definition in place.
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask) {
    if ($existingTask.State -eq "Running") {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $stopDeadline = [DateTime]::UtcNow.AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 250
            $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        } while ($null -ne $existingTask -and $existingTask.State -eq "Running" -and [DateTime]::UtcNow -lt $stopDeadline)

        if ($null -ne $existingTask -and $existingTask.State -eq "Running") {
            throw "The existing scheduled task '$taskName' could not be stopped before it was updated."
        }
    }
}

$actionArguments = '-m canvas_task_sync.windows_startup --config "{0}" --log-path "{1}" --port {2}' -f $configPath, $logPath, $port
$action = New-ScheduledTaskAction `
    -Execute $pythonwPath `
    -Argument $actionArguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $principalUser
$principal = New-ScheduledTaskPrincipal `
    -UserId $principalUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Starts the private Canvas Task Sync website in the background when this user signs in." `
    -Force | Out-Null

$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "Canvas Task Sync.url"
$shortcutContents = @(
    "[InternetShortcut]"
    "URL=$websiteUrl"
    "IconFile=$env:SystemRoot\System32\SHELL32.dll"
    "IconIndex=220"
)
[System.IO.File]::WriteAllLines($shortcutPath, $shortcutContents)

Start-ScheduledTask -TaskName $taskName

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $taskState = if ($null -ne $task) { [string]$task.State } else { "Missing" }
    try {
        $response = Invoke-WebRequest -Uri $websiteUrl -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200 -and $taskState -eq "Running") {
            $ready = $true
            break
        }
    }
    catch {
        # The task can take a few seconds to start while the Python environment loads.
    }
    Start-Sleep -Milliseconds 500
}

if (-not $ready) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    $taskState = if ($null -ne $task) { [string]$task.State } else { "Missing" }
    $lastResult = if ($null -ne $taskInfo) { [string]$taskInfo.LastTaskResult } else { "Unknown" }
    throw "Startup was installed, but the background task did not make $websiteUrl ready. Task state: $taskState. Last task result: $lastResult. See $logPath for startup diagnostics."
}

Write-Host "Windows startup task installed: $taskName"
Write-Host "The server runs in the background via pythonw.exe; no browser was opened."
Write-Host "Desktop shortcut created: $shortcutPath"
Write-Host "Website ready: $websiteUrl"
Write-Host "Startup log: $logPath"
