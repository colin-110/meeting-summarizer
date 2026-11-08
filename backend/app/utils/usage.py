"""Per-job usage metering: what each meeting cost in provider resources.

Service code calls record_tokens() wherever it spends tokens; the job that is
running wraps its work in `with metering() as meter:` and saves the totals.
A context variable keeps concurrent jobs (worker threads) from mixing counts.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_meter: ContextVar[Optional[dict]] = ContextVar("usage_meter", default=None)


def new_usage() -> dict:
    return {"llm_tokens": 0, "llm_calls": 0, "models": {}}


@contextmanager
def metering() -> Iterator[dict]:
    meter = new_usage()
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


def record_tokens(model: str, tokens: float) -> None:
    meter = _meter.get()
    if meter is None:
        return
    meter["llm_tokens"] += int(tokens)
    meter["llm_calls"] += 1
    meter["models"][model] = meter["models"].get(model, 0) + int(tokens)


def merge_usage(total: dict, delta: dict) -> dict:
    """Add delta into total: numbers are summed, nested dicts merged the same way."""
    out = dict(total)
    for key, value in delta.items():
        if isinstance(value, dict):
            out[key] = merge_usage(out.get(key) or {}, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = round(out.get(key, 0) + value, 2)
        else:
            out[key] = value
    return out
