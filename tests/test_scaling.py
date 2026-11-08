"""Scalability behaviour: shared budgets, deferral, checkpoints, long meetings,
live recording, backpressure, metrics, and the standalone worker process."""

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.core import config
from backend.app.database.meetings_repo import (
    claim_next,
    create_meeting,
    get_meeting,
    save_notes_cache,
    save_segments,
    save_transcript,
    update_status,
)
from backend.app.models.meeting import MeetingStatus
from backend.app.services import processing_service, summarization_service
from backend.app.utils import rate_limit
from backend.app.utils.errors import ProviderBusyError

TOKEN = "s" * 32
H = {"X-Owner-Token": TOKEN}
OWNER = hashlib.sha256(TOKEN.encode()).hexdigest()
NOTE = {"title": "T", "summary": "S", "key_decisions": [], "action_items": [], "open_questions": []}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    from backend.app.main import app

    return app


# ---- shared provider budget ----

def test_budget_waits_for_room_then_settles_to_real_usage(monkeypatch):
    sleeps = []
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: clock[0])

    def fake_sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(rate_limit.time, "sleep", fake_sleep)

    r1 = rate_limit.acquire_budget("tokens:m", 6000, limit=7000, window_seconds=60)
    rate_limit.settle_budget(r1, 5000)  # used less than reserved
    rate_limit.acquire_budget("tokens:m", 2000, limit=7000, window_seconds=60)  # fits after settling
    assert sleeps == []
    rate_limit.acquire_budget("tokens:m", 3000, limit=7000, window_seconds=60)  # must wait for the window
    assert sum(sleeps) >= 59
    assert rate_limit.budget_used("tokens:m") <= 7000


def test_budget_gives_up_with_provider_busy(monkeypatch):
    rate_limit.acquire_budget("tokens:x", 7000, limit=7000)
    with pytest.raises(ProviderBusyError):
        rate_limit.acquire_budget("tokens:x", 100, limit=7000, max_wait_seconds=0)


# ---- provider busy -> job deferred, not failed ----

def test_provider_busy_defers_job_and_keeps_audio(tmp_path, monkeypatch):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    m = create_meeting("a.mp3", "h1", str(audio))
    update_status(m.id, MeetingStatus.QUEUED)
    claim_next()

    def busy(*a, **k):
        raise ProviderBusyError("quota", retry_after=5)

    monkeypatch.setattr(processing_service, "transcribe_file", busy)
    processing_service.run(m.id)

    deferred = get_meeting(m.id)
    assert deferred.status == MeetingStatus.QUEUED
    assert deferred.stage == "waiting for AI capacity"
    assert audio.exists()  # will be needed on retry
    assert claim_next() is None  # not runnable until the delay passes


def test_job_stuck_on_quota_for_too_long_fails(monkeypatch):
    m = create_meeting("a.mp3", "h1", "x")
    monkeypatch.setattr(config, "MAX_JOB_AGE_HOURS", 0)
    monkeypatch.setattr(processing_service, "transcribe_file",
                        lambda *a, **k: (_ for _ in ()).throw(ProviderBusyError("quota")))
    processing_service.run(m.id)
    assert get_meeting(m.id).status == MeetingStatus.FAILED


# ---- checkpoints ----

def test_retry_skips_transcription_already_done(monkeypatch):
    m = create_meeting("a.mp3", "h1", "x")
    save_transcript(m.id, "a transcript that was saved before the crash happened")
    monkeypatch.setattr(processing_service, "transcribe_file",
                        lambda *a, **k: pytest.fail("re-transcribed finished audio"))
    monkeypatch.setattr(processing_service, "summarize", lambda t: dict(NOTE, summary="done"))
    processing_service.run(m.id)
    assert get_meeting(m.id).summary == "done"


# ---- long meetings ----

def _segments(n, chars=100):
    return [{"start": i * 10.0, "end": i * 10.0 + 9, "text": f"s{i} " + "x" * chars} for i in range(n)]


def test_split_sections_breaks_between_segments():
    sections = summarization_service.split_sections(_segments(10), max_chars=350)
    assert [s["start"] for s in sections] == [0.0, 30.0, 60.0, 90.0]
    assert all(len(s["text"]) <= 350 for s in sections)


