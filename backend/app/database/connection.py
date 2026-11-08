"""Raw sqlite3 connections — no ORM, the schema is one small table."""

import sqlite3
from pathlib import Path
from typing import Optional

from backend.app.core import config


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or config.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL lets API reads proceed while worker threads write; busy_timeout makes
    # concurrent writers wait instead of failing with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn
