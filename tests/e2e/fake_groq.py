"""A stand-in for the Groq client: same call shapes, canned answers.

Only the network call is faked. Everything around it is real: ffmpeg,
chunking, silence skipping, token budgets, JSON validation, the queue, the
database, the API and the frontend.
"""

import json
from types import SimpleNamespace

SEGMENTS = [
    {"start": 0.0, "end": 2.0, "text": "We decided to launch on March 3rd."},
    {"start": 2.0, "end": 4.0, "text": "Priya will send the budget by Friday."},
    {"start": 4.0, "end": 6.0, "text": "Who books the venue is still open."},
]

SUMMARY = {
    "title": "Launch planning sync",
    "summary": "The team agreed on the launch date and split the remaining work.",
    "key_decisions": ["Launch on March 3rd"],
    "action_items": [{"task": "Send the budget", "assignee": "Priya", "deadline": "Friday"}],
    "open_questions": ["Who books the venue?"],
}

ANSWER = "Priya is sending the budget by Friday."


def _transcribe(**kwargs):
    segments = [dict(s, no_speech_prob=0.01, avg_logprob=-0.2) for s in SEGMENTS]
    return SimpleNamespace(text=" ".join(s["text"] for s in SEGMENTS), segments=segments, duration=6.0)


def _chat(**kwargs):
    content = json.dumps(SUMMARY) if kwargs.get("response_format") else ANSWER
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=420),
    )


class FakeGroq:
    def __init__(self, *args, **kwargs):
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=_transcribe))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_chat))
