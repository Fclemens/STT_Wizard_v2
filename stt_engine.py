from __future__ import annotations

import json
import os
import re
import uuid
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import torch
import whisper

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mpeg", ".mpga", ".webm"}

MODEL_OPTIONS: dict[str, dict[str, str]] = {
    "tiny": {
        "speed": "~10x",
        "vram": "~1 GB",
        "best_for": "Quick drafts and low-spec devices.",
    },
    "base": {
        "speed": "~7x",
        "vram": "~1 GB",
        "best_for": "Balanced starter model for light workloads.",
    },
    "small": {
        "speed": "~4x",
        "vram": "~2 GB",
        "best_for": "Better quality for meetings and interviews.",
    },
    "medium": {
        "speed": "~2x",
        "vram": "~5 GB",
        "best_for": "High quality multilingual transcription.",
    },
    "large": {
        "speed": "1x",
        "vram": "~10 GB",
        "best_for": "Maximum accuracy when hardware allows.",
    },
    "turbo": {
        "speed": "~8x",
        "vram": "~6 GB",
        "best_for": "Best default for fast English transcription.",
    },
}


def get_model_cache_dir() -> Path:
    if model_dir := os.getenv("WHISPER_MODEL_DIR"):
        return Path(model_dir)
    if xdg_cache_home := os.getenv("XDG_CACHE_HOME"):
        return Path(xdg_cache_home) / "whisper"
    return Path.home() / ".cache" / "whisper"


def get_model_checkpoint_path(model_name: str) -> Path | None:
    model_url = getattr(whisper, "_MODELS", {}).get(model_name)
    if not model_url:
        return None
    return get_model_cache_dir() / Path(model_url).name


def get_model_statuses() -> dict[str, dict[str, str | bool]]:
    statuses: dict[str, dict[str, str | bool]] = {}
    for model_name, meta in MODEL_OPTIONS.items():
        checkpoint_path = get_model_checkpoint_path(model_name)
        is_downloaded = bool(checkpoint_path and checkpoint_path.exists())
        statuses[model_name] = {
            **meta,
            "downloaded": is_downloaded,
            "status_label": "Downloaded" if is_downloaded else "Needs first download",
            "cache_path": str(checkpoint_path) if checkpoint_path else "",
        }
    return statuses


def detect_device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


@lru_cache(maxsize=6)
def get_model(model_name: str):
    return whisper.load_model(model_name, device=detect_device())


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def format_timestamp_vtt(seconds: float) -> str:
    return format_timestamp(seconds).replace(",", ".")


def write_txt(path: Path, text: str) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8")


