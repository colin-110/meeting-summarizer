"""LLM summarization via Groq (openai/gpt-oss-120b), JSON-schema-constrained.

gpt-oss-120b is one of only two models on Groq that support strict
structured-output mode — constrained decoding that guarantees the
response matches SUMMARY_SCHEMA exactly, rather than best-effort
JSON that occasionally needs a repair pass. Provider-specific code
stays isolated here; the rest of the app only calls
summarize(transcript) -> dict.
"""

import json
from typing import Optional

from groq import APIConnectionError, Groq, InternalServerError, RateLimitError

from backend.app.utils.errors import clean_provider_message
from backend.app.utils.retry import call_with_retry

_MODEL = "openai/gpt-oss-120b"
_RETRYABLE: tuple[type[BaseException], ...] = (APIConnectionError, RateLimitError, InternalServerError)

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "A concise 2-4 sentence executive summary of the meeting.",
        },
        "key_decisions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Choices the group actually settled on — not options that were "
                "discussed, and not questions left open."
            ),
        },
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "assignee": {
                        "type": ["string", "null"],
                        "description": "The person responsible, or null if no owner was stated.",
                    },
                    "deadline": {
                        "type": ["string", "null"],
                        "description": "The due date as stated, or null if none was given.",
                    },
                },
                "required": ["task", "assignee", "deadline"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "key_decisions", "action_items"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
You turn meeting transcripts into structured notes for people who were not \
in the meeting.

Follow these rules exactly:

1. A decision is something the group actually settled on — a clear commitment \
or choice. Do not record something as a decision if it was only proposed, \
debated, or left open; those are not decisions.
2. An action item is a concrete task assigned to move something forward, \
whether or not an owner or deadline was stated for it.
3. If no owner was stated for a task, set "assignee" to null — never guess a \
name from context. If no deadline was stated, set "deadline" to null — never \
invent one.
4. The summary should read like a briefing for someone who missed the \
meeting: what it was about, what mattered, what happens next. Do not just \
restate the agenda or repeat the transcript.
5. Base everything only on the transcript provided. If it is too short or too \
unclear to support a field, leave that field empty (an empty array, or a \
summary that says so) rather than inventing content to fill it.
"""


class SummarizationError(RuntimeError):
    """Raised when summarization fails. Message is safe to show the user."""


def summarize(transcript: str, *, client: Optional[Groq] = None) -> dict:
    if not transcript or not transcript.strip():
        raise SummarizationError("Cannot summarize an empty transcript.")

    client = client or Groq()

    def _call() -> dict:
        response = client.chat.completions.create(
            model=_MODEL,
            temperature=0.2,
            reasoning_format="hidden",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Meeting transcript:\n\n{transcript}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "meeting_summary",
                    "schema": SUMMARY_SCHEMA,
                    "strict": True,
                },
            },
        )
        return json.loads(response.choices[0].message.content)

    try:
        result = call_with_retry(_call, retry_on=_RETRYABLE)
    except _RETRYABLE as exc:
        raise SummarizationError(
            f"Summarization service unavailable after retries: {clean_provider_message(exc)}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SummarizationError(f"Model returned malformed JSON: {exc}") from exc
    except Exception as exc:
        raise SummarizationError(f"Summarization failed: {clean_provider_message(exc)}") from exc

    _validate_result(result)
    return result


def _validate_result(result: dict) -> None:
    """Belt-and-suspenders check even under strict mode — never trust an
    external system blindly, and this is cheap insurance against a schema
    change or provider bug slipping bad data into the database."""
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        raise SummarizationError("Model response is missing a summary.")
    if not isinstance(result.get("key_decisions"), list):
        raise SummarizationError("Model response is missing key_decisions.")
    if not isinstance(result.get("action_items"), list):
        raise SummarizationError("Model response is missing action_items.")
    for item in result["action_items"]:
        if not isinstance(item, dict) or "task" not in item:
            raise SummarizationError("Model response has a malformed action item.")
