from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import promos_engagement as engagement

NOW = datetime(2026, 10, 16, 18, tzinfo=UTC)


@pytest.fixture
def engagement_db(monkeypatch, tmp_path):
    path = tmp_path / "promos-engagement.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (handle TEXT PRIMARY KEY, label TEXT, group_name TEXT,
            category TEXT, subcategory TEXT, is_canonical INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1, avatar_path TEXT, research_enabled INTEGER DEFAULT 1,
            promos_enabled INTEGER DEFAULT 1, hot_threshold INTEGER, scrape_mode TEXT);
        CREATE TABLE posts (id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, raw_json TEXT,
            updated_at TEXT, hidden INTEGER, is_deleted INTEGER);
        CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, raw_json TEXT,
            video_views INTEGER, video_plays INTEGER, slide_count INTEGER,
            post_type_label TEXT, product_type TEXT, updated_at TEXT, enriched_at TEXT,
            hidden INTEGER, is_deleted INTEGER);
        CREATE TABLE account_snapshots (id INTEGER PRIMARY KEY, handle TEXT,
            followers_count INTEGER, captured_at TEXT);
        CREATE TABLE engagement_observations (shortcode TEXT PRIMARY KEY,
            observed_at TEXT, raw_json TEXT);
        INSERT INTO accounts (handle, label, category) VALUES ('sample', 'Sample', 'competitors');
    """)
    conn.commit()
    conn.close()
    statements = []

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(statements.append)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(engagement.db, "connect", connect)
    return connect, statements


def insert(conn, table, **values):
    names = list(values)
    conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(values.values()))


def post(conn, code, **values):
    insert(conn, "dashboard_posts", account="sample", shortcode=code,
           **{"published_at": "2026-10-15T12:00:00Z", **values})


def test_paired_core_sample_and_full_sample_do_not_mix_missing_with_zero(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "paired", likes=100, comments=10,
             raw_json=json.dumps({"sharesCount": 20, "savesCount": 0, "impressionsCount": 1000}))
        post(conn, "missing_comments", likes=200, comments=None)
        post(conn, "hidden_likes", likes=3, comments=30, raw_json='{"likesCount":999}')
        post(conn, "negative_likes", likes=-1, comments=50)
        post(conn, "known_zero", likes=20, comments=0)
        insert(conn, "account_snapshots", handle="sample", followers_count=1000, captured_at="2026-10-14T12:00:00Z")
        insert(conn, "account_snapshots", handle="sample", followers_count=None, captured_at="2026-10-15T12:00:00Z")
        insert(conn, "account_snapshots", handle="sample", followers_count=9999, captured_at="2026-10-17T12:00:00Z")
    report = engagement.build_promos_engagement(now=NOW)
    account = report["accounts"][0]
    assert account["post_count"] == 5
    assert account["engagement_post_count"] == 2
    assert account["average_engagements"] == 65
    assert account["engagement_rate_pct"] == 6.5
    assert account["engagement_coverage_pct"] == 40
    assert account["followers"] == 1000
    assert account["followers_captured_at"] == "2026-10-14T12:00:00Z"
    assert account["metrics"]["likes"] == {"count": 3, "total": 320, "average": 320 / 3, "coverage_pct": 60}
    assert account["metrics"]["comments"] == {"count": 4, "total": 90, "average": 22.5, "coverage_pct": 80}
    assert account["metrics"]["saves"]["average"] == 0
    assert account["metrics"]["impressions"]["average"] == 1000
    assert account["metrics"]["video_views"]["average"] is None
    assert account["complete_engagement_post_count"] == 1
    assert account["average_complete_engagements"] == 130


def test_latest_observation_overrides_stale_payload_and_duplicate_canonical_is_one_post(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", shortcode="same", section="historical", likes=100, comments=10,
               published_at="2026-10-15T12:00:00Z", updated_at="2026-10-15T13:00:00Z")
        post(conn, "same", likes=80, comments=8, video_views=400, video_plays=600,
             updated_at="2026-10-15T11:00:00Z", raw_json='{"sharesCount":1,"savesCount":2}')
        insert(conn, "engagement_observations", shortcode="same", observed_at="2026-10-16T12:00:00Z",
               raw_json=json.dumps({"shortCode": "same", "likesCount": 200, "commentsCount": 20,
                                    "sharesCount": 5, "savesCount": 10, "impressionsCount": 2000}))
        insert(conn, "posts", shortcode="experiment", section="ab", likes=10000, comments=100,
               published_at="2026-10-15T12:00:00Z")
        insert(conn, "posts", section="historical", likes=10000, comments=100)
    account = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert account["post_count"] == 1
    assert account["average_engagements"] == 220
    assert account["average_complete_engagements"] == 235
    assert account["metrics"]["video_views"]["average"] == 400
    assert account["metrics"]["video_plays"]["average"] == 600
    assert account["metrics_updated_at"] == "2026-10-16T12:00:00+00:00"


def test_publication_window_format_and_active_promos_roster_include_leads(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        for code, date, kind in (("recent_reel", "2026-10-15T12:00:00Z", "clips"),
                                 ("older", "2026-09-01T12:00:00Z", None),
                                 ("old", "2026-01-01T12:00:00Z", None),
                                 ("future", "2026-10-17T12:00:00Z", None),
                                 ("undated", None, None)):
            post(conn, code, likes=100, comments=0, published_at=date, product_type=kind)
        post(conn, "carousel", likes=100, comments=0, post_type_label="Carousel (carousel_container)")
        post(conn, "hidden", likes=500, comments=50, hidden=1)
        post(conn, "deleted", likes=500, comments=50, is_deleted=1)
        for handle, active, enabled, research in (("lead", 1, 1, 0), ("inactive", 0, 1, 1), ("disabled", 1, 0, 1)):
            insert(conn, "accounts", handle=handle, label=handle, category="leads", is_active=active,
                   promos_enabled=enabled, research_enabled=research)
    recent = engagement.build_promos_engagement(now=NOW)
    assert {account["handle"] for account in recent["accounts"]} == {"sample", "lead"}
    sample = next(account for account in recent["accounts"] if account["handle"] == "sample")
    assert sample["post_count"] == 2
    assert sample["first_post_at"] == "2026-10-15T12:00:00+00:00"
    assert sample["engagement_rate_pct"] is None
    assert engagement.build_promos_engagement(days=90, now=NOW)["accounts"][0]["post_count"] == 3
    stored = engagement.build_promos_engagement(days=0, now=NOW)["accounts"][0]
    assert stored["post_count"] == 5
    assert stored["undated_post_count"] == 1
    reel = engagement.build_promos_engagement(format="reel", now=NOW)["accounts"][0]
    assert reel["post_count"] == 1
    assert engagement.build_promos_engagement(format="carousel", now=NOW)["accounts"][0]["post_count"] == 1
    assert engagement.build_promos_engagement(format="image", now=NOW)["accounts"][0]["post_count"] == 0
    empty = next(account for account in recent["accounts"] if account["handle"] == "lead")
    assert empty["average_engagements"] is None
    assert empty["metrics"]["shares"]["total"] is None
    assert recent["period"]["from"] == "2026-09-16T18:00:00+00:00"


def test_hidden_flag_on_either_duplicate_removes_post_and_counts_are_fresh_read_only(engagement_db, monkeypatch):
    from app import apify_sync
    connect, statements = engagement_db
    monkeypatch.setattr(apify_sync, "_run_apify_actor_and_fetch", lambda *a, **k: pytest.fail("Paid scrape during benchmark"))
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", shortcode="hidden_copy", section="historical", hidden=1,
               likes=9999, comments=99, published_at="2026-10-15T12:00:00Z")
        post(conn, "hidden_copy", likes=9999, comments=99, updated_at="2026-10-16T12:00:00Z")
        post(conn, "visible", likes=10, comments=0)
    statements.clear()
    first = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert first["post_count"] == 1
    assert first["average_engagements"] == 10
    assert all(statement.lstrip().startswith(("SELECT", "PRAGMA")) for statement in statements)
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = 30 WHERE shortcode = 'visible'")
    assert engagement.build_promos_engagement(now=NOW)["accounts"][0]["average_engagements"] == 30


def test_metadata_edits_cannot_revert_observed_counts_or_claim_metric_freshness(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", shortcode="same", section="historical", likes=300, comments=30,
               published_at="2026-10-15T12:00:00Z", updated_at="2026-10-15T12:00:00Z")
        # A transcript/OCR repair touched a stale duplicate's generic timestamp.
        post(conn, "same", likes=100, comments=10, updated_at="2026-10-16T12:00:00Z")
        insert(conn, "engagement_observations", shortcode="same", observed_at="2026-10-15T12:00:00Z",
               raw_json='{"shortCode":"same","likesCount":300,"commentsCount":30}')
    account = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 330
    assert account["metrics_updated_at"] == "2026-10-15T12:00:00+00:00"
    with connect() as conn:
        # Reverse the tables: a canonical cover repair has the same outcome.
        conn.execute("UPDATE posts SET likes = 100, comments = 10, updated_at = '2026-10-16T13:00:00Z'")
        conn.execute("UPDATE dashboard_posts SET likes = 300, comments = 30, updated_at = '2026-10-15T12:00:00Z'")
    account = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 330
    assert account["metrics_updated_at"] == "2026-10-15T12:00:00+00:00"


def test_unstamped_promoted_metrics_remain_available_without_claiming_update_time(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "fallback", likes=100, comments=10, updated_at="2026-10-16T12:00:00Z")
    account = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 110
    assert account["metrics_updated_at"] is None


@pytest.mark.parametrize("hidden_likes", [None, -1, 0, 3])
def test_older_visible_observation_does_not_resurrect_newer_unavailable_likes(engagement_db, hidden_likes):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "hidden", likes=hidden_likes, comments=10, updated_at="2026-10-16T12:00:00Z")
        insert(conn, "engagement_observations", shortcode="hidden", observed_at="2026-10-15T12:00:00Z",
               raw_json='{"shortCode":"hidden","likesCount":300,"commentsCount":30}')
    account = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert account["metrics"]["likes"]["average"] is None
    assert account["engagement_post_count"] == 0


def test_newer_observation_can_decrease_counts_and_newer_enrichment_can_replace_it(engagement_db):
    connect, _ = engagement_db
    with connect() as conn:
        post(conn, "changing", likes=300, comments=30, updated_at="2026-10-16T12:00:00Z")
        insert(conn, "engagement_observations", shortcode="changing", observed_at="2026-10-15T12:00:00Z",
               raw_json='{"shortCode":"changing","likesCount":200,"commentsCount":20}')
    account = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 220
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET enriched_at = '2026-10-16T10:00:00Z', raw_json = ?",
                     ('{"likesCount":400,"commentsCount":40,"sharesCount":2}',))
    account = engagement.build_promos_engagement(now=NOW)["accounts"][0]
    assert account["average_engagements"] == 440
    assert account["metrics"]["shares"]["average"] == 2
    assert account["metrics_updated_at"] == "2026-10-16T10:00:00+00:00"


@pytest.fixture
def endpoint_client(monkeypatch):
    from app import main
    state = {"calls": [], "role": "sales"}

    def build(**params):
        state["calls"].append(params)
        return {"accounts": [], "period": {"days": params["days"]}}

    monkeypatch.setattr(main, "build_promos_engagement", build)
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": "member@example.com", "uid": "test"})
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: {
        "is_admin": state["role"] == "admin", "operating_role": state["role"], "operating_roles": f'["{state["role"]}"]',
    })
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.add_api_route("/api/admin/promos/engagement", main.admin_promos_engagement)
    return TestClient(app), state


@pytest.mark.parametrize("role", ["sales", "pd", "vc", "admin"])
def test_promos_members_can_read_endpoint_without_settings_or_tracker_permission(endpoint_client, role):
    client, state = endpoint_client
    state["role"] = role
    response = client.get("/api/admin/promos/engagement?days=90&format=reel", headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert state["calls"] == [{"days": 90, "format": "reel"}]
    assert "no-store" in response.headers["cache-control"]


def test_engagement_endpoint_rejects_unauthenticated_and_invalid_queries(endpoint_client):
    client, state = endpoint_client
    assert client.get("/api/admin/promos/engagement").status_code == 401
    for query in ("days=15", "days=-1", "days=garbage", "format=stories"):
        assert client.get(f"/api/admin/promos/engagement?{query}", headers={"Authorization": "Bearer test"}).status_code == 422
    assert state["calls"] == []


def test_default_and_all_stored_windows_and_real_route_order(endpoint_client):
    from app import main
    client, state = endpoint_client
    headers = {"Authorization": "Bearer test"}
    assert client.get("/api/admin/promos/engagement", headers=headers).status_code == 200
    assert client.get("/api/admin/promos/engagement?days=0", headers=headers).status_code == 200
    assert state["calls"] == [{"days": 30, "format": "all"}, {"days": 0, "format": "all"}]
    paths = [route.path for route in main.app.routes if hasattr(route, "path")]
    assert paths.index("/api/admin/promos/engagement") < paths.index("/api/admin/promos/{account}/{shortcode}")
