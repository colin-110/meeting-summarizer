from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.health import router as health_router
from backend.app.api.meetings import router as meetings_router
from backend.app.core.logging import configure_logging
from backend.app.database.init_db import init_db

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Meeting Summarizer", lifespan=lifespan)
app.include_router(health_router)
app.include_router(meetings_router)
