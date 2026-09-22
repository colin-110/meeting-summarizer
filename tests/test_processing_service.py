import threading
import time

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

    monkeypatch.setattr(processing_service, "transcribe", lambda path: "hello world, this is the start of the meeting")
    monkeypatch.setattr(
        processing_service,
        "summarize",
        lambda transcript: {"summary": "short summary", "key_decisions": ["d1"], "action_items": []},
    )

    processing_service.run(meeting.id)

    result = get_meeting(meeting.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.transcript == "hello world, this is the start of the meeting"
    assert result.summary == "short summary"
    assert result.key_decisions == ["d1"]


def test_run_happy_path_persists_title_and_open_questions(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    meeting = create_meeting("call.mp3", "hash1", "audio/call.mp3")

    monkeypatch.setattr(processing_service, "transcribe", lambda path: "hello world, this is the start of the meeting")
    monkeypatch.setattr(
        processing_service,
        "summarize",
        lambda transcript: {
            "title": "Weekly Sync",
            "summary": "short summary",
            "key_decisions": ["d1"],
            "action_items": [],
            "open_questions": ["What's the budget?"],
        },
    )

    processing_service.run(meeting.id)

    result = get_meeting(meeting.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.title == "Weekly Sync"
    assert result.open_questions == ["What's the budget?"]


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


def test_run_marks_failed_on_too_short_transcript(tmp_path, monkeypatch):
    # Whisper hallucinates short boilerplate for silence/no-speech audio
    # instead of returning empty text — this must not reach the summarizer.
    _setup_db(tmp_path, monkeypatch)
    meeting = create_meeting("silence.wav", "hash1", "audio/silence.wav")

    summarize_called = {"value": False}

    monkeypatch.setattr(processing_service, "transcribe", lambda path: " Thank you.")
    monkeypatch.setattr(processing_service, "summarize", lambda transcript: summarize_called.update(value=True))

    processing_service.run(meeting.id)

    result = get_meeting(meeting.id)
    assert result.status == MeetingStatus.FAILED
    assert "too short" in result.error_message
    assert result.transcript == " Thank you."
    assert summarize_called["value"] is False


def test_run_marks_failed_on_summarization_error(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    meeting = create_meeting("call.mp3", "hash1", "audio/call.mp3")

    def _boom(transcript):
        raise processing_service.SummarizationError("malformed response")

    monkeypatch.setattr(processing_service, "transcribe", lambda path: "hello world, this is the start of the meeting")
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


def _fail_if_called(*args, **kwargs):
    raise AssertionError("should not call the real ASR/LLM while waiting on another in-flight upload")


def test_run_waits_for_in_flight_duplicate_then_reuses_its_result(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(processing_service, "_FOLLOW_POLL_SECONDS", 0.01)

    leader = create_meeting("call.mp3", "duphash", "audio/call.mp3")
    update_status(leader.id, MeetingStatus.PROCESSING)  # still in flight, not COMPLETED yet
    follower = create_meeting("call-copy.mp3", "duphash", "audio/call-copy.mp3")

    monkeypatch.setattr(processing_service, "transcribe", _fail_if_called)
    monkeypatch.setattr(processing_service, "summarize", _fail_if_called)

    def _complete_leader_shortly():
        time.sleep(0.05)
        save_transcript(leader.id, "leader transcript")
        save_summary(leader.id, "leader summary", ["d1"], [])
        update_status(leader.id, MeetingStatus.COMPLETED)

    threading.Thread(target=_complete_leader_shortly).start()

    processing_service.run(follower.id)

    result = get_meeting(follower.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.transcript == "leader transcript"
    assert result.summary == "leader summary"


def test_run_propagates_failure_from_in_flight_duplicate(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(processing_service, "_FOLLOW_POLL_SECONDS", 0.01)

    leader = create_meeting("call.mp3", "duphash", "audio/call.mp3")
    update_status(leader.id, MeetingStatus.PROCESSING)
    follower = create_meeting("call-copy.mp3", "duphash", "audio/call-copy.mp3")

    monkeypatch.setattr(processing_service, "transcribe", _fail_if_called)
    monkeypatch.setattr(processing_service, "summarize", _fail_if_called)

    def _fail_leader_shortly():
        time.sleep(0.05)
        update_status(leader.id, MeetingStatus.FAILED, error_message="upstream ASR error")

    threading.Thread(target=_fail_leader_shortly).start()

    processing_service.run(follower.id)

    result = get_meeting(follower.id)
    assert result.status == MeetingStatus.FAILED
    assert "upstream ASR error" in result.error_message


def test_run_gives_up_and_processes_independently_if_leader_never_resolves(tmp_path, monkeypatch):
    # Simulates a leader whose process crashed mid-PROCESSING — the follower
    # shouldn't wait forever for a restart's fail_stuck_processing sweep.
    _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(processing_service, "_FOLLOW_POLL_SECONDS", 0.01)
    monkeypatch.setattr(processing_service, "_FOLLOW_MAX_WAIT_SECONDS", 0.03)

    leader = create_meeting("call.mp3", "duphash", "audio/call.mp3")
    update_status(leader.id, MeetingStatus.PROCESSING)  # never resolves
    follower = create_meeting("call-copy.mp3", "duphash", "audio/call-copy.mp3")

    monkeypatch.setattr(
        processing_service, "transcribe", lambda path: "hello world, this is the start of the meeting"
    )
    monkeypatch.setattr(
        processing_service,
        "summarize",
        lambda transcript: {"summary": "independent summary", "key_decisions": [], "action_items": []},
    )

    processing_service.run(follower.id)

    result = get_meeting(follower.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.summary == "independent summary"
