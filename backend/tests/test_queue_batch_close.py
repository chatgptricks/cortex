from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest
from starlette.requests import Request

from app import main


def test_admin_can_batch_close_completed_requests_without_links(monkeypatch, tmp_path) -> None:
    database = tmp_path / "queue-batch-close.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY, status TEXT NOT NULL, final_permalink TEXT,
            final_permalinks TEXT, closed_at TEXT, updated_at TEXT
        );
        CREATE TABLE queue_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
            actor_email TEXT NOT NULL, event_type TEXT NOT NULL, details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE queue_live_state (
            id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, event_type TEXT NOT NULL,
            actor_email TEXT NOT NULL, request_ids TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO queue_live_state VALUES (1, 0, '', '', '[]', '');
        INSERT INTO queue_requests (id, status) VALUES (1, 'completed'), (2, 'completed'), (3, 'scheduled');
        """
    )
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
    request.state.user_email = "admin@example.com"
    request.state.operating_roles = ["admin"]
    request.state.is_admin = True

    result = main.dashboard_queue_v2_batch_close(request, '[1, 2, 3]')

    assert result["closed"] == [1, 2]
    assert result["skipped"] == [{"id": 3, "reason": "Only completed requests or assigned jobs scheduled before today can be force-closed.", "status": "scheduled"}]
    with isolated_connect() as check:
        rows = [dict(row) for row in check.execute("SELECT * FROM queue_requests ORDER BY id")]
        assert [row["status"] for row in rows] == ["closed", "closed", "scheduled"]
        assert all(row["final_permalink"] is None for row in rows[:2])
        assert all(row["final_permalinks"] == "[]" for row in rows[:2])
        events = check.execute("SELECT event_type FROM queue_request_events ORDER BY request_id").fetchall()
        assert [row["event_type"] for row in events] == ["closed_by_admin_batch", "closed_by_admin_batch"]


def test_batch_close_requires_admin(monkeypatch, tmp_path) -> None:
    database = tmp_path / "queue-batch-close-denied.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript("CREATE TABLE queue_requests (id INTEGER PRIMARY KEY, status TEXT); INSERT INTO queue_requests VALUES (1, 'completed');")
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

    with pytest.raises(main.HTTPException) as error:
        main.dashboard_queue_v2_batch_close(request, '[1]')
    assert error.value.status_code == 403


def test_admin_can_force_close_overdue_assigned_work_but_not_today_or_unassigned(monkeypatch, tmp_path) -> None:
    database = tmp_path / "queue-force-close-overdue.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY, status TEXT NOT NULL, designer_email TEXT,
            scheduled_date TEXT, final_permalink TEXT, final_permalinks TEXT,
            closed_at TEXT, updated_at TEXT
        );
        CREATE TABLE queue_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
            actor_email TEXT NOT NULL, event_type TEXT NOT NULL, details TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE queue_live_state (
            id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, event_type TEXT NOT NULL,
            actor_email TEXT NOT NULL, request_ids TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO queue_live_state VALUES (1, 0, '', '', '[]', '');
        INSERT INTO queue_requests (id, status, designer_email, scheduled_date) VALUES
            (1, 'scheduled', 'designer@example.com', '2020-01-01'),
            (2, 'in_progress', 'designer@example.com', '2020-01-01'),
            (3, 'scheduled', 'designer@example.com', '2099-01-01'),
            (4, 'scheduled', NULL, '2020-01-01');
        """
    )
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
    request.state.user_email = "admin@example.com"
    request.state.operating_roles = ["admin"]
    request.state.is_admin = True

    result = main.dashboard_queue_v2_batch_close(request, '[1, 2, 3, 4]')

    assert result["closed"] == [1, 2]
    assert [item["id"] for item in result["skipped"]] == [3, 4]
    with isolated_connect() as check:
        rows = [dict(row) for row in check.execute("SELECT id, status FROM queue_requests ORDER BY id")]
        assert rows == [
            {"id": 1, "status": "closed"},
            {"id": 2, "status": "closed"},
            {"id": 3, "status": "scheduled"},
            {"id": 4, "status": "scheduled"},
        ]
