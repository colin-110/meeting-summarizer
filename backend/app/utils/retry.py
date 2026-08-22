"""A small retry helper for transient provider errors.

Only meant to wrap calls whose failure mode is genuinely temporary —
a network blip, a rate limit, an upstream 5xx. Never wrap validation
or auth failures in this; a second attempt won't fix those, it'll
just add latency before the same error surfaces anyway.
"""

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def call_with_retry(
    fn: Callable[[], T],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 3,
    base_delay: float = 1.5,
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retry_on:
            if attempt == attempts:
                raise
            time.sleep(base_delay * attempt)
    raise AssertionError("unreachable")  # loop always returns or raises
