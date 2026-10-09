from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app.public_media_kit import project_public_media_kit


def stats(total=100, average=50, count=2):
    return {"total": total, "average": average, "count": count,
            "median": 50, "min": 1, "max": 90, "coverage_pct": 100,
            "internal_source": "CANARY_STATS_SECRET"}


def post(code="publicA", published="2026-10-08T18:00:00Z", **overrides):
    return {
        "shortcode": code, "published_at": published,
        "permalink": f"https://www.instagram.com/reel/{code}/",
        "public_caption": "The actual published caption.",
        "caption": "CANARY_INTERNAL_TITLE_FALLBACK",
        "hook_text": "CANARY_PRIVATE_HOOK", "title": "CANARY_PRIVATE_TITLE",
        "format": "Reel", "metrics": {"likes": 100, "comments": 10,
            "video_views": 500, "video_plays": 650, "likes_at_1h": 55,
            "analysis.private_score": 123, "carousel_slide_video_views": 900},
        "engagements": 999_999, "engagement_rate_pct": 99_999,
        "id": "CANARY_INTERNAL_ID", "cover_path": "r2://CANARY_PRIVATE_BUCKET/key",
        "local_media_path": "/CANARY_PRIVATE_USER/secret.jpg",
        "hidden": False, "is_deleted": False, "thumbnail_bytes": b"public-thumbnail",
        **overrides,
    }


def report():
    period = {
        "post_count": 2,
        "metrics": {"likes": stats(), "comments": stats(20, 10),
                    "video_views": stats(1000, 500), "video_plays": stats(1300, 650),
                    "likes_at_1h": stats(888), "analysis.private_score": stats(999),
                    "carousel_slide_video_views": stats(900)},
        "engagements": stats(999_999, 99_999),
        "engagement_rate_pct": 99_999, "posts_per_week": 500,
        "hot_posts": 6, "private_metadata": {"api_key": "CANARY_PERIOD_SECRET"},
    }
    return {
        "generated_at": "2026-10-09T18:00:00Z", "timezone": "America/Costa_Rica",
        "account": {"handle": "sample", "public_name": "Public Studio", "public_bio": "Published public biography.",
                    "name": "CANARY_ORG_ALIAS", "label": "CANARY_INTERNAL_LABEL",
                    "followers": 1000, "following": 91, "profile_posts": 10, "verified": True,
                    "platform": "CANARY_CUSTOM_PLATFORM", "profile_url": "http://internal/CANARY_TOKEN",
                    "avatar_bytes": b"public-avatar", "avatar_path": "r2://CANARY_BUCKET/avatar",
                    "email": "CANARY_PRIVATE_EMAIL@example.test", "phone": "CANARY_PRIVATE_PHONE",
                    "bio": "CANARY_REGISTRY_BIO", "biography": "CANARY_REGISTRY_BIOGRAPHY",
                    "group": "CANARY_ACCOUNT_OWNERSHIP", "subcategory": "CANARY_ROUTING",
                    "demographics": {"api_key": "CANARY_AUDIENCE_SECRET"},
                    "private": False, "created_at": "CANARY_INTERNAL_CREATED_DATE"},
        "summary": {key: copy.deepcopy(period) for key in ("all_time", "last_30_days", "previous_30_days", "last_90_days")},
        "public_summary": {key: copy.deepcopy(period) for key in ("all_time", "last_30_days")},
        "public_best_posts": {"all_time": [post()], "last_30_days": [post()],
                       "by_metric": {"secret": "CANARY_RANKING_METADATA"}},
        "best_posts": {"all_time": [post("CANARY_DRAFT", public_caption="CANARY_DRAFT_CAPTION")]},
        "follower_history": [{"date": "2026-10-09", "followers": 1000, "private": True,
                              "full_name": "Public snapshot name", "token": "CANARY_SNAPSHOT_SECRET"}],
        "follower_growth": {"30d": {"delta": 50, "pct": 5.5, "observed_days": 30,
                                     "requested_days": 30, "from": "CANARY_SOURCE_DATE"},
                            "7d": {"pct": "CANARY_GROWTH_SECRET"}},
        "coverage": {"notes": ["CANARY_SOURCE_INVENTORY"]},
        "metrics_appendix": [{"label": "CANARY_RAW_METRIC"}],
        "raw_json": {"credentials": "CANARY_PROVIDER_SECRET"},
        "content": {"hashtags": ["CANARY_CREATIVE_METADATA"]},
        "breakdowns": {"hours": [{"label": "CANARY_PUBLISHING_LEDGER"}]},
    }


