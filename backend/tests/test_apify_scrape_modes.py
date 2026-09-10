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


def test_reload_counts_falls_back_without_hiding_a_live_post(monkeypatch, tmp_path):
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

    with pytest.raises(apify_sync.ApifySyncError, match="saved counts were kept"):
        apify_sync.refresh_single_post("account", "post")

    assert [call["resultsType"] for call in calls] == ["details", "posts"]
    with connect() as connection:
        assert tuple(connection.execute("SELECT likes, comments, is_deleted FROM dashboard_posts").fetchone()) == (100, 10, 0)
