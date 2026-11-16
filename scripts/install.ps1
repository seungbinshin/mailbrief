# Install mailbrief into a local virtual environment (.venv) in the repository folder.
# Usage (in the repository folder):
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Proxy http://proxy.corp:8080
param(
    [string]$Proxy = "",
    [string]$Python = "py"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "[1/4] Python check"
$ver = & $Python -3 -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.11") { throw "Python 3.11 or newer is necessary (found $ver)." }
& $Python -3 -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) { Write-Warning "tkinter not found: the card popup will not show. Install Python from python.org with 'tcl/tk' selected." }

Write-Host "[2/4] Virtual environment (.venv)"
if (-not (Test-Path ".venv")) { & $Python -3 -m venv .venv }
$pip = Join-Path $Root ".venv\Scripts\pip.exe"
$pipArgs = @()
if ($Proxy) { $pipArgs += @("--proxy", $Proxy) }
& $pip install @pipArgs --upgrade pip
& $pip install @pipArgs -e ".[dev]"

Write-Host "[3/4] Configuration"
if (-not (Test-Path "config.toml")) {
    Copy-Item "config.example.toml" "config.toml"
    Write-Host "  config.toml created. Edit [llm] base_url / model and [source] my_address."
} else {
    Write-Host "  config.toml exists (not changed)."
}
New-Item -ItemType Directory -Force -Path "logs" | Out-Null

Write-Host "[4/4] Self test"
& (Join-Path $Root ".venv\Scripts\python.exe") -m pytest -q
Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  .venv\Scripts\mailbrief check-llm            # in-house LLM connection"
Write-Host "  .venv\Scripts\mailbrief run                  # first briefing + card popup"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1   # start at logon"
