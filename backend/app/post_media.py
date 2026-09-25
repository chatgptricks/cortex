"""Collects every piece of media in a post and hands it back as a ZIP.

Replaces the manual DevTools routine: open the post, dig the slide URLs out of
the embedded JSON, save each one by hand.

Two sources, in order:

1. Instagram's own public page. Free, but Instagram serves a login wall to
   datacenter IPs fairly aggressively, so on Render this is expected to fail
   more often than it succeeds. It's tried first because when it does work it
   costs nothing and returns in well under a second.
2. The Apify actor already used elsewhere in this project, with
   resultsType='details'. Reliable, ~$0.002 per post.

Video is preferred over the still whenever a slide has one -- the
poster frame is not the media. Files are passed through byte-for-byte. Instagram serves JPEG; re-encoding to
PNG would inflate each file 3-5x and recover exactly none of the quality the
original JPEG already discarded.
"""

from __future__ import annotations

import html as html_module
import io
import json
import logging
import re
import time
import threading
from datetime import UTC, datetime
import zipfile
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# A desktop UA is the difference between the post JSON and a login wall on the
# paths that still work anonymously.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class PostMediaError(RuntimeError):
    """Raised when no source could produce the post's images."""


# ---------------------------------------------------------------------------
# Source 1: Instagram's public page
# ---------------------------------------------------------------------------

def _dedupe(urls: list[str]) -> list[str]:
    """Order-preserving dedupe, keyed on the CDN path rather than the full URL.

    The same image appears several times in the page JSON under different
    signature query strings (different sizes, different expiry). Comparing full
    URLs would treat those as distinct slides and produce a ZIP with the same
    picture four times.
    """
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        key = urlparse(url).path
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return out


def _slides_from_instagram(shortcode: str) -> list[dict[str, Any]]:
    """Pulls media straight out of the post page's embedded JSON."""
    url = f"https://www.instagram.com/p/{shortcode}/"
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
        response = client.get(url)
    if response.status_code != 200:
        raise PostMediaError(f"Instagram returned HTTP {response.status_code}")

    html = response.text
    if "loginForm" in html or '"is_logged_in":false' in html and "display_url" not in html:
        raise PostMediaError("Instagram served a login wall")

    # Two layers of escaping sit between the page source and a fetchable URL,
    # and both have to come off: JSON escaping (\/ for /) because the value
    # lives in a <script> blob, and HTML entities (&amp; for &) because that
    # blob is inside markup. Leaving the entities in place yields URLs the CDN
    # rejects -- the signature is part of the query string, so a literal
    # "&amp;" corrupts it.
    def _decode(matches: list[str]) -> list[str]:
        out = []
        for candidate in matches:
            try:
                out.append(html_module.unescape(json.loads(f'"{candidate}"')))
            except json.JSONDecodeError:
                continue
        return out

    videos = _dedupe(_decode(re.findall(r'"video_url":"(https:\\?/\\?/[^"]+)"', html)))
    images = _dedupe(_decode(re.findall(r'"display_url":"(https:\\?/\\?/[^"]+)"', html)))

    # A video slide also carries a display_url -- its poster frame. The video
    # is the media; the still is only useful as a thumbnail for the picker, so
    # it rides along on the entry rather than counting as its own item.
    items: list[dict[str, Any]] = []
    for i, url in enumerate(videos):
        items.append({"kind": "video", "url": url, "poster": images[i] if i < len(images) else None})
    for url in images[len(videos):]:
        items.append({"kind": "image", "url": url, "poster": url})

    if not items:
        raise PostMediaError("No media found in the Instagram page")
    return items


# ---------------------------------------------------------------------------
# Source 2: Apify
# ---------------------------------------------------------------------------

def _slides_from_apify(shortcode: str) -> list[dict[str, Any]]:
    from .apify_sync import _run_apify_actor_and_fetch

    payload = {
        "directUrls": [f"https://www.instagram.com/p/{shortcode}/"],
        "resultsType": "details",
    }
    from .ingestion_jobs import call, IngestionPending

    def resolve():
        rows = _run_apify_actor_and_fetch(payload, max_wait_seconds=180.0)
        item = next((row for row in rows if row.get("shortCode") == shortcode), None)
        if item is None:
            raise PostMediaError("Apify returned no matching result for this post")
        media = _slides_from_item(item)
        for index, entry in enumerate(media, start=1):
            entry["index"] = index
            entry["filename"] = f"{index:02d}{_suffix_for(entry['url'], None)}"
        # Cache persistence belongs to the paid job, so a DB failure retries
        # the saved dataset instead of discarding a successful lookup.
        _cache_put(shortcode, media, "apify")
        return media

    try:
        return call(f"manual-media:{shortcode}", resolve)
    except IngestionPending as exc:
        raise PostMediaError(str(exc)) from exc


