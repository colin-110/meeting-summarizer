"""ffmpeg preprocessing: shrink recordings and split them into chunks.

Speech models resample everything to 16 kHz mono anyway, so re-encoding to
that up front changes nothing about accuracy but cuts a recording to roughly
a tenth of its size (about 14 MB/hour at 32 kbps). That makes uploads to the
provider faster and lets hour-long meetings fit under its per-request cap.
Splitting into fixed-length chunks lets them be transcribed in parallel.
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backend.app.core import config


class AudioProcessingError(RuntimeError):
    """Raised when audio can't be decoded or converted. Message is user-facing."""


@dataclass
class Chunk:
    path: Path
    offset_seconds: float


def ffmpeg_exe() -> str:
    """System ffmpeg if present, otherwise the static binary bundled with imageio-ffmpeg."""
    explicit = os.environ.get("FFMPEG_PATH")
    if explicit:
        return explicit
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise AudioProcessingError("ffmpeg is not installed on the server.") from exc


def split_and_compress(audio_path: str, work_dir: Path) -> list[Chunk]:
    """Convert to 16 kHz mono MP3 and split into CHUNK_SECONDS pieces.

    Returns the chunks in order, each with its start offset in the original
    recording (used to turn chunk-relative timestamps into absolute ones).
    """
    pattern = work_dir / "chunk_%04d.mp3"
    cmd = [
        ffmpeg_exe(), "-nostdin", "-v", "error", "-y",
        "-i", audio_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(config.CHUNK_SECONDS),
        "-reset_timestamps", "1",
        str(pattern),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError("Audio conversion timed out.") from exc

    paths = sorted(work_dir.glob("chunk_*.mp3"))
    if result.returncode != 0 or not paths:
        raise AudioProcessingError(
            "Could not read this audio file — it may be corrupted or not real audio."
        )
    return [Chunk(path=p, offset_seconds=i * config.CHUNK_SECONDS) for i, p in enumerate(paths)]


def make_work_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ms-chunks-"))


def cleanup(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)
