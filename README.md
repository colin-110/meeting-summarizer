# Meeting Summarizer

Upload or record a meeting and get a timestamped transcript, a summary, decisions, action items and open questions — then search across all your meetings or ask questions about one.

Runs on free tiers only: Groq (Whisper + gpt-oss) for AI, SQLite for storage, one small VM.

## Features

- **Live recording** — transcribed while you talk; notes are ready ~2 seconds after you stop
- **Upload** — MP3, WAV, M4A, MP4, WebM, OGG, FLAC up to 200 MB
- **Timestamped transcript** and structured notes: summary, decisions, action items (owner + deadline), open questions
- **Long meetings** — summarized section by section, with timestamped chapters
- **Ask this meeting, or all of them** — answers from the relevant transcript passages, with clickable sources
- **Full-text search** across every meeting, with highlighted snippets
- **Export** to Markdown, copy summary, delete a meeting
- **Private by default** — each browser gets a random token; meetings are only visible to the token that created them (only its hash is stored)
- **Live progress** — preparing audio → transcribing → summarizing

## How it works

~~~text
                 ┌──────────── SQLite (WAL) ─────────────┐
Browser ─► Caddy ─► web (N procs) ─►│ jobs · results · FTS5 index · budgets │◄─ worker (N procs)
          HTTPS                     └───────────────────────────────────────┘        │
                                                                                     ├─ ffmpeg: 16 kHz mono, cut at pauses
                                                                                     ├─ Whisper: chunks in parallel
                                                                                     └─ gpt-oss: sections → merged notes
~~~

## Engineering decisions

**Scaling**
- **Durable queue with leases.** Jobs live in SQLite. Workers lease one with `BEGIN IMMEDIATE`; a heartbeat renews the lease while it runs. If a worker dies, the lease expires and another worker takes over — the visibility-timeout pattern SQS uses. Tested with real concurrent OS processes: every job runs exactly once.
- **Web and workers scale separately.** `docker compose up -d --scale worker=3`. Rate limits and provider budgets live in the database, so they hold across every process.
- **Paced to the provider, not failed by it.** Groq's free tier allows 8,000 tokens/min per model. Every call reserves budget from a shared limiter, then settles to the real usage. When the provider is still out of capacity, the job is re-queued with a delay instead of failing.
- **Backpressure.** Past `MAX_QUEUE_DEPTH` queued jobs, uploads get `503` + `Retry-After`. `/metrics` exposes queue depth, latency and token usage in Prometheus format.

**Pipeline**
- **Checkpoints.** Each stage saves its output; a retry never re-transcribes audio or re-summarizes a finished section.
- **Live transcription.** The browser uploads a self-contained piece every 45 s; each is transcribed on arrival, so stopping only leaves the summary.
- **Compress, then cut at pauses.** 16 kHz mono shrinks audio ~10x. Chunk boundaries snap to silences (`silencedetect`) so words aren't split.
- **Long meetings within the token budget.** Transcripts are split into sections at segment boundaries, summarized individually, then merged — hierarchically if needed. Section titles become chapters.
- **Retrieval for Q&A.** Questions search transcript segments (FTS5/BM25), add neighbouring lines, and send only those plus the meeting notes. Requests stay bounded however long the meetings are.
- **Hallucination filter.** Whisper invents phrases over silence; segments with high no-speech probability and low confidence are dropped.

**Why no Kafka/Redis/Celery/Postgres:** one database already gives a durable queue, locking, rate limits and search at this scale. Every extra service costs memory on a 1 GB free-tier VM and adds a failure mode. Postgres becomes worth it when the app needs more than one machine; the queue design (leases + `SKIP LOCKED`-style claiming) carries over directly.

**Privacy and cost:** meetings are private to a browser token (only its hash is stored); audio is deleted after processing; meetings auto-delete after 30 days.

## Run locally

~~~bash
git clone https://github.com/colin-110/meeting-summarizer.git
cd meeting-summarizer

python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env     # set GROQ_API_KEY (free at console.groq.com)
uvicorn backend.app.main:app --reload
~~~

ffmpeg is bundled with the `imageio-ffmpeg` package; a system ffmpeg is used if present.

~~~bash
pytest -v
~~~

## Deploy (free)

**Recommended: one AWS free-tier EC2 instance** (persistent disk, so meetings survive restarts).

1. Launch an Ubuntu `t3.micro`/`t2.micro` instance (whichever your account lists as free-tier eligible); allow ports 22, 80 and 443 in its security group.
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Point a hostname at the instance. No domain? Use `<your-ip>.sslip.io` — it resolves to your IP and Caddy can get a certificate for it.
4. On the instance:
   ~~~bash
   git clone https://github.com/colin-110/meeting-summarizer.git && cd meeting-summarizer
   printf "GROQ_API_KEY=gsk_...\nDOMAIN=1.2.3.4.sslip.io\n" > .env
   docker compose up -d --build
   ~~~

Caddy provisions and renews HTTPS automatically. Web and workers run as separate containers; add workers with `docker compose up -d --scale worker=3`. Update with `git pull && docker compose up -d --build`. Back up the `app-data` volume if the meetings matter to you.

**Quick demo: Render free tier** via `render.yaml`. Its disk is ephemeral, so meetings are lost on each restart or sleep.

## Limits to know about

- **The free AI tier is the real ceiling, not the app.** Measured: a short meeting's summary uses ~1,700 tokens, so the 8,000 tokens/min limit allows roughly 4 short summaries a minute; an hour-long meeting takes several minutes of budget. The limiter queues work instead of failing. More capacity means a paid or second provider — models are set by config.
- **One machine.** SQLite scales to many processes on one VM, not across machines. Moving past one VM means Postgres.
- **No accounts.** Privacy uses an anonymous browser token; clearing browser storage loses access to your meetings.
- **No speaker labels.** Groq's Whisper doesn't identify speakers.
- **Live pieces are cut on a timer**, not at pauses, so a word can occasionally be split at a 45-second boundary. Uploaded files are cut at pauses.

## Tech stack

Python 3.12 · FastAPI · SQLite (WAL, FTS5) · ffmpeg · Groq Whisper and gpt-oss · plain HTML/CSS/JS · Pytest · GitHub Actions · Docker Compose + Caddy · Prometheus metrics

## License

See [LICENSE](LICENSE).
