"""Smoke tests against a deployed server, real AI included.

    BASE_URL=https://3-7-91-183.sslip.io \
    FRONTEND_URL=https://meeting-summarizer-recorder.vercel.app \
    pytest -m live

Uses one 25-second clip (tests/fixtures/speech.mp3, public domain, from
LibriVox) and deletes everything it creates.
"""

import os
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "").rstrip("/")
SPEECH = Path(__file__).resolve().parents[1] / "fixtures" / "speech.mp3"


@pytest.fixture(autouse=True)
def _needs_base_url():
    if not BASE_URL:
        pytest.skip("set BASE_URL to run smoke tests")


def _token() -> str:
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def http():
    with httpx.Client(base_url=BASE_URL, timeout=60) as client:
        yield client


@pytest.fixture(scope="module")
def meeting(http):
    """Upload the clip once, wait for the notes, delete it at the end."""
    token = _token()
    headers = {"X-Owner-Token": token}
    with SPEECH.open("rb") as f:
        r = http.post("/api/v1/meetings", headers=headers, files={"audio": ("speech.mp3", f, "audio/mpeg")})
    assert r.status_code == 201, r.text
    meeting_id = r.json()["id"]
    deadline = time.time() + 180
    while time.time() < deadline:
        status = http.get(f"/api/v1/meetings/{meeting_id}/status", headers=headers).json()["status"]
        if status in ("COMPLETED", "FAILED"):
            break
        time.sleep(2)
    detail = http.get(f"/api/v1/meetings/{meeting_id}", headers=headers).json()
    yield {"id": meeting_id, "headers": headers, "detail": detail}
    http.delete(f"/api/v1/meetings/{meeting_id}", headers=headers)


def test_health(http):
    assert http.get("/health").json() == {"status": "ok", "database": "ok"}
    assert http.head("/health").status_code == 200  # uptime monitors use HEAD


def test_frontend_is_served_by_the_api_too(http):
    r = http.get("/")
    assert r.status_code == 200
    assert "<title>Meeting Summarizer</title>" in r.text


def test_plain_http_redirects_to_https():
    if not BASE_URL.startswith("https://"):
        pytest.skip("not an HTTPS deployment")
    r = httpx.get("http://" + BASE_URL.removeprefix("https://") + "/health", follow_redirects=False, timeout=30)
    assert r.status_code in (301, 302, 307, 308)
    assert r.headers["location"].startswith("https://")


def test_cors_allows_only_the_frontend(http):
    if not FRONTEND_URL:
        pytest.skip("set FRONTEND_URL to check CORS")
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-owner-token"}
    ok = http.options("/api/v1/meetings", headers={"Origin": FRONTEND_URL, **preflight})
    assert ok.headers.get("access-control-allow-origin") == FRONTEND_URL
    evil = http.options("/api/v1/meetings", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in evil.headers


def test_vercel_frontend_points_at_this_api():
    if not FRONTEND_URL:
        pytest.skip("set FRONTEND_URL to check the frontend")
    page = httpx.get(FRONTEND_URL + "/", timeout=30)
    assert page.status_code == 200 and "/config.js" in page.text
    config = httpx.get(FRONTEND_URL + "/config.js", timeout=30).text
    assert BASE_URL in config


def test_requests_without_a_valid_token_are_rejected(http):
    assert http.get("/api/v1/meetings").status_code == 400
    assert http.get("/api/v1/meetings", headers={"X-Owner-Token": "short"}).status_code == 400


def test_rejects_a_file_that_is_not_audio(http):
    r = http.post(
        "/api/v1/meetings",
        headers={"X-Owner-Token": _token()},
        files={"audio": ("notes.mp3", b"this is not audio at all", "audio/mpeg")},
    )
    assert r.status_code in (400, 415, 422)


def test_real_upload_produces_notes(meeting):
    d = meeting["detail"]
    assert d["status"] == "COMPLETED", d.get("error_message")
    assert d["title"].strip()
    assert len(d["summary"]) > 20
    assert len(d["segments"]) >= 3
    assert d["usage"]["audio_seconds"] == pytest.approx(25, abs=1.5)


def test_real_question_gets_an_answer_with_sources(http, meeting):
    r = http.post(
        f"/api/v1/meetings/{meeting['id']}/ask",
        headers=meeting["headers"],
        json={"question": "What is this about?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["answer"]) > 10
    assert body["sources"]


def test_export_is_markdown(http, meeting):
    r = http.get(f"/api/v1/meetings/{meeting['id']}/export", headers=meeting["headers"])
    assert r.status_code == 200
    assert r.text.startswith("# ")


def test_another_user_cannot_see_the_meeting(http, meeting):
    stranger = {"X-Owner-Token": _token()}
    assert http.get(f"/api/v1/meetings/{meeting['id']}", headers=stranger).status_code == 404
    assert http.delete(f"/api/v1/meetings/{meeting['id']}", headers=stranger).status_code == 404
    assert http.get("/api/v1/meetings", headers=stranger).json() == []


def test_unchanged_meeting_is_answered_304(http, meeting):
    first = http.get(f"/api/v1/meetings/{meeting['id']}", headers=meeting["headers"])
    etag = first.headers["etag"]
    again = http.get(f"/api/v1/meetings/{meeting['id']}", headers={**meeting["headers"], "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
