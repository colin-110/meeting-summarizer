"""Schema creation and in-place migrations."""

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

# Full-text indexes, kept in sync by meetings_repo. owner_id is an *indexed*
# column so a search is `owner_id:<hash> AND <terms>` — the index narrows to
# one user's rows instead of matching everyone's and filtering afterwards.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS meetings_fts USING fts5(
    meeting_id UNINDEXED, owner_id, title, summary, transcript,
    tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    meeting_id UNINDEXED, owner_id, seg_index UNINDEXED, start UNINDEXED, text,
    tokenize = 'porter unicode61'
);
"""

# Plain tables: shared rate-limit/budget events, and live-recording pieces.
SUPPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_events (
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_events_key_ts ON rate_events(key, ts);
CREATE TABLE IF NOT EXISTS recording_chunks (
    meeting_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    offset_seconds REAL NOT NULL,
    audio_path TEXT,
    file_hash TEXT,
    status TEXT NOT NULL,
    segments TEXT,
    PRIMARY KEY (meeting_id, seq)
);
CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    until REAL NOT NULL
);
-- Answers keyed by (question, exact context sent): asking the same thing
-- about unchanged meetings costs no tokens.
CREATE TABLE IF NOT EXISTS answer_cache (
    key TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    meeting_id TEXT,
    answer TEXT NOT NULL,
    sources TEXT NOT NULL,
    created_at REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_answer_cache_owner ON answer_cache(owner_id);
"""

# Indexes on columns that only exist after the migration below.
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_meetings_owner_created ON meetings(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_meetings_status_created ON meetings(status, created_at);
"""

# meetings_fts was first created with owner_id UNINDEXED; rebuild it once.
_LEGACY_FTS_MARKER = "owner_id UNINDEXED, title"

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
    "lease_until": "REAL",
    "not_before": "REAL",
    "chapters": "TEXT",
    "notes_cache": "TEXT",
    "source": "TEXT NOT NULL DEFAULT 'upload'",
    "usage": "TEXT",
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
        legacy = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'meetings_fts'").fetchone()
        rebuild = legacy is not None and _LEGACY_FTS_MARKER in legacy["sql"]
        if rebuild:
            conn.execute("DROP TABLE meetings_fts")
        conn.executescript(FTS_SCHEMA)
        conn.executescript(SUPPORT_SCHEMA)
        if rebuild:
            conn.execute(
                "INSERT INTO meetings_fts (meeting_id, owner_id, title, summary, transcript) "
                "SELECT id, owner_id, title, summary, transcript FROM meetings WHERE summary IS NOT NULL"
            )
        conn.commit()
    finally:
        conn.close()
