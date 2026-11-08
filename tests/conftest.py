"""Test-wide setup: no background workers unless a test starts its own,
so API tests never race a real processing thread."""

import os

os.environ["WORKER_CONCURRENCY"] = "0"
os.environ.pop("GROQ_API_KEY", None)
