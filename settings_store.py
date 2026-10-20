from __future__ import annotations

import json
import os

from paths import APP_DIR

SETTINGS_PATH = APP_DIR / "settings.json"
ENV_PATH = APP_DIR / ".env"

DEFAULT_LLM_MODEL = "gpt-6-luna"

DEFAULT_SETTINGS = {
    "review_model": DEFAULT_LLM_MODEL,
    "analysis_model": DEFAULT_LLM_MODEL,
    "auto_review": True,
    "auto_analysis": False,
    "default_glossary": "",
    "default_model": "turbo",
    "default_language": "",
    "accurate_decoding": True,
    "record_mic": True,
    "record_system": True,
    "mic_device": "",
    "system_device": "",
}


def load_env_file() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip()


def load_settings() -> dict[str, object]:
    if not SETTINGS_PATH.exists():
        return DEFAULT_SETTINGS.copy()
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        stored = {}
    return {**DEFAULT_SETTINGS, **stored}


def save_settings(payload: dict[str, object]) -> dict[str, object]:
    merged = {**DEFAULT_SETTINGS, **payload}
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return merged


def update_env_key(key: str, value: str) -> None:
    entries: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            parsed_key, parsed_value = stripped.split("=", 1)
            entries[parsed_key.strip()] = parsed_value.strip()
    if value.strip():
        entries[key] = value.strip()
        os.environ[key] = value.strip()
    else:
        entries.pop(key, None)
        os.environ.pop(key, None)
    content = "\n".join(f"{entry_key}={entry_value}" for entry_key, entry_value in sorted(entries.items()))
    ENV_PATH.write_text(content + ("\n" if content else ""), encoding="utf-8")


def openai_ready() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())
