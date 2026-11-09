from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3

import pytest
from fastapi import HTTPException

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


def _insert_queue_request(connect, *, status: str = "pool", designer: str | None = None, suffix: str = "") -> int:
    with connect() as conn:
        cursor = conn.execute(
            """INSERT INTO queue_requests
               (post_account, post_shortcode, production_points, status, designer_email,
                coordinator_email, scheduled_date, scheduled_start_minutes, created_at, updated_at)
               VALUES ('', ?, 3, ?, ?, 'vc@example.com', '2026-09-01', 600, 'now', 'now')""",
            (f"manual-{status}-{designer or 'pool'}{suffix}", status, designer),
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


@pytest.mark.parametrize("status", ["scheduled", "in_progress", "completed", "closed"])
@pytest.mark.parametrize("placement, expected_date, expected_start", [
    ({"scheduled_date": "2026-09-02"}, "2026-09-02", 600),
    ({"scheduled_start_minutes": 720}, "2026-09-01", 720),
    ({"scheduled_date": "2026-09-02", "scheduled_start_minutes": 720}, "2026-09-02", 720),
])
def test_edit_schedule_preserves_assignment_status_and_work_history(monkeypatch, tmp_path, status, placement, expected_date, expected_start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status=status, designer="pd@example.com")
    with connect() as conn:
        conn.execute(
            "UPDATE queue_requests SET actual_started_at = ?, completed_at = ?, closed_at = ? WHERE id = ?",
            ("2026-09-01T16:00:00+00:00", "2026-09-01T16:20:00+00:00", "2026-09-01T16:30:00+00:00", request_id),
        )

    result = main.dashboard_queue_v2_edit(
        request_id, request=None, production_points=4, **placement,
    )["request"]

    assert result["scheduledDate"] == expected_date
    assert result["scheduledStartMinutes"] == expected_start
    assert result["designerEmail"] == "pd@example.com"
    assert result["status"] == status
    assert result["productionPoints"] == 4
    assert result["actualStartedAt"] == "2026-09-01T16:00:00+00:00"
    assert result["completedAt"] == "2026-09-01T16:20:00+00:00"
    assert result["closedAt"] == "2026-09-01T16:30:00+00:00"


@pytest.mark.parametrize("date", ["", "2026-02-30", "2026-9-01", "tomorrow"])
def test_edit_rejects_invalid_date_without_saving_other_changes(monkeypatch, tmp_path, date):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com")

    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_edit(request_id, request=None, production_points=8, scheduled_date=date)

    assert error.value.status_code == 400
    with connect() as conn:
        saved = conn.execute("SELECT production_points, scheduled_date FROM queue_requests WHERE id = ?", (request_id,)).fetchone()
    assert tuple(saved) == (3, "2026-09-01")


@pytest.mark.parametrize("status", ["pool", "cancelled"])
@pytest.mark.parametrize("placement", [{"scheduled_date": "2026-09-02"}, {"scheduled_start_minutes": 720}])
def test_edit_schedule_does_not_assign_unassigned_or_cancelled_requests(monkeypatch, tmp_path, status, placement):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status=status)

    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_edit(request_id, request=None, production_points=8, **placement)

    assert error.value.status_code == 409
    with connect() as conn:
        saved = conn.execute("SELECT production_points, scheduled_date FROM queue_requests WHERE id = ?", (request_id,)).fetchone()
    assert tuple(saved) == (3, "2026-09-01")


