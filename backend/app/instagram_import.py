from __future__ import annotations

import html
import json
import os
import pickle
import re
from datetime import UTC, datetime
from itertools import islice
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urlparse


class InstagramImportError(RuntimeError):
    pass


@dataclass
class InstagramPostImport:
    url: str
    shortcode: str
    caption: str | None
    title: str
    image_url: str
    image_bytes: bytes
    image_suffix: str
    image_content_type: str | None
    published_at: str | None = None
    likes: int | None = None
    comments: int | None = None


SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", re.IGNORECASE)
PROFILE_POST_LINK_RE = re.compile(
    r'href=[\"\']/(?P<kind>p|reel|tv)/(?P<shortcode>[A-Za-z0-9_-]+)/?(?:\?[^\"\']*)?[\"\']',
    re.IGNORECASE,
)
TIMELINE_POST_URL_RE = re.compile(
    r"/api/v1/feed/user/(?P<username>[A-Za-z0-9._]+)/username/",
    re.IGNORECASE,
)
JSON_LD_RE = re.compile(
    r"<script\s+[^>]*type=[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
EMBEDDED_IMAGE_PATTERNS = (
    re.compile(r'"display_url"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"', re.IGNORECASE),
    re.compile(r'"thumbnail_src"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"', re.IGNORECASE),
    re.compile(r'"thumbnail_url"\s*:\s*"(?P<url>(?:\\.|[^"\\])*)"', re.IGNORECASE),
)
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
IG_API_HEADERS = {
    "User-Agent": (
        "Instagram 219.0.0.12.117 Android (29/10; 420dpi; 1080x1920; "
        "samsung; SM-G973F; beyond1; exynos9820; en_US; 346138351)"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US",
    "X-IG-App-ID": "936619743392459",
}
_COOKIE_CACHE: dict[str, str] | None = None


def fetch_instagram_post(
    url: str,
    timeout: float = 25.0,
    cover_image_url: str | None = None,
) -> InstagramPostImport:
    clean_url = _canonical_instagram_url(url)
    shortcode = _shortcode(clean_url)

    try:
        import httpx
    except ImportError as exc:
        raise InstagramImportError("httpx is not installed in the backend environment.") from exc

    caption: str | None = None
    image_url: str | None = _clean_url(cover_image_url)

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=WEB_HEADERS,
        cookies=_load_instagram_cookies(),
    ) as client:
        try:
            oembed = client.get(f"https://www.instagram.com/api/v1/oembed/?url={quote_plus(clean_url)}")
            if oembed.status_code == 200:
                payload = oembed.json()
                caption = _clean_caption(payload.get("title"))
                image_url = _clean_url(payload.get("thumbnail_url")) or image_url
        except Exception:
            pass

        if not caption or not image_url:
            response = client.get(clean_url)
            if response.status_code >= 400:
                raise InstagramImportError(f"Instagram returned HTTP {response.status_code} for that post URL.")
            html_text = response.text
            meta = _extract_meta(html_text)
            caption = caption or _clean_caption(meta.get("og:description") or meta.get("description"))
            image_url = image_url or _clean_url(meta.get("og:image") or meta.get("twitter:image"))
            json_caption, json_image = _extract_json_ld(html_text)
            caption = caption or json_caption
            image_url = image_url or json_image
            image_url = image_url or _extract_embedded_image(html_text)

        if not caption or not image_url:
            embed = client.get(_instagram_embed_url(clean_url))
            if embed.status_code < 400:
                embed_text = embed.text
                meta = _extract_meta(embed_text)
                caption = caption or _clean_caption(meta.get("og:description") or meta.get("description"))
                image_url = image_url or _clean_url(meta.get("og:image") or meta.get("twitter:image"))
                json_caption, json_image = _extract_json_ld(embed_text)
                caption = caption or json_caption
                image_url = image_url or json_image
                image_url = image_url or _extract_embedded_image(embed_text)

        if not caption or not image_url:
            api_caption, api_image_url = _fetch_from_instagram_api(shortcode, timeout)
            caption = caption or api_caption
            image_url = image_url or api_image_url

        if not image_url:
            raise InstagramImportError("Could not find a cover image on the Instagram post.")

        image = client.get(image_url)
        if image.status_code >= 400:
            raise InstagramImportError(f"Instagram image download returned HTTP {image.status_code}.")
        content_type = image.headers.get("content-type")
        suffix = _image_suffix(image_url, content_type)
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise InstagramImportError(f"Instagram cover image type is not supported: {content_type or suffix}.")

    title = _title_from_caption(caption) or f"Instagram post {shortcode}"
    return InstagramPostImport(
        url=clean_url,
        shortcode=shortcode,
        caption=caption,
        title=title,
        image_url=image_url,
        image_bytes=image.content,
        image_suffix=suffix,
        image_content_type=content_type,
    )


def discover_instagram_profile_post_urls(
    profile: str,
    limit: int = 12,
    timeout: float = 25.0,
) -> list[str]:
    profile_url = _canonical_instagram_profile_url(profile)

    try:
        import httpx
    except ImportError as exc:
        raise InstagramImportError("httpx is not installed in the backend environment.") from exc

    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers=WEB_HEADERS,
        cookies=_load_instagram_cookies(),
    ) as client:
        urls = list(islice(_iter_profile_post_urls_from_timeline(client, profile_url), limit or None))
        if not urls:
            response = client.get(profile_url)
            if response.status_code >= 400:
                raise InstagramImportError(
                    f"Instagram returned HTTP {response.status_code} for that profile URL."
                )
            if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                followup = client.get(response.headers["location"])
                if followup.status_code >= 400:
                    raise InstagramImportError(
                        f"Instagram returned HTTP {followup.status_code} for that profile URL."
                    )
                response = followup
            urls = _extract_profile_post_urls(response.text)
            if not urls:
                # Instagram often rewrites the page body, so a second pass over the
                # same HTML with a stricter canonicalizer is cheap and catches odd
                # quote/encoding combinations.
                urls = _extract_profile_post_urls(html.unescape(response.text))

    if limit > 0:
        return urls[:limit]
    return urls


