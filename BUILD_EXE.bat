@echo off
rem Builds the portable "STT Wizard.exe" (see build.ps1 for options).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
echo.
pause
