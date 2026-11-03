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


EXPORT_RATE = 48000
EXPORT_BITRATE = 128_000


def _mix(paths: list[Path], rate: int) -> np.ndarray:
    """Decode, resample and sum sources into one mono float32 signal."""
    from stt_engine import load_audio

    try:
        tracks = [load_audio(path, rate=rate) for path in paths]
    except Exception as exc:  # noqa: BLE001
        raise RecorderError(f"Could not read the audio: {exc}") from exc
    length = max((len(track) for track in tracks), default=0)
    mix = np.zeros(length, dtype=np.float32)
    for track in tracks:
        mix[: len(track)] += track
    peak = float(np.abs(mix).max()) if length else 0.0
    if peak > 0.95:
        mix *= 0.95 / peak  # keep the sum from clipping
    return mix


def mix_tracks(track_paths: list[Path], target: Path) -> None:
    """Mix tracks into a 16 kHz mono WAV for Whisper (no FFmpeg needed)."""
    from stt_engine import SAMPLE_RATE

    mix = _mix(track_paths, SAMPLE_RATE)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes((mix * 32767).astype(np.int16).tobytes())


def export_mp3(sources: list[Path], target: Path, span: tuple[float, float] | None = None) -> None:
    """Encode one or more sources (mixed) to a 48 kHz mono MP3 for sharing; only ``span`` when trimmed."""
    import av

    mix = _mix(sources, EXPORT_RATE)
    if span:
        mix = mix[int(span[0] * EXPORT_RATE) : int(span[1] * EXPORT_RATE)]
    partial = target.with_suffix(".part")
    with av.open(str(partial), "w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=EXPORT_RATE, layout="mono")
        stream.bit_rate = EXPORT_BITRATE
        frame_size = stream.codec_context.frame_size or 1152
        for offset in range(0, len(mix), frame_size):
            chunk = mix[offset : offset + frame_size]
            frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="fltp", layout="mono")
            frame.sample_rate = EXPORT_RATE
            frame.pts = offset
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    partial.replace(target)


def waveform(path: Path, buckets: int = 1500) -> dict[str, Any]:
    """Peak level per time slice (0..1), for drawing the trim view. Returns duration and peaks."""
    from stt_engine import load_audio

    rate = 8000
    audio = load_audio(path, rate=rate)
    duration = len(audio) / rate
    if not len(audio):
        return {"duration": 0.0, "peaks": []}
    size = max(1, int(np.ceil(len(audio) / buckets)))
    padded = np.pad(np.abs(audio), (0, size * int(np.ceil(len(audio) / size)) - len(audio)))
    peaks = padded.reshape(-1, size).max(axis=1)
    top = float(np.percentile(peaks, 99)) or 1.0
    return {"duration": round(duration, 2), "peaks": [round(float(v), 3) for v in np.minimum(peaks / top, 1.0)]}


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


# Speaker labelling thresholds (dB). Tuned on a real 22-minute call where the mic was
# near silent most of the time; relative-loudness scoring mislabelled its hiss as "Me".
ACTIVE_ABOVE_FLOOR_DB = 15   # a track is "active" this far above its own noise floor...
MIC_MIN_DBFS = -65           # ...and above these absolute levels
SYSTEM_MIN_DBFS = -55
MIC_ABOVE_ECHO_DB = 10       # mic sound must exceed the expected speaker echo by this much
MIN_ACTIVE_SHARE = 0.15      # lines where neither side is active this often stay unlabelled


def label_speakers(segments: list[dict[str, Any]], mic: np.ndarray, system: np.ndarray, rate: int) -> int:
    """Tag each segment "Me" (microphone) or "Others" (system audio) by actual voice activity.

    Per 100 ms frame:
      * system active: well above the system track's noise floor;
      * mic active ("own voice"): well above the mic's noise floor AND above the echo
        expected from the speakers (how strongly system sound leaks into the mic is
        measured over the whole recording).
    A line is labelled by whichever side is active in more of its frames; if neither
    is active often enough it is left unlabelled. Returns the number labelled.
    """

    def frame_db(audio: np.ndarray) -> np.ndarray:
        frame = rate // 10
        usable = len(audio) // frame * frame
        if not usable:
            return np.full(1, -120.0)
        rms = np.sqrt(np.mean(audio[:usable].reshape(-1, frame) ** 2, axis=1))
        return 20 * np.log10(np.maximum(rms, 1e-6))

    mic_db, sys_db = frame_db(mic), frame_db(system)
    n = min(len(mic_db), len(sys_db))
    mic_db, sys_db = mic_db[:n], sys_db[:n]
    mic_floor = max(float(np.percentile(mic_db, 3)), -100.0)  # quietest moments; speech has pauses
    sys_floor = max(float(np.percentile(sys_db, 3)), -100.0)  # gap-filled loopback is digital silence

    sys_active = (sys_db > sys_floor + ACTIVE_ABOVE_FLOOR_DB) & (sys_db > SYSTEM_MIN_DBFS)
    if sys_active.sum() >= 50:
        coupling = float(np.median(mic_db[sys_active] - sys_db[sys_active]))  # speaker -> mic leak
        expected_echo = np.maximum(mic_floor, sys_db + coupling)
    else:
        expected_echo = np.full(n, mic_floor)
    mic_active = (
        (mic_db > mic_floor + ACTIVE_ABOVE_FLOOR_DB)
        & (mic_db > MIC_MIN_DBFS)
        & (mic_db > expected_echo + MIC_ABOVE_ECHO_DB)
    )

    labelled = 0
    for segment in segments:
        segment.pop("speaker", None)
        if segment.get("start") is None:
            continue
        lo = int(segment["start"] * 10)
        hi = min(max(int(np.ceil(segment["end"] * 10)), lo + 1), n)
        if lo >= hi:
            continue
        me, others = float(mic_active[lo:hi].mean()), float(sys_active[lo:hi].mean())
        if max(me, others) < MIN_ACTIVE_SHARE:
            continue
        segment["speaker"] = "Me" if me > others else "Others"
        labelled += 1
    return labelled
