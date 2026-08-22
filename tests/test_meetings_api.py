import io

from fastapi.testclient import TestClient

from backend.app.core import config
from backend.app.models.meeting import MeetingStatus


def _app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")

    from backend.app.main import app

    return app


def test_upload_valid_audio_creates_meeting(tmp_path, monkeypatch):
    # The background task fires synchronously inside TestClient and would try
    # a real Groq() client if a key were present in the environment. Force it
    # unset so this stays hermetic regardless of the developer's local .env.
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    content = b"ID3" + b"\x00" * 100

    with TestClient(_app(tmp_path, monkeypatch)) as client:
        response = client.post(
            "/api/v1/meetings",
            files={"audio": ("standup.mp3", io.BytesIO(content), "audio/mpeg")},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == MeetingStatus.QUEUED
    assert "id" in body


def test_upload_rejects_unsupported_type(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        response = client.post(
            "/api/v1/meetings",
            files={"audio": ("notes.exe", io.BytesIO(b"whatever"), "application/octet-stream")},
        )

    assert response.status_code == 422
    assert "Unsupported file type" in response.json()["detail"]


def test_upload_rejects_missing_file(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        response = client.post("/api/v1/meetings")

    assert response.status_code == 422