def _slides_from_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    # A carousel arrives as childPosts, one entry per slide, in order. Each
    # slide is either a video or an image; the video is the media and its
    # displayUrl is only the poster frame, so it travels as a thumbnail.
    for child in item.get("childPosts") or []:
        if not isinstance(child, dict):
            continue
        video, still = child.get("videoUrl"), child.get("displayUrl")
        if isinstance(video, str):
            items.append({"kind": "video", "url": video, "poster": still if isinstance(still, str) else None})
        elif isinstance(still, str):
            items.append({"kind": "image", "url": still, "poster": still})

    if not items:
        video, still = item.get("videoUrl"), item.get("displayUrl")
        if isinstance(video, str):
            items.append({"kind": "video", "url": video, "poster": still if isinstance(still, str) else None})
        else:
            for value in item.get("images") or []:
                if isinstance(value, str):
                    items.append({"kind": "image", "url": value, "poster": value})
            if not items and isinstance(still, str):
                items.append({"kind": "image", "url": still, "poster": still})

    seen, out = set(), []
    for it in items:
        key = urlparse(it["url"]).path
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    if not out:
        raise PostMediaError("Apify result carried no media URLs")
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Resolving a post costs an Apify run (~$0.0023) and ~10-20s, and a single
# session hits it repeatedly: open the picker, then download all, then grab one
# more file. Without this, the picker turned one paid lookup into four.
#
# The TTL is short because the CDN urls are signed and expire; 15 minutes
# comfortably covers picking through a post while staying well inside that.
_CACHE: dict[str, tuple[float, list[dict[str, Any]], str]] = {}
_CACHE_TTL_SECONDS = 900.0
_CACHE_MAX = 200


# Bounded locks coalesce requests within a process. Durable Apify leases also
# prevent duplicate paid runs between API processes.
_MEDIA_LOCKS = [threading.Lock() for _ in range(64)]


