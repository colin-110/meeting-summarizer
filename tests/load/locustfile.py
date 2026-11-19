"""Load test: what browsers do most, without spending AI quota.

Upload one meeting first and pass it in, so polling has something to poll:

    MEETING_ID=... OWNER_TOKEN=... locust -f tests/load/locustfile.py \
        --host https://3-7-91-183.sslip.io --headless -u 50 -r 10 -t 60s

Uploads and questions are left out on purpose: they're rate-limited per IP
and bounded by the AI provider, not by this server.
"""

import os
import random

from locust import HttpUser, between, task

MEETING_ID = os.environ["MEETING_ID"]
TOKEN = os.environ["OWNER_TOKEN"]
SEARCHES = ["budget", "money", "painter", "beggar", "studio", "friend"]


class Browser(HttpUser):
    wait_time = between(1, 3)  # roughly the UI's 2-second polling

    def on_start(self):
        self.client.headers["X-Owner-Token"] = TOKEN
        self.etag = None

    @task(6)
    def poll_status(self):
        self.client.get(f"/api/v1/meetings/{MEETING_ID}/status", name="/status")

    @task(3)
    def open_meeting(self):
        headers = {"If-None-Match": self.etag} if self.etag else {}
        with self.client.get(
            f"/api/v1/meetings/{MEETING_ID}", headers=headers, name="/meeting", catch_response=True
        ) as r:
            if r.status_code in (200, 304):
                self.etag = r.headers.get("etag", self.etag)
                r.success()

    @task(2)
    def search(self):
        self.client.get("/api/v1/meetings", params={"q": random.choice(SEARCHES)}, name="/search")

    @task(1)
    def history(self):
        self.client.get("/api/v1/meetings", params={"limit": 20}, name="/history")

    @task(1)
    def page(self):
        self.client.get("/", name="/ (page)")