def test_projection_allowlists_every_level_and_never_mutates_input():
    source = report()
    original = copy.deepcopy(source)
    public = project_public_media_kit(source)
    assert source == original
    assert set(public) == {"generated_at", "timezone", "account", "summary", "follower_growth", "best_posts"}
    assert set(public["account"]) == {"handle", "public_name", "public_bio", "platform", "profile_url",
                                      "followers", "profile_posts", "verified", "avatar_bytes"}
    assert set(public["summary"]) == {"all_time", "last_30_days"}
    for period in public["summary"].values():
        assert set(period) == {"post_count", "metrics", "engagements", "engagement_rate_pct"}
        assert set(period["metrics"]) == {"likes", "comments", "video_views", "video_plays"}
        assert all(set(metric) == {"total", "average"} for metric in period["metrics"].values())
    text = json.dumps(public, default=lambda value: "image bytes")
    assert "CANARY" not in text
    assert public["account"]["platform"] == "Instagram"
    assert public["account"]["profile_url"] == "https://www.instagram.com/sample/"
    assert public["follower_growth"] == {"30d": {"pct": 5.5}}
    assert public["best_posts"]["all_time"][0]["thumbnail_bytes"] == b"public-thumbnail"


def test_public_numbers_zero_and_engagement_denominators_are_correct():
    source = report()
    period = source["public_summary"]["all_time"]
    period["post_count"] = 3
    period["metrics"]["likes"] = stats(100, 50, 2)
    period["metrics"]["comments"] = stats(15, 7.5, 2)
    period["metrics"]["video_views"] = stats(0, 0, 2)
    period["engagements"]["count"] = 3
    public = project_public_media_kit(source)
    selected = public["summary"]["all_time"]
    assert selected["engagements"] == {"total": 115, "average": 115 / 3}
    assert selected["engagement_rate_pct"] == pytest.approx(115 / 3 / 1000 * 100)
    assert selected["metrics"]["video_views"] == {"total": 0, "average": 0}
    assert selected["metrics"]["video_plays"] == {"total": 1300, "average": 650}
    showcase = public["best_posts"]["all_time"][0]
    assert showcase["engagements"] == 110
    assert showcase["engagement_rate_pct"] == 11
    assert showcase["metrics"]["video_views"] == 500
    assert showcase["metrics"]["video_plays"] == 650


@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf"), float("-inf"), -1, "123", {"secret": "CANARY"}])
def test_invalid_public_counts_are_unavailable(invalid):
    source = report()
    source["account"]["followers"] = invalid
    source["account"]["profile_posts"] = invalid
    source["public_summary"]["all_time"]["metrics"]["likes"] = stats(invalid, invalid)
    source["public_best_posts"]["all_time"][0]["metrics"]["likes"] = invalid
    public = project_public_media_kit(source)
    assert public["account"]["followers"] is None
    assert public["account"]["profile_posts"] is None
    assert public["summary"]["all_time"]["metrics"]["likes"] == {"total": None, "average": None}
    assert public["summary"]["all_time"]["engagement_rate_pct"] is None
    assert public["best_posts"]["all_time"][0]["metrics"]["likes"] is None


def test_legacy_ambiguous_name_bio_and_caption_never_fall_back_to_private_text():
    source = report()
    source["account"].pop("public_name")
    source["account"].pop("public_bio")
    source["public_best_posts"]["all_time"][0].pop("public_caption")
    source["follower_history"][0]["full_name"] = "CANARY_LEGACY_PROFILE_NAME"
    public = project_public_media_kit(source)
    assert public["account"]["public_name"] == "@sample"
    assert "CANARY_LEGACY_PROFILE_NAME" not in json.dumps(public, default=str)
    assert "public_bio" not in public["account"]
    assert public["best_posts"]["all_time"][0]["public_caption"] is None
    source["follower_history"] = []
    assert project_public_media_kit(source)["account"]["public_name"] == "@sample"
    source["account"]["public_name"] = {"secret": "CANARY"}
    assert project_public_media_kit(source)["account"]["public_name"] == "@sample"


