"""A tiny cross-process lock with a time limit, stored in SQLite.

Used so only one process works on a given background task at a time (e.g.
pre-summarizing a live recording), across several web and worker processes.
The lock expires on its own, so a crashed holder never blocks anyone for long.
"""

import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from backend.app.database.connection import get_connection


def try_acquire(name: str, ttl_seconds: float, db_path: Optional[Path] = None) -> Optional[str]:
    """Take the lock if it's free or expired. Returns a holder id, or None."""
    holder = uuid.uuid4().hex
    now = time.time()
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT until FROM locks WHERE name = ?", (name,)).fetchone()
        if row is not None and row["until"] > now:
            conn.rollback()
            return None
        conn.execute(
            "INSERT OR REPLACE INTO locks (name, holder, until) VALUES (?, ?, ?)", (name, holder, now + ttl_seconds)
        )
        conn.commit()
        return holder
    finally:
        conn.close()


def release(name: str, holder: str, db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder))
        conn.commit()
    finally:
        conn.close()


@contextmanager
def held(name: str, ttl_seconds: float) -> Iterator[bool]:
    """`with held("x", 60) as got:` — got is False if someone else holds it."""
    holder = try_acquire(name, ttl_seconds)
    try:
        yield holder is not None
    finally:
        if holder is not None:
            release(name, holder)
