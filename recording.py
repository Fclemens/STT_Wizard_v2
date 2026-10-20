"""Recording orchestration: one capture at a time, owned by one session.

A capture is either a real recording (writes tracks, session status
"recording") or a short level check ("monitor") that writes nothing.
On stop, tracks are mixed and transcription starts automatically.
"""

from __future__ import annotations

import shutil
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import events
import pipeline
import session_store as store
from recorder import Capture, RecorderError, mix_tracks, repair_wav

MONITOR_SECONDS = 20

_lock = threading.RLock()
_capture: Capture | None = None
_owner: str | None = None
_monitor_timer: threading.Timer | None = None


def _levels_publisher(session_id: str):
    return lambda snapshot: events.publish(session_id, "levels", snapshot)


def _publish(session_id: str) -> None:
    session = store.get_session(session_id)
    if session:
        events.publish(session_id, "session", store.public_view(session))


# ── Level check ────────────────────────────────────────────────────────────


def start_monitor(session_id: str, mic: int | None, system: int | None) -> None:
    global _capture, _owner, _monitor_timer
    with _lock:
        if _capture is not None and _capture.write:
            raise RecorderError("A recording is in progress; levels are already shown there.")
        _stop_monitor_locked()
        _capture = Capture(session_id, None, mic, system, _levels_publisher(session_id), write=False)
        _owner = session_id
        _monitor_timer = threading.Timer(MONITOR_SECONDS, stop_monitor)
        _monitor_timer.daemon = True
        _monitor_timer.start()


def stop_monitor() -> None:
    with _lock:
        _stop_monitor_locked()


def _stop_monitor_locked() -> None:
    global _capture, _owner, _monitor_timer
    if _capture is None or _capture.write:
        return
    owner = _owner
    if _monitor_timer is not None:
        _monitor_timer.cancel()
        _monitor_timer = None
    _capture.stop()
    _capture, _owner = None, None
    if owner:
        events.publish(owner, "levels", {"levels": {}, "elapsed": 0, "paused": False, "monitor": False, "ended": True})


# ── Recording ──────────────────────────────────────────────────────────────


def start(session_id: str, mic: int | None, system: int | None) -> None:
    global _capture, _owner
    with _lock:
        _stop_monitor_locked()
        if _capture is not None:
            raise RecorderError("Another session is recording. Stop it first.")
        session = store.get_session(session_id)
        if not session:
            raise RecorderError("Session not found.")
        if pipeline.is_busy(session):
            raise RecorderError("Wait for the current step to finish first.")
        folder_name = f"rec-{uuid.uuid4().hex[:6]}"
        folder = store.audio_dir(session_id) / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        try:
            capture = Capture(session_id, folder, mic, system, _levels_publisher(session_id), write=True)
        except RecorderError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        _capture, _owner = capture, session_id
        with store.edit(session_id) as live:
            live["status"] = "recording"
            live["error"] = None
            live["notice"] = None
            live["job"] = {
                "id": uuid.uuid4().hex[:10],
                "kind": "record",
                "stage": "recording",
                "progress": 0,
                "message": "Recording",
                "started_at": store.now_iso(),
            }
            live["recording"] = {
                "folder": folder_name,
                "started_at": store.now_iso(),
                "devices": {name: track.device_name for name, track in capture.tracks.items()},
            }
    _publish(session_id)


def set_paused(session_id: str, paused: bool) -> None:
    with _lock:
        if _capture is None or _owner != session_id or not _capture.write:
            raise RecorderError("This session is not recording.")
        _capture.clock.pause() if paused else _capture.clock.resume()
    with store.edit(session_id) as live:
        if live.get("job"):
            live["job"]["stage"] = "paused" if paused else "recording"
            live["job"]["message"] = "Paused" if paused else "Recording"
    _publish(session_id)


def stop(session_id: str, transcribe: bool = True) -> None:
    global _capture, _owner
    with _lock:
        if _capture is None or _owner != session_id or not _capture.write:
            raise RecorderError("This session is not recording.")
        capture, _capture, _owner = _capture, None, None
    info = capture.stop()
    with store.edit(session_id) as live:
        live["job"].update(stage="saving", message="Saving the recording…")
    _publish(session_id)
    threading.Thread(target=_finalize, args=(session_id, info, transcribe), daemon=True).start()


