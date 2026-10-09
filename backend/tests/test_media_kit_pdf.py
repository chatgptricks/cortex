from __future__ import annotations

import io
from copy import deepcopy

from PIL import Image
from pypdf import PdfReader

from app.media_kit_pdf import render_media_kit_pdf


def sample_report():
    stats = {"total": 100, "average": 50.5, "median": 50.5, "min": 0, "max": 100,
             "count": 2, "coverage_pct": 100}
    zero = {**stats, "total": 0, "average": 0, "median": 0, "min": 0, "max": 0}
    period = {"post_count": 2, "posts_per_week": 0.5, "metrics": {"likes": stats, "comments": zero},
              "engagements": stats, "complete_engagement_posts": 1,
              "engagement_rate_pct": 5.05, "like_rate_pct": 5.05, "view_rate_pct": None}
    post = {"published_at": "2026-10-08T18:00:00Z", "caption": "A useful creative example",
            "format": "Reel", "metrics": {"likes": 100, "comments": None},
            "engagements": 100, "engagement_complete": False,
            "permalink": "https://www.instagram.com/p/example/"}
    return {
        "generated_at": "2026-10-09T18:00:00Z", "timezone": "America/Costa_Rica",
        "account": {"handle": "sample", "name": "Sample Studio", "followers": 1_000,
                    "following": 0, "profile_posts": 2, "private": False, "verified": None,
                    "email": "sales@example.test", "bio": "Useful studio profile",
                    "demographics": {"countries": {"Costa Rica": 75}}},
        "summary": {key: deepcopy(period) for key in ("all_time", "last_30_days", "previous_30_days", "last_90_days")},
        "metric_catalog": [{"key": "likes", "label": "Likes", "unit": "count"},
                           {"key": "comments", "label": "Comments", "unit": "count"}],
        "metrics_appendix": [{"key": "likes", "label": "Likes", "unit": "count", "all_time": stats, "last_30_days": stats},
                             {"key": "comments", "label": "Comments", "unit": "count", "all_time": zero, "last_30_days": zero}],
        "follower_history": [{"date": "2026-10-08T18:00:00Z", "followers": 1_000,
                              "following": 0, "profile_posts": 2, "delta": None}],
        "follower_growth": {key: None for key in ("1d", "7d", "30d", "90d", "all_time")},
        "breakdowns": {"formats": [{"label": "Reel", "share_pct": 100, **deepcopy(period)}]},
        "best_posts": {"all_time": [post], "last_30_days": [post], "by_metric": {}},
        "content": {"hashtags": [{"label": "creative", "post_count": 2}], "metadata_counts": {"hashtags": 2}},
        "coverage": {"post_count": 2, "dated_posts": 2, "snapshot_count": 1, "notes": []},
    }


def test_report_handles_missing_growth_and_preserves_samples_contacts_and_local_time():
    report = sample_report()
    output = render_media_kit_pdf(report)
    assert output.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(output))
    text = "\n".join(page.extract_text() for page in reader.pages)
    normalized = " ".join(text.split())
    assert "09 Oct 2026, 12:00" in text
    assert "sales@example.test" in text
    assert "Demographics / countries / Costa Rica" in normalized
    assert "Measured engagements" in text
    assert "partial engagements" in text
    assert "Follower delta" in text
    assert "creative (2)" in text
    assert "N/A" in text and "0" in text
    assert len(reader.pages) >= 5
    links = [annotation.get_object().get("/A", {}).get("/URI")
             for page in reader.pages for annotation in page.get("/Annots", [])]
    assert report["best_posts"]["all_time"][0]["permalink"] in links


def test_optional_owned_image_bytes_embed_and_bad_images_fall_back():
    report = sample_report()
    image = io.BytesIO()
    Image.new("RGB", (90, 90), (0, 165, 145)).save(image, "JPEG")
    report["account"]["avatar_bytes"] = image.getvalue()
    report["best_posts"]["all_time"][0]["thumbnail_bytes"] = image.getvalue()
    reader = PdfReader(io.BytesIO(render_media_kit_pdf(report)))
    assert len(reader.pages[0].images) == 1
    assert len(reader.pages[3].images) == 1
    report["account"]["avatar_bytes"] = b"not an image"
    assert render_media_kit_pdf(report).startswith(b"%PDF-")


def test_empty_account_and_every_dynamic_metric_paginate_without_losing_values():
    report = sample_report()
    report["best_posts"] = {}
    report["follower_history"] = []
    report["summary"] = {}
    report["metrics_appendix"] = []
    report["content"] = {}
    report["account"] = {"handle": "empty"}
    assert render_media_kit_pdf(report).startswith(b"%PDF-")
    report = sample_report()
    for index in range(50):
        stats = {"total": 123_456_789 + index, "average": 1, "median": 1, "min": 0,
                 "max": 123_456_789 + index, "count": 2, "coverage_pct": 100}
        report["metrics_appendix"].append({"key": f"provider.metric_{index}",
            "label": f"Provider metric {index}", "unit": "count", "all_time": stats,
            "last_30_days": stats})
    reader = PdfReader(io.BytesIO(render_media_kit_pdf(report)))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Provider metric 49" in text
    assert "123,456,838" in text
    assert any("Complete metric inventory continued" in page.extract_text() for page in reader.pages)


def test_rich_creative_signals_keep_source_note_with_content_instead_of_an_orphan_page():
    report = sample_report()
    period = report["summary"]["all_time"]
    report["breakdowns"]["formats"] = [{"label": label, "share_pct": 25, **deepcopy(period)}
                                      for label in ("Carousel", "Image", "Reel", "Video")]
    report["content"].update({
        "hashtags": [{"label": value, "post_count": 40} for value in
                     ("ai", "chatgpt", "artificialintelligence", "openai", "AI", "aitools", "tech", "samaltman", "gpt4")],
        "mentions": [{"label": value, "post_count": 40} for value in
                     ("chatgptricks", "higgsfield.ai", "openai", "lovable.dev", "zuck", "Higgsfield.ai", "chatgpt", "chatgptips", "1x.technologies")],
        "coauthors": [{"label": value, "post_count": 40} for value in
                     ("chatgptips", "chatgptricks", "trends", "openai", "aigleeson", "chatgpt", "saysirio", "viclaranja", "speakersdotca")],
        "music": [{"label": value, "post_count": 40} for value in
                  ("Original audio", "Follow @chatgptricks for more!", "Sora 2",
                   "Follow @chatgptricks for more", "Follow @chatgptricks for more!",
                   "i was only temporary (Slowed + Reverb)", "Solitude", "Movies", "Time")],
    })
    reader = PdfReader(io.BytesIO(render_media_kit_pdf(report)))
    content_pages = [page.extract_text() for page in reader.pages if "The content profile" in page.extract_text()]
    assert len(content_pages) == 1
    assert "Publishing times reflect" in content_pages[0]
    assert "Music / audio" in content_pages[0]
