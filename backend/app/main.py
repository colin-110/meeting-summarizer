import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api.health import router as health_router
from backend.app.api.meetings import router as meetings_router
from backend.app.core import config
from backend.app.core.logging import configure_logging, get_logger
from backend.app.database.init_db import init_db
from backend.app.database.meetings_repo import fail_stuck_processing

configure_logging()
log = get_logger(__name__)

_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    recovered = fail_stuck_processing()
    if recovered:
        log.warning("marked %d meeting(s) stuck in PROCESSING as FAILED after restart", recovered)
    if not os.environ.get("GROQ_API_KEY"):
        log.warning(
            "GROQ_API_KEY is not set — uploads will be accepted but every "
            "meeting will fail during transcription. Add it to .env."
        )
    yield


app = FastAPI(title="Meeting Summarizer", lifespan=lifespan)

if config.ALLOWED_ORIGINS:
    # Only needed when the frontend is deployed separately from this API
    # (e.g. Vercel + Render) — same-origin requests never hit CORS at all.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.ALLOWED_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

app.include_router(health_router)
app.include_router(meetings_router)

# Mounted last so it never shadows the API routes above — StaticFiles only
# handles a path once nothing more specific has already matched it.
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
