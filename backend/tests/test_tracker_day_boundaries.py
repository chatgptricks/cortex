import sqlite3
from contextlib import contextmanager

import pytest

from app import main


@pytest.mark.parametrize("captured_at", ["2026-09-09T23:59:00+00:00", "2026-09-10T00:01:00+00:00"])
def test_reload_keeps_daily_posts_on_costa_rica_snapshot_day(monkeypatch, tmp_path, captured_at):
    @contextmanager
    def connect():
        connection = sqlite3.connect(tmp_path / "tracker.sqlite")
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    with connect() as connection:
        connection.execute("CREATE TABLE dashboard_posts (account TEXT, published_at TEXT, likes INTEGER, video_views INTEGER)")
        connection.executemany(
            "INSERT INTO dashboard_posts VALUES ('example', ?, ?, NULL)",
            [
                ("2026-09-09T05:00:00+00:00", 50),  # September 8 in Costa Rica
                ("2026-09-09T23:00:00+00:00", 100),
                ("2026-09-10T02:00:00+00:00", 300),  # September 9 in Costa Rica
                ("2026-09-10T06:00:00+00:00", 900),  # September 10 in Costa Rica
            ],
        )
    snapshots = [
        {"captured_at": "2026-09-09T05:30:00+00:00", "followers_count": 100},
        {"captured_at": captured_at, "followers_count": 125},
    ]
    for snapshot in snapshots:
        snapshot.update({"posts_count": 4, "following_count": 10, "full_name": "Example", "verified": False, "private": False})
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main, "list_accounts", lambda **kwargs: [{"handle": "example", "label": "Example", "group": "sentient"}])
    monkeypatch.setattr(main, "list_account_snapshots", lambda handle: snapshots)

    history = main.tracker_account_detail("example")["followers_history"]

    assert history[0]["posts_that_day"] == 1
    assert history[0]["avg_likes_that_day"] == 50
    assert history[1]["posts_that_day"] == 2
    assert history[1]["avg_likes_that_day"] == 200
    assert history[1]["followers_gained"] == 25
    assert main._tracker_delta(main._collapse_to_last_per_day(snapshots), 1)["delta"] == 25
