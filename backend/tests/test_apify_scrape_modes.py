from __future__ import annotations

from datetime import UTC, datetime

from app import apify_sync


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

    assert [item["shortCode"] for item in items["posts"]] == ["post-only"]
    assert [item["shortCode"] for item in items["reels"]] == ["reel-only"]
    assert [item["shortCode"] for item in items["both"]] == ["both-post", "both-reel"]
    assert calls[0][0] == apify_sync.APIFY_ACTOR_ID
    assert calls[0][1]["directUrls"] == [
        "https://www.instagram.com/posts/",
        "https://www.instagram.com/both/",
    ]
    assert calls[1][0] == apify_sync.APIFY_REEL_ACTOR_ID
    assert calls[1][1]["username"] == ["reels", "both"]
    assert calls[1][1]["includeTranscript"] is True


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
