"""Local Whisper transcription with faster-whisper (CTranslate2).

* Runs on the NVIDIA GPU when one is usable (int8/float16, so large-v3-turbo
  fits in ~2 GB of VRAM), otherwise on the CPU (int8). If the GPU fails at
  runtime (missing CUDA libraries, out of memory) it falls back to the CPU.
* Models are downloaded on first use as plain files into ``models/<name>``
  next to the app (portable; no Hugging Face cache or symlinks).
* Lines stream out as they are decoded; the whole recording is one pass, so
  Whisper keeps context across it. The glossary goes in as ``hotwords``,
  which are added to the prompt of every 30 s window.
* Hallucination guards: voice-activity detection skips silence, and the
  silence guard (``hallucination_silence_threshold``) drops text invented
  over pauses. Without it, carrying context made turbo produce long runs of
  made-up text on our test recording; with it, turbo scored 2.7% WER.
* Word timestamps are used to cut the text into sentence lines, also across
  Whisper's segment boundaries (which often fall mid-sentence).
"""

from __future__ import annotations

import os
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import av
import numpy as np

from paths import APP_DIR, RESOURCE_DIR

MODELS_DIR = Path(os.getenv("WHISPER_MODEL_DIR") or APP_DIR / "models")

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mpeg", ".mpga", ".webm", ".ogg", ".flac"}
ALLOWED_TEXT_EXTENSIONS = {".txt", ".md"}
SAMPLE_RATE = 16000

# key -> faster-whisper model name. Keys stay stable for saved settings.
MODEL_OPTIONS: dict[str, dict[str, str]] = {
    "tiny": {"model": "tiny", "size": "75 MB", "best_for": "Quick drafts and low-spec devices."},
    "base": {"model": "base", "size": "145 MB", "best_for": "Light workloads, fast on CPU."},
    "small": {"model": "small", "size": "480 MB", "best_for": "Better quality for meetings on CPU."},
    "medium": {"model": "medium", "size": "1.5 GB", "best_for": "High quality multilingual."},
    "large": {"model": "large-v3", "size": "3 GB", "best_for": "Maximum accuracy; slow without a GPU."},
    "turbo": {"model": "turbo", "size": "1.6 GB", "best_for": "Best default: near large-v3 accuracy, fast."},
}


# ── Device selection ───────────────────────────────────────────────────────

_cuda_state: dict[str, Any] = {"checked": False, "usable": False, "broken": False}
_cuda_lock = threading.Lock()


def _add_cuda_dll_dirs() -> None:
    """Make the cuBLAS/cuDNN DLLs from the nvidia-* wheels loadable on Windows."""
    if os.name != "nt":
        return
    candidates: list[Path] = []
    try:
        import nvidia  # namespace package from nvidia-cublas-cu12 / nvidia-cudnn-cu12

        for base in nvidia.__path__:
            candidates += list(Path(base).glob("*/bin"))
    except ImportError:
        pass
    candidates += list((RESOURCE_DIR / "nvidia").glob("*/bin"))  # packaged app layout
    for directory in candidates:
        if directory.is_dir():
            os.add_dll_directory(str(directory))
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")


def cuda_usable() -> bool:
    with _cuda_lock:
        if not _cuda_state["checked"]:
            _cuda_state["checked"] = True
            try:
                _add_cuda_dll_dirs()
                import ctranslate2

                _cuda_state["usable"] = ctranslate2.get_cuda_device_count() > 0
            except Exception:  # noqa: BLE001
                _cuda_state["usable"] = False
        return _cuda_state["usable"] and not _cuda_state["broken"]


def _compute_type(device: str) -> str:
    import ctranslate2

    supported = ctranslate2.get_supported_compute_types(device)
    preferences = ["int8_float16", "float16", "int8", "float32"] if device == "cuda" else ["int8", "float32"]
    return next((kind for kind in preferences if kind in supported), "default")


def device_label() -> str:
    return "cuda" if cuda_usable() else "cpu"


# ── Models ─────────────────────────────────────────────────────────────────


