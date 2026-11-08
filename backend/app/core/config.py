"""Environment-driven settings.

No python-dotenv dependency — this is a small enough .env reader to
write by hand, and it keeps requirements.txt to packages that are
actually doing real work.
"""

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(_ENV_FILE)

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
# Uploads are re-encoded to 16 kHz mono before transcription (~14 MB/hour), so the
# upload limit no longer has to match Groq's 25 MB per-request audio cap.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))

# Worker threads pulling jobs from the SQLite-backed queue. Keep low on Groq's free tier.
WORKER_CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "2"))
# Seconds of audio per transcription request, and how many requests run in parallel.
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", "600"))
TRANSCRIBE_PARALLELISM = int(os.environ.get("TRANSCRIBE_PARALLELISM", "3"))
# A job interrupted this many times (restarts/crashes) is failed instead of retried forever.
MAX_JOB_ATTEMPTS = int(os.environ.get("MAX_JOB_ATTEMPTS", "3"))
# Run the queue workers inside the web process (simple single-container deploy).
# Set false when running workers separately with `python -m backend.app.worker`.
RUN_WORKERS = os.environ.get("RUN_WORKERS", "true").lower() == "true"
# A claimed job is leased for this long and renewed by a heartbeat while it runs;
# if its worker dies, the lease expires and another worker picks the job up.
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "120"))
HEARTBEAT_SECONDS = int(os.environ.get("HEARTBEAT_SECONDS", "30"))
# Backpressure: refuse new work (503) once this many jobs are waiting.
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "50"))
# A job that keeps hitting provider quota limits is given up on after this long.
MAX_JOB_AGE_HOURS = int(os.environ.get("MAX_JOB_AGE_HOURS", "24"))
# How long a job waits before retrying after the provider says it's out of capacity.
DEFER_SECONDS = int(os.environ.get("DEFER_SECONDS", "90"))

# --- Provider budgets (Groq free tier: 8,000 tokens/min per LLM model) ---
# Shared across every process via the database, so N workers together stay
# under the limit instead of each one assuming it has the whole quota.
LLM_TOKENS_PER_MINUTE = int(os.environ.get("LLM_TOKENS_PER_MINUTE", "7000"))
WHISPER_REQUESTS_PER_MINUTE = int(os.environ.get("WHISPER_REQUESTS_PER_MINUTE", "18"))
PROVIDER_MAX_WAIT_SECONDS = int(os.environ.get("PROVIDER_MAX_WAIT_SECONDS", "240"))

# --- Summarization sizing ---
# Transcripts up to this many characters are summarized in one call; longer
# ones are summarized section by section, then merged (each call must fit the
# per-minute token budget above).
SINGLE_PASS_CHARS = int(os.environ.get("SINGLE_PASS_CHARS", "10000"))
SECTION_CHARS = int(os.environ.get("SECTION_CHARS", "10000"))
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "openai/gpt-oss-120b")
SECTION_MODEL = os.environ.get("SECTION_MODEL", "openai/gpt-oss-20b")
QA_MODEL = os.environ.get("QA_MODEL", "openai/gpt-oss-20b")
# Characters of retrieved transcript sent with a question.
QA_CONTEXT_CHARS = int(os.environ.get("QA_CONTEXT_CHARS", "12000"))
# Summaries normally use SUMMARY_MODEL. Each model has its own per-minute
# budget, so when that one is saturated for longer than FALLBACK_AFTER_SECONDS
# the call moves to FALLBACK_MODEL instead of waiting (empty disables).
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "openai/gpt-oss-20b")
FALLBACK_AFTER_SECONDS = float(os.environ.get("FALLBACK_AFTER_SECONDS", "8"))

# Pauses longer than SKIP_SILENCE_SECONDS are shortened to KEEP_SILENCE_SECONDS
# before audio is sent for transcription (0 disables). Timestamps are mapped
# back to the original recording.
SKIP_SILENCE_SECONDS = float(os.environ.get("SKIP_SILENCE_SECONDS", "2.0"))
KEEP_SILENCE_SECONDS = float(os.environ.get("KEEP_SILENCE_SECONDS", "0.6"))

# Fairness: how many meetings one user may have waiting at once.
MAX_QUEUED_PER_OWNER = int(os.environ.get("MAX_QUEUED_PER_OWNER", "5"))
# Summarize finished sections of a live recording while it is still going,
# so stopping a long meeting leaves only the final merge.
LIVE_PRESUMMARIZE = os.environ.get("LIVE_PRESUMMARIZE", "true").lower() == "true"

# Keep the uploaded audio after processing? Off by default: less storage, better privacy.
RETAIN_AUDIO = os.environ.get("RETAIN_AUDIO", "false").lower() == "true"
# Meetings older than this are deleted automatically. 0 disables.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

# Comma-separated origins allowed to call the API cross-origin. Empty by
# default: the frontend is served by this same process, so it's same-origin.
ALLOWED_ORIGINS = [origin.strip() for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",") if origin.strip()]

# Trust X-Forwarded-For for the client IP (used by rate limits). Only turn on
# behind a reverse proxy that sets it; otherwise clients could spoof it.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "false").lower() == "true"

DATABASE_PATH = DATA_DIR / "app.db"
AUDIO_DIR = DATA_DIR / "audio"
