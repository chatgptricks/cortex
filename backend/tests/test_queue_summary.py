from contextlib import contextmanager
import sqlite3

from starlette.requests import Request

from app import main


def test_queue_summary_is_always_personal_to_the_active_user(monkeypatch, tmp_path):
    database = tmp_path / "queue-summary.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY,
            designer_email TEXT,
            status TEXT NOT NULL
        );
        CREATE TABLE queue_schedule_drafts (
            request_id INTEGER PRIMARY KEY,
            designer_email TEXT NOT NULL
        );
        INSERT INTO queue_requests VALUES
            (1, 'admin@example.com', 'scheduled'),
            (2, 'admin@example.com', 'completed'),
            (3, 'designer@example.com', 'in_progress'),
            (4, 'designer@example.com', 'pool'),
            (5, 'admin@example.com', 'closed');
        INSERT INTO queue_schedule_drafts VALUES (6, 'admin@example.com');
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
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.user_email = "admin@example.com"
    request.state.is_admin = True
    request.state.operating_roles = ["admin", "vc"]

    assert main.dashboard_queue_v2_summary(request) == {"pending": 3}
