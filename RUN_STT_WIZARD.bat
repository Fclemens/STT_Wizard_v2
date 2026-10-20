@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "APP_URL=http://127.0.0.1:8000"
set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Portable environment not found.
  echo Run START_STT_WIZARD.bat once first to create .venv and install dependencies.
  pause
  exit /b 1
)

echo Starting STT Wizard server...
start "STT Wizard Server" cmd /k ""%PYTHON_EXE%" -m uvicorn app:app --host 127.0.0.1 --port 8000"

echo Waiting for the local app to become available before opening the browser...
for /l %%i in (1,1,45) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing '%APP_URL%' -TimeoutSec 1 | Out-Null; exit 0 } catch { exit 1 }"
  if not errorlevel 1 goto open_browser
  timeout /t 1 /nobreak >nul
)

echo The server is still starting or failed to start. Check the STT Wizard Server window.
pause
exit /b 1

:open_browser
set "CHROME_EXE="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME_EXE if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME_EXE=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if defined CHROME_EXE (
  start "" "%CHROME_EXE%" "%APP_URL%"
) else (
  start "" "%APP_URL%"
)

echo STT Wizard is available at %APP_URL%
endlocal
