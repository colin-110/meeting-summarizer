import io

from fastapi.testclient import TestClient

from backend.app.core import config
from backend.app.database.init_db import init_db
from backend.app.database.meetings_repo import create_meeting, save_summary, save_transcript, update_status
from backend.app.models.meeting import MeetingStatus


def _app(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")

    from backend.app.main import app

    return app


def _seed_completed_meeting(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    monkeypatch.setattr(config, "DATABASE_PATH", db_path)

    meeting = create_meeting("standup.mp3", "hash1", "audio/standup.mp3")
    save_transcript(meeting.id, "hello team")
    save_summary(meeting.id, "Quick sync.", ["Ship Friday"], [{"task": "Write notes", "assignee": "Sam", "deadline": None}])
    update_status(meeting.id, MeetingStatus.COMPLETED)
    return meeting.id


def test_get_unknown_meeting_returns_404(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        response = client.get("/api/v1/meetings/does-not-exist")

    assert response.status_code == 404
    assert "does-not-exist" in response.json()["detail"]


def test_get_unknown_meeting_status_returns_404(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        response = client.get("/api/v1/meetings/does-not-exist/status")

    assert response.status_code == 404


def test_get_meeting_detail_returns_full_result(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    meeting_id = _seed_completed_meeting(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/meetings/{meeting_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == MeetingStatus.COMPLETED
    assert body["transcript"] == "hello team"
    assert body["summary"] == "Quick sync."
    assert body["key_decisions"] == ["Ship Friday"]
    assert body["action_items"][0]["task"] == "Write notes"
    assert body["action_items"][0]["deadline"] is None


def test_get_meeting_status_returns_lightweight_result(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    meeting_id = _seed_completed_meeting(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/meetings/{meeting_id}/status")

    assert response.status_code == 200
    body = response.json()
    assert body == {"id": meeting_id, "status": MeetingStatus.COMPLETED, "error_message": None}


def test_list_meetings_returns_all(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    _seed_completed_meeting(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/meetings")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["filename"] == "standup.mp3"