def model_dir(key: str) -> Path:
    return MODELS_DIR / MODEL_OPTIONS[key]["model"]


def is_downloaded(key: str) -> bool:
    return (model_dir(key) / "model.bin").exists()


def get_model_statuses() -> dict[str, dict[str, Any]]:
    return {key: {**meta, "downloaded": is_downloaded(key)} for key, meta in MODEL_OPTIONS.items()}


_model_lock = threading.Lock()
_loaded: dict[str, Any] = {"key": None, "device": None, "model": None}


def _ensure_downloaded(key: str) -> Path:
    from faster_whisper.utils import download_model

    target = model_dir(key)
    if not is_downloaded(key):
        target.mkdir(parents=True, exist_ok=True)
        download_model(MODEL_OPTIONS[key]["model"], output_dir=str(target))
    return target


# Whisper's decoder window is 448 tokens. As in OpenAI's Whisper, the prompt (glossary + text carried over
# from the previous window) may use at most half of it, so ~224 tokens always remain for the new text.
# faster-whisper allows 223 for the glossary *plus* 223 for the context: with a long glossary the prompt
# exceeds 448 and decoding fails with "The maximum decoding length must be > 0".
PROMPT_TOKEN_BUDGET = 223
GLOSSARY_MAX_TOKENS = 150  # leaves at least ~73 tokens of running context


@lru_cache(maxsize=1)
def _model_class():
    from faster_whisper import WhisperModel

    class BudgetedWhisperModel(WhisperModel):
        """WhisperModel whose glossary + context prompt never exceeds PROMPT_TOKEN_BUDGET."""

        def get_prompt(self, tokenizer, previous_tokens, without_timestamps=False, prefix=None, hotwords=None):
            if hotwords and not prefix:
                room = PROMPT_TOKEN_BUDGET - len(tokenizer.encode(" " + hotwords.strip()))
                previous_tokens = previous_tokens[-room:] if room > 0 else []
            return super().get_prompt(
                tokenizer, previous_tokens, without_timestamps=without_timestamps, prefix=prefix, hotwords=hotwords
            )

    return BudgetedWhisperModel


def get_model(key: str, device: str):
    """Load (or reuse) one model; only one is kept in memory at a time."""
    WhisperModel = _model_class()

    with _model_lock:
        if _loaded["key"] == key and _loaded["device"] == device:
            return _loaded["model"]
        _loaded.update(key=None, device=None, model=None)  # free VRAM before loading
        path = _ensure_downloaded(key)
        model = WhisperModel(str(path), device=device, compute_type=_compute_type(device))
        _loaded.update(key=key, device=device, model=model)
        return model


# ── Audio ──────────────────────────────────────────────────────────────────


