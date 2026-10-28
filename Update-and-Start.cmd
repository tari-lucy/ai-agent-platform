@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
title AI Agent Platform - Update
set "updater=%~dp0scripts\update-local.ps1"
if not exist "%updater%" set "updater=%~dp0runtime\operations\updater\update-local.ps1"
if not exist "%updater%" (
  echo UPDATE_TOOL_MISSING - contact the project maintainer.
  pause
  exit /b 78
)
if "%~1"=="" (
  powershell.exe -NoProfile -File "%updater%" -Config "%~dp0config\local.launcher.json" -Menu
) else (
  powershell.exe -NoProfile -File "%updater%" -Config "%~dp0config\local.launcher.json" %*
)
set "result=%errorlevel%"
echo.
pause
exit /b %result%