def test_long_meeting_map_reduce_with_chapters_and_resume(monkeypatch):
    monkeypatch.setattr(config, "SECTION_CHARS", 350)
    calls = []

    def fake_call(client, *, model, system, user, max_tokens, reasoning_effort=None, **kw):
        calls.append(model)
        if "Section notes" in user:
            return dict(NOTE, title="Whole Meeting", summary="merged")
        n = user.split("Section ")[1].split(" of")[0]
        return dict(NOTE, title=f"Chapter {n}")

    monkeypatch.setattr(summarization_service, "_structured_call", fake_call)
    saved = {}
    result = summarization_service.summarize_long("t", _segments(10), cache={}, on_progress=saved.update, client=object())

    assert result["title"] == "Whole Meeting"
    assert result["chapters"] == [{"title": f"Chapter {i}", "start": float(s)} for i, s in [(1, 0), (2, 30), (3, 60), (4, 90)]]
    assert calls.count(config.SECTION_MODEL) == 4 and calls[-1] == config.SUMMARY_MODEL

    calls.clear()
    partial = dict(list(saved.items())[:3])  # interrupted after 3 of 4 sections
    summarization_service.summarize_long("t", _segments(10), cache=partial, client=object())
    assert calls.count(config.SECTION_MODEL) == 1  # only the missing section redone


def test_merge_is_hierarchical_when_notes_dont_fit(monkeypatch):
    monkeypatch.setattr(config, "SECTION_CHARS", 300)
    merges = []

    def fake_call(client, *, user, **kw):
        merges.append(user.count('"title"'))
        return dict(NOTE)

    monkeypatch.setattr(summarization_service, "_structured_call", fake_call)
    summarization_service._merge(object(), [dict(NOTE, summary="y" * 100) for _ in range(8)])
    assert max(merges) < 8 and len(merges) > 1  # never one giant call


def test_pipeline_routes_long_transcripts_to_sections(monkeypatch):
    monkeypatch.setattr(config, "SINGLE_PASS_CHARS", 50)
    m = create_meeting("a.mp3", "h1", "x")
    segs = _segments(3)
    save_transcript(m.id, " ".join(s["text"] for s in segs))
    save_segments(m.id, segs, 29.0)
    monkeypatch.setattr(processing_service, "summarize", lambda t: pytest.fail("used single pass"))
    monkeypatch.setattr(processing_service, "summarize_long",
                        lambda *a, **k: dict(NOTE, chapters=[{"title": "Intro", "start": 0.0}]))
    processing_service.run(m.id)
    done = get_meeting(m.id)
    assert done.status == MeetingStatus.COMPLETED and done.chapters == [{"title": "Intro", "start": 0.0}]


def test_structured_call_retries_with_low_effort_when_output_truncated(monkeypatch):
    efforts = []

    class _C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    efforts.append(kw.get("reasoning_effort"))
                    reason = "length" if len(efforts) == 1 else "stop"
                    msg = type("M", (), {"content": json.dumps(NOTE)})()
                    return type("R", (), {"choices": [type("Ch", (), {"message": msg, "finish_reason": reason})()]})()

    out = summarization_service._structured_call(_C(), model="m", system="s", user="u", max_tokens=100)
    assert out["title"] == "T" and efforts == [None, "low"]


# ---- live recording ----

def test_live_recording_flow(app, monkeypatch):
    transcribed = []

    def fake_transcribe(path, **kw):
        transcribed.append(path)
        return {"text": "hello there everyone", "segments": [{"start": 1.0, "end": 3.0, "text": "hello there everyone"}],
                "duration": 3.0}

    monkeypatch.setattr(processing_service, "transcribe_file", fake_transcribe)
    piece = lambda n: ("piece.webm", io.BytesIO(b"\x1a\x45\xdf\xa3" + bytes([n]) * 64), "audio/webm")

    with TestClient(app, headers=H) as client:
        rid = client.post("/api/v1/recordings").json()["id"]
        assert client.post(f"/api/v1/recordings/{rid}/finish").status_code == 422  # nothing recorded yet
        r0 = client.post(f"/api/v1/recordings/{rid}/chunks", data={"seq": 0, "offset": 0}, files={"audio": piece(1)})
        r1 = client.post(f"/api/v1/recordings/{rid}/chunks", data={"seq": 1, "offset": 45}, files={"audio": piece(2)})
        dup = client.post(f"/api/v1/recordings/{rid}/chunks", data={"seq": 1, "offset": 45}, files={"audio": piece(2)})
        assert r0.json() == {"seq": 0, "transcribed": True} and r1.json()["transcribed"] and dup.json()["transcribed"]
        assert len(transcribed) == 2  # the retried piece wasn't transcribed twice
        assert client.post(f"/api/v1/recordings/{rid}/finish").json()["status"] == "QUEUED"
        assert client.post(f"/api/v1/recordings/{rid}/chunks", data={"seq": 2, "offset": 90},
                           files={"audio": piece(3)}).status_code == 409  # finished: no more pieces
        other = client.post(f"/api/v1/recordings/{rid}/finish", headers={"X-Owner-Token": "o" * 32})
        assert other.status_code == 404

    monkeypatch.setattr(processing_service, "summarize", lambda t: dict(NOTE, title="Live"))
    claim_next()
    processing_service.run(rid)
    done = get_meeting(rid)
    assert done.status == MeetingStatus.COMPLETED and done.title == "Live"
    assert [s["start"] for s in done.segments] == [1.0, 46.0]  # piece offsets applied
    assert len(transcribed) == 2  # summary step didn't re-transcribe


