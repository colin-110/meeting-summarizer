# Meeting Summarizer

Upload or record a meeting and get a timestamped transcript, a summary, decisions, action items and open questions — then search across all your meetings or ask questions about one.

Runs on free tiers only: Groq (Whisper + gpt-oss) for AI, SQLite for storage.

## Features

- **Live recording** — transcribed and summarized while you talk; notes are ready seconds after you stop
- **Upload** — MP3, WAV, M4A, MP4, WebM, OGG, FLAC up to 200 MB
- **Timestamped transcript** and structured notes: summary, decisions, action items (owner + deadline), open questions
- **Long meetings** — summarized section by section, with timestamped chapters
- **Ask this meeting, or all of them** — answers from the relevant transcript passages, with clickable sources
- **Full-text search** across every meeting, with highlighted snippets
- **Export** to Markdown, copy summary, delete a meeting
- **Private by default** — each browser gets a random token; meetings are only visible to the token that created them (only its hash is stored)
- **Live progress** — preparing audio → transcribing → summarizing
- **Usage per meeting** — audio skipped, AI tokens spent, sections done live

## How it works

~~~text
                 ┌──────────── SQLite (WAL) ─────────────┐
Browser ─► web (N procs) ─►│ jobs · results · FTS5 · budgets · cache │◄─ worker (N procs)
                           └─────────────────────────────────────────┘        │
                                                                               ├─ ffmpeg: 16 kHz mono, pauses found in the same pass
                                                                               ├─ long silences cut out, timestamps mapped back
                                                                               ├─ Whisper: chunks in parallel
                                                                               └─ gpt-oss: sections → merged notes (two model budgets)
~~~

## Engineering decisions

**Scaling**
- **Durable queue with leases.** Jobs live in SQLite. Workers lease one with `BEGIN IMMEDIATE`; a heartbeat renews the lease while it runs. If a worker dies, the lease expires and another worker takes over — the visibility-timeout pattern SQS uses. Tested with real concurrent OS processes: every job runs exactly once.
- **Web and workers scale separately.** Run the API with `RUN_WORKERS=false` and start any number of `python -m backend.app.worker` processes. Rate limits and provider budgets live in the database, so they hold across every process.
- **Fair scheduling.** A finished live recording is picked first (someone is waiting on it), then the user with the fewest jobs running, then the oldest. One user's batch of uploads can't take every worker, and each user can have at most `MAX_QUEUED_PER_OWNER` meetings waiting.
- **Paced to the provider, not failed by it.** Groq's free tier allows 8,000 tokens/min per model. Every call reserves budget from a shared limiter, then settles to the real usage. When the provider is still out of capacity, the job is re-queued with a delay instead of failing.
- **Two models, two budgets.** Each Groq model has its own per-minute limit. When the main model's budget is full, a call moves to the other model instead of waiting. Measured with two uploads at once on a tight budget: both done in 2.6 s, the second on the fallback model, instead of the second waiting about a minute.
- **Backpressure.** Past `MAX_QUEUE_DEPTH` queued jobs, uploads get `503` + `Retry-After`. `/metrics` exposes queue depth, latency, token usage, audio skipped and cache hits in Prometheus format.
- **Conditional GETs.** The status and meeting endpoints the UI polls send an `ETag`; an unchanged meeting is answered `304` with an empty body, checked from four small columns without loading the transcript. Browsers do this on their own, so the frontend has no code for it.

**Pipeline**
- **Checkpoints.** Each stage saves its output; a retry never re-transcribes audio or re-summarizes a finished section.
- **Live transcription and live summaries.** The browser uploads a self-contained piece every 45 s; each is transcribed on arrival. Sections of a long recording that can no longer change are summarized while the meeting is still going (under a cross-process lock, with a short wait for AI capacity). Stopping leaves only the last section and the merge. Measured on a tight token budget: notes 14.8 s after Stop instead of 65.7 s.
- **Skip silence before transcription.** Pauses longer than 2 s are shortened to 0.6 s before audio is sent; Groq meters Whisper by audio length, and silence is where Whisper hallucinates. A timeline of every cut maps transcript timestamps back to the original recording, and the trimmed file's length is checked against it — any mismatch falls back to the untrimmed audio. Measured on a 91 s recording with long pauses: 61 s sent (33% less), and every line start within about 1 s of the truth (without skipping, the worst was off by 6.4 s).
- **One decode pass.** 16 kHz mono shrinks audio ~10x, and pauses are detected in the same ffmpeg pass. Chunk boundaries snap to pauses so words aren't split. ffmpeg is the pinned static build from `imageio-ffmpeg`, so behaviour is the same on every machine (Ubuntu's ffmpeg 8.0.1 silently ignores the `aselect` filter).
- **Long meetings within the token budget.** Transcripts are split into sections at segment boundaries, summarized individually, then merged — hierarchically if needed. Section titles become chapters.
- **Retrieval for Q&A.** Questions search transcript segments (FTS5/BM25), add neighbouring lines, and send only those plus the meeting notes. Requests stay bounded however long the meetings are.
- **Answer cache.** Answers are keyed by the question (ignoring case and punctuation) plus the exact excerpts sent. Asking again about unchanged meetings is free and instant (0.01 s vs 0.72 s); any change to the meetings changes the excerpts, so a stale answer is never served. Cached answers are deleted with their meeting.
- **Usage metering.** Every job records the audio it sent and the tokens it spent, per model, and the UI shows it.
- **Hallucination filter.** Whisper invents phrases over silence; segments with high no-speech probability and low confidence are dropped.

