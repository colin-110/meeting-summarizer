"""CRUD against the `meetings` table.

Every function takes an optional db_path override so tests can point
at an isolated temp database instead of the real data/app.db.
"""

import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.app.core import config
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
        chapters=json.loads(row["chapters"]) if row["chapters"] else [],
        notes_cache=json.loads(row["notes_cache"]) if row["notes_cache"] else {},
        source=row["source"],
    )


def create_meeting(
    filename: str,
    file_hash: str,
    audio_path: str,
    db_path: Optional[Path] = None,
    *,
    owner_id: str = "",
    source: str = "upload",
    status: str = MeetingStatus.UPLOADED,
) -> Meeting:
    meeting_id = uuid.uuid4().hex
    now = _now()
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO meetings (id, filename, file_hash, audio_path, status, created_at, updated_at, owner_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (meeting_id, filename, file_hash, audio_path, status, now, now, owner_id, source),
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

# Highlight from the transcript, else summary, else title — never owner_id,
# which matches every row of a scoped search.
_SNIPPET_SQL = "CASE " + " ".join(
    f"WHEN instr(snippet(meetings_fts, {col}, '[[', ']]', '…', 12), '[[') > 0 "
    f"THEN snippet(meetings_fts, {col}, '[[', ']]', '…', 12)"
    for col in (4, 3, 2)
) + " ELSE NULL END"


def _fts_query(text: str) -> Optional[str]:
    """Turn free user text into a safe FTS5 query: every word quoted (so
    operators/punctuation can't break the syntax) with prefix matching."""
    words = re.findall(r"\w+", text)
    return " ".join(f'"{w}"*' for w in words[:10]) or None


def _scoped(match: str, owner_id: Optional[str]) -> str:
    return f'owner_id:"{owner_id}" AND ({match})' if owner_id is not None else match


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
                f"{_SNIPPET_SQL} AS snippet "
                "FROM meetings_fts JOIN meetings m ON m.id = meetings_fts.meeting_id "
                "WHERE meetings_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?"
            )
            params: list = [_scoped(match, owner_id)]
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
    conn.execute("DELETE FROM segments_fts WHERE meeting_id = ?", (meeting_id,))
    conn.execute(
        "INSERT INTO meetings_fts (meeting_id, owner_id, title, summary, transcript) "
        "SELECT id, owner_id, title, summary, transcript FROM meetings WHERE id = ?",
        (meeting_id,),
    )
    row = conn.execute("SELECT owner_id, segments FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if row and row["segments"]:
        conn.executemany(
            "INSERT INTO segments_fts (meeting_id, owner_id, seg_index, start, text) VALUES (?, ?, ?, ?, ?)",
            [(meeting_id, row["owner_id"], i, s["start"], s["text"]) for i, s in enumerate(json.loads(row["segments"]))],
        )


def claim_next(db_path: Optional[Path] = None) -> Optional[str]:
    """Atomically lease the next runnable job and mark it PROCESSING.

    Runnable means QUEUED (and past any retry delay), or PROCESSING with an
    expired lease — its worker died, so the job is picked up again. This is
    the visibility-timeout pattern SQS uses: no startup sweep is needed, and
    it's safe with any number of worker processes. BEGIN IMMEDIATE takes the
    write lock up front, so two workers can never lease the same job.
    """
    now = time.time()
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Jobs whose worker died repeatedly are abandoned, not retried forever.
        conn.execute(
            "UPDATE meetings SET status = ?, error_message = ?, updated_at = ?, processing_completed_at = ? "
            "WHERE status = ? AND lease_until < ? AND attempts >= ?",
            (
                MeetingStatus.FAILED,
                "Processing was interrupted repeatedly and has been abandoned.",
                _now(),
                _now(),
                MeetingStatus.PROCESSING,
                now,
                config.MAX_JOB_ATTEMPTS,
            ),
        )
        row = conn.execute(
            "SELECT id FROM meetings WHERE "
            "(status = ? AND (not_before IS NULL OR not_before <= ?)) "
            "OR (status = ? AND lease_until < ?) "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
            (MeetingStatus.QUEUED, now, MeetingStatus.PROCESSING, now),
        ).fetchone()
        if row is None:
            conn.commit()  # keep any abandonments made above
            return None
        conn.execute(
            "UPDATE meetings SET status = ?, stage = ?, attempts = attempts + 1, lease_until = ?, "
            "not_before = NULL, processing_started_at = ?, updated_at = ? WHERE id = ?",
            (MeetingStatus.PROCESSING, "starting", now + config.LEASE_SECONDS, _now(), _now(), row["id"]),
        )
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def renew_lease(meeting_id: str, db_path: Optional[Path] = None) -> None:
    """Heartbeat: extend a running job's lease so it isn't taken over."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE meetings SET lease_until = ? WHERE id = ? AND status = ?",
            (time.time() + config.LEASE_SECONDS, meeting_id, MeetingStatus.PROCESSING),
        )
        conn.commit()
    finally:
        conn.close()


def defer(meeting_id: str, delay_seconds: float, reason: str, db_path: Optional[Path] = None) -> None:
    """Put a job back on the queue to retry after a delay (provider out of
    capacity). Doesn't count as a failed attempt."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE meetings SET status = ?, stage = ?, not_before = ?, lease_until = NULL, "
            "attempts = MAX(attempts - 1, 0), updated_at = ? WHERE id = ?",
            (MeetingStatus.QUEUED, reason, time.time() + delay_seconds, _now(), meeting_id),
        )
        conn.commit()
    finally:
        conn.close()


