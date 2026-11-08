# Meeting Summarizer

Upload or record a meeting and get a timestamped transcript, a summary, decisions, action items and open questions — then search across all your meetings or ask questions about one.

Runs on free tiers only: Groq (Whisper + gpt-oss) for AI, SQLite for storage, one small VM.

## Features

- **Upload or record in the browser** — MP3, WAV, M4A, MP4, WebM, OGG, FLAC up to 200 MB
- **Timestamped transcript** and structured notes: summary, decisions, action items (owner + deadline), open questions
- **Ask this meeting** — questions answered from the transcript, with `[mm:ss]` citations
- **Full-text search** across every meeting, with highlighted snippets
- **Export** to Markdown, copy summary, delete a meeting
- **Private by default** — each browser gets a random token; meetings are only visible to the token that created them (only its hash is stored)
- **Live progress** — preparing audio → transcribing → summarizing

## How it works

~~~text
Browser ──► FastAPI ──► SQLite (jobs, results, search index)
                            ▲
            worker threads ─┘ claim jobs atomically
                 │
                 ├─ ffmpeg: 16 kHz mono, split into 10-min chunks
                 ├─ Groq Whisper: chunks transcribed in parallel
                 └─ Groq gpt-oss-120b: structured summary (strict JSON schema)
~~~

## Engineering decisions

- **Durable queue in SQLite.** Uploads insert a `QUEUED` row; workers claim the oldest with `BEGIN IMMEDIATE`, so no job runs twice. Jobs interrupted by a restart are re-queued (and abandoned after 3 attempts), not lost.
- **Compress before transcribing.** Re-encoding to 16 kHz mono (what speech models use anyway) shrinks audio ~10x, so hour-long meetings fit the provider's 25 MB limit and upload faster. Chunks are transcribed in parallel and stitched with corrected timestamps.
- **Hallucination filter.** Whisper invents phrases ("Thank you.") over silence; segments with high no-speech probability and low confidence are dropped.
- **Right-sized models.** Structured summaries use gpt-oss-120b; Q&A uses the faster gpt-oss-20b.
- **Content-hash deduplication.** Identical recordings are processed once.
- **Privacy and cost.** Audio is deleted after processing; meetings auto-delete after 30 days; uploads are rate-limited.
- **SQLite tuned for concurrency.** WAL mode and a busy timeout let the API read while workers write. Search uses FTS5, lists never load transcripts, and responses are gzipped.
- **Layered upload validation.** Extension, size, and file-signature checks.

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

Caddy provisions and renews HTTPS automatically. Update with `git pull && docker compose up -d --build`. Back up the `app-data` volume if the meetings matter to you.

**Quick demo: Render free tier** via `render.yaml`. Its disk is ephemeral, so meetings are lost on each restart or sleep.

## Limits to know about

- Single-process by design: the queue workers and rate limiter live in-process. Scale up the VM before scaling out; going multi-instance means moving the rate limiter out of memory.
- Privacy uses an anonymous browser token, not accounts. Clearing browser storage loses access to your meetings (they expire after 30 days).
- Speaker labels aren't included: Groq's Whisper doesn't diarize.
- Groq's free tier has rate limits; the workers retry transient errors and fail jobs with a readable message if the limit persists.

## Tech stack

Python 3.12 · FastAPI · SQLite (WAL + FTS5) · ffmpeg · Groq Whisper and gpt-oss · plain HTML/CSS/JS · Pytest · GitHub Actions · Docker + Caddy

## License

See [LICENSE](LICENSE).
