"""Audio capture from a microphone and/or system audio (WASAPI loopback).

Each source is written to its own mono WAV track at the device's native rate.
Both tracks share one recording clock, so they stay aligned:

  * Windows sends *no* loopback data while nothing is playing. When data
    resumes, the gap is filled with silence based on wall-clock time.
  * Pausing shifts the clock, so paused time is not padded.

On stop, the tracks are mixed into a 16 kHz mono file for Whisper.
Tracks are kept for speaker labelling (mic = "Me", system = "Others").
"""

from __future__ import annotations

import queue
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover - non-Windows or not installed
    pyaudio = None

GAP_TOLERANCE_SECONDS = 0.15
LEVEL_INTERVAL_SECONDS = 0.1
TRACK_NAMES = ("mic", "system")


class RecorderError(RuntimeError):
    pass


# ── Devices ────────────────────────────────────────────────────────────────


def list_devices() -> dict[str, Any]:
    if pyaudio is None:
        return {"available": False, "error": "PyAudioWPatch is not installed. Run START_STT_WIZARD.bat to update.", "mics": [], "systems": []}
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_mic = wasapi.get("defaultInputDevice", -1)
        try:
            default_loopback = pa.get_default_wasapi_loopback()["index"]
        except Exception:  # noqa: BLE001 - no output device
            default_loopback = -1
        mics, systems = [], []
        for index in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(index)
            if info.get("hostApi") != wasapi["index"] or int(info.get("maxInputChannels", 0)) < 1:
                continue
            entry = {"id": index, "name": _clean_name(info["name"])}
            if info.get("isLoopbackDevice"):
                entry["name"] = entry["name"].replace(" [Loopback]", "")
                entry["default"] = index == default_loopback
                systems.append(entry)
            else:
                entry["default"] = index == default_mic
                mics.append(entry)
        return {"available": True, "error": None, "mics": mics, "systems": systems}
    finally:
        pa.terminate()


def _clean_name(name: str) -> str:
    # PortAudio sometimes hands back UTF-8 bytes decoded as cp1252.
    try:
        name = name.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # Characters already lost upstream (e.g. the ® in "Intel® Smart Sound").
    return " ".join(name.replace("�", "").split())


# ── Track writer ───────────────────────────────────────────────────────────


