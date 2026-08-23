from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile

from backend.app.core.logging import get_logger
from backend.app.database.meetings_repo import create_meeting, get_meeting, list_meetings, update_status
from backend.app.models.meeting import MeetingStatus
from backend.app.schemas.meeting import MeetingDetailOut, MeetingStatusOut, MeetingSummaryOut
from backend.app.services.processing_service import run as run_processing
from backend.app.services.storage_service import UploadValidationError, save_upload

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])
log = get_logger(__name__)


def _get_or_404(meeting_id: str):
    meeting = get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"No meeting found with id '{meeting_id}'.")
    return meeting


@router.post("", status_code=201)
def upload_meeting(background_tasks: BackgroundTasks, audio: UploadFile = File(...)) -> dict:
    try:
        filename, file_hash, audio_path = save_upload(audio)
    except UploadValidationError as exc:
        log.warning("upload rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    meeting = create_meeting(filename, file_hash, audio_path)
    update_status(meeting.id, MeetingStatus.QUEUED)
    background_tasks.add_task(run_processing, meeting.id)
    log.info("meeting queued id=%s file_hash=%s", meeting.id, file_hash)

    return {"id": meeting.id, "status": MeetingStatus.QUEUED}


@router.get("", response_model=list[MeetingSummaryOut])
def get_meetings() -> list:
    return list_meetings()


@router.get("/{meeting_id}/status", response_model=MeetingStatusOut)
def get_meeting_status(meeting_id: str):
    return _get_or_404(meeting_id)


@router.get("/{meeting_id}", response_model=MeetingDetailOut)
def get_meeting_detail(meeting_id: str):
    return _get_or_404(meeting_id)
