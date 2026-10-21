@echo off
rem Builds the portable "STT Wizard.exe": asks for the NVIDIA GPU or CPU-only version (see build.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
echo.
pause
