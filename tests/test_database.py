from backend.app.database.connection import get_connection
from backend.app.database.init_db import init_db
from backend.app.database.meetings_repo import (
    create_meeting,
    fail_stuck_processing,
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


def test_save_summary_persists_title_and_open_questions(tmp_path):
    db_path = _fresh_db(tmp_path)
    meeting = create_meeting("call.mp3", "hash123", "audio/call.mp3", db_path=db_path)

    save_summary(
        meeting.id,
        "Short summary",
        ["Decision one"],
        [{"task": "Follow up", "assignee": "Alex", "deadline": "Friday"}],
        title="Weekly Sync",
        open_questions=["Who owns the budget review?"],
        db_path=db_path,
    )

    result = get_meeting(meeting.id, db_path=db_path)
    assert result.title == "Weekly Sync"
    assert result.open_questions == ["Who owns the budget review?"]


def test_save_summary_defaults_title_and_open_questions_when_omitted(tmp_path):
    db_path = _fresh_db(tmp_path)
    meeting = create_meeting("call.mp3", "hash123", "audio/call.mp3", db_path=db_path)

    save_summary(meeting.id, "Short summary", ["Decision one"], [], db_path=db_path)

    result = get_meeting(meeting.id, db_path=db_path)
    assert result.title is None
    assert result.open_questions == []


def test_fail_stuck_processing_only_touches_processing_rows(tmp_path):
    db_path = _fresh_db(tmp_path)
    stuck = create_meeting("call.mp3", "hash1", "audio/call.mp3", db_path=db_path)
    update_status(stuck.id, MeetingStatus.PROCESSING, db_path=db_path)
    done = create_meeting("other.mp3", "hash2", "audio/other.mp3", db_path=db_path)
    update_status(done.id, MeetingStatus.COMPLETED, db_path=db_path)
    queued = create_meeting("third.mp3", "hash3", "audio/third.mp3", db_path=db_path)
    update_status(queued.id, MeetingStatus.QUEUED, db_path=db_path)

    affected = fail_stuck_processing(db_path=db_path)

    assert affected == 1
    recovered = get_meeting(stuck.id, db_path=db_path)
    assert recovered.status == MeetingStatus.FAILED
    assert "restart" in recovered.error_message
    assert get_meeting(done.id, db_path=db_path).status == MeetingStatus.COMPLETED
    assert get_meeting(queued.id, db_path=db_path).status == MeetingStatus.QUEUED


def test_fail_stuck_processing_returns_zero_when_nothing_stuck(tmp_path):
    db_path = _fresh_db(tmp_path)

    assert fail_stuck_processing(db_path=db_path) == 0


def test_init_db_migrates_columns_onto_existing_table_without_losing_data(tmp_path):
    # Simulates a database created before title/open_questions existed —
    # init_db must add the missing columns in place rather than requiring
    # the database to be dropped and recreated.
    db_path = tmp_path / "legacy.db"
    conn = get_connection(db_path)
    conn.executescript(
        """
        CREATE TABLE meetings (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            audio_path TEXT NOT NULL,
            status TEXT NOT NULL,
            transcript TEXT,
            summary TEXT,
            key_decisions TEXT,
            action_items TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            processing_started_at TEXT,
            processing_completed_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO meetings (id, filename, file_hash, audio_path, status, created_at, updated_at) "
        "VALUES ('legacy-id', 'old.mp3', 'oldhash', 'audio/old.mp3', 'COMPLETED', 't', 't')"
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    columns = {row["name"] for row in get_connection(db_path).execute("PRAGMA table_info(meetings)")}
    assert "title" in columns
    assert "open_questions" in columns

    preserved = get_meeting("legacy-id", db_path=db_path)
    assert preserved.filename == "old.mp3"
    assert preserved.title is None
    assert preserved.open_questions == []
