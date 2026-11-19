"""Run the real app with the fake Groq client: `python -m tests.e2e.server <port>`."""

import sys

import uvicorn

from backend.app.services import qa_service, summarization_service, transcription_service
from tests.e2e.fake_groq import FakeGroq

for module in (transcription_service, summarization_service, qa_service):
    module.Groq = FakeGroq

from backend.app.main import app  # noqa: E402  (after the patch, so nothing grabs the real client)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
