import io

from fastapi.testclient import TestClient

from backend.app.api import meetings as meetings_api
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


def test_upload_is_rate_limited_per_client(tmp_path, monkeypatch):
    # Small, isolated limit so the test is fast and doesn't depend on the
    # real production threshold; a unique X-Forwarded-For value keeps this
    # test's counter separate from every other test hitting this endpoint.
    monkeypatch.setattr(meetings_api, "_UPLOAD_RATE_LIMIT", 2)
    monkeypatch.setattr(meetings_api, "_UPLOAD_RATE_WINDOW_SECONDS", 60)
    headers = {"x-forwarded-for": "203.0.113.5"}

    with TestClient(_app(tmp_path, monkeypatch)) as client:
        for _ in range(2):
            response = client.post(
                "/api/v1/meetings",
                files={"audio": ("notes.exe", io.BytesIO(b"whatever"), "application/octet-stream")},
                headers=headers,
            )
            assert response.status_code == 422  # under the limit, reaches normal validation

        blocked = client.post(
            "/api/v1/meetings",
            files={"audio": ("notes.exe", io.BytesIO(b"whatever"), "application/octet-stream")},
            headers=headers,
        )

    assert blocked.status_code == 429
    assert "Too many uploads" in blocked.json()["detail"]
