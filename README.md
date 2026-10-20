# STT Wizard Portable

This folder is a portable copy of the local Speech-to-Text Wizard.

## Desktop app (recommended)

`STT Wizard.exe` runs the app in its own window: no Python, browser or FFmpeg needed on the target PC.

**Build it** (needs Python 3.11+ on the build PC): double-click

```text
BUILD_EXE.bat
```

It builds outside this folder (in `%LOCALAPPDATA%\STTWizardBuild`, so OneDrive does not sync ~2 GB of build files)
and produces:

- `dist\STT Wizard\STT Wizard.exe` — the app folder (about 1 GB, mostly the NVIDIA GPU library)
- `STT-Wizard-win64.zip` — the same folder zipped, to copy to another PC

**Use it:** unzip anywhere (not in a synced folder if you record long meetings) and run `STT Wizard.exe`.
Everything stays inside that folder: `data\` (sessions, recordings), `models\` (Whisper models, downloaded on first
use), `settings.json`, `.env` (API key) and `logs\app.log` (for troubleshooting).

Notes:

- Requires Windows 10/11 with Microsoft Edge WebView2 (preinstalled on current Windows).
- The exe is not code-signed: Windows SmartScreen may warn on first launch (`More info` → `Run anyway`),
  and managed company PCs may block it until IT allows it.
- Only one copy runs per folder. Closing the window during a recording asks to stop and save it first.

## Run from source (development)

For the first run, double-click:

```text
START_STT_WIZARD.bat
```

On first run it:

1. Creates `.venv` inside this folder.
2. Installs dependencies from `requirements.txt`.
3. Starts the local FastAPI/Uvicorn server.
4. Waits until the local app responds.
5. Opens the browser at `http://127.0.0.1:8000`.

Dependency installation can take several minutes (the NVIDIA GPU library alone is about 750 MB).

`START_STT_WIZARD.bat` also installs any new dependencies after an update, so run it once after pulling a new version.

For later runs, double-click:

```text
RUN_STT_WIZARD.bat
```

`RUN_STT_WIZARD.bat` skips setup and dependency installation. It expects `.venv` to already exist from the first run.

## What Stays Local

- Audio, transcripts, reviews and analyses are stored per session in `data/sessions/<id>/`.
- App preferences (including the default glossary) are stored in `settings.json`.
- The OpenAI API key is stored in `.env`.
- Whisper models are downloaded on first use into `models/` inside this folder (or `WHISPER_MODEL_DIR` if set).

Audio transcription is local. OpenAI is only called for Review, Analysis and key testing.

## Workflow

Everything happens on one page per session:

1. Click `+ New session`.
2. **Record** (default): tick `Microphone` and/or `System audio`, pick the devices, optionally `Check levels`, then `Start recording`.
   - System audio captures what plays on the chosen output (the other people in a Teams/Zoom call, a video…).
   - With both on, each transcript line is labelled **Me** (microphone) or **Others** (system audio).
   - Pause/resume any time. `Stop & transcribe` mixes the tracks and starts transcription automatically.
   - Recording runs in the app, not the browser tab: closing or refreshing the page does not stop it.
     If the app itself closes mid-recording, the audio captured so far is recovered on next start.
   - Let everyone know they are being recorded.

   Or **Upload a file**: drop an audio file (or a `.txt` transcript to go straight to Review).
3. Lines stream in as Whisper works. Open `Transcription settings` to pick another model or language,
   turn on translation, or edit the session glossary, then `Transcribe again`.
4. When the transcript is done, Review runs automatically (if an OpenAI key is set and auto-review is on).
   Suspicious words are highlighted in the transcript; apply, edit or dismiss each suggestion.
5. Click a timestamp to play the audio from that line; click any line to edit it.
6. Export the corrected or original transcript as TXT, SRT, VTT or JSON.
7. Run Analysis for a summary, key points, action items and open questions.

Recordings are kept per session in `data/sessions/<id>/audio/rec-*/`: `mic.wav` and `system.wav` (16-bit WAV at the
device rate, usually 48 kHz) and `mix.wav` (16 kHz, used for transcription). About 800 MB per hour with both sources.

`Export MP3` on the audio bar saves a 48 kHz, 128 kbps MP3 of both sources mixed (about 60 MB per hour).
The export is cached next to the recording.

Each session holds one recording or file; for another meeting, click `+ New session`.
Deleting a session deletes all its files, including recordings and exports.

## Glossary

Whisper is prompted with a glossary of names, acronyms and terms, which makes it spell them correctly far more often.
The same glossary tells Review which spellings are authoritative.

- `Settings → Default glossary`: terms used for every session (your company, team, recurring clients).
- `Transcription settings → Glossary for this session`: terms for this recording only.
- After applying a Review suggestion, `+ Glossary` adds the corrected term to the default glossary.

Write one term per line. The terms are given to Whisper as hotwords for every 30-second window; keep the list short (about 150 words) and put the hardest terms first.

## OpenAI Settings

Open `Settings` (bottom of the sidebar), paste an API key, click `Test key`, then `Save settings`.
Leaving the key field blank keeps the existing saved key.

## Requirements

- Windows
- Python 3.11 or newer
- Internet access on first run to install dependencies and download Whisper models

If the Microsoft Store Python alias causes permission errors, install regular Python from python.org and make sure `python` is available in PATH.

## Do Not Share

Before sending this portable folder to someone else, remove:

```text
.env
.venv
data
```

`.env` may contain an API key. `data` may contain private audio/transcripts. `.venv` is large and machine-specific.

## Notes

Transcription uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper). With an NVIDIA GPU it runs on the GPU
(int8/float16: `turbo` needs about 2 GB of video memory); otherwise, or if the GPU fails, it runs on the CPU.
FFmpeg is not needed: audio is decoded with PyAV.
