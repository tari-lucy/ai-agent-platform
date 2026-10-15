@echo off
setlocal
title AI Agent Platform
powershell.exe -NoProfile -File "%~dp0scripts\start-panel.ps1" %*
set "result=%errorlevel%"
echo.
if not "%result%"=="0" echo Server did not start or has stopped. Read the code above.
pause
exit /b %result%
