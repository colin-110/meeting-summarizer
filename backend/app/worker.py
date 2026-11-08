"""Standalone worker process: `python -m backend.app.worker`.

Runs only the job-queue workers (no web server), so processing can be scaled
separately from the API — e.g. one web container plus N worker containers
sharing the same database volume. Set RUN_WORKERS=false on the web process
when using this.
"""

import signal
import threading

from backend.app.core import config
from backend.app.core.logging import configure_logging, get_logger
from backend.app.database.init_db import init_db
from backend.app.services.worker import WorkerPool


def main() -> None:
    configure_logging()
    log = get_logger("backend.app.worker")
    init_db()
    concurrency = max(1, config.WORKER_CONCURRENCY)
    pool = WorkerPool(concurrency)
    pool.start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker process running with %d thread(s)", concurrency)
    stop.wait()
    log.info("shutting down; any in-flight job's lease will expire and be retried")
    pool.stop()


if __name__ == "__main__":
    main()
