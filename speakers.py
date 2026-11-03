"""Speaker detection: which voice is speaking on each transcript line.

* A voice "fingerprint" (speaker embedding: 3D-Speaker ERes2Net run by sherpa-onnx,
  ~40 MB, downloaded on first use) is computed over short windows of each line.
  On a real Teams call it told the user's voice from the others' far better
  than WeSpeaker ResNet34 or TitaNet (same-vs-different separation 0.99).
* Lines are grouped by voice: agglomerative clustering on cosine similarity,
  stopping when the closest groups are no longer similar enough (or at the
  number of speakers the user asked for).
* Very short lines ("yes", "ok") have too little sound for a reliable
  fingerprint; they take the speaker of the nearest line.
* A long line where the voice clearly changes part-way is split at the word
  where the change happens (when Whisper's word timings are available).

Speakers are anonymous ("Speaker 1", "Speaker 2"…): the user names them.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np
import requests

from stt_engine import MODELS_DIR

MODEL_FILE = "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_FILE}"
MODEL_PATH = MODELS_DIR / "speakers" / MODEL_FILE

WINDOW_SECONDS = 2.0        # fingerprint window...
STEP_SECONDS = 1.0          # ...moved by this much along a line
MIN_LINE_SECONDS = 0.8      # shorter lines take their neighbour's speaker
SAME_VOICE = 0.45           # cosine similarity above which two groups are merged
MIN_SPEAKER_SECONDS = 10.0  # auto mode: smaller groups are folded into the closest one...
MIN_SPEAKER_SHARE = 0.02    # ...unless they hold this share of the talk time
CHUNK_WINDOWS = 3           # windows per chunk (~4 s): the unit that is grouped by voice
SPLIT_MIN_SECONDS = 3.0     # a voice change inside a line must last this long to split it

_extractor = None
_lock = threading.Lock()


class SpeakerError(RuntimeError):
    pass


def model_ready() -> bool:
    return MODEL_PATH.exists()


def _download_model() -> None:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    partial = MODEL_PATH.with_suffix(".part")
    try:
        with requests.get(MODEL_URL, stream=True, timeout=60) as response:
            response.raise_for_status()
            with partial.open("wb") as target:
                for chunk in response.iter_content(1024 * 1024):
                    target.write(chunk)
    except requests.RequestException as exc:
        partial.unlink(missing_ok=True)
        raise SpeakerError(f"Could not download the speaker model: {exc}") from exc
    partial.replace(MODEL_PATH)


def _get_extractor():
    global _extractor
    with _lock:
        if _extractor is None:
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise SpeakerError("Speaker detection needs sherpa-onnx. Run START_STT_WIZARD.bat to install it.") from exc
            if not model_ready():
                _download_model()
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL_PATH), num_threads=4)
            _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        return _extractor


def _embed(extractor, samples: np.ndarray, rate: int) -> np.ndarray:
    stream = extractor.create_stream()
    stream.accept_waveform(rate, samples)
    stream.input_finished()
    vector = np.asarray(extractor.compute(stream), dtype=np.float32)
    return vector / (np.linalg.norm(vector) + 1e-9)


def _windows(start: float, end: float) -> list[tuple[float, float]]:
    if end - start <= WINDOW_SECONDS * 1.5:
        return [(start, end)]
    spans = []
    at = start
    while at + WINDOW_SECONDS <= end + 1e-6:
        spans.append((at, at + WINDOW_SECONDS))
        at += STEP_SECONDS
    if spans[-1][1] < end - 0.3:
        spans.append((end - WINDOW_SECONDS, end))
    return spans


def cluster(vectors: np.ndarray, weights: np.ndarray, count: int | None) -> np.ndarray:
    """Group unit vectors (one per line, weighted by duration). Returns a group index per row.

    Merges the two most similar groups until they are no longer similar enough (auto)
    or until ``count`` real speakers remain. Groups with too little talk time (a cough,
    noise, a briefly distorted voice) are then folded into the closest real speaker;
    they never count as one of the ``count`` speakers.
    """
    n = len(vectors)
    if n == 0:
        return np.zeros(0, dtype=int)
    grand = float(weights.sum())
    tiny = max(MIN_SPEAKER_SECONDS, MIN_SPEAKER_SHARE * grand)
    sums = vectors * weights[:, None]
    centroids = sums / (np.linalg.norm(sums, axis=1, keepdims=True) + 1e-9)
    similarity = centroids @ centroids.T
    np.fill_diagonal(similarity, -np.inf)
    group = np.arange(n)
    total = weights.copy()
    alive = np.ones(n, dtype=bool)
    # Count mode: remember the last state with exactly ``count`` real speakers.
    best = None

    def real_speakers() -> int:
        return int(((total >= tiny) & alive).sum())

    while alive.sum() > 1:
        if count and real_speakers() == count:
            best = (group.copy(), sums.copy(), total.copy(), alive.copy())
        i, j = np.unravel_index(int(np.argmax(similarity)), similarity.shape)
        if not count and similarity[i, j] < SAME_VOICE:
            break
        if count and best is not None and real_speakers() < count:
            break
        sums[i] += sums[j]
        total[i] += total[j]
        group[group == j] = i
        alive[j] = False
        centroids[i] = sums[i] / (np.linalg.norm(sums[i]) + 1e-9)
        row = np.where(alive, centroids @ centroids[i], -np.inf)
        row[i] = -np.inf
        similarity[i, :] = row
        similarity[:, i] = row
        similarity[j, :] = -np.inf
        similarity[:, j] = -np.inf
    if best is not None:
        group, sums, total, alive = best
        centroids = sums / (np.linalg.norm(sums, axis=1, keepdims=True) + 1e-9)

    while True:
        roots = np.unique(group)
        small = [r for r in roots if total[r] < tiny]
        big = [r for r in roots if total[r] >= tiny]
        if not small or not big:
            break
        r = min(small, key=lambda root: total[root])
        closest = max(big, key=lambda root: float(centroids[r] @ centroids[root]))
        sums[closest] += sums[r]
        total[closest] += total[r]
        group[group == r] = closest
        centroids[closest] = sums[closest] / (np.linalg.norm(sums[closest]) + 1e-9)
    return group


def detect(
    segments: list[dict[str, Any]],
    audio: np.ndarray,
    rate: int,
    count: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> tuple[dict[str, int], dict[str, list[tuple[float, int]]]]:
    """Group the given lines by voice.

    Returns (speaker number per line id, numbered 1.. by first appearance;
    voice changes inside lines: line id -> [(time, speaker), ...] after the first part).
    """
    extractor = _get_extractor()
    timed = [seg for seg in segments if seg.get("start") is not None and seg["end"] - seg["start"] >= MIN_LINE_SECONDS]
    windows: list[tuple[int, float, float]] = []  # (line index, start, end)
    for index, seg in enumerate(timed):
        windows += [(index, a, b) for a, b in _windows(float(seg["start"]), float(seg["end"]))]

    vectors = np.zeros((len(windows), 0), dtype=np.float32)
    for done, (_, a, b) in enumerate(windows):
        samples = audio[int(a * rate) : int(b * rate)]
        vector = _embed(extractor, samples if len(samples) else np.zeros(rate // 2, dtype=np.float32), rate)
        if not vectors.shape[1]:
            vectors = np.zeros((len(windows), len(vector)), dtype=np.float32)
        vectors[done] = vector
        if on_progress and done % 25 == 0:
            on_progress(int(done / max(len(windows), 1) * 100))
    if not timed:
        return {}, {}

    # Chunks of up to CHUNK_WINDOWS windows (~4 s) inside each line are what gets grouped:
    # a long line where two people speak must not blur into one averaged voice.
    owner = np.array([w[0] for w in windows])
    chunks: list[tuple[int, float, float, np.ndarray]] = []  # (line index, start, end, vector)
    for index, seg in enumerate(timed):
        rows = np.where(owner == index)[0]
        for k in range(0, len(rows), CHUNK_WINDOWS):
            part = rows[k : k + CHUNK_WINDOWS]
            mean = vectors[part].mean(axis=0)
            start, end = windows[part[0]][1], min(windows[part[-1]][2], float(seg["end"]))
            chunks.append((index, start, end, mean / (np.linalg.norm(mean) + 1e-9)))
    chunk_vectors = np.stack([c[3] for c in chunks])
    weights = np.array([max(c[2] - c[1], 0.1) for c in chunks], dtype=np.float32)
    groups = cluster(chunk_vectors, weights, count)

    # Number speakers 1.. in order of first appearance.
    numbers: dict[int, int] = {}
    for g in groups:
        numbers.setdefault(int(g), len(numbers) + 1)

    # Each line: the speaker of most of its time; lasting changes inside it are reported for splitting.
    by_line: dict[str, int] = {}
    changes: dict[str, list[tuple[float, int]]] = {}
    for index, seg in enumerate(timed):
        mine = [(chunks[i][1], chunks[i][2] - chunks[i][1], numbers[int(groups[i])]) for i in range(len(chunks)) if chunks[i][0] == index]
        talk: dict[int, float] = {}
        for _, seconds, number in mine:
            talk[number] = talk.get(number, 0.0) + seconds
        by_line[seg["id"]] = max(talk, key=talk.get)
        parts = _runs(mine)
        if len(parts) > 1:
            by_line[seg["id"]] = parts[0][1]
            changes[seg["id"]] = parts[1:]

    # Short lines: speaker of the nearest line in time.
    labelled = [seg for seg in segments if seg["id"] in by_line]
    for seg in segments:
        if seg["id"] in by_line or seg.get("start") is None or not labelled:
            continue
        nearest_line = min(labelled, key=lambda other: _gap(seg, other))
        by_line[seg["id"]] = _last_speaker(nearest_line, by_line, changes) if nearest_line["end"] <= seg["start"] else by_line[nearest_line["id"]]
    return by_line, changes


def _runs(chunks: list[tuple[float, float, int]]) -> list[tuple[float, int]]:
    """Lasting runs of the same speaker across a line's chunks (start, seconds, speaker): [(start, speaker), ...]."""
    runs: list[list] = []
    for start, seconds, label in chunks:
        if runs and runs[-1][1] == label:
            runs[-1][2] += seconds
        else:
            runs.append([start, label, seconds])
    lasting = [run for run in runs if run[2] >= SPLIT_MIN_SECONDS]
    merged: list[tuple[float, int]] = []
    for start, label, _ in lasting:
        if not merged or merged[-1][1] != label:
            merged.append((start, label))
    return merged


