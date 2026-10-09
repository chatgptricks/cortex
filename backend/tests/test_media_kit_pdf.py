from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from copy import deepcopy

from PIL import Image
from pypdf import PdfReader
import pytest

from app.media_kit_pdf import _contrast, _date, _fmt, _make_palette, render_media_kit_pdf


def sample_report():
    stats = {"total": 100, "average": 50.5, "median": 50.5, "min": 0, "max": 100,
             "count": 2, "coverage_pct": 100}
    zero = {**stats, "total": 0, "average": 0, "median": 0, "min": 0, "max": 0}
    period = {"post_count": 2, "posts_per_week": 0.5, "metrics": {"likes": stats, "comments": zero},
              "engagements": stats, "complete_engagement_posts": 1,
              "engagement_rate_pct": 5.05, "like_rate_pct": 5.05, "view_rate_pct": None}
    post = {"published_at": "2026-10-08T18:00:00Z", "caption": "INTERNAL_TITLE_SECRET", "public_caption": "A useful creative example",
            "format": "Reel", "metrics": {"likes": 100, "comments": None},
            "engagements": 100, "engagement_complete": False,
            "permalink": "https://www.instagram.com/p/example/"}
    return {
        "generated_at": "2026-10-09T18:00:00Z", "timezone": "America/Costa_Rica",
        "account": {"handle": "sample", "name": "INTERNAL_NAME_SECRET", "public_name": "Sample Studio", "followers": 1_000,
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


def pdf_text(output):
    reader = PdfReader(io.BytesIO(output))
    return reader, "\n".join(page.extract_text() for page in reader.pages)


def test_public_report_is_two_pages_with_links_known_zero_and_no_private_facts():
    report = sample_report()
    reader, text = pdf_text(render_media_kit_pdf(report))
    assert len(reader.pages) == 2
    assert "Sample Studio" in text
    assert "A useful creative example" in text
    assert "08 Oct 2026 / Reel" in text
    assert "Posts published in the last 30 days" in text
    assert "current cumulative public counts" in " ".join(text.split())
    assert "Average comments".upper() in text
    assert "0" in text
    assert "N/A" not in text
    assert "sales@example.test" not in text
    assert "Costa Rica" not in text
    assert "INTERNAL_NAME_SECRET" not in text
    assert "INTERNAL_TITLE_SECRET" not in text
    assert "Prepared 09 Oct 2026" in text
    links = [annotation.get_object().get("/A", {}).get("/URI")
             for page in reader.pages for annotation in page.get("/Annots", [])]
    assert report["best_posts"]["all_time"][0]["permalink"] in links
    assert "https://www.instagram.com/sample/" in links


def test_owned_images_embed_in_public_profile_and_post_cards_and_bad_images_fall_back():
    report = sample_report()
    image = io.BytesIO()
    Image.new("RGB", (90, 90), (0, 165, 145)).save(image, "JPEG")
    report["account"]["avatar_bytes"] = image.getvalue()
    report["best_posts"]["all_time"][0]["thumbnail_bytes"] = image.getvalue()
    reader = PdfReader(io.BytesIO(render_media_kit_pdf(report)))
    assert len(reader.pages[0].images) == 1
    assert len(reader.pages[1].images) == 1  # Reused bytes are embedded once.
    assert sum(operator == b"Do" for _, operator in reader.pages[1].get_contents().operations) == 3  # Header and both cohorts.
    report["account"]["avatar_bytes"] = b"not an image"
    assert render_media_kit_pdf(report).startswith(b"%PDF-")


def test_empty_account_remains_two_pages_without_unavailable_metric_cards():
    reader, text = pdf_text(render_media_kit_pdf({"account": {"handle": "empty"}}))
    assert len(reader.pages) == 2
    assert "Public performance highlights are not available yet." in text
    assert "N/A" not in text
    assert "Not available" not in text
    assert "Total likes".upper() not in text


def test_private_metadata_never_enters_pdf_and_public_highlights_are_capped_at_three():
    report = sample_report()
    report["account"].update({"label": "SECRET_ACCOUNT_LABEL", "group": "SECRET_ACCOUNT_GROUP",
        "subcategory": "SECRET_ROUTE", "name": "SECRET_REGISTRY_NAME", "email": "private-email@secret.test",
        "phone": "PRIVATE_PHONE_VALUE", "bio": "SECRET_BIO_PRIVATE", "id": "SECRET_ACCOUNT_ID",
        "demographics": {"audience": "SECRET_DEMOGRAPHICS"},
        "pricing": {"reel": "SECRET_COMMERCIAL_RATE"}, "packages": ["SECRET_PREMIUM_PACKAGE"],
        "upsell": "SECRET_BRAND_PARTNERSHIP_UPSELL",
        "profile_url": "https://www.instagram.com/sample/?tracking=SECRET_PROFILE_QUERY"})
    report["coverage"] = {"notes": ["SECRET_STORAGE_NOTE"], "snapshot_count": 777777}
    report["content"] = {"hashtags": [{"label": "SECRET_HASHTAG"}]}
    report["breakdowns"] = {"hours": [{"label": "SECRET_HOUR"}]}
    report["metric_catalog"] = [{"key": "model_attention", "label": "SECRET_MODEL_LABEL", "unit": "model_score"}]
    report["metrics_appendix"] = [{"key": "likes_1h", "label": "SECRET_FIRST_HOUR_METRIC", "all_time": {"total": 777777}}]
    for period in report["summary"].values():
        period["metrics"].update({"model_attention": {"total": 777777}, "likes_1h": {"total": 777777},
                                  "routing_score": {"average": 777777}, "HOT": {"total": 777777}})
    posts = []
    for index in range(8):
        post = deepcopy(report["best_posts"]["all_time"][0])
        post.update({"caption": "SECRET_INTERNAL_TITLE", "hook_text": "SECRET_OCR_HOOK",
                     "id": "SECRET_POST_ID", "rank_metric": "SECRET_RANK_METRIC",
                     "shortcode": f"public_{index}",
                     "permalink": f"https://www.instagram.com/p/public_{index}/?tracking=SECRET_POST_QUERY",
                     "public_caption": f"Public creative example {index}"})
        posts.append(post)
    report["best_posts"] = {"all_time": posts, "last_30_days": posts,
                            "by_metric": {"model_score": {"all_time": posts}}}
    reader, text = pdf_text(render_media_kit_pdf(report))
    assert len(reader.pages) == 2
    for index in range(3):
        assert f"Public creative example {index}" in text
    assert "Public creative example 3" not in text
    links = [annotation.get_object().get("/A", {}).get("/URI", "")
             for page in reader.pages for annotation in page.get("/Annots", [])]
    searchable = text + str(reader.metadata) + str(links)
    assert "SECRET" not in searchable
    assert "private-email@secret.test" not in searchable
    assert "PRIVATE_PHONE_VALUE" not in searchable
    assert "777777" not in searchable
    for forbidden in ("complete metric overview", "model", "first-hour", "8-hour", "pace", "routing", "coverage",
                      "stored", "source", "demographics", "publishing by weekday", "follower history"):
        assert forbidden not in text.lower()
    assert all(link.startswith("https://www.instagram.com/") and "?" not in link for link in links)


def test_full_public_name_wraps_without_ellipsis_and_editorial_font_is_embedded():
    report = sample_report()
    full_name = "A public creator studio with an exceptionally long name for artificial intelligence and thoughtful original creative storytelling"
    report["account"]["public_name"] = full_name
    reader, text = pdf_text(render_media_kit_pdf(report, theme="dark", accent="#00ac80"))
    assert full_name in " ".join(text.split())
    assert len(reader.pages) == 2
    fonts = reader.pages[0]["/Resources"]["/Font"].get_object().values()
    display_fonts = [font.get_object() for font in fonts if "Anton" in str(font.get_object().get("/BaseFont", ""))]
    assert display_fonts, "The PDF must embed its condensed display face for portable rendering"
    assert all(font["/FontDescriptor"].get_object().get("/FontFile2") for font in display_fonts)
    assert "CONTENT THAT" in text and "CONNECTS." in text


def test_public_caption_about_product_price_is_preserved_without_commercial_package_fields():
    report = sample_report()
    report["account"]["pricing"] = "SECRET_PRIVATE_RATE"
    report["account"]["packages"] = ["SECRET_PRIVATE_CAMPAIGN_BUNDLE"]
    report["best_posts"]["all_time"][0]["public_caption"] = "A new public AI product costs $20 per month."
    reader, text = pdf_text(render_media_kit_pdf(report))
    assert "A new public AI product costs $20 per month." in " ".join(text.split())
    assert "SECRET" not in text + str(reader.metadata)


def test_public_numbers_limit_decimals_preserve_small_averages_and_select_available_video_counts():
    report = sample_report()
    report["account"]["followers"] = 1000.123456
    report["follower_growth"]["30d"] = {"pct": 1.712345}
    for period in report["summary"].values():
        period["metrics"]["likes"] = {"total": 1234.123456, "average": 0.023809}
        period["metrics"]["comments"] = {"total": 123.123456, "average": 23.123456}
        period["metrics"]["video_plays"] = {"total": 9876.123456, "average": 37.123456}
    reader, text = pdf_text(render_media_kit_pdf(report))
    assert len(reader.pages) == 2
    assert "0.02" in text
    assert "+1.71%" in text
    assert "AVERAGE VIDEO PLAYS" in text
    assert "AVERAGE VIDEO VIEWS" not in text
    assert "N/A" not in text
    assert not re.findall(r"(?<![\w.])[-+]?\d+\.\d{3,}(?![\w.])", text)


def test_missing_recent_period_hides_misleading_two_posts_117_likes_and_recent_links():
    report = sample_report()
    incomplete = deepcopy(report["best_posts"]["last_30_days"][0])
    incomplete.update({"public_caption": "INCOMPLETE_RECENT_ONLY_EXAMPLE",
                       "permalink": "https://www.instagram.com/p/recent_only/",
                       "metrics": {"likes": 117, "comments": 0}})
    report["summary"]["last_30_days"] = {"post_count": 2, "metrics": {"likes": {"total": 117}}}
    report["best_posts"]["last_30_days"] = [incomplete]
    _, before = pdf_text(render_media_kit_pdf(report))
    assert "117" in before
    assert "INCOMPLETE_RECENT_ONLY_EXAMPLE" in before
    # The projection's omitted summary key is authoritative, even if a stale
    # recent-only post list is accidentally retained in the input.
    del report["summary"]["last_30_days"]
    reader, text = pdf_text(render_media_kit_pdf(report))
    assert len(reader.pages) == 2
    assert "117" not in text
    assert "Posts published in the last 30 days" not in text
    assert "PUBLISHED IN THE LAST 30 DAYS" not in text
    assert "INCOMPLETE_RECENT_ONLY_EXAMPLE" not in text
    assert "A useful creative example" in text
    assert "HISTORICAL HIGHLIGHTS" in text
    for forbidden in ("coverage", "incomplete", "stored", "source", "stale"):
        assert forbidden not in text.lower()
    links = [annotation.get_object().get("/A", {}).get("/URI", "")
             for page in reader.pages for annotation in page.get("/Annots", [])]
    assert "https://www.instagram.com/p/recent_only/" not in links


def test_confirmed_zero_post_period_stays_visible_and_is_not_missing_recent_data():
    report = sample_report()
    report["summary"]["last_30_days"] = {"post_count": 0, "metrics": {
        key: {"total": 0, "average": None} for key in ("likes", "comments", "video_views")}}
    report["best_posts"]["last_30_days"] = []
    reader, text = pdf_text(render_media_kit_pdf(report))
    assert len(reader.pages) == 2
    assert "Posts published in the last 30 days" in text
    recent_text = reader.pages[0].extract_text().split("Posts published in the last 30 days")[1]
    assert recent_text.count("\n0\n") == 4
    assert "No public posts were published in this period." in text
    assert "N/A" not in text
    assert "current cumulative public counts" in " ".join(text.split())


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("accent", ["#00ac80", "#8838ff", "#d6ff2a", "#ffffff", "#000000", "#777777"])
def test_report_palette_keeps_exact_accent_and_contrasting_semantic_text(theme, accent):
    palette = _make_palette(theme, accent)
    assert palette.values["accent"] == accent.upper()
    assert _contrast(palette.values["accent_ink"], palette.values["accent"]) >= 4.5
    assert _contrast(palette.values["accent_text"], palette.values["background"]) >= 4.5
    assert _contrast(palette.values["accent_text"], palette.values["card"]) >= 4.5
    assert _contrast(palette.values["ink"], palette.values["card"]) >= 4.5
    assert _contrast(palette.values["header_ink"], palette.values["header"]) >= 4.5
    output = render_media_kit_pdf(sample_report(), theme=theme, accent=accent)
    reader = PdfReader(io.BytesIO(output))
    fill_colors = [operands for operands, operator in reader.pages[0].get_contents().operations if operator == b"rg"]
    expected = [int(accent[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    assert any(tuple(values) == pytest.approx(expected, abs=1e-6) for values in fill_colors)
    if _contrast(palette.values["accent"], palette.values["card"]) < 3:
        stroke_colors = [operands for operands, operator in reader.pages[0].get_contents().operations if operator == b"RG"]
        stroke = palette.values["accent_text"]
        expected_stroke = [int(stroke[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        assert any(tuple(values) == pytest.approx(expected_stroke, abs=1e-6) for values in stroke_colors)
    assert reader.metadata["/CreationDate"].startswith("D:20261009180000")


def test_invalid_renderer_palette_falls_back_and_palette_values_cannot_be_mutated():
    report = sample_report()
    assert render_media_kit_pdf(report, theme="invalid", accent="url(javascript:bad)") == render_media_kit_pdf(report)
    palette = _make_palette("dark", "#00ac80")
    with pytest.raises(TypeError):
        palette.values["accent"] = "#ffffff"
    with pytest.raises(FrozenInstanceError):
        palette.values = {}
    color = palette.accent
    color.red = 1
    assert palette.accent.red == 0


def test_parallel_theme_reports_are_deterministic_and_do_not_leak_palette_state():
    report = sample_report()
    selections = [("light", "#d6ff2a"), ("dark", "#8838ff"), ("dark", "#00ac80"),
                  ("light", "#ffffff"), ("dark", "#000000")]
    reference = {selection: render_media_kit_pdf(report, theme=selection[0], accent=selection[1])
                 for selection in selections}
    def render(selection):
        return selection, render_media_kit_pdf(report, theme=selection[0], accent=selection[1])
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(render, selections * 3))
    assert all(output == reference[selection] for selection, output in results)
    assert len(set(reference.values())) == len(selections)


def test_spanish_pdf_translates_copy_formats_dates_and_preserves_public_content():
    report = sample_report()
    report["generated_at"] = "2026-03-03T02:00:00Z"  # March 2 in Costa Rica.
    report["account"].update({"verified": True, "public_bio": "This public biography stays in its original language."})
    report["follower_growth"]["30d"] = {"pct": 1.712345}
    report["summary"]["all_time"]["metrics"]["likes"]["average"] = 0.023809
    for posts in report["best_posts"].values():
        if isinstance(posts, list):
            for post in posts:
                post.update({"format": "Image", "published_at": "2026-04-03T02:00:00Z"})
    original = deepcopy(report)
    reader, text = pdf_text(render_media_kit_pdf(report, lang="es"))
    flattened = " ".join(text.split())
    assert len(reader.pages) == 2
    assert reader.trailer["/Root"]["/Lang"] == "es-CR"
    for expected in ("AUDIENCIA Y RENDIMIENTO", "SEGUIDORES.", "CONTENIDO QUE", "CONECTA.",
                     "ME GUSTA PROMEDIO", "COMENTARIOS PROMEDIO", "Perfil verificado",
                     "Publicaciones de los últimos 30 días", "PUBLICADAS EN LOS ÚLTIMOS 30 DÍAS",
                     "VER PERFIL PÚBLICO", "VER PUBLICACIÓN", "02 abr 2026 / Imagen",
                     "Preparado 02 mar 2026", "+1,71%", "0,02"):
        assert expected in flattened
    assert "Sample Studio" in text and "A useful creative example" in flattened
    assert report["account"]["public_bio"] in flattened
    assert "INTERNAL" not in text + str(reader.metadata)
    assert "Average comments".upper() not in text and "VIEW PUBLIC POST" not in text
    assert "0" in text and "N/D" not in text
    assert report == original


def test_public_content_that_matches_interface_copy_is_never_translated():
    report = sample_report()
    report["account"].update({"public_name": "Content examples", "public_bio": "Verified profile"})
    report["best_posts"]["all_time"][0]["public_caption"] = "Average likes"
    _, text = pdf_text(render_media_kit_pdf(report, lang="es"))
    for original in ("Content examples", "Verified profile", "Average likes"):
        assert original in text
    assert "EJEMPLOS DE CONTENIDO" in text


def test_spanish_empty_recent_and_missing_recent_keep_known_zero_distinct():
    report = sample_report()
    report["summary"]["last_30_days"] = {"post_count": 0, "metrics": {
        key: {"total": 0, "average": None} for key in ("likes", "comments", "video_views")}}
    report["best_posts"]["last_30_days"] = []
    reader, text = pdf_text(render_media_kit_pdf(report, lang="es"))
    recent = reader.pages[0].extract_text().split("Publicaciones de los últimos 30 días")[1]
    assert recent.count("\n0\n") == 4
    assert "No se publicaron posts públicos en este período." in text
    del report["summary"]["last_30_days"]
    _, missing = pdf_text(render_media_kit_pdf(report, lang="es"))
    assert "Publicaciones de los últimos 30 días" not in missing
    assert "PUBLICADAS EN LOS ÚLTIMOS 30 DÍAS" not in missing
    assert "PUBLICACIONES DESTACADAS" in missing


def test_localized_formatters_preserve_magnitude_precision_and_calendar_day():
    assert _fmt(1234.56, lang="es") == "1 234,56"
    assert _fmt(12500, compact=True, lang="es") == "12,5 mil"
    assert _fmt(1250000000, compact=True, lang="es") == "1,2 mil M"
    assert _fmt(-0.001, "percent", lang="es") == ">-0,01%"
    assert _fmt(None, lang="es") == "N/D"
    assert _date("2026-03-03T02:00:00Z", lang="es") == "02 mar 2026"
    assert _date("2026-03-03", lang="es") == "03 mar 2026"
    assert _date(None, lang="es") == "No disponible"
    assert _fmt(1234.56) == "1,234.56"
    assert _date("2026-03-03T02:00:00Z") == "02 Mar 2026"


def test_long_spanish_secondary_numbers_are_fitted_without_truncating_magnitude():
    report = sample_report()
    for posts in report["best_posts"].values():
        if isinstance(posts, list):
            for post in posts:
                post["metrics"] = {"video_views": 254784, "likes": 12543, "comments": 154, "video_plays": 987654}
    _, text = pdf_text(render_media_kit_pdf(report, lang="es"))
    assert "987,7 mil" in text
    assert "12,5 mil" in text
    assert "reprod." in text
    assert "987,7..." not in text


def test_language_and_theme_are_report_local_and_default_remains_english():
    report = sample_report()
    assert render_media_kit_pdf(report) == render_media_kit_pdf(report, lang="en")
    assert render_media_kit_pdf(report, lang="invalid") == render_media_kit_pdf(report, lang="en")
    selections = [(lang, theme) for lang in ("en", "es") for theme in ("light", "dark")]
    reference = {(lang, theme): render_media_kit_pdf(report, lang=lang, theme=theme) for lang, theme in selections}
    def render(selection):
        lang, theme = selection
        return selection, render_media_kit_pdf(report, lang=lang, theme=theme)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(render, selections * 3))
    assert all(output == reference[selection] for selection, output in results)
    assert len(set(reference.values())) == 4