def sync_instagram_profile_posts(
    profile: str,
    limit: int = 12,
    dry_run: bool = False,
    analyze_now: bool = True,
    duration_seconds: int = 2,
    analyze_post: Callable[[int, int], None] | None = None,
    prune_missing: bool = False,
    stop_on_existing: bool = True,
    refresh_existing: bool = False,
    start_after_shortcode: str | None = None,
) -> dict[str, Any]:
    from .config import UPLOAD_DIR
    from .db import connect, utc_now

    summary: dict[str, Any] = {
        "profile": profile,
        "found": 0,
        "imported": 0,
        "skipped": 0,
        "updated": 0,
        "failed": 0,
        "dry_run": 0,
        "deleted": 0,
        "items": [],
    }
    keep_shortcodes: set[str] = set()
    stopped_on_existing: str | None = None
    seen_resume_anchor = start_after_shortcode is None

    try:
        import httpx
    except ImportError as exc:
        raise InstagramImportError("httpx is not installed in the backend environment.") from exc

    profile_url = _canonical_instagram_profile_url(profile)
    with httpx.Client(
        timeout=25.0,
        follow_redirects=False,
        headers=WEB_HEADERS,
        cookies=_load_instagram_cookies(),
    ) as client:
        for item in _iter_profile_timeline_items(client, profile_url):
            shortcode = str(item.get("code") or "").strip()
            if not shortcode:
                continue
            kind = "reel" if str(item.get("product_type") or "").lower().startswith("clips") else "p"
            post_url = f"https://www.instagram.com/{kind}/{shortcode}/"
            if not seen_resume_anchor:
                if shortcode == start_after_shortcode:
                    seen_resume_anchor = True
                continue
            summary["found"] += 1
            keep_shortcodes.add(shortcode)
            source_ref = f"instagram:{shortcode}"
            metadata = _timeline_item_metadata(item)
            with connect() as conn:
                existing = conn.execute("SELECT id FROM posts WHERE source_ref = ?", (source_ref,)).fetchone()
            if existing:
                summary["skipped"] += 1
                if refresh_existing and not dry_run:
                    with connect() as conn:
                        conn.execute(
                            """
                            UPDATE posts
                            SET title = ?,
                                caption = COALESCE(NULLIF(?, ''), caption),
                                published_at = COALESCE(?, published_at),
                                likes = COALESCE(?, likes),
                                comments = COALESCE(?, comments),
                                post_type_label = ?,
                                shortcode = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                metadata["title"],
                                metadata["caption"],
                                metadata["published_at"],
                                metadata["likes"],
                                metadata["comments"],
                                metadata["post_type_label"],
                                shortcode,
                                utc_now(),
                                int(existing["id"]),
                            ),
                        )
                    summary["updated"] += 1
                    summary["items"].append(
                        {
                            "url": post_url,
                            "source_ref": source_ref,
                            "status": "updated",
                            "post_id": int(existing["id"]),
                        }
                    )
                else:
                    summary["items"].append(
                        {
                            "url": post_url,
                            "source_ref": source_ref,
                            "status": "skipped",
                            "post_id": int(existing["id"]),
                        }
                    )
                if stop_on_existing:
                    stopped_on_existing = source_ref
                    break
                continue

            try:
                imported = _timeline_item_to_import(item, post_url)
            except InstagramImportError as exc:
                summary["failed"] += 1
                summary["items"].append({"url": post_url, "status": "fetch_failed", "error": str(exc)})
                continue

            if dry_run:
                summary["dry_run"] += 1
                summary["items"].append(
                    {
                        "url": post_url,
                        "source_ref": source_ref,
                        "status": "dry_run",
                        "title": imported.title,
                    }
                )
                continue

            image_path = UPLOAD_DIR / f"{os.urandom(16).hex()}{imported.image_suffix}"
            image_path.write_bytes(imported.image_bytes)
            now = utc_now()
            with connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO posts (
                        section, title, caption, published_at, likes, comments, post_type_label, source_ref, shortcode,
                        image_path, original_filename, status, progress_percent,
                        progress_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "single",
                        metadata["title"],
                        metadata["caption"],
                        metadata["published_at"],
                        metadata["likes"],
                        metadata["comments"],
                        metadata["post_type_label"],
                        source_ref,
                        shortcode,
                        str(image_path),
                        f"instagram-{imported.shortcode}{imported.image_suffix}",
                        "queued",
                        5,
                        "Queued",
                        now,
                        now,
                    ),
                )
                post_id = int(cursor.lastrowid)

            if analyze_now and analyze_post is not None:
                analyze_post(post_id, duration_seconds)

            summary["imported"] += 1
            summary["items"].append(
                {
                    "url": post_url,
                    "source_ref": source_ref,
                    "status": "imported",
                    "post_id": post_id,
                    "title": imported.title,
                    "image_path": str(image_path),
                    "analyzed": bool(analyze_now and analyze_post is not None),
                }
            )
    if prune_missing and keep_shortcodes and not stopped_on_existing:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT id, shortcode
                FROM posts
                WHERE source_ref LIKE 'instagram:%'
                """
            ).fetchall()
        for row in rows:
            shortcode = str(row["shortcode"] or "").strip()
            if not shortcode or shortcode in keep_shortcodes:
                continue
            post_id = int(row["id"])
            deleted_files = _delete_post_files(post_id)
            with connect() as conn:
                conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            summary["deleted"] += 1
            summary["items"].append(
                {
                    "post_id": post_id,
                    "source_ref": f"instagram:{shortcode}",
                    "status": "deleted",
                    "deleted_files": deleted_files,
                }
            )

    if stopped_on_existing:
        summary["stopped_on_existing"] = stopped_on_existing

    return summary


