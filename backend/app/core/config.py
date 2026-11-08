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
# Keep the uploaded audio after processing? Off by default: less storage, better privacy.
RETAIN_AUDIO = os.environ.get("RETAIN_AUDIO", "false").lower() == "true"
# Meetings older than this are deleted automatically. 0 disables.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

# Comma-separated list of origins allowed to call the API cross-origin, e.g.
# when the frontend is deployed separately (Vercel) from this backend
# (Render). Empty by default: the frontend is served from this same process
# via StaticFiles unless ALLOWED_ORIGINS says otherwise, so cross-origin
# requests have no reason to be allowed until they do.
ALLOWED_ORIGINS = [origin.strip() for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",") if origin.strip()]

DATABASE_PATH = DATA_DIR / "app.db"
AUDIO_DIR = DATA_DIR / "audio"
