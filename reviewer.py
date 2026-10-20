"""LLM review of a transcript, line by line.

Each suggestion points at one transcript line (``segment_id``) and an exact
substring of it, so applying it edits only that line.

The whole transcript is reviewed in one call whenever it fits (a 2-hour
meeting is ~120k characters), so the model sees every mention of a name or
term. Longer transcripts are split into parts, and each part is sent with the
full transcript as read-only context so corrections stay consistent.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Callable

import requests

from settings_store import DEFAULT_LLM_MODEL, load_settings

SINGLE_CALL_MAX_CHARS = 200_000   # ~10 h of speech, ~50k tokens: review in one call
PART_MAX_CHARS = 60_000           # longer transcripts: parts of this size...
CONTEXT_MAX_CHARS = 1_000_000     # ...each with the full transcript as context, if it fits
MAX_SUGGESTIONS_PER_CALL = 150
REQUEST_TIMEOUT_SECONDS = 600

INSTRUCTIONS = (
    "You review speech-to-text transcripts and return only JSON. "
    "Each transcript line is prefixed with its id in square brackets. "
    "Flag only words that are likely wrong in context: misheard names, acronyms, companies, systems, products, "
    "technical terms, or clearly wrong word substitutions. Do not rewrite style, grammar or punctuation. "
    "For each finding: 'segment_id' is the id of the line; 'original' is the exact text as it appears in that line "
    "(copy it character for character, keep it short: just the wrong word or phrase); 'suggested' is only the "
    "replacement text, with no alternatives or comments; 'justification' explains why, briefly, for the user. "
    "When a glossary is provided, treat it as the authoritative spelling of names and terms and flag near-misses. "
    "Use the whole transcript to keep corrections consistent: a name or term spelled clearly in one place tells you "
    "how garbled versions elsewhere should read. If 'context' is provided, it is the full transcript for reference "
    "only: flag lines from 'transcript' only. "
    f"Return at most {MAX_SUGGESTIONS_PER_CALL} suggestions, most important first, "
    "or an empty suggestions array if nothing is likely wrong."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_id": {"type": "string"},
                    "original": {"type": "string"},
                    "suggested": {"type": "string"},
                    "justification": {"type": "string"},
                },
                "required": ["segment_id", "original", "suggested", "justification"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def format_lines(segments: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{seg['id']}] {seg['text'].strip()}" for seg in segments)


def batch_segments(segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One batch when the transcript fits in a single call, else parts of PART_MAX_CHARS."""
    if len(format_lines(segments)) <= SINGLE_CALL_MAX_CHARS:
        return [segments] if segments else []
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for segment in segments:
        length = len(segment.get("text", "")) + 12
        if current and size + length > PART_MAX_CHARS:
            batches.append(current)
            current, size = [], 0
        current.append(segment)
        size += length
    if current:
        batches.append(current)
    return batches


def review_segments(
    segments: list[dict[str, Any]],
    language: str,
    glossary: str,
    on_progress: Callable[[int, int], Any] | None = None,
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("No OpenAI API key is configured. Add one in Settings.")
    model = str(load_settings().get("review_model") or DEFAULT_LLM_MODEL)

    by_id = {seg["id"]: seg for seg in segments}
    spoken = [seg for seg in segments if seg.get("text", "").strip()]
    batches = batch_segments(spoken)
    full_text = format_lines(spoken)
    context = full_text if len(batches) > 1 and len(full_text) <= CONTEXT_MAX_CHARS else None
    suggestions: list[dict[str, Any]] = []
    discarded = 0
    seen: set[tuple[str, str, str]] = set()

    for index, batch in enumerate(batches):
        if on_progress:
            on_progress(index, len(batches))
        raw = _call_llm(api_key, model, batch, language, glossary, context)
        for item in raw[:MAX_SUGGESTIONS_PER_CALL]:
            segment = by_id.get(str(item.get("segment_id", "")).strip())
            original = str(item.get("original", "")).strip()
            suggested = str(item.get("suggested", "")).strip()
            if not segment or not original or not suggested or original == suggested:
                discarded += 1
                continue
            if find_span(segment["text"], original) is None:
                discarded += 1
                continue
            key = (segment["id"], original.lower(), suggested.lower())
            if key in seen:
                continue
            seen.add(key)
            suggestions.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "segment_id": segment["id"],
                    "original": original,
                    "suggested": suggested,
                    "justification": str(item.get("justification", "")).strip(),
                    "status": "pending",
                }
            )

    return {
        "model": model,
        "created_at": None,
        "batches": len(batches),
        "full_context": len(batches) == 1 or context is not None,
        "discarded": discarded,
        "glossary_used": bool(glossary.strip()),
        "suggestions": suggestions,
    }


def _call_llm(
    api_key: str, model: str, batch: list[dict[str, Any]], language: str, glossary: str, context: str | None
) -> list[dict[str, Any]]:
    user_payload = {"language": language, "glossary": glossary.strip() or None, "transcript": format_lines(batch)}
    if context:
        user_payload["context"] = context
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    response = requests.post(
        f"{base_url.rstrip('/')}/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "input": [
                {"role": "developer", "content": [{"type": "input_text", "text": INSTRUCTIONS}]},
                {"role": "user", "content": [{"type": "input_text", "text": json.dumps(user_payload, ensure_ascii=False)}]},
            ],
            "text": {"format": {"type": "json_schema", "name": "transcript_review", "strict": True, "schema": SCHEMA}},
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise RuntimeError(extract_api_error(response))
    parsed = json.loads(extract_output_text(response.json()))
    return parsed.get("suggestions", [])


def find_span(text: str, needle: str) -> tuple[int, int] | None:
    """Locate ``needle`` in ``text``: exact first, then case-insensitive."""
    index = text.find(needle)
    if index >= 0:
        return index, index + len(needle)
    match = re.search(re.escape(needle), text, flags=re.IGNORECASE)
    if match:
        return match.start(), match.end()
    return None


def apply_to_text(text: str, original: str, replacement: str) -> str | None:
    span = find_span(text, original)
    if span is None:
        return None
    return text[: span[0]] + replacement + text[span[1] :]


def extract_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        for content in item.get("content", []) or []:
            value = content.get("text")
            if isinstance(value, str) and value.strip():
                return value
    raise RuntimeError("The Responses API returned no structured output.")


def extract_api_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"{response.status_code} {response.reason}: {response.text.strip()[:300]}"
    error = payload.get("error", {})
    if isinstance(error, dict):
        pieces = [str(error.get("message") or payload)]
        if error.get("type"):
            pieces.append(f"type={error['type']}")
        if error.get("param"):
            pieces.append(f"param={error['param']}")
        return " | ".join(pieces)
    return str(payload)
