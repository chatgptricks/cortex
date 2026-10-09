from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import account_media_kit as kit

NOW = datetime(2026, 10, 9, 18, tzinfo=UTC)


@pytest.fixture
def report_db(monkeypatch, tmp_path):
    path = tmp_path / "media-kit.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, handle TEXT, label TEXT,
            group_name TEXT, category TEXT, subcategory TEXT, is_canonical INTEGER,
            is_active INTEGER, avatar_path TEXT, biography TEXT, business_email TEXT,
            hot_threshold INTEGER, scrape_mode TEXT, created_at TEXT);
        CREATE TABLE posts (id INTEGER PRIMARY KEY, section TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, caption TEXT, title TEXT,
            image_path TEXT, likes_at_1h INTEGER, likes_at_8h INTEGER, comments_at_8h INTEGER,
            updated_at TEXT, is_hot INTEGER, is_promo INTEGER, hidden INTEGER, is_deleted INTEGER,
            brain_global_mean_abs REAL, virality_potential REAL, analysis_summary TEXT);
        CREATE TABLE dashboard_posts (id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT,
            published_at TEXT, likes INTEGER, comments INTEGER, video_views INTEGER,
            video_plays INTEGER, video_duration REAL, slide_count INTEGER, raw_json TEXT,
            caption TEXT, cover_image_path TEXT, updated_at TEXT, likes_at_1h INTEGER,
            likes_at_8h INTEGER, comments_at_8h INTEGER, likes_at_24h INTEGER, likes_at_48h INTEGER,
            post_type_label TEXT, product_type TEXT, hashtags TEXT, coauthors TEXT,
            mentions TEXT, tagged_users TEXT, music_song TEXT, music_artist TEXT,
            uses_original_audio INTEGER, paid_partnership INTEGER, hidden INTEGER,
            is_deleted INTEGER, is_promo INTEGER, is_hot INTEGER);
        CREATE TABLE account_snapshots (id INTEGER PRIMARY KEY, handle TEXT,
            followers_count INTEGER, following_count INTEGER, posts_count INTEGER,
            full_name TEXT, verified INTEGER, private INTEGER, captured_at TEXT);
        CREATE TABLE engagement_observations (shortcode TEXT PRIMARY KEY,
            observed_at TEXT, raw_json TEXT);
    """)
    connection.execute("""INSERT INTO accounts (handle, label, group_name, category,
        is_canonical, is_active, biography, business_email, hot_threshold, scrape_mode)
        VALUES ('sample', 'Sample Studio', 'sentient', 'sentient', 0, 1,
            'Useful public biography', 'sales@sample.test', 600, 'both')""")
    connection.commit()
    connection.close()
    read_statements = []

    @contextmanager
    def connect():
        value = sqlite3.connect(path)
        value.row_factory = sqlite3.Row
        value.set_trace_callback(read_statements.append)
        try:
            yield value
            value.commit()
        finally:
            value.close()

    monkeypatch.setattr(kit.db, "connect", connect)
    return connect, read_statements


def insert(conn, table, **values):
    names = list(values)
    conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(values.values()))


def test_known_samples_zero_and_hidden_likes_are_distinct(report_db):
    connect, _ = report_db
    with connect() as conn:
        for code, likes, comments in (("a", 100, 10), ("b", 0, None), ("c", -1, 5), ("d", None, None)):
            insert(conn, "dashboard_posts", account="sample", shortcode=code,
                   published_at="2026-10-08T12:00:00Z", likes=likes, comments=comments)
        insert(conn, "account_snapshots", handle="sample", followers_count=1000,
               posts_count=30, captured_at="2026-10-09T12:00:00Z")
    report = kit.build_account_media_kit("@SAMPLE", now=NOW)
    period = report["summary"]["all_time"]
    assert period["post_count"] == 4
    assert period["metrics"]["likes"] == {"total": 100, "average": 50, "median": 50,
        "min": 0, "max": 100, "count": 2, "coverage_pct": 50}
    assert period["metrics"]["comments"]["average"] == 7.5
    assert period["engagements"]["total"] == 115
    assert period["engagements"]["count"] == 3
    assert period["complete_engagement_posts"] == 1
    assert period["engagement_rate_pct"] == pytest.approx(115 / 3 / 1000 * 100)
    assert period["metrics"]["video_views"]["total"] is None
    assert [post["shortcode"] for post in report["best_posts"]["all_time"]] == ["a", "c", "b"]


def test_on_click_reads_changes_and_has_no_scrape_or_db_writes(report_db, monkeypatch):
    from app import apify_sync
    connect, statements = report_db
    monkeypatch.setattr(apify_sync, "_run_apify_actor_and_fetch", lambda *a, **k: pytest.fail("Paid scrape during report"))
    with connect() as conn:
        insert(conn, "dashboard_posts", account="sample", shortcode="a", likes=10,
               published_at="2026-10-08T12:00:00Z")
    statements.clear()
    assert kit.build_account_media_kit("sample", now=NOW)["summary"]["all_time"]["metrics"]["likes"]["total"] == 10
    assert all(statement.startswith(("SELECT", "PRAGMA")) for statement in statements)
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET likes = 25")
    assert kit.build_account_media_kit("sample", now=NOW)["summary"]["all_time"]["metrics"]["likes"]["total"] == 25


def test_canonical_deduplicates_merges_enrichment_and_keeps_canonical_cover(report_db):
    connect, _ = report_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", id=1, shortcode="same", section="historical", likes=120,
               comments=12, published_at="2026-08-01T12:00:00Z", updated_at="2026-10-01T12:00:00Z",
               image_path="r2://canonical/cover.jpg", likes_at_1h=15, virality_potential=0.81)
        insert(conn, "posts", id=2, shortcode="same", section="single", likes=70,
               updated_at="2026-10-08T12:00:00Z")
        insert(conn, "dashboard_posts", id=99, account="sample", shortcode="same", likes=150,
               comments=15, published_at="2026-08-01T12:00:00Z", updated_at="2026-10-08T12:00:00Z",
               video_views=6000, video_plays=8000, likes_at_8h=75, comments_at_8h=8)
        insert(conn, "posts", id=3, section="ab", likes=99999)
        insert(conn, "posts", id=4, section="single", likes=99999)
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["summary"]["all_time"]["post_count"] == 1
    metrics = report["summary"]["all_time"]["metrics"]
    assert metrics["likes"]["total"] == 150
    assert metrics["video_views"]["total"] == 6000
    assert metrics["video_plays"]["total"] == 8000
    assert metrics["likes_at_1h"]["total"] == 15
    assert metrics["likes_at_8h"]["total"] == 75
    assert metrics["virality_potential"]["average"] == 0.81
    top = report["best_posts"]["all_time"][0]
    assert top["id"] == 1
    assert top["cover_url"].endswith("/sample/1")
    assert top["local_media_path"] == "r2://canonical/cover.jpg"


def test_complete_observation_metrics_and_dynamic_provider_fields(report_db):
    connect, _ = report_db
    with connect() as conn:
        insert(conn, "dashboard_posts", account="sample", shortcode="a", likes=10, comments=1,
               published_at="2026-10-08T12:00:00Z", updated_at="2026-10-08T13:00:00Z",
               likes_at_24h=20, likes_at_48h=30, raw_json=json.dumps({"shortCode": "a",
                   "likesCount": 5, "videoPlayCount": 0, "igPlayCount": 9,
                   "metrics": {"watchTime": 88, "completionRate": 0.8},
                   "owner": {"followersCount": 999999}, "childPosts": [{}, {}]}))
        insert(conn, "engagement_observations", shortcode="a", observed_at="2026-10-09T12:00:00Z",
               raw_json=json.dumps({"shortCode": "a", "likesCount": 25, "commentsCount": 4,
                   "videoViewCount": 2500, "videoPlayCount": 0, "sharesCount": 3, "savesCount": 8,
                   "insights": {"impressions": 500, "retentionPct": 60}}))
    report = kit.build_account_media_kit("sample", now=NOW)
    metrics = report["summary"]["all_time"]["metrics"]
    assert metrics["likes"]["total"] == 25
    assert metrics["shares"]["total"] == 3
    assert metrics["saves"]["total"] == 8
    assert metrics["video_views"]["total"] == 2500
    assert metrics["video_plays"]["total"] == 0
    assert metrics["likes_at_24h"]["total"] == 20
    assert metrics["likes_at_48h"]["total"] == 30
    assert metrics["slide_count"]["total"] == 2
    assert metrics["provider.metrics.watch_time"]["total"] == 88
    assert metrics["provider.metrics.completion_rate"]["average"] == 0.8
    assert metrics["provider.insights.retention_pct"]["average"] == 60
    assert not any("followers" in key for key in metrics)
    assert report["best_posts"]["all_time"][0]["metrics_updated_at"] == "2026-10-09T12:00:00+00:00"


def test_newer_duplicate_metrics_win_even_when_publication_date_is_missing(report_db):
    connect, _ = report_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        insert(conn, "posts", id=1, section="historical", shortcode="same", likes=100,
               published_at="2026-09-01T12:00:00Z", updated_at="2026-10-01T12:00:00Z",
               image_path="r2://cover.jpg")
        insert(conn, "dashboard_posts", id=99, account="sample", shortcode="same", likes=20,
               published_at=None, updated_at="2026-10-09T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["summary"]["all_time"]["metrics"]["likes"]["total"] == 20
    top = report["best_posts"]["all_time"][0]
    assert top["published_at"] == "2026-09-01T12:00:00Z"
    assert top["cover_url"].endswith("/sample/1")


def test_older_raw_data_never_resurrects_hidden_current_like_count(report_db):
    connect, _ = report_db
    with connect() as conn:
        insert(conn, "dashboard_posts", account="sample", shortcode="a", likes=None, comments=1,
               published_at="2026-10-08T12:00:00Z", updated_at="2026-10-09T12:00:00Z",
               raw_json=json.dumps({"likesCount": 500, "videoViewCount": 1500}))
        insert(conn, "engagement_observations", shortcode="a", observed_at="2026-10-08T14:00:00Z",
               raw_json=json.dumps({"shortCode": "a", "likesCount": 600, "videoViewCount": 1700}))
    metrics = kit.build_account_media_kit("sample", now=NOW)["summary"]["all_time"]["metrics"]
    assert metrics["likes"]["count"] == 0
    assert metrics["likes"]["total"] is None
    assert metrics["video_views"]["total"] == 1700


def test_rolling_windows_exclude_future_invalid_and_unpublished_dates(report_db):
    connect, _ = report_db
    with connect() as conn:
        for code, published, likes in (("recent", "2026-10-08T12:00:00Z", 100),
                                       ("boundary", "2026-09-09T18:00:00Z", 50),
                                       ("older", "2026-09-09T17:59:59Z", 40),
                                       ("future", "2026-10-10T12:00:00Z", 999),
                                       ("invalid", "garbage", 10), ("undated", None, 5)):
            insert(conn, "dashboard_posts", account="sample", shortcode=code, likes=likes,
                   published_at=published)
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["summary"]["all_time"]["post_count"] == 5
    assert report["summary"]["last_30_days"]["post_count"] == 2
    assert report["summary"]["previous_30_days"]["post_count"] == 1
    assert report["summary"]["last_30_days"]["metrics"]["likes"]["total"] == 150
    assert report["coverage"]["future_posts_excluded"] == 1
    assert report["coverage"]["undated_posts"] == 2
    assert report["summary"]["all_time"]["cadence_post_count"] == 3
    assert [post["shortcode"] for post in report["best_posts"]["last_30_days"]] == ["recent", "boundary"]


def test_profile_history_uses_final_costa_rica_day_and_latest_usable_reading(report_db):
    connect, _ = report_db
    with connect() as conn:
        for timestamp, followers in (("2026-09-09T12:00:00Z", 100),
                                      ("2026-10-08T12:00:00Z", 120),
                                      ("2026-10-08T22:00:00Z", 130),
                                      ("2026-10-09T02:00:00Z", 140),
                                      ("2026-10-09T12:00:00Z", 150),
                                      ("2026-10-09T13:00:00Z", None)):
            insert(conn, "account_snapshots", handle="Sample", followers_count=followers,
                   following_count=20, posts_count=40, full_name="Sample Studio", verified=1,
                   private=0, captured_at=timestamp)
        insert(conn, "dashboard_posts", account="sample", shortcode="night", likes=10,
               published_at="2026-10-09T02:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["account"]["followers"] == 150
    assert report["account"]["profile_posts"] == 40
    assert report["account"]["bio"] == "Useful public biography"
    assert report["account"]["email"] == "sales@sample.test"
    assert report["follower_history"][1]["local_date"] == "2026-10-08"
    assert report["follower_history"][1]["followers"] == 140
    # A later incomplete snapshot cannot erase the day's last usable
    # follower reading. Midnight UTC still belongs to the previous CST day.
    assert report["follower_growth"]["1d"]["delta"] == 10
    assert report["follower_growth"]["1d"]["observed_days"] == 1
    assert report["follower_growth"]["30d"]["delta"] == 50
    assert report["follower_growth"]["30d"]["observed_days"] == 30
    assert report["breakdowns"]["weekdays"][0]["label"] == "Thursday"
    assert report["breakdowns"]["hours"][0]["label"] == "20:00"
    assert "hot_threshold" not in report["account"]
    assert "scrape_mode" not in report["account"]


def test_one_day_growth_never_labels_sparse_history_as_daily_growth(report_db):
    connect, _ = report_db
    with connect() as conn:
        for timestamp, followers in (("2026-09-29T12:00:00Z", 100), ("2026-10-09T12:00:00Z", 150)):
            insert(conn, "account_snapshots", handle="sample", followers_count=followers,
                   captured_at=timestamp)
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["follower_growth"]["1d"] is None
    assert report["follower_growth"]["7d"]["observed_days"] == 10
    assert report["follower_growth"]["7d"]["requested_days"] == 7


def test_metadata_keeps_whole_music_names_empty_arrays_and_unknown_booleans(report_db):
    connect, _ = report_db
    with connect() as conn:
        insert(conn, "account_snapshots", handle="sample", followers_count=100,
               verified=None, private=None, captured_at="2026-10-09T12:00:00Z")
        insert(conn, "dashboard_posts", account="sample", shortcode="music", likes=5,
               hashtags="[]", mentions="[]", coauthors="[]", tagged_users="[]",
               music_song="Instant Crush", music_artist="Daft Punk",
               uses_original_audio="false", paid_partnership="false",
               published_at="2026-10-08T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["content"]["music_song"] == [{"label": "Instant Crush", "post_count": 1}]
    assert report["content"]["music_artist"] == [{"label": "Daft Punk", "post_count": 1}]
    assert report["content"]["metadata_counts"]["hashtags"] == 0
    assert report["content"]["metadata_counts"]["mentions"] == 0
    assert report["content"]["metadata_counts"]["coauthors"] == 0
    assert report["content"]["metadata_counts"]["tagged_users"] == 0
    assert report["summary"]["all_time"]["paid_partnership_posts"] == 0
    assert report["content"]["metadata_counts"]["uses_original_audio"] == 0
    assert report["account"]["verified"] is None
    assert report["account"]["private"] is None


def test_empty_account_unknown_account_and_deleted_showcase(report_db):
    connect, _ = report_db
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["summary"]["all_time"]["post_count"] == 0
    assert report["account"]["followers"] is None
    assert report["best_posts"]["all_time"] == []
    assert report["follower_history"] == []
    with pytest.raises(Exception) as caught:
        kit.build_account_media_kit("missing", now=NOW)
    assert caught.value.status_code == 404
    with connect() as conn:
        for code, likes, deleted, hidden in (("deleted", 999, 1, 0), ("hidden", 888, 0, 1), ("visible", 10, 0, 0)):
            insert(conn, "dashboard_posts", account="sample", shortcode=code, likes=likes,
                   is_deleted=deleted, hidden=hidden, published_at="2026-10-08T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["summary"]["all_time"]["post_count"] == 3
    assert report["summary"]["all_time"]["deleted_posts"] == 1
    assert [post["shortcode"] for post in report["best_posts"]["all_time"]] == ["visible"]


def test_json_endpoint_matches_accounts_auth_and_never_caches(report_db, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "FIREBASE_APP", object())
    monkeypatch.setattr(main.firebase_auth, "verify_id_token", lambda token: {"email": "sales@example.com", "uid": "test"})
    access = {"is_admin": False, "operating_role": "sales", "operating_roles": '["sales"]'}
    monkeypatch.setattr(main, "get_dashboard_user_access", lambda email: access)
    monkeypatch.setattr(main, "log_usage_event", lambda *args: None)
    app = FastAPI()
    app.middleware("http")(main._require_firebase_user)
    app.add_api_route("/api/admin/accounts/{handle}/media-kit", main.admin_account_media_kit)
    client = TestClient(app)
    path = "/api/admin/accounts/sample/media-kit"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer test"}).status_code == 403
    access["is_admin"] = True
    response = client.get(path, headers={"Authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization"
    assert response.json()["account"]["handle"] == "sample"
