@echo off
rem Double-click to set up the Async Storage desktop client (runs install-client.ps1).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-client.ps1" %*
echo.
pause
