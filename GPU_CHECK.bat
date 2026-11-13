@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo .venv not found. Run START_STT_WIZARD.bat once first.
  pause
  exit /b 1
)
"%PYTHON_EXE%" gpu_check.py
echo.
echo Send me the file gpu_check_report.txt (in this folder).
pause
