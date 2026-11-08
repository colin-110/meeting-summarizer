import time

from backend.app.core import config
from backend.app.database.init_db import init_db
from backend.app.database.meetings_repo import (
    claim_next,
    create_meeting,
    delete_meeting,
    get_meeting,
    list_meetings,
    purge_older_than,
    defer,
    renew_lease,
    save_summary,
    save_transcript,
    update_status,
)
from backend.app.models.meeting import MeetingStatus
from backend.app.services.worker import WorkerPool


def _db(tmp_path, monkeypatch):
    path = tmp_path / "q.db"
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    init_db(path)


def _queued(name, h="h", owner="o"):
    m = create_meeting(name, h, f"a/{name}", owner_id=owner)
    update_status(m.id, MeetingStatus.QUEUED)
    return m


def test_claim_next_takes_oldest_queued_once(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    first = _queued("one.mp3", "h1")
    second = _queued("two.mp3", "h2")

    assert claim_next() == first.id
    assert claim_next() == second.id
    assert claim_next() is None

    claimed = get_meeting(first.id)
    assert claimed.status == MeetingStatus.PROCESSING
    assert claimed.attempts == 1


def _expire_lease(meeting_id):
    from backend.app.database.connection import get_connection

    conn = get_connection()
    conn.execute("UPDATE meetings SET lease_until = 0 WHERE id = ?", (meeting_id,))
    conn.commit()
    conn.close()


def test_expired_lease_is_reclaimed_then_abandoned_after_max_attempts(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MAX_JOB_ATTEMPTS", 3)
    m = _queued("one.mp3")

    assert claim_next() == m.id
    assert claim_next() is None  # leased: nobody else can take it

    for expected_attempts in (2, 3):
        _expire_lease(m.id)  # its worker "died"
        assert claim_next() == m.id
        assert get_meeting(m.id).attempts == expected_attempts

    _expire_lease(m.id)
    assert claim_next() is None
    assert get_meeting(m.id).status == MeetingStatus.FAILED


def test_heartbeat_keeps_a_running_job_leased(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    m = _queued("one.mp3")
    claim_next()
    _expire_lease(m.id)
    renew_lease(m.id)
    assert claim_next() is None


def test_deferred_job_waits_and_does_not_burn_an_attempt(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    m = _queued("one.mp3")
    claim_next()
    defer(m.id, 60, "waiting for AI capacity")
    deferred = get_meeting(m.id)
    assert deferred.status == MeetingStatus.QUEUED and deferred.attempts == 0
    assert claim_next() is None  # not before its delay

    defer(m.id, -1, "ready")
    assert claim_next() == m.id


def test_two_processes_never_claim_the_same_job(tmp_path, monkeypatch):
    """Real OS processes racing on one database file."""
    import subprocess
    import sys

    _db(tmp_path, monkeypatch)
    ids = {_queued(f"j{i}.mp3", f"h{i}").id for i in range(40)}
    script = (
        "import sys; from backend.app.core import config; from pathlib import Path;"
        f"config.DATABASE_PATH = Path({str(tmp_path / 'q.db')!r});"
        "from backend.app.database.meetings_repo import claim_next\n"
        "out = []\n"
        "while (i := claim_next()) is not None: out.append(i)\n"
        "print(','.join(out))"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True) for _ in range(4)]
    claimed = [x for p in procs for x in p.communicate(timeout=60)[0].strip().split(",") if x]
    assert sorted(claimed) == sorted(ids)  # every job exactly once, across 4 processes


def test_worker_pool_processes_queued_jobs(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    done = []

    def fake_run(meeting_id):
        done.append(meeting_id)
        update_status(meeting_id, MeetingStatus.COMPLETED)

    monkeypatch.setattr("backend.app.services.worker.processing_service.run", fake_run)
    a, b = _queued("a.mp3", "h1"), _queued("b.mp3", "h2")

    pool = WorkerPool(2)
    pool.start()
    try:
        pool.notify()
        deadline = time.time() + 5
        while len(done) < 2 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        pool.stop()

    assert sorted(done) == sorted([a.id, b.id])  # each job ran exactly once


def test_worker_survives_a_crashing_job(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setattr("backend.app.services.worker._IDLE_POLL_SECONDS", 0.05)
    calls = []

    def flaky(meeting_id):
        calls.append(meeting_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        update_status(meeting_id, MeetingStatus.COMPLETED)

    monkeypatch.setattr("backend.app.services.worker.processing_service.run", flaky)
    _queued("a.mp3", "h1")
    _queued("b.mp3", "h2")

    pool = WorkerPool(1)
    pool.start()
    try:
        deadline = time.time() + 5
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        pool.stop()
    assert len(calls) == 2


def test_search_pagination_and_owner_scoping(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    mine = create_meeting("a.mp3", "h1", "a/a.mp3", owner_id="me")
    save_transcript(mine.id, "we talked about the quarterly budget and hiring")
    save_summary(mine.id, "Budget review.", [], [], title="Budget Planning")
    other = create_meeting("b.mp3", "h2", "a/b.mp3", owner_id="someone-else")
    save_transcript(other.id, "budget budget budget")
    save_summary(other.id, "Secret.", [], [], title="Their Budget")

    hits = list_meetings("me", query="budget")
    assert [h["id"] for h in hits] == [mine.id]
    assert "[[" in hits[0]["snippet"]
    assert list_meetings("me", query="hir")[0]["id"] == mine.id  # prefix match
    assert list_meetings("me", query='"; DROP TABLE meetings; --') == []  # safely quoted
    assert list_meetings("me", query="   ") == []

    for i in range(5):
        create_meeting(f"p{i}.mp3", f"hp{i}", "a/p.mp3", owner_id="pager")
    page1 = list_meetings("pager", limit=3, offset=0)
    page2 = list_meetings("pager", limit=3, offset=3)
    assert len(page1) == 3 and len(page2) == 2
    assert not {m["id"] for m in page1} & {m["id"] for m in page2}
    assert "transcript" not in page1[0]  # list view never loads transcripts


def test_delete_and_purge(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    m = create_meeting("a.mp3", "h1", "a/a.mp3", owner_id="me")
    save_transcript(m.id, "searchable words")
    save_summary(m.id, "s", [], [], title="t")
    update_status(m.id, MeetingStatus.COMPLETED)

    assert delete_meeting(m.id).id == m.id
    assert get_meeting(m.id) is None
    assert list_meetings("me", query="searchable") == []  # removed from the index too
    assert delete_meeting(m.id) is None

    old = create_meeting("o.mp3", "h2", "a/o.mp3", owner_id="me")
    update_status(old.id, MeetingStatus.COMPLETED)
    assert purge_older_than(0)[0].id == old.id
    assert get_meeting(old.id) is None