def load_audio(path: Path, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any audio/video file to mono float32 at ``rate`` (PyAV, no FFmpeg install)."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
    chunks: list[np.ndarray] = []
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("The file has no audio track.")
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


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


def is_low_confidence_segment(segment) -> bool:
    text = str(segment.text).strip()
    if not text:
        return True
    if segment.no_speech_prob > 0.75 and segment.avg_logprob < -0.6:
        return True
    if segment.compression_ratio > 2.8:
        return True
    return is_repetitive_hallucination(text)


# ── Transcription ──────────────────────────────────────────────────────────


def glossary_hotwords(glossary: str, model) -> tuple[str | None, int, int]:
    """Whole glossary terms, in order, that fit in GLOSSARY_MAX_TOKENS. Returns (hotwords, used, total)."""
    terms = [line.strip() for line in glossary.splitlines() if line.strip()]
    kept: list[str] = []
    for term in terms:
        candidate = ", ".join(kept + [term])
        if len(model.hf_tokenizer.encode(" " + candidate, add_special_tokens=False).ids) > GLOSSARY_MAX_TOKENS:
            break
        kept.append(term)
    return (", ".join(kept) or None), len(kept), len(terms)


MAX_LINE_SECONDS = 20.0
MAX_JOIN_GAP_SECONDS = 1.5


class SentenceLines:
    """Turn Whisper's word stream into sentence lines.

    Whisper's segment boundaries often fall mid-sentence, so an unfinished
    sentence is held back and continued with the next segment's words. It is
    emitted on its own after a pause, after MAX_LINE_SECONDS, or at the end.
    """

    def __init__(self) -> None:
        self.pending: list = []

    def feed(self, segment) -> list[tuple[float, float, str]]:
        words = list(segment.words or [])
        if not words:  # no word timings: keep the segment as one line
            return self.flush() + [(float(segment.start), float(segment.end), segment.text.strip())]
        lines: list[tuple[float, float, str]] = []
        if self.pending and words[0].start - self.pending[-1].end > MAX_JOIN_GAP_SECONDS:
            lines += self.flush()
        for word in words:
            self.pending.append(word)
            sentence_end = word.word.strip().endswith((".", "?", "!")) and len(self.pending) >= 3
            too_long = self.pending[-1].end - self.pending[0].start >= MAX_LINE_SECONDS
            if sentence_end or too_long:
                lines += self.flush()
        return lines

    def flush(self) -> list[tuple[float, float, str]]:
        words, self.pending = self.pending, []
        text = "".join(w.word for w in words).strip()
        return [(float(words[0].start), float(words[-1].end), text)] if text else []


class _CudaFailure(Exception):
    pass


def _looks_like_cuda_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(word in message for word in ("cuda", "cublas", "cudnn", "out of memory"))


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
    """Transcribe ``audio_path``, streaming accepted lines via ``on_segments``.

    Returns metadata (language, device, duration, stopped).
    """
    on_progress("loading_audio", 2)
    audio = load_audio(audio_path)
    duration = len(audio) / SAMPLE_RATE

    device = "cuda" if cuda_usable() else "cpu"
    if not is_downloaded(model_name):
        on_progress("downloading_model", 3)
    on_progress("loading_model", 4)
    try:
        return _run(audio, duration, device, model_name, language, task, glossary, accurate, on_progress, on_segments, should_stop)
    except _CudaFailure:
        # GPU unusable at runtime: remember it and redo the whole job on the CPU.
        _cuda_state["broken"] = True
        on_progress("loading_model", 4)
        return _run(audio, duration, "cpu", model_name, language, task, glossary, accurate, on_progress, on_segments, should_stop)


def _run(audio, duration, device, model_name, language, task, glossary, accurate, on_progress, on_segments, should_stop):
    try:
        model = get_model(model_name, device)
        hotwords, terms_used, terms_total = glossary_hotwords(glossary, model)
        segments, info = model.transcribe(
            audio,
            language=language or None,
            task=task,
            beam_size=5 if accurate else 1,
            best_of=5 if accurate else 1,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0] if accurate else 0.0,
            hotwords=hotwords,
            condition_on_previous_text=True,
            word_timestamps=True,
            hallucination_silence_threshold=2.0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
        )
        on_progress("transcribing", 5)
        sentences = SentenceLines()
        counter = 0
        stopped = False
        emitted = False

        def emit(raw_lines: list[tuple[float, float, str]], progress: int) -> None:
            nonlocal counter
            lines = []
            for start, end, text in raw_lines:
                counter += 1
                lines.append(
                    {
                        "id": f"s{counter:05}",
                        "start": round(start, 2),
                        "end": round(min(end, duration), 2),
                        "text": text,
                        "original_text": text,
                        "speaker": None,
                    }
                )
            on_segments(lines, progress, info.language)

        for segment in segments:
            if should_stop():
                stopped = True
                break
            progress = 5 + int(min(segment.end / duration, 1.0) * 93) if duration else 98
            if is_low_confidence_segment(segment):
                emit([], progress)
                continue
            emit(sentences.feed(segment), progress)
            emitted = True
        emit(sentences.flush(), 98)
    except Exception as exc:
        if device == "cuda" and not emitted and _looks_like_cuda_error(exc):
            raise _CudaFailure from exc
        raise
    return {
        "language": info.language or "unknown",
        "device": f"{device} · {_compute_type(device)}",
        "duration": round(duration, 2),
        "glossary_terms": f"{terms_used}/{terms_total}" if terms_total else None,
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
