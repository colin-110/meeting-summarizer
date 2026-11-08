"""ffmpeg preprocessing: shrink recordings, skip long silences, split into chunks.

Speech models resample everything to 16 kHz mono anyway, so re-encoding to
that up front changes nothing about accuracy but cuts a recording to roughly
a tenth of its size (about 14 MB/hour at 32 kbps).

Pauses are found *while* encoding (silencedetect runs in the same pass), so
detection costs no extra decode. Long pauses are then shortened before the
audio is sent: the provider meters audio by the second, and meetings are full
of silence that produces nothing but hallucinated "Thank you."s. A Timeline
records exactly what was cut, so every transcript timestamp is mapped back to
the original recording.
"""

import bisect
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.core import config
from backend.app.core.logging import get_logger

log = get_logger(__name__)


class AudioProcessingError(RuntimeError):
    """Raised when audio can't be decoded or converted. Message is user-facing."""


@dataclass
class Chunk:
    path: Path
    offset_seconds: float  # where this chunk starts in the audio that is sent


@dataclass
class Timeline:
    """Maps time in the (silence-shortened) audio that is sent back to time in
    the original recording. spans: kept pieces as (sent_start, original_start, length)."""

    spans: list
    original_seconds: float

    @classmethod
    def identity(cls, duration: float) -> "Timeline":
        return cls(spans=[(0.0, 0.0, duration)], original_seconds=duration)

    @property
    def sent_seconds(self) -> float:
        return sum(length for _, _, length in self.spans)

    def to_original(self, t: float, *, is_end: bool = False) -> float:
        starts = [s for s, _, _ in self.spans]
        # An end time exactly on a join belongs to the span before the cut.
        i = (bisect.bisect_left(starts, t) if is_end else bisect.bisect_right(starts, t)) - 1
        sent_start, orig_start, _ = self.spans[max(i, 0)]
        return round(orig_start + max(t - sent_start, 0.0), 2)

    def to_sent(self, t: float) -> float:
        """Original time -> sent time; a time inside a removed pause maps to the join."""
        starts = [o for _, o, _ in self.spans]
        i = max(bisect.bisect_right(starts, t) - 1, 0)
        sent_start, orig_start, length = self.spans[i]
        return sent_start + min(max(t - orig_start, 0.0), length)


@dataclass
class PreparedAudio:
    chunks: list
    timeline: Timeline
    removed: list = field(default_factory=list)


def ffmpeg_exe() -> str:
    """FFMPEG_PATH, else the pinned static build bundled with imageio-ffmpeg, else
    the system ffmpeg. The pinned build comes first so behaviour is identical on
    every machine (Ubuntu's ffmpeg 8.0.1, for one, ignores the aselect filter)."""
    explicit = os.environ.get("FFMPEG_PATH")
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    system = shutil.which("ffmpeg")
    if system:
        return system
    raise AudioProcessingError("ffmpeg is not installed on the server.")


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_FILTER = "silencedetect=noise=-35dB:d=0.4"
_ENCODE = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k"]
# Silence is removed in 10 ms frames (160 samples at 16 kHz), so cuts land on
# a known grid and the Timeline matches the output to the sample.
_GRID = 0.01
# Not worth a re-encode unless at least this much silence goes.
_MIN_SAVING_SECONDS = 3.0
# Removing more pauses than this makes an unwieldy filter; keep the longest.
_MAX_CUTS = 400


def _run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError("Audio conversion timed out.") from exc


def _hms(m: re.Match) -> float:
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def _output_seconds(stderr: str) -> float:
    """Length of what ffmpeg just wrote, from its final progress line."""
    times = list(_TIME_RE.finditer(stderr))
    return _hms(times[-1]) if times else 0.0


def _parse_silences(stderr: str, duration: float) -> list[tuple[float, float]]:
    starts = [max(float(x), 0.0) for x in _SILENCE_START_RE.findall(stderr)]
    ends = [float(x) for x in _SILENCE_END_RE.findall(stderr)]
    if len(starts) == len(ends) + 1:
        ends.append(duration)  # recording ends in silence
    return [(a, b) for a, b in zip(starts, ends) if b > a]


def _probe(path: Path) -> tuple[float, list[tuple[float, float]]]:
    """Separate detection pass; only used if the encode pass reported nothing."""
    result = _run([ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(path), "-af", _SILENCE_FILTER, "-f", "null", "-"])
    m = _DURATION_RE.search(result.stderr)
    duration = _hms(m) if m else _output_seconds(result.stderr)
    return duration, _parse_silences(result.stderr, duration)


def _compress(audio_path: str, out: Path) -> tuple[float, list[tuple[float, float]]]:
    """Re-encode to 16 kHz mono MP3 and detect pauses in the same decode pass."""
    result = _run([ffmpeg_exe(), "-nostdin", "-hide_banner", "-nostats", "-y", "-i", audio_path,
                   "-af", _SILENCE_FILTER, *_ENCODE, str(out)])
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise AudioProcessingError("Could not read this audio file — it may be corrupted or not real audio.")
    duration = _output_seconds(result.stderr)
    if duration <= 0:
        return _probe(out)
    return duration, _parse_silences(result.stderr, duration)