def discard(session_id: str) -> None:
    """Stop without saving (session is being deleted)."""
    global _capture, _owner
    with _lock:
        if _capture is not None and _owner == session_id:
            capture, _capture, _owner = _capture, None, None
            capture.stop()


def active_recording() -> str | None:
    """Session id currently recording (not a level check), if any."""
    with _lock:
        return _owner if _capture is not None and _capture.write else None


def is_recording(session_id: str) -> bool:
    with _lock:
        return _capture is not None and _capture.write and _owner == session_id


def _finalize(session_id: str, info: dict[str, Any], transcribe: bool) -> None:
    session = store.get_session(session_id)
    if not session:
        return
    folder_name = session["recording"]["folder"]
    folder = store.audio_dir(session_id) / folder_name
    try:
        save_recording(session_id, folder, info["tracks"], info["duration"])
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with store.edit(session_id) as live:
            live["status"] = "failed"
            live["error"] = f"Could not save the recording: {exc}. The raw tracks are in {folder}."
            live["job"] = None
        _publish(session_id)
        return
    _publish(session_id)
    if transcribe and info["duration"] >= 1:
        try:
            session = store.get_session(session_id)
            pipeline.start_transcription(session_id, pipeline.default_transcribe_options(session))
        except RuntimeError as exc:
            print(f"Auto-transcribe skipped: {exc}")


def save_recording(session_id: str, folder: Path, tracks: dict[str, Any], duration: float, notice: str | None = None) -> None:
    """Mix the tracks and make them the session's audio."""
    paths = [folder / track["file"] for track in tracks.values()]
    mix = folder / "mix.wav"
    mix_tracks(paths, mix)
    folder_name = folder.name
    stamp = datetime.now().strftime("%d %b %Y %H:%M")
    old_audio = None
    with store.edit(session_id) as live:
        old_audio = live.get("audio")
        live["audio"] = {
            "file": f"{folder_name}/mix.wav",
            "original_name": f"Recording {stamp}.wav",
            "kind": "recording",
            "size": mix.stat().st_size,
            "duration": round(duration, 2),
            "added_at": store.now_iso(),
            "tracks": {
                name: {"file": f"{folder_name}/{track['file']}", "device": track.get("device", "")}
                for name, track in tracks.items()
            },
        }
        live["transcript"] = None
        live["review"] = None
        live["analysis"] = None
        live["status"] = "idle"
        live["job"] = None
        live["recording"] = None
        live["notice"] = notice
        if not live.get("user_renamed"):
            live["name"] = f"Recording {stamp}"
    remove_audio(session_id, old_audio, keep=folder_name)


def remove_audio(session_id: str, audio: dict[str, Any] | None, keep: str | None = None) -> None:
    if not audio:
        return
    root = store.audio_dir(session_id)
    first = Path(audio["file"]).parts[0]
    if first == keep:
        return
    target = root / first
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)
        (root / f"export-{target.stem}.mp3").unlink(missing_ok=True)


def recover_interrupted() -> None:
    """After a crash or restart, salvage recordings that were in progress."""
    for item in store.list_sessions():
        if item["status"] != "recording":
            continue
        session = store.get_session(item["id"])
        folder_name = (session.get("recording") or {}).get("folder")
        folder = store.audio_dir(item["id"]) / folder_name if folder_name else None
        tracks: dict[str, Any] = {}
        duration = 0.0
        if folder and folder.exists():
            for name in ("mic", "system"):
                path = folder / f"{name}.wav"
                if path.exists():
                    seconds = repair_wav(path)
                    if seconds > 0:
                        tracks[name] = {"file": path.name, "device": session["recording"]["devices"].get(name, "")}
                        duration = max(duration, seconds)
        try:
            if not tracks:
                raise RecorderError("no audio was captured")
            minutes, seconds = divmod(int(duration), 60)
            save_recording(
                item["id"], folder, tracks, duration,
                notice=f"The app closed during recording. The first {minutes}:{seconds:02} were saved — click Transcribe.",
            )
        except Exception as exc:  # noqa: BLE001
            with store.edit(item["id"]) as live:
                live["status"] = "failed"
                live["job"] = None
                live["error"] = f"The app closed during recording and it could not be recovered ({exc})."
