from contextlib import contextmanager
from datetime import UTC, datetime
import sqlite3

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import db, hot_calibration as hot


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "hot.sqlite"
    queries = []

    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(queries.append)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    with connect() as conn:
        conn.executescript("""
            CREATE TABLE accounts(handle TEXT PRIMARY KEY, label TEXT, group_name TEXT,
                is_canonical INTEGER, is_active INTEGER, hot_threshold INTEGER);
            INSERT INTO accounts VALUES('chatgptricks','ChatGPT Tricks','sentient',1,1,600);
            INSERT INTO accounts VALUES('beta','Beta','competitors',0,0,300);
            CREATE TABLE posts(id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT,
                published_at TEXT, updated_at TEXT, post_type_label TEXT, likes INTEGER, comments INTEGER,
                is_hot INTEGER, hot_checked INTEGER, hot_rate_multiplier REAL, hot_marked_at TEXT,
                likes_at_1h INTEGER, likes_at_8h INTEGER, comments_at_8h INTEGER,
                hidden INTEGER, is_deleted INTEGER, raw_json TEXT);
            INSERT INTO posts VALUES(1,'historical','Shared','2026-11-04T12:00:00+00:00','2026-11-05T17:00:00+00:00',
                'Image',900,20,1,1,2.0,'2026-11-04T13:00:00+00:00',120,400,10,0,0,'CANARY_SECRET');
            INSERT INTO posts VALUES(2,'historical','Unknown','2026-11-03T12:00:00+00:00','2026-11-05T16:00:00+00:00',
                'Carousel',NULL,NULL,0,0,NULL,NULL,NULL,NULL,NULL,1,1,'CANARY_SECRET');
            INSERT INTO posts VALUES(3,'historical','Zero','2026-11-02T12:00:00+00:00','2026-11-05T15:00:00+00:00',
                'Video',0,0,0,1,0,NULL,0,0,0,0,0,'CANARY_SECRET');
            INSERT INTO posts VALUES(4,'historical','SixtyDays','2026-09-06T12:00:00+00:00','2026-09-07T12:00:00+00:00',
                'Image',1000,25,1,1,8,NULL,800,900,20,0,0,'CANARY_SECRET');
            INSERT INTO posts VALUES(5,'ab','InternalDraft','2026-11-05T12:00:00+00:00','2026-11-05T17:00:00+00:00',
                'Image',1000,25,1,1,8,NULL,999999,999999,20,0,0,'CANARY_SECRET');
            INSERT INTO posts VALUES(6,'historical','TooOld','2026-07-01T12:00:00+00:00','2026-07-02T12:00:00+00:00',
                'Image',1000,25,1,1,8,NULL,999999,999999,20,0,0,'CANARY_SECRET');
            INSERT INTO posts VALUES(7,'historical','Future','2026-11-06T12:00:00+00:00','2026-11-06T12:00:00+00:00',
                'Image',1000,25,1,1,8,NULL,999999,999999,20,0,0,'CANARY_SECRET');
            CREATE TABLE dashboard_posts(id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
                published_at TEXT, updated_at TEXT, post_type_label TEXT, product_type TEXT,
                likes INTEGER, comments INTEGER, video_views INTEGER, is_hot INTEGER, hot_checked INTEGER,
                hot_rate_multiplier REAL, likes_at_1h INTEGER, likes_at_8h INTEGER, comments_at_8h INTEGER,
                hidden INTEGER, is_deleted INTEGER, raw_json TEXT);
            INSERT INTO dashboard_posts VALUES(1,'chatgptricks','Shared','2026-11-04T12:00:00+00:00','2026-11-05T18:00:00+00:00',
                'Reel','clips',999999,999999,999999,0,1,0,999999,999999,999999,0,0,'CANARY_SECRET');
            INSERT INTO dashboard_posts VALUES(2,'beta','Inactive','2026-11-05T12:00:00+00:00','2026-11-05T15:00:00+00:00',
                'Video','clips',500,7,1200,0,1,0.5,150,NULL,NULL,0,0,'CANARY_SECRET');
            CREATE TABLE engagement_observations(shortcode TEXT PRIMARY KEY, observed_at TEXT, raw_json TEXT);
            INSERT INTO engagement_observations VALUES('Shared','2026-11-05T17:30:00+00:00','CANARY_PROVIDER_PAYLOAD');
        """)
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(hot, "_now", lambda: datetime(2026, 11, 5, 18, tzinfo=UTC))
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        role = request.headers.get("x-role", "admin")
        request.state.is_admin = role == "admin"
        request.state.is_dev = role == "dev"
        request.state.agent_access_mode = "read"
        return await call_next(request)

    app.include_router(hot.router)
    queries.clear()
    return TestClient(app), connect, queries


