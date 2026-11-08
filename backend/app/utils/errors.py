"""Extracts a clean, user-facing message from a provider exception.

Groq (and OpenAI-compatible APIs generally) put the actually useful text
in exc.body["error"]["message"] — str(exc) gives back the whole
"Error code: 400 - {'error': {...}}" repr, which is fine in a log line
but not something to show a user.
"""


def clean_provider_message(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return str(exc)


class ProviderBusyError(RuntimeError):
    """The AI provider is out of capacity (rate/quota limit). Not a failure of
    the job itself: the job should be retried later, after retry_after seconds."""

    def __init__(self, message: str, retry_after: float = 60.0):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(exc: BaseException, default: float = 60.0) -> float:
    """Best-effort read of a provider's Retry-After header."""
    response = getattr(exc, "response", None)
    try:
        return float(response.headers.get("retry-after"))
    except (AttributeError, TypeError, ValueError):
        return default
