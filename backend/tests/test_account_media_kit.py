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


def seed_consistent_public_window(connect, mode="posts"):
    with connect() as conn:
        conn.execute("UPDATE accounts SET scrape_mode = ?", (mode,))
        insert(conn, "dashboard_posts", account="sample", shortcode="history", likes=10,
               comments=1, published_at="2026-09-08T12:00:00Z")
        insert(conn, "dashboard_posts", account="sample", shortcode="recentA", likes=20,
               comments=2, product_type="clips", published_at="2026-10-08T11:00:00Z")
        insert(conn, "dashboard_posts", account="sample", shortcode="recentB", likes=30,
               comments=3, post_type_label="Video", published_at="2026-10-09T11:00:00Z")
        for captured, count in (("2026-09-09T17:00:00Z", 50),
                                ("2026-10-08T12:00:00Z", 51),
                                ("2026-10-09T12:00:00Z", 52)):
            insert(conn, "account_snapshots", handle="sample", followers_count=1000,
                   posts_count=count, private=0, captured_at=captured)


@pytest.mark.parametrize("mode", ["posts", "both"])
def test_recent_public_availability_requires_full_source_and_consistent_window(report_db, mode):
    connect, _ = report_db
    seed_consistent_public_window(connect, mode)
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["public_recent_available"] is True
    assert report["public_recent_availability_reasons"] == []
    assert report["public_summary"]["last_30_days"]["post_count"] == 2
    assert report["public_summary"]["last_30_days"]["eligible_video_count"] == 2
    assert report["public_summary"]["all_time"]["eligible_video_count"] == 2
    assert report["public_summary"]["all_time"]["post_count"] == 3
    assert "scrape_mode" not in report["account"]


@pytest.mark.parametrize("mode", ["reels", None, "unknown"])
def test_narrow_or_unconfirmed_sources_never_claim_all_recent_account_posts(report_db, mode):
    connect, _ = report_db
    seed_consistent_public_window(connect, mode)
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["public_recent_available"] is False
    assert "all_post_source_unconfirmed" in report["public_recent_availability_reasons"]
    # Keep the full internal data, rather than replacing the observed
    # two-post sample with an inferred publication count or zero metrics.
    assert report["public_summary"]["last_30_days"]["metrics"]["likes"]["total"] == 50


def test_ivan_style_stalled_library_with_later_profile_increases_is_unavailable(report_db):
    connect, _ = report_db
    seed_consistent_public_window(connect)
    with connect() as conn:
        conn.execute("UPDATE dashboard_posts SET published_at = '2026-09-10T12:00:00Z' WHERE shortcode LIKE 'recent%'")
        conn.execute("UPDATE account_snapshots SET posts_count = 94 WHERE captured_at = '2026-09-09T17:00:00Z'")
        conn.execute("UPDATE account_snapshots SET posts_count = 95, captured_at = '2026-09-10T15:00:00Z' WHERE captured_at = '2026-10-08T12:00:00Z'")
        conn.execute("UPDATE account_snapshots SET posts_count = 107 WHERE captured_at = '2026-10-09T12:00:00Z'")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["public_recent_available"] is False
    assert "profile_increase_exceeds_stored_public_posts" in report["public_recent_availability_reasons"]
    assert report["public_summary"]["last_30_days"]["post_count"] == 2
    assert report["public_summary"]["last_30_days"]["metrics"]["likes"]["total"] == 50


def test_deletions_cannot_mask_a_missing_profile_increase_inside_the_window(report_db):
    connect, _ = report_db
    seed_consistent_public_window(connect)
    with connect() as conn:
        # First-to-last net growth is exactly two, matching the two known
        # posts. The earlier +5 interval still proves a missing sample.
        conn.execute("UPDATE account_snapshots SET posts_count = 55 WHERE captured_at = '2026-10-08T12:00:00Z'")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["public_recent_available"] is False
    assert "profile_increase_exceeds_stored_public_posts" in report["public_recent_availability_reasons"]


