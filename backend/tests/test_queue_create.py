import sqlite3
from contextlib import contextmanager

from app import main


def test_vc_can_create_custom_queue_post(monkeypatch, tmp_path):
    database = tmp_path / "queue-create.sqlite3"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (handle TEXT, label TEXT, group_name TEXT, is_active INTEGER, is_canonical INTEGER);
        CREATE TABLE dashboard_posts (
            id INTEGER, account TEXT, shortcode TEXT, caption TEXT, post_type_label TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, permalink TEXT,
            is_hot INTEGER, hot_rate_multiplier REAL
        );
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_account TEXT, post_shortcode TEXT,
            post_title TEXT NOT NULL DEFAULT '', is_custom INTEGER NOT NULL DEFAULT 0,
            post_permalink TEXT NOT NULL DEFAULT '', post_caption TEXT NOT NULL DEFAULT '',
            post_type TEXT NOT NULL DEFAULT '', cover_url TEXT NOT NULL DEFAULT '',
            production_points INTEGER, priority TEXT, deadline_at TEXT, tags TEXT, brief TEXT,
            notes TEXT, reference_links TEXT, attachments TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pool', designer_email TEXT, coordinator_email TEXT,
            recommended_accounts TEXT NOT NULL DEFAULT '[]', scheduled_date TEXT,
            scheduled_start_minutes INTEGER, actual_started_at TEXT, completed_at TEXT,
            closed_at TEXT, final_permalink TEXT, cancellation_reason TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE queue_request_events (
            id INTEGER PRIMARY KEY, request_id INTEGER, actor_email TEXT,
            event_type TEXT, details TEXT, created_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO accounts VALUES ('chatgptricks', 'ChatGPTricks', 'sentient', 1, 0)")
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
    monkeypatch.setattr(main, "list_accounts", lambda active_only=False: [{"handle": "chatgptricks", "label": "ChatGPTricks", "group": "sentient", "is_active": True, "is_canonical": False}])
    monkeypatch.setattr(main, "_queue_v2_access", lambda request, coordinator=False: ("vc@example.com", False, ["vc", "pd"]))
    monkeypatch.setattr(main, "_queue_v2_publish", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main, "_queue_v2_slack_log", lambda **kwargs: True)

    result = main.dashboard_queue_v2_create(
        request=None, account="chatgptricks", title="Launch carousel", post_type="Carousel",
        production_points=5, priority="high", tags="content,design", brief="Build the launch story.",
        notes="Use the new template.", references='["https://example.com/brief"]',
    )

    created = result["request"]
    assert created["isCustom"] is True
    assert created["post"]["title"] == "Launch carousel"
    assert created["post"]["account"] == "chatgptricks"
    assert created["productionPoints"] == 5
    assert created["priority"] == "high"
    assert created["status"] == "pool"
    assert created["references"] == ["https://example.com/brief"]
    assert created["post"]["shortcode"].startswith("manual-")
