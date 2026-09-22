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


def test_health_endpoint_supports_head(tmp_path, monkeypatch):
    # Uptime monitors (e.g. UptimeRobot) default to HEAD requests. A plain
    # @router.get(...) doesn't guarantee HEAD support depends on the
    # framework version — confirmed live that it didn't here, which made
    # the monitor report the service as down every single check even
    # though GET worked fine. api_route with explicit methods fixes it.
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")

    from backend.app.main import app

    with TestClient(app) as client:
        response = client.head("/health")

    assert response.status_code == 200


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


def test_cors_headers_present_when_allowed_origins_configured(tmp_path, monkeypatch):
    # CORSMiddleware is only added at app-construction time based on
    # config.ALLOWED_ORIGINS, so exercising both branches means reloading
    # main after patching the config value, then reloading again afterward
    # so later tests see the default (no CORS) app.
    import importlib

    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://example-frontend.vercel.app"])

    main_module = importlib.import_module("backend.app.main")
    importlib.reload(main_module)
    try:
        with TestClient(main_module.app) as client:
            response = client.get("/health", headers={"Origin": "https://example-frontend.vercel.app"})
        assert response.headers.get("access-control-allow-origin") == "https://example-frontend.vercel.app"
    finally:
        monkeypatch.setattr(config, "ALLOWED_ORIGINS", [])
        importlib.reload(main_module)


def test_no_cors_headers_when_allowed_origins_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test.db")

    from backend.app.main import app

    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "https://example-frontend.vercel.app"})

    assert "access-control-allow-origin" not in response.headers
