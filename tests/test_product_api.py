import hashlib
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.core import config
from backend.app.database.meetings_repo import create_meeting, get_meeting, save_segments, save_summary, save_transcript, update_status
from backend.app.models.meeting import MeetingStatus
from backend.app.services import audio_service, processing_service, qa_service


def _token(n):
    return n * 32


def _owner(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _h(token):
    return {"X-Owner-Token": token}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    from backend.app.database.init_db import init_db
    from backend.app.main import app

    init_db()

    return app


def _completed(owner_token, title="Budget Sync"):
    m = create_meeting("a.mp3", "h" + title, "gone/a.mp3", owner_id=_owner(owner_token))
    save_transcript(m.id, "We agreed to hire two engineers.")
    save_segments(m.id, [{"start": 3.0, "end": 6.0, "text": "We agreed to hire two engineers."}], 6.0)
    save_summary(
        m.id, "Hiring plan.", ["Hire two engineers"],
        [{"task": "Post the job ads", "assignee": "Sam", "deadline": "Friday"}],
        title=title, open_questions=["What is the budget?"],
    )
    update_status(m.id, MeetingStatus.COMPLETED)
    return m.id


def test_owner_header_is_required_and_validated(app):
    with TestClient(app) as client:
        assert client.get("/api/v1/meetings").status_code == 400
        assert client.get("/api/v1/meetings", headers={"X-Owner-Token": "short"}).status_code == 400
        assert client.get("/api/v1/meetings", headers=_h(_token("a"))).status_code == 200


def test_meetings_are_private_to_their_owner(app):
    mine = _completed(_token("a"))
    with TestClient(app) as client:
        assert client.get(f"/api/v1/meetings/{mine}", headers=_h(_token("a"))).status_code == 200
        for path in ("", "/status", "/export"):
            assert client.get(f"/api/v1/meetings/{mine}{path}", headers=_h(_token("b"))).status_code == 404
        assert client.get(f"/api/v1/meetings/{mine}/status", headers=_h(_token("b"))).status_code == 404
        assert client.post(f"/api/v1/meetings/{mine}/ask", json={"question": "what?"}, headers=_h(_token("b"))).status_code == 404
        assert client.delete(f"/api/v1/meetings/{mine}", headers=_h(_token("b"))).status_code == 404
        assert client.get("/api/v1/meetings", headers=_h(_token("b"))).json() == []
        assert len(client.get("/api/v1/meetings", headers=_h(_token("a"))).json()) == 1
    assert get_meeting(mine) is not None  # the other user's delete attempt changed nothing


def test_detail_includes_timestamped_segments(app):
    mid = _completed(_token("a"))
    with TestClient(app) as client:
        body = client.get(f"/api/v1/meetings/{mid}", headers=_h(_token("a"))).json()
    assert body["segments"] == [{"start": 3.0, "end": 6.0, "text": "We agreed to hire two engineers."}]
    assert body["audio_seconds"] == 6.0


def test_search_endpoint_and_pagination_params(app):
    _completed(_token("a"), title="Budget Sync")
    with TestClient(app) as client:
        hits = client.get("/api/v1/meetings?q=engineers", headers=_h(_token("a"))).json()
        assert len(hits) == 1 and "[[engineers]]" in hits[0]["snippet"]
        assert client.get("/api/v1/meetings?q=zebra", headers=_h(_token("a"))).json() == []
        assert client.get("/api/v1/meetings?limit=0", headers=_h(_token("a"))).status_code == 422
        assert client.get("/api/v1/meetings?limit=1&offset=5", headers=_h(_token("a"))).json() == []


def test_ask_answers_from_transcript(app, monkeypatch):
    mid = _completed(_token("a"))
    seen = {}

    def fake_ask(meeting, question, **kw):
        seen["context"] = qa_service.timestamped_transcript(meeting)
        return {"answer": "Two engineers [00:03].", "sources": []}

    monkeypatch.setattr(qa_service, "ask", fake_ask)
    with TestClient(app) as client:
        r = client.post(f"/api/v1/meetings/{mid}/ask", json={"question": "Who is being hired?"}, headers=_h(_token("a")))
        short = client.post(f"/api/v1/meetings/{mid}/ask", json={"question": "hi"}, headers=_h(_token("a")))
    assert r.status_code == 200 and r.json() == {"answer": "Two engineers [00:03].", "sources": []}
    assert seen["context"] == "[00:03] We agreed to hire two engineers."
    assert short.status_code == 422


def test_ask_requires_completed_meeting_and_is_rate_limited(app, monkeypatch):
    pending = create_meeting("p.mp3", "hp", "x", owner_id=_owner(_token("r")))
    mid = _completed(_token("r"))
    monkeypatch.setattr(qa_service, "ask", lambda *a, **k: {"answer": "ok", "sources": []})
    from backend.app.api import meetings as api
    monkeypatch.setattr(api, "_ASK_RATE_LIMIT", 2)
    with TestClient(app, headers=_h(_token("r"))) as client:
        assert client.post(f"/api/v1/meetings/{pending.id}/ask", json={"question": "anything?"}).status_code == 409
        codes = [client.post(f"/api/v1/meetings/{mid}/ask", json={"question": "anything?"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_ask_surfaces_provider_failure_as_502(app, monkeypatch):
    mid = _completed(_token("a"))

    def fail(*a, **k):
        raise qa_service.QAError("Could not answer right now: boom")

    monkeypatch.setattr(qa_service, "ask", fail)
    with TestClient(app) as client:
        r = client.post(f"/api/v1/meetings/{mid}/ask", json={"question": "anything?"}, headers=_h(_token("a")))
    assert r.status_code == 502 and "boom" in r.json()["detail"]


def test_export_markdown(app):
    mid = _completed(_token("a"))
    with TestClient(app) as client:
        r = client.get(f"/api/v1/meetings/{mid}/export", headers=_h(_token("a")))
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="Budget-Sync.md"'
    text = r.text
    assert text.startswith("# Budget Sync")
    assert "- Hire two engineers" in text
    assert "- [ ] Post the job ads — **Sam** (Friday)" in text
    assert "[00:03] We agreed to hire two engineers." in text


def test_delete_meeting(app):
    mid = _completed(_token("a"))
    with TestClient(app, headers=_h(_token("a"))) as client:
        assert client.delete(f"/api/v1/meetings/{mid}").status_code == 204
        assert client.get(f"/api/v1/meetings/{mid}").status_code == 404
        busy = create_meeting("p.mp3", "hp2", "x", owner_id=_owner(_token("a")))
        update_status(busy.id, MeetingStatus.PROCESSING)
        assert client.delete(f"/api/v1/meetings/{busy.id}").status_code == 409


def test_deleting_a_queued_meeting_removes_its_audio_file(app, tmp_path):
    audio = tmp_path / "queued.mp3"
    audio.write_bytes(b"audio")
    m = create_meeting("q.mp3", "hq", str(audio), owner_id=_owner(_token("a")))
    update_status(m.id, MeetingStatus.QUEUED)
    with TestClient(app, headers=_h(_token("a"))) as client:
        assert client.delete(f"/api/v1/meetings/{m.id}").status_code == 204
    assert not audio.exists()


def test_qa_to_markdown_handles_empty_meeting():
    from backend.app.models.meeting import Meeting

    md = qa_service.to_markdown(Meeting(id="1", filename="x.mp3", file_hash="h", audio_path="p", status="COMPLETED"))
    assert "_None recorded._" in md and "# x.mp3" in md


class _FakeLLM:
    def __init__(self, reply=" Answer. "):
        self.calls = []
        outer = self

        class _Completions:
            @staticmethod
            def create(**kw):
                outer.calls.append(kw)
                msg = type("M", (), {"content": reply})()
                return type("R", (), {"choices": [type("Ch", (), {"message": msg})()],
                                      "usage": type("U", (), {"total_tokens": 321})()})()

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _seed(owner_token, title, segments, summary="s"):
    m = create_meeting(f"{title}.mp3", "h" + title, "x", owner_id=_owner(owner_token))
    save_transcript(m.id, " ".join(s["text"] for s in segments))
    save_segments(m.id, segments, segments[-1]["end"])
    save_summary(m.id, summary, ["Ship in October"], [], title=title)
    update_status(m.id, MeetingStatus.COMPLETED)
    return get_meeting(m.id)


def test_qa_short_meeting_sends_whole_transcript_with_tags():
    m = _seed(_token("q"), "Short", [{"start": 3725.0, "end": 3730.0, "text": "late remark about budgets"}])
    llm = _FakeLLM()
    out = qa_service.ask(m, "What was said?", client=llm)
    assert out["answer"] == "Answer."
    prompt = llm.calls[0]["messages"][1]["content"]
    assert "[M1 1:02:05] late remark about budgets" in prompt  # hour timestamps format correctly
    assert "[M1 notes] Title: Short" in prompt and "Decisions: Ship in October" in prompt
    assert llm.calls[0]["model"] == "openai/gpt-oss-20b"


def test_qa_long_meeting_sends_only_relevant_passages(monkeypatch):
    monkeypatch.setattr(config, "QA_CONTEXT_CHARS", 2000)
    filler = [{"start": float(i * 5), "end": float(i * 5 + 5), "text": f"routine filler sentence number {i} about nothing"}
              for i in range(400)]
    filler[250] = {"start": 1250.0, "end": 1255.0, "text": "Priya will migrate the billing database next Tuesday"}
    m = _seed(_token("q"), "Long", filler)
    llm = _FakeLLM()
    out = qa_service.ask(m, "Who is migrating the billing database?", client=llm)

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "Priya will migrate the billing database" in prompt
    assert "[M1 20:50]" in prompt
    assert len(prompt) < 4000  # bounded no matter how long the meeting is
    assert out["sources"][0]["start"] == 1250.0


def test_qa_ask_all_spans_meetings_and_stays_private():
    _seed(_token("q"), "Pricing Review", [{"start": 10.0, "end": 14.0, "text": "we will raise the pro plan price to twelve dollars"}])
    _seed(_token("q"), "Hiring Sync", [{"start": 5.0, "end": 9.0, "text": "pricing for contractors stays the same"}])
    _seed(_token("z"), "Someone Else", [{"start": 1.0, "end": 2.0, "text": "pricing secret from another user"}])
    llm = _FakeLLM()

    out = qa_service.ask_all(_owner(_token("q")), "What did we decide about pricing?", client=llm)

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "twelve dollars" in prompt and "contractors" in prompt
    assert "secret from another user" not in prompt
    assert {s["title"] for s in out["sources"]} == {"Pricing Review", "Hiring Sync"}

    with pytest.raises(qa_service.QAError, match="No meetings matched"):
        qa_service.ask_all(_owner(_token("q")), "zebra giraffe", client=llm)


def test_ask_all_endpoint(app, monkeypatch):
    _seed(_token("w"), "Pricing Review", [{"start": 10.0, "end": 14.0, "text": "raise the price to twelve dollars"}])
    monkeypatch.setattr(qa_service, "answer", lambda context, q, **k: "Twelve dollars [M1 00:10].")
    with TestClient(app, headers=_h(_token("w"))) as client:
        r = client.post("/api/v1/ask", json={"question": "what about the price?"})
        miss = client.post("/api/v1/ask", json={"question": "zebra giraffe"})
    assert r.status_code == 200
    assert r.json()["answer"] == "Twelve dollars [M1 00:10]."
    assert r.json()["sources"][0]["title"] == "Pricing Review"
    assert miss.status_code == 502


def test_new_audio_formats_accepted_with_valid_signatures(app):
    import io

    samples = {
        "rec.webm": b"\x1a\x45\xdf\xa3" + b"\x00" * 50,
        "rec.ogg": b"OggS" + b"\x00" * 50,
        "rec.flac": b"fLaC" + b"\x00" * 50,
        "rec.mp4": b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 50,
    }
    with TestClient(app, headers=_h(_token("a"))) as client:
        for name, content in samples.items():
            r = client.post("/api/v1/meetings", files={"audio": (name, io.BytesIO(content), "application/octet-stream")},
                            headers={"x-forwarded-for": f"198.51.100.{len(name)}{hash(name) % 200}"})
            assert r.status_code == 201, (name, r.text)
        bad = client.post("/api/v1/meetings", files={"audio": ("fake.webm", io.BytesIO(b"not webm at all"), "x")},
                          headers={"x-forwarded-for": "198.51.100.250"})
    assert bad.status_code == 422


def test_end_to_end_upload_to_completed_with_real_ffmpeg(app, tmp_path, monkeypatch):
    """Upload real audio through the API; the worker compresses it, 'transcribes'
    it (fake provider), 'summarizes' it, saves everything, and removes the audio."""
    from backend.app.services import transcription_service
    from tests.test_audio_pipeline import _FakeClient

    wav = tmp_path / "meeting.wav"
    subprocess.run([audio_service.ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
                    str(wav)], check=True)

    monkeypatch.setattr(config, "WORKER_CONCURRENCY", 1)
    monkeypatch.setattr(config, "CHUNK_SECONDS", 5)
    monkeypatch.setattr(transcription_service, "Groq", _FakeClient)
    monkeypatch.setattr(processing_service, "summarize", lambda t: {
        "title": "Tone Test", "summary": "A tone.", "key_decisions": ["Keep the tone"],
        "action_items": [], "open_questions": []})

    with TestClient(app, headers=_h(_token("e"))) as client:
        with wav.open("rb") as f:
            r = client.post("/api/v1/meetings", files={"audio": ("meeting.wav", f, "audio/wav")},
                            headers={"x-forwarded-for": "192.0.2.77"})
        assert r.status_code == 201
        mid = r.json()["id"]

        seen_stages = set()
        deadline = time.time() + 20
        status = {}
        while time.time() < deadline:
            status = client.get(f"/api/v1/meetings/{mid}/status").json()
            seen_stages.add(status.get("stage"))
            if status["status"] in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.05)
        assert status["status"] == "COMPLETED", status

        body = client.get(f"/api/v1/meetings/{mid}").json()
        assert body["title"] == "Tone Test"
        assert [s["start"] for s in body["segments"]] == [0.0, 5.0, 10.0]
        assert body["audio_seconds"] == 15.0
        assert client.get("/api/v1/meetings?q=tone", headers=_h(_token("e"))).json()[0]["id"] == mid

    assert not list((tmp_path / "audio").glob("[!.]*"))  # audio deleted after processing
