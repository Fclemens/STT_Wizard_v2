# STT Wizard Portable

This folder is a portable copy of the local Speech-to-Text Wizard.

## Desktop app (recommended)

`STT Wizard.exe` runs the app in its own window: no Python, browser or FFmpeg needed on the target PC.

**Build it** (needs Python 3.11+ on the build PC): double-click

```text
BUILD_EXE.bat
```

It asks which version to build:

| Version | For | App folder | Zip |
|---|---|---|---|
| **NVIDIA GPU** | PCs with an NVIDIA card: fast GPU transcription (CPU on other PCs) | ~980 MB | ~620 MB |
| **CPU only** | any PC (AMD/Intel graphics, or to keep it small): slower transcription | ~250 MB | ~100 MB |
| **Both** | builds the two above | | |

The difference is NVIDIA's cuBLAS library (~740 MB). Without an NVIDIA card it would never be used.

It builds outside this folder (in `%LOCALAPPDATA%\STTWizardBuild`, so OneDrive does not sync gigabytes of build
files) and produces, per version (`nvidia` / `cpu`):

- `dist-<version>\STT Wizard\STT Wizard.exe` — the app folder
- `STT-Wizard-win64-<version>.zip` — the same folder zipped, to copy to another PC

To skip the question: `build.ps1 -Variant nvidia` (or `cpu`, `both`).

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
   - With both on, your lines are labelled **Me** (microphone) and the other voices are told apart
     (see [Speakers](#speakers)).
   - Pause/resume any time. `Stop & transcribe` mixes the tracks and starts transcription automatically.
   - Recording runs in the app, not the browser tab: closing or refreshing the page does not stop it.
     If the app itself closes mid-recording, the audio captured so far is recovered on next start.
   - Let everyone know they are being recorded.

   Or **Upload a file**: drop an audio file (MP3, WAV, M4A, MP4, WebM, OGG, FLAC), check `Transcription settings`
   (model, language, glossary) and click `Transcribe`. A `.txt` transcript skips straight to Review.
3. Lines stream in as Whisper works. To redo it with another model or language, open
   `Transcribe again with different settings`.
4. When the transcript is done, Review runs automatically (if an OpenAI key is set and auto-review is on in
   Settings; otherwise click `Run review`). Suspicious words are highlighted in the transcript; apply, edit or
   dismiss each suggestion.
5. Click a timestamp to play the audio from that line; click any line to edit it.
   Name the speakers in the **Speakers** panel (see [Speakers](#speakers)).
6. Export the corrected or original transcript as TXT, SRT, VTT or JSON.
7. Run Analysis for a summary, key points, action items and open questions.

Recordings are kept per session in `data/sessions/<id>/audio/rec-*/`: `mic.wav` and `system.wav` (16-bit WAV at the
device rate, usually 48 kHz) and `mix.wav` (16 kHz, used for transcription). About 800 MB per hour with both sources.

The **⋮** button next to the player has:

- `Trim…`: drag the handles on the waveform (or type times) to cut chatter before and after the meeting.
  Only the kept part is played, transcribed, reviewed and exported. The files are not cut: `Undo trim` brings
  everything back. Trimming an existing transcript hides the lines outside the kept part (no need to transcribe again).
- `Export MP3`: a 48 kHz, 128 kbps MP3 of both sources mixed (about 60 MB per hour), of the kept part only when
  trimmed. The export is cached next to the recording.
- Playback speed.

Each session holds one recording or file; for another meeting, click `+ New session`.
Deleting a session deletes all its files, including recordings and exports.

## Speakers

After transcription the app tells the voices apart and labels each line with who is speaking. Labels can be
switched off: `Settings → Speaker labels by default`, `Transcription settings → Speaker labels` for one transcription,
or the `Labels` switch on the Speakers panel at any time (off removes every label and name; on detects them again).
With labels off, transcripts, exports and Analysis have no speaker names.

- Recording microphone + system audio: your lines are **Me** (from your microphone); the other people are told
  apart on the system audio as Speaker 1, Speaker 2…
- Uploaded files, or one source only: all voices are told apart.
- Set `Number of speakers` when you know it (for a recording with both sources, count only the other people);
  `Auto-detect` decides by itself.
- One voice only: lines are left unlabelled.

The **Speakers** panel lists each speaker with their talk time:

- Type a name to rename them everywhere (transcript, exports, Analysis). Names are also given to Review as
  correct spellings.
- ▶ plays a few seconds of their voice (click again for another sample).
- `Merge into…` when one person was split in two; ✕ removes a speaker; `+ Add speaker` adds someone who was missed.
- `Detect again` (optionally with a number of speakers) redoes it; the names you gave are reset, except Me.

In the transcript, click a line's speaker label to move it to someone else (or `+ New speaker…`); Shift-click a
second label to move all the lines in between. A line where the voice changes part-way is split there (at the
exact word for new transcripts; for transcripts made before November 2026 the cut is estimated from the text and can be
a word or two off).

How it works: a voice fingerprint (3D-Speaker ERes2Net, ~40 MB, downloaded from GitHub on first use into
`models\speakers`) is computed every 0.75 s along each line; stretches with similar voices are grouped into
speakers, then the voice is followed inside each line, so a quick back-and-forth within one line is caught.
It runs on the CPU: about 2–3 minutes per hour of speech. If the model cannot be downloaded, the transcript is kept
(with Me/Others labels for two-source recordings) and a notice explains why. It gets confused by people talking over each other, very short replies and similar voices, so check the
labels before relying on them.

## Glossary

Whisper is prompted with a glossary of names, acronyms and terms, which makes it spell them correctly far more often.
The same glossary tells Review which spellings are authoritative.

- `Settings → Default glossary`: terms used for every session (your company, team, recurring clients).
- `Transcription settings → Glossary for this session`: terms for this recording only.
- After applying a Review suggestion, `+ Glossary` adds the corrected term to the default glossary.

Write one term per line, most important first. Whisper gets them as hotwords for every 30-second window, but its
prompt is small (it shares space with the running context), so only the first terms that fit are used — typically
40–60. The transcript header shows how many were used (e.g. `glossary 38/98 terms`). Review always uses the full list.

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

### Cohere Transcribe (`cohere` model)

Besides the Whisper sizes there is [Cohere Transcribe](https://huggingface.co/CohereLabs/cohere-transcribe-03-2026)
(2B parameters, Apache 2.0, top of the Open ASR Leaderboard for English). It runs through ONNX Runtime
(`onnx-asr`, no PyTorch) from the community int8 export
[OpenVoiceOS/cohere-transcribe-2b-onnx](https://huggingface.co/OpenVoiceOS/cohere-transcribe-2b-onnx)
(2.1 GB, pinned to one revision, downloaded on first use into `models/cohere-transcribe`). It always runs on the CPU.

### Parakeet (`parakeet` model)

[NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) runs the same way (ONNX Runtime
via `onnx-asr`, int8 export [istupakov/parakeet-tdt-0.6b-v3-onnx](https://huggingface.co/istupakov/parakeet-tdt-0.6b-v3-onnx),
0.7 GB, pinned revision, `models/parakeet-tdt-0.6b-v3`, CPU). It is small and fast, detects the language itself among
25 European languages (the Language box is ignored; Arabic and Asian languages are not covered), and returns real
word timings, so sentence and speaker splits are as precise as with Whisper. No translate task, no glossary.

Differences from Whisper: you must pick the language (Arabic, Chinese, Dutch, English, French, German, Greek,
Italian, Japanese, Korean, Polish, Portuguese, Spanish, Vietnamese; no auto-detect), there is no translate task,
the glossary is not used during transcription (Review still uses it), and there are no word timestamps, so line
times are estimated from sentence length within each speech stretch. Accurate decoding has no effect.
