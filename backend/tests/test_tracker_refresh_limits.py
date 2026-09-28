import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main, tracker_refresh_queue


def _request(email="sergio@example.com", *, is_admin=False, is_dev=False, preview=False):
    return SimpleNamespace(state=SimpleNamespace(
        user_email=email, is_admin=is_admin, is_dev=is_dev, queue_role_preview_active=preview,
    ))


@pytest.fixture
def last_request(monkeypatch):
    times = {}
    monkeypatch.setattr(main, "last_tracker_refresh_request", lambda email: times.get(email))
    monkeypatch.setattr(main, "list_accounts", lambda active_only=True: [{"handle": "openai"}])
    queued = []
    monkeypatch.setattr(main, "enqueue_tracker_refresh", lambda **job: queued.append(job) or {"job_id": "j1", **job})
    return SimpleNamespace(times=times, queued=queued)


def test_every_user_can_refresh_once(last_request):
    result = main.tracker_account_refresh("@OpenAI", _request())

    assert result["handle"] == "openai"
    assert last_request.queued[0]["requested_by"] == "sergio@example.com"


def test_members_wait_an_hour_between_manual_refreshes(last_request):
    last_request.times["sergio@example.com"] = datetime.now(UTC) - timedelta(minutes=20)

    with pytest.raises(HTTPException) as error:
        main.tracker_snapshot_now(_request())

    assert error.value.status_code == 429
    assert "40 minutes" in error.value.detail
    assert 2300 < int(error.value.headers["Retry-After"]) <= 2400
    assert not last_request.queued

    last_request.times["sergio@example.com"] = datetime.now(UTC) - timedelta(minutes=61)
    main.tracker_snapshot_now(_request())
    assert last_request.queued[0]["kind"] == "all"


def test_admins_wait_five_minutes(last_request):
    last_request.times["admin@example.com"] = datetime.now(UTC) - timedelta(minutes=2)
    with pytest.raises(HTTPException) as error:
        main.tracker_account_refresh("openai", _request("admin@example.com", is_admin=True))
    assert error.value.status_code == 429

    last_request.times["admin@example.com"] = datetime.now(UTC) - timedelta(minutes=6)
    main.tracker_account_refresh("openai", _request("admin@example.com", is_admin=True))
    assert len(last_request.queued) == 1


def test_dev_is_unlimited_unless_previewing_another_role(last_request):
    last_request.times["dev@example.com"] = datetime.now(UTC)
    for _ in range(3):
        main.tracker_account_refresh("openai", _request("dev@example.com", is_dev=True))
    assert len(last_request.queued) == 3
    assert main.tracker_refresh_allowance(_request("dev@example.com", is_dev=True))["unlimited"] is True

    with pytest.raises(HTTPException):
        main.tracker_account_refresh("openai", _request("dev@example.com", is_dev=True, preview=True))


def test_allowance_reports_the_remaining_wait(last_request):
    last_request.times["sergio@example.com"] = datetime.now(UTC) - timedelta(minutes=50)

    allowance = main.tracker_refresh_allowance(_request())

    assert allowance["unlimited"] is False
    assert allowance["windowSeconds"] == 3600
    assert 500 < allowance["retryAfterSeconds"] <= 600


def test_last_requested_at_reads_the_newest_job_per_person(tmp_path, monkeypatch):
    path = tmp_path / "tracker.sqlite"

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(tracker_refresh_queue.db, "connect", connect)
    monkeypatch.setattr(tracker_refresh_queue, "_WAKE", SimpleNamespace(set=lambda: None))
    assert tracker_refresh_queue.last_requested_at("sergio@example.com") is None
    tracker_refresh_queue.enqueue(kind="account", handle="openai", requested_by="sergio@example.com")

    last = tracker_refresh_queue.last_requested_at("sergio@example.com")

    assert last is not None and (datetime.now(UTC) - last).total_seconds() < 5
    assert tracker_refresh_queue.last_requested_at("other@example.com") is None
