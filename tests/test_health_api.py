from fastapi.testclient import TestClient

from backend.app.core import config


def test_health_endpoint_reports_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")

    from backend.app.main import app

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_startup_recovers_meetings_stuck_in_processing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")

    from backend.app.database.init_db import init_db
    from backend.app.database.meetings_repo import create_meeting, get_meeting, update_status
    from backend.app.models.meeting import MeetingStatus

    init_db(config.DATABASE_PATH)
    stuck = create_meeting("call.mp3", "hash1", "audio/call.mp3")
    update_status(stuck.id, MeetingStatus.PROCESSING)

    from backend.app.main import app

    with TestClient(app):
        pass  # lifespan startup should sweep the stuck meeting to FAILED

    recovered = get_meeting(stuck.id)
    assert recovered.status == MeetingStatus.FAILED
    assert "restart" in recovered.error_message
