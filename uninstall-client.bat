@echo off
rem Double-click to remove the Async Storage desktop client setup (runs uninstall-client.ps1).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-client.ps1" %*
echo.
pause
