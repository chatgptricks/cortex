"""Personal-time reservation and Queue movement must be one scoped transaction."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from test_queue_tickets import _ticket_database


DAY = "2026-11-09"
NEXT_DAY = "2026-11-10"


@pytest.fixture
def queue_store(monkeypatch, tmp_path):
    path = tmp_path / "personal-time-reflow.sqlite3"
    _ticket_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE dashboard_users (email TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO dashboard_users VALUES (?)", [("pd@example.com",), ("other@example.com",), ("vc@example.com",)])

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(main, "connect", connect)
    events, logs, notifications = [], [], []
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args: events.append(args[1:]) or 1)
    monkeypatch.setattr(main, "_queue_v2_log", lambda *args: logs.append(args[1:]))
    monkeypatch.setattr(main, "_queue_v2_slack_log", lambda **kwargs: notifications.append(kwargs))
    return connect, events, logs, notifications


def caller(email="vc@example.com", *, coordinator=True):
    return SimpleNamespace(state=SimpleNamespace(user_email=email, is_admin=False, is_dev=False,
        operating_role="vc" if coordinator else "pd", operating_roles=["vc", "pd"] if coordinator else ["pd"]))


def request_row(conn, request_id, start, *, date=DAY, user="pd@example.com", status="scheduled", pp=3, unit=10):
    conn.execute("""INSERT INTO queue_requests
        (id, production_points, minutes_per_pp, status, designer_email, scheduled_date,
         scheduled_start_minutes, post_account, post_shortcode, updated_at, cancellation_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'source', ?, 'original', '')""",
        (request_id, pp, unit, status, user, date, start, f"POST{request_id}"))


def draft_row(conn, request_id, start, *, date=DAY, user="pd@example.com", pp=3, unit=10):
    conn.execute("""INSERT INTO queue_schedule_drafts
        (request_id, coordinator_email, designer_email, scheduled_date, scheduled_start_minutes,
         production_points, minutes_per_pp, updated_at)
        VALUES (?, 'vc@example.com', ?, ?, ?, ?, ?, 'original')""",
        (request_id, user, date, start, pp, unit))


def reserve(**overrides):
    values = dict(request=caller(), category="break", scheduled_date=DAY,
        scheduled_start_minutes=720, duration_minutes=60, title="Lunch", note="Personal lunch",
        designer_email="pd@example.com", reschedule_conflicts=True)
    return main.dashboard_queue_v2_create_time_block(**{**values, **overrides})


def placements(conn):
    return {row["id"]: (row["scheduled_date"], row["scheduled_start_minutes"], row["status"], row["production_points"], row["minutes_per_pp"])
            for row in conn.execute("SELECT * FROM queue_requests")}


def test_lunch_reservation_reflows_only_selected_day_conflicts(queue_store):
    connect, events, logs, notifications = queue_store
    with connect() as conn:
        request_row(conn, 1, 720)
        request_row(conn, 2, 760, pp=2)
        request_row(conn, 3, 600, pp=2)
        request_row(conn, 4, 720, date=NEXT_DAY)
        request_row(conn, 5, 720, user="other@example.com")
        request_row(conn, 6, 540, status="in_progress", pp=2)
        before = placements(conn)

    result = reserve()
    assert result["ticket"]["status"] == "approved"
    assert result["changed_request_ids"] == [1, 2] and result["rescheduled_count"] == 2
    with connect() as conn:
        after = placements(conn)
    assert after[1] == (DAY, 780, "scheduled", 3, 10)
    assert after[2] == (DAY, 820, "scheduled", 2, 10)
    assert {key: after[key] for key in (3, 4, 5, 6)} == {key: before[key] for key in (3, 4, 5, 6)}
    assert events == [("time_block_created", "vc@example.com", [1, 2])]
    assert {item[0] for item in logs} == {1, 2}
    assert notifications == []


def test_move_remaining_after_lunch_includes_unstarted_work_before_lunch(queue_store):
    connect, *_ = queue_store
    with connect() as conn:
        request_row(conn, 1, 600, pp=2)
        request_row(conn, 2, 650, pp=3)
        request_row(conn, 3, 900, status="completed")
        request_row(conn, 4, 720, date=NEXT_DAY)
        before = placements(conn)
    result = reserve(reschedule_conflicts=False, move_remaining_after_block=True)
    assert result["changed_request_ids"] == [1, 2]
    with connect() as conn:
        after = placements(conn)
    assert after[1][:2] == (DAY, 780) and after[2][:2] == (DAY, 810)
    assert after[3] == before[3] and after[4] == before[4]


@pytest.mark.parametrize("status", ["in_progress", "completed", "closed"])
def test_active_and_finished_work_cannot_be_displaced_by_lunch(queue_store, status):
    connect, events, *_ = queue_store
    with connect() as conn:
        request_row(conn, 1, 720, status=status)
        request_row(conn, 2, 600)
        before = placements(conn)
    with pytest.raises(HTTPException) as failure:
        reserve(move_remaining_after_block=True)
    assert failure.value.status_code == 409
    with connect() as conn:
        assert placements(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM queue_tickets").fetchone()[0] == 0
    assert events == []


def test_other_personal_time_is_fixed_and_conflict_rolls_back(queue_store):
    connect, events, *_ = queue_store
    with connect() as conn:
        conn.execute("""INSERT INTO queue_tickets
            (ticket_type, requester_email, status, block_category, title, scheduled_date,
             scheduled_start_minutes, duration_minutes, created_at, updated_at)
            VALUES ('time_block','pd@example.com','approved','meeting','Meeting',?,730,20,'now','now')""", (DAY,))
        request_row(conn, 1, 600)
        before = placements(conn)
    with pytest.raises(HTTPException) as failure:
        reserve(move_remaining_after_block=True)
    assert failure.value.status_code == 409
    with connect() as conn:
        assert placements(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM queue_tickets").fetchone()[0] == 1
    assert events == []


def test_drafts_move_without_assigning_pool_work_or_touching_other_dates(queue_store):
    connect, *_ = queue_store
    with connect() as conn:
        request_row(conn, 1, None, date=None, user=None, status="pool", pp=2)
        request_row(conn, 2, None, date=None, user=None, status="pool", pp=2)
        draft_row(conn, 1, 730, pp=2)
        draft_row(conn, 2, 730, date=NEXT_DAY, pp=2)
    result = reserve()
    assert result["changed_request_ids"] == [1] and result["rescheduled_count"] == 1
    with connect() as conn:
        assert placements(conn)[1][:3] == (None, None, "pool")
        drafts = {row["request_id"]: (row["scheduled_date"], row["scheduled_start_minutes"])
                  for row in conn.execute("SELECT * FROM queue_schedule_drafts")}
    assert drafts == {1: (DAY, 780), 2: (NEXT_DAY, 730)}


def test_paired_committed_and_draft_changes_count_one_request_and_preserve_pp(queue_store):
    connect, *_ = queue_store
    with connect() as conn:
        request_row(conn, 1, 720, pp=2)
        request_row(conn, 2, 750, pp=2)
        draft_row(conn, 1, 720, pp=3, unit=20)
    result = reserve()
    assert result["changed_request_ids"] == [1, 2] and result["rescheduled_count"] == 2
    with connect() as conn:
        assert placements(conn)[1] == (DAY, 780, "scheduled", 2, 10)
        assert placements(conn)[2][:2] == (DAY, 850)
        draft = conn.execute("SELECT * FROM queue_schedule_drafts WHERE request_id = 1").fetchone()
        assert (draft["scheduled_date"], draft["scheduled_start_minutes"], draft["production_points"], draft["minutes_per_pp"]) == (DAY, 780, 3, 20)


def test_overflow_avoids_fixed_next_day_requests_without_moving_them(queue_store):
    connect, *_ = queue_store
    with connect() as conn:
        request_row(conn, 1, 1400, pp=2)
        request_row(conn, 2, 0, date=NEXT_DAY, pp=2)
    result = reserve(scheduled_start_minutes=1380, duration_minutes=60, move_remaining_after_block=True)
    assert result["changed_request_ids"] == [1]
    with connect() as conn:
        after = placements(conn)
    assert after[1][:2] == (NEXT_DAY, 30)
    assert after[2][:2] == (NEXT_DAY, 0)


@pytest.mark.parametrize("flag", ["reschedule_conflicts", "move_remaining_after_block"])
def test_pd_cannot_use_reflow_to_bypass_coordinator_approval(queue_store, flag):
    connect, *_ = queue_store
    with pytest.raises(HTTPException) as failure:
        reserve(request=caller("pd@example.com", coordinator=False), designer_email=None,
                **{"reschedule_conflicts": False, flag: True})
    assert failure.value.status_code == 403
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_tickets").fetchone()[0] == 0


def test_default_pd_creation_remains_pending_and_default_conflicts_remain_rejected(queue_store):
    connect, *_rest, notifications = queue_store
    created = reserve(request=caller("pd@example.com", coordinator=False), designer_email=None,
                      reschedule_conflicts=False)
    assert created["ticket"]["status"] == "pending" and created["rescheduled_count"] == 0
    assert notifications[0]["event_type"] == "time_block_requested"
    with connect() as conn:
        request_row(conn, 1, 900)
    with pytest.raises(HTTPException) as failure:
        reserve(scheduled_start_minutes=900, reschedule_conflicts=False)
    assert failure.value.status_code == 409


def test_publication_failure_rolls_back_block_and_every_move(queue_store, monkeypatch):
    connect, *_ = queue_store
    with connect() as conn:
        request_row(conn, 1, 720)
        before = placements(conn)
    def fail(*args):
        raise RuntimeError("publish failed")
    monkeypatch.setattr(main, "_queue_v2_publish", fail)
    with pytest.raises(RuntimeError):
        reserve()
    with connect() as conn:
        assert placements(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM queue_tickets").fetchone()[0] == 0


@pytest.mark.parametrize("date", ["2026-11-9", "2026-1-09", "not-a-date"])
def test_noncanonical_dates_cannot_bypass_the_selected_day_scope(queue_store, date):
    connect, *_ = queue_store
    with connect() as conn:
        request_row(conn, 1, 720, status="in_progress")
    with pytest.raises(HTTPException) as failure:
        reserve(scheduled_date=date)
    assert failure.value.status_code == 400
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM queue_tickets").fetchone()[0] == 0