def test_public_caption_is_not_a_keyword_blacklist_and_structured_text_is_rejected():
    source = report()
    source["public_best_posts"]["all_time"][0]["public_caption"] = "A public post about secret keys and internal IDs."
    assert project_public_media_kit(source)["best_posts"]["all_time"][0]["public_caption"] == "A public post about secret keys and internal IDs."
    source["public_best_posts"]["all_time"][0]["public_caption"] = {"private": "CANARY"}
    source["public_best_posts"]["all_time"][0]["format"] = {"private": "CANARY"}
    public_post = project_public_media_kit(source)["best_posts"]["all_time"][0]
    assert public_post["public_caption"] is None
    assert public_post["format"] is None


def test_showcase_caps_filters_duplicates_hidden_deleted_future_and_recent_dates():
    source = report()
    entries = [post("hidden", hidden="true"), post("deleted", is_deleted=1),
               post("future", published="2026-10-10T00:00:00Z"),
               post("undated", published=None), post("invalid_date", published="bad"),
               post("old", published="2026-08-01T00:00:00Z"),
               post("one"), post("one"), post("two"), post("three"), post("four")]
    source["public_best_posts"] = {"all_time": entries, "last_30_days": entries}
    public = project_public_media_kit(source)
    assert [post["shortcode"] for post in public["best_posts"]["all_time"]] == ["old", "one", "two"]
    assert [post["shortcode"] for post in public["best_posts"]["last_30_days"]] == ["one", "two", "three"]
    source["account"]["private"] = "true"
    private = project_public_media_kit(source)
    assert private["best_posts"] == {"all_time": [], "last_30_days": []}
    assert all(period["post_count"] == 0 and period["engagements"]["total"] is None for period in private["summary"].values())


@pytest.mark.parametrize("link", ["https://internal/p/secret/", "https://instagram.com.evil.test/p/secret/",
                                 "https://user:secret@instagram.com/p/secret/", "https://instagram.com/p/public/?token=secret",
                                 "https://instagram.com/p/public/#secret", "http://instagram.com/p/public/",
                                 "/api/dashboard/covers/sample/1", "https://instagram.com:443/p/public/"])
def test_arbitrary_or_private_links_never_become_pdf_annotations(link):
    source = report()
    source["public_best_posts"]["all_time"] = [post(None, permalink=link)]
    assert project_public_media_kit(source)["best_posts"]["all_time"] == []


def test_public_links_are_canonicalized_and_source_parameters_dropped():
    source = report()
    source["public_best_posts"]["all_time"] = [post("publicA", permalink="https://internal/?CANARY=token"),
                                        post(None, permalink="https://instagram.com/reel/publicB/")]
    public = project_public_media_kit(source)
    assert [post["permalink"] for post in public["best_posts"]["all_time"]] == [
        "https://www.instagram.com/p/publicA/", "https://www.instagram.com/p/publicB/"]
    source["account"]["handle"] = "sample?CANARY=secret"
    assert project_public_media_kit(source)["account"]["profile_url"] is None


def test_approximate_growth_missing_values_and_empty_report_are_safe():
    source = report()
    source["follower_growth"]["30d"]["observed_days"] = 35
    assert "follower_growth" not in project_public_media_kit(source)
    source["follower_growth"]["30d"] = {"pct": -5, "observed_days": 30}
    assert project_public_media_kit(source)["follower_growth"] == {"30d": {"pct": -5}}
    source["follower_growth"]["30d"]["pct"] = float("nan")
    assert "follower_growth" not in project_public_media_kit(source)
    public = project_public_media_kit({})
    assert public["summary"]["all_time"]["engagements"] == {"total": None, "average": None}
    assert public["best_posts"] == {"all_time": [], "last_30_days": []}
    assert public["account"]["verified"] is None


