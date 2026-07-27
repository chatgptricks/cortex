from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

APIFY_ACTOR_ID = "apify~instagram-scraper"
IG_HANDLE = "chatgptricks"


class ApifySyncError(RuntimeError):
    pass


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _title_from_caption(caption: str) -> str:
    for line in caption.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:120]
    return ""


def _post_type_label(item: dict[str, Any]) -> tuple[str, bool]:
    kind = item.get("type", "")
    if kind == "Sidecar":
        return "Carousel", False
    if kind == "Video":
        return "Video", True
    return "Image", False


def _published_at(item: dict[str, Any]) -> str | None:
    ts = item.get("timestamp")
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(str(ts).replace("Z", ""), fmt)
            return dt.replace(tzinfo=UTC).isoformat(timespec="seconds")
        except ValueError:
            continue
    return str(ts)


def _apify_date_filter(value: str) -> str | None:
    """Convert a stored published_at value into the date format Apify's
    onlyPostsNewerThan expects: YYYY-MM-DDThh:mm:ssZ (no +00:00 offset).
    Returns None if the value can't be parsed, so the caller can safely
    omit the filter rather than send a request Apify will reject with 400.
    """
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_likes_floor(raw: Any) -> int:
    """Instagram/Apify sometimes reports a very low or sentinel (-1, hidden)
    like count for a post. Per product decision, any value of 3 or below
    (including missing/negative/non-numeric) is treated as "unknown" and
    reported as a baseline of 500 instead of the tiny/inaccurate number.
    """
    if isinstance(raw, bool):
        return 500
    if isinstance(raw, int) and raw > 3:
        return raw
    return 500


def _post_age_days(published_at: str | None, now: datetime) -> int | None:
    if not published_at:
        return None
    text = str(published_at).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (now - dt).days


def _is_eligible_for_engagement_refresh(published_at: str | None, now: datetime) -> bool:
    """Update rule: refresh likes/comments for posts that are 10 days old or
    less. Once a post turns 11+ days old, stop touching it -- except for one
    final check at exactly 30 days old.
    """
    age = _post_age_days(published_at, now)
    if age is None:
        return False
    return age <= 10 or age == 30


def sync_new_posts_from_apify(results_limit: int = 60) -> dict[str, Any]:
    """Pull new posts for IG_HANDLE from Apify's Instagram Scraper and insert
    any that aren't already in the posts table (matched by shortcode). Mirrors
    the insert shape sync_instagram_profile_posts() uses for IG-sourced posts
    (section="single", source_ref="instagram:<shortcode>", shortcode set),
    so these stay properly deduplicated against any future sync, unlike rows
    created through the plain POST /api/posts upload endpoint.
    """
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise ApifySyncError("APIFY_TOKEN is not configured on the server.")

    try:
        import httpx
    except ImportError as exc:
        raise ApifySyncError("httpx is not installed in the backend environment.") from exc

    from .config import UPLOAD_DIR
    from .db import connect, utc_now

    with connect() as conn:
        rows = conn.execute("SELECT shortcode, published_at FROM posts").fetchall()
    existing_shortcodes = {row["shortcode"] for row in rows if row["shortcode"]}
    known_dates = [row["published_at"] for row in rows if row["published_at"]]
    newer_than = _apify_date_filter(max(known_dates)) if known_dates else None

    payload: dict[str, Any] = {
        "directUrls": [f"https://www.instagram.com/{IG_HANDLE}/"],
        "resultsType": "posts",
        "resultsLimit": results_limit,
        "skipPinnedPosts": True,
    }
    if newer_than:
        payload["onlyPostsNewerThan"] = newer_than

    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(url, params={"token": token}, json=payload)
            response.raise_for_status()
            items = response.json()
    except httpx.HTTPError as exc:
        raise ApifySyncError(f"Apify request failed: {exc}") from exc

    if not isinstance(items, list):
        raise ApifySyncError("Apify returned an unexpected response shape.")

    new_items = [it for it in items if it.get("shortCode") and it["shortCode"] not in existing_shortcodes]
    new_items.sort(key=lambda it: it.get("timestamp") or "")

    summary: dict[str, Any] = {
        "found": len(items),
        "added": 0,
        "failed": 0,
        "items": [],
    }

    if not new_items:
        return summary

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60.0, headers={"User-Agent": "Mozilla/5.0"}) as image_client:
        for item in new_items:
            shortcode = str(item["shortCode"]).strip()
            source_ref = f"instagram:{shortcode}"
            image_url = item.get("displayUrl") or next(iter(item.get("images") or []), None)
            if not image_url:
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": "no cover image"})
                continue

            try:
                image_response = image_client.get(image_url)
                image_response.raise_for_status()
                image_bytes = image_response.content
            except httpx.HTTPError as exc:
                summary["failed"] += 1
                summary["items"].append({"shortcode": shortcode, "status": "failed", "error": str(exc)})
                continue

            suffix = ".jpg"
            content_type = image_response.headers.get("content-type", "")
            if "png" in content_type:
                suffix = ".png"
            elif "webp" in content_type:
                suffix = ".webp"

            image_path = UPLOAD_DIR / f"{os.urandom(16).hex()}{suffix}"
            image_path.write_bytes(image_bytes)

            caption = _clean_text(item.get("caption"))
            title = _title_from_caption(caption) or f"Instagram post {shortcode}"
            post_type_label, is_video = _post_type_label(item)
            likes = _apply_likes_floor(item.get("likesCount"))
            comments = item.get("commentsCount") or 0
            now = utc_now()

            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO posts (
                        section, title, caption, published_at, likes, comments,
                        post_type_label, source_ref, shortcode, image_path,
                        original_filename, status, progress_percent,
                        progress_message, is_animated, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "single",
                        title,
                        caption,
                        _published_at(item),
                        likes,
                        comments,
                        post_type_label,
                        source_ref,
                        shortcode,
                        str(image_path),
                        f"instagram-{shortcode}{suffix}",
                        "queued",
                        5,
                        "Queued",
                        int(is_video),
                        now,
                        now,
                    ),
                )

            existing_shortcodes.add(shortcode)
            summary["added"] += 1
            summary["items"].append({"shortcode": shortcode, "status": "added", "published_at": _published_at(item)})

    return summary


