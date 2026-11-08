"""Groq Whisper transcription.

Provider-specific code stays isolated here — the rest of the app only
calls transcribe_file(audio_path); swapping ASR providers later means
rewriting this one file, nothing upstream of it.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from groq import APIConnectionError, Groq, InternalServerError, RateLimitError

from backend.app.core import config
from backend.app.services import audio_service
from backend.app.services.audio_service import AudioProcessingError
from backend.app.utils.errors import clean_provider_message
from backend.app.utils.retry import call_with_retry

_MODEL = "whisper-large-v3-turbo"
_RETRYABLE: tuple[type[BaseException], ...] = (APIConnectionError, RateLimitError, InternalServerError)

# Whisper's own thresholds for "this segment is probably not speech": it
# hallucinates plausible phrases ("Thank you.") over silence, but reports a
# high no-speech probability and low confidence while doing it.
_NO_SPEECH_PROB = 0.6
_LOW_LOGPROB = -1.0


class TranscriptionError(RuntimeError):
    """Raised when transcription fails. Message is safe to show the user."""


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _keep_segment(seg) -> bool:
    return not (
        (_field(seg, "no_speech_prob", 0) or 0) > _NO_SPEECH_PROB
        and (_field(seg, "avg_logprob", 0) or 0) < _LOW_LOGPROB
    )


def transcribe(audio_path: str, *, client: Optional[Groq] = None) -> dict:
    """Transcribe one audio file. Returns {"text": str, "segments": [{start, end, text}]}
    with timestamps relative to the start of this file."""
    client = client or Groq()
    path = Path(audio_path)

    def _call() -> dict:
        with path.open("rb") as f:
            result = client.audio.transcriptions.create(
                file=(path.name, f.read()),
                model=_MODEL,
                temperature=0,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        raw_segments = _field(result, "segments") or []
        segments = [
            {
                "start": round(float(_field(s, "start", 0)), 2),
                "end": round(float(_field(s, "end", 0)), 2),
                "text": (_field(s, "text", "") or "").strip(),
            }
            for s in raw_segments
            if _keep_segment(s) and (_field(s, "text", "") or "").strip()
        ]
        if raw_segments:
            text = " ".join(s["text"] for s in segments)
        else:
            text = (_field(result, "text", "") or "").strip()
        return {"text": text, "segments": segments}

    try:
        return call_with_retry(_call, retry_on=_RETRYABLE)
    except _RETRYABLE as exc:
        raise TranscriptionError(
            f"Transcription service unavailable after retries: {clean_provider_message(exc)}"
        ) from exc
    except Exception as exc:
        raise TranscriptionError(f"Transcription failed: {clean_provider_message(exc)}") from exc


def transcribe_file(
    audio_path: str,
    *,
    client: Optional[Groq] = None,
    on_stage: Optional[Callable[[str], None]] = None,
) -> dict:
    """Full pipeline for one recording: compress + split with ffmpeg, then
    transcribe the chunks in parallel and stitch them back together.

    Returns {"text", "segments" (absolute timestamps), "duration"}.
    """
    stage = on_stage or (lambda _: None)
    if not Path(audio_path).exists():
        raise TranscriptionError("The uploaded audio file is no longer available.")

    work_dir = audio_service.make_work_dir()
    try:
        stage("compressing")
        try:
            chunks = audio_service.split_and_compress(audio_path, work_dir)
        except AudioProcessingError as exc:
            raise TranscriptionError(str(exc)) from exc

        stage("transcribing")
        client = client or Groq()
        workers = max(1, min(config.TRANSCRIBE_PARALLELISM, len(chunks)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda c: transcribe(str(c.path), client=client), chunks))
    finally:
        audio_service.cleanup(work_dir)

    segments = []
    for chunk, result in zip(chunks, results):
        for seg in result["segments"]:
            segments.append(
                {
                    "start": round(seg["start"] + chunk.offset_seconds, 2),
                    "end": round(seg["end"] + chunk.offset_seconds, 2),
                    "text": seg["text"],
                }
            )
    # Providers that return no segment data still give us text; keep it.
    text = " ".join(s["text"] for s in segments) if segments else " ".join(
        r["text"] for r in results if r["text"]
    )
    duration = max((s["end"] for s in segments), default=0.0)
    return {"text": text.strip(), "segments": segments, "duration": duration}