def queue_depth(db_path: Optional[Path] = None) -> int:
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM meetings WHERE status = ?", (MeetingStatus.QUEUED,)
        ).fetchone()[0]
    finally:
        conn.close()


def queue_stats(db_path: Optional[Path] = None) -> dict:
    """Numbers for /metrics: counts by status, oldest waiting job, recent durations."""
    conn = get_connection(db_path)
    try:
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM meetings GROUP BY status")}
        oldest = conn.execute(
            "SELECT MIN(created_at) FROM meetings WHERE status = ?", (MeetingStatus.QUEUED,)
        ).fetchone()[0]
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        durations = conn.execute(
            "SELECT processing_started_at, processing_completed_at FROM meetings "
            "WHERE status = ? AND processing_completed_at > ?",
            (MeetingStatus.COMPLETED, cutoff),
        ).fetchall()
    finally:
        conn.close()
    secs = [
        (datetime.fromisoformat(r[1]) - datetime.fromisoformat(r[0])).total_seconds()
        for r in durations if r[0] and r[1]
    ]
    return {
        "by_status": by_status,
        "oldest_queued_seconds": (
            (datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds() if oldest else 0.0
        ),
        "completed_last_hour": len(secs),
        "avg_processing_seconds_last_hour": sum(secs) / len(secs) if secs else 0.0,
    }


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
        conn.execute("DELETE FROM segments_fts WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM recording_chunks WHERE meeting_id = ?", (meeting_id,))
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
    finally:
        conn.close()
    return meeting


def purge_older_than(days: int, db_path: Optional[Path] = None) -> list[Meeting]:
    """Delete finished meetings older than `days`, and live recordings that
    were started but never finished more than a day ago. Returns what was deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    abandoned = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn = get_connection(db_path)
    try:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM meetings WHERE (created_at < ? AND status IN (?, ?)) "
                "OR (updated_at < ? AND status = ?)",
                (cutoff, MeetingStatus.COMPLETED, MeetingStatus.FAILED, abandoned, MeetingStatus.RECORDING),
            )
        ]
    finally:
        conn.close()
    return [m for m in (delete_meeting(i, db_path) for i in ids) if m is not None]


def save_chapters(meeting_id: str, chapters: list, db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE meetings SET chapters = ?, updated_at = ? WHERE id = ?",
            (json.dumps(chapters), _now(), meeting_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_notes_cache(meeting_id: str, cache: dict, db_path: Optional[Path] = None) -> None:
    """Checkpoint finished section summaries, so a retry only redoes what's missing."""
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE meetings SET notes_cache = ? WHERE id = ?", (json.dumps(cache), meeting_id))
        conn.commit()
    finally:
        conn.close()


def search_segments(owner_id: str, terms: list[str], limit: int = 30, meeting_id: Optional[str] = None,
                    db_path: Optional[Path] = None) -> list[dict]:
    """BM25-ranked transcript segments matching any of `terms`, for one owner
    (optionally one meeting). Each hit: meeting_id, title, seg_index, start, text."""
    if not terms:
        return []
    match = _scoped(" OR ".join(f'"{t}"' for t in terms), owner_id)
    sql = (
        "SELECT s.meeting_id, m.title, m.filename, s.seg_index, s.start, s.text "
        "FROM segments_fts s JOIN meetings m ON m.id = s.meeting_id WHERE segments_fts MATCH ? "
    )
    params: list = [match]
    if meeting_id is not None:
        sql += "AND s.meeting_id = ? "
        params.append(meeting_id)
    sql += "ORDER BY rank LIMIT ?"
    params.append(limit)
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---- live recording pieces ----

def add_recording_chunk(meeting_id: str, seq: int, offset_seconds: float, audio_path: str, file_hash: str,
                        db_path: Optional[Path] = None) -> bool:
    """Register an uploaded piece. Returns False if this seq was already received
    (client retry), so a duplicate never double-counts audio."""
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO recording_chunks (meeting_id, seq, offset_seconds, audio_path, file_hash, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (meeting_id, seq, offset_seconds, audio_path, file_hash),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def save_chunk_segments(meeting_id: str, seq: int, segments: list, db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE recording_chunks SET status = 'done', segments = ? WHERE meeting_id = ? AND seq = ?",
            (json.dumps(segments), meeting_id, seq),
        )
        conn.commit()
    finally:
        conn.close()


def list_recording_chunks(meeting_id: str, db_path: Optional[Path] = None) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM recording_chunks WHERE meeting_id = ? ORDER BY seq", (meeting_id,)
        ).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "segments": json.loads(r["segments"]) if r["segments"] else []} for r in rows]


def chunk_audio_in_use(file_hash: str, db_path: Optional[Path] = None) -> bool:
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM recording_chunks WHERE file_hash = ? AND status = 'pending' LIMIT 1", (file_hash,)
        ).fetchone() is not None
    finally:
        conn.close()
