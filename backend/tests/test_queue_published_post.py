from app import main


def test_closed_queue_request_uses_the_published_dashboard_post(monkeypatch):
    row = {
        "status": "closed",
        "final_permalinks": '[{"account":"evolving.ai","url":"https://instagram.com/p/published123/"}]',
        "final_permalink": "https://instagram.com/p/published123/",
        "recommended_accounts": '["evolving.ai"]',
        "post_account": "uncover.ai",
    }
    monkeypatch.setattr(main, "_queue_v2_existing_dashboard_post_from_url", lambda url: {"account": "evolving.ai", "shortcode": "published123"})
    monkeypatch.setattr(main, "_queue_v2_post_snapshot", lambda account, shortcode: {
        "id": 42, "caption": "The delivered post", "type": "Carousel",
        "permalink": "https://www.instagram.com/p/published123/", "publishedAt": "2026-09-04T00:00:00Z",
        "likes": 12, "comments": 3,
    })

    assert main._queue_v2_published_dashboard_post(row) == {
        "id": 42, "account": "evolving.ai", "shortcode": "published123", "title": "The delivered post",
        "isCustom": False, "permalink": "https://www.instagram.com/p/published123/", "caption": "The delivered post",
        "type": "Carousel", "coverUrl": "", "publishedAt": "2026-09-04T00:00:00Z", "likes": 12, "comments": 3,
    }


def test_closed_queue_request_keeps_source_when_delivery_is_not_in_dashboard(monkeypatch):
    row = {
        "status": "closed", "final_permalinks": '[{"account":"evolving.ai","url":"https://instagram.com/p/not-imported/"}]',
        "final_permalink": "https://instagram.com/p/not-imported/", "recommended_accounts": '["evolving.ai"]',
        "post_account": "uncover.ai",
    }
    monkeypatch.setattr(main, "_queue_v2_existing_dashboard_post_from_url", lambda url: None)

    assert main._queue_v2_published_dashboard_post(row) is None