def plan_silence_cuts(duration: float, silences: list, *, min_gap: float, keep: float) -> list[tuple[float, float]]:
    """Which stretches to remove: each pause longer than min_gap is shortened to
    `keep` seconds (half on each side), so sentences stay separated. Returns
    sorted (start, end) on the 10 ms grid; empty when the saving isn't worth it."""
    if min_gap <= 0:
        return []
    pad = keep / 2
    cuts = []
    for a, b in silences:
        if b - a < min_gap:
            continue
        start = round(round((a + pad) / _GRID) * _GRID, 2)
        end = round(round((min(b, duration) - pad) / _GRID) * _GRID, 2)
        if end - start >= _GRID:
            cuts.append((start, end))
    if len(cuts) > _MAX_CUTS:
        cuts = sorted(sorted(cuts, key=lambda c: c[1] - c[0], reverse=True)[:_MAX_CUTS])
    if sum(b - a for a, b in cuts) < _MIN_SAVING_SECONDS:
        return []
    return cuts


def timeline_for(duration: float, cuts: list) -> Timeline:
    spans, sent, cursor = [], 0.0, 0.0
    for a, b in cuts:
        if a > cursor:
            spans.append((round(sent, 2), round(cursor, 2), round(a - cursor, 2)))
            sent += a - cursor
        cursor = b
    if duration > cursor:
        spans.append((round(sent, 2), round(cursor, 2), round(duration - cursor, 2)))
    return Timeline(spans=spans or [(0.0, 0.0, 0.0)], original_seconds=duration)


def _remove_silence(src: Path, out: Path, cuts: list, expected: float) -> bool:
    """Write src minus the cut stretches. Returns False (caller keeps src) if
    ffmpeg fails or the result isn't the length the Timeline predicts — a
    wrong timeline would silently shift every timestamp."""
    # Frames start at multiples of 10 ms; testing at half-grid offsets avoids
    # any floating-point tie at a boundary.
    expr = "+".join(f"between(t,{a - _GRID / 2:.3f},{b - _GRID / 2:.3f})" for a, b in cuts)
    graph = f"asetpts=N/SR/TB,asetnsamples=n=160:p=0,aselect='not({expr})',asetpts=N/SR/TB"
    result = _run([ffmpeg_exe(), "-nostdin", "-hide_banner", "-nostats", "-y", "-i", str(src),
                   "-af", graph, *_ENCODE, str(out)])
    got = _output_seconds(result.stderr)
    if result.returncode != 0 or abs(got - expected) > 0.3 + 0.002 * len(cuts):
        log.warning("silence removal skipped: expected %.2fs of audio, ffmpeg produced %.2fs", expected, got)
        out.unlink(missing_ok=True)
        return False
    return True


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


def split_and_compress(audio_path: str, work_dir: Path) -> PreparedAudio:
    """Compress, shorten long pauses, and split into ~CHUNK_SECONDS chunks.

    Chunk offsets are positions in the audio that is sent; prepared.timeline
    maps those back to the original recording.
    """
    full = work_dir / "full.mp3"
    duration, silences = _compress(audio_path, full)

    source, timeline = full, Timeline.identity(duration)
    removed = plan_silence_cuts(duration, silences, min_gap=config.SKIP_SILENCE_SECONDS,
                                keep=config.KEEP_SILENCE_SECONDS)
    if removed:
        planned = timeline_for(duration, removed)
        trimmed = work_dir / "trimmed.mp3"
        if _remove_silence(full, trimmed, removed, planned.sent_seconds):
            full.unlink(missing_ok=True)
            source, timeline = trimmed, planned
        else:
            removed = []

    sent = timeline.sent_seconds
    chunk_seconds = config.CHUNK_SECONDS
    if sent <= chunk_seconds * 1.25:
        return PreparedAudio([Chunk(path=source, offset_seconds=0.0)], timeline, removed)

    pauses = [timeline.to_sent((a + b) / 2) for a, b in silences]
    cuts = pick_cuts(sent, pauses, chunk_seconds)
    pattern = work_dir / "chunk_%04d.mp3"
    result = _run([
        ffmpeg_exe(), "-nostdin", "-v", "error", "-y", "-i", str(source), "-c", "copy",
        "-f", "segment", "-segment_times", ",".join(str(c) for c in cuts),
        "-reset_timestamps", "1", str(pattern),
    ])
    paths = sorted(work_dir.glob("chunk_*.mp3"))
    if result.returncode != 0 or len(paths) != len(cuts) + 1:
        raise AudioProcessingError("Could not split this audio file.")
    source.unlink(missing_ok=True)
    chunks = [Chunk(path=p, offset_seconds=o) for p, o in zip(paths, [0.0, *cuts])]
    return PreparedAudio(chunks, timeline, removed)


def make_work_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="ms-chunks-"))


def cleanup(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)
