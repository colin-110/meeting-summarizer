"""Groq Whisper transcription.

Provider-specific code stays isolated here — the rest of the app only
ever calls transcribe(audio_path); swapping ASR providers later means
rewriting this one file, nothing upstream of it.
"""

from pathlib import Path
from typing import Optional

from groq import APIConnectionError, Groq, InternalServerError, RateLimitError

from backend.app.utils.errors import clean_provider_message
from backend.app.utils.retry import call_with_retry

_MODEL = "whisper-large-v3-turbo"
_RETRYABLE: tuple[type[BaseException], ...] = (APIConnectionError, RateLimitError, InternalServerError)


class TranscriptionError(RuntimeError):
    """Raised when transcription fails. Message is safe to show the user."""


def transcribe(audio_path: str, *, client: Optional[Groq] = None) -> str:
    client = client or Groq()
    path = Path(audio_path)

    def _call() -> str:
        with path.open("rb") as f:
            result = client.audio.transcriptions.create(
                file=(path.name, f.read()),
                model=_MODEL,
                temperature=0,
                response_format="verbose_json",
            )
        return result.text

    try:
        return call_with_retry(_call, retry_on=_RETRYABLE)
    except _RETRYABLE as exc:
        raise TranscriptionError(
            f"Transcription service unavailable after retries: {clean_provider_message(exc)}"
        ) from exc
    except Exception as exc:
        raise TranscriptionError(f"Transcription failed: {clean_provider_message(exc)}") from exc
