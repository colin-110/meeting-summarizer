import groq
import httpx
import pytest

from backend.app.services.summarization_service import SummarizationError, summarize

VALID_JSON = (
    '{"summary": "The team agreed to ship on Friday.", '
    '"key_decisions": ["Ship the release on Friday"], '
    '"action_items": [{"task": "Prepare release notes", "assignee": "Alex", "deadline": "Friday"}]}'
)


def _client_with(create_fn):
    class _Completions:
        create = staticmethod(create_fn)

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client()


def _response(content: str):
    message = type("Message", (), {"content": content})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()


def _connection_error() -> groq.APIConnectionError:
    return groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))


def _silence_sleep(monkeypatch):
    monkeypatch.setattr("backend.app.utils.retry.time.sleep", lambda _: None)


def test_summarize_returns_parsed_result():
    client = _client_with(lambda **kwargs: _response(VALID_JSON))

    result = summarize("some transcript text", client=client)

    assert result["summary"] == "The team agreed to ship on Friday."
    assert result["key_decisions"] == ["Ship the release on Friday"]
    assert result["action_items"][0]["task"] == "Prepare release notes"


def test_summarize_rejects_empty_transcript():
    with pytest.raises(SummarizationError, match="empty"):
        summarize("   ")


def test_summarize_requests_strict_json_schema():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return _response(VALID_JSON)

    summarize("transcript", client=_client_with(create))

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["model"] == "openai/gpt-oss-120b"


def test_summarize_raises_on_malformed_json():
    client = _client_with(lambda **kwargs: _response("not valid json"))

    with pytest.raises(SummarizationError, match="malformed JSON"):
        summarize("transcript", client=client)


def test_summarize_raises_on_missing_required_field():
    client = _client_with(lambda **kwargs: _response('{"summary": "ok"}'))

    with pytest.raises(SummarizationError):
        summarize("transcript", client=client)


def test_summarize_retries_transient_errors_then_succeeds(monkeypatch):
    _silence_sleep(monkeypatch)
    calls = {"count": 0}

    def create(**kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise _connection_error()
        return _response(VALID_JSON)

    result = summarize("transcript", client=_client_with(create))
    assert calls["count"] == 3
    assert result["summary"]


def test_summarize_raises_after_exhausting_retries(monkeypatch):
    _silence_sleep(monkeypatch)

    def create(**kwargs):
        raise _connection_error()

    with pytest.raises(SummarizationError):
        summarize("transcript", client=_client_with(create))
