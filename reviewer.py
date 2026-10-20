from __future__ import annotations

import json
import os
from typing import Any

import requests


def review_transcript(
    text: str,
    segments: list[dict[str, Any]],
    language: str,
) -> dict[str, Any]:
    review = llm_review(text=text, segments=segments, language=language)
    review["requested_mode"] = "llm"
    review["executed_mode"] = "llm"
    review["api_used"] = True
    return review


def llm_review(text: str, segments: list[dict[str, Any]], language: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    model = os.getenv("STT_REVIEW_MODEL", "gpt-5-mini")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    review_payload = build_review_payload(text=text, segments=segments, language=language)
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
                                "Review speech-to-text transcripts and return only JSON. "
                                "Flag only items likely to be wrong in context, such as misheard names, acronyms, systems, products, or technical terms. "
                                "Do not rewrite the full transcript. "
                                "For each finding, 'suggested' must contain only the proposed replacement text and nothing else. "
                                "Do not put alternatives, explanations, uncertainty notes, punctuation advice, or comments inside 'suggested'. "
                                "Put all rationale, caveats, and guidance for the user inside 'justification'. "
                                "Return an empty suggestions array if there are no likely issues."
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": json.dumps(review_payload, ensure_ascii=False)}],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "transcript_review",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "suggestions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "original": {"type": "string"},
                                        "suggested": {"type": "string"},
                                        "justification": {"type": "string"},
                                        "context": {"type": "string"},
                                    },
                                    "required": ["original", "suggested", "justification", "context"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["suggestions"],
                        "additionalProperties": False,
                    },
                }
            },
        },
        timeout=180,
    )
    if not response.ok:
        raise RuntimeError(extract_api_error(response))
    payload = response.json()
    content = extract_output_text(payload)
    parsed = json.loads(content)
    suggestions = parsed.get("suggestions", [])
    for suggestion in suggestions:
        suggestion.setdefault("suggested", suggestion.get("original", ""))
        suggestion.setdefault("justification", "Review the context before applying this replacement.")
        suggestion.setdefault("context", "")
    return {
        "mode": "llm",
        "language": language,
        "summary": f"LLM review using {model} flagged transcript items that may be wrong in context.",
        "suggestions": suggestions[:25],
    }


def build_review_payload(text: str, segments: list[dict[str, Any]], language: str) -> dict[str, Any]:
    compact_segments = [
        {
            "start": round(segment.get("start", 0), 2),
            "end": round(segment.get("end", 0), 2),
            "text": segment.get("text", "").strip(),
        }
        for segment in segments[:80]
    ]
    return {
        "language": language,
        "instruction": (
            "Find likely wrong words that do not make sense in context. Focus on names, acronyms, tools, systems, products, and odd substitutions. "
            "Only return candidates that should be reviewed by a human. "
            "Return a clean replacement suggestion in 'suggested' and put the user-facing explanation in 'justification'."
        ),
        "transcript": text,
        "segments": compact_segments,
    }


def extract_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text_value = content.get("text")
            if isinstance(text_value, str) and text_value.strip():
                return text_value
    raise RuntimeError("No structured text output was returned by the Responses API.")


def extract_api_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"{response.status_code} {response.reason}: {response.text.strip()}"

    error = payload.get("error", {})
    if isinstance(error, dict):
        message = error.get("message") or payload
        err_type = error.get("type")
        err_param = error.get("param")
        pieces = [str(message)]
        if err_type:
            pieces.append(f"type={err_type}")
        if err_param:
            pieces.append(f"param={err_param}")
        return " | ".join(pieces)
    return str(payload)
