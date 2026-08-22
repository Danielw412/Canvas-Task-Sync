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

$shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Canvas Task Sync.url"
$simpleShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Canvas Task Sync Simple.url"
if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    Remove-Item -LiteralPath $shortcutPath -Force
}
if (Test-Path -LiteralPath $simpleShortcutPath -PathType Leaf) {
    Remove-Item -LiteralPath $simpleShortcutPath -Force
}

Write-Host "Canvas Task Sync no longer starts automatically."
