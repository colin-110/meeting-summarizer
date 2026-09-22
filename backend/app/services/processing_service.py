"""Orchestrates one meeting's pipeline: transcribe -> summarize -> persist.

Called from a FastAPI BackgroundTask, so it runs after the upload
response has already been sent — no broker, no separate worker
process, just a function call on a thread from Starlette's pool.
"""

import time

from backend.app.core.logging import get_logger
from backend.app.database.meetings_repo import (
    find_leader_by_hash,
    get_meeting,
    save_summary,
    save_transcript,
    update_status,
)
from backend.app.models.meeting import Meeting, MeetingStatus
from backend.app.services.summarization_service import SummarizationError, summarize
from backend.app.services.transcription_service import TranscriptionError, transcribe

log = get_logger(__name__)

# Whisper doesn't return an empty string for silence/no-speech audio — it
# hallucinates short boilerplate phrases instead (observed: " Thank you."
# for two seconds of true digital silence). A length floor catches that
# failure mode; a real meeting transcript is essentially never this short.
_MIN_TRANSCRIPT_CHARS = 20

_RESOLVED_STATUSES = (MeetingStatus.COMPLETED, MeetingStatus.FAILED)

# How long a duplicate upload waits on an identical upload that's already
# in flight before giving up and processing independently instead —
# generous enough for a normal meeting's transcribe+summarize pass, bounded
# so a leader whose process died (and hasn't yet been swept by
# fail_stuck_processing at the next restart) doesn't strand a follower
# forever.
_FOLLOW_POLL_SECONDS = 1.0
_FOLLOW_MAX_WAIT_SECONDS = 300


def _copy_result(meeting_id: str, source: Meeting) -> None:
    save_transcript(meeting_id, source.transcript)
    save_summary(
        meeting_id,
        source.summary,
        source.key_decisions,
        source.action_items,
        title=source.title,
        open_questions=source.open_questions,
    )


def _await_leader(leader_id: str) -> "Meeting | None":
    """Poll until the leader resolves to COMPLETED/FAILED, or the wait
    budget runs out. Returns the resolved meeting, or None on timeout."""
    deadline = time.monotonic() + _FOLLOW_MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        leader = get_meeting(leader_id)
        if leader is None or leader.status in _RESOLVED_STATUSES:
            return leader
        time.sleep(_FOLLOW_POLL_SECONDS)
    return None


def run(meeting_id: str) -> None:
    meeting = get_meeting(meeting_id)
    if meeting is None:
        log.error("processing requested for unknown meeting id=%s", meeting_id)
        return

    update_status(meeting_id, MeetingStatus.PROCESSING)
    log.info("processing started id=%s", meeting_id)

    leader = find_leader_by_hash(meeting.file_hash)
    if leader is not None and leader.id != meeting_id:
        resolved = leader if leader.status in _RESOLVED_STATUSES else _await_leader(leader.id)

        if resolved is not None and resolved.status == MeetingStatus.COMPLETED:
            _copy_result(meeting_id, resolved)
            update_status(meeting_id, MeetingStatus.COMPLETED)
            log.info("reused result from concurrent duplicate id=%s source=%s", meeting_id, resolved.id)
            return

        if resolved is not None and resolved.status == MeetingStatus.FAILED:
            update_status(
                meeting_id,
                MeetingStatus.FAILED,
                error_message=f"Duplicate of another upload that failed: {resolved.error_message}",
            )
            log.info("propagated failure from concurrent duplicate id=%s source=%s", meeting_id, resolved.id)
            return

        # Leader never resolved within the wait budget — its process likely
        # died mid-processing. Fall through and do the real work ourselves
        # rather than wait indefinitely for a restart to sweep it.
        log.warning(
            "gave up waiting on leader id=%s for meeting id=%s, processing independently",
            leader.id,
            meeting_id,
        )

    try:
        transcript = transcribe(meeting.audio_path)
        save_transcript(meeting_id, transcript)

        if len(transcript.strip()) < _MIN_TRANSCRIPT_CHARS:
            raise TranscriptionError(
                "Transcript is too short to summarize — the audio may not contain any speech."
            )

        result = summarize(transcript)
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
    except (TranscriptionError, SummarizationError) as exc:
        log.error("processing failed id=%s error=%s", meeting_id, exc)
        update_status(meeting_id, MeetingStatus.FAILED, error_message=str(exc))
    except Exception:
        log.exception("processing failed unexpectedly id=%s", meeting_id)
        update_status(meeting_id, MeetingStatus.FAILED, error_message="Unexpected error during processing.")
