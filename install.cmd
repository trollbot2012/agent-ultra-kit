@echo off
rem agent-ultra-kit installer (Windows CMD)
rem
rem   curl -fsSL https://raw.githubusercontent.com/trollbot2012/agent-ultra-kit/main/install.cmd -o install.cmd && install.cmd
rem
rem Same layout as install.ps1: %USERPROFILE%\.agent-ultra + a shim dir.
rem Uninstall:  rmdir /s /q "%USERPROFILE%\.agent-ultra"
setlocal EnableDelayedExpansion

if "%AGENT_ULTRA_REPO%"=="" (
  set "REPO=https://github.com/trollbot2012/agent-ultra-kit.git"
) else (
  set "REPO=%AGENT_ULTRA_REPO%"
)
set "DIR=%USERPROFILE%\.agent-ultra"
set "VENV=%DIR%\venv"
set "BIN=%DIR%\bin"

echo == agent-ultra-kit installer ==

set "PYCMD="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo ERROR: Python 3.10+ not found. Install from https://python.org and check "Add to PATH".
  exit /b 1
)

%PYCMD% -m venv "%VENV%" || exit /b 1
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet || exit /b 1
echo installing agent-ultra-kit from %REPO% ...
"%VENV%\Scripts\python.exe" -m pip install --quiet "git+%REPO%" || exit /b 1

if not exist "%BIN%" mkdir "%BIN%"
> "%BIN%\agent-ultra.cmd" (
  echo @echo off
  echo "%VENV%\Scripts\agent-ultra.exe" %%*
)
echo NOTE: add %BIN% to your PATH, or call the shim by full path.

echo.
"%VENV%\Scripts\agent-ultra.exe" doctor
if errorlevel 1 (
  echo Doctor reported failures — see docs/troubleshooting.md
  exit /b 1
)
echo.
echo Installed. Try:  "%BIN%\agent-ultra.cmd" demo
endlocal
