import groq
import httpx
import pytest

from backend.app.services.transcription_service import TranscriptionError, transcribe


def _client_with(create_fn):
    class _Transcriptions:
        create = staticmethod(create_fn)

    class _Audio:
        transcriptions = _Transcriptions()

    class _Client:
        audio = _Audio()

    return _Client()


def _connection_error() -> groq.APIConnectionError:
    return groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))


def _silence_sleep(monkeypatch):
    monkeypatch.setattr("backend.app.utils.retry.time.sleep", lambda _: None)


def test_transcribe_returns_text_on_success(tmp_path):
    audio_path = tmp_path / "clip.mp3"
    audio_path.write_bytes(b"fake audio bytes")

    def create(**kwargs):
        assert kwargs["model"] == "whisper-large-v3-turbo"
        return type("Result", (), {"text": "hello from the meeting"})()

    client = _client_with(create)
    assert transcribe(str(audio_path), client=client) == "hello from the meeting"


def test_transcribe_retries_transient_errors_then_succeeds(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    audio_path = tmp_path / "clip.mp3"
    audio_path.write_bytes(b"fake audio bytes")

    calls = {"count": 0}

    def create(**kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise _connection_error()
        return type("Result", (), {"text": "ok"})()

    client = _client_with(create)
    assert transcribe(str(audio_path), client=client) == "ok"
    assert calls["count"] == 3


def test_transcribe_raises_after_exhausting_retries(tmp_path, monkeypatch):
    _silence_sleep(monkeypatch)
    audio_path = tmp_path / "clip.mp3"
    audio_path.write_bytes(b"fake audio bytes")

    def create(**kwargs):
        raise _connection_error()

    client = _client_with(create)
    with pytest.raises(TranscriptionError):
        transcribe(str(audio_path), client=client)


def test_transcribe_wraps_non_retryable_errors(tmp_path):
    audio_path = tmp_path / "clip.mp3"
    audio_path.write_bytes(b"fake audio bytes")

    def create(**kwargs):
        raise RuntimeError("boom")

    client = _client_with(create)
    with pytest.raises(TranscriptionError):
        transcribe(str(audio_path), client=client)
