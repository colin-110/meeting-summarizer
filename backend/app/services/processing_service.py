"""Orchestrates one meeting's pipeline: transcribe -> summarize -> persist.

Called by the worker threads (services/worker.py), which lease jobs from the
SQLite-backed queue. Every stage checkpoints its output, so a retry (after a
crash, a lease expiry, or a provider rate-limit pause) resumes where it left
off instead of redoing — and re-paying for — finished work.
"""

from datetime import datetime, timezone
from pathlib import Path

from backend.app.core import config
from backend.app.core.logging import get_logger
from backend.app.database.meetings_repo import (
    audio_in_use,
    chunk_audio_in_use,
    defer,
    find_leader_by_hash,
    get_meeting,
    list_recording_chunks,
    save_chapters,
    save_chunk_segments,
    save_notes_cache,
    save_segments,
    save_summary,
    save_transcript,
    set_stage,
    update_status,
)
from backend.app.models.meeting import Meeting, MeetingStatus
from backend.app.services.summarization_service import SummarizationError, summarize, summarize_long
from backend.app.services.transcription_service import TranscriptionError, transcribe_file
from backend.app.utils.errors import ProviderBusyError

log = get_logger(__name__)

# Whisper doesn't return an empty string for silence/no-speech audio — it
# hallucinates short boilerplate phrases instead (observed: " Thank you."
# for two seconds of true digital silence). A length floor catches that
# failure mode; a real meeting transcript is essentially never this short.
_MIN_TRANSCRIPT_CHARS = 20

_RESOLVED_STATUSES = (MeetingStatus.COMPLETED, MeetingStatus.FAILED)

# A duplicate upload of audio that's already being processed goes back on
# the queue for this long and checks again, rather than holding a worker
# thread while it waits. After _FOLLOW_MAX_WAIT_SECONDS it stops waiting and
# processes the audio itself (the original's worker probably died).
_FOLLOW_RECHECK_SECONDS = 10
_FOLLOW_MAX_WAIT_SECONDS = 300


def _age_seconds(meeting: Meeting) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(meeting.created_at)).total_seconds()


def release_audio(meeting: Meeting, *, force: bool = False) -> None:
    """Delete the uploaded audio once it's no longer needed: less storage,
    and recordings of private meetings don't linger on disk. Kept while the
    job may still be retried, or while another upload of the same audio
    still needs it; `force` overrides RETAIN_AUDIO (used when the user
    deletes a meeting)."""
    if not meeting.audio_path:
        return
    if not force:
        current = get_meeting(meeting.id)
        if config.RETAIN_AUDIO or (current is not None and current.status not in _RESOLVED_STATUSES):
            return
    if audio_in_use(meeting.file_hash, meeting.id):
        return
    Path(meeting.audio_path).unlink(missing_ok=True)


def _copy_result(meeting_id: str, source: Meeting) -> None:
    save_transcript(meeting_id, source.transcript)
    save_segments(meeting_id, source.segments, source.audio_seconds)
    save_chapters(meeting_id, source.chapters)
    save_summary(
        meeting_id,
        source.summary,
        source.key_decisions,
        source.action_items,
        title=source.title,
        open_questions=source.open_questions,
    )


def transcribe_recording_chunk(meeting_id: str, chunk: dict) -> list:
    """Transcribe one live-recording piece, store its segments (shifted to the
    piece's position in the recording) and delete its audio."""
    result = transcribe_file(chunk["audio_path"])
    segments = [
        {"start": round(s["start"] + chunk["offset_seconds"], 2),
         "end": round(s["end"] + chunk["offset_seconds"], 2),
         "text": s["text"]}
        for s in result["segments"]
    ]
    save_chunk_segments(meeting_id, chunk["seq"], segments)
    if not config.RETAIN_AUDIO and not chunk_audio_in_use(chunk["file_hash"]):
        Path(chunk["audio_path"]).unlink(missing_ok=True)
    return segments


def _assemble_recording(meeting: Meeting) -> None:
    """A live recording was mostly transcribed piece by piece while it was
    being recorded; transcribe any pieces that didn't make it, then stitch."""
    chunks = list_recording_chunks(meeting.id)
    if not chunks:
        raise TranscriptionError("The recording has no audio.")
    set_stage(meeting.id, "transcribing")
    segments: list = []
    for chunk in chunks:
        segments.extend(chunk["segments"] if chunk["status"] == "done" else transcribe_recording_chunk(meeting.id, chunk))
    segments.sort(key=lambda s: s["start"])
    text = " ".join(s["text"] for s in segments).strip()
    save_transcript(meeting.id, text)
    save_segments(meeting.id, segments, max((s["end"] for s in segments), default=0.0))


