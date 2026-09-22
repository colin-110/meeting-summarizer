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
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))

# Comma-separated list of origins allowed to call the API cross-origin, e.g.
# when the frontend is deployed separately (Vercel) from this backend
# (Render). Empty by default: the frontend is served from this same process
# via StaticFiles unless ALLOWED_ORIGINS says otherwise, so cross-origin
# requests have no reason to be allowed until they do.
ALLOWED_ORIGINS = [origin.strip() for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",") if origin.strip()]

DATABASE_PATH = DATA_DIR / "app.db"
AUDIO_DIR = DATA_DIR / "audio"
