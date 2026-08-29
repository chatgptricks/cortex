from contextlib import contextmanager
import sqlite3

import pytest
from fastapi import HTTPException

from app import main


def _ticket_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY,
            production_points INTEGER NOT NULL,
            status TEXT NOT NULL,
            designer_email TEXT,
            scheduled_date TEXT,
            scheduled_start_minutes INTEGER,
            post_account TEXT,
            post_shortcode TEXT,
            updated_at TEXT,
            cancellation_reason TEXT
        );
        CREATE TABLE queue_schedule_drafts (
            request_id INTEGER PRIMARY KEY,
            coordinator_email TEXT,
            designer_email TEXT,
            scheduled_date TEXT,
            scheduled_start_minutes INTEGER,
            recommended_accounts TEXT,
            production_points INTEGER,
            updated_at TEXT
        );
        CREATE TABLE queue_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_type TEXT NOT NULL,
            requester_email TEXT NOT NULL,
            request_id INTEGER,
            status TEXT NOT NULL,
            block_category TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            scheduled_date TEXT,
            scheduled_start_minutes INTEGER,
            duration_minutes INTEGER,
            requested_production_points INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            reviewer_email TEXT,
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def _isolate(monkeypatch, database, *, coordinator=False):
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
    monkeypatch.setattr(
        main,
        "_queue_v2_access",
        lambda request, coordinator=False: (
            "vc@example.com" if coordinator else "pd@example.com",
            bool(coordinator),
            ["vc", "pd"] if coordinator else ["pd"],
        ),
    )
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main, "_queue_v2_log", lambda *args, **kwargs: None)
    return isolated_connect


def test_personal_time_ticket_is_live_then_approved(monkeypatch, tmp_path):
    database = tmp_path / "time-ticket.sqlite3"
    _ticket_database(database)
    connect = _isolate(monkeypatch, database)

    created = main.dashboard_queue_v2_create_time_block(
        request=None,
        category="meeting",
        scheduled_date="2026-09-01",
        scheduled_start_minutes=600,
        duration_minutes=30,
        title="Client sync",
        note="Weekly review",
    )
    assert created["ticket"]["status"] == "pending"
    assert created["ticket"]["durationMinutes"] == 30

    ticket_id = created["ticket"]["id"]
    reviewed = main.dashboard_queue_v2_review_ticket(
        ticket_id=ticket_id,
        request=None,
        action="approve",
        review_note=None,
    )
    assert reviewed["ticket"]["status"] == "approved"
    assert reviewed["ticket"]["reviewerEmail"] == "vc@example.com"

    with connect() as conn:
        assert conn.execute("SELECT status FROM queue_tickets WHERE id = ?", (ticket_id,)).fetchone()["status"] == "approved"


def test_personal_time_cannot_overlap_queue_work(monkeypatch, tmp_path):
    database = tmp_path / "time-conflict.sqlite3"
    _ticket_database(database)
    connect = _isolate(monkeypatch, database)
    with connect() as conn:
        conn.execute(
            "INSERT INTO queue_requests VALUES (1, 3, 'scheduled', 'pd@example.com', '2026-09-01', 600, 'chatgptricks', 'POST1', '', '')"
        )

    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_create_time_block(
            request=None,
            category="break",
            scheduled_date="2026-09-01",
            scheduled_start_minutes=620,
            duration_minutes=20,
            title=None,
            note=None,
        )
    assert error.value.status_code == 409


def test_pp_revision_and_cancellation_ticket_actions(monkeypatch, tmp_path):
    database = tmp_path / "request-tickets.sqlite3"
    _ticket_database(database)
    connect = _isolate(monkeypatch, database)
    monkeypatch.setattr(main, "_queue_v2_reflow_scheduled", lambda *args, **kwargs: 0)
    with connect() as conn:
        conn.execute(
            "INSERT INTO queue_requests VALUES (1, 3, 'scheduled', 'pd@example.com', '2026-09-01', 600, 'chatgptricks', 'POST1', '', '')"
        )

    pp = main.dashboard_queue_v2_request_pp_revision(
        request=None,
        request_id=1,
        production_points=5,
        reason="More source material",
    )
    main.dashboard_queue_v2_review_ticket(
        ticket_id=pp["ticket"]["id"], request=None, action="approve", review_note=None,
    )
    with connect() as conn:
        assert conn.execute("SELECT production_points FROM queue_requests WHERE id = 1").fetchone()["production_points"] == 5

    cancellation = main.dashboard_queue_v2_request_cancellation(
        request=None,
        request_id=1,
        reason="Post no longer needed",
    )
    main.dashboard_queue_v2_review_ticket(
        ticket_id=cancellation["ticket"]["id"], request=None, action="approve", review_note=None,
    )
    with connect() as conn:
        row = conn.execute("SELECT status, cancellation_reason FROM queue_requests WHERE id = 1").fetchone()
        assert row["status"] == "cancelled"
        assert row["cancellation_reason"] == "Post no longer needed"
