"""ffmpeg preprocessing: shrink recordings and split them into chunks.

Speech models resample everything to 16 kHz mono anyway, so re-encoding to
that up front changes nothing about accuracy but cuts a recording to roughly
a tenth of its size (about 14 MB/hour at 32 kbps). That makes uploads to the
provider faster and lets hour-long meetings fit under its per-request cap.
Splitting into fixed-length chunks lets them be transcribed in parallel.
"""

import os
import re
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


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError("Audio conversion timed out.") from exc


def _probe_silences(path: Path) -> tuple[float, list[float]]:
    """Return (duration_seconds, midpoints_of_pauses) for a compressed file."""
    result = _run([
        ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(path),
        "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-",
    ])
    m = _DURATION_RE.search(result.stderr)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    starts = [float(x) for x in _SILENCE_START_RE.findall(result.stderr)]
    ends = [float(x) for x in _SILENCE_END_RE.findall(result.stderr)]
    return duration, [(max(a, 0) + b) / 2 for a, b in zip(starts, ends)]


def pick_cuts(duration: float, pauses: list[float], chunk_seconds: float) -> list[float]:
    """Cut points roughly every chunk_seconds, each snapped to the nearest
    pause in speech so a chunk boundary never lands mid-word. Falls back to
    the plain time grid where there's no pause nearby."""
    window = min(60.0, chunk_seconds * 0.3)
    cuts: list[float] = []
    target = chunk_seconds
    # Don't leave a tiny trailing chunk: stop once less than 25% of a chunk remains.
    while target < duration - chunk_seconds * 0.25:
        nearby = [p for p in pauses if abs(p - target) <= window and p > (cuts[-1] if cuts else 0) + 1]
        cut = min(nearby, key=lambda p: abs(p - target)) if nearby else target
        cuts.append(round(cut, 2))
        target = cut + chunk_seconds
    return cuts


def split_and_compress(audio_path: str, work_dir: Path) -> list[Chunk]:
    """Convert to 16 kHz mono MP3 and split into ~CHUNK_SECONDS pieces.

    Returns the chunks in order, each with its start offset in the original
    recording (used to turn chunk-relative timestamps into absolute ones).
    """
    full = work_dir / "full.mp3"
    result = _run([
        ffmpeg_exe(), "-nostdin", "-v", "error", "-y", "-i", audio_path,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(full),
    ])
    if result.returncode != 0 or not full.exists() or full.stat().st_size == 0:
        raise AudioProcessingError(
            "Could not read this audio file — it may be corrupted or not real audio."
        )

    chunk_seconds = config.CHUNK_SECONDS
    duration, pauses = _probe_silences(full)
    if duration <= chunk_seconds * 1.25:
        return [Chunk(path=full, offset_seconds=0.0)]

    cuts = pick_cuts(duration, pauses, chunk_seconds)
    pattern = work_dir / "chunk_%04d.mp3"
    result = _run([
        ffmpeg_exe(), "-nostdin", "-v", "error", "-y", "-i", str(full), "-c", "copy",
        "-f", "segment", "-segment_times", ",".join(str(c) for c in cuts),
        "-reset_timestamps", "1", str(pattern),
    ])
    paths = sorted(work_dir.glob("chunk_*.mp3"))
    if result.returncode != 0 or len(paths) != len(cuts) + 1:
        raise AudioProcessingError("Could not split this audio file.")
    full.unlink(missing_ok=True)
    return [Chunk(path=p, offset_seconds=o) for p, o in zip(paths, [0.0, *cuts])]


def make_work_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ms-chunks-"))


def cleanup(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)
