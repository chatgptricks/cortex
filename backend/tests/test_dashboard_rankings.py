from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import dashboard_rankings as ranks, db

NOW = datetime(2026, 11, 6, 0, 49, 13, tzinfo=UTC)


@pytest.fixture
def stored(tmp_path, monkeypatch):
    path = tmp_path / "ranking.sqlite"
    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE accounts(handle TEXT PRIMARY KEY, is_active INTEGER, is_canonical INTEGER, hot_threshold INTEGER, label TEXT, group_name TEXT, research_enabled INTEGER);
            INSERT INTO accounts VALUES('alpha',1,1,100,'Alpha','sentient',1),('beta',1,0,100,'Beta','sentient',1);
            CREATE TABLE posts(id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT, published_at TEXT, updated_at TEXT, likes INTEGER, comments INTEGER, raw_json TEXT);
            CREATE TABLE dashboard_posts(id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, published_at TEXT, updated_at TEXT, likes INTEGER, comments INTEGER, video_views INTEGER, hidden INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0, raw_json TEXT);
            CREATE TABLE engagement_observations(shortcode TEXT PRIMARY KEY, observed_at TEXT, raw_json TEXT);
        """)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(ranks, "_now", lambda: NOW)
    monkeypatch.setattr(ranks, "list_accounts", lambda **kwargs: [{"handle": "alpha"}, {"handle": "beta"}])
    return connect


def add(conn, code, likes, published=None, account="beta", **extra):
    values = {"account": account, "shortcode": code, "published_at": (published or NOW - timedelta(hours=2)).isoformat(), "updated_at": (NOW - timedelta(hours=1)).isoformat(), "likes": likes, "comments": 2, "video_views": likes, **extra}
    conn.execute(f"INSERT INTO dashboard_posts ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))


def test_all_rows_current_metrics_exact_rolling_window_and_internal_link(stored):
    with stored() as conn:
        for i in range(225):
            add(conn, f"Post{i:03}", 1000 - i)
        # Winner is outside the first two batches, with a low stale index count.
        conn.execute("INSERT INTO engagement_observations VALUES(?,?,?)", ("Post224", NOW.isoformat(), json.dumps({"shortCode": "Post224", "likesCount": 45000})))
        add(conn, "TooOld", 999999, NOW - timedelta(hours=24, seconds=1))
        add(conn, "Future", 999999, NOW + timedelta(seconds=1))
        add(conn, "Boundary", 33, NOW - timedelta(hours=24))
        add(conn, "Unknown", -1)
        add(conn, "Hidden", 999999, hidden=1)
        add(conn, "Deleted", 999999, is_deleted=1)
    result = ranks.rank_posts(["alpha", "beta"], NOW - timedelta(hours=24), NOW, "likes", 1)
    assert result["data"][0]["shortcode"] == "Post224"
    assert result["data"][0]["likes"] == 45000
    assert result["data"][0]["dashboard_url"] == "https://sentientdash.app/index.html?post=beta%3APost224"
    assert result["coverage"] == {"complete": True, "accounts": 2, "matching_rows": 227, "posts": 227, "unknown_metric": 1}
    assert "instagram.com" not in json.dumps(result)


def test_missing_current_likes_do_not_resurrect_old_winner(stored):
    with stored() as conn:
        add(conn, "OldWinner", 99999)
        add(conn, "Winner", 300)
        conn.execute("INSERT INTO engagement_observations VALUES(?,?,?)", ("OldWinner", NOW.isoformat(), json.dumps({"shortCode": "OldWinner", "likesCount": None})))
    result = ranks.rank_posts(["beta"], NOW - timedelta(hours=24), NOW, "likes", 1)
    assert result["data"][0]["shortcode"] == "Winner"
    assert result["coverage"]["unknown_metric"] == 1


def test_clock_range_validation_and_account_scope(stored):
    with stored() as conn:
        add(conn, "Alpha", 1, account="alpha")
        add(conn, "Beta", 20)
    app = FastAPI(); app.include_router(ranks.router)
    with TestClient(app) as client:
        response = client.get("/api/insights/top-posts?hours=24&account=alpha")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["window"]["to"] == NOW.isoformat()
        assert response.json()["data"][0]["shortcode"] == "Alpha"
        assert client.get("/api/insights/top-posts?account=unavailable").status_code == 404
        assert client.get("/api/insights/top-posts?start=2026-11-05T00:00:00").status_code == 422
        assert client.get("/api/insights/top-posts?hours=745").status_code == 422


def test_search_complete_captions_thresholds_latest_promos_and_all_keys(stored, monkeypatch):
    from app import dashboard_post_search as search
    monkeypatch.setattr(search, "list_accounts", lambda **kwargs: [{"handle": "alpha"}, {"handle": "beta"}])
    with stored() as conn:
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN caption TEXT")
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN is_promo INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN hook_text TEXT")
        add(conn, "LongCaption", 5001, caption="intro " * 120 + "Elon Musk", is_promo=1)
        add(conn, "Boundary", 5000, caption="Elon Musk")
        add(conn, "CoverOnly", 9000, caption="other caption", hook_text="Elon Musk")
        add(conn, "Fresh", 100, caption="Elon Musk", published=NOW - timedelta(minutes=20), is_promo=1)
        conn.execute("INSERT INTO engagement_observations VALUES(?,?,?)", ("Fresh", NOW.isoformat(), json.dumps({"likesCount": 8000})))
    app = FastAPI(); app.include_router(search.router)
    with TestClient(app) as client:
        result = client.get('/api/dashboard/posts/search', params={"q": "Elon Musk", "text_scope": "caption", "min_likes": 5001, "include_keys": True, "limit": 1}).json()
        assert result["matching_post_keys"] == ["beta:Fresh", "beta:LongCaption"]
        assert result["pagination"]["total"] == 2 and result["pagination"]["next_offset"] == 1
        assert result["data"][0]["likes"] == 8000
        promos = client.get('/api/dashboard/posts/search', params={"promos_only": True, "limit": 10}).json()
        assert [p["shortcode"] for p in promos["data"]] == ["Fresh", "LongCaption"]
        assert client.get('/api/dashboard/posts/search', params={"account": "unavailable"}).status_code == 404


def test_format_rankings_all_time_and_offset_dates(stored):
    with stored() as conn:
        conn.execute("ALTER TABLE dashboard_posts ADD COLUMN post_type_label TEXT")
        add(conn, "CarouselWinner", 9999, post_type_label="Carousel")
        add(conn, "ReelWinner", 100, post_type_label="Reel", published=NOW - timedelta(days=100))
        add(conn, "CurrentReel", 10, post_type_label="Reel", published=NOW - timedelta(minutes=5))
        add(conn, "OffsetReel", 20, post_type_label="Reel")
        conn.execute("UPDATE dashboard_posts SET published_at=? WHERE shortcode='OffsetReel'", ((NOW - timedelta(minutes=1)).astimezone(__import__('datetime').timezone(timedelta(hours=12))).isoformat(),))
    app = FastAPI(); app.include_router(ranks.router)
    with TestClient(app) as client:
        result = client.get('/api/insights/top-posts', params={"post_format": "reel", "account": "beta", "all_time": True, "limit": 3}).json()
        assert [p['shortcode'] for p in result['data']] == ['ReelWinner', 'OffsetReel', 'CurrentReel']
        recent = client.get('/api/insights/top-posts', params={"post_format": "reel"}).json()
        assert recent['data'][0]['shortcode'] == 'OffsetReel'
