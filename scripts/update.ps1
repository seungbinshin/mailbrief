# Pull the latest version from GitHub and reinstall. config.toml and the local state are kept.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\update.ps1 [-Proxy http://proxy.corp:8080]
param([string]$Proxy = "", [string]$TaskName = "mailbrief-watch")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$running = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($running) { Stop-ScheduledTask -TaskName $TaskName }

git pull --ff-only
$pipArgs = @()
if ($Proxy) { $pipArgs += @("--proxy", $Proxy) }
& (Join-Path $Root ".venv\Scripts\pip.exe") install @pipArgs -e ".[dev]"

if ($running) { Start-ScheduledTask -TaskName $TaskName; Write-Host "Task $TaskName restarted." }
Write-Host "Updated. Compare config.example.toml with your config.toml for new options."
