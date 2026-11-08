import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api.health import router as health_router
from backend.app.api.meetings import router as meetings_router
from backend.app.api.meetings import ask_router, recordings_router
from backend.app.core import config
from backend.app.core.logging import configure_logging, get_logger
from backend.app.database.init_db import init_db
from backend.app.services.worker import WorkerPool

configure_logging()
log = get_logger(__name__)

_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Interrupted jobs need no startup sweep: their leases expire and any
    # worker (here or in a separate worker process) picks them back up.
    app.state.workers = WorkerPool(config.WORKER_CONCURRENCY if config.RUN_WORKERS else 0)
    app.state.workers.start()
    if not os.environ.get("GROQ_API_KEY"):
        log.warning(
            "GROQ_API_KEY is not set — uploads will be accepted but every "
            "meeting will fail during transcription. Add it to .env."
        )
    yield
    app.state.workers.stop()


app = FastAPI(title="Meeting Summarizer", lifespan=lifespan)

# JSON (transcripts/segments) and static assets compress ~5-10x.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def request_log(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path != "/health":
        log.info(
            "%s %s -> %d (%.0f ms) rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
    return response


if config.ALLOWED_ORIGINS:
    # Only needed when the frontend is deployed separately from this API
    # (e.g. Vercel + Render) — same-origin requests never hit CORS at all.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.ALLOWED_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

app.include_router(health_router)
app.include_router(meetings_router)
app.include_router(recordings_router)
app.include_router(ask_router)

# Mounted last so it never shadows the API routes above — StaticFiles only
# handles a path once nothing more specific has already matched it.
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