def _initialize_cache(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS post_media_cache (
        shortcode TEXT PRIMARY KEY, stored_at DOUBLE PRECISION NOT NULL,
        items_json TEXT NOT NULL, source TEXT NOT NULL)""")


def _urls_available(items: list[dict[str, Any]]) -> bool:
    """Reject expired, incomplete or login-page media before reusing it."""
    if not items:
        return False
    targets = {}
    for item in items:
        url = item.get("url")
        kind = item.get("kind")
        if not isinstance(url, str) or kind not in {"image", "video"}:
            return False
        targets[url] = kind
        if item.get("poster"):
            targets[item["poster"]] = "image"
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
            for url, kind in targets.items():
                if urlparse(url).scheme != "https":
                    return False
                response = client.head(url)
                if response.status_code != 200 or not response.headers.get("content-type", "").lower().startswith(kind + "/"):
                    return False
        return True
    except httpx.HTTPError:
        return False


def _cache_get(shortcode: str) -> tuple[list[dict[str, Any]], str] | None:
    hit = _CACHE.get(shortcode)
    if hit:
        stored_at, items, source = hit
        if time.monotonic() - stored_at < _CACHE_TTL_SECONDS:
            return [dict(it) for it in items], source
        _CACHE.pop(shortcode, None)
    from .db import connect
    with connect() as conn:
        _initialize_cache(conn)
        row = conn.execute("SELECT * FROM post_media_cache WHERE shortcode = ?", (shortcode,)).fetchone()
    if not row:
        return None
    age = time.time() - row["stored_at"]
    if age < 0 or age >= _CACHE_TTL_SECONDS:
        return None
    try:
        items = json.loads(row["items_json"])
        if not _urls_available(items):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    _memory_put(shortcode, items, row["source"], age=age)
    return [dict(it) for it in items], row["source"]


def _memory_put(shortcode, items, source, *, age=0):
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
        _CACHE.pop(oldest, None)
    _CACHE[shortcode] = (time.monotonic() - age, [dict(it) for it in items], source)


def _cache_put(shortcode: str, items: list[dict[str, Any]], source: str) -> None:
    from .db import connect
    with connect() as conn:
        _initialize_cache(conn)
        conn.execute(
            "INSERT INTO post_media_cache (shortcode, stored_at, items_json, source) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(shortcode) DO UPDATE SET stored_at = excluded.stored_at, items_json = excluded.items_json, source = excluded.source",
            (shortcode, time.time(), json.dumps(items), source),
        )
        conn.execute("DELETE FROM post_media_cache WHERE stored_at < ?", (time.time() - _CACHE_TTL_SECONDS,))
    _memory_put(shortcode, items, source)


def _saved_media(shortcode: str) -> list[dict[str, Any]] | None:
    """Reuse a recent, complete payload already paid for by ingestion.

    Likes refreshes change updated_at but not enriched_at: use the latter so
    an old media payload cannot acquire a fresh cache lifetime accidentally.
    Never substitute compressed dashboard covers for original download media.
    """
    from .db import connect
    with connect() as conn:
        rows = conn.execute(
            "SELECT raw_json, enriched_at FROM dashboard_posts WHERE shortcode = ? "
            "AND raw_json IS NOT NULL ORDER BY enriched_at DESC LIMIT 3", (shortcode,),
        ).fetchall()
    for row in rows:
        try:
            captured = datetime.fromisoformat(row["enriched_at"].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - captured).total_seconds()
            if not 0 <= age < _CACHE_TTL_SECONDS:
                continue
            raw = json.loads(row["raw_json"])
            if raw.get("shortCode") != shortcode:
                continue
            children = raw.get("childPosts") or []
            if raw.get("type") == "Sidecar" and not children:
                continue
            if any(child.get("type") == "Video" and not child.get("videoUrl") for child in children):
                continue
            if raw.get("type") == "Video" and not raw.get("videoUrl"):
                continue
            items = _slides_from_item(raw)
            if children and len(items) != len(children):
                continue
            if _urls_available(items):
                return items
        except (ValueError, TypeError, AttributeError, PostMediaError):
            continue
    return None


def collect_media(shortcode: str, *, use_cache: bool = True) -> tuple[list[dict[str, Any]], str]:
    with _MEDIA_LOCKS[hash(shortcode) % len(_MEDIA_LOCKS)]:
        return _collect_media(shortcode, use_cache=use_cache)


def _collect_media(shortcode: str, *, use_cache: bool = True) -> tuple[list[dict[str, Any]], str]:
    """Returns (items, source) for a post, trying the free path first.

    Each item is {kind, url, poster, index, filename}: everything the picker
    needs to show a thumbnail and everything the ZIP needs to name a file.
    """
    if use_cache:
        cached = _cache_get(shortcode)
        if cached:
            items, source = cached
            logger.info("post_media: %s served %d item(s) from cache", shortcode, len(items))
            return items, source

    try:
        items = _slides_from_instagram(shortcode)
        source = "instagram"
        logger.info("post_media: %s resolved %d item(s) from instagram", shortcode, len(items))
    except Exception as exc:  # noqa: BLE001 -- any failure here just means "fall back"
        logger.info("post_media: instagram path failed for %s (%s); falling back to Apify", shortcode, exc)
        items = _saved_media(shortcode) if use_cache else None
        if items is None:
            items = _slides_from_apify(shortcode)
        source = "apify"
        logger.info("post_media: %s resolved %d item(s) from apify", shortcode, len(items))

    for i, it in enumerate(items, start=1):
        it["index"] = i
        it["filename"] = f"{i:02d}{_suffix_for(it['url'], None)}"
    _cache_put(shortcode, items, source)
    return items, source


def _suffix_for(url: str, content_type: str | None) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
        if path.endswith(ext):
            return ext
    if content_type:
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "mp4" in content_type:
            return ".mp4"
    return ".jpg"


def fetch_one(url: str) -> tuple[bytes, str]:
    """Downloads a single item. Used when the picker asks for just one file --
    a lone item in a ZIP is a wrapper the person then has to undo."""
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content, _suffix_for(url, response.headers.get("content-type"))


def build_zip(
    account: str, shortcode: str, only: set[int] | None = None
) -> tuple[bytes, str, dict[str, Any]]:
    """Downloads the requested items and packs them into an in-memory ZIP.

    Downloading server-side rather than from the browser sidesteps the CDN's
    cross-origin rules entirely, and means the client gets one file instead of
    n downloads that Chrome would prompt about.

    `only` keeps the original numbering: picking slides 2 and 5 gives you
    02 and 05, not 01 and 02, so a filename still says where it sat in the post.
    """
    items, source = collect_media(shortcode)
    wanted = [it for it in items if not only or it["index"] in only]
    if not wanted:
        raise PostMediaError("None of the requested items exist in this post")

    buffer = io.BytesIO()
    failed = 0
    written = 0
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for it in wanted:
                try:
                    media = client.get(it["url"])
                    media.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("post_media: item %s of %s failed: %s", it["index"], shortcode, exc)
                    failed += 1
                    continue
                suffix = _suffix_for(it["url"], media.headers.get("content-type"))
                archive.writestr(f"{it['index']:02d}{suffix}", media.content)
                written += 1

    if not written:
        raise PostMediaError("Every item failed to download")

    filename = f"{account}-{shortcode}.zip"
    return buffer.getvalue(), filename, {"slides": written, "failed": failed, "source": source}
