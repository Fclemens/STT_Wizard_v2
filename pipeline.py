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
import speakers
from stt_engine import SAMPLE_RATE, load_audio, transcribe

STAGE_MESSAGES = {
    "queued": "Waiting for the previous transcription to finish.",
    "downloading_model": "Downloading the speech model (first use only)…",
    "loading_model": "Loading the speech model…",
    "loading_audio": "Decoding the audio.",
    "transcribing": "Transcribing — lines appear as they are ready.",
    "stopping": "Stopping after the current window…",
    "speakers": "Telling the voices apart…",
    "speaker_model": "Downloading the speaker model (first use only, ~40 MB)…",
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
        trim = live["audio"].get("trim")
        live["transcript"] = {
            "source": "whisper",
            "range": [trim["start"], trim["end"]] if trim else None,
            "model": options["model"],
            "language": options.get("language") or "",
            "task": options.get("task", "transcribe"),
            "accurate": bool(options.get("accurate", True)),
            "glossary_used": bool(options.get("glossary")),
            "speaker_labels": bool(options.get("speakers", True)),
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
    trim = session["audio"].get("trim")

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
        events.publish(session_id, "segments", {"segments": [store.public_segment(seg) for seg in new_segments], "job": job_snapshot})

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
            span=(trim["start"], trim["end"]) if trim else None,
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _fail(session_id, f"Transcription failed: {exc}")
        return
    if not meta.get("stopped") and options.get("speakers", True):
        _label_lines(session_id, options, lambda stage, pct: _set_job(session_id, stage, pct))
    _finish_transcription(session_id, meta)


def _finish_transcription(session_id: str, meta: dict[str, Any]) -> None:
    with store.edit(session_id) as live:
        transcript = live["transcript"]
        transcript["completed_at"] = store.now_iso()
        transcript["stopped"] = bool(meta.get("stopped"))
        for key in ("language", "device", "glossary_terms"):
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


# ── Speakers ───────────────────────────────────────────────────────────────


def _label_lines(session_id: str, options: dict[str, Any], on_progress) -> None:
    """Label the transcript's lines by speaker; on failure keep the lines and explain in a notice."""
    session = store.get_session(session_id)
    if not session or not (session.get("transcript") or {}).get("segments"):
        return
    notice = None
    try:
        segments, speaker_map = compute_speakers(session, True, options.get("speaker_count"), on_progress)
    except Exception as exc:  # noqa: BLE001 - speaker labels must never cost the transcript
        traceback.print_exc()
        notice = f"Speaker detection failed ({exc}). The transcript is fine; try Detect speakers later."
        try:  # still label Me/Others from the two tracks, which needs no model
            segments, speaker_map = compute_speakers(session, False, None, on_progress)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            segments, speaker_map = None, None
    with store.edit(session_id) as live:
        transcript = live["transcript"]
        if segments is not None:
            transcript["segments"] = segments
            transcript["speaker_map"] = speaker_map
            _forget_old_speakers(transcript)
        if notice:
            live["notice"] = notice


def compute_speakers(
    session: dict[str, Any], detect: bool, count: int | None, on_progress, locked: frozenset = frozenset()
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """New lines (some split where the voice changes) with speaker ids, plus the id -> name map.

    * Recorded with microphone + system audio: the microphone gives "Me"; the other
      voices are told apart on the system track (cleaner than the mix).
    * Anything else: voices are told apart on the whole audio.
    A single voice is not labelled (or stays "Others" next to "Me").
    ``locked`` lines (referenced by review suggestions) are never split.
    """
    transcript = session["transcript"]
    segments = [dict(seg) for seg in transcript["segments"]]
    for seg in segments:
        seg["speaker"] = None
    audio = session["audio"]
    folder = store.audio_dir(session["id"])
    tracks = audio.get("tracks") or {}
    old_names = store.speaker_names(transcript)
    speaker_map: dict[str, dict[str, str]] = {}

    if detect and not speakers.model_ready():
        on_progress("speaker_model", 0)

    if "mic" in tracks and "system" in tracks:
        mic = load_audio(folder / tracks["mic"]["file"])
        system = load_audio(folder / tracks["system"]["file"])
        label_speakers(segments, mic, system, SAMPLE_RATE)
        for seg in segments:
            seg["speaker"] = {"Me": "me", "Others": "others"}.get(seg.get("speaker"))
        speaker_map["me"] = {"name": old_names.get("me", "Me")}
        voiced, track = [seg for seg in segments if seg["speaker"] == "others"], system
    else:
        voiced, track = segments, None

    groups: dict[str, int] = {}
    changes: dict[str, list] = {}
    if detect and voiced:
        if track is None:
            track = load_audio(folder / audio["file"])
        groups, changes = speakers.detect(voiced, track, SAMPLE_RATE, count, lambda pct: on_progress("speakers", pct))

    if len(set(groups.values())) > 1:
        for number in sorted(set(groups.values())):
            speaker_map[f"s{number}"] = {"name": f"Speaker {number}"}
        result = []
        for seg in segments:
            if seg["id"] not in groups:
                result.append(seg)
                continue
            if seg["id"] in changes and seg["id"] not in locked:
                pieces = speakers.split_line(seg, changes[seg["id"]], groups[seg["id"]])
            else:
                pieces = [(seg, groups[seg["id"]])]
            for piece, number in pieces:
                piece["speaker"] = f"s{number}"
                result.append(piece)
        segments = result
    elif any(seg["speaker"] == "others" for seg in segments):
        speaker_map["others"] = {"name": old_names.get("others", "Others")}
    if not any(seg.get("speaker") for seg in segments):
        speaker_map = {}
    return segments, speaker_map


def _forget_old_speakers(transcript: dict[str, Any]) -> None:
    """Lines hidden by a trim were not re-labelled: drop speakers that no longer exist."""
    for seg in transcript.get("trimmed_out") or []:
        if seg.get("speaker") not in transcript["speaker_map"]:
            seg["speaker"] = None


def start_speaker_detection(session_id: str, count: int | None) -> None:
    """Detect speakers again on an existing transcript (names given so far are reset, except "Me")."""
    with store.edit(session_id) as live:
        if is_busy(live):
            raise RuntimeError("This session is already busy.")
        if not (live.get("transcript") or {}).get("segments") or not live.get("audio"):
            raise RuntimeError("There is no transcribed audio to detect speakers in.")
        live["status"] = "diarizing"
        live["error"] = None
        live["transcript"]["speaker_labels"] = True
        job = _new_job("speakers")
        job.update(stage="speakers", message=STAGE_MESSAGES["speakers"])
        live["job"] = job
    _publish_session(session_id)
    threading.Thread(target=_run_speaker_detection, args=(session_id, count), daemon=True).start()


def _run_speaker_detection(session_id: str, count: int | None) -> None:
    session = store.get_session(session_id)
    if not session:
        return
    locked = frozenset(item["segment_id"] for item in (session.get("review") or {}).get("suggestions", []))
    try:
        segments, speaker_map = compute_speakers(
            session, True, count, lambda stage, pct: _set_job(session_id, stage, pct), locked
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _fail(session_id, f"Speaker detection failed: {exc}")
        return
    with store.edit(session_id) as live:
        transcript = live["transcript"]
        transcript["segments"] = segments
        transcript["speaker_map"] = speaker_map
        transcript["speaker_labels"] = True
        _forget_old_speakers(transcript)
        live["status"] = "ready"
        live["job"] = None
    _publish_session(session_id)


def default_transcribe_options(session: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    overrides = overrides or {}
    count = overrides.get("speaker_count", 0)
    return {
        "model": str(overrides.get("model") or settings.get("default_model") or "turbo"),
        "language": str(overrides.get("language", settings.get("default_language", "")) or "").strip().lower(),
        "task": "translate" if overrides.get("task") == "translate" else "transcribe",
        "accurate": bool(overrides.get("accurate", settings.get("accurate_decoding", True))),
        "glossary": effective_glossary(session),
        "speakers": bool(overrides.get("speakers", settings.get("detect_speakers", True))),
        "speaker_count": int(count) if str(count or "0").isdigit() and int(count) > 0 else None,
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
                session_id,
                "reviewing",
                int(done / max(total, 1) * 100),
                "Checking the whole transcript…"
                if total == 1
                else f"Reviewing part {done + 1} of {total} (with the full transcript as context)…",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _fail(session_id, f"Review failed: {exc}")
        return

    review["created_at"] = store.now_iso()
    with store.edit(session_id) as live:
        # A re-run keeps what the user already handled (applied, dismissed, stale) and only
        # replaces the pending suggestions; findings already handled are not proposed again.
        previous = (live.get("review") or {}).get("suggestions", [])
        handled = [item for item in previous if item.get("status") != "pending"]
        handled_keys = {(item["segment_id"], item["original"].lower(), item["suggested"].lower()) for item in handled}
        dismissed = set(live.get("dismissed_signatures") or [])
        hidden = 0
        new = []
        for suggestion in review["suggestions"]:
            if (suggestion["segment_id"], suggestion["original"].lower(), suggestion["suggested"].lower()) in handled_keys:
                continue  # already listed under Handled
            if store.review_signature(suggestion) in dismissed:
                hidden += 1
                continue
            new.append(suggestion)
        review["suggestions"] = new + handled
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
    text = store.speaker_text(session["transcript"])
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
    """Default glossary from Settings, the session's own terms and the speaker names given."""
    default = str(load_settings().get("default_glossary", "")).strip()
    own = str(session.get("glossary", "")).strip()
    names = "\n".join(
        name for key, name in store.speaker_names(session.get("transcript")).items()
        if name not in ("Me", "Others", f"Speaker {key[1:]}")
    )
    return "\n".join(part for part in (default, own, names) if part)


def start_workers() -> None:
    threading.Thread(target=_transcription_worker, name="whisper-worker", daemon=True).start()
