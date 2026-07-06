# agent-ultra-kit installer (Windows PowerShell)
#
#   irm https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.ps1 | iex
#
# Installs into %USERPROFILE%\.agent-ultra (own venv, no system changes except
# a user-PATH entry), then runs the doctor. Uninstall:
#   & ([scriptblock]::Create((irm .../install.ps1))) -Uninstall
# or simply: Remove-Item -Recurse -Force "$env:USERPROFILE\.agent-ultra"
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$Repo = if ($env:AGENT_ULTRA_REPO) { $env:AGENT_ULTRA_REPO }
        else { "https://github.com/trollbot2012/agent-ultra-kit.git" }
$Home_ = Join-Path $env:USERPROFILE ".agent-ultra"
$Venv  = Join-Path $Home_ "venv"
$Bin   = Join-Path $Home_ "bin"

function Remove-FromUserPath($dir) {
    $cur = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($cur) {
        $new = ($cur -split ";" | Where-Object { $_ -and $_ -ne $dir }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $new, "User")
    }
}

if ($Uninstall) {
    if (Test-Path $Home_) { Remove-Item -Recurse -Force $Home_ }
    Remove-FromUserPath $Bin
    Write-Host "agent-ultra-kit removed." -ForegroundColor Green
    return
}

Write-Host "== agent-ultra-kit installer ==" -ForegroundColor Cyan

# 1. find python >= 3.10
$Python = $null
foreach ($cand in @("py -3", "python", "python3")) {
    try {
        $v = Invoke-Expression "$cand -c `"import sys;print('%d.%d'%sys.version_info[:2])`"" 2>$null
        if ($v -and ([version]$v -ge [version]"3.10")) { $Python = $cand; break }
    } catch {}
}
if (-not $Python) {
    Write-Host "ERROR: Python 3.10+ not found. Install from https://python.org (check 'Add to PATH')." -ForegroundColor Red
    exit 1
}
Write-Host "using $Python ($v)"

# 2. venv + install
New-Item -ItemType Directory -Force -Path $Home_ | Out-Null
Invoke-Expression "$Python -m venv `"$Venv`""
& "$Venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
Write-Host "installing agent-ultra-kit from $Repo ..."
& "$Venv\Scripts\python.exe" -m pip install --quiet "git+$Repo"

# 3. shim on user PATH
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
"@echo off`r`n`"$Venv\Scripts\agent-ultra.exe`" %*" |
    Set-Content -Path (Join-Path $Bin "agent-ultra.cmd") -Encoding ascii
$cur = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not ($cur -split ";" | Where-Object { $_ -eq $Bin })) {
    [Environment]::SetEnvironmentVariable("Path", "$cur;$Bin", "User")
    Write-Host "added $Bin to your user PATH (new terminals will see it)"
}

# 4. prove it
Write-Host ""
& "$Venv\Scripts\agent-ultra.exe" doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nDoctor reported failures — see docs/troubleshooting.md" -ForegroundColor Yellow
    exit 1
}
Write-Host "`nInstalled. Try:  agent-ultra demo   (new terminal, or use the full path above)" -ForegroundColor Green
