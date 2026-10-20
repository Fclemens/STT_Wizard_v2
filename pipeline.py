"""Session pipeline: transcribe -> (auto) review -> (auto) analysis.

Transcription runs on a single worker thread (one Whisper model on the GPU at
a time); review and analysis are network-bound and get their own threads.
Nothing here runs on the web server's event loop.
"""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from typing import Any

import events
import session_store as store
from analyzer import analyze_transcript
from reviewer import review_segments
from settings_store import load_settings, openai_ready
from recorder import label_speakers
from stt_engine import SAMPLE_RATE, load_audio, transcribe

STAGE_MESSAGES = {
    "queued": "Waiting for the previous transcription to finish.",
    "loading_model": "Loading the Whisper model (first use downloads it).",
    "loading_audio": "Decoding the audio.",
    "transcribing": "Transcribing — lines appear as they are ready.",
    "stopping": "Stopping after the current window…",
    "reviewing": "Asking the LLM to check names, acronyms and terms…",
    "analyzing": "Summarising the transcript…",
}

_stop_flags: dict[str, threading.Event] = {}
_transcribe_queue: "queue.Queue[tuple[str, str, dict[str, Any]]]" = queue.Queue()


def _publish_session(session_id: str) -> None:
    session = store.get_session(session_id)
    if session:
        events.publish(session_id, "session", store.public_view(session))


def _set_job(session_id: str, stage: str, progress: int, message: str | None = None) -> dict[str, Any]:
    with store.edit(session_id) as live:
        job = live.get("job") or {}
        job.update(stage=stage, progress=progress, message=message or STAGE_MESSAGES.get(stage, ""))
        live["job"] = job
        snapshot = dict(job)
    events.publish(session_id, "job", snapshot)
    return snapshot


def _new_job(kind: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "stage": "queued",
        "progress": 0,
        "message": STAGE_MESSAGES["queued"],
        "started_at": store.now_iso(),
    }


def is_busy(session: dict[str, Any]) -> bool:
    return session.get("status") in store.BUSY_STATUSES


# ── Transcription ──────────────────────────────────────────────────────────


def start_transcription(session_id: str, options: dict[str, Any]) -> None:
    job = _new_job("transcribe")
    with store.edit(session_id) as live:
        if is_busy(live):
            raise RuntimeError("This session is already busy.")
        if not live.get("audio"):
            raise RuntimeError("Add audio before transcribing.")
        live["status"] = "transcribing"
        live["error"] = None
        live["notice"] = None
        live["job"] = job
        live["transcript"] = {
            "source": "whisper",
            "model": options["model"],
            "language": options.get("language") or "",
            "task": options.get("task", "transcribe"),
            "accurate": bool(options.get("accurate", True)),
            "glossary_used": bool(options.get("glossary")),
            "device": None,
            "started_at": store.now_iso(),
            "completed_at": None,
            "stopped": False,
            "segments": [],
        }
        live["review"] = None
        live["analysis"] = None
    _stop_flags[session_id] = threading.Event()
    _publish_session(session_id)
    _transcribe_queue.put((session_id, job["id"], options))


def _transcription_worker() -> None:
    while True:
        session_id, job_id, options = _transcribe_queue.get()
        try:
            _run_transcription(session_id, job_id, options)
        except Exception:  # noqa: BLE001 - worker must survive anything
            traceback.print_exc()
        finally:
            _transcribe_queue.task_done()