def test_internal_totals_are_never_used_without_explicit_public_cohort():
    source = report()
    source.pop("public_summary")
    source.pop("public_best_posts")
    public = project_public_media_kit(source)
    assert public["summary"]["all_time"]["post_count"] is None
    assert public["best_posts"] == {"all_time": [], "last_30_days": []}
    assert public["summary"]["all_time"]["metrics"]["likes"] == {"total": None, "average": None}
    assert public["summary"]["all_time"]["engagements"] == {"total": None, "average": None}


def test_computed_public_metrics_remain_finite_after_arithmetic_overflow():
    source = report()
    source["account"]["followers"] = 1
    period = source["public_summary"]["all_time"]
    period["metrics"]["likes"] = stats(1e308, 1e308)
    period["metrics"]["comments"] = stats(1e308, 1e308)
    source["public_best_posts"]["all_time"][0]["metrics"].update(likes=1e308, comments=1e308)
    public = project_public_media_kit(source)
    assert public["summary"]["all_time"]["engagements"] == {"total": None, "average": None}
    assert public["summary"]["all_time"]["engagement_rate_pct"] is None
    assert public["best_posts"]["all_time"][0]["engagements"] is None
    period["metrics"]["comments"] = stats(0, 0)
    public = project_public_media_kit(source)
    assert public["summary"]["all_time"]["engagements"]["total"] == 1e308
    assert public["summary"]["all_time"]["engagement_rate_pct"] is None


def test_actual_builder_public_provenance_excludes_registry_and_title_fallback(monkeypatch, tmp_path):
    from app import account_media_kit
    path = tmp_path / "public-kit.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (handle TEXT, label TEXT, biography TEXT);
        INSERT INTO accounts VALUES ('sample', 'CANARY_ORG_ALIAS', 'CANARY_REGISTRY_BIO');
        CREATE TABLE account_snapshots (handle TEXT, followers_count INTEGER, full_name TEXT,
            biography TEXT, captured_at TEXT);
        INSERT INTO account_snapshots VALUES ('sample', 1000, 'Actual public profile',
            'Actual public biography', '2026-10-09T12:00:00Z');
        CREATE TABLE dashboard_posts (id INTEGER, account TEXT, shortcode TEXT, caption TEXT,
            title TEXT, likes INTEGER, comments INTEGER, published_at TEXT,
            hidden INTEGER, is_deleted INTEGER);
        INSERT INTO dashboard_posts VALUES (1, 'sample', 'publicA', 'Actual public caption',
            'CANARY_INTERNAL_TITLE', 100, 10, '2026-10-08T12:00:00Z', 0, 0);
        INSERT INTO dashboard_posts VALUES (2, 'sample', 'publicB', NULL,
            'CANARY_INTERNAL_TITLE_FALLBACK', 50, 5, '2026-10-07T12:00:00Z', 0, 0);
        INSERT INTO dashboard_posts VALUES (3, 'sample', 'hidden', 'CANARY_HIDDEN',
            NULL, 9000, 900, '2026-10-07T12:00:00Z', 1, 0);
        INSERT INTO dashboard_posts VALUES (4, 'sample', 'deleted', 'CANARY_DELETED',
            NULL, 9000, 900, '2026-10-07T12:00:00Z', 0, 1);
        INSERT INTO dashboard_posts VALUES (5, 'sample', 'undated', 'CANARY_UNDATED',
            NULL, 9000, 900, NULL, 0, 0);
    """)
    conn.commit()
    conn.close()

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(account_media_kit.db, "connect", connect)
    full = account_media_kit.build_account_media_kit("sample", now=datetime(2026, 10, 9, 18, tzinfo=UTC))
    public = project_public_media_kit(full)
    assert public["account"]["public_name"] == "Actual public profile"
    assert public["account"].get("public_bio") == "Actual public biography"
    assert [post["public_caption"] for post in public["best_posts"]["all_time"]] == ["Actual public caption", None]
    assert full["summary"]["all_time"]["post_count"] == 5
    assert public["summary"]["all_time"]["post_count"] == 2
    assert public["summary"]["all_time"]["metrics"]["likes"]["total"] == 150
    assert public["summary"]["last_30_days"]["metrics"]["comments"]["total"] == 15
    assert "CANARY" not in json.dumps(public)
