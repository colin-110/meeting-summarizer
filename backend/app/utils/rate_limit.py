"""Rate limits and provider budgets, shared across processes via SQLite.

Two tools on one table of timestamped events:
- check_rate_limit: "at most N calls per window" for API clients.
- acquire_budget / settle_budget: "at most N units (tokens, requests) per
  window" for the AI provider. A caller that would exceed the budget waits
  for room instead of failing, so many workers together stay under the
  provider's limit.

State lives in the database rather than process memory, so limits hold when
the app runs as several web processes and separate worker processes.
"""

import time
from pathlib import Path
from typing import Optional

from backend.app.database.connection import get_connection
from backend.app.utils.errors import ProviderBusyError


class RateLimitExceeded(Exception):
    """Raised when a client has exceeded the allowed request rate."""


def check_rate_limit(
    client_id: str, *, max_requests: int, window_seconds: float, db_path: Optional[Path] = None
) -> None:
    """Raise RateLimitExceeded if client_id has made too many calls recently;
    otherwise record this call. Sliding window."""
    now = time.time()
    key = f"rl:{client_id}"
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM rate_events WHERE key = ? AND ts <= ?", (key, now - window_seconds))
        count = conn.execute("SELECT COUNT(*) FROM rate_events WHERE key = ?", (key,)).fetchone()[0]
        if count >= max_requests:
            conn.rollback()
            raise RateLimitExceeded(
                f"Too many uploads from this address — limit is {max_requests} "
                f"per {int(window_seconds // 60) or 1} minute(s). Try again shortly."
            )
        conn.execute("INSERT INTO rate_events (key, ts, amount) VALUES (?, ?, 1)", (key, now))
        conn.commit()
    finally:
        conn.close()


def acquire_budget(
    key: str,
    amount: float,
    *,
    limit: float,
    window_seconds: float = 60.0,
    max_wait_seconds: float = 240.0,
    db_path: Optional[Path] = None,
) -> int:
    """Reserve `amount` units of `key`'s budget, waiting until it fits.

    Returns a reservation id to pass to settle_budget once the real usage is
    known. Raises ProviderBusyError if no room opens up within max_wait_seconds.
    """
    amount = min(amount, limit)  # one oversized request still gets to try alone
    deadline = time.time() + max_wait_seconds
    bkey = f"budget:{key}"
    while True:
        now = time.time()
        conn = get_connection(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_events WHERE key = ? AND ts <= ?", (bkey, now - window_seconds))
            used, oldest = conn.execute(
                "SELECT COALESCE(SUM(amount), 0), MIN(ts) FROM rate_events WHERE key = ?", (bkey,)
            ).fetchone()
            if used + amount <= limit:
                cur = conn.execute(
                    "INSERT INTO rate_events (key, ts, amount) VALUES (?, ?, ?)", (bkey, now, amount)
                )
                conn.commit()
                return cur.lastrowid
            conn.rollback()
        finally:
            conn.close()
        wait = (oldest + window_seconds - now) if oldest else 1.0
        if now + wait > deadline:
            raise ProviderBusyError(
                f"AI provider budget for {key} is exhausted right now.", retry_after=wait
            )
        time.sleep(min(max(wait, 0.2), 5.0))


def acquire_first_available(
    keys: list[str],
    amount: float,
    *,
    limit: float,
    first_wait_seconds: float,
    max_wait_seconds: float,
) -> tuple[int, int]:
    """Reserve on the first key with room soon enough: each key but the last
    gets first_wait_seconds, the last gets max_wait_seconds. Used to spill
    over to a second model (its own budget at the provider) instead of
    waiting for the first. Returns (index of the key used, reservation id)."""
    for i, key in enumerate(keys):
        last = i == len(keys) - 1
        try:
            wait = max_wait_seconds if last else min(first_wait_seconds, max_wait_seconds)
            return i, acquire_budget(key, amount, limit=limit, max_wait_seconds=wait)
        except ProviderBusyError:
            if last:
                raise
    raise ValueError("no budget keys given")


def settle_budget(reservation_id: int, actual: float, db_path: Optional[Path] = None) -> None:
    """Replace a reservation's estimate with what the call really used."""
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE rate_events SET amount = ? WHERE rowid = ?", (actual, reservation_id))
        conn.commit()
    finally:
        conn.close()


def budget_used(key: str, window_seconds: float = 60.0, db_path: Optional[Path] = None) -> float:
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM rate_events WHERE key = ? AND ts > ?",
            (f"budget:{key}", time.time() - window_seconds),
        ).fetchone()[0]
    finally:
        conn.close()