PATH = "/api/admin/engagement/checkpoints"


def test_legacy_evidence_is_available_without_new_table_or_invented_times(setup):
    client, _, queries = setup
    response = client.get(PATH)
    assert response.status_code == 200
    value = response.json()
    assert response.headers["cache-control"] == "private, no-store"
    assert value["pagination"]["total"] == 4
    shared = next(post for post in value["posts"] if post["shortcode"] == "Shared")
    assert shared["likes_at_1h"] == 120
    assert shared["checkpoints"]["first_hot_check"] == {
        "likes": 120, "comments": None, "views": None, "observed_at": None,
        "evaluated_at": None, "age_hours": None, "hot_threshold": None,
        "is_hot": True, "rate_multiplier": 2, "source": "legacy_post_columns",
    }
    assert shared["checkpoints"]["8h"]["likes"] == 400
    assert shared["checkpoints"]["24h"] is None
    assert shared["checkpoints"]["48h"] is None
    assert shared["current_metrics"]["last_observation_at"] == "2026-11-05T17:30:00+00:00"
    assert shared["current_metrics"]["likes"] == 999999
    assert shared["current_metrics"]["views"] == 999999
    assert shared["current_metrics"]["updated_at"] == "2026-11-05T18:00:00+00:00"
    assert shared["current_metrics"]["updated_at"] != shared["checkpoints"]["first_hot_check"]["observed_at"]
    assert "CANARY" not in response.text
    assert all("raw_json" not in query.lower() for query in queries)
    assert not [query for query in queries if query.strip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}]


def test_deduplication_pagination_and_scope_coverage_are_consistent(setup):
    client, _, _ = setup
    first = client.get(PATH, params={"limit": 1}).json()
    assert first["posts"][0]["shortcode"] == "Inactive"
    assert first["posts"][0]["is_active_account"] is False
    assert first["pagination"] == {"limit": 1, "offset": 0, "total": 4, "has_more": True, "next_offset": 1}
    pages = [first]
    for offset in (1, 2, 3):
        pages.append(client.get(PATH, params={"limit": 1, "offset": offset, "as_of": first["as_of"]}).json())
    assert [page["posts"][0]["shortcode"] for page in pages] == ["Inactive", "Shared", "Unknown", "Zero"]
    assert pages[-1]["pagination"]["next_offset"] is None
    assert first["coverage"]["full_filter"]["post_count"] == 4
    assert first["coverage"]["page"]["post_count"] == 1
    assert first["coverage"]["full_filter"]["checkpoints"]["first_hot_check"]["likes_known"] == 3
    scoped = client.get(PATH, params={"account": "@CHATGPTRICKS"}).json()
    assert scoped["pagination"]["total"] == 3
    assert {post["account"] for post in scoped["posts"]} == {"chatgptricks"}
    assert [account["handle"] for account in scoped["accounts"]] == ["chatgptricks"]
    assert client.get(PATH, params={"account": "missing"}).status_code == 404
    assert client.get(PATH, params={"account": "beta,chatgptricks"}).status_code == 422


def test_unknown_and_zero_are_distinct_and_summaries_always_use_30_days(setup):
    client, _, _ = setup
    value = client.get(PATH, params={"days": 90}).json()
    assert value["pagination"]["total"] == 5
    posts = {post["shortcode"]: post for post in value["posts"]}
    assert posts["Zero"]["checkpoints"]["first_hot_check"]["likes"] == 0
    assert posts["Zero"]["checkpoints"]["8h"]["comments"] == 0
    assert posts["Unknown"]["checkpoints"]["first_hot_check"] is None
    assert posts["Unknown"]["current_metrics"]["likes"] is None
    assert posts["Unknown"]["hidden"] and posts["Unknown"]["is_deleted"]
    summary = next(account for account in value["accounts"] if account["handle"] == "chatgptricks")
    assert summary["avg_likes"] == 60
    assert summary["avg_likes_sample_size"] == 2
    assert summary["suggested_hot_threshold"] == 100
    assert summary["posts_in_summary_window"] == 3
    assert value["window"]["days"] == 90 and value["summary_window"]["days"] == 30
    # A narrow export must still include the complete 30-day summary.
    narrow = client.get(PATH, params={"days": 1}).json()
    assert narrow["pagination"]["total"] == 1
    assert next(account for account in narrow["accounts"] if account["handle"] == "chatgptricks")["avg_likes_sample_size"] == 2


