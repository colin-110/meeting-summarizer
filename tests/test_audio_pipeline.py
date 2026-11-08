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
    chunks = audio_service.split_and_compress(str(tone), work).chunks

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


def test_pick_cuts_snaps_to_pauses_and_falls_back_to_grid():
    from backend.app.services.audio_service import pick_cuts

    assert pick_cuts(25, [], 10) == [10, 20]                       # no pauses: plain grid
    assert pick_cuts(30, [12.5], 10) == [12.5, 22.5]               # snaps to the pause, then re-targets from there
    assert pick_cuts(25, [40.0], 10) == [10, 20]                   # far-away pause ignored
    assert pick_cuts(21, [], 10) == [10]                           # no tiny trailing chunk
    assert pick_cuts(5, [2.0], 10) == []                           # short audio: one chunk


def test_split_cuts_at_a_real_pause_not_mid_speech(tmp_path, monkeypatch):
    """Two 12s tones separated by 1s of silence: the cut must land in the gap."""
    monkeypatch.setattr(config, "CHUNK_SECONDS", 10)
    path = tmp_path / "gap.wav"
    subprocess.run(
        [audio_service.ffmpeg_exe(), "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(path)],
        check=True,
    )
    work = tmp_path / "work"
    work.mkdir()
    chunks = audio_service.split_and_compress(str(path), work).chunks

    assert len(chunks) >= 2
    assert abs(chunks[1].offset_seconds - 12.5) < 0.4  # inside the 12.0-13.0 gap
    assert not (work / "full.mp3").exists()


# ---- silence skipping ----

def _gappy(tmp_path, gaps=(6, 6)):
    """5s tone, then for each gap: that many seconds of silence and another 5s tone."""
    path = tmp_path / "gappy.wav"
    inputs, labels = ["-f", "lavfi", "-i", "sine=frequency=440:duration=5"], ["[0]"]
    for i, gap in enumerate(gaps):
        inputs += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={gap}",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=5"]
        labels += [f"[{2 * i + 1}]", f"[{2 * i + 2}]"]
    subprocess.run(
        [audio_service.ffmpeg_exe(), "-v", "error", *inputs, "-filter_complex",
         f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1", "-ac", "1", str(path)],
        check=True,
    )
    return path


def test_plan_silence_cuts_shortens_only_long_pauses():
    from backend.app.services.audio_service import plan_silence_cuts

    silences = [(5.0, 11.0), (12.0, 13.0), (20.0, 26.0)]
    cuts = plan_silence_cuts(30, silences, min_gap=2.0, keep=0.6)
    assert cuts == [(5.3, 10.7), (20.3, 25.7)]           # the 1s pause is left alone
    assert plan_silence_cuts(30, [(5.0, 7.5)], min_gap=2.0, keep=0.6) == []  # saving too small
    assert plan_silence_cuts(30, silences, min_gap=0, keep=0.6) == []        # disabled


def test_timeline_maps_sent_time_back_to_the_recording():
    from backend.app.services.audio_service import timeline_for

    tl = timeline_for(30.0, [(5.3, 10.7), (20.3, 25.7)])
    assert tl.sent_seconds == pytest.approx(19.2)
    assert tl.to_original(0.0) == 0.0
    assert tl.to_original(5.3) == 10.7                   # a start on the join is after the cut
    assert tl.to_original(5.3, is_end=True) == 5.3       # an end on the join is before it
    assert tl.to_original(6.0) == 11.4
    assert tl.to_original(19.2, is_end=True) == 30.0
    assert tl.to_sent(8.0) == pytest.approx(5.3)          # inside a removed pause -> the join


def test_long_pauses_are_not_sent_and_timestamps_still_match(tmp_path, monkeypatch):
    """27s recording with two 6s pauses: ~10.8s of silence isn't sent, and the
    provider's timestamps (relative to what it got) land back on the real tones."""
    monkeypatch.setattr(config, "CHUNK_SECONDS", 600)
    path = _gappy(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    prepared = audio_service.split_and_compress(str(path), work)
    assert prepared.timeline.original_seconds == pytest.approx(27.0, abs=0.1)
    assert prepared.timeline.sent_seconds == pytest.approx(16.2, abs=0.3)
    sent = subprocess.run([audio_service.ffmpeg_exe(), "-i", str(prepared.chunks[0].path), "-f", "null", "-"],
                          capture_output=True, text=True).stderr
    assert audio_service._output_seconds(sent) == pytest.approx(prepared.timeline.sent_seconds, abs=0.1)

    tones_in_sent_audio = [(0.0, 5.0), (5.6, 10.6), (11.2, 16.2)]

    class _T:
        @staticmethod
        def create(**kwargs):
            return type("R", (), {"text": "", "segments": [
                {"start": a, "end": b, "text": f"tone {i}", "no_speech_prob": 0.0, "avg_logprob": -0.1}
                for i, (a, b) in enumerate(tones_in_sent_audio)]})()

    client = type("C", (), {"audio": type("A", (), {"transcriptions": _T()})()})()
    result = transcribe_file(str(path), client=client)
    starts = [s["start"] for s in result["segments"]]
    ends = [s["end"] for s in result["segments"]]
    assert starts == pytest.approx([0.0, 11.0, 22.0], abs=0.25)
    assert ends == pytest.approx([5.0, 16.0, 27.0], abs=0.25)
    assert result["duration"] == pytest.approx(27.0, abs=0.1)
    assert result["sent_seconds"] == pytest.approx(16.2, abs=0.3)


def test_silence_removal_is_dropped_if_the_output_length_is_off(tmp_path):
    """A wrong timeline would shift every timestamp, so a mismatch keeps the original."""
    path = _gappy(tmp_path)
    out = tmp_path / "trimmed.mp3"
    assert audio_service._remove_silence(path, out, [(5.3, 10.7)], expected=3.0) is False
    assert not out.exists()


def test_skip_silence_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SKIP_SILENCE_SECONDS", 0)
    work = tmp_path / "work"
    work.mkdir()
    prepared = audio_service.split_and_compress(str(_gappy(tmp_path)), work)
    assert prepared.removed == []
    assert prepared.timeline.sent_seconds == pytest.approx(27.0, abs=0.1)
