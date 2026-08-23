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
- [x] Phase 7 — result/status APIs
- [x] Phase 8 — frontend
- [x] Phase 9 — file-hash dedupe *(landed inside Phases 2 &amp; 4, not a separate step)*
- [x] Phase 10 — validation, retries, error handling *(built incrementally alongside Phases 3, 5, 6 rather than as one pass — see [Error handling & edge cases](#error-handling--edge-cases))*
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
├── schemas/
│   └── meeting.py         # API response shapes (Pydantic)
└── utils/
    ├── retry.py           # retry helper for transient provider errors
    └── errors.py          # extracts a clean message from a provider exception
frontend/
├── index.html            # upload form + status/result/error views
├── style.css
└── app.js                # upload -> poll status -> render result, plus history
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

Open <http://127.0.0.1:8000> for the app itself, or
<http://127.0.0.1:8000/health> for a liveness check
(`{"status": "ok", "database": "ok"}`). Interactive API docs at
<http://127.0.0.1:8000/docs>.

## API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness + a real SQLite round-trip |
| `POST` | `/api/v1/meetings` | Upload audio (multipart, field `audio`) → `201` + `{id, status}` |
| `GET` | `/api/v1/meetings` | List all meetings (id, filename, status, timestamps) |
| `GET` | `/api/v1/meetings/{id}` | Full result — transcript, summary, key_decisions, action_items |
| `GET` | `/api/v1/meetings/{id}/status` | Lightweight status poll — `{id, status, error_message}` |

An unknown `{id}` returns `404` on both the detail and status routes. Poll
`/status` while a meeting is `QUEUED`/`PROCESSING`; fetch `/{id}` once it's
`COMPLETED` (or read `error_message` if it's `FAILED`).

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

## Frontend

Plain HTML/CSS/vanilla JS — no React, no build step, no `node_modules`.
FastAPI serves it directly via `StaticFiles` (mounted at `/`, after the API
routers so it never shadows them), so the whole app is one process on one
port.

Upload a file and `app.js` polls `/status` every 2 seconds, moving through
`Queued… → Processing… → ` a rendered result — summary, key decisions, a
checklist of action items with owner and deadline, and the transcript
behind a `<details>` toggle. A failure shows the actual `error_message`
from the API, not a generic "something went wrong." A "Recent meetings"
list at the bottom reuses `GET /api/v1/meetings` to let you reopen any
past result (or re-check one still processing) without re-uploading.

## Error handling & edge cases

Every failure mode below was actually triggered and verified against the
real Groq API while building the pipeline — not just asserted in a mocked
test. Everything ends in a clean `FAILED` status with a readable
`error_message`; nothing surfaces as a raw stack trace or hangs.

| Case | Where it's caught | What happens |
| --- | --- | --- |
| Wrong file extension (e.g. `.exe`) | Upload, `storage_service.py` | `422` immediately, upload never written to disk |
| Right extension, wrong content (e.g. a renamed text file) | Upload, byte-signature check | `422` — the first bytes don't match the format's real magic number |
| Empty file (0 bytes) | Upload | `422` before any processing starts |
| File over `MAX_UPLOAD_MB` | Upload, streamed size check | `422` — aborted mid-stream, partial file cleaned up, not just rejected after a full upload |
| Valid header, corrupted/garbage payload (passes the upload check, isn't real audio) | ASR call | Groq's own `400` rejection is caught and surfaced verbatim, e.g. *"could not process file - is it a valid media file?"* — confirmed live with a hand-crafted WAV that has a valid `RIFF` header and garbage after it |
| Silent or no-speech audio | After transcription, `processing_service.py` | Whisper doesn't return empty for silence — it hallucinates short boilerplate (confirmed live: 2 seconds of true digital silence came back as `" Thank you."`). A minimum-transcript-length check catches this before it reaches the summarizer, rather than producing a summary from a hallucinated sentence |
| Network blip / rate limit / upstream 5xx from Groq | `utils/retry.py`, used by both ASR and LLM calls | Retried automatically (backoff, 3 attempts); only surfaces as `FAILED` if all retries are exhausted |
| Malformed LLM JSON response | `summarization_service._validate_result` | Rejected and reported, even though `strict: true` on `gpt-oss-120b` should already guarantee valid shape — never trust an external system blindly |
| Two identical files uploaded before either finishes processing | `find_completed_by_hash` only matches `COMPLETED` rows | Both process independently (a few duplicate provider calls); the dedupe only kicks in for *subsequent* uploads of the same file, once one has actually completed. A known, accepted trade-off — real locking would add complexity out of proportion to what this project needs |
| `GROQ_API_KEY` missing | App startup | Logged as a warning immediately (`main.py`), instead of only failing silently on the first upload |
| Unknown meeting id | `GET /api/v1/meetings/{id}` and `/status` | `404` with a message naming the id that wasn't found |

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
