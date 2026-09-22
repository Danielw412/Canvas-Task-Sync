[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$taskName = "Canvas Task Sync Web"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $stopDeadline = [DateTime]::UtcNow.AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 250
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        } while ($null -ne $task -and $task.State -eq "Running" -and [DateTime]::UtcNow -lt $stopDeadline)

        if ($null -ne $task -and $task.State -eq "Running") {
            throw "The scheduled task '$taskName' could not be stopped and was not removed."
        }
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# In server-hosted mode the task's ssh.exe ends with it; this catches one that did not.
# School Dashboard's own tunnel forwards 8790 and is left alone.
Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $commandLine = [string]$_.CommandLine
    $commandLine.Contains("8890:127.0.0.1:") -or $commandLine.Contains("8891:127.0.0.1:")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Canvas Task Sync.url"
$simpleShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Canvas Task Sync Simple.url"
if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    Remove-Item -LiteralPath $shortcutPath -Force
}
if (Test-Path -LiteralPath $simpleShortcutPath -PathType Leaf) {
    Remove-Item -LiteralPath $simpleShortcutPath -Force
}

Write-Host "Canvas Task Sync no longer starts automatically."
