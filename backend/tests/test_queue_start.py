from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from starlette.requests import Request

from app import main
from app.queue_rules import SCHEDULER_TIMEZONE, intervals_conflict, schedule_absolute


def test_starting_second_request_defers_and_cascades(monkeypatch, tmp_path) -> None:
    database = tmp_path / "queue-start.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY,
            production_points INTEGER NOT NULL,
            status TEXT NOT NULL,
            designer_email TEXT,
            scheduled_date TEXT,
            scheduled_start_minutes INTEGER,
            actual_started_at TEXT,
            completed_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE queue_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            actor_email TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    local_now = datetime.now(SCHEDULER_TIMEZONE)
    current_slot = (local_now.hour * 60 + local_now.minute) // 10 * 10
    active_start = max(0, current_slot - 20)
    rows = [
        (1, 3, "in_progress", "pd@example.com", local_now.date().isoformat(), active_start, "2026-01-01T00:00:00+00:00", None, ""),
        (2, 3, "scheduled", "pd@example.com", local_now.date().isoformat(), current_slot, None, None, ""),
        (3, 3, "scheduled", "pd@example.com", local_now.date().isoformat(), current_slot + 10, None, None, ""),
    ]
    conn.executemany("INSERT INTO queue_requests VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    @contextmanager
    def isolated_connect():
        value = sqlite3.connect(database)
        value.row_factory = sqlite3.Row
        try:
            yield value
            value.commit()
        finally:
            value.close()

    monkeypatch.setattr(main, "connect", isolated_connect)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request.state.user_email = "pd@example.com"
    request.state.operating_roles = ["pd"]
    request.state.is_admin = False

    result = main.dashboard_queue_v2_start(2, request)
    assert result["ok"] is True
    assert result["deferred"] is True

    with isolated_connect() as check:
        saved = [dict(row) for row in check.execute("SELECT * FROM queue_requests ORDER BY id").fetchall()]
    assert saved[0]["status"] == "in_progress"
    assert saved[1]["status"] == "scheduled"
    assert saved[1]["actual_started_at"] is None
    target_start = schedule_absolute(saved[1]["scheduled_date"], saved[1]["scheduled_start_minutes"])
    following_start = schedule_absolute(saved[2]["scheduled_date"], saved[2]["scheduled_start_minutes"])
    assert target_start >= schedule_absolute(local_now.date().isoformat(), current_slot) + 10
    assert following_start >= target_start + 30
    assert not intervals_conflict(target_start, 30, following_start, 30)
