"""The standalone worker process and small helpers that the API tests don't reach."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from backend.app.utils.errors import retry_after_seconds

ROOT = Path(__file__).resolve().parents[1]


def test_worker_process_starts_and_stops_cleanly_on_sigterm(tmp_path):
    env = dict(os.environ, DATA_DIR=str(tmp_path), WORKER_CONCURRENCY="2", ENV_FILE="", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.app.worker"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 20
        lines = []
        while time.time() < deadline:
            line = proc.stdout.readline()
            lines.append(line)
            if "worker process running with 2 thread(s)" in line:
                break
        else:
            raise AssertionError("worker never reported ready:\n" + "".join(lines))

        assert (tmp_path / "app.db").exists(), "the worker creates the database on start"
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0
        assert "shutting down" in out
    finally:
        if proc.poll() is None:
            proc.kill()


def _exc_with_headers(headers):
    return SimpleNamespace(response=SimpleNamespace(headers=headers))


def test_retry_after_reads_the_provider_header():
    assert retry_after_seconds(_exc_with_headers({"retry-after": "12.5"})) == 12.5


def test_retry_after_falls_back_when_missing_or_garbage():
    assert retry_after_seconds(_exc_with_headers({})) == 60.0
    assert retry_after_seconds(_exc_with_headers({"retry-after": "soon"}), default=5) == 5
    assert retry_after_seconds(RuntimeError("no response attached")) == 60.0