def _run_transcription(session_id: str, job_id: str, options: dict[str, Any]) -> None:
    session = store.get_session(session_id)
    if not session or (session.get("job") or {}).get("id") != job_id:
        return  # deleted or superseded while queued
    stop_flag = _stop_flags.setdefault(session_id, threading.Event())
    if stop_flag.is_set():
        _finish_transcription(session_id, {"stopped": True})
        return

    audio_path = store.audio_dir(session_id) / session["audio"]["file"]

    def on_progress(stage: str, progress: int) -> None:
        _set_job(session_id, "stopping" if stop_flag.is_set() else stage, progress)

    def on_segments(new_segments: list[dict[str, Any]], progress: int, language: str) -> None:
        with store.edit(session_id) as live:
            transcript = live["transcript"]
            transcript["segments"].extend(new_segments)
            if language and not transcript.get("language"):
                transcript["language"] = language
            job = live.get("job") or {}
            job.update(
                stage="stopping" if stop_flag.is_set() else "transcribing",
                progress=progress,
                message=STAGE_MESSAGES["stopping" if stop_flag.is_set() else "transcribing"],
            )
            live["job"] = job
            job_snapshot = dict(job)
        events.publish(session_id, "segments", {"segments": new_segments, "job": job_snapshot})

    try:
        meta = transcribe(
            audio_path,
            model_name=options["model"],
            language=options.get("language") or None,
            task=options.get("task", "transcribe"),
            glossary=options.get("glossary", ""),
            accurate=bool(options.get("accurate", True)),
            on_progress=on_progress,
            on_segments=on_segments,
            should_stop=stop_flag.is_set,
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _fail(session_id, f"Transcription failed: {exc}")
        return
    _finish_transcription(session_id, meta)


def _finish_transcription(session_id: str, meta: dict[str, Any]) -> None:
    speakers = _speaker_labels(session_id)
    with store.edit(session_id) as live:
        if speakers:
            for segment in live["transcript"]["segments"]:
                if segment["id"] in speakers:
                    segment["speaker"] = speakers[segment["id"]]
            live["transcript"]["speakers"] = True
        transcript = live["transcript"]
        transcript["completed_at"] = store.now_iso()
        transcript["stopped"] = bool(meta.get("stopped"))
        for key in ("language", "device"):
            if meta.get(key):
                transcript[key] = meta[key]
        if meta.get("duration") and live.get("audio"):
            live["audio"]["duration"] = meta["duration"]
        live["status"] = "ready"
        live["job"] = None
        has_text = bool(transcript["segments"])
    _stop_flags.pop(session_id, None)
    _publish_session(session_id)

    settings = load_settings()
    if has_text and not meta.get("stopped"):
        if settings.get("auto_review") and openai_ready():
            start_review(session_id, chain_analysis=bool(settings.get("auto_analysis")))
        elif settings.get("auto_analysis"):
            start_analysis(session_id)


def _speaker_labels(session_id: str) -> dict[str, str]:
    """Me/Others per line, when the session was recorded with both sources."""
    session = store.get_session(session_id)
    tracks = ((session or {}).get("audio") or {}).get("tracks") or {}
    segments = ((session or {}).get("transcript") or {}).get("segments") or []
    if not ("mic" in tracks and "system" in tracks and segments):
        return {}
    try:
        folder = store.audio_dir(session_id)
        mic = load_audio(folder / tracks["mic"]["file"])
        system = load_audio(folder / tracks["system"]["file"])
        copies = [dict(segment) for segment in segments]
        label_speakers(copies, mic, system, SAMPLE_RATE)
        return {seg["id"]: seg["speaker"] for seg in copies if seg.get("speaker")}
    except Exception:  # noqa: BLE001 - labels are a nice-to-have
        traceback.print_exc()
        return {}


def default_transcribe_options(session: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    overrides = overrides or {}
    return {
        "model": str(overrides.get("model") or settings.get("default_model") or "turbo"),
        "language": str(overrides.get("language", settings.get("default_language", "")) or "").strip().lower(),
        "task": "translate" if overrides.get("task") == "translate" else "transcribe",
        "accurate": bool(overrides.get("accurate", settings.get("accurate_decoding", True))),
        "glossary": effective_glossary(session),
    }


def stop(session_id: str) -> None:
    flag = _stop_flags.get(session_id)
    if flag:
        flag.set()
    session = store.get_session(session_id)
    if session and session.get("status") == "transcribing":
        _set_job(session_id, "stopping", (session.get("job") or {}).get("progress", 0))


# ── Review / analysis ──────────────────────────────────────────────────────


def start_review(session_id: str, chain_analysis: bool = False) -> None:
    with store.edit(session_id) as live:
        if is_busy(live):
            raise RuntimeError("This session is already busy.")
        if not (live.get("transcript") or {}).get("segments"):
            raise RuntimeError("There is no transcript to review yet.")
        live["status"] = "reviewing"
        live["error"] = None
        job = _new_job("review")
        job.update(stage="reviewing", message=STAGE_MESSAGES["reviewing"])
        live["job"] = job
    _publish_session(session_id)
    threading.Thread(target=_run_review, args=(session_id, chain_analysis), daemon=True).start()


def _run_review(session_id: str, chain_analysis: bool) -> None:
    session = store.get_session(session_id)
    if not session:
        return
    transcript = session["transcript"]
    try:
        review = review_segments(
            segments=transcript["segments"],
            language=str(transcript.get("language") or "auto"),
            glossary=effective_glossary(session),
            on_progress=lambda done, total: _set_job(
                session_id, "reviewing", int(done / max(total, 1) * 100), f"Reviewed part {done} of {total}…"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _fail(session_id, f"Review failed: {exc}")
        return

    review["created_at"] = store.now_iso()
    with store.edit(session_id) as live:
        dismissed = set(live.get("dismissed_signatures") or [])
        hidden = 0
        kept = []
        for suggestion in review["suggestions"]:
            if store.review_signature(suggestion) in dismissed:
                hidden += 1
                continue
            kept.append(suggestion)
        review["suggestions"] = kept
        review["hidden_dismissed"] = hidden
        live["review"] = review
        live["status"] = "ready"
        live["job"] = None
    _publish_session(session_id)
    if chain_analysis:
        start_analysis(session_id)


def start_analysis(session_id: str) -> None:
    with store.edit(session_id) as live:
        if is_busy(live):
            raise RuntimeError("This session is already busy.")
        if not (live.get("transcript") or {}).get("segments"):
            raise RuntimeError("There is no transcript to analyse yet.")
        live["status"] = "analyzing"
        live["error"] = None
        job = _new_job("analysis")
        job.update(stage="analyzing", message=STAGE_MESSAGES["analyzing"])
        live["job"] = job
    _publish_session(session_id)
    threading.Thread(target=_run_analysis, args=(session_id,), daemon=True).start()


def _run_analysis(session_id: str) -> None:
    session = store.get_session(session_id)
    if not session:
        return
    text = store.segments_text(session["transcript"]["segments"])
    try:
        analysis = analyze_transcript(text)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _fail(session_id, f"Analysis failed: {exc}")
        return
    analysis["created_at"] = store.now_iso()
    with store.edit(session_id) as live:
        live["analysis"] = analysis
        live["status"] = "ready"
        live["job"] = None
    _publish_session(session_id)


def _fail(session_id: str, message: str) -> None:
    try:
        with store.edit(session_id) as live:
            live["status"] = "failed"
            live["error"] = message
            live["job"] = None
    except KeyError:
        return
    _stop_flags.pop(session_id, None)
    _publish_session(session_id)


def effective_glossary(session: dict[str, Any]) -> str:
    """Default glossary from Settings plus the session's own terms."""
    default = str(load_settings().get("default_glossary", "")).strip()
    own = str(session.get("glossary", "")).strip()
    return "\n".join(part for part in (default, own) if part)


def start_workers() -> None:
    threading.Thread(target=_transcription_worker, name="whisper-worker", daemon=True).start()