def _canonical_instagram_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise InstagramImportError("Instagram URL is required.")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") != "instagram.com":
        raise InstagramImportError("Paste a valid instagram.com post, reel, or tv URL.")
    shortcode = _shortcode(url)
    kind = parsed.path.strip("/").split("/")[0]
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def _canonical_instagram_profile_url(value: str) -> str:
    profile = value.strip()
    if not profile:
        raise InstagramImportError("Instagram profile is required.")
    if profile.startswith("@"):
        profile = profile[1:]
    if not profile.startswith(("http://", "https://")):
        profile = f"https://www.instagram.com/{profile.lstrip('/')}/"
    parsed = urlparse(profile)
    if parsed.netloc.lower().removeprefix("www.") != "instagram.com":
        raise InstagramImportError("Paste a valid instagram.com profile URL or username.")
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        raise InstagramImportError("Paste a valid Instagram profile URL or username.")
    first = path_parts[0]
    if first.lower() in {"p", "reel", "tv"}:
        raise InstagramImportError("Paste a profile username, not a post URL.")
    if not re.fullmatch(r"[A-Za-z0-9._]+", first):
        raise InstagramImportError("Instagram usernames can only contain letters, numbers, periods, and underscores.")
    return f"https://www.instagram.com/{first}/"


def _shortcode(url: str) -> str:
    match = SHORTCODE_RE.search(url)
    if not match:
        raise InstagramImportError("Could not read the Instagram shortcode from that URL.")
    return match.group(1)


