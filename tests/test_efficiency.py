"""Resource-saving and fairness features: model fallback, usage metering,
answer cache, fair scheduling, per-owner caps, conditional GETs, live
pre-summaries and the cross-process lock."""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.core import config
from backend.app.database.meetings_repo import (
    add_recording_chunk,
    claim_next,
    create_meeting,
    get_meeting,
    save_chunk_segments,
    save_segments,
    save_summary,
    save_transcript,
    update_status,
)
from backend.app.models.meeting import MeetingStatus
from backend.app.services import processing_service, qa_service, summarization_service
from backend.app.utils import locks, rate_limit
from backend.app.utils.usage import merge_usage, metering

TOKEN = "e" * 32
H = {"X-Owner-Token": TOKEN}
OWNER = hashlib.sha256(TOKEN.encode()).hexdigest()
NOTE = {"title": "T", "summary": "S", "key_decisions": [], "action_items": [], "open_questions": []}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    from backend.app.main import app

    return app


class _LLM:
    """Fake Groq chat client: records the model of every call, reports token usage."""

    def __init__(self, tokens=1234, content=None):
        self.models = []
        outer = self

        class _Completions:
            @staticmethod
            def create(**kw):
                outer.models.append(kw["model"])
                msg = type("M", (), {"content": content or json.dumps(NOTE)})()
                choice = type("Ch", (), {"message": msg, "finish_reason": "stop"})()
                usage = type("U", (), {"total_tokens": tokens})()
                return type("R", (), {"choices": [choice], "usage": usage})()

        self.chat = type("Chat", (), {"completions": _Completions()})()


# ---- model fallback ----

def test_saturated_model_falls_back_to_the_second_models_budget(monkeypatch):
    monkeypatch.setattr(config, "FALLBACK_AFTER_SECONDS", 0)
    rate_limit.acquire_budget(f"tokens:{config.SUMMARY_MODEL}", config.LLM_TOKENS_PER_MINUTE,
                              limit=config.LLM_TOKENS_PER_MINUTE)
    llm = _LLM()
    summarization_service._structured_call(llm, model=config.SUMMARY_MODEL, system="s", user="u", max_tokens=100)
    assert llm.models == [config.FALLBACK_MODEL]


def test_primary_model_used_while_it_has_room():
    llm = _LLM()
    summarization_service._structured_call(llm, model=config.SUMMARY_MODEL, system="s", user="u", max_tokens=100)
    assert llm.models == [config.SUMMARY_MODEL]


def test_no_fallback_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "FALLBACK_MODEL", "")
    rate_limit.acquire_budget(f"tokens:{config.SUMMARY_MODEL}", config.LLM_TOKENS_PER_MINUTE,
                              limit=config.LLM_TOKENS_PER_MINUTE)
    with pytest.raises(summarization_service.ProviderBusyError):
        summarization_service._structured_call(_LLM(), model=config.SUMMARY_MODEL, system="s", user="u",
                                               max_tokens=100, max_wait=0)


def test_question_spills_over_to_the_other_model_then_reports_busy(app, monkeypatch):
    monkeypatch.setattr(config, "FALLBACK_AFTER_SECONDS", 0)
    full = config.LLM_TOKENS_PER_MINUTE

    def fill(model):  # record a full minute of usage without waiting for room
        rate_limit.acquire_budget(f"tokens:{model}", full, limit=10**9)

    fill(config.QA_MODEL)
    used = []

    class _Chat:
        class completions:
            @staticmethod
            def create(**kw):
                used.append(kw["model"])
                msg = type("M", (), {"content": "Two [M1 00:03]."})()
                return type("R", (), {"choices": [type("Ch", (), {"message": msg})()], "usage": None})()

    client = type("C", (), {"chat": _Chat})()
    assert qa_service.answer("[M1 00:03] We hire two.", "how many?", client=client) == "Two [M1 00:03]."
    assert used == [config.SUMMARY_MODEL]

    fill(config.SUMMARY_MODEL)
    m = _completed()
    with TestClient(app, headers=H) as http:
        r = http.post(f"/api/v1/meetings/{m.id}/ask", json={"question": "how many engineers?"})
    assert r.status_code == 503 and int(r.headers["retry-after"]) >= 1  # busy, not a crash


