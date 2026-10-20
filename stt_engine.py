"""Local Whisper transcription.

Audio is processed in ~30 s windows so text can stream to the UI. To keep the
quality of a single long pass, each window:
  * ends at the quietest point near the 30 s mark (no words cut in half),
  * is prompted with the glossary plus the tail of the previous window,
  * reuses the language detected on the first window.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import numpy as np
import torch
import whisper

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mpeg", ".mpga", ".webm", ".ogg", ".flac"}
ALLOWED_TEXT_EXTENSIONS = {".txt", ".md"}

SAMPLE_RATE = whisper.audio.SAMPLE_RATE
WINDOW_SECONDS = 30
MIN_CUT_SECONDS = 20
CONTEXT_WORDS = 40

MODEL_OPTIONS: dict[str, dict[str, str]] = {
    "tiny": {"speed": "~10x", "vram": "~1 GB", "best_for": "Quick drafts and low-spec devices."},
    "base": {"speed": "~7x", "vram": "~1 GB", "best_for": "Balanced starter model for light workloads."},
    "small": {"speed": "~4x", "vram": "~2 GB", "best_for": "Better quality for meetings and interviews."},
    "medium": {"speed": "~2x", "vram": "~5 GB", "best_for": "High quality multilingual transcription."},
    "large": {"speed": "1x", "vram": "~10 GB", "best_for": "Maximum accuracy when hardware allows."},
    "turbo": {"speed": "~8x", "vram": "~6 GB", "best_for": "Best default: fast and accurate."},
}


def get_model_cache_dir() -> Path:
    if model_dir := os.getenv("WHISPER_MODEL_DIR"):
        return Path(model_dir)
    if xdg_cache_home := os.getenv("XDG_CACHE_HOME"):
        return Path(xdg_cache_home) / "whisper"
    return Path.home() / ".cache" / "whisper"


def get_model_statuses() -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for model_name, meta in MODEL_OPTIONS.items():
        model_url = getattr(whisper, "_MODELS", {}).get(model_name)
        checkpoint = get_model_cache_dir() / Path(model_url).name if model_url else None
        statuses[model_name] = {**meta, "downloaded": bool(checkpoint and checkpoint.exists())}
    return statuses


def detect_device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


@lru_cache(maxsize=2)
def get_model(model_name: str):
    download_root = os.getenv("WHISPER_MODEL_DIR") or None
    return whisper.load_model(model_name, device=detect_device(), download_root=download_root)


def load_audio(path: Path) -> np.ndarray:
    return whisper.load_audio(str(path))


# ── Hallucination filters ──────────────────────────────────────────────────


def is_repetitive_hallucination(text: str) -> bool:
    words = re.findall(r"\b[\wÀ-ÿ']+\b", text.lower())
    if len(words) < 18:
        return False
    if len(set(words)) / len(words) < 0.22:
        return True
    for size in (2, 3, 4, 5):
        grams = [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]
        if not grams:
            continue
        most_common = max(grams.count(gram) for gram in set(grams))
        if most_common >= 6 or most_common / len(grams) > 0.35:
            return True
    return False


def is_low_confidence_segment(segment: dict[str, Any]) -> bool:
    text = str(segment.get("text", "")).strip()
    if not text:
        return True
    no_speech_prob = float(segment.get("no_speech_prob") or 0.0)
    avg_logprob = float(segment.get("avg_logprob") or 0.0)
    if no_speech_prob > 0.75 and avg_logprob < -0.6:
        return True
    if float(segment.get("compression_ratio") or 0.0) > 2.8:
        return True
    return is_repetitive_hallucination(text)


# ── Windowing ──────────────────────────────────────────────────────────────


def quietest_cut(audio: np.ndarray, start: int, end: int) -> int:
    """Return a sample index in [start+MIN_CUT, end] with the lowest energy."""
    search_from = start + MIN_CUT_SECONDS * SAMPLE_RATE
    if end - search_from < SAMPLE_RATE:
        return end
    frame = SAMPLE_RATE // 10  # 100 ms
    region = audio[search_from:end]
    usable = len(region) // frame * frame
    if usable == 0:
        return end
    energy = np.sqrt(np.mean(region[:usable].reshape(-1, frame) ** 2, axis=1))
    best = int(np.argmin(energy))
    return search_from + best * frame + frame // 2


def build_prompt(glossary: str, previous_text: str) -> str | None:
    tail = " ".join(previous_text.split()[-CONTEXT_WORDS:])
    # Whisper keeps only the last ~224 prompt tokens, so the glossary goes last
    # (closest to the audio) and the running context is kept short.
    terms = ", ".join(line.strip() for line in glossary.splitlines() if line.strip())
    parts = [part for part in (tail, terms) if part]
    return " ".join(parts) or None


def transcribe(
    audio_path: Path,
    *,
    model_name: str,
    language: str | None,
    task: str,
    glossary: str,
    accurate: bool,
    on_progress: Callable[[str, int], None],
    on_segments: Callable[[list[dict[str, Any]], int, str], None],
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    """Transcribe ``audio_path``; stream accepted segments via ``on_segments``.

    Returns metadata (language, device, duration, stopped).
    """
    on_progress("loading_model", 2)
    model = get_model(model_name)
    device = detect_device()

    on_progress("loading_audio", 5)
    audio = load_audio(audio_path)
    total = len(audio)
    duration = total / SAMPLE_RATE

    decode_options: dict[str, Any] = {"fp16": device == "cuda"}
    if accurate:
        decode_options.update(beam_size=5, best_of=5, temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0))
    else:
        decode_options.update(temperature=0.0)

    detected_language = language or None
    previous_text = ""
    counter = 0
    start = 0
    stopped = False
    on_progress("transcribing", 5)

    while start < total:
        if should_stop():
            stopped = True
            break
        end = min(total, start + WINDOW_SECONDS * SAMPLE_RATE)
        if end < total:
            end = quietest_cut(audio, start, end)
        chunk = audio[start:end]
        offset = start / SAMPLE_RATE

        result = model.transcribe(
            chunk,
            language=detected_language,
            task=task,
            initial_prompt=build_prompt(glossary, previous_text),
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
            verbose=None,
            **decode_options,
        )
        if not detected_language and result.get("language"):
            detected_language = result["language"]

        accepted: list[dict[str, Any]] = []
        for segment in result.get("segments", []):
            if is_low_confidence_segment(segment):
                continue
            text = str(segment.get("text", "")).strip()
            counter += 1
            accepted.append(
                {
                    "id": f"s{counter:05}",
                    "start": round(float(segment.get("start", 0.0)) + offset, 2),
                    "end": round(min(float(segment.get("end", 0.0)) + offset, duration), 2),
                    "text": text,
                    "original_text": text,
                    "speaker": None,
                }
            )
        chunk_text = " ".join(seg["text"] for seg in accepted)
        if accepted and is_repetitive_hallucination(chunk_text):
            accepted = []
        if accepted:
            previous_text = chunk_text

        start = end
        progress = 5 + int(start / total * 93) if total else 98
        on_segments(accepted, progress, detected_language or "")

    return {
        "language": detected_language or "unknown",
        "device": device,
        "duration": round(duration, 2),
        "stopped": stopped,
    }


# ── Export formats ─────────────────────────────────────────────────────────


def _timestamp(seconds: float, separator: str = ",") -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{ms:03}"


def _label(segment: dict[str, Any], text: str) -> str:
    return f"{segment['speaker']}: {text}" if segment.get("speaker") else text


def to_txt(segments: list[dict[str, Any]], field: str = "text") -> str:
    lines = [_label(seg, str(seg.get(field, "")).strip()) for seg in segments]
    return "\n".join(line for line in lines if line.strip()) + "\n"


def to_srt(segments: list[dict[str, Any]], field: str = "text") -> str:
    blocks = []
    for index, seg in enumerate(segments, start=1):
        blocks.append(f"{index}\n{_timestamp(seg['start'])} --> {_timestamp(seg['end'])}\n{_label(seg, str(seg.get(field, '')).strip())}")
    return "\n\n".join(blocks) + "\n"


def to_vtt(segments: list[dict[str, Any]], field: str = "text") -> str:
    blocks = ["WEBVTT"]
    for seg in segments:
        blocks.append(f"{_timestamp(seg['start'], '.')} --> {_timestamp(seg['end'], '.')}\n{_label(seg, str(seg.get(field, '')).strip())}")
    return "\n\n".join(blocks) + "\n"