def _extract_profile_post_urls(html_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in PROFILE_POST_LINK_RE.finditer(html_text):
        kind = match.group("kind").lower()
        shortcode = match.group("shortcode")
        url = f"https://www.instagram.com/{kind}/{shortcode}/"
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _iter_profile_post_urls_from_timeline(
    client: Any,
    profile_url: str,
):
    username = Path(urlparse(profile_url).path).name
    if not username:
        return
    headers = {**WEB_HEADERS, "X-IG-App-ID": IG_API_HEADERS["X-IG-App-ID"]}

    seen: set[str] = set()
    cursor: str | None = None
    page_size = 50

    while True:
        params = [f"count={page_size}"]
        if cursor:
            params.append(f"max_id={cursor}")
        request_url = f"https://www.instagram.com/api/v1/feed/user/{username}/username/?{'&'.join(params)}"
        response = client.get(request_url, headers=headers)
        if response.status_code >= 400:
            break
        try:
            payload = response.json()
        except Exception:
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            shortcode = str(item.get("code") or "").strip()
            if not shortcode:
                continue
            kind = "reel" if str(item.get("product_type") or "").lower().startswith("clips") else "p"
            url = f"https://www.instagram.com/{kind}/{shortcode}/"
            if url in seen:
                continue
            seen.add(url)
            yield url
        if not payload.get("more_available"):
            return
        next_cursor = str(payload.get("next_max_id") or "").strip()
        if not next_cursor or next_cursor == cursor:
            return
        cursor = next_cursor


def _iter_profile_timeline_items(
    client: Any,
    profile_url: str,
):
    username = Path(urlparse(profile_url).path).name
    if not username:
        return
    headers = {**WEB_HEADERS, "X-IG-App-ID": IG_API_HEADERS["X-IG-App-ID"]}

    seen: set[str] = set()
    cursor: str | None = None
    page_size = 50

    while True:
        params = [f"count={page_size}"]
        if cursor:
            params.append(f"max_id={cursor}")
        request_url = f"https://www.instagram.com/api/v1/feed/user/{username}/username/?{'&'.join(params)}"
        response = client.get(request_url, headers=headers)
        if response.status_code >= 400:
            return
        try:
            payload = response.json()
        except Exception:
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            shortcode = str(item.get("code") or "").strip()
            if not shortcode or shortcode in seen:
                continue
            seen.add(shortcode)
            yield item
        if not payload.get("more_available"):
            return
        next_cursor = str(payload.get("next_max_id") or "").strip()
        if not next_cursor or next_cursor == cursor:
            return
        cursor = next_cursor


def _timeline_item_to_import(item: dict[str, Any], post_url: str) -> InstagramPostImport:
    shortcode = str(item.get("code") or "").strip()
    if not shortcode:
        raise InstagramImportError("Instagram timeline item is missing a shortcode.")

    caption_obj = item.get("caption")
    caption_text = caption_obj.get("text") if isinstance(caption_obj, dict) else None
    caption = _clean_caption(caption_text)
    image_url = _clean_url(item.get("display_uri")) or _best_image_candidate(item)
    carousel_media = item.get("carousel_media")
    if not image_url and isinstance(carousel_media, list) and carousel_media:
        first_candidate = carousel_media[0]
        if isinstance(first_candidate, dict):
            image_url = _clean_url(first_candidate.get("display_uri")) or _best_image_candidate(first_candidate)
    if not image_url:
        raise InstagramImportError("Could not find a cover image on the Instagram timeline item.")

    try:
        import httpx
    except ImportError as exc:
        raise InstagramImportError("httpx is not installed in the backend environment.") from exc

    with httpx.Client(
        timeout=25.0,
        follow_redirects=True,
        headers=WEB_HEADERS,
        cookies=_load_instagram_cookies(),
    ) as client:
        image = client.get(image_url)
    if image.status_code >= 400:
        raise InstagramImportError(f"Instagram image download returned HTTP {image.status_code}.")

    content_type = image.headers.get("content-type")
    suffix = _image_suffix(image_url, content_type)
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise InstagramImportError(f"Instagram cover image type is not supported: {content_type or suffix}.")

    title = _title_from_caption(caption) or f"Instagram post {shortcode}"
    return InstagramPostImport(
        url=post_url,
        shortcode=shortcode,
        caption=caption,
        title=title,
        image_url=image_url,
        image_bytes=image.content,
        image_suffix=suffix,
        image_content_type=content_type,
        published_at=_timeline_item_published_at(item),
        likes=_optional_int(item.get("like_count")),
        comments=_optional_int(item.get("comment_count")),
    )


def _timeline_item_metadata(item: dict[str, Any]) -> dict[str, Any]:
    shortcode = str(item.get("code") or "").strip()
    caption_obj = item.get("caption")
    caption_text = caption_obj.get("text") if isinstance(caption_obj, dict) else None
    caption = _clean_caption(caption_text)
    title = _title_from_caption(caption)
    if not title:
        title = f"Instagram post {shortcode}" if shortcode else "Instagram post"
    product_type = str(item.get("product_type") or "").lower()
    carousel_media = item.get("carousel_media")
    is_carousel = (isinstance(carousel_media, list) and len(carousel_media) > 1) or "carousel" in product_type
    return {
        "shortcode": shortcode,
        "caption": caption,
        "title": title,
        "published_at": _timeline_item_published_at(item),
        "likes": _optional_int(item.get("like_count")),
        "comments": _optional_int(item.get("comment_count")),
        "post_type_label": "Video" if product_type.startswith("clips") else "Carousel" if is_carousel else "Image",
    }


def _timeline_item_published_at(item: dict[str, Any]) -> str | None:
    value = item.get("taken_at")
    if value is None:
        value = item.get("taken_at_timestamp")
    if value is None:
        return None
    if isinstance(value, datetime):
        timestamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return timestamp.isoformat(timespec="seconds")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None
    if not numeric:
        return None
    return datetime.fromtimestamp(numeric, tz=UTC).isoformat(timespec="seconds")


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def _fetch_profile_post_urls_from_timeline(
    client: Any,
    profile_url: str,
    limit: int,
) -> list[str]:
    urls = list(islice(_iter_profile_post_urls_from_timeline(client, profile_url), limit or None))
    return urls


def _delete_post_files(post_id: int) -> int:
    from .db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT image_path, video_path, analysis_path FROM posts WHERE id = ?",
            (post_id,),
        ).fetchone()

    if not row:
        return 0

    deleted = 0
    for field in ("image_path", "video_path", "analysis_path"):
        raw_path = row[field]
        if not raw_path:
            continue
        path = Path(str(raw_path))
        try:
            if path.exists():
                path.unlink()
                deleted += 1
        except Exception:
            continue
    return deleted


