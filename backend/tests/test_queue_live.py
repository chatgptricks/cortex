import json
import sqlite3
from contextlib import contextmanager

from app import main


def test_live_revision_is_durable(monkeypatch, tmp_path):
    database = tmp_path / "queue-live.sqlite3"
    conn = sqlite3.connect(database)
    conn.execute(
        "CREATE TABLE queue_live_state (id INTEGER PRIMARY KEY, revision INTEGER, event_type TEXT, actor_email TEXT, request_ids TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO queue_live_state VALUES (1, 0, '', '', '[]', '')")
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
    with isolated_connect() as value:
        revision = main._queue_v2_publish(value, "draft_updated", "vc@example.com", [3, 1, 3])
    with isolated_connect() as value:
        state = main._queue_v2_live_snapshot(value)
    assert revision == 1
    assert state["revision"] == 1
    assert state["type"] == "draft_updated"
    assert state["actorEmail"] == "vc@example.com"
    assert state["requestIds"] == [1, 3]


def test_shared_draft_planning_uses_existing_drafts(monkeypatch, tmp_path):
    database = tmp_path / "queue-drafts.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY, production_points INTEGER, status TEXT, designer_email TEXT,
            scheduled_date TEXT, scheduled_start_minutes INTEGER, post_account TEXT, post_shortcode TEXT,
            coordinator_email TEXT, updated_at TEXT
        );
        CREATE TABLE dashboard_users (email TEXT PRIMARY KEY, operating_role TEXT, operating_roles TEXT, slack_user_id TEXT);
        CREATE TABLE queue_designer_accounts (designer_email TEXT, account_handle TEXT);
        CREATE TABLE queue_schedule_drafts (
            request_id INTEGER PRIMARY KEY, coordinator_email TEXT, designer_email TEXT,
            scheduled_date TEXT, scheduled_start_minutes INTEGER, recommended_accounts TEXT, updated_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO dashboard_users VALUES ('pd@example.com', 'pd', ?, '')", (json.dumps(["pd"]),))
    conn.execute("INSERT INTO queue_requests VALUES (1, 3, 'pool', NULL, NULL, NULL, 'chatgptricks', 'ONE', 'vc@example.com', '')")
    conn.execute("INSERT INTO queue_requests VALUES (2, 3, 'pool', NULL, NULL, NULL, 'chatgptricks', 'TWO', 'vc@example.com', '')")
    conn.execute("INSERT INTO queue_designer_accounts VALUES ('pd@example.com', 'chatgptricks')")
    conn.execute("INSERT INTO queue_schedule_drafts VALUES (1, 'other-vc@example.com', 'pd@example.com', '2026-09-01', 540, '[]', '')")
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
    with isolated_connect() as value:
        prepared = main._queue_v2_prepare_schedule_changes(value, [{
            "id": 2, "designerEmail": "pd@example.com", "scheduledDate": "2026-09-01",
            "scheduledStartMinutes": 540, "recommendedAccounts": [],
        }])
    assert prepared[0]["date"] == "2026-09-01"
    assert prepared[0]["start"] == 570
