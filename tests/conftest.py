"""Test-wide setup: no background workers unless a test starts its own, and
every test gets its own empty database (shared rate limits live there)."""

import os

import pytest

os.environ["WORKER_CONCURRENCY"] = "0"
# Tests simulate different clients through X-Forwarded-For.
os.environ["TRUST_PROXY_HEADERS"] = "true"
os.environ.pop("GROQ_API_KEY", None)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path_factory, monkeypatch):
    from backend.app.core import config
    from backend.app.database.init_db import init_db

    path = tmp_path_factory.mktemp("db") / "app.db"
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    init_db(path)