def _instagram_embed_url(url: str) -> str:
    return f"{url.rstrip('/')}/embed/captioned/"


def _fetch_from_instagram_api(shortcode: str, timeout: float) -> tuple[str | None, str | None]:
    cookies = _load_instagram_cookies()
    if not cookies:
        return None, None

    try:
        import httpx
    except ImportError as exc:
        raise InstagramImportError("httpx is not installed in the backend environment.") from exc

    media_id = _shortcode_to_media_id(shortcode)
    url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
    headers = {**IG_API_HEADERS, "Referer": f"https://www.instagram.com/p/{shortcode}/"}
    try:
        response = httpx.get(url, headers=headers, cookies=cookies, timeout=timeout, follow_redirects=True)
    except Exception:
        return None, None
    if response.status_code >= 400:
        return None, None
    try:
        payload = response.json()
    except Exception:
        return None, None

    items = payload.get("items") if isinstance(payload, dict) else None
    item = items[0] if isinstance(items, list) and items else None
    if not isinstance(item, dict):
        return None, None

    caption = _clean_caption((item.get("caption") or {}).get("text") if isinstance(item.get("caption"), dict) else None)
    first_media = item
    carousel_media = item.get("carousel_media")
    if isinstance(carousel_media, list) and carousel_media:
        first_candidate = carousel_media[0]
        if isinstance(first_candidate, dict):
            first_media = first_candidate
    return caption, _best_image_candidate(first_media)


def _shortcode_to_media_id(shortcode: str) -> str:
    media_id = 0
    for character in shortcode:
        if character not in SHORTCODE_ALPHABET:
            raise InstagramImportError("Instagram shortcode contains an unsupported character.")
        media_id = (media_id * 64) + SHORTCODE_ALPHABET.index(character)
    return str(media_id)


