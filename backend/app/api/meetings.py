import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse

from backend.app.core.logging import get_logger
from backend.app.core import config
from backend.app.database.meetings_repo import (
    add_recording_chunk,
    create_meeting,
    delete_meeting,
    get_meeting,
    list_meetings,
    list_recording_chunks,
    queue_depth,
    update_status,
)
from backend.app.models.meeting import MeetingStatus
from backend.app.schemas.meeting import (
    AskIn,
    AskOut,
    ChunkOut,
    MeetingDetailOut,
    MeetingStatusOut,
    MeetingSummaryOut,
)
from backend.app.services import processing_service, qa_service
from backend.app.services.storage_service import UploadValidationError, save_upload
from backend.app.services.transcription_service import TranscriptionError
from backend.app.utils.errors import ProviderBusyError
from backend.app.utils.rate_limit import RateLimitExceeded, check_rate_limit

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])
recordings_router = APIRouter(prefix="/api/v1/recordings", tags=["recordings"])
ask_router = APIRouter(prefix="/api/v1", tags=["ask"])
log = get_logger(__name__)

# Generous enough for genuine testing, tight enough that one client can't
# quietly burn through the shared Groq free-tier quota on a public demo.
_UPLOAD_RATE_LIMIT = 10
_UPLOAD_RATE_WINDOW_SECONDS = 600
_ASK_RATE_LIMIT = 30
_ASK_RATE_WINDOW_SECONDS = 600
_CHUNK_RATE_LIMIT = 240
_CHUNK_RATE_WINDOW_SECONDS = 600

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def owner_id(x_owner_token: Optional[str] = Header(default=None)) -> str:
    """Identify the caller by a random token the browser generates once and
    keeps in localStorage. Meetings are only visible to the token that made
    them — private-by-default without needing accounts. Only a hash of the
    token is stored, so the database never holds a usable credential."""
    if not x_owner_token or not _TOKEN_RE.match(x_owner_token):
        raise HTTPException(status_code=400, detail="Missing or invalid X-Owner-Token header.")
    return hashlib.sha256(x_owner_token.encode()).hexdigest()


def _get_or_404(meeting_id: str, owner: str):
    meeting = get_meeting(meeting_id)
    # 404 (not 403) for someone else's meeting, so ids can't be probed.
    if meeting is None or meeting.owner_id != owner:
        raise HTTPException(status_code=404, detail=f"No meeting found with id '{meeting_id}'.")
    return meeting


def _check_capacity() -> None:
    """Backpressure: when the queue is already long, refuse new work with a
    clear 'come back later' instead of accepting jobs that would wait for hours."""
    if queue_depth() >= config.MAX_QUEUE_DEPTH:
        raise HTTPException(
            status_code=503,
            detail="The service is busy processing other meetings. Please try again in a few minutes.",
            headers={"Retry-After": "120"},
        )


def _limit(key: str, max_requests: int, window_seconds: int, message: str) -> None:
    try:
        check_rate_limit(key, max_requests=max_requests, window_seconds=window_seconds)
    except RateLimitExceeded as exc:
        log.warning("rate-limited key=%s", key.split(":")[0])
        raise HTTPException(status_code=429, detail=message or str(exc)) from exc


def _client_ip(request: Request) -> str:
    # Trusts X-Forwarded-For because this app is only ever deployed behind
    # a platform-managed proxy (e.g. Render) that sets it itself — it would
    # be spoofable and unsafe to trust if the app were reachable directly.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("", status_code=201)
