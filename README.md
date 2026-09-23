# Meeting Summarizer

FastAPI service that processes meeting recordings asynchronously and returns transcripts, summaries, decisions, action items, and open questions.

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
          /        \\
         v          v
     Whisper       LLM
     (Groq)      (Groq)
          \\        /
           v      v
            Result
              |
              v
          SQLite
~~~

The upload endpoint returns a queued status instead of waiting for transcription and summarization. A background task performs processing while the frontend polls the meeting status.

## Engineering decisions

- **Asynchronous processing:** keep long-running audio/LLM work off the request path.
- **Content-hash deduplication:** avoid processing identical recordings more than once.
- **Layered file validation:** check extension, content type, size, and byte signatures.
- **Structured model output:** normalize decisions, action items, owners, deadlines, and open questions.
- **Failure handling:** explicitly handle provider errors, malformed responses, silent audio, rate limits, transient failures, and server restarts.
- **Evaluation:** keep model-quality evaluation separate from application behavior so quality changes can be measured.

## Accuracy evaluation

The repository includes a standalone evaluation pipeline using known meeting scripts.

Current documented results:

- Average transcription WER: approximately **1.8%**
- Decision recall: **18/18**
- Action-item recall: **21/21**
- Open-question recall: **14/15**

These results come from the repository's evaluation runs and should be treated as dataset-specific measurements, not general model accuracy.

## Tech stack

- Python 3.11+
- FastAPI
- SQLite
- Groq Whisper
- Groq \`gpt-oss-120b\`
- Plain HTML/CSS/JavaScript
- Pytest
- GitHub Actions

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

Run tests:

~~~bash
pytest -v
~~~

Run the evaluation:

~~~bash
python -m eval.run_accuracy_eval
~~~

## Repository structure

- \`backend/app/\` — API, processing, storage, database, models, and utilities
- \`frontend/\` — static frontend
- \`tests/\` — automated tests
- \`eval/\` — standalone evaluation
- \`.github/workflows/\` — CI

## License

See [LICENSE](LICENSE).