def run(meeting_id: str) -> None:
    meeting = get_meeting(meeting_id)
    if meeting is None:
        log.error("processing requested for unknown meeting id=%s", meeting_id)
        return
    try:
        _run(meeting)
    finally:
        release_audio(meeting)


def _follow_duplicate(meeting: Meeting) -> bool:
    """If identical audio was uploaded earlier, reuse its result. Returns True
    when this job has been dealt with (completed, failed, or deferred)."""
    leader = find_leader_by_hash(meeting.file_hash)
    if leader is None or leader.id == meeting.id:
        return False
    if leader.status == MeetingStatus.COMPLETED:
        _copy_result(meeting.id, leader)
        update_status(meeting.id, MeetingStatus.COMPLETED)
        log.info("reused result from duplicate id=%s source=%s", meeting.id, leader.id)
        return True
    if leader.status == MeetingStatus.FAILED:
        update_status(
            meeting.id,
            MeetingStatus.FAILED,
            error_message=f"Duplicate of another upload that failed: {leader.error_message}",
        )
        return True
    if _age_seconds(meeting) < _FOLLOW_MAX_WAIT_SECONDS:
        defer(meeting.id, _FOLLOW_RECHECK_SECONDS, "waiting for an identical upload")
        return True
    log.warning("gave up waiting on leader id=%s for id=%s, processing independently", leader.id, meeting.id)
    return False


def _run(meeting: Meeting) -> None:
    meeting_id = meeting.id
    update_status(meeting_id, MeetingStatus.PROCESSING)
    log.info("processing started id=%s attempt=%d", meeting_id, meeting.attempts)

    try:
        if meeting.source != "recording" and _follow_duplicate(meeting):
            return

        # Checkpoint: a retry never re-transcribes audio it already transcribed.
        if not (meeting.transcript and meeting.transcript.strip()):
            if meeting.source == "recording":
                _assemble_recording(meeting)
            else:
                transcription = transcribe_file(
                    meeting.audio_path, on_stage=lambda stage: set_stage(meeting_id, stage)
                )
                save_transcript(meeting_id, transcription["text"])
                save_segments(meeting_id, transcription["segments"], transcription["duration"])
            meeting = get_meeting(meeting_id)

        transcript = meeting.transcript or ""
        if len(transcript.strip()) < _MIN_TRANSCRIPT_CHARS:
            raise TranscriptionError(
                "Transcript is too short to summarize — the audio may not contain any speech."
            )

        set_stage(meeting_id, "summarizing")
        if len(transcript) <= config.SINGLE_PASS_CHARS:
            result = summarize(transcript)
        else:
            result = summarize_long(
                transcript,
                meeting.segments,
                cache=meeting.notes_cache,
                on_progress=lambda cache: save_notes_cache(meeting_id, cache),
                on_stage=lambda stage: set_stage(meeting_id, stage),
            )
        save_chapters(meeting_id, result.get("chapters", []))
        save_summary(
            meeting_id,
            result["summary"],
            result["key_decisions"],
            result["action_items"],
            title=result.get("title"),
            open_questions=result.get("open_questions", []),
        )

        update_status(meeting_id, MeetingStatus.COMPLETED)
        log.info("processing completed id=%s", meeting_id)
    except ProviderBusyError as exc:
        if _age_seconds(meeting) > config.MAX_JOB_AGE_HOURS * 3600:
            update_status(meeting_id, MeetingStatus.FAILED,
                          error_message="The AI provider stayed at capacity for too long. Please try again later.")
            return
        delay = max(exc.retry_after, config.DEFER_SECONDS)
        log.warning("provider busy; deferring id=%s for %.0fs: %s", meeting_id, delay, exc)
        defer(meeting_id, delay, "waiting for AI capacity")
    except (TranscriptionError, SummarizationError) as exc:
        log.error("processing failed id=%s error=%s", meeting_id, exc)
        update_status(meeting_id, MeetingStatus.FAILED, error_message=str(exc))
    except Exception:
        log.exception("processing failed unexpectedly id=%s", meeting_id)
        update_status(meeting_id, MeetingStatus.FAILED, error_message="Unexpected error during processing.")