def test_new_checkpoint_metadata_overrides_legacy_without_reconstructing_history(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE engagement_checkpoints(account TEXT, shortcode TEXT, checkpoint TEXT,
                likes INTEGER, comments INTEGER, views INTEGER, observed_at TEXT, evaluated_at TEXT,
                age_hours REAL, hot_threshold INTEGER, is_hot INTEGER, rate_multiplier REAL, source TEXT,
                PRIMARY KEY(account, shortcode, checkpoint));
            INSERT INTO engagement_checkpoints VALUES('chatgptricks','Shared','first_hot_check',120,4,200,
                '2026-11-04T13:05:00+00:00','2026-11-04T13:00:00+00:00',1,60,1,2,'apify_ingestion');
            INSERT INTO engagement_checkpoints VALUES('chatgptricks','Shared','24h',800,18,1300,
                '2026-11-05T12:05:00+00:00','2026-11-05T12:00:00+00:00',24,NULL,NULL,NULL,'apify_ingestion');
            INSERT INTO engagement_checkpoints VALUES('beta','Inactive','48h',NULL,12,3000,
                '2026-11-05T16:00:00+00:00','2026-11-05T16:00:00+00:00',48,NULL,NULL,NULL,'manual_refresh');
        """)
    value = client.get(PATH, params={"limit": 1, "offset": 1}).json()
    shared = value["posts"][0]
    assert shared["shortcode"] == "Shared"
    first = shared["checkpoints"]["first_hot_check"]
    assert first["observed_at"] == "2026-11-04T13:05:00+00:00"
    assert first["evaluated_at"] == "2026-11-04T13:00:00+00:00"
    assert first["hot_threshold"] == 60 and shared["current_hot_threshold"] == 600
    assert first["age_hours"] == 1
    assert shared["checkpoints"]["24h"]["likes"] == 800
    assert value["coverage"]["full_filter"]["checkpoints"]["24h"] == {"likes_known": 1, "with_observed_at": 1}
    assert value["coverage"]["page"]["checkpoints"]["first_hot_check"] == {"likes_known": 1, "with_observed_at": 1}
    inactive = client.get(PATH, params={"account": "beta"}).json()["posts"][0]
    assert inactive["checkpoints"]["48h"]["likes"] is None
    assert inactive["checkpoints"]["48h"]["comments"] == 12


def test_latest_unknown_scalar_does_not_resurrect_stale_metric_or_change_canonical_evidence(setup):
    client, connect, _ = setup
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = NULL, comments = NULL WHERE account = 'chatgptricks'")
        conn.execute("UPDATE posts SET post_type_label = 'Reel' WHERE shortcode = 'Shared'")
    value = client.get(PATH, params={"account": "chatgptricks"}).json()
    shared = next(post for post in value["posts"] if post["shortcode"] == "Shared")
    assert shared["current_metrics"]["likes"] is None
    assert shared["current_metrics"]["comments"] is None
    assert shared["checkpoints"]["first_hot_check"]["likes"] == 120
    assert shared["is_hot"] is True
    assert shared["format"] == "Reel"


@pytest.mark.parametrize("role,status", [("admin", 200), ("dev", 200), ("vc", 403), ("pd", 403), ("", 403)])
def test_explicit_permission_gate_allows_read_only_admin_or_dev(setup, role, status):
    client, _, _ = setup
    assert client.get(PATH, headers={"x-role": role}).status_code == status


@pytest.mark.parametrize("params", [{"days": 0}, {"days": 91}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"as_of": "2026-11-06T18:00:00Z"}])
def test_export_bounds_are_validated(setup, params):
    client, _, _ = setup
    assert client.get(PATH, params=params).status_code == 422
