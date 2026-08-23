"""The Meeting shape. One entity, no relations — a dataclass is enough."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional


class MeetingStatus(StrEnum):
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class Meeting:
    id: str
    filename: str
    file_hash: str
    audio_path: str
    status: str
    transcript: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    key_decisions: list = field(default_factory=list)
    action_items: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    error_message: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    processing_started_at: Optional[str] = None
    processing_completed_at: Optional[str] = None
