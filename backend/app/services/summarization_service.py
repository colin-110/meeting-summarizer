"""LLM summarization — placeholder.

The real implementation (prompt design, structured JSON schema, Groq
Llama call) lands in its own phase; this stub exists only so
processing_service has a stable function to import and orchestrate
around in the meantime.
"""


class SummarizationError(RuntimeError):
    """Raised when summarization fails. Message is safe to show the user."""


def summarize(transcript: str) -> dict:
    raise NotImplementedError("Summarization is not implemented yet.")
