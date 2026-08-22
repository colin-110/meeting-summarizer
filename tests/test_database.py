from backend.app.database.init_db import init_db
from backend.app.database.meetings_repo import (
    create_meeting,
    find_completed_by_hash,
    get_meeting,
    save_summary,
    save_transcript,
    update_status,
)
from backend.app.models.meeting import MeetingStatus


def _fresh_db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    return db_path


def test_create_and_get_meeting(tmp_path):
    db_path = _fresh_db(tmp_path)

    meeting = create_meeting("call.mp3", "hash123", "data/audio/call.mp3", db_path=db_path)

    assert meeting.status == MeetingStatus.UPLOADED
    fetched = get_meeting(meeting.id, db_path=db_path)
    assert fetched.filename == "call.mp3"
    assert fetched.file_hash == "hash123"


def test_get_meeting_missing_returns_none(tmp_path):
    db_path = _fresh_db(tmp_path)

    assert get_meeting("does-not-exist", db_path=db_path) is None


def test_update_status_sets_timestamps(tmp_path):
    db_path = _fresh_db(tmp_path)
    meeting = create_meeting("call.mp3", "hash123", "audio/call.mp3", db_path=db_path)

    update_status(meeting.id, MeetingStatus.PROCESSING, db_path=db_path)
    processing = get_meeting(meeting.id, db_path=db_path)
    assert processing.status == MeetingStatus.PROCESSING
    assert processing.processing_started_at is not None

    update_status(meeting.id, MeetingStatus.FAILED, error_message="ASR timed out", db_path=db_path)
    failed = get_meeting(meeting.id, db_path=db_path)
    assert failed.status == MeetingStatus.FAILED
    assert failed.error_message == "ASR timed out"
    assert failed.processing_completed_at is not None


def test_save_transcript_and_summary(tmp_path):
    db_path = _fresh_db(tmp_path)
    meeting = create_meeting("call.mp3", "hash123", "audio/call.mp3", db_path=db_path)

    save_transcript(meeting.id, "hello world", db_path=db_path)
    save_summary(
        meeting.id,
        "Short summary",
        ["Decision one"],
        [{"task": "Follow up", "assignee": "Alex", "deadline": "Friday"}],
        db_path=db_path,
    )

    result = get_meeting(meeting.id, db_path=db_path)
    assert result.transcript == "hello world"
    assert result.summary == "Short summary"
    assert result.key_decisions == ["Decision one"]
    assert result.action_items[0]["task"] == "Follow up"


def test_find_completed_by_hash_only_matches_completed(tmp_path):
    db_path = _fresh_db(tmp_path)
    meeting = create_meeting("call.mp3", "duphash", "audio/call.mp3", db_path=db_path)

    assert find_completed_by_hash("duphash", db_path=db_path) is None

    update_status(meeting.id, MeetingStatus.COMPLETED, db_path=db_path)
    match = find_completed_by_hash("duphash", db_path=db_path)
    assert match is not None
    assert match.id == meeting.id
