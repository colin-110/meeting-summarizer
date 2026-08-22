from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile

from backend.app.core.logging import get_logger
from backend.app.database.meetings_repo import create_meeting, update_status
from backend.app.models.meeting import MeetingStatus
from backend.app.services.processing_service import run as run_processing
from backend.app.services.storage_service import UploadValidationError, save_upload

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])
log = get_logger(__name__)


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
