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
            scheduled_date TEXT, scheduled_start_minutes INTEGER, recommended_accounts TEXT,
            production_points INTEGER, updated_at TEXT
        );
        CREATE TABLE queue_tickets (
            id INTEGER PRIMARY KEY, ticket_type TEXT, requester_email TEXT, status TEXT,
            scheduled_date TEXT, scheduled_start_minutes INTEGER, duration_minutes INTEGER
        );
        """
    )
    conn.execute("INSERT INTO dashboard_users VALUES ('pd@example.com', 'pd', ?, '')", (json.dumps(["pd"]),))
    conn.execute("INSERT INTO queue_requests VALUES (1, 3, 'pool', NULL, NULL, NULL, 'chatgptricks', 'ONE', 'vc@example.com', '')")
    conn.execute("INSERT INTO queue_requests VALUES (2, 3, 'pool', NULL, NULL, NULL, 'chatgptricks', 'TWO', 'vc@example.com', '')")
    conn.execute("INSERT INTO queue_designer_accounts VALUES ('pd@example.com', 'chatgptricks')")
    conn.execute("INSERT INTO queue_schedule_drafts VALUES (1, 'other-vc@example.com', 'pd@example.com', '2026-09-01', 540, '[]', NULL, '')")
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


def test_every_dashboard_user_is_pd_capable_by_default():
    roles = main._queue_v2_user_roles({
        "operating_role": "sales",
        "operating_roles": json.dumps(["sales"]),
    })
    assert roles == ["sales", "pd"]


def test_schedule_change_can_return_request_to_pool(monkeypatch, tmp_path):
    database = tmp_path / "queue-pool-return.sqlite3"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY, production_points INTEGER, status TEXT, designer_email TEXT,
            scheduled_date TEXT, scheduled_start_minutes INTEGER, post_account TEXT, post_shortcode TEXT,
            coordinator_email TEXT, recommended_accounts TEXT, updated_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO queue_requests VALUES (1, 3, 'scheduled', 'pd@example.com', '2026-09-01', 540, 'chatgptricks', 'ONE', 'vc@example.com', '[]', '')"
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
    with isolated_connect() as value:
        prepared = main._queue_v2_prepare_schedule_changes(value, [{
            "id": 1, "status": "pool", "designerEmail": None, "scheduledDate": None,
            "scheduledStartMinutes": None, "productionPoints": 4, "recommendedAccounts": ["chatgptricks"],
        }])
    assert prepared[0]["pool"] is True
    assert prepared[0]["designer"] is None
    assert prepared[0]["duration"] == 40


def test_hot_posts_above_three_x_are_auto_pooled_once(tmp_path):
    database = tmp_path / "queue-hot.sqlite3"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (handle TEXT, is_canonical INTEGER, is_active INTEGER);
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY, shortcode TEXT, caption TEXT, title TEXT,
            post_type_label TEXT, published_at TEXT, likes INTEGER, comments INTEGER,
            is_hot INTEGER, hot_rate_multiplier REAL
        );
        CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, caption TEXT,
            post_type_label TEXT, published_at TEXT, likes INTEGER, comments INTEGER,
            permalink TEXT, is_hot INTEGER, hot_rate_multiplier REAL
        );
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_account TEXT NOT NULL,
            post_shortcode TEXT NOT NULL, post_permalink TEXT NOT NULL,
            post_caption TEXT NOT NULL, post_type TEXT NOT NULL, cover_url TEXT NOT NULL,
            production_points INTEGER NOT NULL, priority TEXT NOT NULL, deadline_at TEXT NOT NULL,
            tags TEXT NOT NULL, brief TEXT NOT NULL, notes TEXT NOT NULL,
            reference_links TEXT NOT NULL, coordinator_email TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pool',
            UNIQUE(post_account, post_shortcode)
        );
        CREATE TABLE queue_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, actor_email TEXT,
            event_type TEXT, details TEXT, created_at TEXT
        );
        CREATE TABLE queue_live_state (
            id INTEGER PRIMARY KEY, revision INTEGER, event_type TEXT,
            actor_email TEXT, request_ids TEXT, updated_at TEXT
        );
        INSERT INTO queue_live_state VALUES (1, 0, '', '', '[]', '');
        """
    )
    conn.execute("INSERT INTO accounts VALUES ('chatgptricks', 1, 1)")
    conn.execute("INSERT INTO accounts VALUES ('competitor', 0, 1)")
    conn.execute("INSERT INTO posts VALUES (1, 'HOT1', 'caption', 'title', 'Image', '2026', 10, 1, 1, 3.4)")
    conn.execute("INSERT INTO posts VALUES (2, 'EDGE', 'caption', 'title', 'Image', '2026', 10, 1, 1, 3.0)")
    conn.execute("INSERT INTO dashboard_posts VALUES (3, 'competitor', 'HOT2', 'caption', 'Video', '2026', 10, 1, 'https://instagram.com/p/HOT2', 1, 4.2)")
    conn.commit()

    assert main._queue_v2_auto_pool_hot(conn) == [1, 2]
    rows = conn.execute("SELECT post_account, post_shortcode, priority, production_points, tags FROM queue_requests ORDER BY id").fetchall()
    assert [(row["post_account"], row["post_shortcode"]) for row in rows] == [("competitor", "HOT2"), ("chatgptricks", "HOT1")]
    assert all(row["priority"] == "urgent" and row["production_points"] == 3 and row["tags"] == '["hot"]' for row in rows)
    assert main._queue_v2_auto_pool_hot(conn) == []
    conn.close()