def _gap(a: dict[str, Any], b: dict[str, Any]) -> float:
    return max(0.0, max(a["start"], b["start"]) - min(a["end"], b["end"]))


def _last_speaker(seg: dict[str, Any], by_line: dict[str, int], changes: dict[str, list[tuple[float, int]]]) -> int:
    parts = changes.get(seg["id"])
    return parts[-1][1] if parts else by_line[seg["id"]]


def split_line(segment: dict[str, Any], changes: list[tuple[float, int]], first: int) -> list[tuple[dict[str, Any], int]]:
    """Split a line where the voice changes. Needs Whisper's word timings.

    Window times are coarse (±1 s), so each cut goes to the most natural place near the
    change: after a sentence end or before the longest pause, else the nearest word.
    """
    words = segment.get("words") or []
    if len(words) < 2 or segment.get("text") != segment.get("original_text"):
        return [(segment, first)]
    cuts: list[tuple[int, int]] = []  # (index of the first word of the new piece, speaker)
    for at, speaker in changes:
        lowest = cuts[-1][0] + 1 if cuts else 1
        candidates = [i for i in range(lowest, len(words)) if abs(words[i][0] - at) <= 1.5]
        if not candidates:
            continue

        def score(i: int) -> float:
            pause = words[i][0] - words[i - 1][1]
            sentence = words[i - 1][2].strip().endswith((".", "?", "!", "。", "？", "！"))
            return pause + (1.0 if sentence else 0.0) - abs(words[i][0] - at) * 0.1

        cuts.append((max(candidates, key=score), speaker))
    pieces: list[tuple[list, int]] = []
    bounds = [(0, first)] + cuts
    for (begin, speaker), (end, _) in zip(bounds, bounds[1:] + [(len(words), 0)]):
        pieces.append((words[begin:end], speaker))
    result = []
    for index, (chunk, speaker) in enumerate(p for p in pieces if p[0]):
        text = "".join(w[2] for w in chunk).strip()
        if not text:
            continue
        line = {
            **segment,
            "id": segment["id"] if index == 0 else f"{segment['id']}-{index}",
            "start": round(chunk[0][0], 2),
            "end": round(chunk[-1][1], 2),
            "text": text,
            "original_text": text,
            "words": chunk,
        }
        result.append((line, speaker))
    if len(result) < 2:
        return [(segment, first)]
    return result