def test_live_piece_falls_back_to_worker_when_provider_busy(app, monkeypatch):
    state = {"busy": True}

    def flaky(path, **kw):
        if state["busy"]:
            raise ProviderBusyError("quota")
        return {"text": "late words", "segments": [{"start": 0.0, "end": 2.0, "text": "late words arriving after the pause"}], "duration": 2.0}

    monkeypatch.setattr(processing_service, "transcribe_file", flaky)
    with TestClient(app, headers=H) as client:
        rid = client.post("/api/v1/recordings").json()["id"]
        r = client.post(f"/api/v1/recordings/{rid}/chunks", data={"seq": 0, "offset": 10},
                        files={"audio": ("p.webm", io.BytesIO(b"\x1a\x45\xdf\xa3" + b"\x07" * 64), "audio/webm")})
        assert r.json() == {"seq": 0, "transcribed": False}
        client.post(f"/api/v1/recordings/{rid}/finish")

    state["busy"] = False
    monkeypatch.setattr(processing_service, "summarize", lambda t: dict(NOTE))
    claim_next()
    processing_service.run(rid)
    done = get_meeting(rid)
    assert done.status == MeetingStatus.COMPLETED and done.segments[0]["start"] == 10.0


# ---- backpressure + metrics ----

def test_backpressure_returns_503_when_queue_full(app, monkeypatch):
    monkeypatch.setattr(config, "MAX_QUEUE_DEPTH", 2)
    for i in range(2):
        update_status(create_meeting(f"{i}.mp3", f"q{i}", "x").id, MeetingStatus.QUEUED)
    with TestClient(app, headers=H) as client:
        r = client.post("/api/v1/meetings", files={"audio": ("a.mp3", io.BytesIO(b"ID3" + b"\x00" * 50), "audio/mpeg")},
                        headers={"x-forwarded-for": "192.0.2.200"})
    assert r.status_code == 503 and r.headers["retry-after"] == "120"


def test_metrics_endpoint(app):
    update_status(create_meeting("a.mp3", "m1", "x").id, MeetingStatus.QUEUED)
    rate_limit.acquire_budget(f"tokens:{config.SUMMARY_MODEL}", 1234, limit=7000)
    with TestClient(app) as client:
        body = client.get("/metrics").text
    assert 'meetings{status="QUEUED"} 1' in body
    assert f'provider_tokens_last_minute{{model="{config.SUMMARY_MODEL}"}} 1234' in body
    assert "queue_oldest_waiting_seconds" in body


# ---- standalone worker process ----

def test_standalone_worker_process_drains_queue_and_stops_on_sigterm(tmp_path):
    db = tmp_path / "w.db"
    from backend.app.database.init_db import init_db
    init_db(db)
    ids = [create_meeting(f"{i}.mp3", f"w{i}", "x", db_path=db).id for i in range(3)]
    for i in ids:
        update_status(i, MeetingStatus.QUEUED, db_path=db)

    # A tiny launcher: real worker main(), with the pipeline faked to "complete" jobs.
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "from pathlib import Path\n"
        "from backend.app.core import config\n"
        f"config.DATABASE_PATH = Path({str(db)!r})\n"
        "from backend.app.services import processing_service, worker\n"
        "from backend.app.database.meetings_repo import update_status\n"
        "processing_service.run = lambda mid: update_status(mid, 'COMPLETED')\n"
        "worker._IDLE_POLL_SECONDS = 0.1\n"
        "from backend.app.worker import main\n"
        "main()\n"
    )
    env = dict(os.environ, WORKER_CONCURRENCY="2", PYTHONPATH=os.pathsep.join(sys.path))
    proc = subprocess.Popen([sys.executable, str(launcher)], env=env, cwd=os.getcwd())
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if all(get_meeting(i, db_path=db).status == "COMPLETED" for i in ids):
                break
            time.sleep(0.1)
        assert all(get_meeting(i, db_path=db).status == "COMPLETED" for i in ids)
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0


def test_budget_reserves_expected_output_not_the_cap(monkeypatch):
    reserved = []
    monkeypatch.setattr(summarization_service, "acquire_budget", lambda key, amount, **k: reserved.append(amount) or 1)
    monkeypatch.setattr(summarization_service, "settle_budget", lambda *a, **k: None)

    class _C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    msg = type("M", (), {"content": json.dumps(NOTE)})()
                    return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()

    summarization_service._structured_call(_C(), model="m", system="s" * 320, user="u" * 320, max_tokens=3000)
    assert reserved[0] < 1200  # ~250 input + 900 expected output, not +3000
