import shutil
import subprocess

import pytest

from backend.app.core import config
from backend.app.services import audio_service
from backend.app.services.transcription_service import TranscriptionError, transcribe, transcribe_file


@pytest.fixture
def tone(tmp_path):
    """A real 25-second WAV, generated with ffmpeg."""
    path = tmp_path / "tone.wav"
    subprocess.run(
        [audio_service.ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=25",
         "-ar", "44100", "-ac", "2", str(path)],
        check=True,
    )
    return path


class _FakeClient:
    """Returns one 5-second segment per chunk, labelled with the chunk file name."""

    def __init__(self):
        self.calls = []
        outer = self

        class _T:
            @staticmethod
            def create(**kwargs):
                name = kwargs["file"][0]
                outer.calls.append(name)
                return type("R", (), {"text": name, "segments": [
                    {"start": 0.0, "end": 5.0, "text": f"said in {name}", "no_speech_prob": 0.0, "avg_logprob": -0.2},
                ]})()

        class _A:
            transcriptions = _T()

        self.audio = _A()


def test_split_and_compress_chunks_and_shrinks(tone, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SECONDS", 10)
    work = tmp_path / "work"
    work.mkdir()
    chunks = audio_service.split_and_compress(str(tone), work)

    assert [c.offset_seconds for c in chunks] == [0, 10, 20]
    assert all(c.path.suffix == ".mp3" for c in chunks)
    assert sum(c.path.stat().st_size for c in chunks) < tone.stat().st_size / 10


def test_split_rejects_non_audio(tmp_path):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"ID3" + b"\x00" * 200)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(audio_service.AudioProcessingError, match="corrupted"):
        audio_service.split_and_compress(str(junk), work)


def test_transcribe_file_stitches_chunks_with_absolute_timestamps(tone, monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SECONDS", 10)
    stages = []
    client = _FakeClient()
    result = transcribe_file(str(tone), client=client, on_stage=stages.append)

    assert stages == ["compressing", "transcribing"]
    assert len(client.calls) == 3
    assert [s["start"] for s in result["segments"]] == [0.0, 10.0, 20.0]  # offsets applied, order kept
    assert result["segments"][1]["text"] == "said in chunk_0001.mp3"
    assert result["text"].startswith("said in chunk_0000.mp3 said in chunk_0001.mp3")
    assert result["duration"] == 25.0


def test_transcribe_file_reports_missing_audio(tmp_path):
    with pytest.raises(TranscriptionError, match="no longer available"):
        transcribe_file(str(tmp_path / "gone.mp3"), client=_FakeClient())


def test_transcribe_drops_hallucinated_silence_segments(tmp_path):
    audio = tmp_path / "c.mp3"
    audio.write_bytes(b"x")

    class _T:
        @staticmethod
        def create(**kwargs):
            return type("R", (), {"text": "Thank you. real words", "segments": [
                {"start": 0, "end": 2, "text": " Thank you.", "no_speech_prob": 0.9, "avg_logprob": -1.8},
                {"start": 2, "end": 4, "text": " real words", "no_speech_prob": 0.05, "avg_logprob": -0.2},
            ]})()

    class _A:
        transcriptions = _T()

    client = type("C", (), {"audio": _A()})()
    result = transcribe(str(audio), client=client)
    assert result["text"] == "real words"
    assert len(result["segments"]) == 1
