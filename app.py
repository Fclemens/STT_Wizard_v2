from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analyzer import analyze_transcript
from project_store import (
    create_project,
    delete_project,
    ensure_project_paths,
    get_project,
    list_projects,
    project_dir,
    review_signature,
    save_corrected_transcript,
    save_project,
)
from reviewer import review_transcript
from settings_store import load_env_file, load_settings, openai_ready, save_settings, update_env_key
from stt_engine import ALLOWED_EXTENSIONS, MODEL_OPTIONS, get_model_statuses
from transcription_jobs import get_job, job_snapshot, start_transcription_job, stop_job

load_env_file()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="STT Wizard", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def test_openai_api_key(api_key: str | None = None) -> dict[str, object]:
    candidate = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
    if not candidate:
        return {"ok": False, "message": "No OpenAI API key is configured yet."}
    try:
        response = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {candidate}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        return {"ok": False, "message": f"Could not reach OpenAI: {exc}"}
    if response.ok:
        return {"ok": True, "message": "OpenAI API key works. Review and Analysis are enabled."}
    return {
        "ok": False,
        "message": f"OpenAI rejected the key: HTTP {response.status_code}.",
    }


def default_project_name(file_name: str) -> str:
    stem = Path(file_name).stem.replace("_", " ").replace("-", " ").strip()
    if not stem:
        return "New Transcript"
    return " ".join(word.capitalize() for word in stem.split()[:6])


def active_transcript_text(project: dict[str, object] | None) -> str:
    if not project:
        return ""
    corrected = project.get("corrected_transcript")
    if isinstance(corrected, dict):
        return str(corrected.get("text", ""))
    transcript = project.get("transcript")
    if isinstance(transcript, dict):
        return str(transcript.get("text", ""))
    return ""


def normalize_transcript_artifacts(project: dict[str, object] | None) -> bool:
    if not project:
        return False
    transcript = project.get("transcript")
    if not isinstance(transcript, dict):
        return False
    job_id = transcript.get("job_id")
    if not job_id:
        return False
    expected = {
        "txt": f"/projects/{project['id']}/download/{job_id}/txt",
        "srt": f"/projects/{project['id']}/download/{job_id}/srt",
        "vtt": f"/projects/{project['id']}/download/{job_id}/vtt",
        "json": f"/projects/{project['id']}/download/{job_id}/json",
    }
    current = transcript.get("artifacts")
    if current == expected:
        return False
    transcript["artifacts"] = expected
    return True


def normalize_project_runtime_state(project: dict[str, object] | None) -> bool:
    if not project:
        return False
    changed = False
    active_job_id = project.get("active_job_id")
    active_job = get_job(str(active_job_id)) if active_job_id else None
    if active_job_id and not active_job:
        project.pop("active_job_id", None)
        changed = True
    if project.get("status") == "transcribing" and not active_job:
        transcript = project.get("transcript")
        if isinstance(transcript, dict) and transcript.get("text"):
            project["status"] = "transcript_ready"
        else:
            project["status"] = "draft"
        changed = True
    return changed


def resolve_active_job(project: dict[str, object] | None) -> dict[str, object] | None:
    if not project:
        return None
    active_job_id = project.get("active_job_id")
    if not active_job_id:
        return None

    changed = False
    job = get_job(str(active_job_id))
    terminal_states = {"completed", "failed", "stopped"}
    if project.get("status") != "transcribing":
        project.pop("active_job_id", None)
        save_project(project)
        return None
    if not job:
        project.pop("active_job_id", None)
        if project.get("status") == "transcribing":
            transcript = project.get("transcript")
            if isinstance(transcript, dict) and transcript.get("text"):
                project["status"] = "transcript_ready"
            else:
                project["status"] = "draft"
        changed = True
    else:
        status = str(job.get("status", "queued"))
        if status in terminal_states:
            project.pop("active_job_id", None)
            if project.get("status") == "transcribing":
                transcript = project.get("transcript")
                if isinstance(transcript, dict) and transcript.get("text"):
                    project["status"] = "transcript_ready"
                elif status == "failed":
                    project["status"] = "failed"
                else:
                    project["status"] = "draft"
            changed = True
            job = None

    if changed:
        save_project(project)
    return job


