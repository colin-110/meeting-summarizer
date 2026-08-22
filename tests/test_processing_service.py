from backend.app.core import config
from backend.app.database.init_db import init_db
from backend.app.database.meetings_repo import (
    create_meeting,
    get_meeting,
    save_summary,
    save_transcript,
    update_status,
)
from backend.app.models.meeting import MeetingStatus
from backend.app.services import processing_service


def _setup_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)
    init_db(db_path)


def test_run_happy_path_marks_completed(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    meeting = create_meeting("call.mp3", "hash1", "audio/call.mp3")

    monkeypatch.setattr(processing_service, "transcribe", lambda path: "hello world")
    monkeypatch.setattr(
        processing_service,
        "summarize",
        lambda transcript: {"summary": "short summary", "key_decisions": ["d1"], "action_items": []},
    )

    processing_service.run(meeting.id)

    result = get_meeting(meeting.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.transcript == "hello world"
    assert result.summary == "short summary"
    assert result.key_decisions == ["d1"]


def test_run_marks_failed_on_transcription_error(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    meeting = create_meeting("call.mp3", "hash1", "audio/call.mp3")

    def _boom(path):
        raise processing_service.TranscriptionError("provider unreachable")

    summarize_called = {"value": False}

    def _should_not_run(transcript):
        summarize_called["value"] = True

    monkeypatch.setattr(processing_service, "transcribe", _boom)
    monkeypatch.setattr(processing_service, "summarize", _should_not_run)

    processing_service.run(meeting.id)

    result = get_meeting(meeting.id)
    assert result.status == MeetingStatus.FAILED
    assert "provider unreachable" in result.error_message
    assert summarize_called["value"] is False


def test_run_marks_failed_on_summarization_error(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    meeting = create_meeting("call.mp3", "hash1", "audio/call.mp3")

    def _boom(transcript):
        raise processing_service.SummarizationError("malformed response")

    monkeypatch.setattr(processing_service, "transcribe", lambda path: "hello world")
    monkeypatch.setattr(processing_service, "summarize", _boom)

    processing_service.run(meeting.id)

    result = get_meeting(meeting.id)
    assert result.status == MeetingStatus.FAILED
    assert "malformed response" in result.error_message


def test_run_reuses_cached_result_for_duplicate_hash(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    original = create_meeting("call.mp3", "duphash", "audio/call.mp3")
    save_transcript(original.id, "original transcript")
    save_summary(original.id, "original summary", ["d1"], [])
    update_status(original.id, MeetingStatus.COMPLETED)

    duplicate = create_meeting("call-copy.mp3", "duphash", "audio/call-copy.mp3")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("should not call the real ASR/LLM for a duplicate upload")

    monkeypatch.setattr(processing_service, "transcribe", _fail_if_called)
    monkeypatch.setattr(processing_service, "summarize", _fail_if_called)

    processing_service.run(duplicate.id)

    result = get_meeting(duplicate.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.transcript == "original transcript"
    assert result.summary == "original summary"
