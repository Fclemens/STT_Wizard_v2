# Build the portable desktop app: "STT Wizard\STT Wizard.exe" plus a zip.
#
# Everything (build environment, intermediate files, output) goes OUTSIDE this
# folder by default, so OneDrive does not sync ~2 GB of build files.
#
#   .\build.ps1                       # build into %LOCALAPPDATA%\STTWizardBuild
#   .\build.ps1 -OutDir D:\builds     # somewhere else
#   .\build.ps1 -Clean                # recreate the build environment from scratch

param(
    [string]$OutDir = (Join-Path $env:LOCALAPPDATA "STTWizardBuild"),
    [switch]$Clean
)

# Native tools (pip, venv, PyInstaller) write progress and warnings to stderr; judge them by
# their exit codes, not by stderr (PowerShell 5.1 would otherwise abort on the first warning).
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$venv = Join-Path $OutDir "venv"
$python = Join-Path $venv "Scripts\python.exe"
$dist = Join-Path $OutDir "dist"
$appFolder = Join-Path $dist "STT Wizard"
$zip = Join-Path $OutDir "STT-Wizard-win64.zip"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

if ($Clean -and (Test-Path $OutDir)) {
    Step "Removing $OutDir"
    Remove-Item -Recurse -Force $OutDir -ErrorAction Stop
}
New-Item -ItemType Directory -Force $OutDir | Out-Null

if (-not (Test-Path $python)) {
    Step "Creating build environment in $venv"
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { & py -3 -m venv $venv } else { & python -m venv $venv }
    if (-not (Test-Path $python)) { throw "Could not create the build environment. Install Python 3.11+ from python.org." }
}

Step "Installing dependencies (first time: several minutes)"
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r (Join-Path $root "requirements.txt") pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

Step "Building with PyInstaller"
& $python -m PyInstaller --noconfirm --clean `
    --distpath $dist --workpath (Join-Path $OutDir "work") `
    (Join-Path $root "stt_wizard.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

Step "Adding README"
Copy-Item (Join-Path $root "README.md") (Join-Path $appFolder "README.md") -Force

Step "Creating $zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
& tar.exe -a -c -f $zip -C $dist "STT Wizard"
if ($LASTEXITCODE -ne 0) { throw "Creating the zip failed." }

$sizeMb = [math]::Round(((Get-ChildItem -Recurse $appFolder | Measure-Object Length -Sum).Sum) / 1MB)
$zipMb = [math]::Round((Get-Item $zip).Length / 1MB)
Write-Host "`nDone." -ForegroundColor Green
Write-Host "  App folder: $appFolder ($sizeMb MB)  ->  run 'STT Wizard.exe'"
Write-Host "  Zip:        $zip ($zipMb MB)"
Write-Host "  Whisper models are downloaded on first use into the app folder's models\ subfolder."
