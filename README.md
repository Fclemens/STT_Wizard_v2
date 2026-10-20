# STT Wizard Portable

This folder is a portable copy of the local Speech-to-Text Wizard.

## Run

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

Dependency installation can take several minutes because Whisper and Torch are large.

For later runs, double-click:

```text
RUN_STT_WIZARD.bat
```

`RUN_STT_WIZARD.bat` skips setup and dependency installation. It expects `.venv` to already exist from the first run.

## What Stays Local

- Uploaded audio and project data are stored in `data/`.
- App preferences are stored in `settings.json`.
- The OpenAI API key is stored in `.env`.
- Whisper models are downloaded to the normal Whisper cache, usually `%USERPROFILE%\.cache\whisper`, unless `WHISPER_MODEL_DIR` is configured.

Audio transcription is local. OpenAI is only called for optional Review, Analysis, and key testing.

## OpenAI Settings Flow

Open the `Settings` tab.

1. Paste an OpenAI API key.
2. Click `Test OpenAI key`.
3. If the test succeeds, click `Save settings`.
4. Review and Analysis can then call OpenAI.

Leaving the API key field blank keeps the existing saved key.

## Workflow

1. Create or rename a project.
2. Upload audio in `Transcript`.
3. Choose a local Whisper model.
4. Run transcription.
5. Export original TXT/SRT/VTT/JSON if needed.
6. Run Review for entity/term correction suggestions.
7. Apply or dismiss suggestions.
8. Save and export corrected TXT/JSON.
9. Run Analysis for summary, key points, action items, and open questions.

## Requirements

- Windows
- Python 3.11 or newer
- FFmpeg available on PATH, or bundled separately and added to PATH
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

This is not a single-file executable. That is intentional: Whisper and Torch are large, and a portable folder is easier to debug and maintain.
