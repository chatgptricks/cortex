from __future__ import annotations

import sqlite3
import pytest
from contextlib import contextmanager
from datetime import datetime

from starlette.requests import Request

from app import main
from app.queue_rules import SCHEDULER_TIMEZONE


@pytest.mark.parametrize("role", ["pd", "sales", "vc", "trainee", "admin", "dev"])
def test_multiple_active_requests_can_start_and_complete_independently(monkeypatch, tmp_path, role) -> None:
    database = tmp_path / "queue-start.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY,
            production_points INTEGER NOT NULL,
            minutes_per_pp INTEGER NOT NULL DEFAULT 10,
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
        CREATE TABLE queue_schedule_drafts (
            request_id INTEGER PRIMARY KEY,
            coordinator_email TEXT NOT NULL,
            designer_email TEXT NOT NULL,
            scheduled_date TEXT NOT NULL,
            scheduled_start_minutes INTEGER NOT NULL,
            recommended_accounts TEXT NOT NULL DEFAULT '[]',
            production_points INTEGER,
            minutes_per_pp INTEGER,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE queue_tickets (
            id INTEGER PRIMARY KEY, ticket_type TEXT, requester_email TEXT, status TEXT,
            scheduled_date TEXT, scheduled_start_minutes INTEGER, duration_minutes INTEGER
        );
        CREATE TABLE queue_live_state (
            id INTEGER PRIMARY KEY,
            revision INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            actor_email TEXT NOT NULL,
            request_ids TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO queue_live_state VALUES (1, 0, '', '', '[]', '');
        """
    )
    local_now = datetime.now(SCHEDULER_TIMEZONE)
    current_slot = (local_now.hour * 60 + local_now.minute) // 10 * 10
    active_start = max(0, current_slot - 20)
    rows = [
        (1, 3, 10, "in_progress", "pd@example.com", local_now.date().isoformat(), active_start, "2026-01-01T00:00:00+00:00", None, ""),
        (2, 3, 10, "scheduled", "pd@example.com", local_now.date().isoformat(), current_slot, None, None, ""),
        (3, 3, 10, "scheduled", "pd@example.com", local_now.date().isoformat(), current_slot + 10, None, None, ""),
    ]
    conn.executemany("INSERT INTO queue_requests VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executescript("""
        ALTER TABLE queue_requests ADD COLUMN recommended_accounts TEXT DEFAULT '["chatgptips", "planet.ai_"]';
        ALTER TABLE queue_requests ADD COLUMN final_permalink TEXT;
        ALTER TABLE queue_requests ADD COLUMN final_permalinks TEXT;
        ALTER TABLE queue_requests ADD COLUMN closed_at TEXT;
        ALTER TABLE queue_tickets ADD COLUMN request_id INTEGER;
    """)
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
    request.state.operating_roles = [role]
    request.state.is_admin = role == "admin"
    request.state.is_dev = role == "dev"

    result = main.dashboard_queue_v2_start(2, request)
    assert result["ok"] is True
    assert result["deferred"] is False
    assert main.dashboard_queue_v2_start(3, request)["deferred"] is False
    with isolated_connect() as check:
        saved = [dict(row) for row in check.execute("SELECT * FROM queue_requests ORDER BY id").fetchall()]
    assert all(row["status"] == "in_progress" for row in saved)
    assert all(row["actual_started_at"] for row in saved)
    assert saved[0]["actual_started_at"] == rows[0][7]
    starts = [int(row["scheduled_start_minutes"]) for row in saved]
    assert starts == sorted(starts)
    assert starts[1] >= starts[0] + 3 * 10 + 10
    assert starts[2] >= starts[1] + 3 * 10 + 10
    main.dashboard_queue_v2_complete(2, request)
    with isolated_connect() as check:
        assert [row["status"] for row in check.execute("SELECT * FROM queue_requests ORDER BY id")] == ["in_progress", "completed", "in_progress"]
        planned = dict(check.execute("SELECT scheduled_date, scheduled_start_minutes FROM queue_requests WHERE id = 2").fetchone())
    restarted = main.dashboard_queue_v2_start(2, request, move_to_now=False)
    assert restarted["deferred"] is False
    assert restarted["movedToNow"] is False
    assert (restarted["scheduledDate"], restarted["scheduledStartMinutes"]) == (planned["scheduled_date"], planned["scheduled_start_minutes"])
    main.dashboard_queue_v2_complete(2, request)
    with pytest.raises(main.HTTPException) as missing_links:
        main.dashboard_queue_v2_close(2, request, final_permalink="https://instagram.com/p/ONE/")
    assert missing_links.value.status_code == 400
    links = '[{"account":"chatgptips","url":"https://instagram.com/p/ONE/"},{"account":"planet.ai_","url":"https://instagram.com/reel/TWO/"}]'
    if role == "trainee":
        with pytest.raises(main.HTTPException) as approval:
            main.dashboard_queue_v2_close(2, request, final_permalinks=links)
        assert approval.value.status_code == 409
        with isolated_connect() as check:
            check.execute("INSERT INTO queue_tickets (id, ticket_type, request_id, status) VALUES (1, 'trainee_review', 2, 'approved')")
    assert main.dashboard_queue_v2_close(2, request, final_permalinks=links)["ok"]
    with isolated_connect() as check:
        assert [row["status"] for row in check.execute("SELECT * FROM queue_requests ORDER BY id")] == ["in_progress", "closed", "in_progress"]
    if role in {"vc", "admin", "dev"}:
        assert main.dashboard_queue_v2_return_to_not_started(3, request)["ok"]
        with isolated_connect() as check:
            returned = dict(check.execute("SELECT status, actual_started_at FROM queue_requests WHERE id = 3").fetchone())
        assert returned == {"status": "scheduled", "actual_started_at": None}
    else:
        with pytest.raises(main.HTTPException) as return_denied:
            main.dashboard_queue_v2_return_to_not_started(3, request)
        assert return_denied.value.status_code == 403
    request.state.is_admin = False
    request.state.is_dev = False
    request.state.user_email = "someone-else@example.com"
    for action in (main.dashboard_queue_v2_start, main.dashboard_queue_v2_complete, main.dashboard_queue_v2_close):
        with pytest.raises(main.HTTPException) as error:
            action(2, request)
        assert error.value.status_code == 403