# ---- usage metering ----

def test_metering_counts_tokens_per_model():
    with metering() as meter:
        summarization_service._structured_call(_LLM(tokens=900), model="a", system="s", user="u", max_tokens=9,
                                               allow_fallback=False)
        summarization_service._structured_call(_LLM(tokens=100), model="b", system="s", user="u", max_tokens=9,
                                               allow_fallback=False)
    assert meter == {"llm_tokens": 1000, "llm_calls": 2, "models": {"a": 900, "b": 100}}


def test_merge_usage_sums_numbers_and_nested_models():
    total = merge_usage({"llm_tokens": 5, "models": {"a": 5}}, {"llm_tokens": 3, "models": {"a": 1, "b": 2}, "x": True})
    assert total == {"llm_tokens": 8, "models": {"a": 6, "b": 2}, "x": True}


def test_pipeline_records_audio_and_token_usage(monkeypatch):
    m = create_meeting("a.mp3", "hu", "x")
    monkeypatch.setattr(processing_service, "transcribe_file", lambda *a, **k: {
        "text": "We agreed to ship on Friday after the review.", "segments": [], "duration": 90.0, "sent_seconds": 60.0})
    llm = _LLM(tokens=1700)
    monkeypatch.setattr(summarization_service, "Groq", lambda: llm)
    processing_service.run(m.id)
    usage = get_meeting(m.id).usage
    assert usage["audio_seconds"] == 90.0 and usage["audio_sent_seconds"] == 60.0
    assert usage["llm_tokens"] == 1700 and usage["models"] == {config.SUMMARY_MODEL: 1700}


# ---- answer cache ----

def _completed(owner=OWNER, title="Hiring"):
    m = create_meeting("a.mp3", "h" + title, "gone", owner_id=owner)
    save_transcript(m.id, "We agreed to hire two engineers.")
    save_segments(m.id, [{"start": 3.0, "end": 6.0, "text": "We agreed to hire two engineers."}], 6.0)
    save_summary(m.id, "Hiring plan.", ["Hire two engineers"], [], title=title)
    update_status(m.id, MeetingStatus.COMPLETED)
    return get_meeting(m.id)


def _counting_answer(monkeypatch):
    calls = []

    def fake_answer(context, question, client=None):
        calls.append(question)
        return "Two engineers [M1 00:03]."

    monkeypatch.setattr(qa_service, "answer", fake_answer)
    return calls


def test_repeated_question_is_answered_from_cache(monkeypatch):
    calls = _counting_answer(monkeypatch)
    meeting = _completed()
    first = qa_service.ask(meeting, "How many engineers will we hire?")
    again = qa_service.ask(meeting, "how many engineers will we hire")  # case/punctuation don't matter
    assert calls == ["How many engineers will we hire?"]
    assert first["cached"] is False and again["cached"] is True
    assert again["answer"] == first["answer"] and again["sources"] == first["sources"]


def test_cache_misses_when_the_meetings_change(monkeypatch):
    calls = _counting_answer(monkeypatch)
    _completed()
    qa_service.ask_all(OWNER, "how many engineers")
    _completed(title="Hiring follow-up")  # new matching meeting -> different context
    assert qa_service.ask_all(OWNER, "how many engineers")["cached"] is False
    assert len(calls) == 2


def test_cache_is_per_owner_and_cleared_on_delete(app, monkeypatch):
    calls = _counting_answer(monkeypatch)
    other = hashlib.sha256(b"o" * 32).hexdigest()
    mine, theirs = _completed(), _completed(owner=other)
    qa_service.ask(mine, "how many engineers")
    qa_service.ask(theirs, "how many engineers")
    assert len(calls) == 2  # identical content, but never shared across owners

    with TestClient(app, headers=H) as client:
        assert client.delete(f"/api/v1/meetings/{mine.id}").status_code == 204
    from backend.app.database.connection import get_connection

    conn = get_connection()
    left = conn.execute("SELECT COUNT(*) FROM answer_cache WHERE owner_id = ?", (OWNER,)).fetchone()[0]
    conn.close()
    assert left == 0


