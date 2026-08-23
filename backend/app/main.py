import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.health import router as health_router
from backend.app.api.meetings import router as meetings_router
from backend.app.core.logging import configure_logging, get_logger
from backend.app.database.init_db import init_db

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not os.environ.get("GROQ_API_KEY"):
        log.warning(
            "GROQ_API_KEY is not set — uploads will be accepted but every "
            "meeting will fail during transcription. Add it to .env."
        )
    yield


app = FastAPI(title="Meeting Summarizer", lifespan=lifespan)
app.include_router(health_router)
app.include_router(meetings_router)