def test_duplicate_hidden_deleted_future_and_undated_rows_cannot_cover_missing_posts(report_db):
    connect, _ = report_db
    seed_consistent_public_window(connect)
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1")
        conn.execute("UPDATE account_snapshots SET posts_count = 53 WHERE captured_at = '2026-10-09T12:00:00Z'")
        insert(conn, "posts", section="historical", shortcode="recentB", likes=30,
               comments=3, published_at="2026-10-09T11:00:00Z")
        for code, hidden, deleted, published in (("hidden", 1, 0, "2026-10-09T11:00:00Z"),
                                                 ("deleted", 0, 1, "2026-10-09T11:00:00Z"),
                                                 ("future", 0, 0, "2026-10-10T11:00:00Z"),
                                                 ("undated", 0, 0, None)):
            insert(conn, "dashboard_posts", account="sample", shortcode=code, likes=500,
                   comments=5, hidden=hidden, is_deleted=deleted, published_at=published)
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["public_recent_available"] is False
    assert "profile_increase_exceeds_stored_public_posts" in report["public_recent_availability_reasons"]
    assert report["public_summary"]["last_30_days"]["post_count"] == 2
    assert report["summary"]["last_30_days"]["post_count"] == 4
    assert report["coverage"]["future_posts_excluded"] == 1


@pytest.mark.parametrize("case,reason", [
    ("missing_public_history", "insufficient_public_window_history"),
    ("missing_baseline", "missing_profile_window_baseline"),
    ("old_baseline", "missing_profile_window_baseline"),
    ("stale_current", "missing_or_stale_current_profile"),
    ("no_recent_posts", "no_confirmed_recent_posts"),
    ("private", "private_account"),
])
def test_unconfirmed_recent_windows_are_unavailable_instead_of_zero_claims(report_db, case, reason):
    connect, _ = report_db
    seed_consistent_public_window(connect)
    with connect() as conn:
        if case == "missing_public_history":
            conn.execute("DELETE FROM dashboard_posts WHERE shortcode = 'history'")
        elif case == "missing_baseline":
            conn.execute("DELETE FROM account_snapshots WHERE captured_at = '2026-09-09T17:00:00Z'")
        elif case == "old_baseline":
            conn.execute("UPDATE account_snapshots SET captured_at = '2026-09-01T17:00:00Z' WHERE captured_at = '2026-09-09T17:00:00Z'")
        elif case == "stale_current":
            conn.execute("DELETE FROM account_snapshots WHERE captured_at = '2026-10-08T12:00:00Z'")
            conn.execute("UPDATE account_snapshots SET captured_at = '2026-10-05T12:00:00Z' WHERE captured_at = '2026-10-09T12:00:00Z'")
        elif case == "no_recent_posts":
            conn.execute("DELETE FROM dashboard_posts WHERE shortcode LIKE 'recent%'")
            conn.execute("UPDATE account_snapshots SET posts_count = 50")
        elif case == "private":
            conn.execute("UPDATE account_snapshots SET private = 1")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["public_recent_available"] is False
    assert reason in report["public_recent_availability_reasons"]


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


