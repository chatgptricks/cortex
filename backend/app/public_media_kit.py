"""Strict public-only projection of the internal, fresh account report.

This boundary creates new dictionaries at every level. New fields in the
internal report never become client-shareable PDF content automatically.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

_PUBLIC_METRICS = ("likes", "comments", "video_views", "video_plays")
_HANDLE = re.compile(r"[A-Za-z0-9_.]{1,30}\Z")
_SHORTCODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_POST_PATH = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]{1,64})/?\Z")
_POST_FORMATS = {"Carousel", "Image", "Reel", "Video"}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _number(value: Any, *, signed: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        valid = math.isfinite(value) and (signed or value >= 0)
    except OverflowError:
        return None
    return value if valid else None


def _count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number == int(number) else None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _flag(value: Any) -> bool:
    return value is True or value == 1 or isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"}


def _stats(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    return {field: _number(source.get(field)) for field in ("total", "average")}


def _public_name(account: dict[str, Any], handle: str) -> str:
    explicit = _text(account.get("public_name"), 200)
    if explicit:
        return explicit
    # Never promote ambiguous legacy names or internal account labels.
    return f"@{handle}" if handle else "Account"


def _period(value: Any, followers: int | None) -> dict[str, Any]:
    source = _mapping(value)
    metrics = _mapping(source.get("metrics"))
    selected = {key: _stats(metrics.get(key)) for key in _PUBLIC_METRICS}
    totals = [selected[key]["total"] for key in ("likes", "comments") if selected[key]["total"] is not None]
    engagement_total = _number(sum(totals)) if totals else None
    population = _count(source.get("post_count"))
    measured = _count(_mapping(source.get("engagements")).get("count"))
    # The engagement population is the union of posts with known likes or
    # comments, so summing the two separate averages would be misleading.
    average = _number(engagement_total / measured) if engagement_total is not None and measured and population is not None and measured <= population else None
    return {
        "post_count": population,
        "metrics": selected,
        "engagements": {"total": engagement_total, "average": average},
        "engagement_rate_pct": _number(average / followers * 100) if average is not None and followers else None,
    }


def _post_code(post: dict[str, Any]) -> str | None:
    code = post.get("shortcode")
    if isinstance(code, str) and _SHORTCODE.fullmatch(code):
        return code
    link = post.get("permalink")
    if not isinstance(link, str):
        return None
    try:
        parsed = urlsplit(link)
        match = _POST_PATH.fullmatch(parsed.path)
        if parsed.scheme == "https" and parsed.netloc.lower() in {"instagram.com", "www.instagram.com"} and not parsed.query and not parsed.fragment and match:
            return match.group(1)
    except ValueError:
        pass
    return None


def _posts(value: Any, *, followers: int | None, generated: datetime | None, recent: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or generated is None:
        return []
    result = []
    seen = set()
    for raw in value:
        source = _mapping(raw)
        if _flag(source.get("hidden")) or _flag(source.get("is_deleted")):
            continue
        code = _post_code(source)
        published = _date(source.get("published_at"))
        if not code or code in seen or published is None or published > generated:
            continue
        if recent and published < generated - timedelta(days=30):
            continue
        metrics = _mapping(source.get("metrics"))
        selected = {key: _number(metrics.get(key)) for key in _PUBLIC_METRICS}
        components = [selected[key] for key in ("likes", "comments") if selected[key] is not None]
        engagements = _number(sum(components)) if components else None
        post = {
            "shortcode": code,
            "permalink": f"https://www.instagram.com/p/{code}/",
            "public_caption": _text(source.get("public_caption"), 500),
            "format": source.get("format") if isinstance(source.get("format"), str) and source["format"] in _POST_FORMATS else None,
            "published_at": published.isoformat(timespec="seconds") if published else None,
            "metrics": selected,
            "engagements": engagements,
            "engagement_rate_pct": _number(engagements / followers * 100) if engagements is not None and followers else None,
        }
        image = source.get("thumbnail_bytes")
        if isinstance(image, bytes):
            post["thumbnail_bytes"] = image
        result.append(post)
        seen.add(code)
        if len(result) == 3:
            break
    return result


def project_public_media_kit(report: dict[str, Any]) -> dict[str, Any]:
    """Keep only public sales facts; require explicit public text provenance.

    The caller may enrich the original report with bounded image bytes first.
    This function performs no I/O, modifies no input, and forwards no storage
    references, private contacts, internal measurements or source inventories.
    """
    source = _mapping(report)
    original_account = _mapping(source.get("account"))
    handle = original_account.get("handle")
    handle = handle if isinstance(handle, str) and _HANDLE.fullmatch(handle) else ""
    followers = _count(original_account.get("followers"))
    account = {
        "handle": handle,
        "public_name": _public_name(original_account, handle),
        "platform": "Instagram",
        "profile_url": f"https://www.instagram.com/{handle}/" if handle else None,
        "followers": followers,
        "profile_posts": _count(original_account.get("profile_posts")),
        "verified": original_account.get("verified") if isinstance(original_account.get("verified"), bool) else None,
    }
    bio = _text(original_account.get("public_bio"), 500)
    if bio:
        account["public_bio"] = bio
    image = original_account.get("avatar_bytes")
    if isinstance(image, bytes):
        account["avatar_bytes"] = image
    generated = _date(source.get("generated_at"))
    # Internal historical totals can include hidden/deleted/undated posts.
    # Only the separately constructed public cohort may enter a sales PDF.
    summary = _mapping(source.get("public_summary"))
    best = _mapping(source.get("public_best_posts"))
    private = _flag(original_account.get("private"))
    result = {
        "generated_at": generated.isoformat(timespec="seconds") if generated else None,
        "timezone": "America/Costa_Rica",
        "account": account,
        "summary": {key: _period({"post_count": 0} if private else summary.get(key), followers) for key in ("all_time", "last_30_days")},
        "best_posts": {key: [] if private else _posts(best.get(key), followers=followers, generated=generated, recent=key == "last_30_days") for key in ("all_time", "last_30_days")},
    }
    growth = _mapping(_mapping(source.get("follower_growth")).get("30d"))
    pct = _number(growth.get("pct"), signed=True)
    if pct is not None and growth.get("observed_days") == 30:
        result["follower_growth"] = {"30d": {"pct": pct}}
    return result
