"""Response shapes for the meetings API — kept separate from the Meeting
dataclass so the DB representation (JSON-as-TEXT columns) and the wire
representation (nested objects) don't have to be the same shape.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ActionItem(BaseModel):
    task: str
    assignee: Optional[str] = None
    deadline: Optional[str] = None


class Segment(BaseModel):
    start: float
    end: float
    text: str


class MeetingSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    title: Optional[str] = None
    status: str
    stage: Optional[str] = None
    created_at: str
    updated_at: str
    snippet: Optional[str] = None


class MeetingDetailOut(MeetingSummaryOut):
    transcript: Optional[str] = None
    summary: Optional[str] = None
    key_decisions: list[str] = []
    action_items: list[ActionItem] = []
    open_questions: list[str] = []
    segments: list[Segment] = []
    audio_seconds: Optional[float] = None
    error_message: Optional[str] = None
    processing_started_at: Optional[str] = None
    processing_completed_at: Optional[str] = None


class MeetingStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    stage: Optional[str] = None
    error_message: Optional[str] = None


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class AskOut(BaseModel):
    answer: str