def write_srt(path: Path, segments: list[dict[str, Any]]) -> None:
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_timestamp(segment['start'])} --> {format_timestamp(segment['end'])}",
                    segment["text"].strip(),
                ]
            )
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def write_vtt(path: Path, segments: list[dict[str, Any]]) -> None:
    blocks = ["WEBVTT\n"]
    for segment in segments:
        blocks.append(
            "\n".join(
                [
                    f"{format_timestamp_vtt(segment['start'])} --> {format_timestamp_vtt(segment['end'])}",
                    segment["text"].strip(),
                ]
            )
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_wav(path: Path, audio: np.ndarray, sample_rate: int = whisper.audio.SAMPLE_RATE) -> None:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def is_repetitive_hallucination(text: str) -> bool:
    words = re.findall(r"\b[\wÀ-ÿ']+\b", text.lower())
    if len(words) < 18:
        return False
    unique_ratio = len(set(words)) / len(words)
    if unique_ratio < 0.22:
        return True

    for size in (2, 3, 4, 5):
        grams = [" ".join(words[index : index + size]) for index in range(len(words) - size + 1)]
        if not grams:
            continue
        most_common_count = max(grams.count(gram) for gram in set(grams))
        if most_common_count >= 6 or most_common_count / len(grams) > 0.35:
            return True
    return False


def is_low_confidence_segment(segment: dict[str, Any]) -> bool:
    text = str(segment.get("text", "")).strip()
    if not text:
        return True
    no_speech_prob = float(segment.get("no_speech_prob") or 0.0)
    avg_logprob = float(segment.get("avg_logprob") or 0.0)
    compression_ratio = float(segment.get("compression_ratio") or 0.0)
    if no_speech_prob > 0.75 and avg_logprob < -0.6:
        return True
    if compression_ratio > 2.8:
        return True
    return is_repetitive_hallucination(text)


def transcribe_audio(
    audio_path: Path,
    output_dir: Path,
    model_name: str,
    language: str | None,
    task: str,
    temperature: float,
    word_timestamps: bool,
) -> dict[str, Any]:
    model = get_model(model_name)
    job_id = uuid.uuid4().hex[:12]
    job_dir = output_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    result = model.transcribe(
        str(audio_path),
        language=language,
        task=task,
        temperature=temperature,
        word_timestamps=word_timestamps,
        verbose=False,
    )

    payload = {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "device": detect_device(),
        "model": model_name,
        "language": result.get("language", language or "auto"),
        "task": task,
        "text": result["text"].strip(),
        "segments": result.get("segments", []),
        "source_file": audio_path.name,
        "artifacts": {
            "txt": f"/download/{job_id}/txt",
            "srt": f"/download/{job_id}/srt",
            "vtt": f"/download/{job_id}/vtt",
            "json": f"/download/{job_id}/json",
        },
    }

    write_txt(job_dir / "transcript.txt", payload["text"])
    write_srt(job_dir / "transcript.srt", payload["segments"])
    write_vtt(job_dir / "transcript.vtt", payload["segments"])
    write_json(job_dir / "transcript.json", payload)
    return payload


def transcribe_audio_incremental(
    audio_path: Path,
    output_dir: Path,
    model_name: str,
    language: str | None,
    task: str,
    temperature: float,
    word_timestamps: bool,
    progress_callback,
    should_stop,
    chunk_seconds: int = 30,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job_dir = output_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    progress_callback(stage="loading_model", progress=5, text="")
    model = get_model(model_name)

    progress_callback(stage="loading_audio", progress=10, text="")
    audio = whisper.load_audio(str(audio_path))
    sample_rate = whisper.audio.SAMPLE_RATE
    chunk_size = chunk_seconds * sample_rate
    total_samples = len(audio)
    total_chunks = max(1, (total_samples + chunk_size - 1) // chunk_size)

    collected_segments: list[dict[str, Any]] = []
    collected_text: list[str] = []
    detected_language = language or "auto"
    current_time = 0.0

    for index in range(total_chunks):
        if should_stop():
            break
        start = index * chunk_size
        stop = min(total_samples, start + chunk_size)
        audio_chunk = audio[start:stop]
        chunk_path = job_dir / f"chunk-{index:04}.wav"
        write_wav(chunk_path, audio_chunk, sample_rate=sample_rate)

        progress_start = 10 + int((index / total_chunks) * 80)
        progress_callback(
            stage="transcribing",
            progress=progress_start,
            text=" ".join(collected_text).strip(),
        )

        result = model.transcribe(
            str(chunk_path),
            language=language,
            task=task,
            temperature=temperature,
            word_timestamps=word_timestamps,
            verbose=False,
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
            hallucination_silence_threshold=2.0,
        )
        detected_language = result.get("language", detected_language)
        accepted_chunk_text: list[str] = []
        accepted_chunk_segments: list[dict[str, Any]] = []
        for segment in result.get("segments", []):
            if is_low_confidence_segment(segment):
                continue
            segment_copy = dict(segment)
            segment_copy["start"] = segment_copy.get("start", 0.0) + current_time
            segment_copy["end"] = segment_copy.get("end", 0.0) + current_time
            segment_text = str(segment_copy.get("text", "")).strip()
            if segment_text:
                accepted_chunk_text.append(segment_text)
                accepted_chunk_segments.append(segment_copy)
        chunk_text = " ".join(accepted_chunk_text).strip()
        if chunk_text and not is_repetitive_hallucination(chunk_text):
            collected_segments.extend(accepted_chunk_segments)
            collected_text.append(chunk_text)
        current_time += len(audio_chunk) / sample_rate
        progress_callback(
            stage="transcribing",
            progress=10 + int(((index + 1) / total_chunks) * 80),
            text="\n".join(collected_text).strip(),
        )

    final_text = "\n".join(collected_text).strip()
    payload = {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "device": detect_device(),
        "model": model_name,
        "language": detected_language,
        "task": task,
        "text": final_text,
        "segments": collected_segments,
        "source_file": audio_path.name,
        "artifacts": {
            "txt": f"/download/{job_id}/txt",
            "srt": f"/download/{job_id}/srt",
            "vtt": f"/download/{job_id}/vtt",
            "json": f"/download/{job_id}/json",
        },
        "stopped": should_stop(),
    }

    progress_callback(stage="writing_outputs", progress=95, text=final_text)
    write_txt(job_dir / "transcript.txt", payload["text"])
    write_srt(job_dir / "transcript.srt", payload["segments"])
    write_vtt(job_dir / "transcript.vtt", payload["segments"])
    write_json(job_dir / "transcript.json", payload)
    progress_callback(stage="completed" if not payload["stopped"] else "stopped", progress=100, text=final_text)
    return payload