class Track:
    """One source: a PyAudio stream feeding a WAV file through a queue."""

    def __init__(self, name: str, device: dict[str, Any], path: Path, clock: "Clock", write: bool) -> None:
        self.name = name
        self.device_name = _clean_name(device["name"]).replace(" [Loopback]", "")
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, min(int(device["maxInputChannels"]), 8))
        self.device_index = int(device["index"])
        self.path = path
        self.clock = clock
        self.write_enabled = write
        self.level = 0.0
        self.written = 0  # frames
        self.queue: "queue.Queue[tuple[float, bytes] | None]" = queue.Queue()
        self.wav: wave.Wave_write | None = None
        self.stream = None
        self.thread = threading.Thread(target=self._drain, name=f"track-{name}", daemon=True)

    def open(self, pa) -> None:
        if self.write_enabled:
            self.wav = wave.open(str(self.path), "wb")
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(self.rate)
        self.stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.device_index,
            frames_per_buffer=int(self.rate * 0.05),
            stream_callback=self._callback,
        )
        self.thread.start()

    def _callback(self, in_data, frame_count, time_info, status):
        self.queue.put((time.monotonic(), in_data))
        return (None, pyaudio.paContinue)

    def _drain(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            arrived, data = item
            samples = np.frombuffer(data, dtype=np.int16)
            if self.channels > 1:
                samples = samples.reshape(-1, self.channels).mean(axis=1).astype(np.int16)
            if samples.size:
                peak = float(np.abs(samples).max()) / 32768.0
                self.level = max(peak, self.level * 0.85)
            position = self.clock.position(arrived)
            if position is None or not self.write_enabled:
                continue  # paused or monitoring only
            start = position - samples.size / self.rate
            self._pad_to(start)
            self.wav.writeframes(samples.tobytes())
            self.written += samples.size

    def _pad_to(self, seconds: float) -> None:
        missing = int(seconds * self.rate) - self.written
        if missing > GAP_TOLERANCE_SECONDS * self.rate:
            self._write_silence(missing)

    def _write_silence(self, frames: int) -> None:
        chunk = self.rate  # one second at a time
        while frames > 0:
            n = min(chunk, frames)
            self.wav.writeframes(b"\x00\x00" * n)
            self.written += n
            frames -= n

    def close(self, final_seconds: float | None) -> None:
        if self.stream is not None:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:  # noqa: BLE001
                pass
        self.queue.put(None)
        self.thread.join(timeout=5)
        if self.wav is not None:
            if final_seconds is not None:
                self._pad_to(final_seconds)
            self.wav.close()

    @property
    def seconds(self) -> float:
        return self.written / self.rate


class Clock:
    """Recording time that excludes pauses."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.paused_total = 0.0
        self.paused_at: float | None = None
        self.lock = threading.Lock()

    def position(self, moment: float) -> float | None:
        with self.lock:
            if self.paused_at is not None:
                return None
            return moment - self.started - self.paused_total

    def elapsed(self) -> float:
        with self.lock:
            now = self.paused_at if self.paused_at is not None else time.monotonic()
            return now - self.started - self.paused_total

    def pause(self) -> None:
        with self.lock:
            if self.paused_at is None:
                self.paused_at = time.monotonic()

    def resume(self) -> None:
        with self.lock:
            if self.paused_at is not None:
                self.paused_total += time.monotonic() - self.paused_at
                self.paused_at = None

    @property
    def paused(self) -> bool:
        return self.paused_at is not None


# ── Capture session ────────────────────────────────────────────────────────


class Capture:
    def __init__(
        self,
        owner: str,
        folder: Path | None,
        mic: int | None,
        system: int | None,
        on_levels: Callable[[dict[str, Any]], None],
        write: bool = True,
    ) -> None:
        if pyaudio is None:
            raise RecorderError("Recording needs PyAudioWPatch. Run START_STT_WIZARD.bat to install it.")
        if mic is None and system is None:
            raise RecorderError("Choose a microphone, system audio, or both.")
        self.owner = owner
        self.folder = folder
        self.write = write
        self.on_levels = on_levels
        self.pa = pyaudio.PyAudio()
        self.clock = Clock()
        self.tracks: dict[str, Track] = {}
        self.stopped = threading.Event()
        try:
            for name, index in (("mic", mic), ("system", system)):
                if index is None:
                    continue
                device = self.pa.get_device_info_by_index(int(index))
                path = folder / f"{name}.wav" if folder else Path(f"{name}.wav")
                self.tracks[name] = Track(name, device, path, self.clock, write)
            for track in self.tracks.values():
                track.open(self.pa)
        except Exception as exc:
            self._abort()
            raise RecorderError(f"Could not open the audio device: {exc}") from exc
        self.ticker = threading.Thread(target=self._tick, name="levels", daemon=True)
        self.ticker.start()

    def _tick(self) -> None:
        while not self.stopped.wait(LEVEL_INTERVAL_SECONDS):
            self.on_levels(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        return {
            "levels": {name: round(track.level, 3) for name, track in self.tracks.items()},
            "elapsed": round(self.clock.elapsed(), 1),
            "paused": self.clock.paused,
            "monitor": not self.write,
        }

    def stop(self) -> dict[str, Any]:
        final = self.clock.elapsed()
        self.stopped.set()
        for track in self.tracks.values():
            track.close(final if self.write else None)
        self.pa.terminate()
        return {
            "duration": round(final, 2),
            "tracks": {name: {"file": t.path.name, "device": t.device_name, "rate": t.rate} for name, t in self.tracks.items()},
        }

    def _abort(self) -> None:
        self.stopped.set()
        for track in self.tracks.values():
            try:
                track.close(None)
            except Exception:  # noqa: BLE001
                pass
        self.pa.terminate()


# ── Post-processing ────────────────────────────────────────────────────────


def mix_tracks(track_paths: list[Path], target: Path) -> None:
    """Mix tracks into a 16 kHz mono WAV for Whisper (no FFmpeg needed)."""
    from stt_engine import SAMPLE_RATE, load_audio

    try:
        tracks = [load_audio(path) for path in track_paths]
    except Exception as exc:  # noqa: BLE001
        raise RecorderError(f"Could not read the recorded tracks: {exc}") from exc
    length = max((len(track) for track in tracks), default=0)
    mix = np.zeros(length, dtype=np.float32)
    for track in tracks:
        mix[: len(track)] += track
    peak = float(np.abs(mix).max()) if length else 0.0
    if peak > 0.95:
        mix *= 0.95 / peak  # keep the sum from clipping
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes((mix * 32767).astype(np.int16).tobytes())


def repair_wav(path: Path) -> float:
    """Fix the header of a WAV left open by a crash. Returns its duration."""
    size = path.stat().st_size
    if size <= 44:
        return 0.0
    with path.open("r+b") as handle:
        header = handle.read(44)
        if header[:4] != b"RIFF" or header[36:40] != b"data":
            return 0.0
        rate = struct.unpack("<I", header[24:28])[0]
        block_align = struct.unpack("<H", header[32:34])[0] or 2
        data_size = (size - 44) // block_align * block_align
        handle.seek(4)
        handle.write(struct.pack("<I", 36 + data_size))
        handle.seek(40)
        handle.write(struct.pack("<I", data_size))
    return data_size / block_align / rate if rate else 0.0


def label_speakers(segments: list[dict[str, Any]], mic: np.ndarray, system: np.ndarray, rate: int) -> int:
    """Tag each segment "Me" (mic dominant) or "Others" (system dominant).

    Each track is normalised by its own typical speech level, so a quiet mic
    or loud speakers do not bias the result. Returns the number labelled.
    """

    def frame_rms(audio: np.ndarray) -> np.ndarray:
        frame = rate // 10
        usable = len(audio) // frame * frame
        if not usable:
            return np.zeros(1)
        return np.sqrt(np.mean(audio[:usable].reshape(-1, frame) ** 2, axis=1))

    mic_frames, sys_frames = frame_rms(mic), frame_rms(system)
    mic_ref = max(float(np.percentile(mic_frames, 95)), 1e-4)
    sys_ref = max(float(np.percentile(sys_frames, 95)), 1e-4)
    labelled = 0
    for segment in segments:
        if segment.get("start") is None:
            continue
        lo, hi = int(segment["start"] * 10), max(int(segment["end"] * 10), int(segment["start"] * 10) + 1)
        mic_part, sys_part = mic_frames[lo:hi], sys_frames[lo:hi]
        mic_score = float(np.mean(mic_part)) / mic_ref if mic_part.size else 0.0
        sys_score = float(np.mean(sys_part)) / sys_ref if sys_part.size else 0.0
        if max(mic_score, sys_score) < 0.05:
            continue
        segment["speaker"] = "Me" if mic_score > sys_score else "Others"
        labelled += 1
    return labelled
