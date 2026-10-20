from __future__ import annotations

import json
import os
from typing import Any

import requests


def analyze_transcript(text: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return local_analysis(text)

    model = os.getenv("STT_ANALYSIS_MODEL", "gpt-5-mini")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    response = requests.post(
        f"{base_url.rstrip('/')}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": [
                {
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Analyze the transcript and return only JSON. "
                                "Provide a concise executive summary, key points, action items, and open questions."
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "transcript_analysis",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "key_points": {"type": "array", "items": {"type": "string"}},
                            "action_items": {"type": "array", "items": {"type": "string"}},
                            "open_questions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["summary", "key_points", "action_items", "open_questions"],
                        "additionalProperties": False,
                    },
                }
            },
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    content = extract_output_text(payload)
    parsed = json.loads(content)
    parsed["mode"] = "llm"
    parsed["model"] = model
    return parsed


def extract_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        for content in item.get("content", []):
            value = content.get("text")
            if isinstance(value, str) and value.strip():
                return value
    raise RuntimeError("No structured analysis output was returned by the Responses API.")


def local_analysis(text: str) -> dict[str, Any]:
    cleaned_lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = " ".join(cleaned_lines) if cleaned_lines else text.strip()
    sentences = [sentence.strip() for sentence in joined.split(".") if sentence.strip()]
    return {
        "mode": "local",
        "model": "local-fallback",
        "summary": sentences[0] if sentences else "No transcript content was available for analysis.",
        "key_points": sentences[:5],
        "action_items": [],
        "open_questions": [],
    }
