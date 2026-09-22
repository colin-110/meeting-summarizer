from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile

from backend.app.core.logging import get_logger
from backend.app.database.meetings_repo import create_meeting, get_meeting, list_meetings, update_status
from backend.app.models.meeting import MeetingStatus
from backend.app.schemas.meeting import MeetingDetailOut, MeetingStatusOut, MeetingSummaryOut
from backend.app.services.processing_service import run as run_processing
from backend.app.services.storage_service import UploadValidationError, save_upload
from backend.app.utils.rate_limit import RateLimitExceeded, check_rate_limit

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])
log = get_logger(__name__)

# Generous enough for genuine testing, tight enough that one client can't
# quietly burn through the shared Groq free-tier quota on a public demo.
_UPLOAD_RATE_LIMIT = 10
_UPLOAD_RATE_WINDOW_SECONDS = 600


def _get_or_404(meeting_id: str):
    meeting = get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"No meeting found with id '{meeting_id}'.")
    return meeting


def _client_ip(request: Request) -> str:
    # Trusts X-Forwarded-For because this app is only ever deployed behind
    # a platform-managed proxy (e.g. Render) that sets it itself — it would
    # be spoofable and unsafe to trust if the app were reachable directly.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("", status_code=201)
def upload_meeting(request: Request, background_tasks: BackgroundTasks, audio: UploadFile = File(...)) -> dict:
    try:
        check_rate_limit(
            _client_ip(request),
            max_requests=_UPLOAD_RATE_LIMIT,
            window_seconds=_UPLOAD_RATE_WINDOW_SECONDS,
        )
    except RateLimitExceeded as exc:
        log.warning("upload rate-limited: %s", exc)
        raise HTTPException(status_code=429, detail=str(exc)) from exc

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
