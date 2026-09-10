from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

from app import db, main


def _canonical_queue_database(monkeypatch, tmp_path):
    """Build the actual Queue schema instead of a reduced test fixture."""
    database = tmp_path / "canonical-queue.sqlite3"
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DB_PATH", database)
    db.init_db()

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
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main, "_queue_v2_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_queue_v2_slack_log", lambda **kwargs: True)
    return isolated_connect


def _insert_queue_request(connect, *, status: str = "pool", designer: str | None = None) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO queue_requests
               (post_account, post_shortcode, production_points, status, designer_email,
                coordinator_email, scheduled_date, scheduled_start_minutes, created_at, updated_at)
               VALUES ('', ?, 3, ?, ?, 'vc@example.com', '2026-09-01', 600, 'now', 'now')""",
            (f"manual-{status}-{designer or 'pool'}", status, designer),
        )
        return int(cursor.lastrowid)


def test_edit_accepts_recommended_accounts_and_returns_the_canonical_request(monkeypatch, tmp_path):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        main,
        "_queue_v2_access",
        lambda request, coordinator=False: ("vc@example.com", True, ["vc", "pd"]),
    )
    request_id = _insert_queue_request(connect)

    result = main.dashboard_queue_v2_edit(
        request_id,
        request=None,
        production_points=5,
        priority="urgent",
        tags="design,review",
        brief="Tighten the first slide.",
        notes="Preserve the visual system.",
        references=json.dumps(["https://example.com/brief"]),
        recommended_accounts=json.dumps(["@ChatGPTricks", "planet.ai_", "chatgptricks"]),
    )

    assert result["ok"] is True
    assert result["request"]["productionPoints"] == 5
    assert result["request"]["recommendedAccounts"] == ["chatgptricks", "planet.ai_"]
    with connect() as conn:
        saved = conn.execute(
            "SELECT recommended_accounts FROM queue_requests WHERE id = ?", (request_id,)
        ).fetchone()
    assert json.loads(saved["recommended_accounts"]) == ["chatgptricks", "planet.ai_"]


def test_fresh_canonical_schema_allows_trainee_review_endpoint(monkeypatch, tmp_path):
    connect = _canonical_queue_database(monkeypatch, tmp_path)

    def access(_request, coordinator=False):
        if coordinator:
            return "vc@example.com", True, ["vc", "pd"]
        return "trainee@example.com", False, ["trainee", "pd"]

    monkeypatch.setattr(main, "_queue_v2_access", access)
    request_id = _insert_queue_request(
        connect,
        status="completed",
        designer="trainee@example.com",
    )

    result = main.dashboard_queue_v2_request_trainee_review(
        request=None,
        request_id=request_id,
        canva_link="https://www.canva.com/design/ABC123/edit",
    )

    assert result["ticket"]["type"] == "trainee_review"
    assert result["ticket"]["status"] == "pending"
    with connect() as conn:
        stored = conn.execute(
            "SELECT ticket_type FROM queue_tickets WHERE request_id = ?", (request_id,)
        ).fetchone()
    assert stored["ticket_type"] == "trainee_review"


def test_legacy_ticket_schema_migrates_without_losing_rows():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE dashboard_users (email TEXT PRIMARY KEY, updated_at TEXT NOT NULL);
        CREATE TABLE accounts (handle TEXT PRIMARY KEY);
        CREATE TABLE queue_requests (id INTEGER PRIMARY KEY);
        INSERT INTO queue_requests VALUES (7);
        CREATE TABLE queue_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_type TEXT NOT NULL CHECK(ticket_type IN ('time_block','pp_revision','cancellation')),
            requester_email TEXT NOT NULL,
            request_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            block_category TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            scheduled_date TEXT,
            scheduled_start_minutes INTEGER,
            duration_minutes INTEGER,
            requested_production_points INTEGER,
            requested_accounts TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT '',
            reviewer_email TEXT,
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(request_id) REFERENCES queue_requests(id) ON DELETE CASCADE
        );
        INSERT INTO queue_tickets
            (ticket_type, requester_email, request_id, status, title, created_at, updated_at)
            VALUES ('time_block', 'pd@example.com', 7, 'approved', 'Existing block', 'now', 'now');
        """
    )

    db._ensure_runtime_schema_extensions(connection)
    db._ensure_runtime_schema_extensions(connection)

    preserved = connection.execute(
        "SELECT id, ticket_type, title FROM queue_tickets"
    ).fetchone()
    assert dict(preserved) == {"id": 1, "ticket_type": "time_block", "title": "Existing block"}
    connection.execute(
        """INSERT INTO queue_tickets
           (ticket_type, requester_email, request_id, status, title, created_at, updated_at)
           VALUES ('trainee_review', 'trainee@example.com', 7, 'pending', 'Canva review', 'now', 'now')"""
    )
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM queue_tickets WHERE ticket_type = 'trainee_review'"
    ).fetchone()["count"] == 1
