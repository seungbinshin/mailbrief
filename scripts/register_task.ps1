# Register a Windows scheduled task that starts `mailbrief watch` at logon (no console window).
# watch = new mail -> analyze + popup, and full briefing at [watch] brief_times.
# The task runs in your interactive session (necessary for Outlook COM and for the popup).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Unregister
param(
    [string]$TaskName = "mailbrief-watch",
    [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed task $TaskName"
    exit 0
}

$pyw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pyw)) { throw "Run scripts\install.ps1 first ($pyw not found)." }
if (-not (Test-Path (Join-Path $Root "config.toml"))) { throw "config.toml not found." }

$taskArgs = "-m mailbrief -c `"$Root\config.toml`" --log-file `"$Root\logs\watch.log`" watch"
$action = New-ScheduledTaskAction -Execute $pyw -Argument $taskArgs -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT1M"   # wait 1 minute after logon so Outlook can start first
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "mailbrief: Outlook mail briefing with in-house LLM" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Registered and started task '$TaskName'. Log: $Root\logs\watch.log"
