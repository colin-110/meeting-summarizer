import uuid

import pytest

from backend.app.utils.rate_limit import RateLimitExceeded, check_rate_limit

# Each test uses its own client id — the limiter's state is module-level
# global state, so a shared id across tests would leak counts between them.


def _client() -> str:
    return uuid.uuid4().hex


def test_allows_requests_under_the_limit():
    client = _client()
    for _ in range(3):
        check_rate_limit(client, max_requests=3, window_seconds=60)


def test_blocks_once_limit_is_reached():
    client = _client()
    for _ in range(2):
        check_rate_limit(client, max_requests=2, window_seconds=60)

    with pytest.raises(RateLimitExceeded):
        check_rate_limit(client, max_requests=2, window_seconds=60)


def test_different_clients_have_independent_limits():
    client_a, client_b = _client(), _client()
    for _ in range(2):
        check_rate_limit(client_a, max_requests=2, window_seconds=60)

    # client_b hasn't made any requests yet, so it isn't affected by A's count.
    check_rate_limit(client_b, max_requests=2, window_seconds=60)


def test_old_requests_fall_out_of_the_window():
    client = _client()
    check_rate_limit(client, max_requests=1, window_seconds=0)

    # window_seconds=0 means the first request is immediately outside the
    # window on the next check, so it should not count against the limit.
    check_rate_limit(client, max_requests=1, window_seconds=0)
