"""The Meeting shape. One entity, no relations — a dataclass is enough."""

from dataclasses import dataclass, field
from enum import StrEnum


class MeetingStatus(StrEnum):
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RECORDING = "RECORDING"


@dataclass
class Meeting:
    id: str
    filename: str
    file_hash: str
    audio_path: str
    status: str
    transcript: str | None = None
    title: str | None = None
    summary: str | None = None
    key_decisions: list = field(default_factory=list)
    action_items: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    error_message: str | None = None
    created_at: str = ""
    updated_at: str = ""
    processing_started_at: str | None = None
    processing_completed_at: str | None = None
    owner_id: str = ""
    stage: str | None = None
    segments: list = field(default_factory=list)
    attempts: int = 0
    audio_seconds: float | None = None
    chapters: list = field(default_factory=list)
    notes_cache: dict = field(default_factory=dict)
    source: str = "upload"
    usage: dict = field(default_factory=dict)
