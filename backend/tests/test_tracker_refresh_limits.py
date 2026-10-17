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
    monkeypatch.setattr(main, "list_accounts", lambda active_only=True: [{"handle": "openai"}])
    queued = []
    monkeypatch.setattr(main, "enqueue_tracker_refresh", lambda **job: queued.append(job) or {"job_id": "j1", **job})
    return SimpleNamespace(times=times, queued=queued)


def test_every_user_can_refresh_once(last_request):
    result = main.tracker_account_refresh("@OpenAI", _request())

    assert result["handle"] == "openai"
    assert last_request.queued[0]["requested_by"] == "sergio@example.com"


@pytest.mark.parametrize("role", [
    {}, {"is_admin": True}, {"is_dev": True}, {"is_dev": True, "preview": True},
])
def test_every_role_can_refresh_after_previous_completion(last_request, role):
    last_request.times["sergio@example.com"] = datetime.now(UTC)
    for _ in range(3):
        main.tracker_snapshot_now(_request(**role))
        main.tracker_account_refresh("openai", _request(**role))
    assert len(last_request.queued) == 6
    assert main.tracker_refresh_allowance(_request(**role)) == {
        "unlimited": True, "windowSeconds": 0, "retryAfterSeconds": 0,
    }


def test_unknown_account_still_cannot_be_refreshed(last_request):
    with pytest.raises(HTTPException) as error:
        main.tracker_account_refresh("unknown", _request())
    assert error.value.status_code == 404
    assert last_request.queued == []


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
