"""Response shapes for the meetings API — kept separate from the Meeting
dataclass so the DB representation (JSON-as-TEXT columns) and the wire
representation (nested objects) don't have to be the same shape.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict


class ActionItem(BaseModel):
    task: str
    assignee: Optional[str] = None
    deadline: Optional[str] = None


class MeetingSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    status: str
    created_at: str
    updated_at: str


class MeetingDetailOut(MeetingSummaryOut):
    transcript: Optional[str] = None
    summary: Optional[str] = None
    key_decisions: list[str] = []
    action_items: list[ActionItem] = []
    error_message: Optional[str] = None
    processing_started_at: Optional[str] = None
    processing_completed_at: Optional[str] = None


class MeetingStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    error_message: Optional[str] = None