def should_show_job_banner(project: dict[str, object] | None, job: dict[str, object] | None) -> bool:
    if not project or not job:
        return False
    if project.get("status") != "transcribing":
        return False
    if not project.get("active_job_id"):
        return False
    return bool(job.get("is_active"))


def materialize_transcript_artifact(project_id: str, job_id: str, artifact: str) -> Path | None:
    project = get_project(project_id)
    if not project:
        return None
    transcript = project.get("transcript")
    if not isinstance(transcript, dict) or str(transcript.get("job_id")) != job_id:
        return None

    target_dir = ensure_project_paths(project_id)["outputs"] / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"transcript.{artifact}"

    if artifact == "txt":
        target.write_text(str(transcript.get("text", "")).strip() + "\n", encoding="utf-8")
        return target
    if artifact == "json":
        target.write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        return target
    if artifact == "srt":
        target.write_text("", encoding="utf-8")
        return target
    if artifact == "vtt":
        target.write_text("WEBVTT\n", encoding="utf-8")
        return target
    return None


def materialize_corrected_artifact(project_id: str, artifact: str) -> Path | None:
    project = get_project(project_id)
    if not project:
        return None
    text = active_transcript_text(project).strip()
    if not text:
        return None

    paths = ensure_project_paths(project_id)
    target = paths["corrected"] / f"corrected_transcript.{artifact}"
    if artifact == "txt":
        target.write_text(text + "\n", encoding="utf-8")
        return target
    if artifact == "json":
        target.write_text(json.dumps({"text": text}, indent=2, ensure_ascii=False), encoding="utf-8")
        return target
    return None


def build_context(
    request: Request,
    project: dict[str, object] | None,
    active_tab: str = "transcript",
    error: str | None = None,
    success: str | None = None,
) -> dict[str, object]:
    active_job: dict[str, object] | None = None
    if project:
        changed = False
        if normalize_transcript_artifacts(project):
            changed = True
        if normalize_project_runtime_state(project):
            changed = True
        if changed:
            save_project(project)
        active_job_state = resolve_active_job(project)
        active_job = job_snapshot(active_job_state, project)
    show_job_banner = should_show_job_banner(project, active_job)
    return {
        "request": request,
        "projects": list_projects(),
        "project": project,
        "active_tab": active_tab,
        "models": get_model_statuses(),
        "allowed_extensions": ", ".join(sorted(ALLOWED_EXTENSIONS)),
        "settings": load_settings(),
        "api_key_configured": openai_ready(),
        "active_transcript_text": active_transcript_text(project),
        "active_job": active_job,
        "show_job_banner": show_job_banner,
        "error": error,
        "success": success,
    }


