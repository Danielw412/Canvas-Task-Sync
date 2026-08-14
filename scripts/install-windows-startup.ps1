[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$taskName = "Canvas Task Sync Web"
$websiteUrl = "http://127.0.0.1:8790/"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$configPath = Join-Path $projectRoot "config\courses.yaml"
$launcherPath = Join-Path $PSScriptRoot "start-windows-web.ps1"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "The project virtual environment was not found at $pythonPath"
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "The course configuration was not found at $configPath"
}

if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "The Windows launcher was not found at $launcherPath"
}

$actionArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $launcherPath
$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument $actionArguments `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Starts the private Canvas Task Sync website when this user signs in." `
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
    try {
        $response = Invoke-WebRequest -Uri $websiteUrl -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    throw "Startup was installed, but the website did not become ready at $websiteUrl"
}

Write-Host "Windows startup task installed: $taskName"
Write-Host "Desktop shortcut created: $shortcutPath"
Write-Host "Website ready: $websiteUrl"
