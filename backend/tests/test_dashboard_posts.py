from contextlib import contextmanager
import sqlite3

from app import main


def test_dashboard_posts_payload_pages_global_feed_without_loading_every_row(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY, title TEXT, caption TEXT, hook_text TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER,
            post_type_label TEXT, shortcode TEXT, is_animated INTEGER,
            source_row_number INTEGER, section TEXT, is_hot INTEGER,
            hot_rate_multiplier REAL, is_promo INTEGER, hidden INTEGER,
            is_deleted INTEGER
        );
        CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, caption TEXT,
            post_type_label TEXT, is_animated INTEGER, permalink TEXT,
            is_hot INTEGER, hot_rate_multiplier REAL, hook_text TEXT,
            music_song TEXT, music_artist TEXT, music_audio_id TEXT,
            uses_original_audio INTEGER, is_promo INTEGER, hidden INTEGER,
            is_deleted INTEGER, transcript TEXT
        );
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY, post_account TEXT, post_shortcode TEXT,
            status TEXT, designer_email TEXT, coordinator_email TEXT,
            production_points INTEGER, actual_started_at TEXT, completed_at TEXT,
            final_permalink TEXT, final_permalinks TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO posts VALUES (1, 'Canonical', 'caption', '', '2026-09-08T12:00:00Z', 10, 1, 'Image', 'CAN', 0, 1, 'single', 0, NULL, 0, 0, 0)"
    )
    conn.executemany(
        "INSERT INTO dashboard_posts VALUES (?, 'competitor', ?, ?, ?, 1, ?, 'Image', 0, ?, 0, NULL, '', NULL, NULL, NULL, 0, 0, 0, 0, '')",
        [
            (2, "NEW", "2026-09-08T13:00:00Z", 20, "new", "https://instagram.com/p/NEW"),
            (3, "OLD", "2026-09-07T13:00:00Z", 30, "old", "https://instagram.com/p/OLD"),
        ],
    )
    conn.commit()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(main, "connect", fake_connect)
    monkeypatch.setattr(
        main,
        "list_accounts",
        lambda active_only=True: [
            # Simulate a legacy Postgres registry where chatgptricks retained
            # the old default flag while another row is incorrectly marked.
            {"handle": "chatgptricks", "group": "sentient", "is_canonical": False},
            {"handle": "competitor", "group": "competitors", "is_canonical": True},
        ],
    )

    first = main._dashboard_posts_payload(limit=2, offset=0)
    assert [post["shortcode"] for post in first["posts"]] == ["NEW", "CAN"]
    assert first["summary"]["Exported posts"] == 3
    assert first["pagination"] == {"offset": 0, "limit": 2, "total": 3, "hasMore": True, "nextOffset": 2}

    second = main._dashboard_posts_payload(limit=2, offset=2)
    assert [post["shortcode"] for post in second["posts"]] == ["OLD"]
    assert second["pagination"]["hasMore"] is False
    assert second["pagination"]["nextOffset"] is None


def test_dashboard_posts_payload_keeps_legacy_catalogue_when_registry_row_is_missing(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY, title TEXT, caption TEXT, hook_text TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER,
            post_type_label TEXT, shortcode TEXT, is_animated INTEGER,
            source_row_number INTEGER, section TEXT, is_hot INTEGER,
            hot_rate_multiplier REAL, is_promo INTEGER, hidden INTEGER,
            is_deleted INTEGER
        );
        CREATE TABLE dashboard_posts (
            id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, caption TEXT,
            post_type_label TEXT, is_animated INTEGER, permalink TEXT,
            is_hot INTEGER, hot_rate_multiplier REAL, hook_text TEXT,
            music_song TEXT, music_artist TEXT, music_audio_id TEXT,
            uses_original_audio INTEGER, is_promo INTEGER, hidden INTEGER,
            is_deleted INTEGER, transcript TEXT
        );
        CREATE TABLE queue_requests (
            id INTEGER PRIMARY KEY, post_account TEXT, post_shortcode TEXT,
            status TEXT, designer_email TEXT, coordinator_email TEXT,
            production_points INTEGER, actual_started_at TEXT, completed_at TEXT,
            final_permalink TEXT, final_permalinks TEXT
        );
        INSERT INTO posts VALUES (1, 'Legacy', 'caption', '', '2026-09-08T12:00:00Z', 10, 1, 'Image', 'LEGACY', 0, 1, 'single', 0, NULL, 0, 0, 0);
        """
    )

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(main, "connect", fake_connect)
    monkeypatch.setattr(
        main,
        "list_accounts",
        lambda active_only=True: [
            {"handle": "competitor", "group": "competitors", "is_canonical": True},
        ],
    )

    payload = main._dashboard_posts_payload(limit=10, offset=0)
    assert [post["shortcode"] for post in payload["posts"]] == ["LEGACY"]
    assert payload["posts"][0]["account"] == "chatgptricks"
    assert payload["summary"]["Exported posts"] == 1
