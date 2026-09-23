# Meeting Summarizer

An AI meeting transcription and analysis application built with FastAPI, SQLite, and Groq.

Upload a recording and receive a transcript, concise summary, decisions, action items with owners/deadlines, and open questions.

## Architecture

~~~text
Browser
   |
   v
FastAPI
   |
   +----> SQLite
   |
   +----> BackgroundTasks
              |
              v
       Processing service
          /        \
         v          v
     Whisper      LLM
     (Groq)      (Groq)
          \        /
           v      v
            Result
              |
              v
          SQLite
~~~

The upload endpoint returns immediately with a queued status. Transcription and summarization happen in a background task, while the frontend polls the meeting status.

## Key engineering work

- Asynchronous audio processing with FastAPI BackgroundTasks.
- Content-hash deduplication so identical recordings are not processed twice.
- File validation using extension, content type, size, and byte signatures.
- Structured LLM output for decisions, action items, owners, deadlines, and open questions.
- Explicit handling of provider failures, malformed responses, silent audio, rate limits, and server restarts.
- Retry handling for transient provider failures.
- Golden-dataset evaluation for transcription and summarization quality.
- Automated tests through GitHub Actions.

## Accuracy evaluation

The repository includes a separate evaluation pipeline using known meeting scripts.

Current documented results:

- Average transcription WER: approximately **1.8%** across the evaluation runs.
- Decision recall: **18/18** across three runs.
- Action-item recall: **21/21** across three runs.
- Open-question recall: **14/15** across three runs.

The evaluation is deliberately separate from the application so model quality can be measured rather than inferred from a few manual examples.

## Tech stack

- Python 3.11+
- FastAPI
- SQLite
- Groq Whisper
- Groq `gpt-oss-120b`
- Plain HTML/CSS/JavaScript
- Pytest
- GitHub Actions

No Redis, Celery, Docker, or frontend build system is required.

## Run locally

~~~bash
git clone https://github.com/colin-110/meeting-summarizer.git
cd meeting-summarizer

python -m venv .venv
pip install -r requirements-dev.txt

cp .env.example .env
# Set GROQ_API_KEY in .env

uvicorn backend.app.main:app --reload
~~~

Open `http://127.0.0.1:8000`.

Run tests:

~~~bash
pytest -v
~~~

Run the accuracy evaluation:

~~~bash
python -m eval.run_accuracy_eval
~~~

## Repository structure

- `backend/app/` — API, processing, storage, database, models, and utilities.
- `frontend/` — static frontend.
- `tests/` — automated tests.
- `eval/` — standalone accuracy evaluation.
- `.github/workflows/` — CI.

## License

See [LICENSE](LICENSE).
