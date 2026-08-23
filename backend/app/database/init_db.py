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

# Added after the table already existed in deployed/local databases —
# CREATE TABLE IF NOT EXISTS won't retrofit new columns onto an existing
# table, so missing ones are migrated in by hand here.
_NEW_COLUMNS = {
    "title": "TEXT",
    "open_questions": "TEXT",
}


def init_db(db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
        for column, column_type in _NEW_COLUMNS.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE meetings ADD COLUMN {column} {column_type}")
        conn.commit()
    finally:
        conn.close()
