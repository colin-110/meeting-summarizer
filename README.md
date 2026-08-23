# Meeting Summarizer

Upload a meeting recording, get back a transcript, a summary, the decisions
that were made, and a list of action items with owners and deadlines.

## Status

🚧 In progress.

- [x] Phase 1 — repository skeleton, dependencies, CI
- [x] Phase 2 — FastAPI app + SQLite persistence
- [x] Phase 3 — audio upload API + validation
- [x] Phase 4 — background processing
- [x] Phase 5 — ASR integration (Groq Whisper)
- [x] Phase 6 — LLM summarization (Groq gpt-oss-120b, strict JSON schema)
- [ ] Phase 7 — result/status APIs
- [ ] Phase 8 — frontend
- [ ] Phase 9 — file-hash dedupe
- [ ] Phase 10 — validation, retries, error handling
- [ ] Phase 11+ — docs, demo video, cleanup

## Stack

FastAPI, SQLite (stdlib `sqlite3`, no ORM), FastAPI `BackgroundTasks` for
async processing, a plain HTML/CSS/JS frontend. No Docker, Redis, Celery, or
frontend build step — see [Why no queue or cache](#why-no-queue-or-cache).

## Project structure

```
backend/app/
├── main.py              # FastAPI app + startup
├── api/
│   ├── health.py
│   └── meetings.py       # POST /api/v1/meetings — upload + validate
├── services/
│   ├── storage_service.py        # extension/signature/size checks, content-addressed storage
│   ├── processing_service.py     # orchestrates one meeting: transcribe -> summarize -> persist
│   ├── transcription_service.py  # Groq Whisper, isolated so the provider can change later
│   └── summarization_service.py  # Groq gpt-oss-120b, strict JSON-schema output
├── database/
│   ├── connection.py    # sqlite3 connection helper
│   ├── init_db.py       # schema creation
│   └── meetings_repo.py # CRUD against the meetings table
├── models/
│   └── meeting.py        # Meeting dataclass + status enum
├── core/
│   ├── config.py         # env-driven settings, no python-dotenv
│   └── logging.py
├── schemas/                # request/response models (later phases)
└── utils/
    └── retry.py           # retry helper for transient provider errors
frontend/                  # plain HTML/CSS/JS (later phase)
tests/
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
pip install -r requirements-dev.txt

cp .env.example .env
# then add GROQ_API_KEY=gsk_... to .env — free tier at https://console.groq.com

uvicorn backend.app.main:app --reload
```

Open <http://127.0.0.1:8000/health> — should return `{"status": "ok", "database": "ok"}`.
Interactive API docs at <http://127.0.0.1:8000/docs>.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness + a real SQLite round-trip |
| `POST` | `/api/v1/meetings` | Upload audio (multipart, field `audio`) → `201` + `{id, status}` |

Accepted formats: `.mp3`, `.wav`, `.m4a` — checked by extension, declared
content type, and a byte-signature sniff of the file itself, since the
brief specifically says not to trust the filename. Files are stored under
their own sha256 hash rather than the client-supplied name, which also
gives free dedupe: uploading identical content twice reuses the file on
disk instead of writing it again.

## Summarization

The LLM step runs on Groq's `openai/gpt-oss-120b` with `response_format`
set to a strict JSON schema — one of only two models on Groq where `strict:
true` is actually enforced by constrained decoding rather than best-effort,
so a malformed response isn't a failure mode to defend against so much as
one to double-check (`_validate_result` still does, on principle).

The system prompt (`summarization_service._SYSTEM_PROMPT`) encodes the
distinctions that go wrong most often in meeting notes:

- a **decision** is something the group actually settled on, not something
  merely discussed or left open
- a missing owner or deadline becomes `null`, never a guessed name or an
  invented date
- the summary is a briefing for someone who missed the meeting, not a
  restatement of the transcript

## Tests

```bash
pytest -v
```

Runs automatically on every push via GitHub Actions
(`.github/workflows/tests.yml`).

## Why no queue or cache

The assignment's own submission guidelines ask for minimal, native
dependencies — "no extra modules," "use only what is strictly required."
FastAPI's built-in `BackgroundTasks` covers "process audio without making
the user wait" without a Celery/Redis broker, and a `file_hash` column on
the `meetings` table covers dedupe without a separate cache layer. Both are
one process, one database.
