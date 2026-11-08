"""CRUD against the `meetings` table.

Every function takes an optional db_path override so tests can point
at an isolated temp database instead of the real data/app.db.
"""

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
        owner_id=row["owner_id"],
        stage=row["stage"],
        segments=json.loads(row["segments"]) if row["segments"] else [],
        attempts=row["attempts"],
        audio_seconds=row["audio_seconds"],
    )


def create_meeting(
    filename: str,
    file_hash: str,
    audio_path: str,
    db_path: Optional[Path] = None,
    *,
    owner_id: str = "",
) -> Meeting:
    meeting_id = uuid.uuid4().hex
    now = _now()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO meetings (id, filename, file_hash, audio_path, status, created_at, updated_at, owner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (meeting_id, filename, file_hash, audio_path, MeetingStatus.UPLOADED, now, now, owner_id),
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


_LIST_COLUMNS = "id, filename, title, status, stage, created_at, updated_at"


def _fts_query(text: str) -> Optional[str]:
    """Turn free user text into a safe FTS5 query: every word quoted (so
    operators/punctuation can't break the syntax) with prefix matching."""
    words = re.findall(r"\w+", text)
    return " ".join(f'"{w}"*' for w in words[:10]) or None


def list_meetings(
    owner_id: Optional[str] = None,
    *,
    limit: int = 50,
    offset: int = 0,
    query: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Lean listing: never loads transcripts. owner_id=None lists everything
    (internal/test use); the API always passes the caller's owner."""
    conn = get_connection(db_path)
    try:
        if query:
            match = _fts_query(query)
            if match is None:
                return []
            sql = (
                "SELECT m.id, m.filename, m.title, m.status, m.stage, m.created_at, m.updated_at, "
                "snippet(meetings_fts, -1, '[[', ']]', '…', 12) AS snippet "
                "FROM meetings_fts JOIN meetings m ON m.id = meetings_fts.meeting_id "
                "WHERE meetings_fts MATCH ? "
            )
            params: list = [match]
            if owner_id is not None:
                sql += "AND m.owner_id = ? "
                params.append(owner_id)
            sql += "ORDER BY rank LIMIT ? OFFSET ?"
        else:
            sql = f"SELECT {_LIST_COLUMNS}, NULL AS snippet FROM meetings "
            params = []
            if owner_id is not None:
                sql += "WHERE owner_id = ? "
                params.append(owner_id)
            sql += "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


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
        _index_for_search(conn, meeting_id)
        conn.commit()
    finally:
        conn.close()


def _index_for_search(conn: sqlite3.Connection, meeting_id: str) -> None:
    conn.execute("DELETE FROM meetings_fts WHERE meeting_id = ?", (meeting_id,))
    conn.execute(
        "INSERT INTO meetings_fts (meeting_id, owner_id, title, summary, transcript) "
        "SELECT id, owner_id, title, summary, transcript FROM meetings WHERE id = ?",
        (meeting_id,),
    )


def claim_next(db_path: Optional[Path] = None) -> Optional[str]:
    """Atomically take the oldest QUEUED meeting and mark it PROCESSING.

    BEGIN IMMEDIATE takes the write lock up front, so two worker threads (or
    processes) can never claim the same row. Returns the meeting id or None.
    """
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM meetings WHERE status = ? ORDER BY created_at ASC, id ASC LIMIT 1",
            (MeetingStatus.QUEUED,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        now = _now()
        conn.execute(
            "UPDATE meetings SET status = ?, stage = ?, attempts = attempts + 1, "
            "processing_started_at = ?, updated_at = ? WHERE id = ?",
            (MeetingStatus.PROCESSING, "starting", now, now, row["id"]),
        )
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def requeue_stuck_processing(
    max_attempts: int, db_path: Optional[Path] = None
) -> tuple[int, int]:
    """Recover jobs interrupted by a restart: put them back on the queue, or
    fail them once they've been interrupted max_attempts times (so one
    poisonous file can't crash-loop the worker forever). Returns (requeued, failed)."""
    conn = get_connection(db_path)
    try:
        now = _now()
        failed = conn.execute(
            "UPDATE meetings SET status = ?, error_message = ?, updated_at = ?, processing_completed_at = ? "
            "WHERE status = ? AND attempts >= ?",
            (
                MeetingStatus.FAILED,
                "Processing was interrupted repeatedly and has been abandoned.",
                now,
                now,
                MeetingStatus.PROCESSING,
                max_attempts,
            ),
        ).rowcount
        requeued = conn.execute(
            "UPDATE meetings SET status = ?, stage = NULL, updated_at = ? WHERE status = ?",
            (MeetingStatus.QUEUED, now, MeetingStatus.PROCESSING),
        ).rowcount
        conn.commit()
        return requeued, failed
    finally:
        conn.close()


def set_stage(meeting_id: str, stage: str, db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE meetings SET stage = ?, updated_at = ? WHERE id = ?", (stage, _now(), meeting_id)
        )
        conn.commit()
    finally:
        conn.close()


def save_segments(
    meeting_id: str, segments: list, audio_seconds: Optional[float], db_path: Optional[Path] = None
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE meetings SET segments = ?, audio_seconds = ?, updated_at = ? WHERE id = ?",
            (json.dumps(segments), audio_seconds, _now(), meeting_id),
        )
        conn.commit()
    finally:
        conn.close()


def audio_in_use(file_hash: str, exclude_id: str, db_path: Optional[Path] = None) -> bool:
    """True if another meeting with this audio is still waiting to be processed."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM meetings WHERE file_hash = ? AND id != ? AND status IN (?, ?, ?) LIMIT 1",
            (file_hash, exclude_id, MeetingStatus.UPLOADED, MeetingStatus.QUEUED, MeetingStatus.PROCESSING),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def delete_meeting(meeting_id: str, db_path: Optional[Path] = None) -> Optional[Meeting]:
    """Delete one meeting row (and its search entry). Returns the deleted
    meeting so the caller can clean up its audio file, or None if absent."""
    meeting = get_meeting(meeting_id, db_path)
    if meeting is None:
        return None
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM meetings_fts WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
    finally:
        conn.close()
    return meeting


def purge_older_than(days: int, db_path: Optional[Path] = None) -> list[Meeting]:
    """Delete finished meetings older than `days`. Returns what was deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_connection(db_path)
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM meetings WHERE created_at < ? AND status IN (?, ?)",
                (cutoff, MeetingStatus.COMPLETED, MeetingStatus.FAILED),
            )
        ]
    finally:
        conn.close()
    return [m for m in (delete_meeting(i, db_path) for i in ids) if m is not None]
