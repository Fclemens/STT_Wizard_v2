"""NVIDIA Parakeet TDT 0.6B v3 through ONNX Runtime (``onnx-asr``), no PyTorch needed.

* 25 European languages (Bulgarian, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French,
  German, Greek, Hungarian, Italian, Latvian, Lithuanian, Maltese, Polish, Portuguese, Romanian, Slovak,
  Slovenian, Spanish, Swedish, Russian, Ukrainian). It detects the language by itself: the Language box
  is ignored. No translate task, no glossary.
* Weights: the int8 ONNX export ``istupakov/parakeet-tdt-0.6b-v3-onnx`` (~0.7 GB), pinned to one revision.
* Unlike Cohere it returns the time of every token, so lines get real word timings (precise sentence
  splits and precise speaker changes inside a line).
* Like Cohere, the recording is cut at pauses with the Silero voice activity detection from faster-whisper
  and each speech stretch is decoded on its own.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import numpy as np

from cohere_engine import SAMPLE_RATE, clean_text, speech_chunks

REPO = "istupakov/parakeet-tdt-0.6b-v3-onnx"
REVISION = "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce"
FILES = (
    "config.json",
    "vocab.txt",
    "nemo128.onnx",
    "encoder-model.int8.onnx",
    "decoder_joint-model.int8.onnx",
)

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
            model = onnx_asr.load_model("nemo-parakeet-tdt-0.6b-v3", str(directory), quantization="int8")
            _loaded.update(path=str(directory), model=model.with_timestamps())
        return _loaded["model"]


def words_from_tokens(tokens: list[str], times: list[float], chunk_end: float, offset: float) -> list[SimpleNamespace]:
    """Join SentencePiece tokens into words. A token starting with a space begins a new word.

    ``times`` are token start times in seconds from the start of the chunk; a word ends where the next one
    starts (or at the end of the chunk), capped at 0.6 s after its last token so a pause is not stretched.
    """
    words: list[dict[str, Any]] = []
    for token, start in zip(tokens, times):
        if not token:
            continue
        if not words or token.startswith(" "):
            words.append({"text": token, "start": start, "last": start})
        else:
            words[-1]["text"] += token
            words[-1]["last"] = start
    result = []
    for index, word in enumerate(words):
        following = words[index + 1]["start"] if index + 1 < len(words) else chunk_end
        end = min(following, word["last"] + 0.6)
        result.append(SimpleNamespace(word=word["text"], start=word["start"] + offset, end=max(end, word["start"] + 0.05) + offset))
    return result


def run(
    audio: np.ndarray,
    duration: float,
    offset: float,
    model_dir: Path,
    on_progress: Callable[[str, int], None],
    on_segments: Callable[[list[dict[str, Any]], int, str], None],
    should_stop: Callable[[], bool],
) -> dict[str, Any]:
    from stt_engine import SentenceLines

    if not is_downloaded(model_dir):
        on_progress("downloading_model", 3)
    on_progress("loading_model", 4)
    model = get_model(model_dir)
    on_progress("transcribing", 5)

    sentences = SentenceLines()
    counter = 0
    skipped = 0
    stopped = False

    def emit(raw_lines: list[tuple[float, float, str, list]], progress: int) -> None:
        nonlocal counter
        lines = []
        for start, end, text, words in raw_lines:
            counter += 1
            lines.append(
                {
                    "id": f"s{counter:05}",
                    "start": round(start + offset, 2),
                    "end": round(min(end, duration) + offset, 2),
                    "text": text,
                    "original_text": text,
                    "speaker": None,
                    "words": [[round(a + offset, 2), round(b + offset, 2), word] for a, b, word in words],
                }
            )
        on_segments(lines, progress, "auto")

    for start, end in speech_chunks(audio):
        if should_stop():
            stopped = True
            break
        clip = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
        result = model.recognize(clip, sample_rate=SAMPLE_RATE)
        text = clean_text(str(result.text))
        progress = 5 + int(min(end / duration, 1.0) * 93) if duration else 98
        if not text or not result.tokens:
            skipped += 1
            emit([], progress)
            continue
        words = words_from_tokens(list(result.tokens), list(result.timestamps or []), end - start, start)
        emit(sentences.feed(SimpleNamespace(words=words, start=start, end=end, text=text)), progress)
    emit(sentences.flush(), 98)
    return {
        "language": "auto",
        "device": "cpu · int8 (onnx)",
        "glossary_terms": None,
        "skipped_chunks": skipped,
        "stopped": stopped,
    }
