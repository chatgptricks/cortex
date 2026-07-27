from __future__ import annotations

import os
import re
from datetime import UTC, datetime
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
            likes = item.get("likesCount")
            likes = likes if isinstance(likes, int) and likes >= 0 else 0
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
