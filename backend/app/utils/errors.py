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