def render_workspace(
    request: Request,
    project: dict[str, object] | None,
    active_tab: str = "transcript",
    error: str | None = None,
    success: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        "workspace.html",
        build_context(request, project, active_tab, error, success),
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> RedirectResponse:
    projects = list_projects()
    selected = projects[0] if projects else create_project()
    return RedirectResponse(url=f"/projects/{selected['id']}?tab=transcript", status_code=303)


@app.post("/projects")
async def new_project(name: str = Form("New Transcript")) -> RedirectResponse:
    project = create_project(name=name.strip() or "New Transcript")
    return RedirectResponse(url=f"/projects/{project['id']}?tab=transcript", status_code=303)


@app.post("/projects/{project_id}/rename")
async def rename_project(project_id: str, name: str = Form(...)) -> RedirectResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    project["name"] = name.strip() or project["name"]
    project["user_renamed"] = True
    save_project(project)
    return RedirectResponse(url=f"/projects/{project_id}?tab=transcript", status_code=303)


@app.post("/projects/{project_id}/delete")
async def remove_project(project_id: str) -> RedirectResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    delete_project(project_id)
    projects = list_projects()
    if projects:
        return RedirectResponse(url=f"/projects/{projects[0]['id']}?tab=transcript", status_code=303)
    new_item = create_project()
    return RedirectResponse(url=f"/projects/{new_item['id']}?tab=transcript", status_code=303)


@app.post("/settings")
async def update_settings(
    project_id: str = Form(...),
    active_tab: str = Form("transcript"),
    review_model: str = Form("gpt-5-mini"),
    analysis_model: str = Form("gpt-5-mini"),
    auto_review: str = Form("false"),
    auto_analysis: str = Form("false"),
    openai_api_key: str = Form(""),
) -> RedirectResponse:
    save_settings(
        {
            "review_model": review_model,
            "analysis_model": analysis_model,
            "auto_review": auto_review == "true",
            "auto_analysis": auto_analysis == "true",
        }
    )
    if openai_api_key.strip():
        update_env_key("OPENAI_API_KEY", openai_api_key)
    update_env_key("STT_REVIEW_MODEL", review_model)
    update_env_key("STT_ANALYSIS_MODEL", analysis_model)
    return RedirectResponse(url=f"/projects/{project_id}?tab={active_tab}", status_code=303)


@app.post("/settings/test-openai-key")
async def test_settings_openai_key(openai_api_key: str = Form("")) -> JSONResponse:
    return JSONResponse(test_openai_api_key(openai_api_key or None))


@app.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_workspace(request: Request, project_id: str, tab: str = "transcript") -> HTMLResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return render_workspace(request, project=project, active_tab=tab)


@app.post("/projects/{project_id}/transcribe")
async def run_transcription(
    project_id: str,
    file: UploadFile = File(...),
    model: str = Form("turbo"),
    language: str = Form(""),
    task: str = Form("transcribe"),
    temperature: float = Form(0.0),
    word_timestamps: str = Form("true"),
) -> JSONResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    if model not in MODEL_OPTIONS:
        return JSONResponse({"detail": "Unknown model selection."}, status_code=400)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return JSONResponse(
            {"detail": f"Unsupported file type. Use one of: {', '.join(sorted(ALLOWED_EXTENSIONS))}."},
            status_code=400,
        )

    paths = ensure_project_paths(project_id)
    safe_name = Path(file.filename or f"audio{suffix}").name
    input_path = paths["audio"] / f"{uuid.uuid4().hex[:8]}-{safe_name}"
    with input_path.open("wb") as buffer:
        while chunk := await file.read(1024 * 1024):
            buffer.write(chunk)

    if not project.get("user_renamed"):
        project["name"] = default_project_name(safe_name)
    save_project(project)
    job_state = start_transcription_job(
        project_id=project_id,
        audio_path=input_path,
        source_file=safe_name,
        model_name=model,
        language=language or None,
        task=task,
        temperature=temperature,
        word_timestamps=word_timestamps == "true",
    )
    project = get_project(project_id)
    snapshot = job_snapshot(job_state, project) or {}
    return JSONResponse(
        {
            "job_id": job_state["job_id"],
            "project_id": project_id,
            "redirect_url": f"/projects/{project_id}?tab=transcript",
            "progress": snapshot,
        }
    )


@app.get("/projects/{project_id}/jobs/{job_id}")
async def job_status(project_id: str, job_id: str) -> JSONResponse:
    state = get_job(job_id)
    if not state or state["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Job not found.")
    project = get_project(project_id)
    snapshot = job_snapshot(state, project)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse(snapshot)


@app.post("/projects/{project_id}/jobs/{job_id}/stop")
async def stop_transcription(project_id: str, job_id: str) -> JSONResponse:
    project = get_project(project_id)
    state = stop_job(job_id)
    if not state or state["project_id"] != project_id:
        if project and project.get("active_job_id") == job_id:
            project["status"] = "stopped"
            project.pop("active_job_id", None)
            save_project(project)
            return JSONResponse({"ok": True, "progress": {"status": "stopped", "is_active": False}})
        raise HTTPException(status_code=404, detail="Job not found.")
    if project and project.get("active_job_id") == job_id:
        project["status"] = "stopped"
        project.pop("active_job_id", None)
        save_project(project)
    return JSONResponse({"ok": True, "progress": job_snapshot(state, project)})


@app.post("/projects/{project_id}/review/import", response_class=HTMLResponse)
async def import_review_text(
    request: Request,
    project_id: str,
    transcript_file: UploadFile = File(...),
) -> HTMLResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    suffix = Path(transcript_file.filename or "").suffix.lower()
    if suffix not in {".txt", ".md"}:
        return render_workspace(
            request,
            project,
            active_tab="review",
            error="Use a .txt or .md transcript file for review import.",
            status_code=400,
        )
    content = (await transcript_file.read()).decode("utf-8", errors="replace").strip()
    if not content:
        return render_workspace(
            request,
            project,
            active_tab="review",
            error="The uploaded transcript file was empty.",
            status_code=400,
        )
    paths = ensure_project_paths(project_id)
    import_path = paths["review"] / f"imported-{Path(transcript_file.filename or 'transcript.txt').name}"
    import_path.write_text(content, encoding="utf-8")
    project["source_file"] = Path(transcript_file.filename or "Imported Transcript").name
    if not project.get("user_renamed"):
        project["name"] = default_project_name(project["source_file"])
    project["transcript"] = {
        "job_id": "imported-text",
        "job_dir": str(paths["review"]),
        "device": "n/a",
        "model": "Imported Text",
        "language": "text",
        "task": "review-only",
        "text": content,
        "segments": [],
        "source_file": project["source_file"],
        "source_path": str(import_path),
        "artifacts": {},
        "imported": True,
    }
    project["corrected_transcript"] = {
        "text": content,
        "saved_files": save_corrected_transcript(project_id, content),
    }
    project["review"] = None
    project["analysis"] = None
    project["status"] = "transcript_ready"
    save_project(project)
    return render_workspace(
        request,
        project,
        active_tab="review",
        success="Transcript text imported. You can run review immediately.",
    )


@app.post("/projects/{project_id}/review", response_class=HTMLResponse)
async def run_review(request: Request, project_id: str) -> HTMLResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    text = active_transcript_text(project)
    transcript = project.get("transcript")
    if not text or not isinstance(transcript, dict):
        return render_workspace(
            request,
            project,
            active_tab="review",
            error="Transcript is required before review can run.",
            status_code=400,
        )

    project["status"] = "reviewing"
    save_project(project)
    try:
        review = review_transcript(
            text=text,
            segments=transcript.get("segments", []),
            language=str(transcript.get("language", "auto")),
        )
    except Exception as exc:
        project["status"] = "failed"
        save_project(project)
        return render_workspace(
            request,
            project,
            active_tab="review",
            error=f"Review failed: {exc}",
            status_code=500,
        )
    dismissed_signatures = set(project.get("dismissed_review_signatures", []) or [])
    if dismissed_signatures:
        review["suggestions"] = [
            suggestion
            for suggestion in review.get("suggestions", [])
            if review_signature(suggestion) not in dismissed_signatures
        ]
        review["summary"] = (
            f"{review['summary']} "
            f"{len(dismissed_signatures)} dismissed finding(s) were hidden from this rerun."
        ).strip()
    project["review"] = review
    project["status"] = "reviewed"
    save_project(project)
    return render_workspace(request, project, active_tab="review", success="Review finished.")


@app.post("/projects/{project_id}/save-corrections", response_class=HTMLResponse)
async def save_corrections(
    request: Request,
    project_id: str,
    corrected_text: str = Form(...),
) -> HTMLResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    project["corrected_transcript"] = {
        "text": corrected_text,
        "saved_files": save_corrected_transcript(project_id, corrected_text),
    }
    save_project(project)
    return render_workspace(
        request,
        project,
        active_tab="review",
        success="Corrected transcript saved to the project folder.",
    )


@app.post("/projects/{project_id}/review-state")
async def save_review_state(
    project_id: str,
    corrected_text: str = Form(...),
    suggestions_json: str = Form("[]"),
    dismissed_json: str = Form("[]"),
) -> JSONResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    try:
        suggestions = json.loads(suggestions_json)
        dismissed = json.loads(dismissed_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid review payload: {exc}") from exc

    project["corrected_transcript"] = {
        "text": corrected_text,
        "saved_files": save_corrected_transcript(project_id, corrected_text),
    }
    if isinstance(project.get("review"), dict):
        project["review"]["suggestions"] = suggestions if isinstance(suggestions, list) else []
    project["dismissed_review_signatures"] = dismissed if isinstance(dismissed, list) else []
    save_project(project)
    return JSONResponse({"ok": True})


@app.post("/projects/{project_id}/analysis", response_class=HTMLResponse)
async def run_analysis(request: Request, project_id: str) -> HTMLResponse:
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    text = active_transcript_text(project)
    if not text:
        return render_workspace(
            request,
            project,
            active_tab="analysis",
            error="Transcript is required before analysis can run.",
            status_code=400,
        )

    project["status"] = "analyzing"
    save_project(project)
    try:
        analysis = analyze_transcript(text)
    except Exception as exc:
        project["status"] = "failed"
        save_project(project)
        return render_workspace(
            request,
            project,
            active_tab="analysis",
            error=f"Analysis failed: {exc}",
            status_code=500,
        )

    analysis_path = ensure_project_paths(project_id)["analysis"] / "analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
    project["analysis"] = {**analysis, "saved_path": str(analysis_path)}
    project["status"] = "analyzed"
    save_project(project)
    return render_workspace(request, project, active_tab="analysis", success="Analysis complete.")


@app.get("/projects/{project_id}/download/{job_id}/{artifact}")
async def download_artifact(project_id: str, job_id: str, artifact: str) -> FileResponse:
    if job_id == "corrected":
        return await download_corrected_artifact(project_id, artifact)
    if job_id == "analysis":
        return await download_analysis_artifact(project_id, artifact)

    allowed_artifacts = {"txt", "srt", "vtt", "json"}
    if artifact not in allowed_artifacts:
        raise HTTPException(status_code=404, detail="Unknown artifact.")
    target = project_dir(project_id) / "outputs" / job_id / f"transcript.{artifact}"
    if not target.exists():
        target = materialize_transcript_artifact(project_id, job_id, artifact)
    if not target or not target.exists():
        raise HTTPException(status_code=404, detail="Artifact not found.")
    media_type = {
        "txt": "text/plain",
        "srt": "application/x-subrip",
        "vtt": "text/vtt",
        "json": "application/json",
    }[artifact]
    return FileResponse(target, filename=target.name, media_type=media_type)


@app.get("/projects/{project_id}/download/corrected/{artifact}")
async def download_corrected_artifact(project_id: str, artifact: str) -> FileResponse:
    if artifact not in {"txt", "json"}:
        raise HTTPException(status_code=404, detail="Unknown corrected artifact.")
    target = project_dir(project_id) / "corrected" / f"corrected_transcript.{artifact}"
    if not target.exists():
        target = materialize_corrected_artifact(project_id, artifact)
    if not target or not target.exists():
        raise HTTPException(status_code=404, detail="Corrected artifact not found.")
    media_type = {
        "txt": "text/plain",
        "json": "application/json",
    }[artifact]
    return FileResponse(target, filename=target.name, media_type=media_type)


@app.get("/projects/{project_id}/download/analysis/{artifact}")
async def download_analysis_artifact(project_id: str, artifact: str) -> FileResponse:
    if artifact != "json":
        raise HTTPException(status_code=404, detail="Unknown analysis artifact.")
    target = project_dir(project_id) / "analysis" / "analysis.json"
    if not target.exists():
        raise HTTPException(status_code=404, detail="Analysis artifact not found.")
    return FileResponse(target, filename=target.name, media_type="application/json")
