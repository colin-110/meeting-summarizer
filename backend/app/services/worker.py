"""Background workers that drain the SQLite-backed job queue.

The queue is just the `meetings` table: an upload inserts a QUEUED row and
workers atomically lease the oldest one (meetings_repo.claim_next). While a
job runs, a heartbeat renews its lease; if the worker dies, the lease
expires and any other worker — in this process or another — takes it over.
Because the queue lives in the database, jobs survive restarts, and workers
can run inside the web process or as separate processes
(`python -m backend.app.worker`). No Redis/Celery needed.
"""

import threading
import time

from backend.app.core import config
from backend.app.core.logging import get_logger
from backend.app.database.meetings_repo import claim_next, purge_older_than, renew_lease
from backend.app.services import processing_service

log = get_logger(__name__)

_IDLE_POLL_SECONDS = 2.0
_PURGE_INTERVAL_SECONDS = 3600


class WorkerPool:
    def __init__(self, concurrency: int):
        self._concurrency = concurrency
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_purge = 0.0

    def start(self) -> None:
        for i in range(self._concurrency):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("started %d worker thread(s)", self._concurrency)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=5)

    def notify(self) -> None:
        """Wake an idle worker right away instead of waiting for the next poll."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                meeting_id = claim_next()
                if meeting_id is None:
                    self._maybe_purge()
                    self._wake.wait(_IDLE_POLL_SECONDS)
                    self._wake.clear()
                    continue
                self._run_with_heartbeat(meeting_id)
            except Exception:
                # A bug must never kill the worker thread.
                log.exception("worker loop error")
                time.sleep(_IDLE_POLL_SECONDS)

    def _run_with_heartbeat(self, meeting_id: str) -> None:
        done = threading.Event()

        def beat() -> None:
            while not done.wait(config.HEARTBEAT_SECONDS):
                try:
                    renew_lease(meeting_id)
                except Exception:
                    log.exception("lease renewal failed id=%s", meeting_id)

        heartbeat = threading.Thread(target=beat, name=f"heartbeat-{meeting_id[:8]}", daemon=True)
        heartbeat.start()
        try:
            processing_service.run(meeting_id)
        finally:
            done.set()
            heartbeat.join(timeout=2)

    def _maybe_purge(self) -> None:
        if config.RETENTION_DAYS <= 0:
            return
        now = time.monotonic()
        if now - self._last_purge < _PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        deleted = purge_older_than(config.RETENTION_DAYS)
        for meeting in deleted:
            processing_service.release_audio(meeting, force=True)
        if deleted:
            log.info("retention purge removed %d meeting(s)", len(deleted))
