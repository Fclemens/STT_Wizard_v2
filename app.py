from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import events
import pipeline
import session_store as store
from reviewer import apply_to_text
from settings_store import DEFAULT_LLM_MODEL, load_env_file, load_settings, openai_ready, save_settings, update_env_key
from stt_engine import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_TEXT_EXTENSIONS,
    MODEL_OPTIONS,
    get_model_statuses,
    to_srt,
    to_txt,
    to_vtt,
)

load_env_file()

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.recover_interrupted_sessions()
    pipeline.start_workers()
    yield


app = FastAPI(title="STT Wizard", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ── Helpers ────────────────────────────────────────────────────────────────


def require_session(session_id: str) -> dict[str, Any]:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


def require_idle(session: dict[str, Any]) -> None:
    if pipeline.is_busy(session):
        raise HTTPException(status_code=409, detail="Wait for the current step to finish (or stop it) first.")


def session_response(session_id: str) -> JSONResponse:
    session = require_session(session_id)
    events.publish(session_id, "session", store.public_view(session))
    return JSONResponse(store.public_view(session))


def name_from_file(file_name: str) -> str:
    stem = Path(file_name).stem.replace("_", " ").replace("-", " ").strip()
    return " ".join(word[:1].upper() + word[1:] for word in stem.split()[:8]) or "New session"


def public_settings() -> dict[str, Any]:
    return {**load_settings(), "api_key_configured": openai_ready()}


# ── Pages ──────────────────────────────────────────────────────────────────


@app.get("/")
def index() -> RedirectResponse:
    sessions = store.list_sessions()
    target = sessions[0]["id"] if sessions else store.create_session()["id"]
    return RedirectResponse(url=f"/sessions/{target}", status_code=303)


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
def session_page(request: Request, session_id: str) -> HTMLResponse:
    session = store.get_session(session_id)
    if not session:
        return RedirectResponse(url="/", status_code=303)
    bootstrap = {
        "session": store.public_view(session),
        "sessions": store.list_sessions(),
        "settings": public_settings(),
        "models": get_model_statuses(),
        "audio_extensions": sorted(ALLOWED_AUDIO_EXTENSIONS),
        "text_extensions": sorted(ALLOWED_TEXT_EXTENSIONS),
    }
    return templates.TemplateResponse(
        request,
        "session.html",
        {"session": session, "bootstrap_json": json.dumps(bootstrap, ensure_ascii=False).replace("</", "<\\/")},
    )


# ── Sessions ───────────────────────────────────────────────────────────────


@app.get("/api/sessions")
def api_list_sessions() -> list[dict[str, Any]]:
    return store.list_sessions()


@app.post("/api/sessions")
def api_create_session(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip() or "New session"
    return store.public_view(store.create_session(name))


@app.get("/api/sessions/{session_id}")
def api_get_session(session_id: str) -> dict[str, Any]:
    return store.public_view(require_session(session_id))


@app.patch("/api/sessions/{session_id}")
def api_update_session(session_id: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    require_session(session_id)
    with store.edit(session_id) as live:
        if "name" in payload and str(payload["name"]).strip():
            live["name"] = str(payload["name"]).strip()[:120]
            live["user_renamed"] = True
        if "glossary" in payload:
            live["glossary"] = str(payload["glossary"])[:4000]
    return session_response(session_id)


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    if session.get("status") == "transcribing":
        pipeline.stop(session_id)
    store.delete_session(session_id)
    return {"ok": True}


@app.get("/api/sessions/{session_id}/events")
async def api_session_events(request: Request, session_id: str) -> StreamingResponse:
    require_session(session_id)
    queue = events.subscribe(session_id)

    async def stream():
        try:
            session = store.get_session(session_id)
            if session:
                yield f"event: session\ndata: {json.dumps(store.public_view(session), ensure_ascii=False)}\n\n"
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            events.unsubscribe(session_id, queue)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ── Input: audio upload or transcript import ───────────────────────────────


@app.post("/api/sessions/{session_id}/upload")
def api_upload(session_id: str, file: UploadFile = File(...)) -> JSONResponse:
    session = require_session(session_id)
    require_idle(session)
    original_name = Path(file.filename or "audio").name
    suffix = Path(original_name).suffix.lower()

    if suffix in ALLOWED_TEXT_EXTENSIONS:
        return import_transcript(session_id, original_name, file)
    if suffix not in ALLOWED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_AUDIO_EXTENSIONS | ALLOWED_TEXT_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Use one of: {allowed}.")

    folder = store.audio_dir(session_id)
    stored_name = f"{uuid.uuid4().hex[:8]}{suffix}"
    with (folder / stored_name).open("wb") as target:
        shutil.copyfileobj(file.file, target, length=1024 * 1024)

    previous = (session.get("audio") or {}).get("file")
    with store.edit(session_id) as live:
        live["audio"] = {
            "file": stored_name,
            "original_name": original_name,
            "kind": "upload",
            "size": (folder / stored_name).stat().st_size,
            "duration": None,
            "added_at": store.now_iso(),
        }
        live["transcript"] = None
        live["review"] = None
        live["analysis"] = None
        live["status"] = "idle"
        live["error"] = None
        if not live.get("user_renamed"):
            live["name"] = name_from_file(original_name)
    if previous and previous != stored_name:
        (folder / previous).unlink(missing_ok=True)
    return session_response(session_id)


def import_transcript(session_id: str, original_name: str, file: UploadFile) -> JSONResponse:
    content = file.file.read().decode("utf-8", errors="replace")
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="The transcript file is empty.")
    segments = [
        {"id": f"s{index:05}", "start": None, "end": None, "text": line, "original_text": line, "speaker": None}
        for index, line in enumerate(lines, start=1)
    ]
    with store.edit(session_id) as live:
        live["audio"] = None
        live["transcript"] = {
            "source": "import",
            "model": "Imported text",
            "language": "",
            "source_file": original_name,
            "started_at": store.now_iso(),
            "completed_at": store.now_iso(),
            "stopped": False,
            "segments": segments,
        }
        live["review"] = None
        live["analysis"] = None
        live["status"] = "ready"
        live["error"] = None
        if not live.get("user_renamed"):
            live["name"] = name_from_file(original_name)
    return session_response(session_id)


@app.get("/api/sessions/{session_id}/audio")
def api_audio(session_id: str) -> FileResponse:
    session = require_session(session_id)
    audio = session.get("audio")
    if not audio:
        raise HTTPException(status_code=404, detail="No audio in this session.")
    path = store.audio_dir(session_id) / audio["file"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio file is missing.")
    return FileResponse(path, filename=audio.get("original_name") or path.name)


# ── Pipeline actions ───────────────────────────────────────────────────────


@app.post("/api/sessions/{session_id}/transcribe")
def api_transcribe(session_id: str, payload: dict[str, Any] = Body(default={})) -> JSONResponse:
    session = require_session(session_id)
    require_idle(session)
    settings = load_settings()
    model = str(payload.get("model") or settings.get("default_model") or "turbo")
    if model not in MODEL_OPTIONS:
        raise HTTPException(status_code=400, detail="Unknown model.")
    task = "translate" if payload.get("task") == "translate" else "transcribe"
    options = {
        "model": model,
        "language": str(payload.get("language") or "").strip().lower(),
        "task": task,
        "accurate": bool(payload.get("accurate", settings.get("accurate_decoding", True))),
        "glossary": pipeline.effective_glossary(session),
    }
    try:
        pipeline.start_transcription(session_id, options)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(store.public_view(require_session(session_id)))


@app.post("/api/sessions/{session_id}/stop")
def api_stop(session_id: str) -> dict[str, Any]:
    require_session(session_id)
    pipeline.stop(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/review")
def api_review(session_id: str) -> JSONResponse:
    require_session(session_id)
    if not openai_ready():
        raise HTTPException(status_code=400, detail="Review needs an OpenAI API key. Add it in Settings.")
    try:
        pipeline.start_review(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(store.public_view(require_session(session_id)))


@app.post("/api/sessions/{session_id}/analysis")
def api_analysis(session_id: str) -> JSONResponse:
    require_session(session_id)
    try:
        pipeline.start_analysis(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(store.public_view(require_session(session_id)))


# ── Editing ────────────────────────────────────────────────────────────────


@app.patch("/api/sessions/{session_id}/segments/{segment_id}")
def api_edit_segment(session_id: str, segment_id: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    require_session(session_id)
    text = str(payload.get("text", "")).strip()
    with store.edit(session_id) as live:
        segments = (live.get("transcript") or {}).get("segments") or []
        segment = next((seg for seg in segments if seg["id"] == segment_id), None)
        if not segment:
            raise HTTPException(status_code=404, detail="Line not found.")
        segment["text"] = text
        result = dict(segment)
    events.publish(session_id, "segment", result)
    return JSONResponse(result)


@app.post("/api/sessions/{session_id}/suggestions/{suggestion_id}")
def api_suggestion_action(session_id: str, suggestion_id: str, payload: dict[str, Any] = Body(...)) -> JSONResponse:
    session = require_session(session_id)
    if session.get("status") == "reviewing":
        raise HTTPException(status_code=409, detail="Wait for the review to finish.")
    action = payload.get("action")
    with store.edit(session_id) as live:
        review = live.get("review") or {}
        suggestion = next((item for item in review.get("suggestions", []) if item["id"] == suggestion_id), None)
        if not suggestion:
            raise HTTPException(status_code=404, detail="Suggestion not found.")
        if action == "apply":
            ok = _apply(live, suggestion, str(payload.get("replacement") or suggestion["suggested"]))
            if not ok:
                raise HTTPException(status_code=409, detail="The original text is no longer in that line (it was edited).")
        elif action == "dismiss":
            suggestion["status"] = "dismissed"
            signatures = set(live.get("dismissed_signatures") or [])
            signatures.add(store.review_signature(suggestion))
            live["dismissed_signatures"] = sorted(signatures)
        elif action == "revert":
            if suggestion["status"] == "applied":
                segments = (live.get("transcript") or {}).get("segments") or []
                segment = next((seg for seg in segments if seg["id"] == suggestion["segment_id"]), None)
                reverted = apply_to_text(segment["text"], suggestion.get("applied", ""), suggestion["original"]) if segment else None
                if reverted is None:
                    raise HTTPException(status_code=409, detail="That line was edited since; undo it by hand.")
                segment["text"] = reverted
            suggestion["status"] = "pending"
            suggestion.pop("applied", None)
        elif action == "reopen":
            suggestion["status"] = "pending"
            signatures = set(live.get("dismissed_signatures") or [])
            signatures.discard(store.review_signature(suggestion))
            live["dismissed_signatures"] = sorted(signatures)
        else:
            raise HTTPException(status_code=400, detail="Unknown action.")
    return session_response(session_id)


@app.post("/api/sessions/{session_id}/suggestions-apply-all")
def api_apply_all(session_id: str) -> JSONResponse:
    session = require_session(session_id)
    require_idle(session)
    with store.edit(session_id) as live:
        for suggestion in (live.get("review") or {}).get("suggestions", []):
            if suggestion["status"] == "pending":
                _apply(live, suggestion, suggestion["suggested"])
    return session_response(session_id)


def _apply(live: dict[str, Any], suggestion: dict[str, Any], replacement: str) -> bool:
    segments = (live.get("transcript") or {}).get("segments") or []
    segment = next((seg for seg in segments if seg["id"] == suggestion["segment_id"]), None)
    updated = apply_to_text(segment["text"], suggestion["original"], replacement) if segment else None
    if updated is None:
        suggestion["status"] = "stale"
        return False
    segment["text"] = updated
    suggestion["status"] = "applied"
    suggestion["applied"] = replacement
    return True


# ── Export ─────────────────────────────────────────────────────────────────


@app.get("/api/sessions/{session_id}/export")
def api_export(session_id: str, format: str = "txt", version: str = "corrected") -> PlainTextResponse:
    session = require_session(session_id)
    transcript = session.get("transcript") or {}
    segments = transcript.get("segments") or []
    if not segments:
        raise HTTPException(status_code=404, detail="Nothing to export yet.")
    field = "original_text" if version == "original" else "text"
    timed = transcript.get("source") != "import"
    base = f"{session['name']} ({'original' if version == 'original' else 'corrected'})"

    if format == "txt":
        body, media = to_txt(segments, field), "text/plain"
    elif format == "srt" and timed:
        body, media = to_srt(segments, field), "application/x-subrip"
    elif format == "vtt" and timed:
        body, media = to_vtt(segments, field), "text/vtt"
    elif format == "json":
        payload = {
            "name": session["name"],
            "language": transcript.get("language"),
            "model": transcript.get("model"),
            "segments": [
                {"start": seg["start"], "end": seg["end"], "speaker": seg.get("speaker"), "text": seg.get(field, "")}
                for seg in segments
            ],
        }
        if session.get("analysis"):
            payload["analysis"] = session["analysis"]
        body, media = json.dumps(payload, indent=2, ensure_ascii=False), "application/json"
    else:
        raise HTTPException(status_code=400, detail="Unsupported export format for this transcript.")

    safe = "".join(ch for ch in base if ch.isalnum() or ch in " ()-_").strip() or "transcript"
    return PlainTextResponse(
        body,
        media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{requests.utils.quote(safe)}.{format}"},
    )


# ── Settings ───────────────────────────────────────────────────────────────


@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    return public_settings()


@app.post("/api/settings")
def api_save_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    current = load_settings()
    allowed = {
        "review_model": str,
        "analysis_model": str,
        "auto_review": bool,
        "auto_analysis": bool,
        "default_glossary": str,
        "default_model": str,
        "default_language": str,
        "accurate_decoding": bool,
    }
    for key, kind in allowed.items():
        if key in payload:
            current[key] = kind(payload[key])
    for key in ("review_model", "analysis_model"):
        current[key] = str(current.get(key) or "").strip() or DEFAULT_LLM_MODEL
    if current.get("default_model") not in MODEL_OPTIONS:
        current["default_model"] = "turbo"
    save_settings(current)
    if str(payload.get("openai_api_key") or "").strip():
        update_env_key("OPENAI_API_KEY", str(payload["openai_api_key"]))
    return public_settings()


@app.post("/api/settings/glossary-term")
def api_add_glossary_term(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    term = str(payload.get("term", "")).strip()
    if not term:
        raise HTTPException(status_code=400, detail="Empty term.")
    settings = load_settings()
    lines = [line.strip() for line in str(settings.get("default_glossary", "")).splitlines() if line.strip()]
    if term.lower() not in {line.lower() for line in lines}:
        lines.append(term)
    settings["default_glossary"] = "\n".join(lines)
    save_settings(settings)
    return public_settings()


@app.post("/api/settings/test-key")
def api_test_key(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    candidate = (str(payload.get("openai_api_key") or "") or os.getenv("OPENAI_API_KEY", "")).strip()
    if not candidate:
        return {"ok": False, "message": "No OpenAI API key is configured yet."}
    try:
        response = requests.get(
            "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {candidate}"}, timeout=15
        )
    except requests.RequestException as exc:
        return {"ok": False, "message": f"Could not reach OpenAI: {exc}"}
    if response.ok:
        return {"ok": True, "message": "The key works."}
    return {"ok": False, "message": f"OpenAI rejected the key (HTTP {response.status_code})."}
