import sqlite3
from contextlib import contextmanager

from app import apify_sync, db, main


def _snapshot_connect(path):
    @contextmanager
    def connect():
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    return connect


def _create_snapshot_table(connect):
    with connect() as connection:
        connection.execute(
            """
            CREATE TABLE account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                handle TEXT NOT NULL,
                followers_count INTEGER,
                posts_count INTEGER,
                full_name TEXT,
                verified INTEGER NOT NULL DEFAULT 0,
                private INTEGER NOT NULL DEFAULT 0,
                following_count INTEGER,
                captured_at TEXT NOT NULL,
                snapshot_date TEXT,
                UNIQUE(handle, snapshot_date)
            )
            """
        )


def test_account_snapshot_upsert_keeps_one_reading_per_day(monkeypatch, tmp_path):
    connect = _snapshot_connect(tmp_path / "snapshots.sqlite")
    _create_snapshot_table(connect)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "utc_now", lambda: "2026-09-09T12:00:00+00:00")

    db.insert_account_snapshot("ChatGPTricks", 100, 10, "First", False, False, 20)
    db.insert_account_snapshot("chatgptricks", 125, 11, "Updated", True, False, 21)

    with connect() as connection:
        rows = connection.execute("SELECT * FROM account_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["snapshot_date"] == "2026-09-09"
    assert rows[0]["followers_count"] == 125
    assert rows[0]["full_name"] == "Updated"


def test_snapshot_one_account_reuses_today_without_second_apify_read(monkeypatch, tmp_path):
    connect = _snapshot_connect(tmp_path / "snapshots.sqlite")
    _create_snapshot_table(connect)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "utc_now", lambda: "2026-09-09T12:00:00+00:00")
    calls = []

    def preview(handle):
        calls.append(handle)
        return {
            "handle": handle,
            "followers_count": 100,
            "following_count": 20,
            "posts_count": 5,
            "full_name": "ChatGPTricks",
            "verified": False,
            "private": False,
        }

    monkeypatch.setattr(apify_sync, "fetch_profile_preview", preview)
    first = apify_sync.snapshot_one_account("@chatgptricks")
    second = apify_sync.snapshot_one_account("chatgptricks")

    assert calls == ["chatgptricks"]
    assert first["followers_count"] == second["followers_count"] == 100
    assert second["captured_at"] == "2026-09-09T12:00:00+00:00"


def test_forced_snapshot_updates_today_reading(monkeypatch, tmp_path):
    connect = _snapshot_connect(tmp_path / "snapshots.sqlite")
    _create_snapshot_table(connect)
    monkeypatch.setattr(db, "connect", connect)
    readings = iter([100, 125])
    monkeypatch.setattr(db, "utc_now", lambda: "2026-09-09T12:00:00+00:00")
    monkeypatch.setattr(apify_sync, "fetch_profile_preview", lambda handle: {
        "handle": handle, "followers_count": next(readings), "following_count": 20,
        "posts_count": 5, "full_name": "ChatGPTricks", "verified": False, "private": False,
    })

    apify_sync.snapshot_one_account("chatgptricks")
    refreshed = apify_sync.snapshot_one_account("chatgptricks", force=True)

    assert refreshed["followers_count"] == 125
    assert refreshed["captured_at"] == "2026-09-09T12:00:00+00:00"
    with connect() as connection:
        rows = connection.execute("SELECT followers_count FROM account_snapshots").fetchall()
    assert [row["followers_count"] for row in rows] == [125]


def test_all_snapshots_normalizes_legacy_handle_casing(monkeypatch, tmp_path):
    connect = _snapshot_connect(tmp_path / "snapshots.sqlite")
    _create_snapshot_table(connect)
    monkeypatch.setattr(db, "connect", connect)
    with connect() as connection:
        connection.execute(
            "INSERT INTO account_snapshots (handle, followers_count, captured_at) VALUES (?, ?, ?)",
            ("TrasElVeloReal", 2881480, "2026-09-09T12:00:00+00:00"),
        )

    snapshots = db.all_account_snapshots()
    assert snapshots["traselveloreal"][0]["followers_count"] == 2881480


def test_tracker_summary_uses_latest_usable_follower_reading(monkeypatch, tmp_path):
    connect = _snapshot_connect(tmp_path / "summary.sqlite")
    with connect() as connection:
        connection.execute("CREATE TABLE dashboard_posts (account TEXT, published_at TEXT, likes INTEGER)")
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main, "list_accounts", lambda active_only=True: [{"handle": "traselveloreal", "group": "sentient", "label": "Tras el Velo"}])
    monkeypatch.setattr(main, "all_account_snapshots", lambda: {"traselveloreal": [
        {"followers_count": 2881480, "posts_count": 2383, "captured_at": "2026-09-09T06:07:00+00:00", "full_name": "Tras el Velo", "verified": True, "private": False},
        {"followers_count": None, "posts_count": None, "captured_at": "2026-09-09T12:07:00+00:00", "full_name": None, "verified": False, "private": False},
    ]})

    summary = main.tracker_summary()

    assert summary["accounts"][0]["followers"] == 2881480
    assert summary["accounts"][0]["captured_at"] == "2026-09-09T06:07:00+00:00"


def test_dashboard_projection_drops_duplicate_canonical_shortcodes():
    rows = [
        {"id": 10, "shortcode": "NEWER"},
        {"id": 11, "shortcode": "NEWER"},
        {"id": 12, "shortcode": "UNIQUE"},
        {"id": 13, "shortcode": None},
        {"id": 14, "shortcode": None},
    ]
    projected = main._dedupe_canonical_rows(rows)
    assert [row["id"] for row in projected] == [10, 12, 13, 14]


def test_dashboard_projection_drops_duplicate_account_shortcodes():
    posts = [
        {"account": "chatgptricks", "shortcode": "SAME", "id": 1},
        {"account": "chatgptricks", "shortcode": "SAME", "id": 2},
        {"account": "other", "shortcode": "SAME", "id": 3},
        {"account": "chatgptricks", "shortcode": "", "id": 4},
        {"account": "chatgptricks", "shortcode": "", "id": 5},
    ]
    projected = main._dedupe_projected_posts(posts)
    assert [post["id"] for post in projected] == [1, 3, 4, 5]
