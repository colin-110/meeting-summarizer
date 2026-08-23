"""Orchestrates one meeting's pipeline: transcribe -> summarize -> persist.

Called from a FastAPI BackgroundTask, so it runs after the upload
response has already been sent — no broker, no separate worker
process, just a function call on a thread from Starlette's pool.
"""

from backend.app.core.logging import get_logger
from backend.app.database.meetings_repo import (
    find_completed_by_hash,
    get_meeting,
    save_summary,
    save_transcript,
    update_status,
)
from backend.app.models.meeting import MeetingStatus
from backend.app.services.summarization_service import SummarizationError, summarize
from backend.app.services.transcription_service import TranscriptionError, transcribe

log = get_logger(__name__)

# Whisper doesn't return an empty string for silence/no-speech audio — it
# hallucinates short boilerplate phrases instead (observed: " Thank you."
# for two seconds of true digital silence). A length floor catches that
# failure mode; a real meeting transcript is essentially never this short.
_MIN_TRANSCRIPT_CHARS = 20


def run(meeting_id: str) -> None:
    meeting = get_meeting(meeting_id)
    if meeting is None:
        log.error("processing requested for unknown meeting id=%s", meeting_id)
        return

    update_status(meeting_id, MeetingStatus.PROCESSING)
    log.info("processing started id=%s", meeting_id)

    try:
        cached = find_completed_by_hash(meeting.file_hash)
        reuse = cached is not None and cached.id != meeting_id

        transcript = cached.transcript if reuse else transcribe(meeting.audio_path)
        save_transcript(meeting_id, transcript)

        if not reuse and len(transcript.strip()) < _MIN_TRANSCRIPT_CHARS:
            raise TranscriptionError(
                "Transcript is too short to summarize — the audio may not contain any speech."
            )

        if reuse:
            summary, key_decisions, action_items = cached.summary, cached.key_decisions, cached.action_items
            log.info("reused cached result id=%s source=%s", meeting_id, cached.id)
        else:
            result = summarize(transcript)
            summary = result["summary"]
            key_decisions = result["key_decisions"]
            action_items = result["action_items"]
        save_summary(meeting_id, summary, key_decisions, action_items)

        update_status(meeting_id, MeetingStatus.COMPLETED)
        log.info("processing completed id=%s", meeting_id)
    except (TranscriptionError, SummarizationError) as exc:
        log.error("processing failed id=%s error=%s", meeting_id, exc)
        update_status(meeting_id, MeetingStatus.FAILED, error_message=str(exc))
    except Exception:
        log.exception("processing failed unexpectedly id=%s", meeting_id)
        update_status(meeting_id, MeetingStatus.FAILED, error_message="Unexpected error during processing.")