def _best_image_candidate(media: dict[str, Any]) -> str | None:
    candidates = (media.get("image_versions2") or {}).get("candidates")
    if not isinstance(candidates, list):
        return None
    valid = [candidate for candidate in candidates if isinstance(candidate, dict) and _clean_url(candidate.get("url"))]
    if not valid:
        return None
    best = max(valid, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
    return _clean_url(best.get("url"))


def _load_instagram_cookies() -> dict[str, str]:
    global _COOKIE_CACHE
    if _COOKIE_CACHE is not None:
        return _COOKIE_CACHE

    cookies = _load_instaloader_session_cookies()
    if not cookies:
        cookies = _load_browser_cookies()
    _COOKIE_CACHE = cookies
    return cookies


def _load_instaloader_session_cookies() -> dict[str, str]:
    for session_file in _candidate_session_files():
        try:
            raw = pickle.loads(session_file.read_bytes())
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        cookies = {
            str(name): str(value)
            for name, value in raw.items()
            if isinstance(name, str) and isinstance(value, str) and value
        }
        if cookies.get("sessionid"):
            return cookies
    return {}


def _candidate_session_files() -> list[Path]:
    env_paths = [
        os.getenv("INSTAGRAM_SESSION_FILE", ""),
        os.getenv("INSTALOADER_SESSION_FILE", ""),
    ]
    files: list[Path] = []
    for raw_path in env_paths:
        if raw_path.strip():
            path = Path(raw_path).expanduser()
            files.append(path if path.is_absolute() else Path.cwd() / path)

    app_root = Path(__file__).resolve().parents[2]
    search_roots = [
        app_root / ".instaloader",
        Path.home() / ".instaloader",
        Path.home() / "Desktop" / "Codex Projects",
    ]
    for root in search_roots:
        if not root.exists():
            continue
        if root.name == ".instaloader":
            files.extend(root.glob("session-*"))
        else:
            for depth in ("*/.instaloader/session-*", "*/*/.instaloader/session-*", "*/*/*/.instaloader/session-*"):
                files.extend(root.glob(depth))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.expanduser()
        if resolved in seen or not resolved.exists() or not resolved.is_file():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _load_browser_cookies() -> dict[str, str]:
    browser = (
        os.getenv("INSTAGRAM_COOKIE_BROWSER", "")
        or os.getenv("INSTALOADER_BROWSER", "")
        or ""
    ).strip().lower().replace("-", "_")
    if not browser:
        return {}
    try:
        import browser_cookie3
    except ImportError:
        return {}
    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        return {}
    try:
        jar = loader(domain_name=".instagram.com")
    except Exception:
        return {}
    return {
        cookie.name: cookie.value
        for cookie in jar
        if "instagram" in cookie.domain and cookie.value
    }


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attributes = {name.lower(): value for name, value in attrs if value is not None}
        name = (attributes.get("property") or attributes.get("name") or "").lower()
        content = attributes.get("content")
        if name in {"og:image", "og:description", "twitter:image", "description"} and content:
            self.values[name] = content


def _extract_meta(html_text: str) -> dict[str, str]:
    parser = _MetaParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    return parser.values


def _extract_json_ld(html_text: str) -> tuple[str | None, str | None]:
    for match in JSON_LD_RE.finditer(html_text):
        try:
            payload = json.loads(html.unescape(match.group("body")).strip())
        except Exception:
            continue
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            caption = _clean_caption(item.get("caption") or item.get("description") or item.get("name"))
            image = item.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            elif isinstance(image, dict):
                image = image.get("url")
            image_url = _clean_url(image)
            if caption or image_url:
                return caption, image_url
    return None, None


def _extract_embedded_image(html_text: str) -> str | None:
    for pattern in EMBEDDED_IMAGE_PATTERNS:
        for match in pattern.finditer(html_text):
            try:
                value = json.loads(f'"{match.group("url")}"')
            except (json.JSONDecodeError, TypeError):
                continue
            image_url = _clean_url(value)
            if image_url:
                return image_url
    return None


def _clean_caption(value: Any) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text


def _clean_url(value: Any) -> str | None:
    if value is None:
        return None
    url = html.unescape(str(value)).strip()
    return url if url.startswith("http") else None


def _image_suffix(url: str, content_type: str | None) -> str:
    if content_type:
        kind = content_type.split(";", 1)[0].strip().lower()
        if kind == "image/jpeg":
            return ".jpg"
        if kind == "image/png":
            return ".png"
        if kind == "image/webp":
            return ".webp"
    suffix = Path(urlparse(url).path).suffix.lower()
    return ".jpg" if suffix in {".jpg", ".jpeg", ""} else suffix


def _title_from_caption(caption: str | None) -> str | None:
    if not caption:
        return None
    for line in caption.splitlines():
        clean = line.strip()
        if clean:
            return clean[:120]
    return caption.strip()[:120] or None
