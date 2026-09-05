import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app import config, db, main, media_backfill, scheduler


@pytest.fixture
def isolated_database(monkeypatch, tmp_path):
    path = tmp_path / "resilience.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE scheduler_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
            "CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, cover_image_path TEXT);"
            "CREATE TABLE posts (id INTEGER PRIMARY KEY, image_path TEXT);"
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, avatar_path TEXT);"
        )

    @contextmanager
    def connect():
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(media_backfill, "connect", connect)
    return connect


def test_scheduler_claim_has_one_winner(isolated_database):
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: scheduler._claim_bucket("hour", "2026-09-04T15"), range(24)))
    assert sum(results) == 1
    assert not scheduler._claim_bucket("hour", "2026-09-04T14")
    assert scheduler._claim_bucket("hour", "2026-09-04T16")


def test_scheduler_uses_requested_45_minute_window():
    from datetime import datetime, timedelta, timezone

    cst = timezone(timedelta(hours=-6))
    assert scheduler._bucket_key(datetime(2026, 9, 5, 6, 15, tzinfo=cst)) == "2026-09-05T06:15"
    assert scheduler._bucket_key(datetime(2026, 9, 5, 7, 0, tzinfo=cst)) == "2026-09-05T07:00"
    assert scheduler._bucket_key(datetime(2026, 9, 5, 23, 30, tzinfo=cst)) == "2026-09-05T23:30"
    assert scheduler._bucket_key(datetime(2026, 9, 6, 0, 0, tzinfo=cst)) == "2026-09-06T00:00"
    assert scheduler._bucket_key(datetime(2026, 9, 5, 6, 14, tzinfo=cst)) == "2026-09-05T06:00"


def test_scheduler_never_runs_paid_jobs_without_durable_claim(monkeypatch):
    def unavailable():
        raise RuntimeError("database unavailable")

    jobs = []
    monkeypatch.setattr(db, "connect", unavailable)
    monkeypatch.setattr(scheduler, "_run_media_backfill", lambda now: None)
    for name in ("_run_short_term_jobs", "_run_daily_jobs", "_run_ocr_job", "_run_account_snapshot_job"):
        monkeypatch.setattr(scheduler, name, lambda: jobs.append("ran"))
    with pytest.raises(RuntimeError):
        scheduler._tick()
    with pytest.raises(RuntimeError):
        scheduler._state_get("hour")
    with pytest.raises(RuntimeError):
        scheduler._state_set("hour", "value")
    assert jobs == []


def test_scheduler_respects_global_disable(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setenv("SENTIENT_SCHEDULER_ENABLED", "true")
    assert not scheduler.scheduler_enabled()


def test_scheduler_cannot_restart_while_old_thread_is_stopping(monkeypatch):
    class BusyThread:
        def join(self, timeout):
            pass

        def is_alive(self):
            return True

    thread = BusyThread()
    monkeypatch.setattr(scheduler, "_thread", thread)
    monkeypatch.setattr(scheduler, "_started", True)
    monkeypatch.setattr(scheduler, "_stop_event", threading.Event())
    scheduler.stop_scheduler(timeout=0)
    assert scheduler._thread is thread
    assert scheduler._started
    assert scheduler._stop_event.is_set()


def test_live_subscribers_share_one_nonblocking_database_read(monkeypatch):
    calls = []
    main_thread = threading.get_ident()

    def read():
        calls.append(threading.get_ident())
        time.sleep(0.05)
        return {"revision": len(calls)}

    async def scenario():
        monkeypatch.setattr(main, "_queue_stream_lock", asyncio.Lock())
        monkeypatch.setattr(main, "_queue_stream_state", None)
        monkeypatch.setattr(main, "_queue_stream_checked_at", 0)
        monkeypatch.setattr(main, "_read_queue_stream_state", read)
        pending = [asyncio.create_task(main._queue_v2_stream_snapshot()) for _ in range(40)]
        await asyncio.sleep(0.01)
        assert not pending[0].done()
        assert (await main.health())["ok"]
        results = await asyncio.gather(*pending)
        assert all(result["revision"] == 1 for result in results)
        assert len(calls) == 1
        assert calls[0] != main_thread
        monkeypatch.setattr(main, "_queue_stream_checked_at", time.monotonic() - 1)
        assert (await main._queue_v2_stream_snapshot())["revision"] == 2

    asyncio.run(scenario())


def test_auth_database_and_usage_work_do_not_block_event_loop(monkeypatch):
    calls = []
    main_thread = threading.get_ident()

    def record(result):
        def callback(*args):
            calls.append(threading.get_ident())
            return result
        return callback

    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", record({"email": "pd@example.com", "uid": "test"}))
    monkeypatch.setattr(main, "get_dashboard_user_access", record({
        "is_admin": False, "operating_role": "pd", "operating_roles": '["pd"]', "time_zone": "America/Bogota",
    }))
    monkeypatch.setattr(main, "set_dashboard_user_time_zone", record(None))
    monkeypatch.setattr(main, "log_usage_event", record(None))
    request = Request({
        "type": "http", "method": "GET", "path": "/api/dashboard/posts",
        "headers": [(b"authorization", b"Bearer test"), (b"x-sentient-time-zone", b"America/Costa_Rica")],
    })

    async def call_next(request):
        return Response(status_code=200)

    response = asyncio.run(main._require_firebase_user(request, call_next))
    assert response.status_code == 200
    assert len(calls) == 4
    assert all(thread != main_thread for thread in calls)


def test_backfill_preserves_concurrent_media_updates(monkeypatch, isolated_database):
    with isolated_database() as connection:
        connection.execute("INSERT INTO dashboard_posts VALUES (1, 'old.jpg')")

    def upload(reference):
        with isolated_database() as connection:
            connection.execute("UPDATE dashboard_posts SET cover_image_path = 'new.jpg' WHERE id = 1")
        return "r2://uploads/old.jpg"

    monkeypatch.setattr(media_backfill, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_backfill, "upload_legacy_local_media", upload)
    monkeypatch.setattr(media_backfill, "init_db", lambda: pytest.fail("recurring batches must not run migrations"))
    result = media_backfill.backfill(1, dry_run=False)
    assert result == {"scanned": 1, "uploaded": 0, "skipped": 1, "failed": 0}
    with isolated_database() as connection:
        assert connection.execute("SELECT cover_image_path FROM dashboard_posts").fetchone()[0] == "new.jpg"


def test_backfill_continues_after_one_upload_fails(monkeypatch, isolated_database):
    with isolated_database() as connection:
        connection.executemany("INSERT INTO dashboard_posts VALUES (?, ?)", [(1, "bad.jpg"), (2, "good.jpg")])

    def upload(reference):
        if reference == "bad.jpg":
            raise OSError("upload failed")
        return "r2://uploads/good.jpg"

    monkeypatch.setattr(media_backfill, "r2_enabled", lambda: True)
    monkeypatch.setattr(media_backfill, "upload_legacy_local_media", upload)
    assert media_backfill.backfill(2, dry_run=False) == {"scanned": 2, "uploaded": 1, "skipped": 0, "failed": 1}