def upload_meeting(request: Request, audio: UploadFile = File(...), owner: str = Depends(owner_id)) -> dict:
    try:
        check_rate_limit(
            _client_ip(request),
            max_requests=_UPLOAD_RATE_LIMIT,
            window_seconds=_UPLOAD_RATE_WINDOW_SECONDS,
        )
    except RateLimitExceeded as exc:
        log.warning("upload rate-limited: %s", exc)
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    _check_capacity()

    try:
        filename, file_hash, audio_path = save_upload(audio)
    except UploadValidationError as exc:
        log.warning("upload rejected: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    meeting = create_meeting(filename, file_hash, audio_path, owner_id=owner)
    update_status(meeting.id, MeetingStatus.QUEUED)
    request.app.state.workers.notify()
    log.info("meeting queued id=%s file_hash=%s", meeting.id, file_hash)

    return {"id": meeting.id, "status": MeetingStatus.QUEUED}


@router.get("", response_model=list[MeetingSummaryOut])
def get_meetings(
    owner: str = Depends(owner_id),
    q: Optional[str] = Query(default=None, max_length=200, description="Full-text search"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list:
    return list_meetings(owner, limit=limit, offset=offset, query=q)


@router.get("/{meeting_id}/status", response_model=MeetingStatusOut)
def get_meeting_status(meeting_id: str, owner: str = Depends(owner_id)):
    return _get_or_404(meeting_id, owner)


@router.get("/{meeting_id}", response_model=MeetingDetailOut)
def get_meeting_detail(meeting_id: str, owner: str = Depends(owner_id)):
    return _get_or_404(meeting_id, owner)


@router.post("/{meeting_id}/ask", response_model=AskOut)
def ask_meeting(meeting_id: str, body: AskIn, owner: str = Depends(owner_id)) -> dict:
    meeting = _get_or_404(meeting_id, owner)
    if meeting.status != MeetingStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="This meeting hasn't finished processing yet.")
    _limit(f"ask:{owner}", _ASK_RATE_LIMIT, _ASK_RATE_WINDOW_SECONDS, "Too many questions — try again shortly.")
    try:
        return qa_service.ask(meeting, body.question)
    except qa_service.QAError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/{meeting_id}/export", response_class=PlainTextResponse)
def export_meeting(meeting_id: str, owner: str = Depends(owner_id)) -> Response:
    meeting = _get_or_404(meeting_id, owner)
    if meeting.status != MeetingStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="This meeting hasn't finished processing yet.")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", meeting.title or "meeting").strip("-") or "meeting"
    return PlainTextResponse(
        qa_service.to_markdown(meeting),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.md"'},
    )


@router.delete("/{meeting_id}", status_code=204)
def remove_meeting(meeting_id: str, owner: str = Depends(owner_id)) -> Response:
    meeting = _get_or_404(meeting_id, owner)
    if meeting.status == MeetingStatus.PROCESSING:
        raise HTTPException(status_code=409, detail="Can't delete a meeting while it is processing.")
    delete_meeting(meeting_id)
    processing_service.release_audio(meeting, force=True)
    return Response(status_code=204)


# ---- ask across all meetings ----

@ask_router.post("/ask", response_model=AskOut)
def ask_all_meetings(body: AskIn, owner: str = Depends(owner_id)) -> dict:
    _limit(f"ask:{owner}", _ASK_RATE_LIMIT, _ASK_RATE_WINDOW_SECONDS, "Too many questions — try again shortly.")
    try:
        return qa_service.ask_all(owner, body.question)
    except qa_service.QAError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---- live recording: pieces are transcribed while the meeting is still going ----

def _recording_or_404(meeting_id: str, owner: str):
    meeting = _get_or_404(meeting_id, owner)
    if meeting.status != MeetingStatus.RECORDING:
        raise HTTPException(status_code=409, detail="This recording has already been finished.")
    return meeting


@recordings_router.post("", status_code=201)
def start_recording(request: Request, owner: str = Depends(owner_id)) -> dict:
    _limit(_client_ip(request), _UPLOAD_RATE_LIMIT, _UPLOAD_RATE_WINDOW_SECONDS,
           "Too many recordings from this address — try again shortly.")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    meeting = create_meeting(
        f"Recording {stamp}", f"rec-{uuid.uuid4().hex}", "", owner_id=owner,
        source="recording", status=MeetingStatus.RECORDING,
    )
    return {"id": meeting.id, "status": MeetingStatus.RECORDING}


@recordings_router.post("/{meeting_id}/chunks", response_model=ChunkOut)
def upload_recording_chunk(
    meeting_id: str,
    seq: int = Form(..., ge=0, le=10_000),
    offset: float = Form(..., ge=0),
    audio: UploadFile = File(...),
    owner: str = Depends(owner_id),
) -> dict:
    """One piece of a live recording. Transcribed right away so that when the
    meeting ends, only the summary is left to do. If transcription can't run
    now (provider busy), the piece is kept and the worker transcribes it later."""
    _recording_or_404(meeting_id, owner)
    _limit(f"chunk:{owner}", _CHUNK_RATE_LIMIT, _CHUNK_RATE_WINDOW_SECONDS, "Too many recording pieces — slow down.")
    try:
        _, file_hash, audio_path = save_upload(audio)
    except UploadValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not add_recording_chunk(meeting_id, seq, offset, audio_path, file_hash):
        done = any(c["seq"] == seq and c["status"] == "done" for c in list_recording_chunks(meeting_id))
        return {"seq": seq, "transcribed": done}  # client retried a piece we already have

    chunk = {"seq": seq, "offset_seconds": offset, "audio_path": audio_path, "file_hash": file_hash}
    try:
        processing_service.transcribe_recording_chunk(meeting_id, chunk)
        return {"seq": seq, "transcribed": True}
    except (TranscriptionError, ProviderBusyError) as exc:
        log.warning("live chunk deferred to worker id=%s seq=%d: %s", meeting_id, seq, exc)
        return {"seq": seq, "transcribed": False}


@recordings_router.post("/{meeting_id}/finish")
def finish_recording(meeting_id: str, request: Request, owner: str = Depends(owner_id)) -> dict:
    _recording_or_404(meeting_id, owner)
    if not list_recording_chunks(meeting_id):
        raise HTTPException(status_code=422, detail="Nothing was recorded.")
    _check_capacity()
    update_status(meeting_id, MeetingStatus.QUEUED)
    request.app.state.workers.notify()
    return {"id": meeting_id, "status": MeetingStatus.QUEUED}
