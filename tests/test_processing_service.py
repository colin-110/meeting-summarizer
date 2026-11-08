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

    monkeypatch.setattr(processing_service, "transcribe_file", lambda path, **kw: {"text": "hello world, this is the start of the meeting", "segments": [], "duration": 0.0})
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

    monkeypatch.setattr(processing_service, "transcribe_file", lambda path, **kw: {"text": "hello world, this is the start of the meeting", "segments": [], "duration": 0.0})
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

    def _boom(path, **kw):
        raise processing_service.TranscriptionError("provider unreachable")

    summarize_called = {"value": False}

    def _should_not_run(transcript):
        summarize_called["value"] = True

    monkeypatch.setattr(processing_service, "transcribe_file", _boom)
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

    monkeypatch.setattr(processing_service, "transcribe_file", lambda path, **kw: {"text": " Thank you.", "segments": [], "duration": 0.0})
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

    monkeypatch.setattr(processing_service, "transcribe_file", lambda path, **kw: {"text": "hello world, this is the start of the meeting", "segments": [], "duration": 0.0})
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

    monkeypatch.setattr(processing_service, "transcribe_file", _fail_if_called)
    monkeypatch.setattr(processing_service, "summarize", _fail_if_called)

    processing_service.run(duplicate.id)

    result = get_meeting(duplicate.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.transcript == "original transcript"
    assert result.summary == "original summary"


def _fail_if_called(*args, **kwargs):
    raise AssertionError("should not call the real ASR/LLM while waiting on another in-flight upload")


def test_in_flight_duplicate_defers_instead_of_blocking_then_reuses_result(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    leader = create_meeting("call.mp3", "duphash", "audio/call.mp3")
    update_status(leader.id, MeetingStatus.PROCESSING)  # still in flight
    follower = create_meeting("call-copy.mp3", "duphash", "audio/call-copy.mp3")
    monkeypatch.setattr(processing_service, "transcribe_file", _fail_if_called)
    monkeypatch.setattr(processing_service, "summarize", _fail_if_called)

    started = time.monotonic()
    processing_service.run(follower.id)
    assert time.monotonic() - started < 1  # returned immediately: no worker thread held

    waiting = get_meeting(follower.id)
    assert waiting.status == MeetingStatus.QUEUED
    assert waiting.stage == "waiting for an identical upload"

    save_transcript(leader.id, "leader transcript")
    save_summary(leader.id, "leader summary", ["d1"], [])
    update_status(leader.id, MeetingStatus.COMPLETED)
    processing_service.run(follower.id)

    result = get_meeting(follower.id)
    assert result.status == MeetingStatus.COMPLETED
    assert result.transcript == "leader transcript"
    assert result.summary == "leader summary"


def test_an_earlier_failed_upload_does_not_block_reprocessing(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    failed = create_meeting("call.mp3", "duphash", "audio/call.mp3")
    update_status(failed.id, MeetingStatus.FAILED, error_message="upstream ASR error")
    retry = create_meeting("call-again.mp3", "duphash", "audio/call-again.mp3")
    monkeypatch.setattr(
        processing_service, "transcribe_file",
        lambda path, **kw: {"text": "hello world, this is the start of the meeting", "segments": [], "duration": 0.0},
    )
    monkeypatch.setattr(processing_service, "summarize",
                        lambda t: {"summary": "fresh", "key_decisions": [], "action_items": []})

    processing_service.run(retry.id)

    assert get_meeting(retry.id).status == MeetingStatus.COMPLETED
    assert get_meeting(retry.id).summary == "fresh"


def test_run_processes_independently_once_it_has_waited_too_long(tmp_path, monkeypatch):
    # The leader's worker died; the follower shouldn't wait forever.
    _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(processing_service, "_FOLLOW_MAX_WAIT_SECONDS", 0)

    leader = create_meeting("call.mp3", "duphash", "audio/call.mp3")
    update_status(leader.id, MeetingStatus.PROCESSING)  # never resolves
    follower = create_meeting("call-copy.mp3", "duphash", "audio/call-copy.mp3")

    monkeypatch.setattr(
        processing_service, "transcribe_file", lambda path, **kw: {"text": "hello world, this is the start of the meeting", "segments": [], "duration": 0.0}
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


def test_audio_deleted_after_processing_unless_retained_or_still_needed(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "RETAIN_AUDIO", False)
    audio = tmp_path / "a.mp3"

    def fresh(name="x.mp3"):
        audio.write_bytes(b"audio")
        return create_meeting(name, "same-hash", str(audio))

    done = {"text": "hello world, this is the start of the meeting", "segments": [], "duration": 0.0}
    monkeypatch.setattr(processing_service, "transcribe_file", lambda path, **kw: done)
    monkeypatch.setattr(processing_service, "summarize", lambda t: {
        "title": "T", "summary": "s", "key_decisions": [], "action_items": [], "open_questions": []})

    # Another upload of the same audio is still waiting -> keep the file for it.
    first = fresh()
    waiting = create_meeting("y.mp3", "same-hash", str(audio))
    update_status(waiting.id, MeetingStatus.QUEUED)
    processing_service.run(first.id)
    assert audio.exists()

    # Nothing else needs it -> deleted.
    update_status(waiting.id, MeetingStatus.COMPLETED)
    other = fresh("z.mp3")
    update_status(other.id, MeetingStatus.PROCESSING)
    processing_service.release_audio(get_meeting(other.id))
    assert audio.exists()  # still processing: may retry, so keep it
    update_status(other.id, MeetingStatus.COMPLETED)
    update_status(first.id, MeetingStatus.COMPLETED)
    processing_service.release_audio(get_meeting(other.id))
    assert not audio.exists()

    # RETAIN_AUDIO keeps it.
    monkeypatch.setattr(config, "RETAIN_AUDIO", True)
    audio.write_bytes(b"audio")
    processing_service.release_audio(get_meeting(other.id))
    assert audio.exists()


def test_run_saves_segments_and_reports_stages(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    meeting = create_meeting("call.mp3", "hash-seg", "audio/call.mp3")
    stages = []
    segs = [{"start": 0.0, "end": 4.5, "text": "hello world, this is the start of the meeting"}]

    def fake_transcribe(path, on_stage=None, **kw):
        on_stage("compressing")
        on_stage("transcribing")
        return {"text": segs[0]["text"], "segments": segs, "duration": 4.5}

    monkeypatch.setattr(processing_service, "transcribe_file", fake_transcribe)
    monkeypatch.setattr(processing_service, "set_stage", lambda mid, stage: stages.append(stage))
    monkeypatch.setattr(processing_service, "summarize", lambda t: {
        "title": "T", "summary": "s", "key_decisions": [], "action_items": [], "open_questions": []})

    processing_service.run(meeting.id)

    result = get_meeting(meeting.id)
    assert result.segments == segs and result.audio_seconds == 4.5
    assert stages == ["compressing", "transcribing", "summarizing"]


def test_duplicate_upload_copies_segments_too(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    from backend.app.database.meetings_repo import save_segments

    leader = create_meeting("a.mp3", "dup", "audio/a.mp3")
    save_transcript(leader.id, "hello world, this is the start of the meeting")
    save_segments(leader.id, [{"start": 1.0, "end": 2.0, "text": "hi"}], 2.0)
    save_summary(leader.id, "s", [], [], title="T")
    update_status(leader.id, MeetingStatus.COMPLETED)

    follower = create_meeting("b.mp3", "dup", "audio/b.mp3")
    monkeypatch.setattr(processing_service, "transcribe_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reprocessed")))
    processing_service.run(follower.id)

    copied = get_meeting(follower.id)
    assert copied.status == MeetingStatus.COMPLETED
    assert copied.segments == [{"start": 1.0, "end": 2.0, "text": "hi"}] and copied.audio_seconds == 2.0
