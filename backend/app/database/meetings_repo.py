"""CRUD against the `meetings` table.

Every function takes an optional db_path override so tests can point
at an isolated temp database instead of the real data/app.db.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.app.database.connection import get_connection
from backend.app.models.meeting import Meeting, MeetingStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_meeting(row: sqlite3.Row) -> Meeting:
    return Meeting(
        id=row["id"],
        filename=row["filename"],
        file_hash=row["file_hash"],
        audio_path=row["audio_path"],
        status=row["status"],
        transcript=row["transcript"],
        title=row["title"],
        summary=row["summary"],
        key_decisions=json.loads(row["key_decisions"]) if row["key_decisions"] else [],
        action_items=json.loads(row["action_items"]) if row["action_items"] else [],
        open_questions=json.loads(row["open_questions"]) if row["open_questions"] else [],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        processing_started_at=row["processing_started_at"],
        processing_completed_at=row["processing_completed_at"],
    )


def create_meeting(
    filename: str,
    file_hash: str,
    audio_path: str,
    db_path: Optional[Path] = None,
) -> Meeting:
    meeting_id = uuid.uuid4().hex
    now = _now()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO meetings (id, filename, file_hash, audio_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (meeting_id, filename, file_hash, audio_path, MeetingStatus.UPLOADED, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return get_meeting(meeting_id, db_path)


def get_meeting(meeting_id: str, db_path: Optional[Path] = None) -> Optional[Meeting]:
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_meeting(row) if row else None


def list_meetings(db_path: Optional[Path] = None) -> list[Meeting]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM meetings ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()
    return [_row_to_meeting(row) for row in rows]


def find_leader_by_hash(file_hash: str, db_path: Optional[Path] = None) -> Optional[Meeting]:
    """The canonical meeting for this content: the earliest-created,
    non-FAILED meeting sharing this file_hash.

    A new upload of identical audio defers to whichever meeting this
    returns instead of reprocessing: reusing its result directly if it's
    already COMPLETED, or waiting on it if it's still QUEUED/PROCESSING
    (see processing_service.run). If this *is* the earliest meeting for
    the hash, the caller is the leader and does the real work.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM meetings WHERE file_hash = ? AND status != ? "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
            (file_hash, MeetingStatus.FAILED),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_meeting(row) if row else None


def update_status(
    meeting_id: str,
    status: str,
    *,
    error_message: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    conn = get_connection(db_path)
    try:
        fields = ["status = ?", "updated_at = ?"]
        values: list = [status, _now()]
        if status == MeetingStatus.PROCESSING:
            fields.append("processing_started_at = ?")
            values.append(_now())
        if status in (MeetingStatus.COMPLETED, MeetingStatus.FAILED):
            fields.append("processing_completed_at = ?")
            values.append(_now())
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(error_message)
        values.append(meeting_id)
        conn.execute(f"UPDATE meetings SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def fail_stuck_processing(
    message: str = "Interrupted by a server restart before processing finished.",
    db_path: Optional[Path] = None,
) -> int:
    """Mark any meeting left in PROCESSING as FAILED. Returns the count affected.

    BackgroundTasks run in-process with no broker or resumption mechanism —
    if the process restarts while one is running, that row would otherwise
    stay PROCESSING forever with nothing to ever move it forward. Called
    once at startup so a crash doesn't leave a meeting silently stuck.
    """
    conn = get_connection(db_path)
    try:
        now = _now()
        cursor = conn.execute(
            "UPDATE meetings SET status = ?, error_message = ?, updated_at = ?, processing_completed_at = ? "
            "WHERE status = ?",
            (MeetingStatus.FAILED, message, now, now, MeetingStatus.PROCESSING),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def save_transcript(meeting_id: str, transcript: str, db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE meetings SET transcript = ?, updated_at = ? WHERE id = ?",
            (transcript, _now(), meeting_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_summary(
    meeting_id: str,
    summary: str,
    key_decisions: list,
    action_items: list,
    title: Optional[str] = None,
    open_questions: Optional[list] = None,
    db_path: Optional[Path] = None,
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            UPDATE meetings
            SET title = ?, summary = ?, key_decisions = ?, action_items = ?, open_questions = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                summary,
                json.dumps(key_decisions),
                json.dumps(action_items),
                json.dumps(open_questions or []),
                _now(),
                meeting_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
