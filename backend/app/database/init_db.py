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
    summary TEXT,
    key_decisions TEXT,
    action_items TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    processing_started_at TEXT,
    processing_completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_meetings_file_hash ON meetings(file_hash);
"""


def init_db(db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
