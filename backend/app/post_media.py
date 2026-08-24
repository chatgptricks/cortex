"""Collects every image in a post and hands it back as a ZIP.

Replaces the manual DevTools routine: open the post, dig the slide URLs out of
the embedded JSON, save each one by hand.

Two sources, in order:

1. Instagram's own public page. Free, but Instagram serves a login wall to
   datacenter IPs fairly aggressively, so on Render this is expected to fail
   more often than it succeeds. It's tried first because when it does work it
   costs nothing and returns in well under a second.
2. The Apify actor already used elsewhere in this project, with
   resultsType='details'. Reliable, ~$0.002 per post.

Images are passed through byte-for-byte. Instagram serves JPEG; re-encoding to
PNG would inflate each file 3-5x and recover exactly none of the quality the
original JPEG already discarded.
"""

from __future__ import annotations

import html as html_module
import io
import json
import logging
import re
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


def _slides_from_instagram(shortcode: str) -> list[str]:
    """Pulls slide URLs straight out of the post page's embedded JSON."""
    url = f"https://www.instagram.com/p/{shortcode}/"
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
        response = client.get(url)
    if response.status_code != 200:
        raise PostMediaError(f"Instagram returned HTTP {response.status_code}")

    html = response.text
    if "loginForm" in html or '"is_logged_in":false' in html and "display_url" not in html:
        raise PostMediaError("Instagram served a login wall")

    # display_url is the full-resolution slide. Two layers of escaping sit
    # between the page source and a fetchable URL, and both have to come off:
    # JSON escaping (\/ for /) because the value lives in a <script> blob, and
    # HTML entities (&amp; for &) because that blob is inside markup. Leaving
    # the entities in place yields URLs the CDN rejects -- the signature is
    # part of the query string, so a literal "&amp;" corrupts it.
    raw = re.findall(r'"display_url":"(https:\\?/\\?/[^"]+)"', html)
    urls = []
    for candidate in raw:
        try:
            decoded = json.loads(f'"{candidate}"')
        except json.JSONDecodeError:
            continue
        urls.append(html_module.unescape(decoded))

    urls = _dedupe(urls)
    if not urls:
        raise PostMediaError("No images found in the Instagram page")
    return urls


# ---------------------------------------------------------------------------
# Source 2: Apify
# ---------------------------------------------------------------------------

def _slides_from_apify(shortcode: str) -> list[str]:
    from .apify_sync import _run_apify_actor_and_fetch

    payload = {
        "directUrls": [f"https://www.instagram.com/p/{shortcode}/"],
        "resultsType": "details",
    }
    items = _run_apify_actor_and_fetch(payload, max_wait_seconds=180.0)
    item = next((i for i in items if isinstance(i, dict)), None)
    if not item:
        raise PostMediaError("Apify returned no result for this post")

    urls: list[str] = []
    # `images` is the carousel in order. childPosts covers the actors/versions
    # that nest slides instead, and displayUrl is the single-image case.
    for value in item.get("images") or []:
        if isinstance(value, str):
            urls.append(value)
    for child in item.get("childPosts") or []:
        if isinstance(child, dict) and isinstance(child.get("displayUrl"), str):
            urls.append(child["displayUrl"])
    if isinstance(item.get("displayUrl"), str):
        urls.append(item["displayUrl"])

    urls = _dedupe(urls)
    if not urls:
        raise PostMediaError("Apify result carried no image URLs")
    return urls


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_slide_urls(shortcode: str) -> tuple[list[str], str]:
    """Returns (urls, source) for a post, trying the free path first."""
    try:
        urls = _slides_from_instagram(shortcode)
        logger.info("post_media: %s resolved %d slide(s) from instagram", shortcode, len(urls))
        return urls, "instagram"
    except Exception as exc:  # noqa: BLE001 -- any failure here just means "fall back"
        logger.info("post_media: instagram path failed for %s (%s); falling back to Apify", shortcode, exc)

    urls = _slides_from_apify(shortcode)
    logger.info("post_media: %s resolved %d slide(s) from apify", shortcode, len(urls))
    return urls, "apify"


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


def build_zip(account: str, shortcode: str) -> tuple[bytes, str, dict[str, Any]]:
    """Downloads every slide and packs them into an in-memory ZIP.

    Downloading server-side rather than from the browser sidesteps the CDN's
    cross-origin rules entirely, and means the client gets one file instead of
    n downloads that Chrome would prompt about.
    """
    urls, source = collect_slide_urls(shortcode)

    buffer = io.BytesIO()
    failed: list[str] = []
    written = 0
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_BROWSER_HEADERS) as client:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, url in enumerate(urls, start=1):
                try:
                    media = client.get(url)
                    media.raise_for_status()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("post_media: slide %d of %s failed: %s", index, shortcode, exc)
                    failed.append(url)
                    continue
                suffix = _suffix_for(url, media.headers.get("content-type"))
                archive.writestr(f"{index:02d}{suffix}", media.content)
                written += 1

    if not written:
        raise PostMediaError("Every slide failed to download")

    filename = f"{account}-{shortcode}.zip"
    meta = {"slides": written, "failed": len(failed), "source": source}
    return buffer.getvalue(), filename, meta