# ---- fair scheduling ----

def _queued(owner, name, source="upload"):
    m = create_meeting(name, "h" + name, "x", owner_id=owner, source=source)
    update_status(m.id, MeetingStatus.QUEUED)
    time.sleep(0.002)  # distinct created_at ordering
    return m.id


def test_one_owners_batch_does_not_starve_another_owner():
    a, b = "a" * 64, "b" * 64
    a_jobs = [_queued(a, f"a{i}") for i in range(3)]
    b_job = _queued(b, "b0")
    assert claim_next() == a_jobs[0]  # oldest first while nobody is running
    assert claim_next() == b_job      # a already has one running, b has none
    assert claim_next() == a_jobs[1]


def test_finished_live_recording_jumps_the_queue():
    first = _queued("a" * 64, "upload")
    rec = _queued("b" * 64, "rec", source="recording")
    assert claim_next() == rec
    assert claim_next() == first


def test_owner_backlog_cap(app, monkeypatch):
    monkeypatch.setattr(config, "MAX_QUEUED_PER_OWNER", 2)
    for i in range(2):
        _queued(OWNER, f"q{i}")
    wav = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 100
    with TestClient(app, headers=H) as client:
        r = client.post("/api/v1/meetings", files={"audio": ("a.wav", wav, "audio/wav")})
    assert r.status_code == 429 and "2 meetings" in r.json()["detail"]


# ---- conditional GET ----

def test_unchanged_meeting_is_answered_304(app):
    m = _completed()
    with TestClient(app, headers=H) as client:
        for path in (f"/api/v1/meetings/{m.id}", f"/api/v1/meetings/{m.id}/status"):
            first = client.get(path)
            etag = first.headers["etag"]
            assert first.status_code == 200 and first.headers["cache-control"] == "private, no-cache"
            again = client.get(path, headers={"If-None-Match": etag})
            assert again.status_code == 304 and again.content == b""
        time.sleep(0.002)
        update_status(m.id, MeetingStatus.COMPLETED)  # any change -> new version
        assert client.get(f"/api/v1/meetings/{m.id}", headers={"If-None-Match": etag}).status_code == 200


def test_conditional_get_still_hides_other_owners_meetings(app):
    m = _completed(owner="z" * 64)
    with TestClient(app, headers=H) as client:
        assert client.get(f"/api/v1/meetings/{m.id}", headers={"If-None-Match": "*"}).status_code == 404


# ---- live pre-summaries ----

def _recording_with_text(n_chunks, words_per_chunk=40):
    m = create_meeting("rec", "rec-h", "", owner_id=OWNER, source="recording", status=MeetingStatus.RECORDING)
    for seq in range(n_chunks):
        add_recording_chunk(m.id, seq, seq * 45.0, "", f"c{seq}")
        segs = [{"start": seq * 45.0 + i, "end": seq * 45.0 + i + 1, "text": f"chunk {seq} word {i}"}
                for i in range(words_per_chunk)]
        save_chunk_segments(m.id, seq, segs)
    return m.id


