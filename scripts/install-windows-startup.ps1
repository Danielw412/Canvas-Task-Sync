<#
.SYNOPSIS
    Make the Canvas Task Sync dashboards available at sign-in.

.DESCRIPTION
    Without -ServerHost the task runs the whole application on this computer.
    With -ServerHost the server runs the backend and both dashboards. The task then only
    keeps an SSH tunnel open, so http://127.0.0.1:8890 and :8891 on this laptop reach the
    server. No web server, sync runtime, credentials, or database run on this laptop.

.EXAMPLE
    .\scripts\install-windows-startup.ps1
    .\scripts\install-windows-startup.ps1 -ServerHost daniel@100.87.157.44
#>
[CmdletBinding()]
param(
    [string]$ServerHost,
    [int]$RemoteBackendPort = 8790,
    [int]$RemoteSimplePort = 8891
)

$ErrorActionPreference = "Stop"

$taskName = "Canvas Task Sync Web"
$websiteUrl = "http://127.0.0.1:8890/"
$simpleWebsiteUrl = "http://127.0.0.1:8891/"
$port = 8890
$simplePort = 8891
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

# A manually launched or previously detached pythonw process can outlive the scheduled-task
# action. Stop only this project's startup module before testing port readiness, otherwise an old
# server can make the installer report success while the replacement exits with WinError 10048.
$staleServers = @(
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $commandLine = [string]$_.CommandLine
        $_.Name -in @("python.exe", "pythonw.exe") -and
        $commandLine.Contains("canvas_task_sync.windows_startup") -and
        $commandLine.Contains($configPath)
    }
)
foreach ($staleServer in $staleServers) {
    Stop-Process -Id $staleServer.ProcessId -Force -ErrorAction Stop
}

# An ssh.exe left behind by an earlier tunnel keeps holding the dashboard ports. This also
# covers the retired 8879 tunnel of the old laptop-hosted dashboards. School Dashboard's
# own tunnel forwards 8790 and is left alone.
$staleTunnels = @(
    Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $commandLine = [string]$_.CommandLine
        $commandLine.Contains("$($port):127.0.0.1:") -or
        $commandLine.Contains("$($simplePort):127.0.0.1:") -or
        $commandLine.Contains("8879:127.0.0.1:")
    }
)
foreach ($staleTunnel in $staleTunnels) {
    Stop-Process -Id $staleTunnel.ProcessId -Force -ErrorAction SilentlyContinue
}

$portReleaseDeadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    $listeners = @(Get-NetTCPConnection -LocalPort $port, $simplePort -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) {
        break
    }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $portReleaseDeadline)

if ($listeners.Count -ne 0) {
    $owners = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "Ports $port and $simplePort are still occupied after stopping this project's prior server. Owning process IDs: $owners"
}

$actionArguments = '-m canvas_task_sync.windows_startup --config "{0}" --log-path "{1}" --port {2} --simple-port {3}' -f $configPath, $logPath, $port, $simplePort
if ($ServerHost) {
    $actionArguments += ' --ssh-target "{0}" --remote-backend-port {1} --remote-simple-port {2}' -f $ServerHost, $RemoteBackendPort, $RemoteSimplePort
    Write-Host "Server-hosted mode: both dashboards run on $ServerHost; this laptop only keeps an SSH tunnel open." -ForegroundColor Cyan
}
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
    -Description $(if ($ServerHost) { "Opens the SSH tunnel to the Canvas Task Sync dashboards on $ServerHost when this user signs in." } else { "Starts the private Canvas Task Sync website in the background when this user signs in." }) `
    -Force | Out-Null

$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "Canvas Task Sync.url"
$simpleShortcutPath = Join-Path $desktopPath "Canvas Task Sync Simple.url"
$shortcutContents = @(
    "[InternetShortcut]"
    "URL=$websiteUrl"
    "IconFile=$env:SystemRoot\System32\SHELL32.dll"
    "IconIndex=220"
)
[System.IO.File]::WriteAllLines($shortcutPath, $shortcutContents)
$simpleShortcutContents = @(
    "[InternetShortcut]"
    "URL=$simpleWebsiteUrl"
    "IconFile=$env:SystemRoot\System32\SHELL32.dll"
    "IconIndex=220"
)
[System.IO.File]::WriteAllLines($simpleShortcutPath, $simpleShortcutContents)

Start-ScheduledTask -TaskName $taskName
$startupBeganAt = [DateTime]::Now.AddSeconds(-2)

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $taskState = if ($null -ne $task) { [string]$task.State } else { "Missing" }
    try {
        $response = Invoke-WebRequest -Uri $websiteUrl -UseBasicParsing -TimeoutSec 1
        $simpleResponse = Invoke-WebRequest -Uri $simpleWebsiteUrl -UseBasicParsing -TimeoutSec 1
        # One process owns both ports: the server in local mode, ssh.exe in server mode.
        $activeListeners = @(Get-NetTCPConnection -LocalPort $port, $simplePort -State Listen -ErrorAction SilentlyContinue)
        $listenerPorts = @($activeListeners | Select-Object -ExpandProperty LocalPort -Unique)
        $listenerOwners = @($activeListeners | Select-Object -ExpandProperty OwningProcess -Unique)
        $listenerIsReplacement = (
            $listenerPorts.Count -eq 2 -and
            $listenerOwners.Count -eq 1 -and
            $activeListeners[0].CreationTime -ge $startupBeganAt
        )
        if ($response.StatusCode -eq 200 -and $simpleResponse.StatusCode -eq 200 -and $taskState -eq "Running" -and $listenerIsReplacement) {
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
    $hint = if ($ServerHost) { " Check that 'ssh $ServerHost' connects without a password prompt and that 'systemctl --user status canvas-task-sync' is active on the server." } else { "" }
    throw "Startup was installed, but the background task did not make $websiteUrl and $simpleWebsiteUrl ready. Task state: $taskState. Last task result: $lastResult. See $logPath for startup diagnostics.$hint"
}

Write-Host "Windows startup task installed: $taskName"
if ($ServerHost) {
    Write-Host "The SSH tunnel to $ServerHost runs in the background via pythonw.exe; no browser was opened."
}
else {
    Write-Host "The server runs in the background via pythonw.exe; no browser was opened."
}
Write-Host "Desktop shortcut created: $shortcutPath"
Write-Host "Simple UI shortcut created: $simpleShortcutPath"
Write-Host "Website ready: $websiteUrl"
Write-Host "Simple UI ready: $simpleWebsiteUrl"
Write-Host "Startup log: $logPath"