**Why no Kafka/Redis/Celery/Postgres:** one database already gives a durable queue, locking, rate limits, caching and search at this scale. Every extra service costs memory on a small free-tier VM and adds a failure mode. Postgres becomes worth it when the app needs more than one machine; the queue design (leases + `SKIP LOCKED`-style claiming) carries over directly.

**Privacy and cost:** meetings are private to a browser token (only its hash is stored); audio is deleted after processing; meetings and cached answers auto-delete after 30 days. `X-Forwarded-For` is ignored unless `TRUST_PROXY_HEADERS=true`, so clients can't dodge the per-IP limits.

## Run locally

~~~bash
git clone https://github.com/colin-110/meeting-summarizer.git
cd meeting-summarizer

python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env     # set GROQ_API_KEY (free at console.groq.com)
uvicorn backend.app.main:app --reload
~~~

ffmpeg comes with the `imageio-ffmpeg` package; nothing else to install.

~~~bash
pytest -v
~~~

To run workers as their own processes: start the API with `RUN_WORKERS=false`, then run `python -m backend.app.worker` once per worker process.

## Deployment

The API runs on one AWS Lightsail instance (USD 5/month: 512 MB RAM, 2 vCPU, 20 GB), the frontend on Vercel.

- `deploy/lightsail.yaml` — CloudFormation: the instance, a static IP, and a firewall that only opens 80/443 (SSH only from one IP).
- `deploy/setup.sh` — installs or updates the app on Ubuntu: Python venv, systemd service, Caddy for automatic HTTPS on an `sslip.io` name (no domain needed), 1 GB swap. Re-run it to deploy a new version.
- The Groq key lives in `/etc/meeting-summarizer/secrets.env` on the server, never in the repo.
- `frontend/config.js` points the Vercel copy at the API; `ALLOWED_ORIGINS` on the server allows only that origin.

Sized from measurements, not guesses: running locally with the server's settings, an 11-minute and a 48-minute recording peaked at 113 MB RAM and used 31 CPU-seconds in total; on the live server the app peaks around 95 MB. The bottleneck is Groq's free token limit, not the machine.

~~~bash
aws cloudformation deploy --region ap-south-1 --stack-name meeting-summarizer \
  --template-file deploy/lightsail.yaml \
  --parameter-overrides KeyPairName=<lightsail-key> SshCidr=<your-ip>/32
scp deploy/setup.sh ubuntu@<ip>:/tmp/ && ssh ubuntu@<ip> 'sudo bash /tmp/setup.sh <ip-with-dashes>.sslip.io https://<app>.vercel.app'
~~~

## Build next

- **Push status instead of polling.** Server-Sent Events from `/status`, so the UI updates the moment a stage changes.
- **Postgres.** Port the queue (`claim_next` → `SELECT … FOR UPDATE SKIP LOCKED`) and FTS5 search (→ `tsvector`) to run on more than one machine.
- **Calendar export.** Turn action items with deadlines into an `.ics` file.
- **Smarter cache.** Match questions by meaning (embeddings) instead of by exact words.

## Limits to know about

- **The free AI tier is the real ceiling, not the app.** Measured: a short meeting's summary uses ~1,700–2,300 tokens, so each model's 8,000 tokens/min allows about 4 short summaries a minute, roughly 8 with the fallback model. An hour-long meeting takes several minutes of budget (spread across the meeting when it's recorded live). The limiter queues work instead of failing.
- **One machine.** SQLite scales to many processes on one VM, not across machines. Moving past one VM means Postgres.
- **No accounts.** Privacy uses an anonymous browser token; clearing browser storage loses access to your meetings.
- **No speaker labels.** Groq's Whisper doesn't identify speakers.
- **Live pieces are cut on a timer**, not at pauses, so a word can occasionally be split at a 45-second boundary. Uploaded files are cut at pauses.

## Tech stack

Python 3.12 · FastAPI · SQLite (WAL, FTS5) · ffmpeg · Groq Whisper and gpt-oss · plain HTML/CSS/JS · Pytest · GitHub Actions · Prometheus metrics · AWS Lightsail + CloudFormation · Caddy · Vercel
