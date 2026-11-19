"""Response shapes for the meetings API — kept separate from the Meeting
dataclass so the DB representation (JSON-as-TEXT columns) and the wire
representation (nested objects) don't have to be the same shape.
"""

from pydantic import BaseModel, ConfigDict, Field


class ActionItem(BaseModel):
    task: str
    assignee: str | None = None
    deadline: str | None = None


class Segment(BaseModel):
    start: float
    end: float
    text: str


class Chapter(BaseModel):
    title: str
    start: float


class MeetingSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    title: str | None = None
    status: str
    stage: str | None = None
    created_at: str
    updated_at: str
    snippet: str | None = None


class MeetingDetailOut(MeetingSummaryOut):
    transcript: str | None = None
    summary: str | None = None
    key_decisions: list[str] = []
    action_items: list[ActionItem] = []
    open_questions: list[str] = []
    segments: list[Segment] = []
    chapters: list[Chapter] = []
    audio_seconds: float | None = None
    source: str = "upload"
    usage: dict = {}
    error_message: str | None = None
    processing_started_at: str | None = None
    processing_completed_at: str | None = None


class MeetingStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    stage: str | None = None
    error_message: str | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class Source(BaseModel):
    tag: str
    meeting_id: str
    title: str | None = None
    start: float


class AskOut(BaseModel):
    answer: str
    sources: list[Source] = []
    cached: bool = False


class ChunkOut(BaseModel):
    seq: int
    transcribed: bool
