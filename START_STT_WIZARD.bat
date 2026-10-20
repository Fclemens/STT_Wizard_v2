@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "APP_URL=http://127.0.0.1:8000"
set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo First run: creating local Python environment...
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 -m venv ".venv"
  ) else (
    python -m venv ".venv"
  )
  if not exist "%PYTHON_EXE%" (
    echo Could not create .venv. Install Python 3.11+ and try again.
    pause
    exit /b 1
  )

  echo Installing dependencies. This can take several minutes because Whisper/Torch are large.
  "%PYTHON_EXE%" -m pip install --upgrade pip
  "%PYTHON_EXE%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
  )
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