@pytest.mark.parametrize("placement, expected_date, requested_start", [
    ({"scheduled_date": "2026-09-02"}, "2026-09-02", 600),
    ({"scheduled_start_minutes": 720}, "2026-09-01", 720),
    ({"scheduled_date": "2026-09-02", "scheduled_start_minutes": 720}, "2026-09-02", 720),
])
def test_edit_schedule_reflows_conflicts_and_updates_shared_draft(monkeypatch, tmp_path, placement, expected_date, requested_start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com")
    fixed_id = _insert_queue_request(connect, status="closed", designer="pd@example.com")
    following_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com", suffix="-following")
    with connect() as conn:
        conn.execute("UPDATE queue_requests SET scheduled_date = ?, scheduled_start_minutes = ? WHERE id IN (?, ?)", (expected_date, requested_start, fixed_id, following_id))
        conn.execute(
            """INSERT INTO queue_schedule_drafts
               (request_id, coordinator_email, designer_email, scheduled_date, scheduled_start_minutes,
                production_points, minutes_per_pp, updated_at)
               VALUES (?, 'vc@example.com', 'pd@example.com', '2026-09-01', 600, 3, 10, 'now')""",
            (request_id,),
        )

    result = main.dashboard_queue_v2_edit(
        request_id, request=None, production_points=4, **placement,
    )["request"]

    assert (result["scheduledDate"], result["scheduledStartMinutes"]) == (expected_date, requested_start + 40)
    with connect() as conn:
        fixed = conn.execute("SELECT scheduled_date, scheduled_start_minutes FROM queue_requests WHERE id = ?", (fixed_id,)).fetchone()
        following = conn.execute("SELECT scheduled_date, scheduled_start_minutes FROM queue_requests WHERE id = ?", (following_id,)).fetchone()
        draft = conn.execute("SELECT scheduled_date, scheduled_start_minutes, production_points FROM queue_schedule_drafts WHERE request_id = ?", (request_id,)).fetchone()
    assert tuple(fixed) == (expected_date, requested_start)
    assert tuple(following) == (expected_date, requested_start + 90)
    assert tuple(draft) == (expected_date, requested_start + 40, 4)


@pytest.mark.parametrize("placement, expected_date, requested_start", [
    ({"scheduled_date": "2026-09-02"}, "2026-09-02", 600),
    ({"scheduled_start_minutes": 720}, "2026-09-01", 720),
    ({"scheduled_date": "2026-09-02", "scheduled_start_minutes": 720}, "2026-09-02", 720),
])
def test_edit_schedule_reflows_other_requests_drafts_on_destination_day(monkeypatch, tmp_path, placement, expected_date, requested_start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com")
    draft_id = _insert_queue_request(connect)
    with connect() as conn:
        conn.execute(
            """INSERT INTO queue_schedule_drafts
               (request_id, coordinator_email, designer_email, scheduled_date, scheduled_start_minutes,
                production_points, minutes_per_pp, updated_at)
               VALUES (?, 'other-vc@example.com', 'pd@example.com', ?, ?, 3, 10, 'now')""",
            (draft_id, expected_date, requested_start),
        )

    main.dashboard_queue_v2_edit(request_id, request=None, production_points=3, **placement)

    with connect() as conn:
        draft = conn.execute("SELECT scheduled_date, scheduled_start_minutes FROM queue_schedule_drafts WHERE request_id = ?", (draft_id,)).fetchone()
    assert tuple(draft) == (expected_date, requested_start + 40)


@pytest.mark.parametrize("start", [-10, 1440, 601, 610.5, "600", True])
def test_edit_rejects_invalid_time_without_saving_other_changes(monkeypatch, tmp_path, start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com")

    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_edit(request_id, request=None, production_points=8, scheduled_start_minutes=start)

    assert error.value.status_code == 400
    with connect() as conn:
        saved = conn.execute("SELECT production_points, scheduled_date, scheduled_start_minutes FROM queue_requests WHERE id = ?", (request_id,)).fetchone()
    assert tuple(saved) == (3, "2026-09-01", 600)


@pytest.mark.parametrize("start", [0, 1430])
def test_edit_accepts_time_boundaries(monkeypatch, tmp_path, start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com")

    result = main.dashboard_queue_v2_edit(request_id, request=None, production_points=1, scheduled_start_minutes=start)

    assert result["request"]["scheduledDate"] == "2026-09-01"
    assert result["request"]["scheduledStartMinutes"] == start


def test_edit_without_placement_fields_preserves_distinct_draft_placement(monkeypatch, tmp_path):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com")
    with connect() as conn:
        conn.execute(
            """INSERT INTO queue_schedule_drafts
               (request_id, coordinator_email, designer_email, scheduled_date, scheduled_start_minutes,
                production_points, minutes_per_pp, updated_at)
               VALUES (?, 'vc@example.com', 'pd@example.com', '2026-09-02', 720, 3, 10, 'now')""",
            (request_id,),
        )

    result = main.dashboard_queue_v2_edit(request_id, request=None, production_points=3)["request"]

    assert (result["scheduledDate"], result["scheduledStartMinutes"]) == ("2026-09-01", 600)
    with connect() as conn:
        draft = conn.execute("SELECT scheduled_date, scheduled_start_minutes FROM queue_schedule_drafts WHERE request_id = ?", (request_id,)).fetchone()
    assert tuple(draft) == ("2026-09-02", 720)


def _freeze_queue_clock(monkeypatch, value="2026-11-09T09:30:00"):
    frozen = datetime.fromisoformat(value).replace(tzinfo=main.SCHEDULER_TIMEZONE)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", FrozenDateTime)


@pytest.mark.parametrize("points, expected_start", [(3, 660), (8, 760)])
def test_next_available_uses_gaps_without_moving_committed_drafts_or_personal_time(monkeypatch, tmp_path, points, expected_start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    _freeze_queue_clock(monkeypatch)
    request_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com")
    occupied_id = _insert_queue_request(connect, status="scheduled", designer="pd@example.com", suffix="-occupied")
    with connect() as conn:
        conn.execute("UPDATE queue_requests SET scheduled_date = '2026-11-09', scheduled_start_minutes = 580 WHERE id = ?", (request_id,))
        conn.execute("UPDATE queue_requests SET scheduled_date = '2026-11-09', scheduled_start_minutes = 600 WHERE id = ?", (occupied_id,))
        conn.executemany(
            """INSERT INTO queue_schedule_drafts
               (request_id, coordinator_email, designer_email, scheduled_date, scheduled_start_minutes,
                production_points, minutes_per_pp, updated_at)
               VALUES (?, 'vc@example.com', 'pd@example.com', '2026-11-09', ?, 3, 10, 'now')""",
            [(request_id, 580), (occupied_id, 720)],
        )
        conn.execute(
            """INSERT INTO queue_tickets
               (ticket_type, requester_email, block_category, scheduled_date, scheduled_start_minutes,
                duration_minutes, created_at, updated_at)
               VALUES ('time_block', 'pd@example.com', 'meeting', '2026-11-09', 640, 20, 'now', 'now')""",
        )
        original_request = tuple(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (occupied_id,)).fetchone())
        original_draft = tuple(conn.execute("SELECT * FROM queue_schedule_drafts WHERE request_id = ?", (occupied_id,)).fetchone())
        original_ticket = tuple(conn.execute("SELECT * FROM queue_tickets").fetchone())

    def unexpected_reflow(*args, **kwargs):
        pytest.fail("Next available must not reflow another request or draft")

    monkeypatch.setattr(main, "_queue_v2_reflow_scheduled", unexpected_reflow)
    monkeypatch.setattr(main, "_queue_v2_reflow_drafts", unexpected_reflow)
    monkeypatch.setattr(main, "_queue_v2_slack_log", unexpected_reflow)
    result = main.dashboard_queue_v2_edit(
        request_id, request=None, production_points=points, move_to_next_available=True,
        scheduled_date="ignored", scheduled_start_minutes=-1,
    )["request"]

    assert (result["scheduledDate"], result["scheduledStartMinutes"]) == ("2026-11-09", expected_start)
    with connect() as conn:
        assert tuple(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (occupied_id,)).fetchone()) == original_request
        assert tuple(conn.execute("SELECT * FROM queue_schedule_drafts WHERE request_id = ?", (occupied_id,)).fetchone()) == original_draft
        assert tuple(conn.execute("SELECT * FROM queue_tickets").fetchone()) == original_ticket
        assert conn.execute("SELECT 1 FROM queue_schedule_drafts WHERE request_id = ?", (request_id,)).fetchone() is None


@pytest.mark.parametrize("status", ["scheduled", "in_progress", "completed", "closed"])
@pytest.mark.parametrize("clock, expected_date, expected_start", [
    ("2026-11-09T09:30:00", "2026-11-09", 580),
    ("2026-11-09T09:31:10", "2026-11-09", 580),
    ("2026-11-09T23:59:59", "2026-11-10", 0),
])
def test_next_available_is_strictly_after_now_and_preserves_history(monkeypatch, tmp_path, status, clock, expected_date, expected_start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    _freeze_queue_clock(monkeypatch, clock)
    request_id = _insert_queue_request(connect, status=status, designer="pd@example.com")
    with connect() as conn:
        conn.execute("UPDATE queue_requests SET actual_started_at = '2026-09-01T16:00:00+00:00', completed_at = '2026-09-01T16:20:00+00:00', closed_at = '2026-09-01T16:30:00+00:00' WHERE id = ?", (request_id,))
        conn.execute("UPDATE queue_requests SET scheduled_date = ?, scheduled_start_minutes = ? WHERE id = ?", (expected_date, expected_start, request_id))

    result = main.dashboard_queue_v2_edit(request_id, request=None, production_points=3, move_to_next_available=True)["request"]

    assert (result["scheduledDate"], result["scheduledStartMinutes"]) == (expected_date, expected_start)
    assert result["status"] == status
    assert result["designerEmail"] == "pd@example.com"
    assert result["actualStartedAt"] == "2026-09-01T16:00:00+00:00"
    assert result["completedAt"] == "2026-09-01T16:20:00+00:00"
    assert result["closedAt"] == "2026-09-01T16:30:00+00:00"


@pytest.mark.parametrize("minutes_per_pp, expected_unit, expected_start", [(None, 16, 660), (30, 30, 660)])
def test_next_available_assigns_pool_to_explicit_designer_with_their_duration(monkeypatch, tmp_path, minutes_per_pp, expected_unit, expected_start):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    _freeze_queue_clock(monkeypatch)
    request_id = _insert_queue_request(connect)
    occupied_id = _insert_queue_request(connect, status="scheduled", designer="trainee@example.com")
    with connect() as conn:
        conn.execute("INSERT INTO dashboard_users (email, operating_role, operating_roles, minutes_per_pp, created_at, updated_at) VALUES ('trainee@example.com', 'trainee', '[\"trainee\"]', ?, 'now', 'now')", (minutes_per_pp,))
        conn.execute("UPDATE queue_requests SET scheduled_date = '2026-11-09', scheduled_start_minutes = 620 WHERE id = ?", (occupied_id,))

    result = main.dashboard_queue_v2_edit(
        request_id, request=None, production_points=3, move_to_next_available=True, designer_email="trainee@example.com",
    )["request"]

    assert result["status"] == "scheduled"
    assert result["designerEmail"] == "trainee@example.com"
    assert result["coordinatorEmail"] == "vc@example.com"
    assert result["minutesPerPP"] == expected_unit
    assert result["durationMinutes"] == 3 * expected_unit
    assert (result["scheduledDate"], result["scheduledStartMinutes"]) == ("2026-11-09", expected_start)


@pytest.mark.parametrize("status, designer, selected, error_status", [
    ("pool", None, None, 400), ("pool", None, "unknown@example.com", 400),
    ("scheduled", "pd@example.com", "other@example.com", 400),
    ("cancelled", "pd@example.com", None, 409),
])
def test_next_available_rejects_invalid_assignment_without_saving(monkeypatch, tmp_path, status, designer, selected, error_status):
    connect = _canonical_queue_database(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_queue_v2_access", lambda *args, **kwargs: ("vc@example.com", True, ["vc"]))
    request_id = _insert_queue_request(connect, status=status, designer=designer)
    with connect() as conn:
        original = tuple(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone())

    with pytest.raises(HTTPException) as error:
        main.dashboard_queue_v2_edit(request_id, request=None, production_points=8, move_to_next_available=True, designer_email=selected)

    assert error.value.status_code == error_status
    with connect() as conn:
        assert tuple(conn.execute("SELECT * FROM queue_requests WHERE id = ?", (request_id,)).fetchone()) == original


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