def test_live_recording_presummarizes_final_sections_and_finish_reuses_them(monkeypatch):
    monkeypatch.setattr(config, "SECTION_CHARS", 300)
    monkeypatch.setattr(config, "SINGLE_PASS_CHARS", 300)
    sections = []
    monkeypatch.setattr(processing_service, "summarize_section",
                        lambda i, text, **kw: sections.append(i) or dict(NOTE, title=f"Part {i + 1}"))
    mid = _recording_with_text(3)

    done = processing_service.presummarize_recording(mid)
    assert done > 0 and sections == list(range(done))  # every closed section, in order
    assert processing_service.presummarize_recording(mid) == 0  # nothing new: no repeat calls

    # Stop: the worker only summarizes what wasn't done live, then merges.
    later = []
    monkeypatch.setattr(summarization_service, "summarize_section",
                        lambda i, text, **kw: later.append(i) or dict(NOTE, title=f"Part {i + 1}"))
    monkeypatch.setattr(summarization_service, "_merge", lambda client, notes: dict(NOTE, title="Whole"))
    monkeypatch.setattr(summarization_service, "Groq", lambda: object())
    update_status(mid, MeetingStatus.QUEUED)
    processing_service.run(mid)
    meeting = get_meeting(mid)
    assert meeting.status == MeetingStatus.COMPLETED, meeting.error_message
    assert later and min(later) >= done  # the live sections were reused, not redone
    assert [c["title"] for c in meeting.chapters] == [f"Part {i + 1}" for i in range(done + len(later))]


def test_presummary_skips_short_recordings_and_gaps(monkeypatch):
    monkeypatch.setattr(processing_service, "summarize_section", lambda *a, **k: pytest.fail("called"))
    assert processing_service.presummarize_recording(_recording_with_text(2, words_per_chunk=3)) == 0

    monkeypatch.setattr(config, "SECTION_CHARS", 100)
    monkeypatch.setattr(config, "SINGLE_PASS_CHARS", 100)
    m = create_meeting("rec", "rec-g", "", owner_id=OWNER, source="recording", status=MeetingStatus.RECORDING)
    add_recording_chunk(m.id, 1, 45.0, "", "g1")  # piece 0 hasn't arrived yet
    save_chunk_segments(m.id, 1, [{"start": 45.0 + i, "end": 46.0 + i, "text": "word " * 10} for i in range(20)])
    assert processing_service.presummarize_recording(m.id) == 0


def test_presummary_stops_quietly_when_provider_is_busy(monkeypatch):
    monkeypatch.setattr(config, "SECTION_CHARS", 300)
    monkeypatch.setattr(config, "SINGLE_PASS_CHARS", 300)

    def busy(*a, **k):
        raise summarization_service.ProviderBusyError("full", retry_after=30)

    monkeypatch.setattr(processing_service, "summarize_section", busy)
    mid = _recording_with_text(3)
    assert processing_service.presummarize_recording(mid) == 0
    assert get_meeting(mid).status == MeetingStatus.RECORDING


def test_presummary_runs_once_at_a_time():
    mid = _recording_with_text(1)
    holder = locks.try_acquire(f"presummary:{mid}", 60)
    assert holder and processing_service.presummarize_recording(mid) == 0
    locks.release(f"presummary:{mid}", holder)


def test_lock_expires_on_its_own():
    assert locks.try_acquire("x", 60) is not None
    assert locks.try_acquire("x", 60) is None
    assert locks.try_acquire("y", -1) is not None  # already expired
    assert locks.try_acquire("y", 60) is not None


# ---- metrics ----

def test_metrics_report_savings(app):
    m = create_meeting("a.mp3", "hm", "x")
    from backend.app.database.meetings_repo import add_usage

    add_usage(m.id, {"audio_seconds": 90.0, "audio_sent_seconds": 60.0, "llm_tokens": 500})
    with TestClient(app) as client:
        body = client.get("/metrics").text
    assert "audio_seconds_skipped_total 30" in body
    assert "llm_tokens_total 500" in body
    assert "answer_cache_hits_total 0" in body


def test_forwarded_header_ignored_unless_behind_a_trusted_proxy(app, monkeypatch):
    from backend.app.api.meetings import _client_ip

    req = type("R", (), {"headers": {"x-forwarded-for": "6.6.6.6"},
                         "client": type("C", (), {"host": "10.0.0.1"})()})()
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", False)
    assert _client_ip(req) == "10.0.0.1"   # a client can't pick its own rate-limit bucket
    monkeypatch.setattr(config, "TRUST_PROXY_HEADERS", True)
    assert _client_ip(req) == "6.6.6.6"
