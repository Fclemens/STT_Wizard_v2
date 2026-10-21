# Build the portable desktop app: "STT Wizard\STT Wizard.exe" plus a zip, in two flavours:
#
#   nvidia  GPU transcription on NVIDIA cards (bundles cuBLAS), CPU elsewhere  ~970 MB folder / ~620 MB zip
#   cpu     CPU transcription only, runs on any PC                              ~235 MB folder / ~100 MB zip
#
# Everything (build environment, intermediate files, output) goes OUTSIDE this
# folder by default, so OneDrive does not sync gigabytes of build files.
#
#   .\build.ps1                       # asks which version to build
#   .\build.ps1 -Variant cpu          # nvidia | cpu | both, without asking
#   .\build.ps1 -OutDir D:\builds     # somewhere else
#   .\build.ps1 -Clean                # recreate the build environment from scratch

param(
    [string]$OutDir = (Join-Path $env:LOCALAPPDATA "STTWizardBuild"),
    [ValidateSet("", "nvidia", "cpu", "both")]
    [string]$Variant = "",
    [switch]$Clean
)

# Native tools (pip, venv, PyInstaller) write progress and warnings to stderr; judge them by
# their exit codes, not by stderr (PowerShell 5.1 would otherwise abort on the first warning).
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$venv = Join-Path $OutDir "venv"
$python = Join-Path $venv "Scripts\python.exe"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

if (-not $Variant) {
    Write-Host "`nWhich version do you want to build?" -ForegroundColor Cyan
    Write-Host "  1) NVIDIA GPU  - fast on NVIDIA graphics cards, CPU elsewhere   (~970 MB folder, ~620 MB zip)"
    Write-Host "  2) CPU only    - any PC, slower transcription                   (~235 MB folder, ~100 MB zip)"
    Write-Host "  3) Both"
    do { $choice = (Read-Host "Choose 1, 2 or 3").Trim() } until ($choice -in "1", "2", "3")
    $Variant = @{ "1" = "nvidia"; "2" = "cpu"; "3" = "both" }[$choice]
}
$variants = if ($Variant -eq "both") { @("nvidia", "cpu") } else { @($Variant) }

if ($Clean -and (Test-Path $OutDir)) {
    Step "Removing $OutDir"
    Remove-Item -Recurse -Force $OutDir -ErrorAction Stop
}
New-Item -ItemType Directory -Force $OutDir | Out-Null

# Outputs of the earlier single-variant layout would only cause confusion.
foreach ($old in @((Join-Path $OutDir "dist"), (Join-Path $OutDir "work"), (Join-Path $OutDir "STT-Wizard-win64.zip"))) {
    if (Test-Path $old) { Remove-Item -Recurse -Force $old }
}

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

$results = @()
foreach ($v in $variants) {
    $dist = Join-Path $OutDir "dist-$v"
    $appFolder = Join-Path $dist "STT Wizard"
    $zip = Join-Path $OutDir "STT-Wizard-win64-$v.zip"

    Step "Building the $v version with PyInstaller"
    $env:STTW_VARIANT = $v
    & $python -m PyInstaller --noconfirm --clean `
        --distpath $dist --workpath (Join-Path $OutDir "work-$v") `
        (Join-Path $root "stt_wizard.spec")
    Remove-Item Env:\STTW_VARIANT
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($v)." }

    Copy-Item (Join-Path $root "README.md") (Join-Path $appFolder "README.md") -Force

    Step "Creating $zip"
    if (Test-Path $zip) { Remove-Item -Force $zip }
    & tar.exe -a -c -f $zip -C $dist "STT Wizard"
    if ($LASTEXITCODE -ne 0) { throw "Creating the zip failed ($v)." }

    $sizeMb = [math]::Round(((Get-ChildItem -Recurse $appFolder | Measure-Object Length -Sum).Sum) / 1MB)
    $zipMb = [math]::Round((Get-Item $zip).Length / 1MB)
    $results += "  [$v] $appFolder ($sizeMb MB)`n         $zip ($zipMb MB)"
}

Write-Host "`nDone." -ForegroundColor Green
$results | ForEach-Object { Write-Host $_ }
Write-Host "  Run 'STT Wizard.exe' inside the folder. Whisper models are downloaded on first use into its models\ subfolder."
