"""Browser tests: start the real server (fake AI) once, drive it with Chromium."""

import itertools
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from backend.app.services.audio_service import ffmpeg_exe

ROOT = Path(__file__).resolve().parents[2]
_client_ids = itertools.count(1)
_frequencies = itertools.count(300, 37)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url(tmp_path_factory):
    port = _free_port()
    data = tmp_path_factory.mktemp("e2e-data")
    env = dict(
        os.environ,
        DATA_DIR=str(data),
        ENV_FILE="",
        GROQ_API_KEY="fake",
        RUN_WORKERS="true",
        WORKER_CONCURRENCY="2",
        TRUST_PROXY_HEADERS="true",
    )
    with open(data / "server.log", "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tests.e2e.server", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log
        )
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/health", timeout=1)
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError((data / "server.log").read_text()) from None
                time.sleep(0.1)
        yield url
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # A fake microphone that plays a tone, and no permission prompt.
    return {
        **browser_type_launch_args,
        "args": ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
    }


@pytest.fixture
def browser_context_args(browser_context_args):
    # Each test is its own user: a new browser profile, so a new owner token.
    return {
        **browser_context_args,
        "permissions": ["microphone", "clipboard-read", "clipboard-write"],
        "accept_downloads": True,
    }


def as_client(context, base_url: str, ip: str) -> None:
    """Send this context's requests to our server from `ip` (only ours: adding
    the header to third-party requests like Google Fonts breaks their CORS)."""

    def add_ip(route):
        route.continue_(headers={**route.request.headers, "x-forwarded-for": ip})

    context.route(f"{base_url}/**", add_ip)


@pytest.fixture(autouse=True)
def _own_client_ip(request, base_url):
    if "context" in request.fixturenames:
        as_client(request.getfixturevalue("context"), base_url, f"10.0.0.{next(_client_ids)}")


@pytest.fixture
def make_audio(tmp_path):
    """A short real WAV; each frequency gives different bytes, so no dedupe across tests."""

    def make(seconds: float = 6.0) -> Path:
        out = tmp_path / f"meeting-{next(_frequencies)}.wav"
        freq = int(out.stem.split("-")[1])
        subprocess.run(
            [ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", str(out)],
            check=True,
        )
        return out

    return make
