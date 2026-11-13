"""Cohere Transcribe (2B, Apache 2.0) through ONNX Runtime, no PyTorch needed.

* The weights are the community ONNX export ``OpenVoiceOS/cohere-transcribe-2b-onnx`` (int8, ~2.1 GB),
  loaded with ``onnx-asr``. The revision is pinned: the export is third-party, so a later push to the
  repository must not change what the app runs. The model itself is Cohere's ``cohere-transcribe-03-2026``.
* It runs on the CPU (about 0.2x real time on a desktop CPU in the exporter's measurements).
* The model decodes clips of up to ~30 s, so the recording is cut at pauses with the same Silero voice
  activity detection that faster-whisper uses (silence is skipped, which also avoids invented text).
* Differences from Whisper: it needs the language chosen (14 are supported), has no translate task, no
  glossary/hotwords and no word timestamps. A line gets the time span of its sentences, spread over the
  chunk by length; speaker splitting inside a line then falls back to estimated word times.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

REPO = "OpenVoiceOS/cohere-transcribe-2b-onnx"
REVISION = "4864772d909c20afa8acab2248db01cecb1e47fe"
FILES = (
    "config.json",
    "vocab.txt",
    "decoder-model.int8.onnx",
    "encoder-model.int8.onnx",
    "encoder-model.int8.onnx.data",
)

LANGUAGES = {
    "ar": "Arabic", "de": "German", "el": "Greek", "en": "English", "es": "Spanish", "fr": "French",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "nl": "Dutch", "pl": "Polish",
    "pt": "Portuguese", "vi": "Vietnamese", "zh": "Chinese",
}

SAMPLE_RATE = 16000
MAX_CHUNK_SECONDS = 25.0  # the model is good to ~30 s
MERGE_GAP_SECONDS = 0.8  # speech pieces closer than this are decoded together, for context
MIN_CHUNK_SECONDS = 0.3

_lock = threading.Lock()
_loaded: dict[str, Any] = {"path": None, "model": None}


def is_downloaded(directory: Path) -> bool:
    return all((directory / name).exists() for name in FILES)


def ensure_downloaded(directory: Path) -> Path:
    if not is_downloaded(directory):
        from huggingface_hub import hf_hub_download

        directory.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            if not (directory / name).exists():
                hf_hub_download(REPO, name, revision=REVISION, local_dir=str(directory))
    return directory


def get_model(directory: Path):
    with _lock:
        if _loaded["path"] != str(directory):
            _loaded.update(path=None, model=None)  # free memory before loading
            import onnx_asr

            ensure_downloaded(directory)
            model = onnx_asr.load_model("nemo-conformer-aed", str(directory), quantization="int8")
            _loaded.update(path=str(directory), model=model)
        return _loaded["model"]


def language_code(language: str | None) -> str:
    code = (language or "").strip().lower().replace("_", "-").split("-")[0]
    if code in LANGUAGES:
        return code
    names = ", ".join(f"{name} ({key})" for key, name in LANGUAGES.items())
    if not code:
        raise ValueError(f"Cohere Transcribe cannot detect the language: choose one of {names}.")
    raise ValueError(f"Cohere Transcribe does not support '{language}'. Supported: {names}.")


def speech_chunks(audio: np.ndarray) -> list[tuple[float, float]]:
    """(start, end) seconds of the spoken stretches, each at most MAX_CHUNK_SECONDS long."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(min_silence_duration_ms=500, max_speech_duration_s=MAX_CHUNK_SECONDS)
    pieces = [(item["start"] / SAMPLE_RATE, item["end"] / SAMPLE_RATE) for item in get_speech_timestamps(audio, options)]
    chunks: list[tuple[float, float]] = []
    for start, end in pieces:
        if chunks and start - chunks[-1][1] <= MERGE_GAP_SECONDS and end - chunks[-1][0] <= MAX_CHUNK_SECONDS:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((start, end))
    return [(a, b) for a, b in chunks if b - a >= MIN_CHUNK_SECONDS]


_SENTENCE = re.compile(r".*?(?:[.!?…。！？]+[\"'”’)\]]*(?=\s|$)|[。！？]+|$)", re.S)


def split_sentences(text: str) -> list[str]:
    sentences = [match.group(0).strip() for match in _SENTENCE.finditer(text.strip())]
    return [s for s in sentences if s]


def lines_for_chunk(start: float, end: float, text: str) -> list[tuple[float, float, str]]:
    """Cut a chunk's text into sentence lines; each gets a share of the chunk's time by length."""
    sentences = split_sentences(text)
    total = sum(len(s) for s in sentences)
    if not total:
        return []
    lines, done = [], 0
    for sentence in sentences:
        a = start + (end - start) * done / total
        done += len(sentence)
        lines.append((a, start + (end - start) * done / total, sentence))
    return lines


def run(
    audio: np.ndarray,
    duration: float,
    offset: float,
    language: str | None,
    model_dir: Path,
    on_progress: Callable[[str, int], None],
    on_segments: Callable[[list[dict[str, Any]], int, str], None],
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    code = language_code(language)
    if not is_downloaded(model_dir):
        on_progress("downloading_model", 3)
    on_progress("loading_model", 4)
    model = get_model(model_dir)
    on_progress("transcribing", 5)

    counter = 0
    stopped = False
    for start, end in speech_chunks(audio):
        if should_stop():
            stopped = True
            break
        clip = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
        text = str(model.recognize(clip, sample_rate=SAMPLE_RATE, language=code, pnc=True)).strip()
        lines = []
        for a, b, sentence in lines_for_chunk(start, end, text):
            counter += 1
            lines.append(
                {
                    "id": f"s{counter:05}",
                    "start": round(a + offset, 2),
                    "end": round(min(b, duration) + offset, 2),
                    "text": sentence,
                    "original_text": sentence,
                    "speaker": None,
                    "words": [],
                }
            )
        on_segments(lines, 5 + int(min(end / duration, 1.0) * 93) if duration else 98, code)
    return {
        "language": code,
        "device": "cpu · int8 (onnx)",
        "glossary_terms": None,
        "stopped": stopped,
    }
