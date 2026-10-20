"""Session persistence.

A session is one recording/upload plus everything derived from it. The
transcript is stored as a list of timed lines ("segments"); every other view
(corrected text, exports, review suggestions) is computed from those lines.

All mutations go through ``edit(session_id)`` which holds a per-session lock,
so the pipeline thread and API handlers never overwrite each other.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from paths import APP_DIR

SESSIONS_DIR = APP_DIR / "data" / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

SCHEMA_VERSION = 1

# Pipeline states. "busy" states mean a background job owns the session.
BUSY_STATUSES = {"recording", "transcribing", "reviewing", "analyzing"}

_cache: dict[str, dict[str, Any]] = {}
_locks: dict[str, threading.RLock] = {}
_registry_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def session_file(session_id: str) -> Path:
    return session_dir(session_id) / "session.json"


def audio_dir(session_id: str) -> Path:
    path = session_dir(session_id) / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lock_for(session_id: str) -> threading.RLock:
    with _registry_lock:
        if session_id not in _locks:
            _locks[session_id] = threading.RLock()
        return _locks[session_id]


def _read_json(path: Path) -> dict[str, Any]:
    # OneDrive can briefly lock files; retry before failing.
    for _ in range(8):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            time.sleep(0.15)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.15 * (attempt + 1))
    os.replace(tmp, path)


def new_session_payload(name: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "rev": 0,
        "id": uuid.uuid4().hex[:10],
        "name": name,
        "user_renamed": False,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        # idle | transcribing | reviewing | analyzing | ready | failed
        "status": "idle",
        "job": None,
        "error": None,
        "notice": None,
        "recording": None,
        "glossary": "",
        "audio": None,
        "transcript": None,
        "review": None,
        "analysis": None,
        "dismissed_signatures": [],
    }


def create_session(name: str = "New session") -> dict[str, Any]:
    payload = new_session_payload(name)
    with _lock_for(payload["id"]):
        _write_json(session_file(payload["id"]), payload)
        _cache[payload["id"]] = payload
    return json.loads(json.dumps(payload))


def _load(session_id: str) -> dict[str, Any] | None:
    if session_id in _cache:
        return _cache[session_id]
    target = session_file(session_id)
    if not target.exists():
        return None
    try:
        payload = _read_json(target)
    except (json.JSONDecodeError, PermissionError):
        return None
    _cache[session_id] = payload
    return payload


def get_session(session_id: str) -> dict[str, Any] | None:
    """Return a detached copy of the session (safe to read without the lock)."""
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return None
    with _lock_for(session_id):
        payload = _load(session_id)
        return json.loads(json.dumps(payload)) if payload else None


@contextmanager
def edit(session_id: str) -> Iterator[dict[str, Any]]:
    """Lock, yield the live session dict, then persist it."""
    with _lock_for(session_id):
        payload = _load(session_id)
        if payload is None:
            raise KeyError(session_id)
        yield payload
        # Monotonic revision: the browser drops snapshots older than what it has.
        payload["rev"] = int(payload.get("rev", 0)) + 1
        payload["updated_at"] = now_iso()
        _write_json(session_file(session_id), payload)


def list_sessions() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for candidate in SESSIONS_DIR.glob("*/session.json"):
        session = get_session(candidate.parent.name)
        if not session:
            continue
        items.append(
            {
                "id": session["id"],
                "name": session["name"],
                "status": session["status"],
                "updated_at": session.get("updated_at", ""),
                "created_at": session.get("created_at", ""),
                "duration": (session.get("audio") or {}).get("duration"),
            }
        )
    return sorted(items, key=lambda item: item["updated_at"], reverse=True)


def delete_session(session_id: str) -> None:
    with _lock_for(session_id):
        _cache.pop(session_id, None)
        target = session_dir(session_id)
        if target.exists():
            shutil.rmtree(target, onexc=_force_remove)


def _force_remove(function, path: str, excinfo) -> None:
    try:
        os.chmod(path, 0o666)
        function(path)
    except (FileNotFoundError, PermissionError):
        # Best effort on Windows/OneDrive.
        return


def recover_interrupted_sessions() -> None:
    """Jobs live in threads; a server restart kills them. Mark them clearly."""
    for candidate in SESSIONS_DIR.glob("*/session.json"):
        session_id = candidate.parent.name
        session = get_session(session_id)
        if not session or session.get("status") not in BUSY_STATUSES - {"recording"}:
            continue  # recordings are salvaged by recording.recover_interrupted()
        with edit(session_id) as live:
            step = {"transcribing": "Transcription", "reviewing": "Review", "analyzing": "Analysis"}[live["status"]]
            live["job"] = None
            live["status"] = "failed"
            live["error"] = f"{step} was interrupted because the app was restarted. Run it again."


# ── Transcript helpers ─────────────────────────────────────────────────────


def segments_text(segments: list[dict[str, Any]], field: str = "text") -> str:
    return "\n".join(str(seg.get(field, "")).strip() for seg in segments if str(seg.get(field, "")).strip())


def review_signature(suggestion: dict[str, Any]) -> str:
    return f"{str(suggestion.get('original', '')).strip().lower()}=>{str(suggestion.get('suggested', '')).strip().lower()}"


def public_view(session: dict[str, Any]) -> dict[str, Any]:
    """Session as sent to the browser (no absolute paths)."""
    view = json.loads(json.dumps(session))
    audio = view.get("audio")
    if isinstance(audio, dict):
        audio.pop("path", None)
        audio["url"] = f"/api/sessions/{view['id']}/audio"
    view.pop("dismissed_signatures", None)
    return view
