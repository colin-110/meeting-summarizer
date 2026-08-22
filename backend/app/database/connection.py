"""Raw sqlite3 connections — no ORM, the schema is one small table."""

import sqlite3
from pathlib import Path
from typing import Optional

from backend.app.core import config


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or config.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn
