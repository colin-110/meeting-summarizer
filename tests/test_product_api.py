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
        return "Two engineers [00:03]."

    monkeypatch.setattr(qa_service, "ask", fake_ask)
    with TestClient(app) as client:
        r = client.post(f"/api/v1/meetings/{mid}/ask", json={"question": "Who is being hired?"}, headers=_h(_token("a")))
        short = client.post(f"/api/v1/meetings/{mid}/ask", json={"question": "hi"}, headers=_h(_token("a")))
    assert r.status_code == 200 and r.json() == {"answer": "Two engineers [00:03]."}
    assert seen["context"] == "[00:03] We agreed to hire two engineers."
    assert short.status_code == 422


def test_ask_requires_completed_meeting_and_is_rate_limited(app, monkeypatch):
    pending = create_meeting("p.mp3", "hp", "x", owner_id=_owner(_token("r")))
    mid = _completed(_token("r"))
    monkeypatch.setattr(qa_service, "ask", lambda *a, **k: "ok")
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


def test_qa_to_markdown_handles_empty_meeting():
    from backend.app.models.meeting import Meeting

    md = qa_service.to_markdown(Meeting(id="1", filename="x.mp3", file_hash="h", audio_path="p", status="COMPLETED"))
    assert "_None recorded._" in md and "# x.mp3" in md


def test_qa_ask_builds_prompt_and_wraps_errors():
    from backend.app.models.meeting import Meeting

    meeting = Meeting(id="1", filename="x", file_hash="h", audio_path="p", status="COMPLETED",
                      transcript="t", segments=[{"start": 3725.0, "end": 3730.0, "text": "late remark"}])
    captured = {}

    class _C:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    captured.update(kw)
                    return type("R", (), {"choices": [type("Ch", (), {"message": type("M", (), {"content": " Answer. "})()})()]})()

    assert qa_service.ask(meeting, "What was said?", client=_C()) == "Answer."
    assert "[1:02:05] late remark" in captured["messages"][1]["content"]  # hour-long timestamps format correctly
    assert captured["model"] == "openai/gpt-oss-20b"

    with pytest.raises(qa_service.QAError, match="no transcript"):
        qa_service.ask(Meeting(id="2", filename="x", file_hash="h", audio_path="p", status="COMPLETED"), "q?", client=_C())


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