def refresh_recent_engagement(window_days: int = 35, results_limit: int = 200) -> dict[str, Any]:
    """Re-scrape recent posts for IG_HANDLE and update likes/comments on
    existing rows that are eligible for a refresh: posts 10 days old or
    less get refreshed every time; posts 11-29 or 31+ days old are left
    alone; posts exactly 30 days old get one final refresh. Any like count
    of 3 or below (including hidden/-1 values Apify sometimes returns) is
    floored to 500 via _apply_likes_floor.
    """
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise ApifySyncError("APIFY_TOKEN is not configured on the server.")

    try:
        import httpx
    except ImportError as exc:
        raise ApifySyncError("httpx is not installed in the backend environment.") from exc

    from .db import connect, utc_now

    now = datetime.now(UTC)

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, shortcode, published_at, likes FROM posts WHERE shortcode IS NOT NULL AND shortcode != ''"
        ).fetchall()

    eligible_by_shortcode: dict[str, dict[str, Any]] = {}
    for row in rows:
        shortcode = row["shortcode"]
        if not shortcode or shortcode.startswith("post-"):
            continue
        if not _is_eligible_for_engagement_refresh(row["published_at"], now):
            continue
        eligible_by_shortcode[shortcode] = {"id": row["id"], "likes": row["likes"]}

    summary: dict[str, Any] = {"checked": len(eligible_by_shortcode), "updated": 0, "unmatched": 0}
    if not eligible_by_shortcode:
        return summary

    window_start = now - timedelta(days=window_days)
    payload: dict[str, Any] = {
        "directUrls": [f"https://www.instagram.com/{IG_HANDLE}/"],
        "resultsType": "posts",
        "resultsLimit": results_limit,
        "skipPinnedPosts": True,
        "onlyPostsNewerThan": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    try:
        with httpx.Client(timeout=180.0) as client:
            response = client.post(url, params={"token": token}, json=payload)
            response.raise_for_status()
            items = response.json()
    except httpx.HTTPError as exc:
        raise ApifySyncError(f"Apify engagement refresh request failed: {exc}") from exc

    if not isinstance(items, list):
        raise ApifySyncError("Apify returned an unexpected response shape for engagement refresh.")

    items_by_shortcode = {it.get("shortCode"): it for it in items if it.get("shortCode")}
    now_iso = utc_now()

    for shortcode, info in eligible_by_shortcode.items():
        item = items_by_shortcode.get(shortcode)
        if not item:
            summary["unmatched"] += 1
            continue
        likes = _apply_likes_floor(item.get("likesCount"))
        comments = item.get("commentsCount")
        comments = comments if isinstance(comments, int) and comments >= 0 else None
        with connect() as conn:
            if comments is not None:
                conn.execute(
                    "UPDATE posts SET likes = ?, comments = ?, updated_at = ? WHERE id = ?",
                    (likes, comments, now_iso, info["id"]),
                )
            else:
                conn.execute(
                    "UPDATE posts SET likes = ?, updated_at = ? WHERE id = ?",
                    (likes, now_iso, info["id"]),
                )
        summary["updated"] += 1

    return summary
