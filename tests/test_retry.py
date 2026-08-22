import pytest

from backend.app.utils.retry import call_with_retry


class _FlakyError(Exception):
    pass


class _OtherError(Exception):
    pass


def test_call_with_retry_returns_on_first_success():
    assert call_with_retry(lambda: 42, retry_on=(_FlakyError,)) == 42


def test_call_with_retry_retries_then_succeeds():
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _FlakyError("temporary")
        return "ok"

    result = call_with_retry(flaky, retry_on=(_FlakyError,), attempts=5, base_delay=0)
    assert result == "ok"
    assert attempts["count"] == 3


def test_call_with_retry_raises_after_exhausting_attempts():
    def always_fails():
        raise _FlakyError("still broken")

    with pytest.raises(_FlakyError):
        call_with_retry(always_fails, retry_on=(_FlakyError,), attempts=2, base_delay=0)


def test_call_with_retry_does_not_retry_unlisted_exceptions():
    calls = {"count": 0}

    def raises_other():
        calls["count"] += 1
        raise _OtherError("not retryable")

    with pytest.raises(_OtherError):
        call_with_retry(raises_other, retry_on=(_FlakyError,), attempts=5, base_delay=0)
    assert calls["count"] == 1
