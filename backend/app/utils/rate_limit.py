"""In-memory per-client rate limiting.

No Redis or external store — this is a single-process app, so a plain
in-process counter is enough. The trade-off is the same one accepted
elsewhere in this project (see file_hash dedupe): state resets on
restart, which is fine for "stop one client from hammering the upload
endpoint," not fine for a durable, multi-instance rate limit.
"""

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_requests: dict[str, deque] = defaultdict(deque)


class RateLimitExceeded(Exception):
    """Raised when a client has exceeded the allowed request rate."""


def check_rate_limit(client_id: str, *, max_requests: int, window_seconds: float) -> None:
    """Raise RateLimitExceeded if client_id has made too many calls recently.

    Records the call otherwise. A sliding window per client, implemented
    as a deque of timestamps trimmed to the window on each check.
    """
    now = time.monotonic()
    with _lock:
        timestamps = _requests[client_id]
        while timestamps and now - timestamps[0] > window_seconds:
            timestamps.popleft()
        if len(timestamps) >= max_requests:
            raise RateLimitExceeded(
                f"Too many uploads from this address — limit is {max_requests} "
                f"per {int(window_seconds // 60) or 1} minute(s). Try again shortly."
            )
        timestamps.append(now)
