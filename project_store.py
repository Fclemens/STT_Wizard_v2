from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = BASE_DIR / "data" / "projects"
DELETED_IDS_PATH = BASE_DIR / "data" / "deleted_projects.json"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def project_file(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    # OneDrive can briefly lock files; retry a few times before failing.
    for _ in range(8):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            time.sleep(0.15)
    return json.loads(path.read_text(encoding="utf-8"))


def load_deleted_ids() -> set[str]:
    if not DELETED_IDS_PATH.exists():
        return set()
    try:
        payload = json.loads(DELETED_IDS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(payload, list):
        return {str(item) for item in payload}
    return set()


def save_deleted_ids(deleted_ids: set[str]) -> None:
    DELETED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DELETED_IDS_PATH.write_text(json.dumps(sorted(deleted_ids), indent=2), encoding="utf-8")


def create_project(name: str = "New Transcript") -> dict[str, Any]:
    project_id = uuid.uuid4().hex[:10]
    payload = {
        "id": project_id,
        "name": name,
        "user_renamed": False,
        "status": "draft",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "source_file": None,
        "transcript": None,
        "corrected_transcript": None,
        "review": None,
        "analysis": None,
    }
    save_project(payload)
    return payload


def save_project(payload: dict[str, Any]) -> None:
    payload["updated_at"] = now_iso()
    write_json(project_file(payload["id"]), payload)


def get_project(project_id: str) -> dict[str, Any] | None:
    if project_id in load_deleted_ids():
        return None
    target = project_file(project_id)
    if not target.exists():
        return None
    try:
        return read_json(target)
    except (json.JSONDecodeError, PermissionError):
        return None


def list_projects() -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    deleted_ids = load_deleted_ids()
    for candidate in PROJECTS_DIR.glob("*/project.json"):
        if candidate.parent.name in deleted_ids:
            continue
        try:
            projects.append(read_json(candidate))
        except (json.JSONDecodeError, PermissionError):
            continue
    return sorted(projects, key=lambda item: item.get("updated_at", ""), reverse=True)


def ensure_project_paths(project_id: str) -> dict[str, Path]:
    root = project_dir(project_id)
    audio_dir = root / "audio"
    output_dir = root / "outputs"
    review_dir = root / "review"
    analysis_dir = root / "analysis"
    corrected_dir = root / "corrected"
    for path in [audio_dir, output_dir, review_dir, analysis_dir, corrected_dir]:
        path.mkdir(parents=True, exist_ok=True)
    return {
        "root": root,
        "audio": audio_dir,
        "outputs": output_dir,
        "review": review_dir,
        "analysis": analysis_dir,
        "corrected": corrected_dir,
    }


def save_corrected_transcript(project_id: str, text: str) -> dict[str, str]:
    paths = ensure_project_paths(project_id)
    txt_path = paths["corrected"] / "corrected_transcript.txt"
    json_path = paths["corrected"] / "corrected_transcript.json"
    txt_path.write_text(text.strip() + "\n", encoding="utf-8")
    write_json(json_path, {"text": text})
    return {
        "txt_path": str(txt_path),
        "json_path": str(json_path),
    }


def review_signature(suggestion: dict[str, Any]) -> str:
    original = str(suggestion.get("original", "")).strip()
    context = str(suggestion.get("context", "")).strip()
    return f"{original}||{context}"


def delete_project(project_id: str) -> None:
    deleted_ids = load_deleted_ids()
    deleted_ids.add(project_id)
    save_deleted_ids(deleted_ids)
    target_dir = project_dir(project_id)
    project_json = project_file(project_id)
    if project_json.exists():
        force_unlink(project_json)
    if target_dir.exists():
        shutil.rmtree(target_dir, onexc=handle_remove_readonly)


def force_unlink(path: Path) -> None:
    for _ in range(10):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            try:
                os.chmod(path, 0o666)
            except FileNotFoundError:
                return
            time.sleep(0.2)
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        return


def handle_remove_readonly(function, path: str, excinfo) -> None:
    try:
        os.chmod(path, 0o666)
        function(path)
    except FileNotFoundError:
        return
    except PermissionError:
        # Best effort cleanup on Windows/OneDrive. The project is already hidden
        # from the UI once project.json is removed.
        return
