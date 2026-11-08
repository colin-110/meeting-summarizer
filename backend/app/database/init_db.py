"""Schema creation for the single `meetings` table."""

from pathlib import Path
from typing import Optional

from backend.app.database.connection import get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    status TEXT NOT NULL,
    transcript TEXT,
    title TEXT,
    summary TEXT,
    key_decisions TEXT,
    action_items TEXT,
    open_questions TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    processing_started_at TEXT,
    processing_completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_meetings_file_hash ON meetings(file_hash);
"""

# Full-text index over the searchable text. Kept in sync by meetings_repo.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(
    meeting_id UNINDEXED, owner_id UNINDEXED, title, summary, transcript,
    tokenize = 'porter unicode61'
);
"""

# Indexes on columns that only exist after the migration below.
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_meetings_owner_created ON meetings(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_meetings_status_created ON meetings(status, created_at);
"""

# Added after the table already existed in deployed/local databases —
# CREATE TABLE IF NOT EXISTS won't retrofit new columns onto an existing
# table, so missing ones are migrated in by hand here.
_NEW_COLUMNS = {
    "title": "TEXT",
    "open_questions": "TEXT",
    "owner_id": "TEXT NOT NULL DEFAULT ''",
    "stage": "TEXT",
    "segments": "TEXT",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "audio_seconds": "REAL",
}


def init_db(db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
        for column, column_type in _NEW_COLUMNS.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE meetings ADD COLUMN {column} {column_type}")
        conn.executescript(POST_MIGRATION_INDEXES)
        conn.executescript(FTS_SCHEMA)
        conn.commit()
    finally:
        conn.close()