def test_public_profile_and_caption_never_use_internal_registry_or_title_fallbacks(report_db):
    connect, _ = report_db
    with connect() as conn:
        conn.execute("UPDATE accounts SET is_canonical = 1, label = 'PRIVATE_LABEL', biography = 'PRIVATE_BIO'")
        insert(conn, "posts", id=1, section="historical", shortcode="publicA", title="PRIVATE_TITLE",
               caption=None, likes=10, comments=1, published_at="2026-10-08T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["account"]["name"] == "PRIVATE_LABEL"
    assert report["account"]["public_name"] == "sample"
    assert report["account"]["public_bio"] is None
    assert report["best_posts"]["all_time"][0]["caption"] == "PRIVATE_TITLE"
    assert report["best_posts"]["all_time"][0]["public_caption"] == ""
    with connect() as conn:
        conn.execute("ALTER TABLE account_snapshots ADD COLUMN biography TEXT")
        insert(conn, "account_snapshots", handle="sample", full_name="Public Brand", biography="Public profile bio",
               followers_count=1000, captured_at="2026-10-09T12:00:00Z")
        conn.execute("UPDATE posts SET caption = 'Public caption'")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["account"]["public_name"] == "Public Brand"
    assert report["account"]["public_bio"] == "Public profile bio"
    assert report["best_posts"]["all_time"][0]["public_caption"] == "Public caption"


def test_public_performance_excludes_nonpublic_rows_and_private_account_posts(report_db):
    connect, _ = report_db
    with connect() as conn:
        for code, extra in (("visible", {}), ("hidden", {"hidden": 1}), ("deleted", {"is_deleted": 1}),
                            (None, {}), ("future", {"published_at": "2026-10-10T12:00:00Z"})):
            insert(conn, "dashboard_posts", account="sample", shortcode=code, likes=100, comments=10,
                   **{"published_at": "2026-10-08T12:00:00Z", **extra})
        insert(conn, "account_snapshots", handle="sample", followers_count=1000,
               private=0, captured_at="2026-10-09T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["summary"]["all_time"]["metrics"]["likes"]["total"] == 400
    assert report["public_summary"]["all_time"]["post_count"] == 1
    assert report["public_summary"]["last_30_days"]["metrics"]["likes"]["total"] == 100
    assert [post["shortcode"] for post in report["public_best_posts"]["all_time"]] == ["visible"]
    assert set(report["public_summary"]["all_time"]["metrics"]) == {"likes", "comments", "video_views", "video_plays"}
    with connect() as conn:
        conn.execute("UPDATE account_snapshots SET private = 1")
    private = kit.build_account_media_kit("sample", now=NOW)
    assert private["public_summary"]["all_time"]["post_count"] == 0
    assert private["public_summary"]["all_time"]["metrics"]["likes"]["total"] is None
    assert private["public_best_posts"] == {"all_time": [], "last_30_days": []}


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


def test_provider_discovery_ignores_urls_ids_and_substrings(report_db):
    connect, _ = report_db
    with connect() as conn:
        insert(conn, "dashboard_posts", account="sample", shortcode="a", likes=10,
               raw_json=json.dumps({"displayUrl": "https://cdn.test/display.jpg", "displayCount": 12,
                   "ownerId": 998, "igPlayCount": 50, "videoViewCount": 60,
                   "commentId": 179000000001, "commentIds": 179000000002,
                   "videoViewsUrl": 123456, "videoViewsUrls": 123456,
                   "likesUpdatedAt": 1790000000, "commentsDate": 1790000000,
                   "playsTimestamp": 1790000000,
                   "replaySourceUrl": "https://cdn.test/video.mp4",
                   "insights": {"watchTime": 100, "replayCount": 3}}),
               published_at="2026-10-08T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    keys = {entry["key"] for entry in report["metric_catalog"]}
    assert "provider.display_url" not in keys
    assert "provider.display_count" not in keys
    assert "provider.owner_id" not in keys
    assert "provider.replay_source_url" not in keys
    for key in ("comment_id", "comment_ids", "video_views_url", "video_views_urls",
                "likes_updated_at", "comments_date", "plays_timestamp"):
        assert "provider." + key not in keys
    assert report["summary"]["all_time"]["metrics"]["video_plays"]["total"] == 50
    assert report["summary"]["all_time"]["metrics"]["provider.insights.replay_count"]["total"] == 3


def test_carousel_slide_performance_is_separate_and_reports_known_subsets(report_db):
    connect, _ = report_db
    video_views = [658, 221, 143, 131, 80, 59, 65]
    durations = [13.675102, 10.215329, 5.014059, 12.815964, 22.707664, 5.014059, 9.913469]
    children = [{"type": "Image", "likesCount": None, "commentsCount": 0},
                {"type": "Image", "likesCount": None, "commentsCount": 0}]
    children += [{"type": "Video", "commentsCount": 0, "likesCount": None,
                  "videoViewCount": views, "videoDuration": duration}
                 for views, duration in zip(video_views, durations)]
    children[2]["videoPlayCount"] = 0
    children[3]["videoPlayCount"] = 300
    children[4]["likesCount"] = 5
    children[4]["insights"] = {"retentionPct": 60, "replayCount": 2}
    children[5]["insights"] = {"retentionPct": 80, "replayCount": 3}
    with connect() as conn:
        insert(conn, "dashboard_posts", account="sample", shortcode="carousel", likes=350, comments=19,
               post_type_label="Carousel", product_type="carousel_container",
               raw_json=json.dumps({"type": "Sidecar", "likesCount": 38, "commentsCount": 5,
                   "videoViewCount": None, "childPosts": children}),
               published_at="2026-10-08T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    metrics = report["summary"]["all_time"]["metrics"]
    assert metrics["likes"]["total"] == 350
    assert metrics["comments"]["total"] == 19
    assert metrics["video_views"]["total"] is None
    assert metrics["video_plays"]["total"] is None
    assert metrics["video_duration"]["total"] is None
    assert metrics["slide_count"]["total"] == 9
    assert metrics["carousel_video_slides"]["total"] == 7
    assert metrics["carousel_slide_video_views"]["total"] == 1357
    assert metrics["carousel_slide_video_views_measured_slides"]["total"] == 7
    assert metrics["carousel_slide_video_duration"]["total"] == pytest.approx(sum(durations))
    assert metrics["carousel_slide_video_duration_measured_slides"]["total"] == 7
    assert metrics["carousel_slide_video_plays"]["total"] == 300
    assert metrics["carousel_slide_video_plays_measured_slides"]["total"] == 2
    assert metrics["carousel_slide_likes"]["total"] == 5
    assert metrics["carousel_slide_likes_measured_slides"]["total"] == 1
    assert metrics["carousel_slide_comments"]["total"] == 0
    assert metrics["carousel_slide_comments_measured_slides"]["total"] == 9
    assert metrics["carousel_slide_provider.insights.retention_pct"]["average"] == 70
    assert metrics["carousel_slide_provider.insights.retention_pct"]["total"] is None
    assert metrics["carousel_slide_provider.insights.retention_pct_measured_slides"]["total"] == 2
    assert metrics["carousel_slide_provider.insights.replay_count"]["total"] == 5
    assert report["summary"]["all_time"]["engagements"]["total"] == 369
    assert not report["best_posts"]["by_metric"]["video_views"]["all_time"]
    definition = next(entry for entry in report["metric_catalog"] if entry["key"] == "carousel_slide_video_views")
    assert definition["source"] == "stored carousel slide measurements"
    assert "separate from parent" in definition["label"]
    assert any("Carousel slide measurements remain separate" in note for note in report["coverage"]["notes"])


def test_reel_preview_count_never_implies_carousel_slides_or_child_performance(report_db):
    connect, _ = report_db
    with connect() as conn:
        insert(conn, "dashboard_posts", account="sample", shortcode="reel", likes=50,
               post_type_label="Video", product_type="clips", slide_count=2,
               raw_json=json.dumps({"type": "Video", "productType": "clips",
                   "childPosts": [], "images": ["https://cdn.test/preview1.jpg", "https://cdn.test/preview2.jpg"]}),
               published_at="2026-10-08T12:00:00Z")
    report = kit.build_account_media_kit("sample", now=NOW)
    assert report["best_posts"]["all_time"][0]["format"] == "Reel"
    assert report["summary"]["all_time"]["metrics"]["slide_count"]["total"] == 2
    assert report["summary"]["all_time"]["metrics"]["carousel_video_slides"]["count"] == 0
    assert report["summary"]["all_time"]["metrics"]["carousel_slide_video_views"]["count"] == 0
    definition = next(entry for entry in report["metric_catalog"] if entry["key"] == "slide_count")
    assert definition["label"] == "Stored media items (slides / previews)"
    assert any("For Reels, images may be preview assets" in note for note in report["coverage"]["notes"])


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
