from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from project_store import ensure_project_paths, get_project, save_corrected_transcript, save_project
from stt_engine import transcribe_audio_incremental

JOB_REGISTRY: dict[str, dict[str, Any]] = {}
JOB_LOCK = threading.Lock()

ACTIVE_STATUSES = {"queued", "loading_model", "loading_audio", "transcribing", "writing_outputs", "stopping"}
TERMINAL_STATUSES = {"completed", "failed", "stopped"}
STAGE_MESSAGES = {
    "queued": "Waiting for the transcription worker to start.",
    "loading_model": "Loading the selected local Whisper model.",
    "loading_audio": "Preparing the audio file.",
    "transcribing": "Transcribing audio and streaming partial text.",
    "writing_outputs": "Writing TXT, SRT, VTT, and JSON outputs.",
    "stopping": "Stop requested. Whisper will finish the current chunk before exiting.",
    "completed": "Transcription complete.",
    "stopped": "Transcription stopped.",
    "failed": "Transcription failed.",
}


def clamp_progress(progress: int | float) -> int:
    return max(0, min(100, int(progress)))


def job_snapshot(state: dict[str, Any] | None, project: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not state:
        return None
    status = str(state.get("status", "queued"))
    progress = clamp_progress(state.get("progress", 0))
    transcript = project.get("transcript") if project else None
    return {
        "job_id": state.get("job_id"),
        "project_id": state.get("project_id"),
        "status": status,
        "stage": status.replace("_", " ").title(),
        "message": state.get("message") or STAGE_MESSAGES.get(status, "Working on transcription."),
        "progress": progress,
        "percent": progress,
        "partial_text": state.get("partial_text", ""),
        "is_active": status in ACTIVE_STATUSES,
        "is_terminal": status in TERMINAL_STATUSES,
        "can_stop": status in ACTIVE_STATUSES and status != "stopping",
        "error": state.get("error"),
        "source_file": state.get("source_file"),
        "model": state.get("model"),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
        "project_status": project.get("status") if project else None,
        "transcript_ready": bool(transcript),
        "result_artifacts": transcript.get("artifacts") if isinstance(transcript, dict) else None,
    }


def start_transcription_job(
    project_id: str,
    audio_path: Path,
    source_file: str,
    model_name: str,
    language: str | None,
    task: str,
    temperature: float,
    word_timestamps: bool,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job_state = {
        "job_id": job_id,
        "project_id": project_id,
        "status": "queued",
        "progress": 0,
        "partial_text": "",
        "started_at": time.time(),
        "updated_at": time.time(),
        "error": None,
        "stopping": False,
        "message": STAGE_MESSAGES["queued"],
        "source_file": source_file,
        "model": model_name,
    }
    with JOB_LOCK:
        JOB_REGISTRY[job_id] = job_state

    project = get_project(project_id)
    if project:
        project["status"] = "transcribing"
        project["active_job_id"] = job_id
        project["source_file"] = source_file
        save_project(project)

    thread = threading.Thread(
        target=_run_transcription_job,
        kwargs={
            "job_id": job_id,
            "project_id": project_id,
            "audio_path": audio_path,
            "model_name": model_name,
            "language": language,
            "task": task,
            "temperature": temperature,
            "word_timestamps": word_timestamps,
        },
        daemon=True,
    )
    thread.start()
    return job_state


def _run_transcription_job(
    job_id: str,
    project_id: str,
    audio_path: Path,
    model_name: str,
    language: str | None,
    task: str,
    temperature: float,
    word_timestamps: bool,
) -> None:
    paths = ensure_project_paths(project_id)

    def update(stage: str, progress: int, text: str) -> None:
        with JOB_LOCK:
            state = JOB_REGISTRY[job_id]
            if state.get("stopping") and stage not in {"stopped", "failed"}:
                state["status"] = "stopping"
                state["message"] = STAGE_MESSAGES["stopping"]
                state["updated_at"] = time.time()
                return
            state["status"] = stage
            state["progress"] = clamp_progress(progress)
            state["partial_text"] = text
            state["message"] = STAGE_MESSAGES.get(stage, "Working on transcription.")
            state["updated_at"] = time.time()

    def should_stop() -> bool:
        with JOB_LOCK:
            return bool(JOB_REGISTRY[job_id]["stopping"])

    try:
        result = transcribe_audio_incremental(
            audio_path=audio_path,
            output_dir=paths["outputs"],
            model_name=model_name,
            language=language,
            task=task,
            temperature=temperature,
            word_timestamps=word_timestamps,
            progress_callback=update,
            should_stop=should_stop,
        )
        project = get_project(project_id)
        if project:
            result["artifacts"] = {
                "txt": f"/projects/{project_id}/download/{result['job_id']}/txt",
                "srt": f"/projects/{project_id}/download/{result['job_id']}/srt",
                "vtt": f"/projects/{project_id}/download/{result['job_id']}/vtt",
                "json": f"/projects/{project_id}/download/{result['job_id']}/json",
            }
            project["transcript"] = {
                **result,
                "source_path": str(audio_path),
                "settings": {
                    "model": model_name,
                    "language": language or "",
                    "task": task,
                    "temperature": temperature,
                    "word_timestamps": word_timestamps,
                },
            }
            project["corrected_transcript"] = {
                "text": result["text"],
                "saved_files": save_corrected_transcript(project_id, result["text"]),
            }
            project["review"] = None
            project["analysis"] = None
            project["status"] = "stopped" if result.get("stopped") else "transcript_ready"
            project.pop("active_job_id", None)
            save_project(project)
        with JOB_LOCK:
            state = JOB_REGISTRY[job_id]
            state["result"] = result
            state["status"] = "stopped" if result.get("stopped") else "completed"
            state["progress"] = 100
            state["partial_text"] = result["text"]
            state["message"] = STAGE_MESSAGES[state["status"]]
            state["updated_at"] = time.time()
    except Exception as exc:
        project = get_project(project_id)
        if project:
            project["status"] = "failed"
            project.pop("active_job_id", None)
            save_project(project)
        with JOB_LOCK:
            state = JOB_REGISTRY[job_id]
            state["status"] = "failed"
            state["error"] = str(exc)
            state["message"] = STAGE_MESSAGES["failed"]
            state["updated_at"] = time.time()


def get_job(job_id: str) -> dict[str, Any] | None:
    with JOB_LOCK:
        state = JOB_REGISTRY.get(job_id)
        return dict(state) if state else None


def stop_job(job_id: str) -> dict[str, Any] | None:
    with JOB_LOCK:
        state = JOB_REGISTRY.get(job_id)
        if not state:
            return None
        state["stopping"] = True
        state["status"] = "stopping"
        state["message"] = STAGE_MESSAGES["stopping"]
        state["updated_at"] = time.time()
        return dict(state)
