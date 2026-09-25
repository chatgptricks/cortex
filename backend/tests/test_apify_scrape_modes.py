from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from app import apify_sync, db, main


def test_collect_short_term_items_uses_the_selected_profile_surface(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_fetch(payload, timeout=180.0, actor_id=apify_sync.APIFY_ACTOR_ID):
        calls.append((actor_id, payload))
        if actor_id == apify_sync.APIFY_ACTOR_ID:
            return [
                {"shortCode": "post-only", "ownerUsername": "posts", "type": "Image"},
                {"shortCode": "feed-reel", "ownerUsername": "posts", "productType": "clips"},
                {"shortCode": "both-post", "ownerUsername": "both", "type": "Image"},
            ]
        return [
            {"shortCode": "reel-only", "owner": {"username": "reels"}, "type": "Video"},
            {"shortCode": "both-reel", "ownerUsername": "both", "type": "Video"},
        ]

    monkeypatch.setattr(apify_sync, "_fetch_apify_items", fake_fetch)
    configs = {
        "posts": {"handle": "posts", "scrape_mode": "posts"},
        "reels": {"handle": "reels", "scrape_mode": "reels"},
        "both": {"handle": "both", "scrape_mode": "both"},
    }

    items = apify_sync._collect_short_term_items(configs, 20, datetime.now(UTC))

    assert [item["shortCode"] for item in items["posts"]] == ["post-only", "feed-reel"]
    assert [item["shortCode"] for item in items["reels"]] == ["reel-only"]
    assert [item["shortCode"] for item in items["both"]] == ["both-post", "both-reel"]
    assert calls[0][0] == apify_sync.APIFY_ACTOR_ID
    assert calls[0][1]["directUrls"] == [
        "https://www.instagram.com/posts/",
        "https://www.instagram.com/both/",
    ]
    assert calls[1][0] == apify_sync.APIFY_REEL_ACTOR_ID
    assert calls[1][1]["username"] == ["reels", "both"]
    assert calls[1][1]["includeTranscript"] is False


def test_automated_collection_never_starts_the_reels_actor(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch(payload, timeout=180.0, actor_id=apify_sync.APIFY_ACTOR_ID):
        calls.append(actor_id)
        return []

    monkeypatch.setattr(apify_sync, "_fetch_apify_items", fake_fetch)
    apify_sync._collect_short_term_items(
        {"both": {"handle": "both", "scrape_mode": "both"}},
        20,
        datetime.now(UTC),
        include_reels=False,
    )

    assert calls == [apify_sync.APIFY_ACTOR_ID]


def test_manual_post_catch_up_can_extend_the_normal_posts_window() -> None:
    now = datetime(2026, 9, 4, 18, 0, tzinfo=UTC)

    payload = apify_sync._short_term_payload(["account"], 50, now, lookback_hours=24)

    assert payload["resultsLimit"] == 50
    assert payload["onlyPostsNewerThan"] == "2026-09-03T18:00:00Z"


def test_eight_hour_engagement_uses_rolling_window_and_never_inserts(monkeypatch) -> None:
    monkeypatch.setattr(apify_sync, "_needs_eight_hour_refresh", lambda *args: True)
    now = datetime(2026, 9, 5, 15, 0, tzinfo=UTC)  # 09:00 CST
    captured: dict = {}

    monkeypatch.setattr(apify_sync, "get_account_config", lambda account: {
        "handle": account,
        "scrape_mode": "reels",
        "table": "dashboard_posts",
    })
    monkeypatch.setattr(apify_sync, "_collect_short_term_items", lambda configs, limit, current, **kwargs: (
        captured.update(configs=configs, limit=limit, now=current, kwargs=kwargs) or {"account": []}
    ))
    monkeypatch.setattr(apify_sync, "_process_short_term_items", lambda account, cfg, items, current, **kwargs: (
        captured.update(process_kwargs=kwargs) or {"engagement": {"updated": 0}}
    ))
    monkeypatch.setattr(apify_sync, "_reconcile_queue_hot", lambda: None)
    monkeypatch.setattr("app.ingestion_jobs.now", lambda: now)

    result = apify_sync.run_day_engagement_cycle_batch(["account"])

    assert result["account"]["engagement"]["updated"] == 0
    assert captured["configs"]["account"]["scrape_mode"] == "posts"
    assert captured["kwargs"]["include_reels"] is False
    assert captured["kwargs"]["lookback_hours"] == 11
    assert captured["process_kwargs"]["insert_new"] is False
    assert captured["process_kwargs"]["refresh_window_hours"] == 8
    assert captured["process_kwargs"]["finalize_after_hours"] == 8


def test_eight_hour_engagement_finalizes_once_after_harvey_ball_expires(monkeypatch, tmp_path) -> None:
    now = datetime(2026, 9, 5, 15, 0, tzinfo=UTC)
    path = tmp_path / "eight-hour.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE dashboard_posts (
                id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, published_at TEXT,
                likes INTEGER, comments INTEGER, hot_checked INTEGER NOT NULL DEFAULT 1,
                likes_at_8h INTEGER, comments_at_8h INTEGER,
                refreshed_8h INTEGER NOT NULL DEFAULT 0, updated_at TEXT
            )"""
        )
        connection.executemany(
            "INSERT INTO dashboard_posts VALUES (?, 'account', ?, ?, 10, 1, 1, NULL, NULL, ?, '')",
            [
                (1, "inside", "2026-09-05T08:00:00+00:00", 0),
                (2, "crossed", "2026-09-05T06:30:00+00:00", 0),
                (3, "done", "2026-09-05T06:00:00+00:00", 1),
            ],
        )

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(db, "utc_now", lambda: now.isoformat())
    result = apify_sync._process_short_term_items(
        "account",
        {"table": "dashboard_posts", "group": "sentient", "hot_threshold": 600},
        [
            {"shortCode": "inside", "likesCount": 70, "commentsCount": 7},
            {"shortCode": "crossed", "likesCount": 85, "commentsCount": 8},
            {"shortCode": "done", "likesCount": 999, "commentsCount": 99},
        ],
        now,
        lookback_hours=11,
        insert_new=False,
        refresh_window_hours=8,
        finalize_after_hours=8,
    )

    assert result["engagement"]["updated"] == 2
    assert result["engagement"]["finalized_8h"] == 1
    with connect() as connection:
        inside = connection.execute("SELECT * FROM dashboard_posts WHERE shortcode = 'inside'").fetchone()
        crossed = connection.execute("SELECT * FROM dashboard_posts WHERE shortcode = 'crossed'").fetchone()
        done = connection.execute("SELECT * FROM dashboard_posts WHERE shortcode = 'done'").fetchone()
    assert (inside["likes"], inside["refreshed_8h"], inside["likes_at_8h"]) == (70, 0, None)
    assert (crossed["likes"], crossed["comments"], crossed["refreshed_8h"]) == (85, 8, 1)
    assert (crossed["likes_at_8h"], crossed["comments_at_8h"]) == (85, 8)
    assert (done["likes"], done["comments"]) == (10, 1)


def test_manual_reel_catch_up_can_extend_the_reels_window() -> None:
    now = datetime(2026, 9, 4, 18, 0, tzinfo=UTC)

    payload = apify_sync._short_term_reels_payload(["account"], 50, now, lookback_hours=24)

    assert payload["resultsLimit"] == 50
    assert payload["onlyPostsNewerThan"] == "2026-09-03T18:00:00Z"


def test_reel_only_recovery_does_not_repeat_the_profile_posts_call(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch(payload, timeout=180.0, actor_id=apify_sync.APIFY_ACTOR_ID):
        calls.append(actor_id)
        return []

    monkeypatch.setattr(apify_sync, "_fetch_apify_items", fake_fetch)
    apify_sync._collect_short_term_items(
        {"both": {"handle": "both", "scrape_mode": "both"}},
        20,
        datetime.now(UTC),
        include_posts=False,
        include_reels=True,
        lookback_hours=168,
    )

    assert calls == [apify_sync.APIFY_REEL_ACTOR_ID]


def test_reel_transcript_is_promoted_without_touching_caption() -> None:
    extracted = apify_sync.extract_apify_fields({
        "caption": "Visible Instagram caption",
        "transcript": "Text spoken in the Reel.",
    })

    assert extracted["transcript"] == "Text spoken in the Reel."


def test_dedupe_items_excludes_a_reel_returned_by_both_actor_shapes() -> None:
    items = apify_sync._dedupe_items([
        {"shortCode": "same-media", "url": "https://www.instagram.com/p/same-media/"},
        {"url": "https://www.instagram.com/reel/same-media/?igsh=test"},
        {"url": "https://www.instagram.com/reel/url-only-media/"},
    ])

    assert [item["shortCode"] for item in items] == ["same-media", "url-only-media"]


def test_completed_run_recovery_attributes_reels_and_profile_collaborations() -> None:
    configs = {
        "reels": {"handle": "reels"},
        "profile": {"handle": "profile"},
    }

    assert main._recovery_account_for_item(
        {"owner": {"username": "reels"}, "url": "https://www.instagram.com/reel/new-reel/"}, configs
    ) == "reels"
    assert main._recovery_account_for_item(
        {"ownerUsername": "collaborator", "inputUrl": "https://www.instagram.com/profile/"}, configs
    ) == "profile"
    assert main._recovery_account_for_item(
        {"owner": {"username": "untracked"}, "shortCode": "foreign"}, configs
    ) is None


def test_empty_profile_does_not_discard_other_accounts_paid_posts(monkeypatch):
    monkeypatch.setattr(apify_sync, '_fetch_apify_items', lambda *a, **k: [
        {'error': 'no_items', 'inputUrl': 'https://www.instagram.com/empty/'},
        {'shortCode': 'saved', 'ownerUsername': 'active', 'type': 'Image'},
    ])
    configs = {name: {'handle': name, 'scrape_mode': 'posts'} for name in ('empty', 'active')}
    result = apify_sync._collect_short_term_items(configs, 20, datetime.now(UTC), include_reels=False)
    assert result['empty'] == []
    assert result['active'][0]['shortCode'] == 'saved'


def test_unattributed_post_does_not_block_attributed_paid_posts(monkeypatch, caplog):
    monkeypatch.setattr(apify_sync, '_fetch_apify_items', lambda *a, **k: [
        {'shortCode': 'unmatched', 'ownerUsername': 'neighboring-profile'},
        {'shortCode': 'saved', 'ownerUsername': 'active', 'type': 'Image'},
    ])
    configs = {'active': {'handle': 'active', 'scrape_mode': 'posts'}}

    result = apify_sync._collect_short_term_items(
        configs, 20, datetime.now(UTC), include_reels=False
    )

    assert [item['shortCode'] for item in result['active']] == ['saved']
    assert 'skipped 1 post(s) without a matching account' in caplog.text


def test_reconcile_queue_hot_uses_database_connection(monkeypatch):
    connection = object()
    calls = []

    @contextmanager
    def fake_connect():
        yield connection

    monkeypatch.setattr('app.db.connect', fake_connect)
    monkeypatch.setattr('app.main._queue_v2_auto_pool_hot', calls.append)

    apify_sync._reconcile_queue_hot()

    assert calls == [connection]


def test_unavailable_profile_does_not_block_the_shared_scheduled_batch(monkeypatch, caplog):
    monkeypatch.setattr(apify_sync, '_fetch_apify_items', lambda *a, **k: [
        {
            'url': 'https://www.instagram.com/missing/',
            'username': 'missing',
            'error': 'not_found',
        },
        {'shortCode': 'saved', 'ownerUsername': 'active', 'type': 'Image'},
    ])
    configs = {name: {'handle': name, 'scrape_mode': 'posts'} for name in ('missing', 'active')}

    result = apify_sync._collect_short_term_items(configs, 20, datetime.now(UTC), include_reels=False)

    assert result['missing'] == []
    assert [item['shortCode'] for item in result['active']] == ['saved']
    assert 'skipped unavailable profile missing: not_found' in caplog.text


def test_profile_feed_reel_is_kept_and_profile_attribution_is_preserved(monkeypatch):
    monkeypatch.setattr(apify_sync, '_fetch_apify_items', lambda *a, **k: [
        {'shortCode': 'reel', 'ownerUsername': 'collaborator', 'type': 'Video', 'productType': 'clips', 'inputUrl': 'https://www.instagram.com/active/'},
        {'shortCode': 'shared', 'ownerUsername': 'collaborator', 'type': 'Image', 'inputUrl': 'https://www.instagram.com/active/'},
    ])
    result = apify_sync._collect_short_term_items({'active': {'handle': 'active', 'scrape_mode': 'posts'}}, 20, datetime.now(UTC), include_reels=False)
    assert [item['shortCode'] for item in result['active']] == ['reel', 'shared']


def test_short_apify_call_respects_the_callers_deadline(monkeypatch):
    captured = {}

    def run(payload, max_wait_seconds, poll_interval, actor_id):
        captured.update(payload=payload, max_wait_seconds=max_wait_seconds, poll_interval=poll_interval, actor_id=actor_id)
        return []

    monkeypatch.setattr(apify_sync, "_run_apify_actor_and_fetch", run)
    apify_sync._fetch_apify_items({"directUrls": ["https://example.test/post"]}, timeout=80)

    assert captured["max_wait_seconds"] == 80
    assert captured["poll_interval"] == 5.0


def test_reload_counts_marks_post_deleted_after_two_empty_direct_checks(monkeypatch, tmp_path):
    path = tmp_path / "reload.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE dashboard_posts (
                id INTEGER PRIMARY KEY, account TEXT, shortcode TEXT, likes INTEGER, comments INTEGER,
                permalink TEXT, cover_image_path TEXT, cover_source_url TEXT, is_deleted INTEGER, updated_at TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO dashboard_posts VALUES (1, 'account', 'post', 100, 10, '', '', '', 0, '2026-09-10')"
        )

    @contextmanager
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    calls = []
    monkeypatch.setattr(db, "connect", connect)
    monkeypatch.setattr(apify_sync, "get_account_config", lambda _: {"table": "dashboard_posts"})
    monkeypatch.setattr(apify_sync, "_fetch_apify_items", lambda payload, **_: calls.append(payload) or [])

    result = apify_sync.refresh_single_post("account", "post")

    assert [call["resultsType"] for call in calls] == ["details", "posts"]
    assert result["deleted"] is True
    with connect() as connection:
        assert tuple(connection.execute("SELECT likes, comments, is_deleted FROM dashboard_posts").fetchone()) == (100, 10, 1)
